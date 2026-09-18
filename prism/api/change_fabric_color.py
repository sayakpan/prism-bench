"""
POST /api/ai/change-fabric-color

Recolour a single fabric image (a swatch / flat material shot) using a supplied
colour palette, while preserving the weave/knit structure, stitch detail and the
geometry of any woven/printed pattern exactly.

FAST PATH: unlike the generate-moodboard endpoints (which route through a gpt-5
host model + streamed partial images), this calls `client.images.edit` DIRECTLY
— one shot, no host-model planning turn, no progressive renders. That both cuts
latency and preserves the source pixels (knit/pattern) better, which is exactly
what a recolour wants. Same gpt-image-2 → gpt-image-1 access fallback.

Inputs (JSON body, or multipart/form-data with the same field names):
  • fabricImageUrl : "https://..."  server fetches it                  (option A)
  • fabricImage    : {"mimeType": "...", "data": "<raw base64>"}       (option B)
  • fabricImage    : an uploaded file, multipart only                  (option C)
      → supply exactly one of the three.
  • colors         : [{"hex": "#RRGGBB", "pantone": "19-4052 TCX", "name": "..."}]
      → array of colour objects; Pantone resolved from hex when missing.
  • prompt         : optional colour-only refinements (e.g. "create three colour
                     stripes"). Colour names bind strictly to the palette;
                     structure / pattern-geometry changes requested here are ignored.
  • imageModel     : "openai" / "openai-1.5" / "openai-2" (default).
  • size           : 1024x1024 | 1024x1792 | 1792x1024 (mapped per model).
  • quality        : "low" (default, fastest) | "medium" | "high" | "auto".

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
from moodboard_ai.services.fabric_recolor_contract import compose_fabric_recolor_contract
from moodboard_ai.services.pantone import resolve_pantone_code
from moodboard_ai.services.reference_images import ALLOWED_MIMES, MAX_BYTES

router = APIRouter()

_SOFT_HINT = (
    "Output ONE image: the same fabric, same weave/knit, same pattern geometry — "
    "recoloured using only the palette colours."
)

# ── Direct images.edit knobs (mirror moodboard-image-edit) ────────────────────
_DEFAULT_QUALITY = "low"   # fastest; recolour rarely needs more
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


def _resolve_model_from_image_model(image_model: str | None) -> str:
    k = (image_model or "").strip()
    if k == "openai":
        return "gpt-image-1"
    if k == "openai-1.5":
        return "gpt-image-1.5"
    return _DEFAULT_MODEL


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


def _filename_for(mime: str) -> str:
    if mime == "image/jpeg":
        return "fabric.jpg"
    if mime == "image/webp":
        return "fabric.webp"
    return "fabric.png"


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
    """Fetch an https image URL server-side. Returns (bytes, mime)."""
    if not url.lower().startswith("https://"):
        raise ValueError('fabricImageUrl must start with "https://"')
    async with httpx.AsyncClient() as client:
        resp = await client.get(url, timeout=15.0, follow_redirects=True)
        if resp.status_code >= 400:
            raise ValueError(
                f"fabricImageUrl fetch failed: {resp.status_code} {resp.reason_phrase}"
            )
    declared = (resp.headers.get("content-type") or "").split(";")[0].strip().lower()
    mime = declared if declared in ALLOWED_MIMES else _guess_mime_from_url(url)
    if not mime or mime not in ALLOWED_MIMES:
        raise ValueError(
            f'fabricImageUrl returned unsupported content-type "{declared or "(none)"}"'
        )
    body = resp.content
    if len(body) > MAX_BYTES:
        raise ValueError(f"fabricImageUrl body too large ({len(body)}B > {MAX_BYTES}B)")
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


class ChangeFabricColorRequest(BaseModel):
    fabricImage: dict[str, str] | None = None
    fabricImageUrl: str | None = None
    colors: list[dict[str, Any]] | None = None
    color: dict[str, Any] | list[dict[str, Any]] | None = None
    prompt: str | None = None
    imageModel: str | None = None
    size: str | None = None
    quality: str | None = None


def _coerce_json(value: Any) -> Any:
    """Form fields arrive as strings; parse ones that should be JSON (colors,
    color, fabricImage). Leaves plain strings/None alone."""
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


@router.post("/")
async def change_fabric_color(req: Request):
    started_at = time.perf_counter()

    if not get_settings().openai_api_key:
        return JSONResponse(
            status_code=500, content={"error": "OpenAI API key not configured"}
        )

    # ── Parse inputs from EITHER JSON body OR multipart/form-data ────────────
    # multipart lets the client upload the fabric as a file (field: fabricImage);
    # JSON keeps the URL / base64 paths. Both share the same field names.
    ctype = (req.headers.get("content-type") or "").lower()
    upload_bytes: bytes | None = None
    upload_mime: str | None = None

    if "multipart/form-data" in ctype:
        form = await req.form()
        file_field = form.get("fabricImage")
        if isinstance(file_field, UploadFile):
            upload_bytes = await file_field.read()
            upload_mime = (file_field.content_type or "").lower()
            fabric_image_json = None
        else:
            # fabricImage supplied as a base64 JSON string in a form field.
            fabric_image_json = _coerce_json(file_field)
        body = ChangeFabricColorRequest(
            fabricImage=fabric_image_json if isinstance(fabric_image_json, dict) else None,
            fabricImageUrl=(form.get("fabricImageUrl") or None),
            colors=_coerce_json(form.get("colors")),
            color=_coerce_json(form.get("color")),
            prompt=(form.get("prompt") or None),
            imageModel=(form.get("imageModel") or None),
            size=(form.get("size") or None),
            quality=(form.get("quality") or None),
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
            return JSONResponse(status_code=400, content={"error": "JSON body must be an object"})
        body = ChangeFabricColorRequest(**raw)

    # ── Resolve the fabric image (uploaded file / base64 object / https URL) ──
    has_file = upload_bytes is not None
    has_inline = bool(body.fabricImage)
    has_url = bool((body.fabricImageUrl or "").strip())
    if sum([has_file, has_inline, has_url]) != 1:
        return JSONResponse(
            status_code=400,
            content={
                "error": "provide exactly one of: uploaded file 'fabricImage', "
                "'fabricImage' (base64 object), or 'fabricImageUrl' (https)"
            },
        )

    image_buffer: bytes
    image_mime: str
    if has_file:
        image_buffer = upload_bytes or b""
        image_mime = upload_mime or ""
        if image_mime not in ALLOWED_MIMES:
            return JSONResponse(
                status_code=400,
                content={"error": f"fabricImage file: unsupported type '{image_mime}' (use JPG, PNG, WebP)"},
            )
        if not image_buffer:
            return JSONResponse(status_code=400, content={"error": "fabricImage file is empty"})
        if len(image_buffer) > MAX_BYTES:
            return JSONResponse(
                status_code=400,
                content={"error": f"fabricImage file too large ({len(image_buffer)}B > {MAX_BYTES}B)"},
            )
    elif has_inline:
        mime = str((body.fabricImage or {}).get("mimeType") or "").lower()
        data = str((body.fabricImage or {}).get("data") or "")
        if mime not in ALLOWED_MIMES:
            return JSONResponse(
                status_code=400,
                content={"error": f"fabricImage: unsupported type '{mime}' (use JPG, PNG, WebP)"},
            )
        try:
            image_buffer = base64.b64decode(data)
        except Exception:
            return JSONResponse(
                status_code=400, content={"error": "fabricImage: data is not valid base64"}
            )
        if not image_buffer:
            return JSONResponse(status_code=400, content={"error": "fabricImage: empty data"})
        if len(image_buffer) > MAX_BYTES:
            return JSONResponse(
                status_code=400,
                content={"error": f"fabricImage too large ({len(image_buffer)}B > {MAX_BYTES}B)"},
            )
        image_mime = mime
    else:
        try:
            image_buffer, image_mime = await _fetch_image_url(body.fabricImageUrl.strip())
        except Exception as err:
            return JSONResponse(
                status_code=400,
                content={"error": str(err) or "fabricImageUrl fetch failed"},
            )

    # ── Normalise palette + resolve Pantone ──────────────────────────────────
    raw_palette: list[Any] = []
    if isinstance(body.colors, list):
        raw_palette = body.colors
    elif isinstance(body.color, list):
        raw_palette = body.color
    elif isinstance(body.color, dict):
        raw_palette = [body.color]
    palette = [c for c in raw_palette if isinstance(c, dict)]

    prompt = (body.prompt or "").strip()
    if not palette and not prompt:
        return JSONResponse(
            status_code=400,
            content={"error": "provide a 'colors' array and/or a 'prompt'"},
        )

    resolved_palette = [{**c, "pantone": resolve_pantone_code(c)} for c in palette]

    # ── Build the edit prompt (contract carries the preservation lock) ───────
    contract = compose_fabric_recolor_contract(colors=resolved_palette, prompt=prompt)
    edit_prompt = f"{contract}\n\n{_SOFT_HINT}"
    if len(edit_prompt) > 32_000:
        edit_prompt = edit_prompt[:32_000]

    quality = body.quality if body.quality in _ALLOWED_QUALITIES else _DEFAULT_QUALITY
    primary_model = _resolve_model_from_image_model(body.imageModel)
    primary_size = _pick_size_for_model(body.size, primary_model)

    pantone_list = [c.get("pantone") for c in resolved_palette if c.get("pantone")]
    palette_names = [
        n for n in (str(c.get("name") or "").strip() for c in resolved_palette) if n
    ]

    t = step("change-fabric-color", "request", {
        "engine": "images.edit",
        "model": primary_model,
        "size": primary_size,
        "quality": quality,
        "source": "file" if has_file else ("url" if has_url else "inline"),
        "imageBytes": len(image_buffer),
        "imageMime": image_mime,
        "paletteSize": len(resolved_palette),
        "paletteNames": palette_names,
        "pantones": pantone_list,
        "promptLen": len(edit_prompt),
    })

    async def event_generator() -> AsyncGenerator[dict[str, Any], None]:
        yield {
            "event": "meta",
            "data": json.dumps({
                "model": primary_model,
                "size": primary_size,
                "quality": quality,
                "pantones": pantone_list or None,
            }, ensure_ascii=False),
        }

        async def run_edit(model: str, sz: str) -> Any:
            client = _get_client()
            return await client.images.edit(
                model=model,
                image=(_filename_for(image_mime), image_buffer, image_mime),
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
                    f"[change-fabric-color] {primary_model} failed ({err}); "
                    f"falling back to {_FALLBACK_MODEL}",
                    flush=True,
                )
                model_used = _FALLBACK_MODEL
                size_used = _pick_size_for_model(body.size, _FALLBACK_MODEL)
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
                f"[change-fabric-color] failed: name={type(err).__name__} message={msg}",
                flush=True,
            )
            yield {
                "event": "error",
                "data": json.dumps({
                    "error": "Edit cancelled" if aborted else (msg or "fabric recolour failed"),
                    "retriable": not aborted,
                }, ensure_ascii=False),
            }

    return EventSourceResponse(event_generator(), ping=15)
