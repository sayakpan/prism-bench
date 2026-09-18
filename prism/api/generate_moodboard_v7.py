"""
POST /api/ai/generate-moodboard-v7

The fixed-garment moodboard endpoint. Same machinery as v6 (OpenAI Responses
with `image_generation` forced, SSE streaming, same host/image models, same
layout freedom, same chat-refine continuation turns) — the difference is how
the inputs are USED, and v7 drops every knob v6 carried:

  • selectedStyles are the EXACT GARMENTS. Each one already has its
    silhouette, fabric and colour decided in its reference image. The model
    reproduces every supplied garment EXACTLY, worn by a model — no
    restyling, no re-fabricing, no recolouring, no omissions, no invented
    garments. Up to 10 garments.
  • selectedFabrics are reference/fidelity images ONLY. The garments are
    already on their fabric — these exist so the model can reproduce the
    fine weave/knit structure faithfully and so the board can show tangible
    swatches. They are never assigned or applied to a garment. Up to 8
    fabrics.
  • selectedColours are DISPLAY-ONLY palette chips (Pantone TCX, resolved
    server-side from the hex) — they never recolour a garment.
  • season drives the BACKGROUND / mood only — SS → Spring/Summer,
    AW → Autumn/Winter.
  • prompt / briefContext set the THEME only.

There is no creativity slider, no trend-report design layer, and no
"imagine the garments" fallback — v7 always works from a fixed, fully
supplied garment set. A garment or fabric reference image that cannot be
fetched is dropped silently and the turn proceeds: the contract marks an
image-less garment "do NOT fabricate this garment" and omits image-less
fabrics from the swatch list, so the board never invents what it could not
see. Drops are logged server-side, and the `garment_refs` / `fabric_refs`
status events report fetched-vs-total.

Body shape is a subset of v6 (no `creativity_bias` / `trend_reports`).

SSE events: `meta`, `status`, `start`, `partial`, `reply`, `final`, `error`.
`status` is the only addition over v3/v4/v5/v6 — a per-stage progress event
(see services/progress.py) reporting measured work: reference-image fetches
one by one, palette resolution, contract composition, ref packing, then the
model-side stages driven off real OpenAI Responses stream events. Nothing is
simulated; a stage opens when the work starts and closes when it ends.

Because that reporting has to begin before the reference images are fetched,
the fetch now runs INSIDE the stream. An unfetchable garment/fabric reference
no longer fails the turn at all — it was once an HTTP 422, then an SSE `error`
event with `code: "reference_images_unfetchable"`; it is now simply dropped
and the generation continues. The cheap, instant validations (missing prompt,
missing selectedStyles, malformed referenceImages) are still HTTP 4xx as
before.
"""
from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncGenerator
from typing import Any


from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict
from sse_starlette.sse import EventSourceResponse

from moodboard_ai.config import get_settings
from moodboard_ai.services.contract_v7 import (
    compose_moodboard_contract_v7,
    normalize_keywords,
)
from moodboard_ai.services.fabric_refs import fetch_selected_fabric_images
from moodboard_ai.services.openai_responses import (
    ALLOWED_SIZES,
    DEFAULT_IMAGE_TOOL_MODEL,
    stream_moodboard_response_with_fallback,
)
from moodboard_ai.services.pantone import resolve_pantone_code
from moodboard_ai.services.progress import StatusEmitter
from moodboard_ai.services.reference_images import validate_reference_images
from moodboard_ai.services.style_refs import fetch_selected_style_images
from moodboard_ai.log import step

router = APIRouter()

# Soft hint appended to the instructions. The detailed rules live in
# compose_moodboard_contract_v7.
_MOODBOARD_HINT = (
    "A rich, free-flowing editorial moodboard built around the supplied "
    "finished garments (already in their own fabric and colour) — a hero "
    "title, a short story caption, palette chips with Pantone numbers, "
    "fabric swatches, garment crops, and multiple model shots, all woven "
    "into a layered, overlapping, NON-grid collage."
)

