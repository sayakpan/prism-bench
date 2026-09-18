"""
POST /api/ai/analyze-prints

Analyse garment artwork images and extract, PER PRINT, the costing inputs:
print type (most-expensive type detected), length + width (in), number of
colours, and coverage (% of the garment panel the print covers). One article →
ONE costing, so all multiplicity (colour options / variants / multiple types on
one placement) is resolved into a single set of print rows.

Each entry in the payload carries an `id` plus a `front` and `back` image URL
(either may be null). Both views are analysed and merged into ONE prints array
per id, each row tagged with the `view` it came from. The result for each id is
streamed back over SSE as soon as that id finishes (ids run in PARALLEL,
capped, so a fast one isn't blocked behind a slow one).

Per-id pipeline:
  1. Server-side fetch of each present https image URL (no CORS, can pull the
     S3-hosted source the FE references).
  2. ONE vision call per view (structured output) returning, per
     genuinely-distinct print: placement, the print types present (constrained
     to the allowed `print_types` vocabulary), length/width, colour count, and
     coverage %.
  3. Deterministic post-processing: the single `print_type` is chosen IN CODE
     as the most-expensive type present, ranked by the `cost_per_inch` values
     supplied in the payload — never left to the model.

Body (application/json):
  {
    "images": [                             # required, non-empty
      {"id": "abc", "front": "https://...", "back": "https://..."},
      {"id": "def", "front": "https://...", "back": null}
    ],
    "print_types": [                         # shared vocabulary + costing inputs
      {"print_type": "Foil", "cost_per_inch": 0.5, "manpower_cost": 8.0},
      ...
    ]
  }

SSE events:
  meta   → {model, count}
  ping   → every 15s (keep-alive)
  result → {id, prints: [...]}            one per id, emitted as it finishes
           {id, prints: [], error: "..."} on a per-id failure
  done   → {count, ms}
  error  → fatal stream error

Each print row:
  {print_no, view, placement, print_type, length_in, width_in,
   no_of_colours, coverage_pct}
"""
from __future__ import annotations

import asyncio
import base64
import json
import time
from collections.abc import AsyncGenerator
from typing import Any
from urllib.parse import urlparse

import httpx
from fastapi import APIRouter
from fastapi.responses import JSONResponse
from openai import AsyncOpenAI
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse

from moodboard_ai.config import get_settings
from moodboard_ai.log import step

router = APIRouter()

# ── Hardcoded knobs ──────────────────────────────────────────────────────────
# Reading printed dimension callouts and counting distinct colours needs strong
# vision; gpt-4o reads small artwork text far more reliably than the -mini tier
# used by the classifier endpoint. Swap here if cost matters more than accuracy.
_ANALYSIS_MODEL = "gpt-4o"
_MAX_TOKENS = 2000
_CONCURRENCY = 5  # max ids analysed at once

_VIEWS = ("front", "back")

# ── Upload / fetch limits ────────────────────────────────────────────────────
_ALLOWED_MIMES = frozenset({"image/png", "image/jpeg", "image/webp"})
_MAX_BYTES = 20 * 1024 * 1024
_FETCH_TIMEOUT_S = 15.0
_CALL_TIMEOUT_S = 120.0


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


# ── Request models ───────────────────────────────────────────────────────────
class CostOption(BaseModel):
    """A single print type with its costing inputs.

    The name may arrive under `print_type` or `name` (the payload uses
    `print_type`). `cost_per_inch` drives the most-expensive selection;
    `manpower_cost` is carried through for context only.
    """

    print_type: str | None = None
    name: str | None = None
    cost_per_inch: float | None = None
    manpower_cost: float | None = None

    @property
    def label(self) -> str:
        return (self.print_type or self.name or "").strip()


class ImageItem(BaseModel):
    id: str
    front: str | None = None
    back: str | None = None


class AnalyzePrintsRequest(BaseModel):
    images: list[ImageItem] = []
    print_types: list[CostOption] = []


# ── Structured-output schema (model side) ────────────────────────────────────
# `print_type` is intentionally NOT requested from the model — it is resolved
# deterministically in code from `print_types_present` + the payload cost table.
_PRINT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["prints"],
    "properties": {
        "prints": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "placement",
                    "print_types_present",
                    "length_in",
                    "width_in",
                    "no_of_colours",
                    "coverage_pct",
                ],
                "properties": {
                    "placement": {"type": ["string", "null"]},
                    "print_types_present": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "length_in": {"type": ["number", "null"]},
                    "width_in": {"type": ["number", "null"]},
                    "no_of_colours": {"type": ["integer", "null"]},
                    "coverage_pct": {"type": ["number", "null"]},
                },
            },
        }
    },
}


