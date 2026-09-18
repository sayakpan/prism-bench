"""
POST /api/ai/moodboard-image-edit

Isolated image-edit endpoint â€” direct call to `client.images.edit`. No
Responses API, no host model, no chain. Accepts a canvas (file or URL)
plus a prompt, returns the edited canvas as a base64 PNG over SSE.

Body (multipart/form-data):
  prompt:     <text>    required, â‰¤ 32k chars
  image:      <file>    option A: PNG/JPEG/WEBP, â‰¤ 20 MB
  imageUrl:   <text>    option B: https URL the server fetches
  size:       <text>    optional; for openai-2 any value in ALLOWED_SIZES
                        (the same set generate-moodboard-v6 offers, up to
                        2560x2560 / 3072x2048). openai / openai-1.5 accept
                        only 1024x1024 | 1024x1536 | 1536x1024 on edit, so a
                        larger value is collapsed to the nearest bucket.
  imageModel: <text>    optional: "openai" / "openai-1.5" / "openai-2" (default)

Exactly one of `image` / `imageUrl` must be supplied.

If the chosen model 4xx's with a model-access error, transparently
retries with gpt-image-1 (matches v3's fallback).

SSE events: meta â†’ ping (auto, every 15s) â†’ final | error
"""
from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncGenerator
from typing import Any
from urllib.parse import urlparse

import httpx
from fastapi import APIRouter, File, Form, UploadFile
from fastapi.responses import JSONResponse
from openai import AsyncOpenAI
from sse_starlette.sse import EventSourceResponse

from moodboard_ai.config import get_settings
from moodboard_ai.log import step
from moodboard_ai.services.openai_responses import ALLOWED_SIZES

router = APIRouter()

# â”€â”€ Hardcoded knobs â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
_QUALITY = "low"
_N = 1
_DEFAULT_MODEL = "gpt-image-2"
_FALLBACK_MODEL = "gpt-image-1"

# Size sets per model (images.edit accepted values).
#
# gpt-image-2 accepts the full curated set generate-moodboard-v6 exposes
# (ALLOWED_SIZES) — the size list is a property of the model, so it's the same
# whether the image is produced via images.generate or images.edit.
_GPTIMG2_SIZES = ALLOWED_SIZES
# gpt-image-1 / gpt-image-1.5 accept only three sizes on edit. Any larger
# gpt-image-2 value is collapsed into the nearest square/portrait/landscape
# bucket rather than rejected.
_GPTIMG1_SIZES = frozenset({"1024x1024", "1024x1536", "1536x1024"})
_DEFAULT_SIZE = "1536x1024"

_ALLOWED_MIMES = frozenset({"image/png", "image/jpeg", "image/webp"})
_MAX_BYTES = 20 * 1024 * 1024

# Wall timeout for the OpenAI call. gpt-image-2 portrait/landscape edits
# with an attached canvas can legitimately take 3-6 min; 10 min gives
# headroom for the slowest legitimate cases while still cutting off a
# truly stuck stream.
_EDIT_TIMEOUT_S = 600.0


def _resolve_model_from_image_model(image_model: str | None) -> str:
    k = (image_model or "").strip()
    if k == "openai":
        return "gpt-image-1"
    if k == "openai-1.5":
        return "gpt-image-1.5"
    return _DEFAULT_MODEL  # "openai-2" or anything else


def _collapse_to_gptimg1_bucket(size: str) -> str:
    """Map any ``WxH`` into gpt-image-1's nearest supported edit bucket."""
    try:
        w_s, h_s = size.lower().split("x")
        ratio = int(w_s) / int(h_s)
    except (ValueError, ZeroDivisionError):
        return _DEFAULT_SIZE
    if ratio < 0.9:
        return "1024x1536"  # portrait
    if ratio > 1.1:
        return "1536x1024"  # landscape
    return "1024x1024"  # square


def _pick_size_for_model(input_size: str | None, model: str) -> str:
    trimmed = (input_size or "").strip()
    if model == "gpt-image-2":
        return trimmed if trimmed in _GPTIMG2_SIZES else _DEFAULT_SIZE
    # gpt-image-1 + gpt-image-1.5: only three sizes are legal on edit, so a
    # larger gpt-image-2 value is collapsed into its nearest bucket.
    if not trimmed:
        return _DEFAULT_SIZE
    if trimmed in _GPTIMG1_SIZES:
        return trimmed
    return _collapse_to_gptimg1_bucket(trimmed)


