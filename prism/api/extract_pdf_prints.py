"""
POST /api/ai/extract-pdf-prints

Turn a design deck / lookbook PDF into named print swatches — one close-up image
per print in the deck, each with a name, motif, technique and colour reading.

All the actual work lives in moodboard_ai/services/pdf_prints.py; this route is
only the transport. It accepts the PDF four ways so the FE, a worker and a curl
one-liner can all use it without a wrapper:

  1. multipart/form-data   pdf=<file>            (browser upload)
  2. application/json      {"pdfUrl": "https://..."}
  3. application/json      {"pdfBase64": "JVBERi0x..."}
  4. application/pdf       raw bytes as the body  (curl --data-binary)

Options may be sent as form fields, JSON keys, or query params — query params
win, so the raw-body form can still be tuned:

  includeGarmentCrops  bool  default true   crop prints off garments/models too
  includeReferences    bool  default true   keep inspiration-section images
  cleanLowRes          bool  default true   re-render small artwork bigger
  frameSwatches        bool  default true   trim + centre each print on a square
  maxPages             int   default all
  maxPrints            int   default all    biggest candidates first
  techniques           csv   default the service's vocabulary

Streams over SSE because a 50-page deck runs ~120 classify calls plus a
gpt-image-2 edit per low-res print, which no proxy will hold open silently. Each
print is delivered the moment it is named rather than at the end.

SSE events:
  meta      → {pages, uniqueImages, candidates, deckNames, models, ...}
  print     → {print: {...}}  one per print, as soon as it lands
  progress  → {done, total, prints}
  ping      → every 15s (keep-alive)
  done      → {prints, skipped: {reason: count}, ms}
  error     → fatal stream error

A print looks like:
  {id, name, nameSource, motif, motifFamily, technique, placement, scale,
   colourCount, colourNames, dominantColours, description, confidence,
   image, originalImage?, duplicateOf, derivation, cleaned, lowRes,
   hasLettering, isReference, section, pages, sourceWidth, sourceHeight,
   contentWidth, contentHeight, framed, trimmed, storedPpi,
   nameNote?, cleanSkipped?, cleanError?}

`nameSource` is the field to read before trusting a name: "deck_text" means the
deck named it, "artwork_text" means it was read out of the artwork's own pixels,
"generated" means we invented it. `cleaned: true` means `image` was re-rendered
by gpt-image-2 and is therefore a redraw — `originalImage` carries the
designer's untouched pixels in that case. `cleanSkipped` means the print was
low-res but too small to enlarge without inventing detail, so it shipped its
real pixels; `duplicateOf` means this is another view of the print with that id.

`framed: true` means `image` is a square SWATCH_CANVAS_PX canvas with the print
trimmed out of its garment/paper ground and centred on it — a crop and a paste,
never a repaint. `contentWidth`/`contentHeight` are the artwork's real pixel
dimensions inside that canvas, so read those (not the canvas) to judge
resolution. Pass frameSwatches=false to get the untrimmed image instead.
"""
from __future__ import annotations

import asyncio
import base64
import json
import time
from collections.abc import AsyncGenerator
from typing import Any
from urllib.parse import urlparse

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from sse_starlette.sse import EventSourceResponse

from moodboard_ai.log import step
from moodboard_ai.services.pdf_prints import (
    DEFAULT_TECHNIQUES,
    ExtractOptions,
    extract_prints,
)

router = APIRouter()

# ── Upload / fetch limits ────────────────────────────────────────────────────
# A print-heavy deck with full-bleed photography runs large; 64 MB comfortably
# covers a 50-page lookbook while still refusing an accidental video upload.
_MAX_PDF_BYTES = 64 * 1024 * 1024
_FETCH_TIMEOUT_S = 60.0
_PDF_MIMES = frozenset({"application/pdf", "application/x-pdf", "application/octet-stream"})
_PDF_MAGIC = b"%PDF"


def _err(message: str, status: int = 400) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": message})


