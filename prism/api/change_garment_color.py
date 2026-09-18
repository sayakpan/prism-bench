"""
POST /api/ai/change-garment-color

Recolour a single garment image to a target colour while keeping the fabric
finish and every construction/geometry detail identical. Same machinery as the
generate-moodboard endpoints (OpenAI Responses API with `image_generation`
forced, SSE streaming with partial images, same host/image models and the same
gpt-image-2 → gpt-image-1 fallback) — only the inputs and the instruction
contract differ.

Inputs (JSON body):
  • garmentImage    : {"mimeType": "...", "data": "<raw base64>"}   (option A)
  • garmentImageUrl : "https://..."  server fetches + encodes it    (option B)
      → supply exactly one of the two.
  • color           : {"hex": "#RRGGBB", "pantone": "19-4052 TCX", "name": "..."}
      → any subset; Pantone is resolved from hex when missing.
  • prompt          : optional colour-only refinements (e.g. "add thin stripes").
      → shape / feature / fabric changes requested here are ignored by design.
  • imageModel, size, quality, partialImages: same optional knobs as v6.

SSE events are identical to the moodboard endpoints:
  meta → start → partial(s) → reply → final | error
"""
from __future__ import annotations

import asyncio
import base64
import json
from collections.abc import AsyncGenerator
from typing import Any
from urllib.parse import urlparse

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse

from moodboard_ai.config import get_settings
from moodboard_ai.log import step
from moodboard_ai.services.openai_responses import (
    ALLOWED_SIZES,
    DEFAULT_IMAGE_TOOL_MODEL,
    stream_moodboard_response_with_fallback,
)
from moodboard_ai.services.pantone import resolve_pantone_code
from moodboard_ai.services.recolor_contract import compose_recolor_contract
from moodboard_ai.services.reference_images import (
    ALLOWED_MIMES,
    MAX_BYTES,
    validate_reference_images,
)

router = APIRouter()

_SOFT_HINT = (
    "Output ONE image: the same garment, same fabric, same shape — recoloured to "
    "the target colour."
)


def _resolve_image_tool_model(image_model: str | None) -> str:
    if image_model == "openai":
        return "gpt-image-1"
    if image_model == "openai-1.5":
        return "gpt-image-1.5"
    return DEFAULT_IMAGE_TOOL_MODEL


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


async def _fetch_image_url(url: str) -> dict[str, str]:
    """Fetch an https image URL server-side and return it in the validated
    reference-image shape ({mimeType, data}). Mirrors the fetch guard in the
    image-edit route."""
    if not url.lower().startswith("https://"):
        raise ValueError('garmentImageUrl must start with "https://"')
    async with httpx.AsyncClient() as client:
        resp = await client.get(url, timeout=15.0, follow_redirects=True)
        if resp.status_code >= 400:
            raise ValueError(
                f"garmentImageUrl fetch failed: {resp.status_code} {resp.reason_phrase}"
            )
    declared = (resp.headers.get("content-type") or "").split(";")[0].strip().lower()
    mime = declared if declared in ALLOWED_MIMES else _guess_mime_from_url(url)
    if not mime or mime not in ALLOWED_MIMES:
        raise ValueError(
            f'garmentImageUrl returned unsupported content-type "{declared or "(none)"}"'
        )
    body = resp.content
    if len(body) > MAX_BYTES:
        raise ValueError(f"garmentImageUrl body too large ({len(body)}B > {MAX_BYTES}B)")
    return {"mimeType": mime, "data": base64.b64encode(body).decode("ascii")}


class ChangeColorRequest(BaseModel):
    garmentImage: dict[str, str] | None = None
    garmentImageUrl: str | None = None
    # Palette: an array of colour objects {hex, pantone, name}. `color`
    # (single object or list) is accepted too and folded into `colors`.
    colors: list[dict[str, Any]] | None = None
    color: dict[str, Any] | list[dict[str, Any]] | None = None
    prompt: str | None = None
    imageModel: str | None = None
    size: str | None = None
    quality: str | None = None


