"""
POST /api/ai/generate-moodboard-v5

The deliberately-simple moodboard endpoint. Same machinery as v4 (OpenAI
Responses with `image_generation` forced, SSE streaming, same host/image
models, same layout freedom, same chat-refine continuation turns) — the
difference is how the inputs are USED:

  • selectedStyles give the garment SILHOUETTES (slight restyling allowed).
  • selectedFabrics are the MATERIAL the garments are made of — the model
    assigns the supplied fabrics across the garments; fabric detailing is
    crucial and only the supplied fabrics may appear. The fabrics also show as
    swatches on the board.
  • selectedColours recolour the looks; each palette chip shows the Pantone
    TCX number (resolved server-side from the hex).
  • season (new key, e.g. "AW25" / "SS26") drives the BACKGROUND / mood only —
    SS → Spring/Summer, AW → Autumn/Winter.
  • prompt / briefContext set the THEME only.

There is no creativity slider — v5 drops every knob v3/v4 carried. Caps are
tighter on purpose: 4 garments, 2 fabrics.

Body shape (a superset of v3/v4 — the FE sends the same payload plus `season`)
and SSE event names are identical to v3/v4.
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
from moodboard_ai.services.contract_v5 import compose_moodboard_contract_v5
from moodboard_ai.services.fabric_refs import fetch_selected_fabric_images
from moodboard_ai.services.openai_responses import (
    ALLOWED_SIZES,
    DEFAULT_IMAGE_TOOL_MODEL,
    MAX_REFS_TO_RESPONSES,
    stream_moodboard_response_with_fallback,
)
from moodboard_ai.services.pantone import resolve_pantone_code
from moodboard_ai.services.reference_images import validate_reference_images
from moodboard_ai.services.style_refs import fetch_selected_style_images
from moodboard_ai.log import step

router = APIRouter()

# Soft hint appended to the instructions. The detailed rules live in
# compose_moodboard_contract_v5.
_MOODBOARD_HINT = (
    "A rich, free-flowing editorial moodboard built around the supplied garment "
    "silhouettes rendered in the supplied fabrics — a hero title, a short story "
    "caption, palette chips with Pantone numbers, fabric swatches, garment "
    "crops, and multiple model shots, all woven into a layered, overlapping, "
    "NON-grid collage."
)

# Per-category caps for the OpenAI input_image budget. v5 caps are deliberately
# tight: 4 garments, 2 fabrics. Garments are concatenated FIRST so they survive
# the slice to MAX_REFS_TO_RESPONSES.
_MAX_STYLE_REFS = 4
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
        "BRAND STYLE OVERVIEW (the target brand's DNA — keep every garment, "
        "colour, fabric, silhouette and mood on-brand; never drift outside "
        "this brand's identity):\n"
        + rendered[:6000]
    )


def _resolve_image_tool_model(image_model: str | None) -> str:
    if image_model == "openai":
        return "gpt-image-1"
    if image_model == "openai-1.5":
        return "gpt-image-1.5"
    return DEFAULT_IMAGE_TOOL_MODEL


class V5Request(BaseModel):
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
    # NEW in v5: the season code, e.g. "AW25" / "SS26". SS → Spring/Summer,
    # AW → Autumn/Winter. Drives the background/mood only.
    season: str | None = None
    # NEW in v5: garment categories for the board (e.g. ["dresses", "tops"] or
    # a comma-separated string). When selectedStyles is empty, the model
    # imagines garments in these categories, built from the supplied fabrics.
    style_category: list[str] | str | None = None
    imageModel: str | None = None
    size: str | None = None
    partialImages: int | None = None
    # NEW in v5: image_generation quality ("low" | "medium" | "high" | "auto").
    # When passed it overrides the server default; otherwise falls back to the
    # configured default. Higher quality notably improves hands/anatomy.
    quality: str | None = None


@router.post("/")
async def generate_moodboard_v5(req: Request, body: V5Request):
    prompt = (body.prompt or "").strip()
    if not prompt:
        return JSONResponse(status_code=400, content={"error": "Prompt is required"})
    if not get_settings().openai_api_key:
        return JSONResponse(
            status_code=500, content={"error": "OpenAI API key not configured"}
        )

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

    # Mode resolution mirrors v4 — v5 keeps the contract + refs on continue/thread
    # turns so the garment/fabric lock persists across chat-refine turns.
    use_thread = bool(body.previousResponseId) and not bool(body.rehydrate)
    is_continue = use_thread or bool(prev_validated)

    # Fetch garment (style) + fabric images in parallel. Fabrics use macro=True:
    # each fabric ref is replaced by a centre macro crop so the fine knit/weave
    # structure survives OpenAI's input downscale (fixes fine-rib → corduroy).
    style_images, fabric_images = await asyncio.gather(
        fetch_selected_style_images(body.selectedStyles, _MAX_STYLE_REFS),
        fetch_selected_fabric_images(body.selectedFabrics, _MAX_FABRIC_REFS, macro=True),
    )

    # Ref budget. Garments FIRST (highest priority), then fabrics, then user
    # attachments. previousImage is the seed/rehydrate visual anchor (in thread
    # mode the chain already carries the canvas, so it's omitted there).
    prev_for_refs = prev_validated if not use_thread else []
    all_refs = [
        *prev_for_refs,
        *[{"mimeType": s["mimeType"], "data": s["data"]} for s in style_images],
        *[{"mimeType": f["mimeType"], "data": f["data"]} for f in fabric_images],
        *user_refs_v.images,
    ][:MAX_REFS_TO_RESPONSES]

    previous_image_data_url = (
        f"data:{prev_validated[0]['mimeType']};base64,{prev_validated[0]['data']}"
        if prev_validated
        else None
    )

    print(
        f"[moodboard-v5] request kind: "
        f"{'CONTINUE (existing moodboard)' if is_continue else 'FRESH CREATION'} "
        f"| garments={len(style_images)} fabrics={len(fabric_images)} season={body.season or '-'}",
        flush=True,
    )

    prior_image_for_streamer = None if use_thread else previous_image_data_url

    # Resolve each colour's Pantone TCX from its hex (exact match first, else
    # nearest) so the board can render the Pantone number.
    resolved_colours = [
        {**c, "pantone": resolve_pantone_code(c)}
        for c in (body.selectedColours or [])
        if isinstance(c, dict)
    ]

    # Foundation contract is ALWAYS composed — the garment/fabric lock persists
    # on every turn (incl. continuation turns).
    foundation_block = compose_moodboard_contract_v5(
        selected_fabrics=body.selectedFabrics,
        selected_colours=resolved_colours,
        selected_styles=body.selectedStyles,
        fabrics_with_image=fabric_images,
        styles_with_image=style_images,
        season=body.season,
        style_category=body.style_category,
    )

    brief_block = ""
    if isinstance(body.briefContext, str) and body.briefContext.strip():
        brief_block = (
            "BRAND BRIEF (theme/mood anchor — garments and fabrics are NOT defined here):\n"
            + body.briefContext.strip()[:3500]
        )

    summary_block = ""
    if (
        body.rehydrate
        and isinstance(body.conversationSummary, str)
        and body.conversationSummary.strip()
    ):
        summary_block = (
            "PRIOR ITERATIONS SUMMARY (compressed history of the moodboard's "
            "evolution before this turn):\n"
            + body.conversationSummary.strip()[:4000]
        )

    # Brand style overview block. Composed on every turn (the brand DNA, like
    # the garment/fabric lock, must persist across continuation turns). Comment
    # the `style_overview_block,` line in the join below to drop it entirely.
    style_overview_block = _compose_style_overview_block(body.style_overview)

    instructions = "\n\n".join(
        b for b in (
            brief_block,
            style_overview_block,  # comment this line to remove brand style overview from the prompt
            foundation_block,
            summary_block,
            _MOODBOARD_HINT,
        ) if b
    )

    resolved_partial_images = 1 if is_continue else (body.partialImages or 2)

    trimmed_prompt = prompt

    t_req = step("moodboard-v5", "request", {
        "mode": "thread" if use_thread else ("rehydrate" if body.rehydrate else "seed"),
        "isContinue": is_continue,
        "imageToolModel": image_tool_model,
        "size": resolved_size,
        "partials": resolved_partial_images,
        "quality": body.quality or "(default)",
        "promptLen": len(trimmed_prompt),
        "instructionsLen": len(instructions),
        "hasStyleOverview": bool(style_overview_block),
        "season": body.season or "",
        "styleCategory": body.style_category or "",
        "imagineMode": not bool(style_images),
        "refsSent": len(all_refs),
        "garmentImgs": len(style_images),
        "fabricImgs": len(fabric_images),
        "styles": len(body.selectedStyles or []),
        "fabrics": len(body.selectedFabrics or []),
        "colours": len(body.selectedColours or []),
        "hasPrev": bool(previous_image_data_url),
        "priorImageAttached": bool(prior_image_for_streamer),
        "hasResponseId": bool(body.previousResponseId),
        "rehydrate": bool(body.rehydrate),
        "summaryLen": len(summary_block),
    })

    # Surface how many supplied garments did NOT make the ref budget — silent
    # truncation would read as "all garments rendered" when they weren't.
    supplied_styles = len(body.selectedStyles or [])
    if supplied_styles > len(style_images):
        print(
            f"[moodboard-v5] WARNING: {supplied_styles} garments supplied but only "
            f"{len(style_images)} fetched/fit the ref budget — some garments will be absent",
            flush=True,
        )
    supplied_fabrics = len(body.selectedFabrics or [])
    if supplied_fabrics > len(fabric_images):
        print(
            f"[moodboard-v5] WARNING: {supplied_fabrics} fabrics supplied but only "
            f"{len(fabric_images)} fetched/fit the ref budget — some fabrics will be absent",
            flush=True,
        )

    event_queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()

    async def event_generator() -> AsyncGenerator[dict[str, Any], None]:
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
                    yield {"event": etype or "message", "data": json.dumps(ev, ensure_ascii=False)}
        except asyncio.CancelledError:
            run_task.cancel()
            raise

    return EventSourceResponse(event_generator(), ping=15)
