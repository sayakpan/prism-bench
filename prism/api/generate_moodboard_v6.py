"""
POST /api/ai/generate-moodboard-v6

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

There is no creativity slider — v6 drops every knob v3/v4 carried. Caps are
tighter on purpose: 4 supplied garments, 2 fabrics.

`creativity_bias == "advanced_trend"` adds a DESIGN LAYER in front of the board.
Trend reports are no longer attached to the board generator as reference images
(it copied them or ignored them, and 7-8 reports never fit the 10-ref budget
anyway). Instead:

  1. trend_design   — one vision+reasoning pass over EVERY report resolves them
                      into a single design direction plus concrete garment
                      concepts, each bound to a `style_category`, to one of the
                      supplied fabrics (chosen on technical suitability), to the
                      supplied palette, to the season, and to the brand DNA.
  2. trend_garments — each concept is rendered as a product packshot on the
                      strongest image model, already made of its fabric in its
                      colour, with its signature construction built and visible.
  3. this route     — those packshots REPLACE selectedStyles as the board's
                      garment references. The board's only remaining job is to
                      put them on models in the themed setting.

The layer is skipped for every other bias, and falls back to the original
attach-the-report-images path whenever it cannot produce usable garments.

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
from pydantic import AliasChoices, BaseModel, ConfigDict, Field
from sse_starlette.sse import EventSourceResponse

from moodboard_ai.config import get_settings
from moodboard_ai.services.contract_v6 import (
    compose_moodboard_contract_v6,
    normalize_style_categories,
    normalize_trend_reports,
    season_label as resolve_season_label,
)
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
from moodboard_ai.services.trend_design import (
    MAX_ANALYSIS_IMAGES as _MAX_TREND_ANALYSIS_IMAGES,
    MAX_IMAGES_PER_REPORT as _MAX_TREND_IMAGES_PER_REPORT,
)
from moodboard_ai.services.trend_garments import (
    MAX_TREND_GARMENTS,
    build_trend_garments,
    to_data_url,
)
from moodboard_ai.services.trend_refs import fetch_trend_report_images
from moodboard_ai.log import step

router = APIRouter()

# Soft hint appended to the instructions. The detailed rules live in
# compose_moodboard_contract_v6.
_MOODBOARD_HINT = (
    "A rich, free-flowing editorial moodboard built around the supplied garment "
    "silhouettes rendered in the supplied fabrics — a hero title, a short story "
    "caption, palette chips with Pantone numbers, fabric swatches, garment "
    "crops, and multiple model shots, all woven into a layered, overlapping, "
    "NON-grid collage."
)

# Per-category caps for the OpenAI input_image budget. v6 caps are deliberately
# tight: 4 supplied garments, 2 fabrics. Garments are concatenated FIRST so they
# survive the slice to MAX_REFS_TO_RESPONSES. (advanced_trend replaces the
# supplied garments with MAX_TREND_GARMENTS designed ones — see trend_garments.)
_MAX_STYLE_REFS = 4
_MAX_FABRIC_REFS = 2
# advanced_trend LEGACY fallback only: up to this many trend reference images
# (total, across all reports) attached directly to the board. They ride AFTER
# garments + fabrics in the ref budget, so with styles present they can be
# squeezed out by MAX_REFS_TO_RESPONSES — a drop is logged.
#
# The primary advanced_trend path no longer uses these at all: the reports are
# read upstream (trend_design → trend_garments) and arrive as designed garment
# packshots instead, costing the board zero trend ref slots.
_MAX_TREND_REFS = 4


def _resolve_trend_categories(
    style_category: Any,
    selected_styles: list[dict[str, Any]] | None,
) -> list[str]:
    """
    The garment categories the advanced_trend designer is BOUND to (tops stay
    tops). `style_category` is the source of truth; when the FE omits it we fall
    back to the product groups of whatever garments were selected, so an
    advanced_trend request is not silently downgraded just because the category
    picker was skipped.

    An empty result disables the generated path — designing garments with no
    category binding is exactly the unbounded behaviour we are replacing.
    """
    categories = normalize_style_categories(style_category)
    if categories:
        return categories
    derived: list[str] = []
    for s in selected_styles or []:
        if not isinstance(s, dict):
            continue
        group = s.get("productGroup") or s.get("category")
        if isinstance(group, str) and group.strip():
            derived.append(group.strip())
    return normalize_style_categories(derived)


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


class V6Request(BaseModel):
    # The FE sends a mix of camelCase and snake_case keys. populate_by_name +
    # per-field AliasChoices let the trend feature's keys arrive under EITHER
    # convention (e.g. `creativityBias` or `creativity_bias`) — the observed
    # failure was the FE sending `creativityBias` while the field was snake-only.
    model_config = ConfigDict(populate_by_name=True)

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
    # NEW in v6: how much the supplied garment silhouettes may change. Touches
    # the GARMENTS/styles section ONLY (nothing else depends on it):
    #   • "aligned"        → reproduce the silhouettes EXACTLY; no restyling at all.
    #   • "balanced"       → default; minor restyling allowed (v5 behaviour).
    #   • "trend"          → reimagine the silhouettes with a modern, on-brand viewpoint.
    #   • "advanced_trend" → the garments are DESIGNED from `trend_reports` below
    #                        by the upstream trend layer and replace selectedStyles
    #                        entirely (bound to `style_category`). Empty reports →
    #                        falls back to "trend".
    # Accepts `creativity_bias` OR `creativityBias` (the FE sends the latter).
    creativity_bias: str | None = Field(
        default=None,
        validation_alias=AliasChoices("creativity_bias", "creativityBias"),
    )
    # NEW: only used when creativity_bias == "advanced_trend". WGSN-style trend
    # reports the garments are DESIGNED from:
    #   [{ report_title, detail_signals: [str, ...], image_urls: [url, ...] }]
    # 7-8 reports at a time is normal and all of them are read: titles, signals
    # and imagery all feed the upstream design pass, not the board's ref budget.
    # Accepts `trend_reports` OR `trendReports`.
    trend_reports: list[dict[str, Any]] | None = Field(
        default=None,
        validation_alias=AliasChoices("trend_reports", "trendReports"),
    )
    imageModel: str | None = None
    size: str | None = None
    partialImages: int | None = None
    # NEW in v5: image_generation quality ("low" | "medium" | "high" | "auto").
    # When passed it overrides the server default; otherwise falls back to the
    # configured default. Higher quality notably improves hands/anatomy.
    quality: str | None = None


@router.post("/")
async def generate_moodboard_v6(req: Request, body: V6Request):
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

    # Mode resolution mirrors v4 — v6 keeps the contract + refs on continue/thread
    # turns so the garment/fabric lock persists across chat-refine turns.
    use_thread = bool(body.previousResponseId) and not bool(body.rehydrate)
    is_continue = use_thread or bool(prev_validated)

    # Trend reports are read ONLY for advanced_trend (any other bias ignores
    # them entirely — existing behaviour intact).
    want_trend = (
        (body.creativity_bias or "").strip().lower() == "advanced_trend"
        and bool(body.trend_reports)
    )
    # …and the generated pipeline additionally needs categories to bind the
    # designer. Without them we keep the legacy report-refs-on-the-board path.
    trend_categories = (
        _resolve_trend_categories(body.style_category, body.selectedStyles)
        if want_trend
        else []
    )
    want_generated_trend = want_trend and bool(trend_categories)

    # Fetch garment (style) + fabric (+ trend) images in parallel. Fabrics use
    # macro=True: each fabric ref is replaced by a centre macro crop so the fine
    # knit/weave structure survives OpenAI's input downscale (fixes fine-rib →
    # corduroy). Trend fetch is a no-op (returns []) when not in advanced_trend.
    #
    # The generated path pulls a much larger, per-report-fair set of trend
    # imagery: it feeds a reasoning call, not the board's 10-ref budget, so the
    # constraint that forced 8 reports through 4 slots simply does not apply.
    # Style images are still fetched — they are the fallback if generation fails.
    style_images, fabric_images, trend_images = await asyncio.gather(
        fetch_selected_style_images(body.selectedStyles, _MAX_STYLE_REFS),
        fetch_selected_fabric_images(body.selectedFabrics, _MAX_FABRIC_REFS, macro=True),
        fetch_trend_report_images(
            body.trend_reports if want_trend else None,
            _MAX_TREND_ANALYSIS_IMAGES if want_generated_trend else _MAX_TREND_REFS,
            per_report_limit=_MAX_TREND_IMAGES_PER_REPORT if want_generated_trend else None,
        ),
    )

    # previousImage is the seed/rehydrate visual anchor (in thread mode the
    # chain already carries the canvas, so it's omitted there). The rest of the
    # ref budget is assembled inside run(), once the trend pipeline has settled
    # whether the garments are the user's or the ones it designed.
    prev_for_refs = prev_validated if not use_thread else []

    previous_image_data_url = (
        f"data:{prev_validated[0]['mimeType']};base64,{prev_validated[0]['data']}"
        if prev_validated
        else None
    )

    print(
        f"[moodboard-v6] request kind: "
        f"{'CONTINUE (existing moodboard)' if is_continue else 'FRESH CREATION'} "
        f"| garments={len(style_images)} fabrics={len(fabric_images)} "
        f"trendReports={len(body.trend_reports or [])} trendImgs={len(trend_images)} "
        f"trendMode={'generated' if want_generated_trend else ('legacy' if want_trend else '-')} "
        f"season={body.season or '-'}",
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
    resolved_pantones = [
        p for p in (str(c.get("pantone") or "").strip() for c in resolved_colours) if p
    ]

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

    resolved_partial_images = body.partialImages or (1 if is_continue else 2)

    trimmed_prompt = prompt

    supplied_fabrics = len(body.selectedFabrics or [])
    if supplied_fabrics > len(fabric_images):
        print(
            f"[moodboard-v6] WARNING: {supplied_fabrics} fabrics supplied but only "
            f"{len(fabric_images)} fetched/fit the ref budget — some fabrics will be absent",
            flush=True,
        )

    # The request-level step is opened inside run(), once the trend pipeline has
    # decided what the board is actually working from. The holder lets the drain
    # loop below close it without reaching into the coroutine.
    t_req_holder: list[Any] = []

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
                # ── advanced_trend: design the garments BEFORE the board ──────
                #
                # This runs inside run() rather than before the response opens
                # so its progress reaches the client: the pipeline adds real
                # wall-clock (one reasoning pass + N packshots) and silent dead
                # air on an SSE endpoint reads as a hang.
                trend_direction: dict[str, Any] | None = None
                gen_style_images: list[dict[str, Any]] = []

                if want_generated_trend:
                    build = await build_trend_garments(
                        reports=normalize_trend_reports(body.trend_reports),
                        report_images=trend_images,
                        categories=trend_categories,
                        fabric_images=fabric_images,
                        pantones=resolved_pantones,
                        season=body.season,
                        season_label=resolve_season_label(body.season),
                        brand_overview=style_overview_block,
                        garment_count=MAX_TREND_GARMENTS,
                        on_progress=on_event_cb,
                    )
                    if build and build.garment_images:
                        trend_direction = build.direction
                        gen_style_images = build.garment_images
                        on_event_cb({
                            "type": "trend_garments",
                            "cached": build.cached,
                            "directionTitle": build.direction.get("direction_title"),
                            "directionSummary": build.direction.get("direction_summary"),
                            "garments": [
                                {
                                    "name": r["concept"]["name"],
                                    "category": r["concept"]["category"],
                                    "signatureDetails": r["concept"].get("signature_details") or [],
                                    "trendExpression": r["concept"].get("trend_expression") or "",
                                    "fabric": r["concept"].get("fabric_name") or "",
                                    "fabricRationale": r["concept"].get("fabric_rationale") or "",
                                    "pantone": r["concept"].get("colour_pantone") or "",
                                    "secondaryPantone": r["concept"].get("secondary_pantone") or "",
                                    "pattern": r["concept"].get("pattern") or "",
                                    "sourceReports": r["concept"].get("source_reports") or [],
                                    "src": to_data_url(r),
                                }
                                for r in gen_style_images
                            ],
                        })
                    else:
                        # Never fail the board for this — fall back to the
                        # legacy report-refs path and say so out loud.
                        print(
                            "[moodboard-v6] WARNING: advanced_trend garment pipeline produced "
                            "nothing usable — falling back to attaching trend report images "
                            "directly (legacy path)",
                            flush=True,
                        )
                        on_event_cb({"type": "trend_status", "stage": "fallback"})

                trend_generated = bool(gen_style_images)
                # Generated garments REPLACE selectedStyles: they were designed
                # from the same categories, already carry the trend, and already
                # wear the assigned fabric and palette colour.
                effective_style_images = gen_style_images if trend_generated else style_images
                effective_styles = (
                    [r["style"] for r in gen_style_images]
                    if trend_generated
                    else (body.selectedStyles or [])
                )
                # Legacy trend refs only ride along when the pipeline did NOT
                # run; generated garments carry the trend and cost zero slots.
                legacy_trend_images = (
                    [] if trend_generated else trend_images[:_MAX_TREND_REFS]
                )

                # Ref budget. Garments FIRST (highest priority), then fabrics,
                # then legacy trend refs, then user attachments.
                all_refs = [
                    *prev_for_refs,
                    *[{"mimeType": s["mimeType"], "data": s["data"]} for s in effective_style_images],
                    *[{"mimeType": f["mimeType"], "data": f["data"]} for f in fabric_images],
                    *[{"mimeType": t["mimeType"], "data": t["data"]} for t in legacy_trend_images],
                    *user_refs_v.images,
                ][:MAX_REFS_TO_RESPONSES]

                # How many legacy trend refs actually survived the cap → the
                # contract only claims "N trend images attached" for the ones
                # the model will really see.
                trend_refs_in_budget = max(
                    0,
                    min(
                        len(legacy_trend_images),
                        MAX_REFS_TO_RESPONSES
                        - len(prev_for_refs)
                        - len(effective_style_images)
                        - len(fabric_images),
                    ),
                )
                if len(legacy_trend_images) > trend_refs_in_budget:
                    print(
                        f"[moodboard-v6] WARNING: {len(legacy_trend_images)} trend reference "
                        f"images fetched but only {trend_refs_in_budget} fit the ref budget "
                        f"(cap {MAX_REFS_TO_RESPONSES}, after garments+fabrics) — the rest are "
                        f"dropped; their trend signals still reach the model as text",
                        flush=True,
                    )
                supplied_styles = len(body.selectedStyles or [])
                if not trend_generated and supplied_styles > len(style_images):
                    print(
                        f"[moodboard-v6] WARNING: {supplied_styles} garments supplied but only "
                        f"{len(style_images)} fetched/fit the ref budget — some garments will "
                        f"be absent",
                        flush=True,
                    )

                # Foundation contract is ALWAYS composed — the garment/fabric
                # lock persists on every turn (incl. continuation turns).
                foundation_block = compose_moodboard_contract_v6(
                    selected_fabrics=body.selectedFabrics,
                    selected_colours=resolved_colours,
                    selected_styles=effective_styles,
                    fabrics_with_image=fabric_images,
                    styles_with_image=effective_style_images,
                    season=body.season,
                    style_category=body.style_category,
                    creativity_bias=body.creativity_bias,
                    trend_reports=body.trend_reports,
                    trend_refs_count=trend_refs_in_budget,
                    trend_direction=trend_direction,
                    trend_generated=trend_generated,
                )

                instructions = "\n\n".join(
                    b for b in (
                        brief_block,
                        style_overview_block,  # comment this line to remove brand style overview from the prompt
                        foundation_block,
                        summary_block,
                        _MOODBOARD_HINT,
                    ) if b
                )

                t_req = step("moodboard-v6", "request", {
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
                    "creativityBias": body.creativity_bias or "(default)",
                    "trendMode": (
                        "generated" if trend_generated
                        else ("legacy" if want_trend else "-")
                    ),
                    "trendCategories": ",".join(trend_categories),
                    "trendDirection": (trend_direction or {}).get("direction_title", ""),
                    "imagineMode": not bool(effective_style_images),
                    "refsSent": len(all_refs),
                    "garmentImgs": len(effective_style_images),
                    "fabricImgs": len(fabric_images),
                    "trendReports": len(body.trend_reports or []),
                    "trendImgs": trend_refs_in_budget,
                    "trendImgsFetched": len(trend_images),
                    "styles": len(body.selectedStyles or []),
                    "fabrics": len(body.selectedFabrics or []),
                    "colours": len(body.selectedColours or []),
                    "hasPrev": bool(previous_image_data_url),
                    "priorImageAttached": bool(prior_image_for_streamer),
                    "hasResponseId": bool(body.previousResponseId),
                    "rehydrate": bool(body.rehydrate),
                    "summaryLen": len(summary_block),
                })
                t_req_holder.append(t_req)

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
                    "trendDirection": trend_direction,
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
                    # Absent only if run() died before the board call started
                    # (e.g. the trend pipeline raised) — nothing to close.
                    if t_req_holder:
                        t_req_holder[0].done({
                            "responseId": payload.get("responseId"),
                            "modelUsed": payload.get("modelUsed"),
                        })
                elif etype == "__error__":
                    msg = ev.get("message") or "Failed to generate moodboard"
                    lower = msg.lower()
                    aborted = "aborted" in lower or "disconnected" in lower
                    if t_req_holder:
                        t_req_holder[0].fail(msg)
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
