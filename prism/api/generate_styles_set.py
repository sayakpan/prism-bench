"""
POST /api/ai/generate-styles-set

SSE endpoint that turns a moodboard wizard's creative inputs into a set of
generated product packshots — one image per product, each with the scaffold
(gender, style category, garment name, fabric code, description) that the range
board needs.

HOW MANY PRODUCTS sits between 10 and 12. The floor is hard — a range board
needs filling — and the planner is walled in at it (see styles_set_design:
`set_size` enum, instructions, and a top-up pass when a plan lands short).
Where inside that band the range settles is the model's call, and the brief is
pushed WIDER rather than repeated to reach the floor: near-duplicates do not
count towards it.

THE SAME BRIEF DOES NOT PRODUCE THE SAME RANGE TWICE. The planner has no
sampling knob and the garment signals are a fixed list, so without help "cover
the signals" has exactly one obvious answer and every call returns it. Each
request therefore draws a `variationSeed` that names a structural axis the run
must lead with, and the instructions say plainly that a signal is a direction to
express rather than a product to transcribe. Pin the seed to get a range back.

THE TWO STAGES OVERLAP. The planner streams, so each product is dispatched to
the renderer the moment it is written rather than after the whole plan lands —
a set finishes in roughly the time planning alone used to take. Range-board copy
is written on the same overlap, since no render reads it.

A GARMENT'S BODY COLOUR IS ITS FABRIC'S COLOUR. Every fabric arrives already
dyed, so choosing the cloth chooses the colour: there is no body-colour decision
for the model to make, and `colours` (the palette card) reaches only prints,
trims and colour-blocking. The macro swatch attached to each render is the
authority on the shade; the mill's `color` text is the tie-breaker.

Body (application/json):
  garmentInspiration: [{"imageUrl", "productName"}]
                        Base inspiration. Read for CUT, PROPORTION and DETAIL
                        only — never for colour, fabric or print.
  garmentSignals:     {"description", "garments": [{"category","name",
                        "priority","reason","silhouette"}]}
                        The garment direction to realise.
  printDirection:     [{"printName", "imageUrl"}]  Print artwork, applied to
                        garments where it strengthens the piece. `printImage`
                        (a data: URL, as context-v2 returns) is accepted too.
  styleCategories:    ["Tops","Dresses"]   HARD LIMIT. Defaults to the
                        categories present in garmentSignals when omitted.
  genders:            ["Women"]            HARD LIMIT.
  fabrics:            [{"fabCode","fabBatch","quality","gsm","composition",
                        "imageUrl","name","color"}]   Every garment is cut from
                        one of these; the model assigns them freely.
                        `fabBatch` is the roll the cloth was cut from, and
                        `(fabCode, fabBatch)` is an entry's identity: two
                        entries sharing a code are two rolls, not a duplicate.
                        Only the FIRST of them is offered to the planner
                        though — the model picks a fabric by code alone, and
                        one code cannot name two rolls. The batch comes back
                        on every product cut from that fabric, verbatim.
                        `color` is the mill's free-text name for the shade this
                        cloth is dyed in — "BLACK", "Veiled pink", "CLOUD
                        DANCER 11-4201 TCX", "RFD". IT SETS THE GARMENT'S MAIN
                        BODY COLOUR (see below); a Pantone number inside it is
                        resolved to a hex, and ready-for-dye shorthand is
                        treated as undyed greige.
  colours:            [{"id","name","hex","pantone","selected"}]
                        The PALETTE. Used for prints, print recolouring, trims,
                        bindings and colour-blocked panels — NOT for main body
                        colours. Nothing outside this card and the fabrics'
                        own shades may appear anywhere.
  season:             <text>  optional, e.g. "SS27".
  brandOverview:      <text>  optional brand DNA context.
  maxProducts:        <int>   optional ceiling, clamped to 10-12. Values below
                        the floor are raised to it — this trims the range, it
                        cannot shrink it past MIN_PRODUCTS.
  variationSeed:      <text>  optional. Omit and every request gets a fresh
                        random seed, so the same brief yields a structurally
                        different range each time; pass one back (it is
                        returned on `meta`) to reproduce a range you liked.
                        The seed selects which structural axis — proportion,
                        length, construction, neckline, material spread, print
                        balance — the run is told to lead with.

SSE events, in order:
  meta            — the resolved request: constraints, input counts, the range
                    band (`minProducts`/`maxProducts`) and `variationSeed`
                    (echo it back to reproduce this range).
  stage           — {stage, status, ...} progress through reference fetching
                    and planning. Stages: fetching_references, planning.
  product_planned — one product's scaffold, emitted the instant the planner
                    closes it — while the rest of the range is still being
                    written and its own render is already running. Board copy
                    (`reason`, `fabricRationale`, `description`) is EMPTY here;
                    it is written concurrently and arrives filled in on `plan`
                    and `product`. Optional to consume: a client that ignores
                    it still gets the identical `plan`/`product` stream, just
                    with less to show early.
  plan            — the whole range plan, emitted when planning completes and
                    always BEFORE any `product`: {setSize, rangeSummary,
                    products: [scaffold...]}. Unchanged in shape.
  product         — one finished product: the full scaffold, its board copy and
                    `image` (a data: URL). Renders start as products are
                    planned, so images are usually ready by the time `plan`
                    fires; they are held back until then so this event never
                    precedes the plan. Completion order, not plan order —
                    `index` locates it.
  product_failed  — {index, garmentName, reason}. One dead render costs that
                    product, not the set.
  final           — {setSize, generated, failed, products: [...], ms}.
  error           — {error, retriable}. Failures are always an `error` event,
                    never a bare disconnect.

Every scaffold — on `product_planned`, `plan`, `product` and `final` alike —
carries `fabBatch` beside `fabricCode`. It is the batch that arrived on the
fabric the product was actually cut from, never a synthesised one, and it is
null when the request named no roll for that fabric.

Validation failures (missing constraints, missing API key) are returned as a
plain JSON 400/500 before the stream opens.
"""
from __future__ import annotations