def _is_model_access_error(err: Exception) -> bool:
    msg = str(err).lower()
    if "model" not in msg:
        return False
    return any(w in msg for w in (
        "not found", "not available", "not verified",
        "not supported", "exist",
    )) or "invalid model" in msg or "unsupported model" in msg


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
    Returns (buffer, mime_type). Raises with a clear message on failure.

    Used by the FE when reloading a saved board whose canvas lives at an
    S3 URL â€” cross-origin client fetch may hit CORS, the server has no
    such constraint.
    """
    if not url.lower().startswith("https://"):
        raise ValueError(f'imageUrl must start with "https://" (got {url[:32]}...)')
    async with httpx.AsyncClient() as client:
        resp = await client.get(url, timeout=15.0, follow_redirects=True)
        if resp.status_code >= 400:
            raise ValueError(
                f"imageUrl fetch failed: {resp.status_code} {resp.reason_phrase}"
            )
        declared = (resp.headers.get("content-type") or "").split(";")[0].strip().lower()
        mime = declared if declared in _ALLOWED_MIMES else _guess_mime_from_url(url)
        if not mime or mime not in _ALLOWED_MIMES:
            raise ValueError(
                f'imageUrl returned unsupported content-type "{declared or "(none)"}"'
            )
        body = resp.content
        if len(body) > _MAX_BYTES:
            raise ValueError(
                f"imageUrl body too large ({len(body)}B > {_MAX_BYTES}B)"
            )
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


@router.post("/")
async def moodboard_image_edit(
    image: UploadFile | None = File(default=None),
    imageUrl: str | None = Form(default=None),
    prompt: str | None = Form(default=None),
    size: str | None = Form(default=None),
    imageModel: str | None = Form(default=None),
):
    started_at = time.perf_counter()

    prompt_clean = (prompt or "").strip()
    requested_size = (size or "").strip()
    requested_image_model = (imageModel or "").strip()
    primary_model = _resolve_model_from_image_model(requested_image_model)
    primary_size = _pick_size_for_model(requested_size, primary_model)

    # â”€â”€ Pre-SSE validation (4xx returns plain JSON) â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    if not prompt_clean:
        return JSONResponse(
            status_code=400,
            content={"error": "prompt is required (multipart text field)"},
        )
    if len(prompt_clean) > 32_000:
        return JSONResponse(
            status_code=400, content={"error": "prompt is too long (> 32k chars)"}
        )
    if image is None and not (imageUrl or "").strip():
        return JSONResponse(
            status_code=400,
            content={
                "error": (
                    "provide either an 'image' multipart file OR an "
                    "'imageUrl' (https) text field"
                )
            },
        )
    if image is not None and (imageUrl or "").strip():
        return JSONResponse(
            status_code=400,
            content={"error": "provide ONE of 'image' or 'imageUrl', not both"},
        )

    # Resolve to (buffer, mime).
    image_buffer: bytes
    image_mime: str
    if image is not None:
        image_buffer = await image.read()
        image_mime = (image.content_type or "").lower()
        if image_mime not in _ALLOWED_MIMES:
            return JSONResponse(
                status_code=400,
                content={
                    "error": (
                        f'unsupported image mime "{image_mime}" '
                        f"(allowed: {', '.join(sorted(_ALLOWED_MIMES))})"
                    )
                },
            )
        if len(image_buffer) > _MAX_BYTES:
            return JSONResponse(
                status_code=400,
                content={"error": f"image too large ({len(image_buffer)}B > {_MAX_BYTES}B)"},
            )
    else:
        try:
            image_buffer, image_mime = await _fetch_image_url(imageUrl.strip())
        except Exception as err:
            return JSONResponse(
                status_code=400, content={"error": str(err) or "imageUrl fetch failed"}
            )

    t = step("moodboard-image-edit", "request", {
        "model": primary_model,
        "requestedImageModel": requested_image_model or None,
        "size": primary_size,
        "requestedSize": requested_size or None,
        "quality": _QUALITY,
        "n": _N,
        "source": "file" if image is not None else "url",
        "imageBytes": len(image_buffer),
        "imageMime": image_mime,
        "promptLen": len(prompt_clean),
    })

    async def event_generator() -> AsyncGenerator[dict[str, Any], None]:
        yield {
            "event": "meta",
            "data": json.dumps({
                "model": primary_model,
                "size": primary_size,
                "quality": _QUALITY,
            }),
        }

        async def run_edit(model: str, sz: str) -> Any:
            client = _get_client()
            return await client.images.edit(
                model=model,
                image=(_filename_for(image_mime), image_buffer, image_mime),
                prompt=prompt_clean,
                size=sz,
                quality=_QUALITY,
                n=_N,
                timeout=_EDIT_TIMEOUT_S,
            )

        model_used = primary_model
        size_used = primary_size

        try:
            try:
                result = await asyncio.wait_for(
                    run_edit(primary_model, primary_size), timeout=_EDIT_TIMEOUT_S
                )
            except asyncio.TimeoutError as err:
                raise RuntimeError("Edit timed out") from err
            except Exception as err:
                # Transparent fallback to gpt-image-1 when primary isn't
                # reachable on edit. Only when primary wasn't already
                # gpt-image-1 â€” otherwise propagate.
                if not _is_model_access_error(err):
                    raise
                if primary_model == _FALLBACK_MODEL:
                    raise
                print(
                    f"[moodboard-image-edit] {primary_model} failed ({err}); "
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
                    "quality": _QUALITY,
                    "ms": elapsed_ms,
                }),
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
                f"[moodboard-image-edit] failed: name={type(err).__name__} "
                f"message={msg}",
                flush=True,
            )
            yield {
                "event": "error",
                "data": json.dumps({
                    "error": "Edit cancelled" if aborted else (msg or "image edit failed"),
                    "code": getattr(err, "code", None),
                    "type": getattr(err, "type", None),
                    "retriable": not aborted,
                }),
            }

    return EventSourceResponse(event_generator(), ping=15)
