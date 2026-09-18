"""
POST /api/ai/generate-individual-garment

Edit ONE existing garment from a generated styles set. `generate-styles-set`
plans and renders a whole range in one pass; this endpoint is the follow-up —
a user refining a single product afterwards without replanning the set. There
is no planning stage here: the caller sends back the full product record
`generate-styles-set` returned for this garment plus the edit instructions,
and this endpoint returns the SAME shape with whatever the edit changed
rewritten and the rest passed through untouched.

ENGINE: a DIRECT `client.images.edit` call — the same choice as
apply-fabric-to-garment and change-fabric-color, and for the same reason: the
current garment image is a CANVAS that must survive, and edit-mode is the
right semantics for that (an attachment is material to reproduce, not a seed
to riff on). No gpt-5 host-model turn, no streamed partials.

Body (JSON):
  currentImage:       the garment image to edit — https URL, data: URL, or
                       {"mimeType","data"} base64 object. Required.
  styleCode:           <text> optional passthrough identifier (SKU etc.),
                       echoed back verbatim.

  The rest of the product record generate-styles-set returned for this
  garment, echoed straight back from the caller. Every field is optional
  here and is passed through UNCHANGED in the response except where the edit
  below rewrites it:
    index, gender, styleCategory, garmentName, reason, fabricCode, fabBatch,
    fabricName, fabricRationale, description, colour, secondaryColour,
    colourTreatment, printLabel, printApplication, signatureDetails,
    designBrief.

  selectedFabric:      {"fabCode", "fabBatch"?}  optional. Requests a fabric
                       swap. MUST match an entry in `fabricListOptions` on
                       (fabCode, fabBatch) — anything else is a plain 400
                       before the stream opens. One code may be listed under
                       several batches (the same quality, one roll per shade),
                       so sending `fabBatch` is how a caller names the exact
                       roll; omitting it falls back to the first entry under
                       that code. The swatch is fetched from THAT matched
                       entry, never trusted off `selectedFabric` itself, and
                       attached as a macro crop exactly like
                       apply-fabric-to-garment. Not requesting a fabric change
                       means no fabric image is attached at all — nothing for
                       the model to accidentally reproduce.
  selectedPrint:       {"printName"/"label", "imageUrl"?}  optional. Requests
                       a print change. Matched against `printListOptions` by
                       label when possible, and its artwork attached as a
                       reference. A print outside that list is fine too —
                       describe it in `prompt` instead (no image reference
                       required) and it is designed from that text.
  fabricListOptions:   [{"fabCode","fabBatch","quality","gsm","composition",
                       "imageUrl","name","color"}]   The garment's valid
                       fabric choices — same shape as generate-styles-set's
                       `fabrics`. HARD LIMIT for `selectedFabric`.
  printListOptions:    [{"printName","imageUrl"}]  Same shape as
                       generate-styles-set's `printDirection`. NOT a hard
                       limit — see `selectedPrint` above.
  prompt:              <text> optional free-form edit instructions — sleeve
                       length, trims, an off-list print description, any
                       other tweak. Layered on top of the fabric/print
                       decisions above; never overrides the resolved fabric
                       or print identity.
  imageModel:          "openai" (gpt-image-1) / "openai-1.5" / "openai-2"
                       (default).
  quality:             "low" | "medium" (default) | "high" | "auto".

FABRIC FIDELITY: exactly the apply-fabric-to-garment discipline — the swatch
is attached as a MACRO CENTRE CROP (services/image_macro) so its weave/knit
survives OpenAI's input downscale, and it rides along ONLY when a fabric
change is actually requested.

SSE events (single final image, no partials):
  meta → final | error

Validation failures (missing currentImage, a fabric code outside
fabricListOptions, missing API key) are returned as a plain JSON 400/500
before the stream opens.
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
from openai import AsyncOpenAI
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse

from moodboard_ai.config import get_settings
from moodboard_ai.log import step
from moodboard_ai.services.image_macro import make_macro_crop
from moodboard_ai.services.individual_garment_contract import (
    compose_individual_garment_contract,
)
from moodboard_ai.services.reference_images import ALLOWED_MIMES, MAX_BYTES
from moodboard_ai.services.styles_set_design import (
    fab_batch,
    fabric_code,
    fabric_colour,
    normalize_fabrics,
    normalize_prints,
)
from moodboard_ai.services.styles_set_design import print_label as _print_label_of

router = APIRouter()

_SOFT_HINT = (
    "Output ONE image: the garment from IMAGE 1, edited exactly as instructed above — "
    "same design, same view, same background — and nothing else changed."
)

# ── Direct images.edit knobs (mirror apply-fabric-to-garment) ────────────────
# "medium" rather than that endpoint's "high": a single-garment edit is a quick
# iteration loop for the caller, and quality=high on gpt-image-2 nearly doubled
# latency (~1.8min observed) for a fidelity gain that matters less here than it
# does on the first-generation packshot. Callers that want max fidelity can
# still pass quality="high" explicitly.
_DEFAULT_QUALITY = "medium"
_ALLOWED_QUALITIES = frozenset({"low", "medium", "high", "auto"})
_N = 1
_DEFAULT_MODEL = "gpt-image-2"
_FALLBACK_MODEL = "gpt-image-1"

_GPTIMG2_SIZES = frozenset({"1024x1024", "1024x1792", "1792x1024"})
_GPTIMG1_SIZE_MAP = {
    "1024x1024": "1024x1024",
    "1024x1792": "1024x1536",
    "1792x1024": "1536x1024",
}
_GPTIMG1_SIZE_SET = frozenset(_GPTIMG1_SIZE_MAP.values())
_DEFAULT_SIZE = "1024x1024"
_EDIT_TIMEOUT_S = 600.0
_PORTRAIT_RATIO = 1.2


def _resolve_model_from_image_model(image_model: str | None) -> str:
    k = (image_model or "").strip()
    if k == "openai":
        return "gpt-image-1"
    if k == "openai-1.5":
        return "gpt-image-1.5"
    return _DEFAULT_MODEL


def _aspect_size_for(raw: bytes) -> str | None:
    """Pick the canonical size bucket matching the garment's aspect ratio."""
    try:
        import io

        from PIL import Image

        with Image.open(io.BytesIO(raw)) as im:
            w, h = im.size
        if not w or not h:
            return None
        if h >= w * _PORTRAIT_RATIO:
            return "1024x1792"
        if w >= h * _PORTRAIT_RATIO:
            return "1792x1024"
        return "1024x1024"
    except Exception as err:
        print(f"[generate-individual-garment] aspect probe skipped: {err}", flush=True)
        return None