import asyncio
import base64
import json
import time
import uuid
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse

from moodboard_ai.config import get_settings
from moodboard_ai.log import step
from moodboard_ai.services.fabric_refs import fetch_selected_fabric_images
from moodboard_ai.services.style_refs import fetch_selected_style_images
from moodboard_ai.services.styles_set_design import (
    MAX_PRODUCTS,
    MIN_PRODUCTS,
    copy_semaphore,
    normalize_colours,
    normalize_fabrics,
    normalize_inspiration,
    normalize_prints,
    plan_styles_set,
    write_product_copy,
)
from moodboard_ai.services.styles_set_images import render_product
from moodboard_ai.streaming import sse_event

router = APIRouter()


class StylesSetRequest(BaseModel):
    garmentInspiration: list[dict[str, Any]] = []
    garmentSignals: dict[str, Any] = {}
    printDirection: list[dict[str, Any]] = []
    styleCategories: Any = []
    genders: Any = []
    fabrics: list[dict[str, Any]] = []
    colours: list[dict[str, Any]] = []
    season: str = ""
    brandOverview: str = ""
    maxProducts: int | None = None
    variationSeed: str | None = None


def _as_list(raw: Any) -> list[str]:
    """Accept a list, a JSON array string, or a comma-separated string."""
    if isinstance(raw, str):
        text = raw.strip()
        if text.startswith("["):
            try:
                raw = json.loads(text)
            except (TypeError, ValueError):
                raw = text.split(",")
        else:
            raw = text.split(",")
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for v in raw:
        s = str(v).strip()
        if s and s.lower() not in seen:
            seen.add(s.lower())
            out.append(s)
    return out


