"""
POST /api/ai/context-v2

SSE-based endpoint that derives the starting creative-direction "signals"
for a moodboard from a multipart/form-data payload: collection title/season/
audience/style categories, a free-text designer vision, per-brand DNA
overviews, inspiration images, and brief documents (xlsx/pdf/pptx).

Body (multipart/form-data):
  title:             <text>  Collection/project title. Required.
  season:            <text>  e.g. "SS27", "AW27".
  userVision:        <text>  Free-text designer vision. Required.
  brandIds:          <text>  JSON array of brand id strings, e.g. ["shein"].
  genders:           <text>  JSON array, e.g. ["Women"].
  styleCategories:   <text>  JSON array, e.g. ["Tops","Dresses"].
  briefDocuments:    <text>  JSON array of manifests:
                              [{"index","name","mimeType","sizeBytes","kind",
                                "url"?}]
  inspirationImages: <text>  JSON array of manifests:
                              [{"index","name","mimeType","sizeBytes","label",
                                "url"?}]
  brandOverviews:    <text>  JSON array of {"brandId", "overview": {...}} —
                              the brand DNA payload (colour strategy, allowed/
                              disallowed garment types, visual identity, ...).
  brandPreferences:  <text>  JSON object of the manufacturer's house
                              capability — circular-knit qualities and the
                              trend colour palette. Optional. See below.
  brief[N]:          <file>  binary for briefDocuments[N].
  inspiration[N]:    <file>  binary for inspirationImages[N].

Briefs and inspiration images may be sent as a BINARY PART or as a `url` on
the manifest entry, mixed freely within one request. A manifest entry with a
`url` and no matching binary part is fetched server-side (https only) during
the `fetching_remote_inputs` stage; when both are present the binary part
wins. A failed fetch costs only that one input — it is reported as an
`item_failed` event and the request continues.

brandPreferences is the TOP authority for fabric and colour, ranking above
brief documents (see moodboard_ai/services/context_v2_preferences.py). Its 26
circular-knit type names are mapped down onto the 9 allowed FABRIC_QUALITIES;
Jacquard Knit is a patterning structure rather than a construction and is
dropped. Its per-quality GSM bands SUPERSEDE the hardcoded bands in the
signals system prompt. Colour rows resolve their Pantone TCX to an exact hex
via moodboard_ai/services/pantone.py, so the model copies real values instead
of inventing them. All MRP/price columns are ignored — targetPrice/targetFob
have no derivation logic and remain null.

Each entry in colorPalette carries {name, hex, pantone}; `pantone` is copied
verbatim when the swatch came from brandPreferences, and otherwise backfilled
from the hex by resolve_pantone_code().

SSE events: meta -> stage (repeated, real-time progress) -> final OR error.

Implemented signals: colorPalette, moodTheme, keywordDirection,
printDirection, fabricDirection, garmentDirection, keyInsights (see
moodboard_ai/services/context_v2_signals.py and context_v2_wgsn.py). All of
them reuse the SAME WGSN general-trend grounding fetched once per request
(no duplicate Frappe/embedding calls per signal).

printDirection first looks for an explicit print signal in the supplied
inputs (images/documents/vision/brand DNA); only when none exists does it
fall back to WGSN print-focused trend rows. printDirection in the response
is NOT the raw analysis — it's an array of print swatch images:
    [{"printImage": "data:image/png;base64,...", "printName": str,
      "reason": str, "source": "generated" | "inspiration"}, ...]
The underlying analysis (has_print/motifs/scale/placement/description/
source) that drove the image prompts is returned separately as
printDirectionSignals, for traceability.

That array is TWO sets of swatches, in this order:
  source="generated"    one gpt-image-2 render per printDirection motif (low
                        quality, square, hard-capped at MAX_PRINTS)
  source="inspiration"  the prints ALREADY PRESENT in the uploaded
                        inspiration images, extracted rather than imagined —
                        isolated artwork is lifted whole, and a print worn on
                        a garment is located and cropped off it. Capped at
                        context_v2_print_images.MAX_INSPIRATION_PRINTS and
                        reported by the `inspiration_prints` stage event.
                        Reuses the /api/ai/extract-pdf-prints pipeline
                        (moodboard_ai/services/pdf_prints.py) and runs
                        CONCURRENTLY with everything from the WGSN fetch
                        onward, so it costs the request almost no wall time.
                        Purely additive: it never displaces a generated
                        swatch, and if it finds nothing the response is
                        exactly what it was before.

fabricDirection and garmentDirection are text-only recommendations (no image
generation): fabricDirection prioritises explicit fabric/composition/GSM
data in brief documents over the brand's materials_and_fabrics over WGSN
trend rows; garmentDirection prioritises the brand's allowed_garment_types
(filtered by style categories/gender/season) over explicit garment mentions
in the vision/brief over WGSN silhouette signals.

keyInsights is the collection's at-a-glance summary — exactly three entries,
always in this order:
    [{"topic": "silhouette",     "heading": str, "subheading": str},
     {"topic": "tones",          "heading": str, "subheading": str},
     {"topic": "style_summary",  "heading": str, "subheading": str}]

The remaining signals — targetPrice, targetFob — have no logic yet and are
returned as null; a `pending_signals` stage event announces them up front so
the frontend can render their sections immediately instead of waiting.
"""
from __future__ import annotations