def _pick_size_for_model(input_size: str | None, model: str) -> str:
    trimmed = (input_size or "").strip()
    if model == "gpt-image-2":
        if trimmed in _GPTIMG2_SIZES:
            return trimmed
        if trimmed == "1024x1536":
            return "1024x1792"
        if trimmed == "1536x1024":
            return "1792x1024"
        return _DEFAULT_SIZE
    if trimmed in _GPTIMG1_SIZE_MAP:
        return _GPTIMG1_SIZE_MAP[trimmed]
    if trimmed in _GPTIMG1_SIZE_SET:
        return trimmed
    return _DEFAULT_SIZE


def _is_model_access_error(err: Exception) -> bool:
    msg = str(err).lower()
    if "model" not in msg:
        return False
    return any(w in msg for w in (
        "not found", "not available", "not verified", "not supported", "exist",
    )) or "invalid model" in msg or "unsupported model" in msg


def _filename_for(stem: str, mime: str) -> str:
    if mime == "image/jpeg":
        return f"{stem}.jpg"
    if mime == "image/webp":
        return f"{stem}.webp"
    return f"{stem}.png"


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


async def _fetch_https(url: str, field: str) -> tuple[bytes, str]:
    """Fetch an https image URL server-side. Returns (bytes, mime). Raises ValueError."""
    async with httpx.AsyncClient() as client:
        resp = await client.get(url, timeout=15.0, follow_redirects=True)
        if resp.status_code >= 400:
            raise ValueError(f"{field} fetch failed: {resp.status_code} {resp.reason_phrase}")
    declared = (resp.headers.get("content-type") or "").split(";")[0].strip().lower()
    mime = declared if declared in ALLOWED_MIMES else _guess_mime_from_url(url)
    if not mime or mime not in ALLOWED_MIMES:
        raise ValueError(f'{field} returned unsupported content-type "{declared or "(none)"}"')
    body = resp.content
    if len(body) > MAX_BYTES:
        raise ValueError(f"{field} body too large ({len(body)}B > {MAX_BYTES}B)")
    return body, mime