def _as_bool(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in ("1", "true", "yes", "on"):
        return True
    if text in ("0", "false", "no", "off"):
        return False
    return default


def _as_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        parsed = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _as_techniques(value: Any) -> tuple[str, ...]:
    """Accept a JSON array or a comma-separated list; fall back to the default."""
    if isinstance(value, (list, tuple)):
        items = [str(v).strip() for v in value]
    elif isinstance(value, str) and value.strip():
        items = [p.strip() for p in value.split(",")]
    else:
        return DEFAULT_TECHNIQUES
    cleaned = tuple(i for i in items if i)
    return cleaned or DEFAULT_TECHNIQUES


def _looks_like_pdf(data: bytes) -> bool:
    """Some producers emit a few junk bytes before %PDF — allow a short lead-in."""
    return _PDF_MAGIC in data[:1024]


async def _fetch_pdf(url: str) -> bytes:
    """
    Server-side fetch of an https PDF. No CORS constraint here, so it can pull
    the same file-server URL the FE only has a link to.

    A wrong content-type is not fatal on its own — plenty of file servers send
    octet-stream for PDFs — so the %PDF magic is the real gate.
    """
    if not url.lower().startswith("https://"):
        raise ValueError('pdfUrl must start with "https://"')
    async with httpx.AsyncClient() as client:
        resp = await client.get(url, timeout=_FETCH_TIMEOUT_S, follow_redirects=True)
        if resp.status_code >= 400:
            raise ValueError(
                f"pdfUrl fetch failed: {resp.status_code} {resp.reason_phrase}"
            )
        body = resp.content
        if len(body) > _MAX_PDF_BYTES:
            raise ValueError(
                f"pdf too large ({len(body)}B > {_MAX_PDF_BYTES}B)"
            )
        declared = (resp.headers.get("content-type") or "").split(";")[0].strip().lower()
        path_is_pdf = urlparse(url).path.lower().endswith(".pdf")
        if not _looks_like_pdf(body):
            if declared and declared not in _PDF_MIMES and not path_is_pdf:
                raise ValueError(
                    f'pdfUrl returned "{declared}", which is not a PDF'
                )
            raise ValueError("pdfUrl body is not a PDF (no %PDF header)")
        return body


async def _read_input(request: Request) -> tuple[bytes, dict[str, Any], str]:
    """
    Resolve the PDF bytes plus the raw option bag, whichever way it arrived.

    Returns (pdf_bytes, options, how) — `how` is only for logging.
    Raises ValueError with a caller-facing message on any bad input.
    """
    content_type = (request.headers.get("content-type") or "").split(";")[0].strip().lower()
    raw_opts: dict[str, Any] = {}
    data: bytes | None = None
    how = content_type or "(none)"

    if content_type == "multipart/form-data":
        form = await request.form()
        raw_opts = {k: v for k, v in form.items() if not hasattr(v, "read")}
        upload = None
        for key in ("pdf", "file", "document"):
            candidate = form.get(key)
            if candidate is not None and hasattr(candidate, "read"):
                upload = candidate
                break
        if upload is not None:
            data = await upload.read()
            how = "multipart"
        else:
            url = raw_opts.get("pdfUrl") or raw_opts.get("pdf_url")
            if not url:
                raise ValueError(
                    "multipart body needs a 'pdf' file field or a 'pdfUrl' field"
                )
            data = await _fetch_pdf(str(url))
            how = "multipart+url"

    elif content_type == "application/json":
        body = await request.body()
        try:
            parsed = json.loads(body or b"{}")
        except json.JSONDecodeError as err:
            raise ValueError(f"invalid JSON body: {err}") from err
        if not isinstance(parsed, dict):
            raise ValueError("JSON body must be an object")
        raw_opts = parsed
        url = parsed.get("pdfUrl") or parsed.get("pdf_url")
        b64 = parsed.get("pdfBase64") or parsed.get("pdf_base64")
        if url:
            data = await _fetch_pdf(str(url))
            how = "json+url"
        elif b64:
            payload = str(b64)
            if payload.startswith("data:"):
                payload = payload.split(",", 1)[-1]
            # Whitespace is stripped (wrapped base64 is common) but the decode
            # is then STRICT. With validate=False a payload of pure garbage
            # decodes to b"" and the caller is told "no PDF supplied", which
            # sends them looking for a missing field instead of a broken
            # encoding.
            payload = "".join(payload.split())
            try:
                data = base64.b64decode(payload, validate=True)
            except Exception as err:
                raise ValueError(f"pdfBase64 is not valid base64: {err}") from err
            how = "json+base64"
        else:
            raise ValueError("JSON body needs 'pdfUrl' or 'pdfBase64'")

    else:
        data = await request.body()
        how = "raw-body"
        if not data:
            raise ValueError(
                "no PDF supplied — send multipart 'pdf', JSON 'pdfUrl'/"
                "'pdfBase64', or the PDF bytes as the request body"
            )

    if not data:
        raise ValueError("no PDF supplied")
    if len(data) > _MAX_PDF_BYTES:
        raise ValueError(f"pdf too large ({len(data)}B > {_MAX_PDF_BYTES}B)")
    if not _looks_like_pdf(data):
        raise ValueError("the supplied bytes are not a PDF (no %PDF header)")

    # Query params override whatever the body said, so the raw-bytes form is
    # still configurable.
    raw_opts.update(dict(request.query_params))
    return data, raw_opts, how


def _build_options(raw: dict[str, Any]) -> ExtractOptions:
    def pick(*keys: str) -> Any:
        for key in keys:
            if key in raw and raw[key] not in (None, ""):
                return raw[key]
        return None

    return ExtractOptions(
        include_garment_crops=_as_bool(
            pick("includeGarmentCrops", "include_garment_crops"), True
        ),
        include_references=_as_bool(
            pick("includeReferences", "include_references"), True
        ),
        clean_low_res=_as_bool(pick("cleanLowRes", "clean_low_res"), True),
        frame_swatches=_as_bool(pick("frameSwatches", "frame_swatches"), True),
        techniques=_as_techniques(pick("techniques")),
        max_pages=_as_int(pick("maxPages", "max_pages")),
        max_prints=_as_int(pick("maxPrints", "max_prints")),
    )


@router.post("/")
async def extract_pdf_prints(request: Request):
    started_at = time.perf_counter()

    try:
        pdf_bytes, raw_opts, how = await _read_input(request)
    except ValueError as err:
        return _err(str(err))
    except Exception as err:  # fetch/transport failure — caller-facing message
        return _err(f"could not read the PDF: {err}")

    opts = _build_options(raw_opts)

    t = step("extract-pdf-prints", "request", {
        "via": how,
        "bytes": len(pdf_bytes),
        "crops": opts.include_garment_crops,
        "refs": opts.include_references,
        "clean": opts.clean_low_res,
    })

    async def event_generator() -> AsyncGenerator[dict[str, Any], None]:
        prints = 0
        try:
            async for event in extract_prints(pdf_bytes, opts):
                kind = event.pop("type", None)
                if kind == "print":
                    prints += 1
                    yield {"event": "print", "data": json.dumps(event)}
                elif kind == "meta":
                    yield {"event": "meta", "data": json.dumps(event)}
                elif kind == "progress":
                    yield {"event": "progress", "data": json.dumps(event)}
                elif kind == "summary":
                    yield {"event": "done", "data": json.dumps(event)}
            t.done({
                "prints": prints,
                "ms": int((time.perf_counter() - started_at) * 1000),
            })

        except asyncio.CancelledError:
            t.fail("client disconnected")
            raise
        except Exception as err:
            t.fail(err)
            msg = str(err)
            lower = msg.lower()
            aborted = "aborted" in lower or "disconnected" in lower
            print(
                f"[extract-pdf-prints] failed: "
                f"name={type(err).__name__} message={msg}",
                flush=True,
            )
            yield {
                "event": "error",
                "data": json.dumps({
                    "error": "Extraction cancelled" if aborted
                             else (msg or "extraction failed"),
                    "code": getattr(err, "code", None),
                    "type": getattr(err, "type", None),
                    "retriable": not aborted,
                }),
            }

    return EventSourceResponse(event_generator(), ping=15)