_ANALYSIS_PROMPT = """You are a garment print-costing analyst. Look at the image and extract, PER PRINT, the inputs needed to cost the article. The article produces ONE costing — resolve ALL multiplicity into a single set of print rows.

CORE DEFINITIONS
- PRINT: a distinct placement/artwork on the garment (e.g. chest print, back print, sleeve print). Genuinely different placements are SEPARATE prints → one row each.
- PRINT TYPE: the application method (Puff, Non-PVC, Foil, etc.). A single print may use more than one print type.
- OPTION / COLOUR VARIANT: the SAME article shown in different colourways (Option A / Option B / Colour 1 / Red / Blue / White). These are NOT separate prints and NOT separate rows.

SELECTION RULES (resolve multiplicity — never expose a picker)
1. MULTIPLE PRINT TYPES ON ONE PRINT → list ALL of them in print_types_present. (The single most-expensive one is chosen downstream; do not split a print into separate rows per type.)
2. MULTIPLE COLOUR VARIANTS / OPTIONS of the same article → collapse to a SINGLE row:
   - print_types_present → the union of types seen across variants
   - no_of_colours → the HIGHEST colour count seen across variants
   - length / width → the LARGEST applicable value seen across variants
3. MULTIPLE GENUINELY DIFFERENT PRINTS (e.g. chest vs back) → keep as separate rows; apply rules 1–2 within each print independently.

LENGTH & WIDTH (inches)
- If the artwork has measurement callouts, read length and width from them and use them as-is.
- If NO callouts are present (e.g. a clean product photo), ESTIMATE length and width in inches from the print's size relative to the garment. Scale against standard adult garment panel dimensions as the reference: a front/back body panel is roughly 20 in wide × 28 in tall; a sleeve roughly 8 in wide. So a small chest logo ≈ 3–4 in, a standard chest print ≈ 9–11 in, a large front graphic ≈ 12–16 in. Keep length × width roughly consistent with coverage_pct against the panel. Round to one decimal. Always return numbers in this case — do NOT leave them null.
- Only leave a value null if the print itself is too unclear to judge its size at all.

NUMBER OF COLOURS
- Count the distinct print colours for each print. If a legend states a total and a type-specific subset, prefer the count for the selected (most-expensive) type's scope; otherwise use total visible colours. If not legible, leave null.

COVERAGE (coverage_pct)
- Estimate what percentage (0–100) of the garment panel this print covers, based on the print's visual size relative to the garment in the image. A small chest logo ≈ 5–15; a large front graphic ≈ 40–70; an all-over print ≈ 90–100. If the garment is not visible enough to judge, leave null.

PRINT TYPE VOCABULARY
print_types_present MUST use ONLY names from this allowed list (match exactly, case-sensitive):
{print_types_list}
For a real print, NEVER leave print_types_present empty — if the exact technique isn't clearly visible (common in flat product photos), still put your single best-guess technique. When you truly cannot tell, default to the most common standard screen-print technique (prefer "Pigment" if it's in the list).

Cost-per-inch per type (higher = more expensive — for your reference only; the final pick is computed downstream):
{cost_list}

OUTPUT
Return the prints array. placement is one of chest/back/sleeve/… or null when unclear. Emit one object per genuinely-distinct print after all collapsing. If the garment has NO print at all, return an EMPTY array ([]) — do NOT invent a placeholder row with null values."""


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
    """Server-side fetch of an https URL into a buffer. Validates mime + size.

    Returns (buffer, mime_type). Raises ValueError with a clear message on
    failure. The server has no CORS constraint, so it can pull the S3-hosted
    source images the FE references directly.
    """
    if not url.lower().startswith("https://"):
        raise ValueError(f'image url must start with "https://" (got {url[:32]}...)')
    async with httpx.AsyncClient() as client:
        resp = await client.get(url, timeout=_FETCH_TIMEOUT_S, follow_redirects=True)
        if resp.status_code >= 400:
            raise ValueError(
                f"image url fetch failed: {resp.status_code} {resp.reason_phrase}"
            )
        declared = (resp.headers.get("content-type") or "").split(";")[0].strip().lower()
        mime = declared if declared in _ALLOWED_MIMES else _guess_mime_from_url(url)
        if not mime or mime not in _ALLOWED_MIMES:
            raise ValueError(
                f'image url returned unsupported content-type "{declared or "(none)"}"'
            )
        body = resp.content
        if len(body) > _MAX_BYTES:
            raise ValueError(f"image url body too large ({len(body)}B > {_MAX_BYTES}B)")
        return body, mime


def _build_cost_map(
    print_types: list[CostOption],
) -> dict[str, tuple[str, float]]:
    """Build {normalized_name: (canonical_name, cost_per_inch)} from print_types.

    A missing cost_per_inch counts as 0.0 so it never wins the "most expensive"
    tie-break by accident.
    """
    cost_map: dict[str, tuple[str, float]] = {}
    for opt in print_types:
        label = opt.label
        if not label:
            continue
        cost = float(opt.cost_per_inch) if opt.cost_per_inch is not None else 0.0
        cost_map[label.lower()] = (label, cost)
    return cost_map


