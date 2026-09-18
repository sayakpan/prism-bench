"""
POST /api/ai/assist

Conversational AI co-pilot for the moodboard editor. Mode-required:
the FE tab (`images` / `patterns` / `embroidery`) is mandatory.

Streams over SSE (like generate-moodboard-v6 and moodboard-extract-products)
so the slow image batch doesn't trip a proxy idle timeout and the FE gets the
reply text before the images finish rendering.

Request:
  {
    prompt: str,
    context: { title, brand, collection, season, gender, mood,
               styleCategory, aiPrompt, fabrics?, colours? },
    imageModel?: "openai" | "openai-1.5" | "openai-2" | "gemini",
    mode: "images" | "patterns" | "embroidery"   (REQUIRED),
    referenceImages?: [{ mimeType, data }]
  }

SSE events:
  meta   -> { mode, intent: "images", imageModel }         (handshake)
  reply  -> { reply, prompts }                             (as soon as Claude plans;
             reply is PRESENT-CONTINUOUS "I'm adding..." â€” images still rendering)
  done   -> { reply, mode, intent: "images", images,       (final envelope; reply is
             layout: null, imageError, layoutError: null }   PAST tense "I added...")
  error  -> { error, retriable }                           (on failure)
  ping   -> 15s keepalive (emitted by EventSourceResponse)

The `done` payload is the exact envelope the pre-SSE JSON endpoint returned, so
callers only need to swap the transport, not the result shape.

Bad-request validation (missing prompt / invalid mode / bad reference images)
still returns a plain 400 JSON body before the stream opens.
"""
from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncGenerator
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse

from moodboard_ai.log import step
from moodboard_ai.services.assist import VALID_MODES, run_assist
from moodboard_ai.services.reference_images import validate_reference_images

router = APIRouter()


class AssistRequest(BaseModel):
    prompt: str | None = None
    context: dict[str, Any] | None = None
    imageModel: str | None = None
    mode: str | None = None
    referenceImages: list[dict[str, str]] | None = None


@router.post("/")
async def assist_endpoint(req: Request, body: AssistRequest):
    prompt = (body.prompt or "").strip()
    if not prompt:
        return JSONResponse(status_code=400, content={"error": "Prompt is required"})

    if not body.mode or body.mode not in VALID_MODES:
        return JSONResponse(
            status_code=400,
            content={
                "error": (
                    f"mode is required and must be one of: "
                    f"{', '.join(sorted(VALID_MODES))}"
                )
            },
        )

    validation = validate_reference_images(body.referenceImages)
    if validation.error:
        return JSONResponse(status_code=400, content={"error": validation.error})

    mode = body.mode
    image_model = body.imageModel
    reference_images = validation.images or None

    t_req = step("ai-assist", "request", {
        "mode": mode,
        "imageModel": image_model or "openai",
        "hasRefs": bool(reference_images),
    })

    event_queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()

    async def event_generator() -> AsyncGenerator[dict[str, Any], None]:
        yield {
            "event": "meta",
            "data": json.dumps({
                "mode": mode,
                "intent": "images",
                "imageModel": image_model or "openai",
            }, ensure_ascii=False),
        }

        def on_event_cb(ev: dict[str, Any]) -> None:
            try:
                event_queue.put_nowait(ev)
            except asyncio.QueueFull:
                pass

        async def run() -> None:
            try:
                result = await run_assist(
                    prompt=prompt,
                    context=body.context,
                    image_model=image_model,
                    mode=mode,
                    reference_images=reference_images,
                    on_event=on_event_cb,
                )
                event_queue.put_nowait({"type": "__final__", **result})
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
                    yield {"event": "done", "data": json.dumps(payload, ensure_ascii=False)}
                    t_req.done({
                        "images": len(payload.get("images") or []),
                        "imageError": bool(payload.get("imageError")),
                    })
                elif etype == "__error__":
                    msg = ev.get("message") or "Assist failed"
                    lower = msg.lower()
                    aborted = "aborted" in lower or "disconnected" in lower
                    t_req.fail(msg)
                    yield {
                        "event": "error",
                        "data": json.dumps({
                            "error": "Assist cancelled" if aborted else msg,
                            "retriable": not aborted,
                        }, ensure_ascii=False),
                    }
                else:
                    # Intermediate progress (e.g. `reply`). Strip the internal
                    # dispatch key; the event name already carries it.
                    payload = {k: v for k, v in ev.items() if k != "type"}
                    yield {
                        "event": etype or "message",
                        "data": json.dumps(payload, ensure_ascii=False),
                    }
        except asyncio.CancelledError:
            run_task.cancel()
            raise

    return EventSourceResponse(event_generator(), ping=15)