def _decode_data_url(url: str) -> dict[str, str] | None:
    """`data:image/png;base64,...` → `{mimeType, data}`."""
    try:
        header, payload = url.split(",", 1)
        mime = header[5:].split(";")[0].strip() or "image/png"
        base64.b64decode(payload, validate=True)
    except Exception:
        return None
    return {"mimeType": mime, "data": payload}


async def _fetch_labeled(items: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """
    `[{label, imageUrl}]` → `{label: {mimeType, data}}`, dropping whatever
    could not be fetched.

    Data URLs are decoded in place — context-v2 hands prints back that way, so
    a caller chaining the two endpoints never has to re-host the artwork.
    """
    out: dict[str, dict[str, Any]] = {}
    remote: list[dict[str, Any]] = []
    for item in items:
        url = item.get("imageUrl") or ""
        if url.startswith("data:"):
            decoded = _decode_data_url(url)
            if decoded:
                out[item["label"]] = decoded
        elif url:
            remote.append(item)

    if remote:
        fetched = await fetch_selected_style_images(remote, limit=len(remote))
        for rec in fetched:
            out[rec["style"]["label"]] = {"mimeType": rec["mimeType"], "data": rec["data"]}
    return out


def _scaffold(product: dict[str, Any]) -> dict[str, Any]:
    """
    The FE-facing shape of one product, with or without its image yet.

    One shape, three events: `plan`, `product` and `final` all emit exactly
    this, so a field added here lands on all of them at once.

    `fabBatch` is spelled the wizard's way, not `fabricBatch`. These products
    are posted on to `save_generated_styles` verbatim, and that endpoint takes
    an explicit `fabBatch` as authoritative — so this key IS the one that
    persists. Rename it and the card paints correctly, the lot saves as
    nothing, and the bug stays invisible until a reload.
    """
    secondary = product.get("secondaryColour")
    return {
        "index": product["index"],
        "gender": product["gender"],
        "styleCategory": product["styleCategory"],
        "garmentName": product["garmentName"],
        "reason": product["reason"],
        "fabricCode": product["fabricCode"],
        "fabBatch": product["fabBatch"],
        "fabricName": product["fabric"]["name"],
        "fabricRationale": product["fabricRationale"],
        "description": product["description"],
        "colour": product["colour"],
        "secondaryColour": secondary,
        "colourTreatment": product["colourTreatment"],
        "printLabel": product["printLabel"],
        "printApplication": product["printApplication"],
        "signatureDetails": product["signatureDetails"],
        "designBrief": product["designBrief"],
    }


@router.post("")
@router.post("/")
async def generate_styles_set(req: Request):
    try:
        body = StylesSetRequest(**(await req.json()))
    except Exception as err:
        return JSONResponse(status_code=400, content={"error": f"invalid JSON body: {err}"})

    genders = _as_list(body.genders)
    categories = _as_list(body.styleCategories)
    signals = body.garmentSignals or {}
    if not categories:
        # The signals carry a category per garment; a caller that sent signals
        # but no explicit category list still gets a hard limit, derived.
        categories = _as_list([
            g.get("category") for g in (signals.get("garments") or []) if isinstance(g, dict)
        ])

    fabrics = normalize_fabrics(body.fabrics)
    colours = normalize_colours(body.colours)
    inspiration = normalize_inspiration(body.garmentInspiration)
    prints = normalize_prints(body.printDirection)

    missing = [name for name, value in (
        ("genders", genders),
        ("styleCategories", categories),
        ("fabrics", fabrics),
        ("colours", colours),
    ) if not value]
    if missing:
        return JSONResponse(
            status_code=400,
            content={"error": f"required and empty: {', '.join(missing)}"},
        )
    if not get_settings().openai_api_key:
        return JSONResponse(status_code=500, content={"error": "OpenAI API key not configured"})

    # Clamped at BOTH ends: a caller-supplied ceiling under the floor is a
    # request for a smaller range than the board supports, and loses to it.
    cap = (
        MAX_PRODUCTS if body.maxProducts is None
        else max(MIN_PRODUCTS, min(body.maxProducts, MAX_PRODUCTS))
    )
    season = body.season.strip()
    # A fresh seed per request unless the caller pins one. Identical inputs
    # otherwise produce an identical plan — the planner has no sampling knob and
    # the garment signals are a fixed list, so "cover the signals" has one
    # obvious answer. The seed picks the structural lens this run leads with; it
    # comes back on `meta` so a range can be reproduced or deliberately varied.
    seed = (body.variationSeed or "").strip() or uuid.uuid4().hex[:8]

    async def event_generator():
        started = time.monotonic()
        t_req = step("styles-set", "request", {
            "categories": ",".join(categories),
            "genders": ",".join(genders),
            "fabrics": len(fabrics),
            "colours": len(colours),
            "inspiration": len(inspiration),
            "prints": len(prints),
            "floor": MIN_PRODUCTS,
            "cap": cap,
        })

        try:
            yield sse_event("meta", {
                "styleCategories": categories,
                "genders": genders,
                "fabrics": [
                    {"code": f["code"], "name": f["name"], "colour": f["colour"]}
                    for f in fabrics
                ],
                "colours": colours,
                "inspirationImages": len(inspiration),
                "printOptions": [p["label"] for p in prints],
                "season": season,
                "minProducts": MIN_PRODUCTS,
                "maxProducts": cap,
                "variationSeed": seed,
            })

            # ── fetch every reference image ─────────────────────────────
            yield sse_event("stage", {
                "stage": "fetching_references",
                "status": "in_progress",
                "fabrics": len(fabrics),
                "inspiration": len(inspiration),
                "prints": len(prints),
            })
            # Fabric swatches are fetched as MACRO CROPS: the knit/weave
            # structure has to survive OpenAI's input downscale, and fabric
            # fidelity is the binding requirement of every render.
            fabric_fetched, inspiration_refs, print_refs = await asyncio.gather(
                fetch_selected_fabric_images(fabrics, limit=len(fabrics), macro=True),
                _fetch_labeled(inspiration),
                _fetch_labeled(prints),
            )
            fabric_refs = {
                rec["fabric"]["code"]: {"mimeType": rec["mimeType"], "data": rec["data"]}
                for rec in fabric_fetched
            }
            yield sse_event("stage", {
                "stage": "fetching_references",
                "status": "complete",
                "fabricSwatches": len(fabric_refs),
                "inspirationImages": len(inspiration_refs),
                "printArtworks": len(print_refs),
            })

            # ── plan the range, rendering each product AS IT IS PLANNED ──
            #
            # The planner streams. Every product it closes is dispatched to the
            # renderer immediately, so stage 2 runs underneath the rest of stage
            # 1 instead of queueing behind it — the set finishes in roughly the
            # time planning alone used to take. Board copy is written on the
            # same overlap: nothing in the render path reads it.
            yield sse_event("stage", {"stage": "planning", "status": "in_progress"})

            events: asyncio.Queue = asyncio.Queue()
            planned: list[dict[str, Any]] = []
            copy_tasks: dict[int, asyncio.Task] = {}
            render_tasks: list[asyncio.Task] = []
            range_summary = ""

            def _on_summary(text: str) -> None:
                nonlocal range_summary
                range_summary = text

            def _on_product(product: dict[str, Any]) -> None:
                events.put_nowait(("planned", product))

            async def _write_copy(product: dict[str, Any]) -> None:
                async with copy_semaphore():
                    product.update(await write_product_copy(
                        product,
                        range_summary=range_summary,
                        season=season,
                    ))

            plan_task = asyncio.ensure_future(plan_styles_set(
                inspiration_images=[
                    {**inspiration_refs[s["label"]], "label": s["label"]}
                    for s in inspiration if s["label"] in inspiration_refs
                ],
                print_images=[
                    {**print_refs[p["label"]], "label": p["label"]}
                    for p in prints if p["label"] in print_refs
                ],
                signals=signals,
                categories=categories,
                genders=genders,
                fabrics=fabrics,
                colours=colours,
                season=season,
                brand_overview=body.brandOverview,
                max_products=cap,
                variation_seed=seed,
                on_product=_on_product,
                on_summary=_on_summary,
            ))
            plan_task.add_done_callback(lambda _t: events.put_nowait(("plan_done", None)))

            done: dict[int, dict[str, Any]] = {}
            failed = 0
            in_flight = 0
            plan_finished = False
            plan_sent = False
            # `plan` still precedes every `product`, exactly as before streaming:
            # a render that lands early waits here rather than reordering the
            # stream under a client that builds its grid from the plan event.
            held: list[dict[str, Any]] = []

            try:
                while not plan_finished or in_flight > 0:
                    kind, payload = await events.get()

                    if kind == "planned":
                        planned.append(payload)
                        copy_tasks[payload["index"]] = asyncio.ensure_future(
                            _write_copy(payload)
                        )
                        task = asyncio.ensure_future(render_product(
                            payload,
                            fabric_refs=fabric_refs,
                            print_refs=print_refs,
                            inspiration_refs=inspiration_refs,
                            season_label=season,
                            range_summary=range_summary,
                            brand_overview=body.brandOverview,
                            palette=colours,
                        ))
                        task.add_done_callback(
                            lambda t, i=payload["index"]: events.put_nowait(("rendered", (i, t)))
                        )
                        render_tasks.append(task)
                        in_flight += 1
                        yield sse_event("product_planned", _scaffold(payload))
                        continue

                    if kind == "rendered":
                        index, task = payload
                        in_flight -= 1
                        rendered = None if task.cancelled() else task.result()
                        if rendered is None:
                            failed += 1
                            continue
                        # Copy is minutes cheaper than a packshot, so it has
                        # landed by now; awaiting it costs nothing and keeps the
                        # product event whole.
                        copy = copy_tasks.get(index)
                        if copy is not None:
                            await copy
                        scaffold = {
                            **_scaffold(rendered),
                            "image": rendered["image"],
                            "imageModel": rendered["imageModel"],
                        }
                        done[index] = scaffold
                        if plan_sent:
                            yield sse_event("product", scaffold)
                        else:
                            held.append(scaffold)
                        continue

                    # plan_done
                    plan_finished = True
                    plan = plan_task.result() if not plan_task.cancelled() else None
                    if plan is None and not planned:
                        t_req.fail("planning produced no usable products")
                        yield sse_event("error", {
                            "error": "Could not plan a product set from these inputs.",
                            "retriable": True,
                        })
                        return
                    if plan is not None:
                        range_summary = plan["rangeSummary"] or range_summary
                    products = planned
                    yield sse_event("stage", {
                        "stage": "planning",
                        "status": "complete",
                        "setSize": len(products),
                    })
                    yield sse_event("plan", {
                        "setSize": len(products),
                        "rangeSummary": range_summary,
                        "products": [_scaffold(p) for p in products],
                    })
                    plan_sent = True
                    for scaffold in held:
                        yield sse_event("product", scaffold)
                    held.clear()
            finally:
                for task in render_tasks:
                    task.cancel()
                for task in copy_tasks.values():
                    task.cancel()
                plan_task.cancel()

            products = planned
            for p in products:
                if p["index"] not in done:
                    yield sse_event("product_failed", {
                        "index": p["index"],
                        "garmentName": p["garmentName"],
                        "reason": "image generation failed",
                    })

            ms = int((time.monotonic() - started) * 1000)
            t_req.done({"planned": len(products), "generated": len(done), "failed": failed})
            yield sse_event("final", {
                "setSize": len(products),
                "rangeSummary": range_summary,
                "generated": len(done),
                "failed": failed,
                "products": [done[i] for i in sorted(done)],
                "ms": ms,
            })
        except asyncio.CancelledError:
            raise
        except Exception as err:
            t_req.fail(err)
            yield sse_event("error", {"error": str(err), "retriable": True})

    return EventSourceResponse(event_generator(), ping=15)