def _decode_inline(obj: dict[str, Any], field: str) -> tuple[bytes, str]:
    """Decode a {"mimeType", "data"} base64 object. Raises ValueError."""
    mime = str((obj or {}).get("mimeType") or "").lower()
    data = str((obj or {}).get("data") or "")
    if mime not in ALLOWED_MIMES:
        raise ValueError(f"{field}: unsupported type '{mime}' (use JPG, PNG, WebP)")
    try:
        buf = base64.b64decode(data)
    except Exception as err:
        raise ValueError(f"{field}: data is not valid base64") from err
    if not buf:
        raise ValueError(f"{field}: empty data")
    if len(buf) > MAX_BYTES:
        raise ValueError(f"{field} too large ({len(buf)}B > {MAX_BYTES}B)")
    return buf, mime


def _decode_data_url(url: str, field: str) -> tuple[bytes, str]:
    """Decode a `data:image/...;base64,...` URL. Raises ValueError."""
    try:
        header, payload = url.split(",", 1)
        mime = header[5:].split(";")[0].strip().lower() or "image/png"
        buf = base64.b64decode(payload, validate=True)
    except Exception as err:
        raise ValueError(f"{field}: malformed data URL") from err
    if mime not in ALLOWED_MIMES:
        raise ValueError(f"{field}: unsupported type '{mime}' (use JPG, PNG, WebP)")
    if not buf:
        raise ValueError(f"{field}: empty data")
    if len(buf) > MAX_BYTES:
        raise ValueError(f"{field} too large ({len(buf)}B > {MAX_BYTES}B)")
    return buf, mime


async def _resolve_image_source(value: Any, field: str) -> tuple[bytes, str] | None:
    """
    Accepts an https:// URL, a data: URL, or a {"mimeType","data"} object —
    the three shapes this codebase's fabric/print/garment fields all arrive
    in. Returns None when `value` is absent/empty (a non-fatal "not
    supplied"); raises ValueError when it is present but malformed or
    unfetchable, which the caller turns into a 400.
    """
    if isinstance(value, dict):
        if not value:
            return None
        return _decode_inline(value, field)
    if isinstance(value, str):
        v = value.strip()
        if not v:
            return None
        if v.startswith("data:"):
            return _decode_data_url(v, field)
        if v.lower().startswith("https://"):
            return await _fetch_https(v, field)
        raise ValueError(f"{field} must be an https:// URL, a data: URL, or {{mimeType,data}}")
    return None


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


class IndividualGarmentRequest(BaseModel):
    currentImage: Any = None
    styleCode: str | None = None

    # Passthrough product record — see module docstring.
    index: int | None = None
    gender: str = ""
    styleCategory: str = ""
    garmentName: str = ""
    reason: str = ""
    fabricCode: str = ""
    fabBatch: str | None = None
    fabricName: str = ""
    fabricRationale: str = ""
    description: str = ""
    colour: dict[str, Any] | None = None
    secondaryColour: dict[str, Any] | None = None
    colourTreatment: str = ""
    printLabel: str = ""
    printApplication: str = ""
    signatureDetails: list[str] = []
    designBrief: str = ""

    # Edit instructions.
    selectedFabric: dict[str, Any] | None = None
    selectedPrint: dict[str, Any] | None = None
    fabricListOptions: list[dict[str, Any]] = []
    printListOptions: list[dict[str, Any]] = []
    prompt: str = ""

    imageModel: str | None = None
    quality: str | None = None


