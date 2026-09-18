"""
POST /api/ai/extract-garment-views

Extract clean front + back ecommerce product shots of ONE garment that is
worn by a model or mannequin in the source photo(s).

Use case: a single garment (e.g. t-shirt, tank top, top, pant) is photographed
on a model/mannequin from the front and the back. This endpoint strips the
model, mannequin, body parts, background and props and returns the garment ONLY
as two clean white-background product images — front and back — in the same
flat-catalog style as /api/ai/moodboard-extract-products.

Because each view is grounded in its own real source photo, the two extractions
run in PARALLEL with no drift — neither view is invented. A sequential,
reference-based fallback only kicks in when one of the two view URLs is missing:
the available view is extracted first, then the opposite view is generated from
that clean result so colour/print/trims stay consistent (flagged `generated`).

Body (application/json):
  {
    "garment_type": "t-shirt",          # required, non-empty
    "image_urls": {                      # at least ONE of front/back required
      "front": "https://...",            # optional https URL
      "back":  "https://..."             # optional https URL
    }
  }

Streams over SSE so the slow images.edit calls don't trip a proxy idle timeout.
Each finished view fires a `view` event as soon as it lands.

SSE events: meta → ping (15s) → view (each finished view) → done OR error.
"""
from __future__ import annotations

import asyncio
import base64
import json
import time
from collections.abc import AsyncGenerator
from io import BytesIO
from typing import Any
from urllib.parse import urlparse

import httpx
from fastapi import APIRouter
from fastapi.responses import JSONResponse
from openai import AsyncOpenAI
from PIL import Image
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse

from moodboard_ai.config import get_settings
from moodboard_ai.log import step

router = APIRouter()


# ── Hardcoded knobs ──────────────────────────────────────────────────────────
_MODEL = "gpt-image-2"
_FALLBACK_MODEL = "gpt-image-1"
_SIZE = "1024x1792"          # portrait — standard single-garment product shot
_QUALITY = "low"
_N = 1
_EDIT_TIMEOUT_S = 600.0      # 10 min — slow gpt-image-2 edits can take 3–6 min

# gpt-image-1 doesn't accept gpt-image-2's portrait size; map to the closest.
_FALLBACK_SIZE = "1024x1536"

_VIEWS = ("front", "back")

# ── Upload / fetch limits ────────────────────────────────────────────────────
_ALLOWED_MIMES = frozenset({"image/png", "image/jpeg", "image/webp"})
_MAX_BYTES = 20 * 1024 * 1024

# ── Output encoding ──────────────────────────────────────────────────────────
# gpt-image returns a lossless PNG (several MB for a 1024x1792 product shot).
# Re-encode to lossy WebP before streaming so each `view` SSE payload drops from
# MBs to a few hundred KB. q80 is visually lossless for flat white-bg catalog
# shots; method=6 is the slowest/smallest encoder setting (fine — one frame).
_WEBP_QUALITY = 80


def _png_to_webp(png_bytes: bytes) -> bytes:
    img = Image.open(BytesIO(png_bytes)).convert("RGB")
    out = BytesIO()
    img.save(out, format="WEBP", quality=_WEBP_QUALITY, method=6)
    return out.getvalue()


# ── Prompts ──────────────────────────────────────────────────────────────────
# Adapted from _PROMPT in moodboard_extract_products.py, narrowed to a single
# named garment + a single named view, grounded in a real worn photo.
def _extract_prompt(garment_type: str, view: str) -> str:
    return f"""Produce a clean ecommerce product image of ONLY the {garment_type} worn in the input photo, shown as a {view} view.

Strip out the human model, mannequin, body parts, skin, hair, hands, shadows, props, hangers, and the background entirely.

Output (strict):
- the {garment_type} alone, {view}-facing, flat catalog product style
- centered on a pure white background, fully surrounded by white space
- no model, no mannequin, no body, no other garments or accessories

Garment fidelity:
- preserve the original texture, shape, colour, prints, seams, trims, and proportions exactly
- crisp edges, no halos, no shadow remnants
- do NOT invent details that aren't visible on the garment

Return ONLY the extracted {garment_type} as a finished product image. If other
garments or accessories are present in the photo, ignore them — output just the
{garment_type}."""


