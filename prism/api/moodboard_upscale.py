"""
POST /api/ai/moodboard-upscale

Re-render a FINISHED moodboard at a higher resolution without changing its
content. Unlike moodboard-image-edit (which applies a user prompt), this
endpoint hard-codes a "reproduce exactly" prompt and only raises the output
size + quality. The board's layout, garments, colours, Pantone codes and
text are meant to survive untouched; only resolution/sharpness increase.

It streams over SSE with real partial images (gpt-image-2 `stream=True`),
so the FE can show the canvas resolving instead of a spinner.

Body (multipart/form-data):
  image:         <file>  option A: the finished board — PNG/JPEG/WEBP, <= 20 MB
  imageUrl:      <text>  option B: https URL the server fetches (e.g. saved
                         board on the file server)
  size:          <text>  optional target size (WxH). The endpoint never crops
                         or reshapes, so a size that doesn't match the board's
                         aspect (or isn't larger) is SNAPPED to the closest
                         same-aspect target by megapixels rather than rejected.
                         Omitted -> the largest same-aspect upscale. The `meta`
                         and `final` events report `snapped`/`snapNote` so the
                         FE can tell the user which resolution they got.
  quality:       <text>  optional: low|medium|high|auto (default "high")
  partialImages: <text>  optional 0-3 (default 2). 0 = final image only.

Exactly one of `image` / `imageUrl` must be supplied.

Why gpt-image-2 only: gpt-image-1 / 1.5 accept only 1024x1024 / 1024x1536 /
1536x1024 on edit, which would DOWNSCALE most finished boards. Upscaling needs
gpt-image-2's flexible sizes, so there is no gpt-image-1 fallback here — if
gpt-image-2 is unreachable the request errors rather than silently shrinking
the board.

SSE events: meta -> partial* -> ping (auto, 15s) -> final | error
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
from fastapi import APIRouter, File, Form, UploadFile
from fastapi.responses import JSONResponse
from openai import AsyncOpenAI
from PIL import ExifTags, Image
from sse_starlette.sse import EventSourceResponse

from moodboard_ai.config import get_settings
from moodboard_ai.log import step
from moodboard_ai.services.openai_responses import ALLOWED_QUALITIES, ALLOWED_SIZES

router = APIRouter()

# ── Hardcoded knobs ──────────────────────────────────────────────────────────
_MODEL = "gpt-image-2"  # the only model whose edit sizes go above 1536x1024
_N = 1
_DEFAULT_QUALITY = "high"

# gpt-image-2 accepts any size where both edges are multiples of 16, the max
# edge is <= 3840, the long:short ratio is <= 3:1, and total pixels are between
# 655,360 and 8,294,400. UPSCALE_SIZES is the generate-v6 curated set plus
# 3840x2160 (4K, 16:9) — the only same-aspect step ABOVE QHD (2560x1440), which
# ALLOWED_SIZES otherwise tops out at. 3840x2160 is verified against all four
# constraints (8,294,400 px == the ceiling exactly).
UPSCALE_SIZES = frozenset(ALLOWED_SIZES | {"3840x2160"})

# Two sizes count as the same shape when their aspect ratios agree within this
# tolerance. Keeps a 3:2 board upgrading only among 3:2 targets (never cropped
# into a 16:9 QHD frame, and vice-versa).
_ASPECT_TOL = 0.02

_ALLOWED_MIMES = frozenset({"image/png", "image/jpeg", "image/webp"})
_MAX_BYTES = 20 * 1024 * 1024

# gpt-image-2 high-quality edits at 4K legitimately take several minutes.
_EDIT_TIMEOUT_S = 600.0

# The whole point of the endpoint: keep every pixel of intent, add only pixels.
_PRESERVE_PROMPT = (
    "Reproduce this moodboard EXACTLY as-is. Keep the identical composition, "
    "layout, grid, every garment, every colour, every fabric texture, every "
    "swatch, and all text and labels — including Pantone codes — in the same "
    "positions, sizes and proportions. Do not add, remove, move, restyle, "
    "recolour, or reinterpret anything, and do not change any wording. The only "
    "change is to render the same board at higher resolution with crisper, "
    "sharper detail."
)

_ORIENTATION_TAG = next(
    (tag for tag, name in ExifTags.TAGS.items() if name == "Orientation"), 274
)


# ── helpers ──────────────────────────────────────────────────────────────────
def _filename_for(mime: str) -> str:
    if mime == "image/jpeg":
        return "board.jpg"
    if mime == "image/webp":
        return "board.webp"
    return "board.png"


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
    """Server-side fetch of an https board URL into a buffer (validates mime + size)."""
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
            raise ValueError(f"imageUrl body too large ({len(body)}B > {_MAX_BYTES}B)")
        return body, mime


def _read_dimensions(raw: bytes) -> tuple[int, int]:
    """Visible (orientation-corrected) pixel dimensions of the source board."""
    img = Image.open(BytesIO(raw))
    img.load()
    width, height = img.size
    try:
        exif = img.getexif()
        orientation = exif.get(_ORIENTATION_TAG, 1) if exif else 1
    except Exception:
        orientation = 1
    if 5 <= orientation <= 8:  # stored axes swapped from visible
        width, height = height, width
    return width, height


def _dims(size: str) -> tuple[int, int]:
    w_s, h_s = size.lower().split("x")
    return int(w_s), int(h_s)


def _same_aspect_upscales(src_w: int, src_h: int) -> list[str]:
    """
    Allowed sizes that share the source aspect (within tolerance) AND have more
    pixels than the source, sorted small -> large. These are the only legal
    "same board, bigger" targets.
    """
    if src_w <= 0 or src_h <= 0:
        return []
    src_ratio = src_w / src_h
    src_area = src_w * src_h
    out: list[tuple[int, str]] = []
    for s in UPSCALE_SIZES:
        w, h = _dims(s)
        if abs((w / h) - src_ratio) > _ASPECT_TOL:
            continue
        area = w * h
        if area <= src_area:
            continue
        out.append((area, s))
    out.sort()
    return [s for _, s in out]


def _resolve_target(
    requested: str, src_w: int, src_h: int
) -> tuple[str | None, dict[str, Any]]:
    """
    Resolve the FE's requested tier to a real upscale target.

    The endpoint never crops or reshapes, so the only legal targets are
    same-aspect sizes larger than the source. Rather than reject a request
    whose aspect doesn't fit the board (e.g. a 3:2 "Max" asked of a 16:9 QHD
    board), we SNAP it to the same-aspect option closest in megapixels — the
    user picked a quality level, so give them the nearest same-shape one.

    Returns (target_size, info). target_size is None ONLY when the board is
    already at/above every same-aspect target (nothing larger to produce);
    info then carries the human-readable reason. On success info records
    whether the pick was exact, snapped, or auto (no size requested).
    """
    options = _same_aspect_upscales(src_w, src_h)
    if not options:
        return None, {
            "reason": (
                f"source {src_w}x{src_h} already meets or exceeds every "
                "same-aspect target — nothing larger to upscale to"
            )
        }

    if not requested:
        return options[-1], {"mode": "auto", "requested": None, "snapped": False}

    # Exact same-aspect, larger target — take it as-is.
    if requested in options:
        return requested, {"mode": "exact", "requested": requested, "snapped": False}

    # Snap to the same-aspect option nearest the requested megapixel count.
    try:
        rw, rh = _dims(requested)
        req_area = rw * rh
    except Exception:
        # Unparseable size → treat as "give me the best": largest same-aspect.
        return options[-1], {
            "mode": "snap", "requested": requested, "snapped": True,
            "note": "unparseable size; used largest same-aspect target",
        }
    best = min(options, key=lambda s: abs((_dims(s)[0] * _dims(s)[1]) - req_area))
    return best, {
        "mode": "snap", "requested": requested, "snapped": True,
        "note": (
            f'"{requested}" is not a same-aspect upscale for a {src_w}x{src_h} '
            f"board; snapped to nearest same-aspect target {best}"
        ),
    }


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


# ── route ────────────────────────────────────────────────────────────────────
@router.post("/")
async def moodboard_upscale(
    image: UploadFile | None = File(default=None),
    imageUrl: str | None = Form(default=None),
    size: str | None = Form(default=None),
    quality: str | None = Form(default=None),
    partialImages: str | None = Form(default=None),
):
    started_at = time.perf_counter()

    requested_size = (size or "").strip()
    resolved_quality = (quality or "").strip().lower()
    if resolved_quality not in ALLOWED_QUALITIES:
        resolved_quality = _DEFAULT_QUALITY

    try:
        resolved_partials = int((partialImages or "").strip() or "2")
    except ValueError:
        resolved_partials = 2
    resolved_partials = max(0, min(3, resolved_partials))

    # ── Pre-SSE validation (4xx returns plain JSON) ──────────────────────────
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

    # Read source dimensions and resolve a same-aspect, larger target.
    try:
        src_w, src_h = _read_dimensions(image_buffer)
    except Exception as err:
        return JSONResponse(
            status_code=400,
            content={"error": f"could not read image dimensions: {err}"},
        )

    target_size, info = _resolve_target(requested_size, src_w, src_h)
    if target_size is None:
        return JSONResponse(
            status_code=400,
            content={
                "error": info.get("reason", "no valid upscale target"),
                "sourceSize": f"{src_w}x{src_h}",
                "options": _same_aspect_upscales(src_w, src_h),
            },
        )
    snapped = bool(info.get("snapped"))

    t = step("moodboard-upscale", "request", {
        "model": _MODEL,
        "sourceSize": f"{src_w}x{src_h}",
        "targetSize": target_size,
        "requestedSize": requested_size or None,
        "targetInfo": info,
        "quality": resolved_quality,
        "partialImages": resolved_partials,
        "n": _N,
        "source": "file" if image is not None else "url",
        "imageBytes": len(image_buffer),
        "imageMime": image_mime,
    })

    async def event_generator() -> AsyncGenerator[dict[str, Any], None]:
        yield {
            "event": "meta",
            "data": json.dumps({
                "model": _MODEL,
                "sourceSize": f"{src_w}x{src_h}",
                "size": target_size,
                "requestedSize": requested_size or None,
                "snapped": snapped,
                "snapNote": info.get("note"),
                "quality": resolved_quality,
                "partialImages": resolved_partials,
            }),
        }

        stream = None
        final_b64: str | None = None
        usage: dict[str, Any] | None = None
        try:
            client = _get_client()
            stream = await client.images.edit(
                model=_MODEL,
                image=(_filename_for(image_mime), image_buffer, image_mime),
                prompt=_PRESERVE_PROMPT,
                size=target_size,
                quality=resolved_quality,
                n=_N,
                stream=True,
                partial_images=resolved_partials,
                timeout=_EDIT_TIMEOUT_S,
            )

            async for event in stream:
                etype = getattr(event, "type", "")
                if etype == "image_edit.partial_image":
                    b64 = getattr(event, "b64_json", None)
                    if not b64:
                        continue
                    yield {
                        "event": "partial",
                        "data": json.dumps({
                            "index": getattr(event, "partial_image_index", None),
                            "src": f"data:image/png;base64,{b64}",
                            "size": target_size,
                            "quality": resolved_quality,
                        }),
                    }
                elif etype == "image_edit.completed":
                    final_b64 = getattr(event, "b64_json", None)
                    u = getattr(event, "usage", None)
                    if u is not None:
                        usage = {
                            "input_tokens": getattr(u, "input_tokens", None),
                            "output_tokens": getattr(u, "output_tokens", None),
                            "total_tokens": getattr(u, "total_tokens", None),
                        }

            if not final_b64:
                raise RuntimeError("images.edit stream ended with no final image")

            elapsed_ms = int((time.perf_counter() - started_at) * 1000)
            yield {
                "event": "final",
                "data": json.dumps({
                    "src": f"data:image/png;base64,{final_b64}",
                    "model": _MODEL,
                    "sourceSize": f"{src_w}x{src_h}",
                    "size": target_size,
                    "requestedSize": requested_size or None,
                    "snapped": snapped,
                    "snapNote": info.get("note"),
                    "quality": resolved_quality,
                    "ms": elapsed_ms,
                    "usage": usage,
                }),
            }
            t.done({"ms": elapsed_ms, "targetSize": target_size, "quality": resolved_quality})

        except asyncio.CancelledError:
            if stream is not None:
                try:
                    await stream.close()
                except Exception:
                    pass
            t.fail("client disconnected")
            raise
        except Exception as err:
            t.fail(err)
            msg = str(err)
            lower = msg.lower()
            aborted = "aborted" in lower or "disconnected" in lower
            print(
                f"[moodboard-upscale] failed: name={type(err).__name__} message={msg}",
                flush=True,
            )
            yield {
                "event": "error",
                "data": json.dumps({
                    "error": "Upscale cancelled" if aborted else (msg or "upscale failed"),
                    "code": getattr(err, "code", None),
                    "type": getattr(err, "type", None),
                    "retriable": not aborted,
                }),
            }

    return EventSourceResponse(event_generator(), ping=15)