@router.post("/")
async def change_garment_color(req: Request, body: ChangeColorRequest):
    if not get_settings().openai_api_key:
        return JSONResponse(
            status_code=500, content={"error": "OpenAI API key not configured"}
        )

    # ── Resolve the garment image (base64 object XOR https URL) ──────────────
    has_inline = bool(body.garmentImage)
    has_url = bool((body.garmentImageUrl or "").strip())
    if has_inline == has_url:
        return JSONResponse(
            status_code=400,
            content={
                "error": "provide exactly one of 'garmentImage' (base64) or "
                "'garmentImageUrl' (https)"
            },
        )

    if has_inline:
        garment_v = validate_reference_images([body.garmentImage])
        if garment_v.error:
            return JSONResponse(
                status_code=400, content={"error": f"garmentImage: {garment_v.error}"}
            )
        garment_ref = garment_v.images[0]
    else:
        try:
            garment_ref = await _fetch_image_url(body.garmentImageUrl.strip())
        except Exception as err:
            return JSONResponse(
                status_code=400,
                content={"error": str(err) or "garmentImageUrl fetch failed"},
            )

    # Normalise the palette. Prefer `colors`; fall back to `color` (single
    # object or list) for backward compatibility. Keep only dict entries.
    raw_palette: list[Any] = []
    if isinstance(body.colors, list):
        raw_palette = body.colors
    elif isinstance(body.color, list):
        raw_palette = body.color
    elif isinstance(body.color, dict):
        raw_palette = [body.color]
    palette = [c for c in raw_palette if isinstance(c, dict)]

    # A palette is the point of this endpoint; a bare prompt colour also works,
    # but at least one of the two must be present.
    prompt = (body.prompt or "").strip()
    if not palette and not prompt:
        return JSONResponse(
            status_code=400,
            content={"error": "provide a 'colors' array and/or a 'prompt'"},
        )

    resolved_size = body.size if body.size in ALLOWED_SIZES else "1024x1024"
    image_tool_model = _resolve_image_tool_model(body.imageModel)

    # Resolve each palette colour's Pantone TCX from its hex when not supplied
    # outright, and normalise the key to `pantone` for the contract renderer.
    resolved_palette = [
        {**c, "pantone": resolve_pantone_code(c)} for c in palette
    ]

    instructions = compose_recolor_contract(
        colors=resolved_palette,
        prompt=prompt,
    )
    instructions = f"{instructions}\n\n{_SOFT_HINT}"

    # The garment is the sole visual anchor. Passed as the seed image (not a
    # thread) so the model reproduces it and only recolours it.
    garment_data_url = f"data:{garment_ref['mimeType']};base64,{garment_ref['data']}"

    # A tiny, fixed prompt — the heavy lifting is in `instructions`. Name the
    # palette colours so the prompt's colour references have something to bind
    # to; the strict-palette rule lives in the instructions.
    palette_names = [
        str(c.get("name") or c.get("pantone") or c.get("hex") or "").strip()
        for c in resolved_palette
    ]
    palette_names = [n for n in palette_names if n]
    if palette_names:
        user_prompt = "Recolour this garment using only these palette colours: " + ", ".join(palette_names) + "."
    else:
        user_prompt = "Recolour this garment as described."
    if prompt:
        user_prompt += f" {prompt}"

    # Single-shot: no progressive previews. The shared streamer still needs a
    # partial budget of >= 1, so we keep it minimal and drop `partial` events
    # from the SSE feed below — the client only ever sees the final image.
    resolved_partials = 1

    pantone_list = [c.get("pantone") for c in resolved_palette if c.get("pantone")]

    t_req = step("change-garment-color", "request", {
        "imageToolModel": image_tool_model,
        "size": resolved_size,
        "quality": body.quality or "(default)",
        "source": "url" if has_url else "inline",
        "paletteSize": len(resolved_palette),
        "paletteNames": palette_names,
        "pantones": pantone_list,
        "promptLen": len(prompt),
        "instructionsLen": len(instructions),
    })

    event_queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()

    async def event_generator() -> AsyncGenerator[dict[str, Any], None]:
        yield {
            "event": "meta",
            "data": json.dumps({
                "imageToolModel": image_tool_model,
                "size": resolved_size,
                "pantones": pantone_list or None,
            }, ensure_ascii=False),
        }

        def on_event_cb(ev: dict[str, Any]) -> None:
            # Single-final mode: swallow progressive previews; the client only
            # gets the completed image via the `final` event.
            if isinstance(ev, dict) and ev.get("type") == "partial":
                return
            try:
                event_queue.put_nowait(ev)
            except asyncio.QueueFull:
                pass

        async def run() -> None:
            try:
                result = await stream_moodboard_response_with_fallback(
                    prompt=user_prompt,
                    reference_images=[garment_ref],
                    previous_response_id=None,
                    previous_image_data_url=garment_data_url,
                    instructions=instructions,
                    image_tool_model=image_tool_model,
                    size=resolved_size,
                    partial_images=resolved_partials,
                    image_quality=body.quality,
                    on_event=on_event_cb,
                )
                event_queue.put_nowait({
                    "type": "__final__",
                    "responseId": result.response_id,
                    "src": result.src,
                    "reply": result.reply,
                    "responseCreatedAt": result.response_created_at,
                    "modelUsed": result.model_used,
                    "llm_prompt_whole": result.llm_prompt_whole,
                })
            except asyncio.CancelledError:
                raise
            except Exception as err:
                event_queue.put_nowait({"type": "__error__", "message": str(err)})
            finally:
                event_queue.put_nowait(None)

        run_task = asyncio.create_task(run())

        try:
            while True:
                if await req.is_disconnected():
                    run_task.cancel()
                    return

                try:
                    ev = await asyncio.wait_for(event_queue.get(), timeout=1.0)
                except asyncio.TimeoutError:
                    continue

                if ev is None:
                    break

                etype = ev.get("type") if isinstance(ev, dict) else None
                if etype == "__final__":
                    payload = {k: v for k, v in ev.items() if k != "type"}
                    yield {"event": "final", "data": json.dumps(payload, ensure_ascii=False)}
                    t_req.done({
                        "responseId": payload.get("responseId"),
                        "modelUsed": payload.get("modelUsed"),
                    })
                elif etype == "__error__":
                    msg = ev.get("message") or "Failed to recolour garment"
                    lower = msg.lower()
                    aborted = "aborted" in lower or "disconnected" in lower
                    t_req.fail(msg)
                    yield {
                        "event": "error",
                        "data": json.dumps({
                            "error": "Generation cancelled" if aborted else msg,
                            "retriable": not aborted,
                        }, ensure_ascii=False),
                    }
                else:
                    yield {"event": etype or "message", "data": json.dumps(ev, ensure_ascii=False)}
        except asyncio.CancelledError:
            run_task.cancel()
            raise

    return EventSourceResponse(event_generator(), ping=15)