# Used only when one view is missing: generate the opposite view FROM the clean
# extracted view so colour/print/trims stay consistent (no source photo for it).
def _generate_opposite_prompt(garment_type: str, missing_view: str, source_view: str) -> str:
    return f"""The input image is a clean product shot of the {source_view} of a {garment_type} on a white background.

Render the {missing_view} of this EXACT SAME {garment_type}: identical colour, print, fabric, seams, and trims. Keep it consistent with the garment shown — do not change the design.

Output (strict):
- the {garment_type} alone, {missing_view}-facing, flat catalog product style
- centered on a pure white background, fully surrounded by white space
- no model, no mannequin, no body, crisp edges, no halos, no shadow remnants

Return ONLY the {missing_view} of the {garment_type} as a finished product image."""


# ── Request model ────────────────────────────────────────────────────────────
class _ImageUrls(BaseModel):
    front: str | None = None
    back: str | None = None


class ExtractGarmentViewsRequest(BaseModel):
    garment_type: str
    image_urls: _ImageUrls


_client: AsyncOpenAI | None = None


def _get_client() -> AsyncOpenAI:
    global _client
    if _client is not None:
        return _client
    key = get_settings().openai_api_key
    if not key:
        raise RuntimeError("OPENAI_API_KEY not configured")
    _client = AsyncOpenAI(api_key=key)
    return _client


def _filename_for(mime: str) -> str:
    if mime == "image/jpeg":
        return "image.jpg"
    if mime == "image/webp":
        return "image.webp"
    return "image.png"


def _guess_mime_from_url(url: str) -> str | None:
    try:
        ext = urlparse(url).path.lower().rsplit(".", 1)[-1]
    except Exception:
        return None
    if ext in ("jpg", "jpeg"):
        return "image/jpeg"
    if ext == "png":
        return "image/png"
    if ext == "webp":
        return "image/webp"
    return None


async def _fetch_image_url(url: str) -> tuple[bytes, str]:
    """
    Server-side fetch of an https URL into a buffer. Validates mime + size.
    Returns (buffer, mime_type). Raises ValueError with a clear message on
    failure. The server has no CORS constraint, so it can pull the S3-hosted
    source photos the FE references directly.
    """
    if not url.lower().startswith("https://"):
        raise ValueError(f'image url must start with "https://" (got {url[:32]}...)')
    async with httpx.AsyncClient() as client:
        resp = await client.get(url, timeout=15.0, follow_redirects=True)
        if resp.status_code >= 400:
            raise ValueError(
                f"image url fetch failed: {resp.status_code} {resp.reason_phrase}"
            )
        declared = (resp.headers.get("content-type") or "").split(";")[0].strip().lower()
        mime = declared if declared in _ALLOWED_MIMES else _guess_mime_from_url(url)
        if not mime or mime not in _ALLOWED_MIMES:
            raise ValueError(
                f'image url returned unsupported content-type "{declared or "(none)"}"'
            )
        body = resp.content
        if len(body) > _MAX_BYTES:
            raise ValueError(f"image url body too large ({len(body)}B > {_MAX_BYTES}B)")
        return body, mime


def _is_model_access_error(err: Exception) -> bool:
    msg = str(err).lower()
    if "model" not in msg:
        return False
    return any(w in msg for w in (
        "not found", "not available", "not verified",
        "not supported", "exist",
    )) or "invalid model" in msg or "unsupported model" in msg


async def _run_edit(image_bytes: bytes, image_mime: str, prompt: str) -> bytes:
    """
    Single images.edit call → PNG bytes. Transparently falls back to
    gpt-image-1 (closest portrait size) on a model-access error, mirroring
    moodboard_image_edit.py.
    """
    client = _get_client()

    async def call(model: str, size: str) -> Any:
        return await client.images.edit(
            model=model,
            image=(_filename_for(image_mime), image_bytes, image_mime),
            prompt=prompt,
            size=size,
            quality=_QUALITY,
            n=_N,
            timeout=_EDIT_TIMEOUT_S,
        )

    try:
        result = await asyncio.wait_for(call(_MODEL, _SIZE), timeout=_EDIT_TIMEOUT_S)
    except asyncio.TimeoutError as err:
        raise RuntimeError("Edit timed out") from err
    except Exception as err:
        if not _is_model_access_error(err):
            raise
        print(
            f"[extract-garment-views] {_MODEL} failed ({err}); "
            f"falling back to {_FALLBACK_MODEL}",
            flush=True,
        )
        try:
            result = await asyncio.wait_for(
                call(_FALLBACK_MODEL, _FALLBACK_SIZE), timeout=_EDIT_TIMEOUT_S
            )
        except asyncio.TimeoutError as err2:
            raise RuntimeError("Edit timed out") from err2

    data = getattr(result, "data", None)
    b64 = getattr(data[0], "b64_json", None) if data else None
    if not b64:
        raise RuntimeError("images.edit returned no image data")
    return base64.b64decode(b64)


