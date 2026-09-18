"""
POST /api/ai/generate-moodboard-v3

Image-first moodboard generation via OpenAI Responses with
`image_generation` as a forced tool call. Streams partial images +
reply deltas over SSE.

Body shape, mode semantics (thread / rehydrate / seed), and SSE event
names (`meta`, `partial`, `reply`, `final`, `error`, plus
sse-starlette's automatic `ping`) are 1:1 with the Node service.
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

from moodboard_ai.config import get_settings
from moodboard_ai.services.contract_v3 import compose_moodboard_contract_v3
from moodboard_ai.services.fabric_refs import fetch_selected_fabric_images
from moodboard_ai.services.openai_responses import (
    ALLOWED_SIZES,
    DEFAULT_IMAGE_TOOL_MODEL,
    MAX_REFS_TO_RESPONSES,
    stream_moodboard_response_with_fallback,
)
from moodboard_ai.services.reference_images import validate_reference_images
from moodboard_ai.services.style_refs import fetch_selected_style_images
from moodboard_ai.log import step

router = APIRouter()

# Soft style guide for SEED turns only â€” the detailed sourcing rules
# live in compose_moodboard_contract_v3. Edit turns ship with empty
# instructions on purpose (see below), so this never lands on a follow-up.
_MOODBOARD_HINT = (
    "A cohesive fashion moodboard image. Composition is free-form â€” grid is "
    "allowed but not required. Use the user's prompt as the lead creative "
    "direction; weave in every element the contract above calls for."
)

# Per-category caps for the OpenAI input_image budget. Sum can exceed
# MAX_REFS_TO_RESPONSES; the route concatenates in priority order then
# slices to the total so the most important refs survive when over budget.
# Priority order: previous image, styles, fabrics, user attachments.
_MAX_STYLE_REFS = 3
_MAX_FABRIC_REFS = 2


def _compose_style_overview_block(style_overview: Any) -> str:
    """
    Render the brand style overview JSON (passed from the FE) into a prompt
    block so the model knows which brand is being targeted. Accepts either the
    raw style-overview object or the full API envelope
    ({"message": {"data": {...}}}) and narrows to the useful payload.

    Returns "" when nothing usable was supplied.
    """
    if not style_overview:
        return ""
    data: Any = style_overview
    # Narrow through the FE/API envelope if present.
    if isinstance(data, dict) and isinstance(data.get("message"), dict):
        data = data["message"].get("data", data["message"])
    if not data:
        return ""
    try:
        rendered = json.dumps(data, ensure_ascii=False, indent=2)
    except (TypeError, ValueError):
        return ""
    return (
        "BRAND STYLE OVERVIEW (the target brand's DNA â€” keep every garment, "
        "colour, fabric, silhouette and mood on-brand; never drift outside "
        "this brand's identity):\n"
        + rendered[:6000]
    )


def _resolve_image_tool_model(image_model: str | None) -> str:
    """
    "openai-2" / "openai-2-streaming" / None â†’ gpt-image-2  (v3 default)
    "openai-1.5"                              â†’ gpt-image-1.5
    "openai"                                  â†’ gpt-image-1
    """
    if image_model == "openai":
        return "gpt-image-1"
    if image_model == "openai-1.5":
        return "gpt-image-1.5"
    return DEFAULT_IMAGE_TOOL_MODEL


class V3Request(BaseModel):
    prompt: str | None = None
    referenceImages: list[dict[str, str]] | None = None
    previousResponseId: str | None = None
    previousImage: dict[str, str] | None = None
    rehydrate: bool | None = None
    conversationSummary: str | None = None
    briefContext: str | None = None
    selectedFabrics: list[dict[str, Any]] | None = None
    selectedColours: list[dict[str, Any]] | None = None
    selectedStyles: list[dict[str, Any]] | None = None
    # Full brand DNA passed from the FE so the model knows which brand is
    # being targeted. Accepts the raw style-overview object or the full API
    # envelope ({"message": {"data": {...}}}).
    style_overview: dict[str, Any] | None = None
    # Slider-driven weighting of PSL inventory vs WGSN trend brief.
    # Accepted values: "aligned" | "balanced" | "trend". Anything else
    # (including absent) falls back to "balanced" inside the composer â€”
    # so old FE payloads without this field keep working unchanged.
    creativityBias: str | None = None
    imageModel: str | None = None
    size: str | None = None
    partialImages: int | None = None


@router.post("/")
async def generate_moodboard_v3(req: Request, body: V3Request):
    prompt = (body.prompt or "").strip()
    if not prompt:
        return JSONResponse(status_code=400, content={"error": "Prompt is required"})
    if not get_settings().openai_api_key:
        return JSONResponse(
            status_code=500, content={"error": "OpenAI API key not configured"}
        )

    # â”€â”€ Pre-SSE validation: 4xx stays JSON so the FE handles it like a
    #    normal failed fetch, not an SSE error event.
    user_refs_v = validate_reference_images(body.referenceImages)
    if user_refs_v.error:
        return JSONResponse(status_code=400, content={"error": user_refs_v.error})

    prev_validated: list[dict[str, str]] = []
    if body.previousImage:
        prev_v = validate_reference_images([body.previousImage])
        if prev_v.error:
            return JSONResponse(
                status_code=400,
                content={"error": f"previousImage: {prev_v.error}"},
            )
        prev_validated = prev_v.images

    resolved_size = body.size if body.size in ALLOWED_SIZES else "1024x1024"
    image_tool_model = _resolve_image_tool_model(body.imageModel)

    # Mode resolution:
    #  - thread:    previousResponseId present AND not rehydrate
    #  - seed:      rehydrate=true OR previousResponseId missing
    # Edit mode = any turn continuing from a prior canvas (thread OR seed-with-prev).
    use_thread = bool(body.previousResponseId) and not bool(body.rehydrate)
    edit_mode = use_thread or bool(prev_validated)

    # Fetch fabric + style images in parallel. Kept running even on edit
    # turns (we don't forward them downstream there) for telemetry shape.
    fabric_images, style_images = await asyncio.gather(
        fetch_selected_fabric_images(body.selectedFabrics, _MAX_FABRIC_REFS),
        fetch_selected_style_images(body.selectedStyles, _MAX_STYLE_REFS),
    )

    # All refs combined, capped. On edit turns: NO input_image refs at all
    # (the canvas arrives via previous_response_id or previousImageDataUrl).
    if edit_mode:
        all_refs: list[dict[str, str]] = []
    else:
        all_refs = [
            *prev_validated,
            *[{"mimeType": s["mimeType"], "data": s["data"]} for s in style_images],
            *[{"mimeType": f["mimeType"], "data": f["data"]} for f in fabric_images],
            *user_refs_v.images,
        ][:MAX_REFS_TO_RESPONSES]

    # Previous-image data URL used by seed/rehydrate mode as the visual anchor.
    previous_image_data_url = (
        f"data:{prev_validated[0]['mimeType']};base64,{prev_validated[0]['data']}"
        if prev_validated
        else None
    )

    print(
        f"[moodboard-v3] request kind: "
        f"{'EDIT (existing moodboard)' if edit_mode else 'FRESH CREATION'}",
        flush=True,
    )

    # Thread mode: chain carries the canvas â€” don't double-feed it as input_image.
    prior_image_for_streamer = None if use_thread else previous_image_data_url

    # Seed-only context: contract + brief + summary + soft hint. All skipped
    # on edit turns (mirrors ChatGPT's native edit behaviour).
    foundation_block = (
        ""
        if edit_mode
        else compose_moodboard_contract_v3(
            selected_fabrics=body.selectedFabrics,
            selected_colours=body.selectedColours,
            selected_styles=body.selectedStyles,
            fabrics_with_image=fabric_images,
            styles_with_image=style_images,
            creativity_bias=body.creativityBias,
        )
    )

    brief_block = ""
    if not edit_mode and isinstance(body.briefContext, str) and body.briefContext.strip():
        brief_block = (
            "BRAND BRIEF (anchor; do not drift from these constraints):\n"
            + body.briefContext.strip()[:3500]
        )

    summary_block = ""
    if (
        not edit_mode
        and body.rehydrate
        and isinstance(body.conversationSummary, str)
        and body.conversationSummary.strip()
    ):
        summary_block = (
            "PRIOR ITERATIONS SUMMARY (compressed history of the moodboard's "
            "evolution before this turn):\n"
            + body.conversationSummary.strip()[:4000]
        )

    # Brand style overview block. Comment the `style_overview_block,` line in
    # the join below to drop it from the prompt entirely.
    style_overview_block = "" if edit_mode else _compose_style_overview_block(body.style_overview)

    instructions = (
        ""
        if edit_mode
        else "\n\n".join(
            b for b in (
                brief_block,
                style_overview_block,  # comment this line to remove brand style overview from the prompt
                foundation_block,
                summary_block,
                _MOODBOARD_HINT,
            ) if b
        )
    )

    # Edit turns get a single partial â€” gpt-image-2 redraws the full canvas
    # on each partial, so multi-partial editing is just three full re-renders
    # racing. Seed turns keep the FE-requested count (default 2).
    resolved_partial_images = 1 if edit_mode else (body.partialImages or 2)

    # No wrapping on edit turns â€” send the literal prompt straight through.
    trimmed_prompt = prompt

    t_req = step("moodboard-v3", "request", {
        "mode": "thread" if use_thread else ("rehydrate" if body.rehydrate else "seed"),
        "editMode": edit_mode,
        "imageToolModel": image_tool_model,
        "size": resolved_size,
        "partials": resolved_partial_images,
        "promptLen": len(trimmed_prompt),
        "instructionsLen": len(instructions),
        "hasStyleOverview": bool(style_overview_block),
        "refsSent": len(all_refs),
        "fabricImgs": len(fabric_images),
        "styleImgs": len(style_images),
        "fabrics": len(body.selectedFabrics or []),
        "styles": len(body.selectedStyles or []),
        "colours": len(body.selectedColours or []),
        "creativityBias": body.creativityBias or "balanced",
        "hasPrev": bool(previous_image_data_url),
        "priorImageAttached": bool(prior_image_for_streamer),
        "hasResponseId": bool(body.previousResponseId),
        "rehydrate": bool(body.rehydrate),
        "summaryLen": len(summary_block),
    })

    # Event queue bridges the streamer task's synchronous on_event callbacks
    # to the async SSE generator.
    event_queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()

    async def event_generator() -> AsyncGenerator[dict[str, Any], None]:
        # Meta â€” surfaces request mode to the FE so it can distinguish
        # "first generation" vs "rehydrate" feedback.
        yield {
            "event": "meta",
            "data": json.dumps({
                "mode": "thread" if use_thread else ("rehydrate" if body.rehydrate else "seed"),
                "imageToolModel": image_tool_model,
                "size": resolved_size,
            }, ensure_ascii=False),
        }

        def on_event_cb(ev: dict[str, Any]) -> None:
            try:
                event_queue.put_nowait(ev)
            except asyncio.QueueFull:
                pass

        async def run() -> None:
            try:
                result = await stream_moodboard_response_with_fallback(
                    prompt=trimmed_prompt,
                    reference_images=all_refs,
                    previous_response_id=body.previousResponseId if use_thread else None,
                    previous_image_data_url=prior_image_for_streamer,
                    instructions=instructions,
                    image_tool_model=image_tool_model,
                    size=resolved_size,
                    partial_images=resolved_partial_images,
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
                event_queue.put_nowait(None)  # sentinel: stream done

        run_task = asyncio.create_task(run())

        try:
            while True:
                # Client disconnect â†’ cascade cancellation into the run task,
                # which itself cancels the upstream OpenAI SDK call.
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
                    msg = ev.get("message") or "Failed to generate moodboard"
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
                    # Pass-through SDK-routed event (start / partial / reply).
                    yield {"event": etype or "message", "data": json.dumps(ev, ensure_ascii=False)}
        except asyncio.CancelledError:
            run_task.cancel()
            raise

    # ping=15s matches the Node service's heartbeat cadence.
    return EventSourceResponse(event_generator(), ping=15)