import asyncio
import base64
import json
import re
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from sse_starlette.sse import EventSourceResponse
from starlette.datastructures import UploadFile

from moodboard_ai.config import get_settings
from moodboard_ai.log import step
from moodboard_ai.services.context_v2_inputs import (
    fetch_brief_url,
    fetch_inspiration_url,
    parse_brief_document,
    validate_inspiration_images,
)
from moodboard_ai.services.context_v2_preferences import parse_brand_preferences
from moodboard_ai.services.context_v2_print_images import (
    MAX_PRINTS,
    extract_inspiration_swatches,
    generate_one_swatch,
)
from moodboard_ai.services.context_v2_signals import derive_context_signals
from moodboard_ai.services.context_v2_wgsn import fetch_wgsn_grounding, format_wgsn_context
from moodboard_ai.services.pantone import resolve_pantone_code
from moodboard_ai.services.reference_images import to_claude_image_blocks
from moodboard_ai.streaming import sse_event

router = APIRouter()

_BRIEF_KEY_RE = re.compile(r"^brief\[(\d+)\]$")
_INSPIRATION_KEY_RE = re.compile(r"^inspiration\[(\d+)\]$")

# Signals with no derivation logic yet — announced as pending, returned null.
_PENDING_SIGNALS = [
    "targetPrice",
    "targetFob",
]


def _parse_json_field(raw: Any, default: Any) -> Any:
    if not raw:
        return default
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return default


def _url_jobs(
    manifest: Any, uploaded: dict[int, UploadFile], kind: str
) -> list[dict[str, Any]]:
    """
    Manifest entries that need a server-side fetch: those carrying a `url`
    with no matching binary part. A binary part always wins — if the FE sent
    both, the upload is already in hand and refetching it would be waste.
    """
    jobs: list[dict[str, Any]] = []
    if not isinstance(manifest, list):
        return jobs
    for meta in manifest:
        if not isinstance(meta, dict):
            continue
        url = str(meta.get("url") or "").strip()
        idx = meta.get("index")
        if not url or idx in uploaded:
            continue
        jobs.append({
            "kind": kind,
            "index": idx,
            "name": meta.get("name") or f"{kind}-{idx}",
            "url": url,
        })
    return jobs


async def _run_fetch(job: dict[str, Any]) -> dict[str, Any]:
    """Fetch one job. Never raises — failure is reported in the return dict."""
    fetcher = fetch_brief_url if job["kind"] == "brief" else fetch_inspiration_url
    try:
        body, mime = await fetcher(job["url"], label=job["name"])
    except Exception as err:
        return {**job, "ok": False, "reason": str(err)}
    return {
        **job,
        "ok": True,
        "body": body,
        "mimeType": mime,
        "sizeBytes": len(body),
    }