@router.post("")
@router.post("/")
async def generate_individual_garment(req: Request):
    started_at = time.perf_counter()

    if not get_settings().openai_api_key:
        return JSONResponse(status_code=500, content={"error": "OpenAI API key not configured"})

    try:
        raw = await req.json()
    except Exception:
        return JSONResponse(status_code=400, content={"error": "body must be JSON"})
    if not isinstance(raw, dict):
        return JSONResponse(status_code=400, content={"error": "JSON body must be an object"})
    body = IndividualGarmentRequest(**raw)

    # ── the canvas: currentImage is the one required input ───────────────────
    try:
        resolved_current = await _resolve_image_source(body.currentImage, "currentImage")
    except ValueError as err:
        return JSONResponse(status_code=400, content={"error": str(err)})
    if resolved_current is None:
        return JSONResponse(
            status_code=400,
            content={"error": "currentImage is required (https URL, data: URL, or {mimeType,data})"},
        )
    garment_bytes, garment_mime = resolved_current

    # ── fabric change: HARD-validated against fabricListOptions ──────────────
    # `selectedFabric` only names WHICH ENTRY the caller wants — the swatch
    # itself always comes from the matched fabricListOptions entry, never
    # trusted off the caller's own object, exactly like the enum re-snap in
    # styles_set_design._validate_product.
    #
    # The entry is addressed by (fabCode, fabBatch), not by code alone. One
    # code can appear several times in the list — the same quality dyed in
    # several shades, one roll each — and the caller here is not a planner
    # choosing from an enum but a user who already picked an exact roll off
    # the wizard. Matching on code alone would hand back whichever roll was
    # listed first, render the wrong shade, and (worse) book the style against
    # a lot the design was never approved on — see `fab_batch`.
    resolved_fabric_entry: dict[str, Any] | None = None
    fabric_bytes: bytes | None = None
    fabric_mime: str | None = None
    fabric_is_macro = False

    if body.selectedFabric:
        requested_code = fabric_code(body.selectedFabric)
        if not requested_code:
            return JSONResponse(status_code=400, content={"error": "selectedFabric is missing a fabric code"})
        requested_batch = fab_batch(body.selectedFabric)

        # Matched on the RAW list: normalize_fabrics collapses on code alone,
        # so normalizing first would drop every roll but the first and put the
        # entry being asked for out of reach. The winner is normalized on its
        # own afterwards, giving the identical record shape downstream.
        candidates = [
            f for f in body.fabricListOptions
            if isinstance(f, dict) and fabric_code(f).lower() == requested_code.lower()
        ]
        if not candidates:
            return JSONResponse(
                status_code=400,
                content={"error": f"selectedFabric.fabCode '{requested_code}' is not in fabricListOptions"},
            )
        if requested_batch:
            candidates = [
                f for f in candidates if fab_batch(f).lower() == requested_batch.lower()
            ]
            if not candidates:
                return JSONResponse(
                    status_code=400,
                    content={
                        "error": (
                            f"selectedFabric fabCode '{requested_code}' / fabBatch "
                            f"'{requested_batch}' is not in fabricListOptions"
                        )
                    },
                )
        matched = normalize_fabrics(candidates[:1])
        resolved_fabric_entry = matched[0] if matched else None
        if resolved_fabric_entry is None:
            return JSONResponse(
                status_code=400,
                content={"error": f"selectedFabric.fabCode '{requested_code}' is not in fabricListOptions"},
            )
        if not resolved_fabric_entry.get("imageUrl"):
            return JSONResponse(
                status_code=400,
                content={"error": f"fabricListOptions entry '{resolved_fabric_entry['code']}' has no imageUrl"},
            )
        try:
            fetched = await _resolve_image_source(resolved_fabric_entry["imageUrl"], "selectedFabric image")
        except ValueError as err:
            return JSONResponse(status_code=400, content={"error": str(err)})
        if fetched is None:
            return JSONResponse(status_code=400, content={"error": "selectedFabric image could not be resolved"})
        fabric_bytes, fabric_mime = fetched
        macro = make_macro_crop(fabric_bytes)
        if macro:
            fabric_bytes, fabric_mime = macro, "image/png"
            fabric_is_macro = True

    fabric_change_meta: dict[str, Any] | None = None
    if resolved_fabric_entry:
        spec_bits = [b for b in (
            f"quality {resolved_fabric_entry['quality']}" if resolved_fabric_entry.get("quality") else "",
            f"{resolved_fabric_entry['gsm']} GSM" if resolved_fabric_entry.get("gsm") else "",
            resolved_fabric_entry.get("composition") or "",
        ) if b]
        fabric_change_meta = {
            "name": resolved_fabric_entry["name"],
            "spec": f" ({', '.join(spec_bits)})" if spec_bits else "",
            "is_macro": fabric_is_macro,
        }

    # ── print change: matched against printListOptions when possible, ────────
    # otherwise left unattached — an off-list print is carried as a name hint
    # here and, per the caller's own free text, fully described in `prompt`.
    print_options = normalize_prints(body.printListOptions)
    print_change_meta: dict[str, Any] | None = None
    print_bytes: bytes | None = None
    print_mime: str | None = None

    if body.selectedPrint:
        requested_label = _print_label_of(body.selectedPrint)
        match = None
        if requested_label:
            match = next(
                (p for p in print_options if p["label"].lower() == requested_label.lower()), None
            )
        if match:
            resolved_label = match["label"]
            image_url = match["imageUrl"]
        else:
            resolved_label = requested_label or "Print"
            image_url = str(
                body.selectedPrint.get("imageUrl")
                or body.selectedPrint.get("printImage")
                or body.selectedPrint.get("image")
                or ""
            ).strip()
        application = str(
            body.selectedPrint.get("application") or body.selectedPrint.get("printApplication") or ""
        ).strip()

        fetched = None
        if image_url:
            try:
                fetched = await _resolve_image_source(image_url, "selectedPrint image")
            except ValueError:
                # An off-list print with a broken/unfetchable reference degrades
                # to a text-only description rather than failing the whole edit —
                # only fabric identity is a hard requirement.
                fetched = None
        if fetched is not None:
            print_bytes, print_mime = fetched

        print_change_meta = {
            "label": resolved_label,
            "attached": fetched is not None,
            "application": application,
        }

    # ── compose the edit prompt ───────────────────────────────────────────────
    contract = compose_individual_garment_contract(
        garment_name=body.garmentName or "this garment",
        style_category=body.styleCategory or "garment",
        fabric_change=fabric_change_meta,
        print_change=print_change_meta,
        prompt=body.prompt,
    )
    edit_prompt = f"{contract}\n\n{_SOFT_HINT}"
    if len(edit_prompt) > 32_000:
        edit_prompt = edit_prompt[:32_000]

    quality = body.quality if body.quality in _ALLOWED_QUALITIES else _DEFAULT_QUALITY
    primary_model = _resolve_model_from_image_model(body.imageModel)
    primary_size = _pick_size_for_model(_aspect_size_for(garment_bytes), primary_model)

    t = step("generate-individual-garment", "request", {
        "engine": "images.edit",
        "model": primary_model,
        "size": primary_size,
        "quality": quality,
        "garment": body.garmentName or "-",
        "styleCode": body.styleCode or "-",
        "fabricChange": resolved_fabric_entry["code"] if resolved_fabric_entry else None,
        "printChange": print_change_meta["label"] if print_change_meta else None,
        "printAttached": bool(print_change_meta and print_change_meta["attached"]),
        "promptLen": len(body.prompt or ""),
    })

    async def event_generator() -> AsyncGenerator[dict[str, Any], None]:
        yield {
            "event": "meta",
            "data": json.dumps({
                "model": primary_model,
                "size": primary_size,
                "quality": quality,
                "fabricChange": resolved_fabric_entry["code"] if resolved_fabric_entry else None,
                "printChange": print_change_meta["label"] if print_change_meta else None,
            }, ensure_ascii=False),
        }

        async def run_edit(model: str, sz: str) -> Any:
            client = _get_client()
            images: list[tuple[str, bytes, str]] = [
                (_filename_for("garment", garment_mime), garment_bytes, garment_mime)
            ]
            if fabric_bytes:
                images.append((_filename_for("fabric", fabric_mime), fabric_bytes, fabric_mime))
            if print_bytes:
                images.append((_filename_for("print", print_mime), print_bytes, print_mime))
            return await client.images.edit(
                model=model,
                image=images,
                prompt=edit_prompt,
                size=sz,
                quality=quality,
                n=_N,
                timeout=_EDIT_TIMEOUT_S,
            )

        model_used = primary_model
        size_used = primary_size

        try:
            if await req.is_disconnected():
                t.fail("client disconnected")
                return

            try:
                result = await asyncio.wait_for(
                    run_edit(primary_model, primary_size), timeout=_EDIT_TIMEOUT_S
                )
            except asyncio.TimeoutError as err:
                raise RuntimeError("Edit timed out") from err
            except Exception as err:
                if not _is_model_access_error(err) or primary_model == _FALLBACK_MODEL:
                    raise
                print(
                    f"[generate-individual-garment] {primary_model} failed ({err}); "
                    f"falling back to {_FALLBACK_MODEL}",
                    flush=True,
                )
                model_used = _FALLBACK_MODEL
                size_used = _pick_size_for_model(_aspect_size_for(garment_bytes), _FALLBACK_MODEL)
                try:
                    result = await asyncio.wait_for(
                        run_edit(_FALLBACK_MODEL, size_used), timeout=_EDIT_TIMEOUT_S
                    )
                except asyncio.TimeoutError as err:
                    raise RuntimeError("Edit timed out") from err

            b64 = None
            data = getattr(result, "data", None)
            if data:
                b64 = getattr(data[0], "b64_json", None)
            if not b64:
                raise RuntimeError("images.edit returned no image data")

            # ── rewrite the passthrough record: only what the edit changed ──────
            colour = body.colour
            fab_code_out = body.fabricCode
            fab_batch_out = body.fabBatch
            fab_name_out = body.fabricName
            if resolved_fabric_entry:
                colour = fabric_colour(resolved_fabric_entry)
                fab_code_out = resolved_fabric_entry["code"]
                fab_batch_out = resolved_fabric_entry["batch"] or None
                fab_name_out = resolved_fabric_entry["name"]

            print_label_out = body.printLabel
            print_application_out = body.printApplication
            if print_change_meta:
                print_label_out = print_change_meta["label"]
                if print_change_meta.get("application"):
                    print_application_out = print_change_meta["application"]

            elapsed_ms = int((time.perf_counter() - started_at) * 1000)
            product = {
                "index": body.index,
                "gender": body.gender,
                "styleCategory": body.styleCategory,
                "garmentName": body.garmentName,
                "styleCode": body.styleCode,
                "reason": body.reason,
                "fabricCode": fab_code_out,
                "fabBatch": fab_batch_out,
                "fabricName": fab_name_out,
                "fabricRationale": body.fabricRationale,
                "description": body.description,
                "colour": colour,
                "secondaryColour": body.secondaryColour,
                "colourTreatment": body.colourTreatment,
                "printLabel": print_label_out,
                "printApplication": print_application_out,
                "signatureDetails": body.signatureDetails,
                "designBrief": body.designBrief,
                "image": f"data:image/png;base64,{b64}",
                "imageModel": model_used,
            }

            yield {
                "event": "final",
                "data": json.dumps({
                    **product,
                    "size": size_used,
                    "quality": quality,
                    "ms": elapsed_ms,
                }, ensure_ascii=False),
            }
            t.done({"ms": elapsed_ms, "modelUsed": model_used, "sizeUsed": size_used})

        except asyncio.CancelledError:
            t.fail("client disconnected")
            raise
        except Exception as err:
            t.fail(err)
            msg = str(err)
            lower = msg.lower()
            aborted = "aborted" in lower or "disconnected" in lower
            print(
                f"[generate-individual-garment] failed: name={type(err).__name__} message={msg}",
                flush=True,
            )
            yield {
                "event": "error",
                "data": json.dumps({
                    "error": "Edit cancelled" if aborted else (msg or "garment edit failed"),
                    "retriable": not aborted,
                }, ensure_ascii=False),
            }

    return EventSourceResponse(event_generator(), ping=15)