# Per-category caps for the OpenAI input_image budget. v7-specific — NOT
# shared with v3/v4/v5/v6's MAX_REFS_TO_RESPONSES. Garments are concatenated
# FIRST so they always survive the slice to _MAX_REFS_TO_MODEL.
_MAX_STYLE_REFS = 10
_MAX_FABRIC_REFS = 8
# Own total ref budget: 10 garments + 8 fabrics is the worst case (an
# unfetchable one is dropped, never replaced — see `_missing_refs` below),
# plus headroom for the previous-turn seed image (1) and user-attached
# referenceImages (up to reference_images.MAX_REFS == 3).
_MAX_REFS_TO_MODEL = 22


def _resolve_image_tool_model(image_model: str | None) -> str:
    if image_model == "openai":
        return "gpt-image-1"
    if image_model == "openai-1.5":
        return "gpt-image-1.5"
    if image_model == "openai-2":
        return "gpt-image-2"
    return DEFAULT_IMAGE_TOOL_MODEL


# The stage plan advertised in the `meta` event so the FE can render the full
# checklist up front instead of growing it as events land. Order is the order
# stages open; `image` and `model` overlap by design.
_STAGE_PLAN = [
    {"stage": "prepare", "label": "Preparing board inputs"},
    {"stage": "garment_refs", "label": "Fetching garment references"},
    {"stage": "fabric_refs", "label": "Fetching fabric references"},
    {"stage": "palette", "label": "Resolving Pantone codes"},
    {"stage": "contract", "label": "Composing the garment/fabric contract"},
    {"stage": "refs_packed", "label": "Packing reference images"},
    {"stage": "model_request", "label": "Sending the board request to OpenAI"},
    {"stage": "model", "label": "Model planning the board"},
    {"stage": "image", "label": "Rendering the board"},
    {"stage": "reply", "label": "Writing the accompanying note"},
    {"stage": "complete", "label": "Moodboard ready"},
]


def _ref_label(item: dict[str, Any]) -> str:
    """Best human-readable name for a supplied garment/fabric."""
    return str(
        item.get("name") or item.get("sku") or item.get("fabCode")
        or item.get("code") or item.get("id") or "(unnamed)"
    )


def _ref_candidates(items: list[dict[str, Any]] | None, limit: int) -> list[dict[str, Any]]:
    """
    The exact subset the fetchers will attempt — items with an imageUrl, capped
    at `limit`. Used so the progress `total` matches what actually gets fetched
    rather than the raw supplied count.
    """
    if not isinstance(items, list):
        return []
    return [i for i in items if isinstance(i, dict) and i.get("imageUrl")][:limit]


def _missing_refs(
    supplied: list[dict[str, Any]],
    fetched: list[dict[str, Any]],
    limit: int,
    fetched_key: str,
) -> list[str]:
    """
    Compare a supplied garment/fabric list against what actually got fetched
    and return human-readable labels for every item that failed — either it
    had no imageUrl at all, or the fetch dropped it (bad host, timeout,
    oversize, bad mime, etc.). Only the first `limit` supplied items are in
    scope (the same slice the fetcher itself applies).

    Identity is by object reference: the fetcher hands back the exact same
    dict it was given for every success, so `id()` reliably tells success
    from failure without needing a stable business key.
    """
    candidates = supplied[:limit]
    fetched_ids = {id(f[fetched_key]) for f in fetched}
    missing = []
    for c in candidates:
        if id(c) in fetched_ids:
            continue
        label = _ref_label(c)
        if not (isinstance(c.get("imageUrl"), str) and c["imageUrl"]):
            missing.append(f"{label} (no reference image supplied)")
        else:
            missing.append(f"{label} (reference image could not be fetched)")
    return missing