@router.post("/")
async def context_v2(req: Request):
    form = await req.form()

    title = str(form.get("title") or "").strip()
    season = str(form.get("season") or "").strip()
    user_vision = str(form.get("userVision") or "").strip()
    brand_ids = _parse_json_field(form.get("brandIds"), [])
    genders = _parse_json_field(form.get("genders"), [])
    style_categories = _parse_json_field(form.get("styleCategories"), [])
    brief_manifest = _parse_json_field(form.get("briefDocuments"), [])
    inspiration_manifest = _parse_json_field(form.get("inspirationImages"), [])
    brand_overviews = _parse_json_field(form.get("brandOverviews"), [])
    brand_preferences = parse_brand_preferences(
        _parse_json_field(form.get("brandPreferences"), {})
    )

    if not title or not user_vision:
        return JSONResponse(
            status_code=400, content={"error": "title and userVision are required"}
        )
    if not get_settings().anthropic_api_key:
        return JSONResponse(
            status_code=500, content={"error": "Anthropic API key not configured"}
        )

    brief_files: dict[int, UploadFile] = {}
    inspiration_files: dict[int, UploadFile] = {}
    for key, value in form.multi_items():
        if not isinstance(value, UploadFile):
            continue
        m = _BRIEF_KEY_RE.match(key)
        if m:
            brief_files[int(m.group(1))] = value
            continue
        m = _INSPIRATION_KEY_RE.match(key)
        if m:
            inspiration_files[int(m.group(1))] = value

    async def event_generator():
        yield sse_event("meta", {
            "title": title,
            "season": season,
            "brandIds": brand_ids,
            "genders": genders,
            "styleCategories": style_categories,
            "briefDocuments": len(brief_manifest),
            "inspirationImages": len(inspiration_manifest),
        })

        # Held outside the try so the failure path can cancel it — a
        # background extraction outliving the request it belongs to would keep
        # billing vision calls for a response nobody will ever receive.
        inspiration_task: asyncio.Task[Any] | None = None

        t_req = step("context-v2", "request", {
            "title": title,
            "season": season,
            "briefDocs": len(brief_manifest),
            "inspirationImgs": len(inspiration_manifest),
            "brands": ",".join(brand_ids) if brand_ids else "-",
        })

        try:
            # Announce the not-yet-implemented signals up front so the FE can
            # render their sections without waiting on this request at all.
            yield sse_event("stage", {
                "stage": "pending_signals",
                "status": "pending",
                "fields": _PENDING_SIGNALS,
            })

            yield sse_event("stage", {
                "stage": "brand_preferences",
                "status": "complete",
                "supplied": not brand_preferences.is_empty,
                **brand_preferences.stats(),
            })

            # ── fetch URL-supplied briefs/images ────────────────────────
            # Manifest entries may carry a `url` instead of a binary part.
            # Fetched concurrently; a dead link fails only its own item.
            fetch_jobs = [
                *_url_jobs(brief_manifest, brief_files, "brief"),
                *_url_jobs(inspiration_manifest, inspiration_files, "inspiration"),
            ]
            yield sse_event("stage", {
                "stage": "fetching_remote_inputs",
                "status": "in_progress",
                "total": len(fetch_jobs),
                "briefs": sum(1 for j in fetch_jobs if j["kind"] == "brief"),
                "images": sum(1 for j in fetch_jobs if j["kind"] == "inspiration"),
            })
            fetched_briefs: dict[int, tuple[bytes, str]] = {}
            fetched_images: dict[int, tuple[bytes, str]] = {}
            fetch_ok = 0
            fetch_failed = 0
            if fetch_jobs:
                for coro in asyncio.as_completed(
                    [_run_fetch(job) for job in fetch_jobs]
                ):
                    outcome = await coro
                    if outcome["ok"]:
                        fetch_ok += 1
                        target = (
                            fetched_briefs
                            if outcome["kind"] == "brief"
                            else fetched_images
                        )
                        target[outcome["index"]] = (
                            outcome.pop("body"),
                            outcome["mimeType"],
                        )
                    else:
                        fetch_failed += 1
                    yield sse_event("stage", {
                        "stage": "fetching_remote_inputs",
                        "status": "item_complete" if outcome["ok"] else "item_failed",
                        "kind": outcome["kind"],
                        "index": outcome["index"],
                        "name": outcome["name"],
                        "mimeType": outcome.get("mimeType"),
                        "sizeBytes": outcome.get("sizeBytes"),
                        "reason": outcome.get("reason"),
                    })
            yield sse_event("stage", {
                "stage": "fetching_remote_inputs",
                "status": "complete",
                "fetched": fetch_ok,
                "failed": fetch_failed,
            })

            # ── parse brief documents ───────────────────────────────────
            yield sse_event("stage", {
                "stage": "parsing_documents",
                "status": "in_progress",
                "total": len(brief_manifest),
            })
            document_blocks: list[dict[str, Any]] = []
            parsed_ok = 0
            parsed_failed = 0
            for meta in brief_manifest:
                if not isinstance(meta, dict):
                    continue
                idx = meta.get("index")
                name = meta.get("name") or f"document-{idx}"
                upload = brief_files.get(idx)
                fetched = fetched_briefs.get(idx)
                if upload is not None:
                    raw = await upload.read()
                    mime = meta.get("mimeType") or upload.content_type
                elif fetched is not None:
                    # Trust the fetched content-type over the manifest's — the
                    # manifest is a client-side guess, the response header is not.
                    raw, mime = fetched[0], fetched[1]
                else:
                    parsed_failed += 1
                    yield sse_event("stage", {
                        "stage": "parsing_documents",
                        "status": "document_failed",
                        "index": idx,
                        "name": name,
                        "reason": (
                            "url fetch failed"
                            if meta.get("url")
                            else "file not received"
                        ),
                    })
                    continue
                parsed = parse_brief_document(
                    index=idx,
                    name=name,
                    mime_type=mime,
                    raw_bytes=raw,
                )
                if parsed.ok:
                    parsed_ok += 1
                    document_blocks.extend(parsed.content_blocks)
                else:
                    parsed_failed += 1
                yield sse_event("stage", {
                    "stage": "parsing_documents",
                    "status": "document_complete" if parsed.ok else "document_failed",
                    "index": idx,
                    "name": name,
                    "kind": parsed.kind,
                    "reason": parsed.error,
                })
            yield sse_event("stage", {
                "stage": "parsing_documents",
                "status": "complete",
                "parsed": parsed_ok,
                "failed": parsed_failed,
            })

            # ── prepare inspiration images ──────────────────────────────
            yield sse_event("stage", {
                "stage": "preparing_images",
                "status": "in_progress",
                "total": len(inspiration_manifest),
            })
            raw_images: list[dict[str, str]] = []
            for meta in inspiration_manifest:
                if not isinstance(meta, dict):
                    continue
                idx = meta.get("index")
                upload = inspiration_files.get(idx)
                fetched = fetched_images.get(idx)
                if upload is not None:
                    raw = await upload.read()
                    mime = meta.get("mimeType") or upload.content_type or ""
                elif fetched is not None:
                    raw, mime = fetched[0], fetched[1]
                else:
                    continue
                raw_images.append({
                    "mimeType": mime,
                    "data": base64.b64encode(raw).decode("ascii"),
                })
            valid_images, rejected = validate_inspiration_images(raw_images)
            image_blocks = to_claude_image_blocks(valid_images)
            yield sse_event("stage", {
                "stage": "preparing_images",
                "status": "complete",
                "valid": len(valid_images),
                "rejected": rejected,
            })

            # ── prints already IN the inspiration images ────────────────
            # Started here and collected after the generated swatches, so
            # its vision calls overlap the WGSN fetch, the signals call and
            # the gpt-image-2 batch. It depends on nothing but the images,
            # so run sequentially it would be pure added latency; run this
            # way it is usually finished before it is awaited.
            inspiration_task = asyncio.create_task(
                extract_inspiration_swatches(valid_images)
            )

            # ── WGSN trend grounding (general context + print-focused fallback) ──
            yield sse_event("stage", {"stage": "wgsn_grounding", "status": "in_progress"})
            wgsn = await asyncio.to_thread(
                fetch_wgsn_grounding,
                season=season,
                genders=genders,
                style_categories=style_categories,
                user_vision=user_vision,
            )
            yield sse_event("stage", {
                "stage": "wgsn_grounding",
                "status": "complete",
                "totalMatched": wgsn.total_matched,
                "generalSignals": len(wgsn.general_candidates),
                "printSignals": len(wgsn.print_candidates),
                "seasonFallback": wgsn.season_fallback_used,
                "genderFallback": wgsn.gender_fallback_used,
                "categoryFallback": wgsn.category_fallback_used,
                "error": wgsn.error,
            })

            # ── derive color palette / mood theme / keyword+print direction / key insights ──
            yield sse_event("stage", {"stage": "creative_direction", "status": "in_progress"})
            signals = await derive_context_signals(
                title=title,
                season=season,
                user_vision=user_vision,
                brand_ids=brand_ids,
                genders=genders,
                style_categories=style_categories,
                brand_overviews=brand_overviews,
                brand_preferences=brand_preferences,
                document_blocks=document_blocks,
                image_blocks=image_blocks,
                wgsn_general_context=format_wgsn_context(wgsn.general_candidates),
                wgsn_print_context=format_wgsn_context(wgsn.print_candidates),
            )
            yield sse_event("stage", {"stage": "creative_direction", "status": "complete"})

            # ── render each print motif as a standalone swatch image ───
            motifs = signals.print_direction.motifs[:MAX_PRINTS]
            yield sse_event("stage", {
                "stage": "print_images",
                "status": "in_progress",
                "total": len(motifs),
            })
            async def _indexed_swatch(i: int, motif: Any) -> tuple[int, dict[str, Any]]:
                result = await generate_one_swatch(
                    motif_name=motif.name,
                    technique=motif.technique,
                    scale=motif.scale,
                    placement=motif.placement,
                    reason=motif.reason,
                )
                return i, result

            indexed_results: dict[int, dict[str, Any]] = {}
            if motifs:
                tasks = [_indexed_swatch(i, m) for i, m in enumerate(motifs)]
                for coro in asyncio.as_completed(tasks):
                    idx, result = await coro
                    indexed_results[idx] = result
                    yield sse_event("stage", {
                        "stage": "print_images",
                        "status": "image_complete" if result.get("printImage") else "image_failed",
                        "printName": result.get("printName"),
                    })
            print_images = [
                {**indexed_results[i], "source": "generated"}
                for i in sorted(indexed_results)
            ]
            yield sse_event("stage", {
                "stage": "print_images",
                "status": "complete",
                "generated": sum(1 for r in print_images if r.get("printImage")),
                "failed": sum(1 for r in print_images if not r.get("printImage")),
            })

            # ── append the prints found in the inspiration images ───────
            # Appended, never substituted: the generated motifs answer the
            # brief, these are what the designer already put in front of us.
            inspiration_prints, inspiration_stats = await inspiration_task
            print_images.extend(inspiration_prints)
            yield sse_event("stage", {
                "stage": "inspiration_prints",
                "status": "complete",
                "images": len(valid_images),
                **inspiration_stats,
            })

            # Backfill the TCX for any swatch the model invented rather than
            # took from the preferences palette, so every chip carries one —
            # same helper the v4-v7 moodboard routes use.
            color_palette = [
                {**c, "pantone": c.get("pantone") or resolve_pantone_code(c)}
                for c in (s.model_dump() for s in signals.color_palette)
            ]

            final_payload = {
                "colorPalette": color_palette,
                "fabricDirection": signals.fabric_direction.model_dump(),
                "garmentDirection": signals.garment_direction.model_dump(),
                "printDirection": print_images,
                "printDirectionSignals": signals.print_direction.model_dump(),
                "moodTheme": signals.mood_theme.model_dump(),
                "keywordDirection": signals.keyword_direction,
                "targetPrice": None,
                "targetFob": None,
                "keyInsights": [k.model_dump() for k in signals.key_insights],
            }
            t_req.done({
                "colors": len(color_palette),
                "prints": len(print_images),
                "insights": len(final_payload["keyInsights"]),
            })
            yield sse_event("final", final_payload)
        except Exception as err:
            t_req.fail(err)
            yield sse_event("error", {"error": str(err), "retriable": True})
        finally:
            if inspiration_task is not None and not inspiration_task.done():
                inspiration_task.cancel()

    return EventSourceResponse(event_generator(), ping=15)