def _select_print_type(
    types_present: list[str], cost_map: dict[str, tuple[str, float]]
) -> str | None:
    """Pick the most-expensive print type by payload cost_per_inch.

    Falls back to the first detected type when none match the cost table (so an
    unknown-but-real type still yields a print_type rather than silently
    dropping the row). Returns None only when nothing was detected.
    """
    best_name: str | None = None
    best_cost = float("-inf")
    for t in types_present:
        entry = cost_map.get((t or "").strip().lower())
        if entry is None:
            continue
        canonical, cost = entry
        if cost > best_cost:
            best_cost = cost
            best_name = canonical
    if best_name is None and types_present:
        return types_present[0]
    return best_name


def _cheapest_type(cost_map: dict[str, tuple[str, float]]) -> str | None:
    """Lowest-cost allowed print type — the safe default when the model can't
    identify the technique, so a fallback never inflates the costing."""
    if not cost_map:
        return None
    canonical, _cost = min(cost_map.values(), key=lambda v: v[1])
    return canonical


def _build_prompt(print_types: list[CostOption]) -> str:
    """Inject the allowed print-type vocabulary + cost table into the prompt."""
    names = [o.label for o in print_types if o.label]
    print_types_list = "\n".join(f"- {n}" for n in names) or "- (none supplied)"

    cost_lines = []
    for o in print_types:
        if not o.label:
            continue
        cpi = o.cost_per_inch if o.cost_per_inch is not None else 0.0
        cost_lines.append(f"- {o.label}: {cpi}")
    cost_list = "\n".join(cost_lines) or "- (none supplied)"

    return _ANALYSIS_PROMPT.format(
        print_types_list=print_types_list, cost_list=cost_list
    )


def _resolve_print(
    raw: dict[str, Any],
    print_no: int,
    view: str,
    cost_map: dict[str, tuple[str, float]],
) -> dict[str, Any]:
    """Turn one model print object into the final costing row.

    `print_type` is computed here (most expensive type by payload cost).
    """
    types_present = [
        t.strip()
        for t in (raw.get("print_types_present") or [])
        if isinstance(t, str) and t.strip()
    ]
    return {
        "print_no": print_no,
        "view": view,
        "placement": raw.get("placement"),
        "print_type": _select_print_type(types_present, cost_map),
        "length_in": raw.get("length_in"),
        "width_in": raw.get("width_in"),
        "no_of_colours": raw.get("no_of_colours"),
        "coverage_pct": raw.get("coverage_pct"),
    }


def _is_empty_row(row: dict[str, Any]) -> bool:
    """A row carrying no usable signal — a placeholder the model shouldn't have
    emitted for a no-print garment. Drop these so callers get [] not [nulls]."""
    return all(
        row.get(k) in (None, "")
        for k in (
            "placement",
            "print_type",
            "length_in",
            "width_in",
            "no_of_colours",
            "coverage_pct",
        )
    )


async def _analyze_image(
    client: AsyncOpenAI,
    image_buffer: bytes,
    image_mime: str,
    prompt: str,
    view: str,
    cost_map: dict[str, tuple[str, float]],
) -> list[dict[str, Any]]:
    """Run the single vision call for one view → list of resolved print rows."""
    data_url = (
        f"data:{image_mime};base64,{base64.b64encode(image_buffer).decode('ascii')}"
    )
    view_note = f"\n\nThis image is the {view.upper()} view of the garment."
    res = await asyncio.wait_for(
        client.chat.completions.create(
            model=_ANALYSIS_MODEL,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt + view_note},
                        {
                            "type": "image_url",
                            "image_url": {"url": data_url, "detail": "high"},
                        },
                    ],
                }
            ],
            response_format={
                "type": "json_schema",
                "json_schema": {
                    "name": "print_analysis",
                    "schema": _PRINT_SCHEMA,
                    "strict": True,
                },
            },
            max_tokens=_MAX_TOKENS,
        ),
        timeout=_CALL_TIMEOUT_S,
    )
    content = res.choices[0].message.content if res.choices else None
    if not content:
        raise RuntimeError("analysis returned empty content")
    parsed = json.loads(content)
    raw_prints = parsed.get("prints") if isinstance(parsed, dict) else None
    if not isinstance(raw_prints, list):
        raise RuntimeError("analysis returned no 'prints' array")

    rows: list[dict[str, Any]] = []
    for raw in raw_prints:
        if not isinstance(raw, dict):
            continue
        row = _resolve_print(raw, len(rows) + 1, view, cost_map)
        if _is_empty_row(row):
            continue  # no-print placeholder → drop so the array stays empty
        # print_type is mandatory on a costable row — if the model couldn't name
        # a technique, default to the cheapest allowed type (never null).
        if row["print_type"] is None:
            row["print_type"] = _cheapest_type(cost_map)
        rows.append(row)
    return rows