class V7Request(BaseModel):
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
    style_overview: dict[str, Any] | list[Any] | None = None
    # The season code, e.g. "AW25" / "SS26". SS → Spring/Summer, AW →
    # Autumn/Winter. Drives the background/mood only.
    season: str | None = None
    # `title` is the board's HERO TITLE (e.g. "Resort Drop") — when supplied it
    # is lettered on the board verbatim, as the dominant headline, instead of
    # the model inventing one. Whitespace is collapsed and it is capped at 80
    # chars (see clean_title): a hero line is a few words, and an image model
    # asked to letter a paragraph at display size mangles it.
    # `keywords` are cue words (e.g. ["poolside", "golden hour", "ruching"]) and
    # remain ART DIRECTION ONLY — never lettered, never allowed to change a
    # garment. Accepts a list or a comma-separated string. See
    # _compose_title_directive / _compose_direction_block in contract_v7.
    title: str | None = None
    keywords: list[str] | str | None = None
    imageModel: str | None = None
    size: str | None = None
    partialImages: int | None = None
    # image_generation quality ("low" | "medium" | "high" | "auto"). When
    # passed it overrides the server default; otherwise falls back to the
    # configured default. Higher quality notably improves hands/anatomy.
    quality: str | None = None


def _compose_style_overview_block(style_overview: Any) -> str:
    """
    Render the brand style overview JSON (passed from the FE) into a prompt
    block so the model knows which brand is being targeted. Accepts the raw
    style-overview object, a LIST of them (the shape the wizard sends), or the
    full API envelope ({"message": {"data": {...}}}), and narrows to the useful
    payload.

    Returns "" when nothing usable was supplied.
    """
    if not style_overview:
        return ""
    data: Any = style_overview
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