@router.post("/")
async def extract_garment_views(req: ExtractGarmentViewsRequest):
    started_at = time.perf_counter()

    garment_type = (req.garment_type or "").strip()
    if not garment_type:
        return JSONResponse(
            status_code=400,
            content={"error": "'garment_type' is required (non-empty string)"},
        )

    # Collect provided views in canonical front→back order.
    raw_urls: dict[str, str] = {}
    for view in _VIEWS:
        url = (getattr(req.image_urls, view, None) or "").strip()
        if url:
            raw_urls[view] = url
    if not raw_urls:
        return JSONResponse(
            status_code=400,
            content={"error": "'image_urls' must contain at least one of 'front' / 'back'"},
        )

    # Fetch every provided source up-front so a bad URL fails as a clean 4xx
    # BEFORE the SSE stream opens (matches the other AI endpoints).
    sources: dict[str, tuple[bytes, str]] = {}
    for view, url in raw_urls.items():
        try:
            sources[view] = await _fetch_image_url(url)
        except Exception as err:
            return JSONResponse(
                status_code=400,
                content={"error": f"'{view}' image url: {err}" or "image url fetch failed"},
            )

    t = step("extract-garment-views", "request", {
        "model": _MODEL,
        "size": _SIZE,
        "quality": _QUALITY,
        "garmentType": garment_type,
        "views": list(sources.keys()),
        "missing": [v for v in _VIEWS if v not in sources],
    })

    async def event_generator() -> AsyncGenerator[dict[str, Any], None]:
        yield {
            "event": "meta",
            "data": json.dumps({"model": _MODEL, "size": _SIZE, "views": list(_VIEWS)}),
        }

        try:
            grounded = [v for v in _VIEWS if v in sources]
            missing = [v for v in _VIEWS if v not in sources]
            results: dict[str, bytes] = {}

            # ── 1. Grounded views: extract in PARALLEL (no drift) ────────────
            async def extract_grounded(view: str) -> tuple[str, bytes]:
                buf, mime = sources[view]
                png = await _run_edit(buf, mime, _extract_prompt(garment_type, view))
                return view, png

            for coro in asyncio.as_completed(
                [extract_grounded(v) for v in grounded]
            ):
                view, png = await coro
                results[view] = png
                webp = _png_to_webp(png)
                view_ms = int((time.perf_counter() - started_at) * 1000)
                yield {
                    "event": "view",
                    "data": json.dumps({
                        "view": view,
                        "src": f"data:image/webp;base64,{base64.b64encode(webp).decode('ascii')}",
                        "generated": False,
                        "ms": view_ms,
                    }),
                }

            # ── 2. Missing view (fallback): generate from the clean opposite ─
            for view in missing:
                source_view = next((v for v in _VIEWS if v in results), None)
                if source_view is None:
                    break  # nothing to base it on (shouldn't happen — ≥1 grounded)
                png = await _run_edit(
                    results[source_view],
                    "image/png",
                    _generate_opposite_prompt(garment_type, view, source_view),
                )
                results[view] = png
                webp = _png_to_webp(png)
                view_ms = int((time.perf_counter() - started_at) * 1000)
                yield {
                    "event": "view",
                    "data": json.dumps({
                        "view": view,
                        "src": f"data:image/webp;base64,{base64.b64encode(webp).decode('ascii')}",
                        "generated": True,
                        "ms": view_ms,
                    }),
                }

            elapsed_ms = int((time.perf_counter() - started_at) * 1000)
            yield {
                "event": "done",
                "data": json.dumps({"count": len(results), "ms": elapsed_ms}),
            }
            t.done({"ms": elapsed_ms, "count": len(results)})

        except asyncio.CancelledError:
            t.fail("client disconnected")
            raise
        except asyncio.TimeoutError:
            t.fail("Edit timed out")
            yield {
                "event": "error",
                "data": json.dumps({"error": "Edit timed out", "retriable": True}),
            }
        except Exception as err:
            t.fail(err)
            msg = str(err)
            lower = msg.lower()
            aborted = "aborted" in lower or "disconnected" in lower
            print(
                f"[extract-garment-views] failed: name={type(err).__name__} "
                f"message={msg}",
                flush=True,
            )
            yield {
                "event": "error",
                "data": json.dumps({
                    "error": "Extraction cancelled" if aborted else (msg or "extraction failed"),
                    "code": getattr(err, "code", None),
                    "type": getattr(err, "type", None),
                    "retriable": not aborted,
                }),
            }

    return EventSourceResponse(event_generator(), ping=15)