async def _process_one(
    client: AsyncOpenAI,
    item: ImageItem,
    prompt: str,
    cost_map: dict[str, tuple[str, float]],
    sem: asyncio.Semaphore,
) -> dict[str, Any]:
    """Fetch + analyse the front/back views for one id, merged into one array.

    Never raises — a failure becomes {id, prints: [], error}. A single failing
    view is logged and skipped; the id only errors if EVERY provided view fails.
    """
    async with sem:
        try:
            views = [
                (v, (getattr(item, v) or "").strip())
                for v in _VIEWS
                if (getattr(item, v) or "").strip()
            ]
            if not views:
                return {
                    "id": item.id,
                    "prints": [],
                    "error": "no 'front' or 'back' image url provided",
                }

            all_rows: list[dict[str, Any]] = []
            errors: list[str] = []
            for view, url in views:
                try:
                    buf, mime = await _fetch_image_url(url)
                    rows = await _analyze_image(
                        client, buf, mime, prompt, view, cost_map
                    )
                    all_rows.extend(rows)
                except asyncio.CancelledError:
                    raise
                except asyncio.TimeoutError:
                    errors.append(f"{view}: analysis timed out")
                except Exception as err:
                    print(
                        f"[analyze-prints] id={item.id!r} view={view} failed: "
                        f"name={type(err).__name__} message={err}",
                        flush=True,
                    )
                    errors.append(f"{view}: {err}")

            # Every provided view failed → surface the error for this id.
            if not all_rows and errors:
                return {"id": item.id, "prints": [], "error": "; ".join(errors)}

            # Renumber print_no contiguously across the merged front+back rows.
            for i, row in enumerate(all_rows, 1):
                row["print_no"] = i
            return {"id": item.id, "prints": all_rows}

        except asyncio.CancelledError:
            raise
        except Exception as err:
            print(
                f"[analyze-prints] id={item.id!r} failed: "
                f"name={type(err).__name__} message={err}",
                flush=True,
            )
            return {"id": item.id, "prints": [], "error": str(err) or "analysis failed"}


@router.post("/")
async def analyze_prints(req: AnalyzePrintsRequest):
    started_at = time.perf_counter()

    if not req.images:
        return JSONResponse(
            status_code=400,
            content={"error": "'images' is required (non-empty array of {id, front, back})"},
        )
    for it in req.images:
        if not (it.id or "").strip():
            return JSONResponse(
                status_code=400,
                content={"error": "every image needs a non-empty 'id'"},
            )

    cost_map = _build_cost_map(req.print_types)
    prompt = _build_prompt(req.print_types)

    t = step("analyze-prints", "request", {
        "model": _ANALYSIS_MODEL,
        "images": len(req.images),
        "printTypes": len(req.print_types),
    })

    async def event_generator() -> AsyncGenerator[dict[str, Any], None]:
        yield {
            "event": "meta",
            "data": json.dumps({"model": _ANALYSIS_MODEL, "count": len(req.images)}),
        }

        sem = asyncio.Semaphore(_CONCURRENCY)
        tasks: list[asyncio.Task[dict[str, Any]]] = []
        try:
            client = _get_client()
            tasks = [
                asyncio.create_task(_process_one(client, it, prompt, cost_map, sem))
                for it in req.images
            ]

            done_count = 0
            for fut in asyncio.as_completed(tasks):
                result = await fut
                done_count += 1
                yield {"event": "result", "data": json.dumps(result)}

            elapsed_ms = int((time.perf_counter() - started_at) * 1000)
            yield {
                "event": "done",
                "data": json.dumps({"count": done_count, "ms": elapsed_ms}),
            }
            t.done({"ms": elapsed_ms, "count": done_count})

        except asyncio.CancelledError:
            for task in tasks:
                task.cancel()
            t.fail("client disconnected")
            raise
        except Exception as err:
            for task in tasks:
                task.cancel()
            t.fail(err)
            msg = str(err)
            lower = msg.lower()
            aborted = "aborted" in lower or "disconnected" in lower
            print(
                f"[analyze-prints] failed: name={type(err).__name__} message={msg}",
                flush=True,
            )
            yield {
                "event": "error",
                "data": json.dumps({
                    "error": "Analysis cancelled" if aborted else (msg or "analysis failed"),
                    "code": getattr(err, "code", None),
                    "type": getattr(err, "type", None),
                    "retriable": not aborted,
                }),
            }

    return EventSourceResponse(event_generator(), ping=15)
