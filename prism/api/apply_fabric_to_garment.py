"""
POST /api/ai/apply-fabric-to-garment

Re-make an existing garment in a different fabric. Two images go in — a STYLE
image (the garment) and a FABRIC image (a swatch) — and one image comes out:
the same garment, design untouched, cut and sewn from the supplied cloth. The
garment's main-body colour becomes the swatch's colour and its self-fabric
details (collar, cuffs, pockets, belt, plackets) come with it; hardware stays
hardware.

ENGINE: a DIRECT two-image `client.images.edit` call — no gpt-5 host-model
planning turn, no streamed partials. Edit-mode is the right semantics here
because the STYLE image is a canvas that must survive: the design is the thing
the caller is paying to keep. The fabric rides along as the second attachment,
which the contract labels explicitly as material-reference-only so the model
never confuses which of the two it is meant to reproduce.

FABRIC FIDELITY: OpenAI downscales every input image, which is exactly what
destroys a fine rib and brings it back as corduroy. The fabric is therefore
sent as a MACRO CENTRE CROP (see services/image_macro), and the contract tells
the model that image 2 is magnified so it scales the structure back down to
real apparel size. Pass `fabricMacro: false` to send the swatch untouched.

Inputs (JSON body, or multipart/form-data with the same field names):
  • garmentImage / garmentImageUrl : the style image  (exactly one)
  • fabricImage  / fabricImageUrl  : the fabric swatch (exactly one)
      → each accepts an uploaded file (multipart), a {"mimeType","data"}
        base64 object, or an https:// URL the server fetches.
  • prompt      : optional refinements about how the fabric is applied.
                  Anything that would change the garment's design is ignored
                  by the contract.
  • imageModel  : "openai" (gpt-image-1) / "openai-1.5" / "openai-2" (default).
  • size        : 1024x1024 | 1024x1792 | 1792x1024, mapped per model.
                  Omitted → derived from the garment image's own aspect ratio
                  so a tall packshot doesn't come back squared off.
  • quality     : "low" | "medium" | "high" (default) | "auto". High by
                  default: the weave surviving into the render IS the product.
  • fabricMacro : bool, default true.

SSE events (single final image, no partials):
  meta → final | error
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
# Starlette's UploadFile, not FastAPI's: `form.get()` hands back the former,
# and `fastapi.UploadFile` is a *subclass* of it — so isinstance() against the
# FastAPI one is always False and the whole multipart path silently dies as
# "no image supplied". (Only applies to hand-parsed forms; the `file:
# UploadFile = File(...)` annotation style used elsewhere is unaffected.)
from starlette.datastructures import UploadFile

from moodboard_ai.config import get_settings
from moodboard_ai.log import step
from moodboard_ai.services.fabric_apply_contract import compose_fabric_apply_contract
from moodboard_ai.services.image_macro import make_macro_crop
from moodboard_ai.services.reference_images import ALLOWED_MIMES, MAX_BYTES

router = APIRouter()

_SOFT_HINT = (
    "Output ONE image: the garment from image 1 — same design, same details, "
    "same view, same background — made of the cloth from image 2."
)

# ── Direct images.edit knobs (mirror change-fabric-color) ────────────────────
# "high" rather than that endpoint's "low": a recolour survives a cheap render,
# but reproducing a knit structure does not.
_DEFAULT_QUALITY = "high"
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
# gpt-image-2 edit runs to ~300s at this repo's measured P95 (openai_images.
# _TIMEOUTS); quality=high on a two-image edit sits at the top of that band.
_EDIT_TIMEOUT_S = 600.0

# Anything beyond this ratio is clearly a portrait/landscape packshot rather
# than a square one, so the output bucket should follow it.
_PORTRAIT_RATIO = 1.2


def _resolve_model_from_image_model(image_model: str | None) -> str:
    k = (image_model or "").strip()
    if k == "openai":
        return "gpt-image-1"
    if k == "openai-1.5":
        return "gpt-image-1.5"
    return _DEFAULT_MODEL


def _aspect_size_for(raw: bytes) -> str | None:
    """Pick the canonical size bucket matching the garment's aspect ratio.

    Returns a gpt-image-2 bucket name (remapped later for gpt-image-1), or None
    if the bytes can't be measured — the caller then falls back to square.
    """
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
        print(f"[apply-fabric-to-garment] aspect probe skipped: {err}", flush=True)
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


async def _fetch_image_url(url: str, field: str) -> tuple[bytes, str]:
    """Fetch an https image URL server-side. Returns (bytes, mime)."""
    if not url.lower().startswith("https://"):
        raise ValueError(f'{field} must start with "https://"')
    async with httpx.AsyncClient() as client:
        resp = await client.get(url, timeout=15.0, follow_redirects=True)
        if resp.status_code >= 400:
            raise ValueError(
                f"{field} fetch failed: {resp.status_code} {resp.reason_phrase}"
            )
    declared = (resp.headers.get("content-type") or "").split(";")[0].strip().lower()
    mime = declared if declared in ALLOWED_MIMES else _guess_mime_from_url(url)
    if not mime or mime not in ALLOWED_MIMES:
        raise ValueError(
            f'{field} returned unsupported content-type "{declared or "(none)"}"'
        )
    body = resp.content
    if len(body) > MAX_BYTES:
        raise ValueError(f"{field} body too large ({len(body)}B > {MAX_BYTES}B)")
    return body, mime


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


class ApplyFabricRequest(BaseModel):
    garmentImage: dict[str, str] | None = None
    garmentImageUrl: str | None = None
    fabricImage: dict[str, str] | None = None
    fabricImageUrl: str | None = None
    prompt: str | None = None
    imageModel: str | None = None
    size: str | None = None
    quality: str | None = None
    fabricMacro: bool | None = None


def _coerce_json(value: Any) -> Any:
    """Form fields arrive as strings; parse the ones that should be JSON
    objects. Leaves plain strings / None alone."""
    if not isinstance(value, str):
        return value
    s = value.strip()
    if not s:
        return None
    if s[0] in "[{":
        try:
            return json.loads(s)
        except Exception:
            return value
    return value


def _coerce_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        s = value.strip().lower()
        if s in ("true", "1", "yes", "on"):
            return True
        if s in ("false", "0", "no", "off"):
            return False
    return None


def _resolve_inline(obj: dict[str, str] | None, field: str) -> tuple[bytes, str]:
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


def _resolve_upload(buf: bytes, mime: str, field: str) -> tuple[bytes, str]:
    """Validate a multipart upload. Raises ValueError."""
    if mime not in ALLOWED_MIMES:
        raise ValueError(f"{field} file: unsupported type '{mime}' (use JPG, PNG, WebP)")
    if not buf:
        raise ValueError(f"{field} file is empty")
    if len(buf) > MAX_BYTES:
        raise ValueError(f"{field} file too large ({len(buf)}B > {MAX_BYTES}B)")
    return buf, mime


@router.post("/")
async def apply_fabric_to_garment(req: Request):
    started_at = time.perf_counter()

    if not get_settings().openai_api_key:
        return JSONResponse(
            status_code=500, content={"error": "OpenAI API key not configured"}
        )

    # ── Parse inputs from EITHER JSON body OR multipart/form-data ────────────
    # multipart lets the client upload both images as files; JSON keeps the
    # URL / base64 paths. Both share the same field names.
    ctype = (req.headers.get("content-type") or "").lower()
    uploads: dict[str, tuple[bytes, str]] = {}

    if "multipart/form-data" in ctype:
        form = await req.form()
        inline: dict[str, Any] = {}
        for field in ("garmentImage", "fabricImage"):
            value = form.get(field)
            if isinstance(value, UploadFile):
                uploads[field] = (
                    await value.read(),
                    (value.content_type or "").lower(),
                )
            else:
                parsed = _coerce_json(value)
                inline[field] = parsed if isinstance(parsed, dict) else None
        body = ApplyFabricRequest(
            garmentImage=inline.get("garmentImage"),
            garmentImageUrl=(form.get("garmentImageUrl") or None),
            fabricImage=inline.get("fabricImage"),
            fabricImageUrl=(form.get("fabricImageUrl") or None),
            prompt=(form.get("prompt") or None),
            imageModel=(form.get("imageModel") or None),
            size=(form.get("size") or None),
            quality=(form.get("quality") or None),
            fabricMacro=_coerce_bool(form.get("fabricMacro")),
        )
    else:
        try:
            raw = await req.json()
        except Exception:
            return JSONResponse(
                status_code=400,
                content={"error": "body must be JSON or multipart/form-data"},
            )
        if not isinstance(raw, dict):
            return JSONResponse(
                status_code=400, content={"error": "JSON body must be an object"}
            )
        body = ApplyFabricRequest(**raw)

    # ── Resolve both images (uploaded file / base64 object / https URL) ──────
    sources: dict[str, str] = {}
    resolved: dict[str, tuple[bytes, str]] = {}
    for field, url_field, inline_obj, url_value in (
        ("garmentImage", "garmentImageUrl", body.garmentImage, body.garmentImageUrl),
        ("fabricImage", "fabricImageUrl", body.fabricImage, body.fabricImageUrl),
    ):
        has_file = field in uploads
        has_inline = bool(inline_obj)
        has_url = bool((url_value or "").strip())
        if sum([has_file, has_inline, has_url]) != 1:
            return JSONResponse(
                status_code=400,
                content={
                    "error": f"provide exactly one of: uploaded file '{field}', "
                    f"'{field}' (base64 object), or '{url_field}' (https)"
                },
            )
        try:
            if has_file:
                buf, mime = uploads[field]
                resolved[field] = _resolve_upload(buf, mime, field)
                sources[field] = "file"
            elif has_inline:
                resolved[field] = _resolve_inline(inline_obj, field)
                sources[field] = "inline"
            else:
                resolved[field] = await _fetch_image_url((url_value or "").strip(), url_field)
                sources[field] = "url"
        except ValueError as err:
            return JSONResponse(status_code=400, content={"error": str(err)})
        except Exception as err:
            return JSONResponse(
                status_code=400,
                content={"error": str(err) or f"{field} could not be resolved"},
            )

    garment_bytes, garment_mime = resolved["garmentImage"]
    fabric_bytes, fabric_mime = resolved["fabricImage"]

    # ── Macro-crop the swatch so the weave survives OpenAI's downscale ───────
    want_macro = True if body.fabricMacro is None else bool(body.fabricMacro)
    fabric_is_macro = False
    if want_macro:
        macro = make_macro_crop(fabric_bytes)
        if macro:
            fabric_bytes, fabric_mime = macro, "image/png"
            fabric_is_macro = True

    # ── Build the edit prompt (contract carries the preservation lock) ───────
    prompt = (body.prompt or "").strip()
    contract = compose_fabric_apply_contract(
        prompt=prompt, fabric_is_macro=fabric_is_macro
    )
    edit_prompt = f"{contract}\n\n{_SOFT_HINT}"
    if len(edit_prompt) > 32_000:
        edit_prompt = edit_prompt[:32_000]

    quality = body.quality if body.quality in _ALLOWED_QUALITIES else _DEFAULT_QUALITY
    primary_model = _resolve_model_from_image_model(body.imageModel)
    # An unspecified size follows the garment's own shape rather than squaring
    # a tall packshot off — the contract promises the same framing.
    requested_size = (body.size or "").strip() or _aspect_size_for(garment_bytes)
    primary_size = _pick_size_for_model(requested_size, primary_model)

    t = step("apply-fabric-to-garment", "request", {
        "engine": "images.edit",
        "model": primary_model,
        "size": primary_size,
        "sizeSource": "client" if (body.size or "").strip() else "aspect",
        "quality": quality,
        "garmentSource": sources["garmentImage"],
        "fabricSource": sources["fabricImage"],
        "garmentBytes": len(garment_bytes),
        "fabricBytes": len(fabric_bytes),
        "fabricMacro": fabric_is_macro,
        "promptLen": len(edit_prompt),
        "userPromptLen": len(prompt),
    })

    async def event_generator() -> AsyncGenerator[dict[str, Any], None]:
        yield {
            "event": "meta",
            "data": json.dumps({
                "model": primary_model,
                "size": primary_size,
                "quality": quality,
                "fabricMacro": fabric_is_macro,
            }, ensure_ascii=False),
        }

        async def run_edit(model: str, sz: str) -> Any:
            client = _get_client()
            # Order is load-bearing: image 1 is the canvas the contract tells
            # the model to preserve, image 2 the material reference.
            return await client.images.edit(
                model=model,
                image=[
                    (_filename_for("garment", garment_mime), garment_bytes, garment_mime),
                    (_filename_for("fabric", fabric_mime), fabric_bytes, fabric_mime),
                ],
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
                    f"[apply-fabric-to-garment] {primary_model} failed ({err}); "
                    f"falling back to {_FALLBACK_MODEL}",
                    flush=True,
                )
                model_used = _FALLBACK_MODEL
                size_used = _pick_size_for_model(requested_size, _FALLBACK_MODEL)
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

            elapsed_ms = int((time.perf_counter() - started_at) * 1000)
            yield {
                "event": "final",
                "data": json.dumps({
                    "src": f"data:image/png;base64,{b64}",
                    "model": model_used,
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
                f"[apply-fabric-to-garment] failed: name={type(err).__name__} message={msg}",
                flush=True,
            )
            yield {
                "event": "error",
                "data": json.dumps({
                    "error": "Edit cancelled" if aborted else (msg or "fabric application failed"),
                    "retriable": not aborted,
                }, ensure_ascii=False),
            }

    return EventSourceResponse(event_generator(), ping=15)