@router.post("/")
async def generate_moodboard_v7(req: Request, body: V7Request):
    prompt = (body.prompt or "").strip()
    if not prompt:
        return JSONResponse(status_code=400, content={"error": "Prompt is required"})
    if not get_settings().openai_api_key:
        return JSONResponse(
            status_code=500, content={"error": "OpenAI API key not configured"}
        )
    if not body.selectedStyles:
        return JSONResponse(
            status_code=400,
            content={"error": "selectedStyles is required — v7 builds the board from a fixed, supplied garment set"},
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

    use_thread = bool(body.previousResponseId) and not bool(body.rehydrate)
    is_continue = use_thread or bool(prev_validated)

    prev_for_refs = prev_validated if not use_thread else []

    previous_image_data_url = (
        f"data:{prev_validated[0]['mimeType']};base64,{prev_validated[0]['data']}"
        if prev_validated
        else None
    )
    prior_image_for_streamer = None if use_thread else previous_image_data_url

    resolved_partial_images = body.partialImages or (1 if is_continue else 2)
    trimmed_prompt = prompt

    t_req_holder: list[Any] = []

    event_queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()

    async def event_generator() -> AsyncGenerator[dict[str, Any], None]:
        yield {
            "event": "meta",
            "data": json.dumps({
                "mode": "thread" if use_thread else ("rehydrate" if body.rehydrate else "seed"),
                "imageToolModel": image_tool_model,
                "size": resolved_size,
                # What we asked the image tool for. `partialImages` is an
                # UPPER BOUND — OpenAI emits a preview only when it has a
                # meaningfully new frame, so fewer can arrive. The FE should
                # key its preview UI off the `partial` events it actually
                # receives (each carries `partialsReceived`), not off this.
                "partialImagesRequested": resolved_partial_images,
                # The stage plan. Each key below turns up as a `status` event
                # with state running → done (or failed). Stages are reported
                # from real work, never from a timer, so they do not arrive on
                # a fixed cadence and `image` may sit in `running` for minutes.
                "stages": _STAGE_PLAN,
            }, ensure_ascii=False),
        }

        def on_event_cb(ev: dict[str, Any]) -> None:
            try:
                event_queue.put_nowait(ev)
            except asyncio.QueueFull:
                pass

        status = StatusEmitter(on_event_cb)

        def _ref_progress(stage: str, label: str) -> Any:
            """Per-image callback — fires as each fetch settles, in race order."""
            def cb(p: dict[str, Any]) -> None:
                status.update(
                    stage,
                    label,
                    current=p["completed"],
                    total=p["total"],
                    item=_ref_label(p["item"]),
                    itemOk=p["ok"],
                )
            return cb

        async def run() -> None:
            try:
                style_candidates = _ref_candidates(body.selectedStyles, _MAX_STYLE_REFS)
                fabric_candidates = _ref_candidates(body.selectedFabrics, _MAX_FABRIC_REFS)

                status.start(
                    "prepare",
                    "Preparing board inputs",
                    garments=len(body.selectedStyles or []),
                    fabrics=len(body.selectedFabrics or []),
                    colours=len(body.selectedColours or []),
                )

                # --- reference images (the slowest non-model phase: up to 18
                # HTTP fetches plus a macro crop per fabric) ---
                status.start(
                    "garment_refs",
                    "Fetching garment references",
                    current=0,
                    total=len(style_candidates),
                )
                status.start(
                    "fabric_refs",
                    "Fetching fabric references",
                    current=0,
                    total=len(fabric_candidates),
                )
                style_images, fabric_images = await asyncio.gather(
                    fetch_selected_style_images(
                        body.selectedStyles,
                        _MAX_STYLE_REFS,
                        on_progress=_ref_progress("garment_refs", "Fetching garment references"),
                    ),
                    fetch_selected_fabric_images(
                        body.selectedFabrics,
                        _MAX_FABRIC_REFS,
                        macro=True,
                        on_progress=_ref_progress("fabric_refs", "Fetching fabric references"),
                    ),
                )
                status.done(
                    "garment_refs",
                    "Garment references ready",
                    fetched=len(style_images),
                    total=len(style_candidates),
                )
                status.done(
                    "fabric_refs",
                    "Fabric references ready",
                    fetched=len(fabric_images),
                    total=len(fabric_candidates),
                )

                # Unfetchable references are dropped, not fatal. The turn
                # proceeds with whatever came back: compose_moodboard_contract_v7
                # marks an image-less garment "do NOT fabricate this garment"
                # and omits image-less fabrics from the swatch list, so the
                # board stays honest instead of the whole generation failing on
                # one bad host. Logged server-side so the gap is traceable.
                missing_styles = _missing_refs(
                    body.selectedStyles or [], style_images, _MAX_STYLE_REFS, "style"
                )
                missing_fabrics = _missing_refs(
                    body.selectedFabrics or [], fabric_images, _MAX_FABRIC_REFS, "fabric"
                )
                if missing_styles or missing_fabrics:
                    dropped = []
                    if missing_styles:
                        dropped.append(f"garments: {', '.join(missing_styles)}")
                    if missing_fabrics:
                        dropped.append(f"fabrics: {', '.join(missing_fabrics)}")
                    print(
                        "[moodboard-v7] dropped unfetchable reference(s) — "
                        + "; ".join(dropped),
                        flush=True,
                    )

                # --- palette ---
                status.start(
                    "palette",
                    "Resolving Pantone codes",
                    total=len(body.selectedColours or []),
                )
                resolved_colours = [
                    {**c, "pantone": resolve_pantone_code(c)}
                    for c in (body.selectedColours or [])
                    if isinstance(c, dict)
                ]
                status.done(
                    "palette",
                    "Palette resolved",
                    resolved=sum(1 for c in resolved_colours if c.get("pantone")),
                    total=len(resolved_colours),
                )

                # --- contract ---
                status.start("contract", "Composing the garment/fabric contract")
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

                # Brand style overview block. Composed on every turn (the brand
                # DNA, like the garment/fabric lock, must persist across
                # continuation turns).
                style_overview_block = _compose_style_overview_block(body.style_overview)

                # Foundation contract is ALWAYS composed — the garment/fabric
                # lock persists on every turn (incl. continuation turns).
                foundation_block = compose_moodboard_contract_v7(
                    selected_fabrics=body.selectedFabrics,
                    selected_colours=resolved_colours,
                    selected_styles=body.selectedStyles,
                    fabrics_with_image=fabric_images,
                    styles_with_image=style_images,
                    season=body.season,
                    title=body.title,
                    keywords=body.keywords,
                )

                instructions = "\n\n".join(
                    b for b in (
                        brief_block,
                        style_overview_block,
                        foundation_block,
                        summary_block,
                        _MOODBOARD_HINT,
                    ) if b
                )
                status.done(
                    "contract",
                    "Contract composed",
                    chars=len(instructions),
                    hasBrief=bool(brief_block),
                    hasStyleOverview=bool(style_overview_block),
                    hasSummary=bool(summary_block),
                )

                # --- ref budget. Garments FIRST (highest priority), then
                # fabrics, then user attachments. Garments + fabrics only ever
                # shrink (unfetchable ones already dropped above), so only
                # prev/user refs can be squeezed by the cap. ---
                status.start("refs_packed", "Packing reference images")
                all_refs = [
                    *prev_for_refs,
                    *[{"mimeType": s["mimeType"], "data": s["data"]} for s in style_images],
                    *[{"mimeType": f["mimeType"], "data": f["data"]} for f in fabric_images],
                    *user_refs_v.images,
                ]
                packed_total = len(all_refs)
                all_refs = all_refs[:_MAX_REFS_TO_MODEL]
                status.done(
                    "refs_packed",
                    "Reference images packed",
                    sent=len(all_refs),
                    dropped=packed_total - len(all_refs),
                    budget=_MAX_REFS_TO_MODEL,
                    garments=len(style_images),
                    fabrics=len(fabric_images),
                    previous=len(prev_for_refs),
                    userAttached=len(user_refs_v.images),
                )
                status.done("prepare", "Inputs ready")

                print(
                    f"[moodboard-v7] request kind: "
                    f"{'CONTINUE (existing moodboard)' if is_continue else 'FRESH CREATION'} "
                    f"| garments={len(style_images)} fabrics={len(fabric_images)} "
                    f"season={body.season or '-'} title={body.title or '-'} "
                    f"keywords={len(normalize_keywords(body.keywords))}",
                    flush=True,
                )

                t_req = step("moodboard-v7", "request", {
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
                    "title": body.title or "",
                    "keywords": len(normalize_keywords(body.keywords)),
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
                t_req_holder.append(t_req)

                # The streamer drives the model-side stages (model_request,
                # model, image, reply) off real Responses stream events.
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
                    status=status,
                )
                status.done(
                    "complete",
                    "Moodboard ready",
                    partialImagesRequested=result.partial_images_requested,
                    partialImagesReceived=result.partial_images_received,
                    modelUsed=result.model_used,
                )
                event_queue.put_nowait({
                    "type": "__final__",
                    "responseId": result.response_id,
                    "src": result.src,
                    "reply": result.reply,
                    "responseCreatedAt": result.response_created_at,
                    "modelUsed": result.model_used,
                    "llm_prompt_whole": result.llm_prompt_whole,
                    # Reconciliation for the FE: how many previews we asked for
                    # vs how many OpenAI actually streamed, plus the raw event
                    # tally behind that number.
                    "partialImagesRequested": result.partial_images_requested,
                    "partialImagesReceived": result.partial_images_received,
                    "eventTally": result.event_tally,
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
                    error_payload = {
                        "error": "Generation cancelled" if aborted else msg,
                        "retriable": ev.get("retriable", not aborted),
                    }
                    # Input-validation failures that used to be an HTTP 4xx now
                    # arrive here, because the work that detects them runs
                    # inside the stream. `code` / `httpStatus` carry what the
                    # status line used to.
                    for k in ("code", "httpStatus", "stage"):
                        if ev.get(k) is not None:
                            error_payload[k] = ev[k]
                    yield {"event": "error", "data": json.dumps(error_payload, ensure_ascii=False)}
                else:
                    yield {"event": etype or "message", "data": json.dumps(ev, ensure_ascii=False)}
        except asyncio.CancelledError:
            run_task.cancel()
            raise

    return EventSourceResponse(event_generator(), ping=15)
