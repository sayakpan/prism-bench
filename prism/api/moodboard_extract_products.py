"""
POST /api/ai/moodboard-extract-products

Extract individual garments out of a moodboard image.

Pipeline:
  1. images.edit with a strict flat-lay prompt â†’ one composite image
     with every garment laid out in a row on a clean white background.
  2. PIL greyscale + threshold against white â†’ binary mask.
  3. cv2.connectedComponentsWithStats â†’ per-garment bounding boxes
     (replaces the hand-rolled CCL in the Node service; same output,
     ~10x faster).
  4. PIL.crop per bbox â†’ one PNG per garment, with an edge-cleanup
     pass that paints neighbour-bleed back to white.
  4b. Visual dedup: a per-crop signature (dominant colour + silhouette
     aspect + difference-hash) drops near-duplicate crops BEFORE they are
     emitted, so a garment the generator rendered twice never reaches the
     grid. Same-shape / different-colour garments stay distinct.
  5. Single brand-voice gpt-4o-mini call on the composite for context.
  6. Per-garment gpt-4o-mini structured classification, capped at
     CLASSIFIER_CONCURRENCY=5 with asyncio.Semaphore.

Streams over SSE so the slow images.edit call doesn't trip a proxy idle
timeout. Each crop fires `product`; each classifier result fires `attrs`.

Body (multipart/form-data):
  image:          <file>  moodboard PNG/JPEG/WEBP, â‰¤ 20 MB. Required.
  garment_styles: <text>  OPTIONAL JSON array of master garment-style names.
                          When supplied, product_category is HARD-LOCKED to the
                          list (schema enum, closest match, no invention).
                          Absent / malformed -> free-text (no break).
  selected_fabrics:<text> OPTIONAL JSON array of fabric objects
                          ({id?, code?, name, ...}). When supplied, each
                          garment is HARD-LOCKED to one fabric from the list
                          (schema enum, no guessing); the full chosen fabric
                          object is echoed back on `attrs.matched_fabric` and
                          its name on `attrs.fabric_quality`. Absent /
                          malformed â†’ free-text fabric_quality (no break).
  gender:         <text>  OPTIONAL JSON array (or bare string) of target
                          genders, taken VERBATIM as the allowed vocabulary
                          (e.g. ["Women"], ["Kids - Girls", "Kids - Boys"]).
                          When supplied, each garment's `gender` is HARD-LOCKED
                          to the passed list (schema enum, no guessing outside
                          it) and `garment_name` is phrased for that audience
                          (e.g. "Women's ...", "Girls' ..."). Empty / malformed
                          â†’ free pick from the base-schema fallback (no break).
  size:           <text>  OPTIONAL gpt-image size (e.g. "2048x2048"). Falls
                          back to the default when omitted / unsupported.
  quality:        <text>  OPTIONAL gpt-image quality ("low"|"medium"|"high"|
                          "auto"). Falls back to the default when omitted /
                          unsupported. Use "high" for crisp, non-blurry crops.
  season:         <text>  OPTIONAL season tag (e.g. "SS26"). Supporting
                          context for the editorial description only.
  theme:          <text>  OPTIONAL moodboard theme. Supporting context
                          for the editorial description only.

SSE events: meta â†’ ping (15s) â†’ product (each crop) â†’ attrs (each
classification) â†’ done OR error.
"""
from __future__ import annotations

import asyncio
import base64
import json
import re
import time
from collections.abc import AsyncGenerator
from io import BytesIO
from typing import Any

import cv2
import numpy as np
from fastapi import APIRouter, File, Form, UploadFile
from fastapi.responses import JSONResponse
from openai import AsyncOpenAI
from PIL import Image
from sse_starlette.sse import EventSourceResponse

from moodboard_ai.config import get_settings
from moodboard_ai.log import step
from moodboard_ai.services.openai_responses import ALLOWED_QUALITIES, ALLOWED_SIZES

router = APIRouter()

# Hardcoded knobs
_MODEL = "gpt-image-2"
# size / quality are now caller-overridable (see the `size` + `quality` form
# fields). These are the fallbacks when the payload omits them or sends an
# unsupported value. `low` quality is what made earlier crops look blurry — the
# FE can now pass "high" to get crisp extractions without changing the default.
_DEFAULT_SIZE = "2560x1440"
_DEFAULT_QUALITY = "high"
_N = 1

_CLASSIFIER_MODEL = "gpt-4o-mini"
_CLASSIFIER_CONCURRENCY = 5

_BRAND_VOICE_PROMPT = """Look at this moodboard. Write ONE LINE (â‰¤ 25 words) capturing the brand voice + season + dominant aesthetic. This line will be fed into a per-garment classifier so it can name garments in the right tone.

Examples:
- "Cozy minimal lounge: muted neutrals, women's loungewear, autumn calm."
- "Streetwear utility: oversized silhouettes, earth tones, fall workwear."
- "Resort glamour: jewel tones, women's eveningwear, spring/summer."

Return just the line. No quotes, no prefix, no JSON."""

_CLASSIFIER_PROMPT = """You are a fashion product analyst. Look at the garment crop in the image below and classify it.

The TEXT BELOW the schema describes the source moodboard's brand voice / season vibe ” use it to name the garment in the right tone.

CRITICAL MANUFACTURING CONSTRAINT (NON-NEGOTIABLE):
This factory manufactures KNITTED garments ONLY. Every garment you classify
is, by definition, a KNIT. You MUST NEVER describe, name, or imply that ANY
garment is WOVEN. The word "woven" (and woven-only constructions such as
poplin, twill, chambray, denim, canvas, oxford, broadcloth, taffeta,
gabardine, etc.) is STRICTLY FORBIDDEN in EVERY field ” garment_name,
product_category, description, and fabric_quality. If a garment visually
looks woven, treat and describe it as the equivalent KNITTED construction
(e.g. single jersey, interlock, rib knit, French terry, pique, ponte,
fleece-back knit, jacquard knit, waffle knit). When in doubt, default to a
neutral knit term. Producing the word "woven" anywhere is a hard failure.

Guidance per field:

- gender: the target audience. When the request supplies an allowed gender list, pick EXACTLY one value from it and never anything outside it.
- product_category: free-text product type (e.g. "Tops", "Bottoms", "Pants", "Trousers", "Dress", "Outerwear", "Skirt", "Co-ord"). Be specific where the silhouette makes it obvious.
- garment_name: a single descriptive product line in the brand voice suggested below (e.g. "Women's lace top in emerald jewel tone"). 6â€“12 words. No SKU codes.
- element_colour_hex: the single DOMINANT garment colour as a hex code in "#RRGGBB" form (e.g. "#1F6B4C"). Estimate it from the crop. If the garment is multi-coloured, pick the most prominent single colour. Always return a valid 6-digit hex with a leading '#'.
- description: a 2-3 sentence editorial product story in brand voice. Focus on the STYLE - its silhouette, mood, and how it's worn - referencing at most one or two signature details or materials. Aspirational, lifestyle product-page copy. Do NOT mention price, cost, internal codes, MOQ, GSM, fabric counts/blends, or any technical construction specs. Use sentence case (no ALL CAPS); keep it warm and concise.
- fabric_quality: a DESCRIPTIVE label of the KNITTED construction (e.g. "Single jersey with elastane", "Interlock knit", "French terry", "Rib knit", "Pique knit"). NEVER a woven construction (no poplin, twill, denim, etc.). NOT a SKU code.
"""

_CLASSIFIER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "gender",
        "product_category",
        "garment_name",
        "element_colour_hex",
        "fabric_quality",
        "description",
    ],
    "properties": {
        "gender": {
            "type": "string",
            "enum": ["Women", "Men", "Kids", "Kids - Girls", "Kids - Boys"],
        },
        "product_category": {"type": "string"},
        "garment_name": {"type": "string"},
        "element_colour_hex": {"type": "string"},
        "fabric_quality": {"type": "string"},
        "description": {"type": "string"},
    },
}

_PROMPT = """Produce a clean ecommerce flat-lay of ONLY the fabric garments visible in the input image.

Strip out humans, mannequins, body parts, skin, hair, shadows, props, hangers, and the background.

Layout (strict):
- landscape canvas
- arrange garments in a grid: AT MOST 5 garments per row, stacked
  into as many rows as needed to fit them all
- if 5 or fewer garments â†’ one row
- if 6â€“10 garments â†’ two rows of up to 5 each
- if 11â€“15 garments â†’ three rows of up to 5 each
- leave a clear empty gap between every garment (both horizontally
  between neighbours in the same row AND vertically between rows)
- garments MUST NOT touch, overlap, or share edges with each other
- align garments to a consistent baseline within each row
- center the whole grid on the canvas
- each garment fully surrounded by white space on all four sides

Garment fidelity:
- preserve original texture, shape, color, prints, and proportions
- front-facing, flat catalog style
- crisp edges, no halos, no shadow remnants
- white background everywhere outside the garments

NO DUPLICATES (critical):
- render each DISTINCT garment EXACTLY ONCE
- if the same garment appears multiple times in the source (e.g. worn
  by a model AND shown as a separate flat-lay, or repeated across two
  photos, or shown front and back), consolidate it into ONE flat-lay
- never duplicate, mirror, clone, or repeat a garment to fill the grid
- never invent or add garments that are not present in the source
- two garments that differ ONLY in colour ARE distinct â€” keep both;
  two views of the SAME garment in the SAME colour are one garment

INCLUDE ONLY: complete wearable FABRIC garments rendered as finished
photographic product images â€” tops, t-shirts, shirts, sweaters,
knitwear, dresses, skirts, trousers, shorts, jackets, coats, and other
body-worn clothing made of fabric.

EXCLUDE (never output these): footwear of any kind (shoes, sneakers,
trainers, boots, sandals, heels), bags (totes, backpacks, handbags,
clutches, pouches), belts, hats / caps / beanies, jewelry (chains,
necklaces, bracelets, rings, earrings), watches, sunglasses / eyewear,
gloves, socks, scarves, and any other accessory that is not a fabric
body-garment. Also exclude color palettes, paint swatches, hex chips,
fabric pattern swatches, surface prints shown as a square or rectangle,
textile texture samples, technical line drawings / sketches / tech
packs, mood imagery, abstract design elements, and any reference that
is not a finished fabric garment.

If the source image contains 9 elements but only 5 are finished fabric
garments (the rest being accessories, swatches, or duplicates), output
5 â€” not 9.
"""

# CV knobs (match the Python cropper)
_WHITE_THRESHOLD = 240      # grey value above this = background
_MIN_AREA = 8000            # pixelsÂ² â€” drop speckle / labels
_PADDING = 30               # pixels around each crop
_ROW_TOLERANCE = 80         # y-band size for row-major sort

# Upload + timeouts
_ALLOWED_MIMES = frozenset({"image/png", "image/jpeg", "image/webp"})
_MAX_BYTES = 20 * 1024 * 1024
_EDIT_TIMEOUT_S = 600.0     # 10 min â€” slow gpt-image-2 edits can take 3â€“6 min

# Edge cleanup constants â€” paint neighbour-garment slivers at the bbox
# edges back to white. Cheap, robust to which side the bleed is on.
_CLEANUP_MIN_BLOB = 100
_CLEANUP_MARGIN = 4

# Duplicate detection. The generative flat-lay step (gpt-image-2) can render the
# same garment more than once (e.g. a garment shown worn AND as a flat-lay in the
# source), which CCL then crops into duplicate cards. We compute a cheap visual
# signature per crop and drop near-duplicates BEFORE emitting them, so dupes never
# reach the grid. A pair is treated as the same garment only when structure AND
# colour AND aspect all match — biased toward precision, because a false merge
# would DROP a genuinely distinct garment (worse than leaving a rare dupe). Two
# same-shape garments in different colours (e.g. navy vs charcoal jacket) stay
# distinct via the colour gate. Thresholds are heuristic; tune if needed.
_DEDUP_HASH_SIZE = 16          # dHash grid → _DEDUP_HASH_SIZE² bits
_DEDUP_HAMMING_MAX = 45        # max differing bits (~17% of 256) to call structure equal
_DEDUP_COLOUR_MAX = 32.0       # max mean-RGB euclidean distance to call colour equal
_DEDUP_ASPECT_MAX = 0.35       # max |aspectA - aspectB| to call silhouette equal
_DEDUP_MIN_FG = 50             # min foreground px before a signature is trustworthy


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


_HEX_RE = re.compile(r"#?([0-9A-Fa-f]{6})")
_FALLBACK_HEX = "#CCCCCC"


def _normalize_hex(value: Any) -> str:
    """Coerce the model's colour output to a canonical "#RRGGBB" string.

    Structured output isn't guaranteed to be a clean hex, so we pull the first
    6-hex-digit token and upper-case it; anything unparseable falls back to a
    neutral grey so the FE always has a valid swatch to seed the TCX match.
    """
    if not isinstance(value, str):
        return _FALLBACK_HEX
    m = _HEX_RE.search(value.strip())
    return f"#{m.group(1).upper()}" if m else _FALLBACK_HEX


# Deterministic backstop for the knit-only constraint. The prompt instructs
# the model never to say "woven", but Pratibha manufactures KNITTED garments
# only and reacts badly to the word, so we also scrub it in code as a hard
# guarantee. Case-preserving so "Woven" / "WOVEN" / "woven" all map sensibly.
_WOVEN_RE = re.compile(r"\bwoven\b", re.IGNORECASE)


def _scrub_woven(value: Any) -> Any:
    """Replace any standalone "woven" with "knitted", preserving casing."""
    if not isinstance(value, str):
        return value

    def _repl(m: re.Match[str]) -> str:
        word = m.group(0)
        if word.isupper():
            return "KNITTED"
        if word[0].isupper():
            return "Knitted"
        return "knitted"

    return _WOVEN_RE.sub(_repl, value)


def _augment_context(context_line: str, season: str | None, theme: str | None) -> str:
    """Append optional season + moodboard-theme hints to the classifier context.

    Both are supporting signals for the editorial `description` only - the style
    itself stays the subject. Blank / absent values are skipped (backward compat).
    """
    bits = [context_line]
    if season and season.strip():
        bits.append(
            f'Season: "{season.strip()}" (supporting hint for the description, not the subject).'
        )
    if theme and theme.strip():
        bits.append(
            f'Moodboard theme: "{theme.strip()}" (supporting hint for the description, not the subject).'
        )
    return "\n".join(bits)


def _filename_for(mime: str) -> str:
    if mime == "image/jpeg":
        return "image.jpg"
    if mime == "image/webp":
        return "image.webp"
    return "image.png"


def _parse_genders(raw: str | None) -> list[str]:
    """
    Parse the optional `gender` multipart text field into a clean list.

    Accepts a JSON array (e.g. `["Women"]`, `["Kids - Girls", "Kids - Boys"]`)
    or a bare string (e.g. `"Women"`). The values are taken VERBATIM from the
    caller and become the `gender` schema enum as-is â€” we do NOT restrict them
    to a hardcoded canonical set, because the FE owns the gender vocabulary
    (Women / Men / Kids / Kids - Girls / Kids - Boys / ...) and the AI must pick
    ONLY from what was passed, never outside it.

    Each label has whitespace collapsed (so an embedded newline can never break
    the strict-mode enum) and duplicates/blanks are dropped, order preserved.
    Tolerant of absence / bad JSON / wrong shape â€” always returns a list, never
    raises. Empty result â†’ caller leaves `gender` free (model picks from the
    base-schema fallback).
    """
    if not isinstance(raw, str) or not raw.strip():
        return []
    try:
        parsed = json.loads(raw)
    except Exception:
        # Tolerate a bare, unquoted value like `Women`.
        parsed = raw
    if isinstance(parsed, str):
        parsed = [parsed]
    if not isinstance(parsed, list):
        return []
    out: list[str] = []
    for g in parsed:
        if not isinstance(g, str):
            continue
        # Collapse whitespace/newlines â€” OpenAI strict outputs reject a "\n"
        # inside an enum string literal.
        label = " ".join(g.split())
        if label and label not in out:
            out.append(label)
    return out


def _parse_garment_styles(raw: str | None) -> list[str]:
    """
    Parse the optional `garment_styles` multipart text field (JSON array)
    into a clean string list. Tolerant of absence / bad JSON / wrong
    shape â€” always returns a list, never raises.
    """
    if not isinstance(raw, str) or not raw.strip():
        return []
    try:
        parsed = json.loads(raw)
    except Exception:
        return []
    if not isinstance(parsed, list):
        return []
    return [s for s in parsed if isinstance(s, str) and s.strip()]


def _parse_selected_fabrics(raw: str | None) -> list[dict[str, Any]]:
    """
    Parse the optional `selected_fabrics` multipart text field (JSON array of
    fabric objects) into a clean list of dicts. Each kept entry must be a dict
    carrying at least a usable label (`name` or `code`). Tolerant of absence /
    bad JSON / wrong shape â€” always returns a list, never raises.
    """
    if not isinstance(raw, str) or not raw.strip():
        return []
    try:
        parsed = json.loads(raw)
    except Exception:
        return []
    if not isinstance(parsed, list):
        return []
    out: list[dict[str, Any]] = []
    for f in parsed:
        if not isinstance(f, dict):
            continue
        label = f.get("name") or f.get("code")
        if isinstance(label, str) and label.strip():
            out.append(f)
    return out


def _fabric_label(fabric: dict[str, Any]) -> str:
    """Human label for a fabric entry: `name`, falling back to `code`.

    Whitespace is collapsed to single spaces (``" ".join(...split())``) so any
    embedded newlines / tabs / runs are flattened. This matters because the
    label becomes a schema *enum* value, and OpenAI strict structured outputs
    reject a ``\\n`` inside an enum string literal ("is not allowed in string
    literals for structured outputs").
    """
    label = fabric.get("name") or fabric.get("code") or ""
    return " ".join(str(label).split())


def _resolve_fabric_match(
    chosen: Any, fabrics: list[dict[str, Any]]
) -> dict[str, Any] | None:
    """
    Map the model's `fabric_match` string back to the supplied fabric object so
    the FE gets the canonical {id, code, name}. Exact (case-insensitive) match
    first, then a substring match either way. Returns None when nothing lines
    up â€” the model declined to commit, so we leave the garment unassigned.
    """
    if not isinstance(chosen, str) or not chosen.strip() or not fabrics:
        return None
    needle = chosen.strip().casefold()
    for f in fabrics:
        if _fabric_label(f).casefold() == needle:
            return f
    for f in fabrics:
        label = _fabric_label(f).casefold()
        if label and (label in needle or needle in label):
            return f
    return None


def _build_classifier(
    garment_styles: list[str],
    selected_fabrics: list[dict[str, Any]] | None = None,
    genders: list[str] | None = None,
) -> dict[str, Any]:
    """
    Build the per-garment classifier prompt + JSON schema, biased by any
    caller-supplied lists.

    - `garment_styles`: HARD-LOCK `product_category` to the list via a schema
      enum (the model MUST pick the closest one, no invention, no "Other").
      Absent -> free-text `product_category`.
    - `selected_fabrics`: ask the model to DECIDE which fabric from the list
      each garment is made of and return its EXACT name in `fabric_quality`
      (verbatim, no extra words). This is HARD-LOCKED to the list via a schema
      enum: the model MUST pick one of the supplied fabric names, no guessing.
      The route resolves the chosen name back to the full fabric object and
      always echoes it on `matched_fabric` (never null when a list is
      supplied). Without a list, `fabric_quality` stays free-text.
    - `genders`: HARD-LOCK `gender` to the supplied list via a schema enum (the
      model can only return one of them) AND require `garment_name` to be phrased
      for that audience (e.g. "Women's ..."). Absent â†’ free pick from all of
      _ALLOWED_GENDERS and the name is unconstrained on gender.
    """
    selected_fabrics = selected_fabrics or []
    genders = genders or []
    prompt = _CLASSIFIER_PROMPT
    schema = _CLASSIFIER_SCHEMA
    schema_copied = False

    def _ensure_schema_copy() -> dict[str, Any]:
        # Deep-copy the module-level constant lazily so it is never mutated and
        # unconstrained calls keep the shared base schema.
        nonlocal schema, schema_copied
        if not schema_copied:
            schema = json.loads(json.dumps(_CLASSIFIER_SCHEMA))
            schema_copied = True
        return schema

    if genders:
        gender_listing = ", ".join(f'"{g}"' for g in genders)
        if len(genders) == 1:
            g = genders[0]
            gender_rule = (
                f'\n\nGENDER (NON-NEGOTIABLE) - every garment on this moodboard '
                f'targets {gender_listing}. You MUST set `gender` to "{g}" '
                f'(exactly as written, the only allowed value), and `garment_name` '
                f'MUST clearly read as a {g} product so the target wearer is '
                f'unmistakable (e.g. Women -> "Women\'s ribbed knit top in emerald '
                f'jewel tone"; Kids - Girls -> "Girls\' knit A-line dress in blush").'
            )
        else:
            gender_rule = (
                f"\n\nGENDER (NON-NEGOTIABLE) - set `gender` to whichever of these "
                f"the garment targets, EXACTLY as written and nothing outside the "
                f"list: {gender_listing}. `garment_name` MUST clearly read as a "
                f"product for the chosen audience (make the target wearer "
                f'unmistakable in the name, e.g. "Women\'s ...", "Men\'s ...", '
                f'"Girls\' ...", "Boys\' ...").'
            )
        prompt = prompt + gender_rule
        # Hard-lock the enum to the supplied genders.
        schema = _ensure_schema_copy()
        schema["properties"]["gender"] = {"type": "string", "enum": genders}

    # De-dupe + whitespace-collapse the style labels while preserving order so
    # the enum is clean (no embedded "\n", which strict structured outputs
    # reject) even if the caller sends duplicates or multi-line names.
    category_labels: list[str] = []
    for s in garment_styles:
        label = " ".join(s.split())
        if label and label not in category_labels:
            category_labels.append(label)

    if category_labels:
        listing = "\n".join(f"- {lbl}" for lbl in category_labels)
        prompt = (
            prompt
            + "\n\nPRODUCT CATEGORY (NON-NEGOTIABLE) - `product_category` MUST be "
            + "ONE of the categories below, EXACTLY as written, and nothing "
            + "outside this list:\n"
            + listing
            + "\nStudy the garment's silhouette and pick the single closest "
            + 'match. Do NOT invent a new category and do NOT return "Other".'
        )
        # Hard-lock to the list via a schema enum so the model can only ever
        # return one of the supplied categories (no guessing / no invention).
        schema = _ensure_schema_copy()
        schema["properties"]["product_category"] = {
            "type": "string",
            "enum": category_labels,
        }

    # De-dupe labels while preserving order so the enum is clean even if the
    # caller sends two fabrics with the same name.
    fabric_labels: list[str] = []
    for f in selected_fabrics:
        label = _fabric_label(f)
        if label and label not in fabric_labels:
            fabric_labels.append(label)

    if fabric_labels:
        fabric_listing = "\n".join(f"- {lbl}" for lbl in fabric_labels)
        prompt = (
            prompt
            + "\n\nFABRIC SELECTION â€” this garment is made from ONE of the "
            + "fabrics below. Study the crop's visible knit structure, surface "
            + "texture, sheen and drape, then decide which it is:\n"
            + fabric_listing
            + "\nYou MUST set `fabric_quality` to one of these fabric names "
            + "EXACTLY as written above â€” character-for-character and nothing "
            + "else, no extra words (return \"Interlock\", never \"Interlock "
            + "knit\"). Do NOT invent a fabric or return any name not on this "
            + "list. Pick the single closest match."
        )
        # Hard-lock to the list via a schema enum so the model can only ever
        # return one of the supplied fabric names (no guessing). Uses the lazy
        # deep-copy so the module-level constant is never mutated and fabric-less
        # calls keep a free-text fabric_quality.
        schema = _ensure_schema_copy()
        schema["properties"]["fabric_quality"] = {
            "type": "string",
            "enum": fabric_labels,
        }

    return {"prompt": prompt, "schema": schema, "fabrics": selected_fabrics}


def _find_components(
    composite_bytes: bytes,
) -> tuple[list[dict[str, int]], int, int]:
    """
    Decode â†’ greyscale â†’ threshold â†’ cv2.connectedComponentsWithStats.

    Returns (components, width, height), with components sorted in
    row-major order (top-to-bottom in y-bands of _ROW_TOLERANCE px,
    left-to-right within each band).
    """
    img = Image.open(BytesIO(composite_bytes)).convert("L")
    width, height = img.size
    arr = np.array(img, dtype=np.uint8)
    mask = (arr < _WHITE_THRESHOLD).astype(np.uint8)

    num_labels, _labels, stats, _centroids = cv2.connectedComponentsWithStats(
        mask, connectivity=4
    )
    components: list[dict[str, int]] = []
    for i in range(1, num_labels):  # 0 is background
        area = int(stats[i, cv2.CC_STAT_AREA])
        if area < _MIN_AREA:
            continue
        x = int(stats[i, cv2.CC_STAT_LEFT])
        y = int(stats[i, cv2.CC_STAT_TOP])
        w = int(stats[i, cv2.CC_STAT_WIDTH])
        h = int(stats[i, cv2.CC_STAT_HEIGHT])
        components.append({
            "minX": x,
            "minY": y,
            "maxX": x + w - 1,
            "maxY": y + h - 1,
            "area": area,
        })

    components.sort(key=lambda c: (c["minY"] // _ROW_TOLERANCE, c["minX"]))
    return components, width, height


def _clean_crop_edges(crop_png: bytes) -> bytes:
    """
    Edge cleanup. Decode crop â†’ find dominant connected blob â†’ paint
    everything outside its bbox (+ small margin) back to white. Robust
    to which side neighbour-bleed is on.
    """
    img = Image.open(BytesIO(crop_png)).convert("RGBA")
    arr = np.array(img, dtype=np.uint8)  # (H, W, 4)
    height, width = arr.shape[:2]

    grey = arr[:, :, :3].mean(axis=2)
    mask = (grey < _WHITE_THRESHOLD).astype(np.uint8)

    num_labels, _labels, stats, _centroids = cv2.connectedComponentsWithStats(
        mask, connectivity=4
    )
    if num_labels <= 2:
        return crop_png  # only background + a single blob

    # Find the largest blob (skip index 0 = background). Filter by
    # CLEANUP_MIN_BLOB so noise doesn't compete.
    areas = stats[1:, cv2.CC_STAT_AREA]
    candidates = [
        (i + 1, int(areas[i]))
        for i in range(len(areas))
        if int(areas[i]) >= _CLEANUP_MIN_BLOB
    ]
    if len(candidates) <= 1:
        return crop_png

    dominant_idx = max(candidates, key=lambda t: t[1])[0]
    dx = int(stats[dominant_idx, cv2.CC_STAT_LEFT])
    dy = int(stats[dominant_idx, cv2.CC_STAT_TOP])
    dw = int(stats[dominant_idx, cv2.CC_STAT_WIDTH])
    dh = int(stats[dominant_idx, cv2.CC_STAT_HEIGHT])
    min_x = max(0, dx - _CLEANUP_MARGIN)
    min_y = max(0, dy - _CLEANUP_MARGIN)
    max_x = min(width - 1, dx + dw - 1 + _CLEANUP_MARGIN)
    max_y = min(height - 1, dy + dh - 1 + _CLEANUP_MARGIN)

    # Paint outside the keep-region to white (RGB channels only).
    if min_y > 0:
        arr[:min_y, :, :3] = 255
    if max_y + 1 < height:
        arr[max_y + 1 :, :, :3] = 255
    if min_x > 0:
        arr[min_y : max_y + 1, :min_x, :3] = 255
    if max_x + 1 < width:
        arr[min_y : max_y + 1, max_x + 1 :, :3] = 255

    out = BytesIO()
    Image.fromarray(arr, mode="RGBA").save(out, format="PNG")
    return out.getvalue()


def _crop_component(
    composite_bytes: bytes,
    c: dict[str, int],
    width: int,
    height: int,
) -> tuple[bytes, dict[str, int]]:
    """Crop one component as a standalone PNG (with edge cleanup applied)."""
    left = max(0, c["minX"] - _PADDING)
    top = max(0, c["minY"] - _PADDING)
    right = min(width, c["maxX"] + 1 + _PADDING)
    bottom = min(height, c["maxY"] + 1 + _PADDING)
    w = right - left
    h = bottom - top

    img = Image.open(BytesIO(composite_bytes))
    crop = img.crop((left, top, right, bottom))
    raw = BytesIO()
    crop.save(raw, format="PNG")
    raw_bytes = raw.getvalue()
    cleaned = _clean_crop_edges(raw_bytes)
    return cleaned, {"x": left, "y": top, "w": w, "h": h}


def _crop_signature(crop_png: bytes) -> dict[str, Any] | None:
    """
    Compute a cheap visual signature for duplicate detection: dominant colour,
    silhouette aspect ratio, and a difference-hash of the garment region.

    The hash + aspect are measured over the FOREGROUND bounding box (non-white,
    non-transparent pixels), not the padded crop, so surrounding whitespace /
    padding / position never skews the comparison. Returns None when there isn't
    enough foreground to trust a signature (caller then keeps the crop).
    """
    img = Image.open(BytesIO(crop_png)).convert("RGBA")
    arr = np.array(img, dtype=np.uint8)
    rgb = arr[:, :, :3].astype(np.float32)
    alpha = arr[:, :, 3]
    grey = rgb.mean(axis=2)
    fg = (grey < _WHITE_THRESHOLD) & (alpha > 0)
    if int(fg.sum()) < _DEDUP_MIN_FG:
        return None

    ys, xs = np.where(fg)
    y0, y1 = int(ys.min()), int(ys.max())
    x0, x1 = int(xs.min()), int(xs.max())
    h = y1 - y0 + 1
    w = x1 - x0 + 1

    mean_rgb = rgb[fg].mean(axis=0)             # dominant colour of the garment
    aspect = w / h if h else 1.0

    # dHash over the greyscale foreground region (resize normalises scale).
    region = Image.fromarray(arr[y0 : y1 + 1, x0 : x1 + 1, :3]).convert("L")
    small = region.resize((_DEDUP_HASH_SIZE + 1, _DEDUP_HASH_SIZE))
    px = np.asarray(small, dtype=np.int16)
    dhash = (px[:, 1:] > px[:, :-1]).flatten()  # bool array, _DEDUP_HASH_SIZE² bits
    return {"mean_rgb": mean_rgb, "aspect": aspect, "dhash": dhash}


def _is_duplicate_of(sig: dict[str, Any], kept: list[dict[str, Any]]) -> bool:
    """
    True when `sig` matches an already-kept crop on ALL of colour, aspect, and
    structure. Requiring all three keeps genuinely distinct garments (same shape
    but different colour, or same colour but different shape) as separate cards.
    """
    for k in kept:
        if float(np.linalg.norm(sig["mean_rgb"] - k["mean_rgb"])) > _DEDUP_COLOUR_MAX:
            continue
        if abs(sig["aspect"] - k["aspect"]) > _DEDUP_ASPECT_MAX:
            continue
        hamming = int(np.count_nonzero(sig["dhash"] != k["dhash"]))
        if hamming <= _DEDUP_HAMMING_MAX:
            return True
    return False


async def _describe_brand_voice(
    client: AsyncOpenAI, composite_bytes: bytes
) -> str:
    """
    One-shot brand-voice summariser. Sends the source moodboard ONCE so
    the per-garment classifier can name garments in the right tone
    without re-sending the source image dozens of times.

    Returns "" on any failure so callers can proceed without context.
    """
    src_data_url = (
        f"data:image/png;base64,{base64.b64encode(composite_bytes).decode('ascii')}"
    )
    try:
        res = await client.chat.completions.create(
            model=_CLASSIFIER_MODEL,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": _BRAND_VOICE_PROMPT},
                        {
                            "type": "image_url",
                            "image_url": {"url": src_data_url, "detail": "low"},
                        },
                    ],
                }
            ],
            max_tokens=80,
        )
    except Exception as err:
        print(
            f"[moodboard-extract-products] brand-voice call failed: {err}",
            flush=True,
        )
        return ""
    content = res.choices[0].message.content if res.choices else None
    # Scrub here too so a "woven" vibe never seeds the per-garment classifier.
    return _scrub_woven((content or "").strip())


async def _classify_garment(
    client: AsyncOpenAI,
    crop_png: bytes,
    brand_voice: str,
    classifier: dict[str, Any],
    season: str | None = None,
    theme: str | None = None,
) -> dict[str, Any]:
    crop_data_url = (
        f"data:image/png;base64,{base64.b64encode(crop_png).decode('ascii')}"
    )
    context_line = (
        f'Moodboard brand voice / vibe: "{brand_voice}"'
        if brand_voice
        else "(No brand voice context available â€” use a neutral, descriptive tone.)"
    )
    res = await client.chat.completions.create(
        model=_CLASSIFIER_MODEL,
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": classifier["prompt"]},
                    {
                        "type": "image_url",
                        "image_url": {"url": crop_data_url, "detail": "low"},
                    },
                    {"type": "text", "text": _augment_context(context_line, season, theme)},
                ],
            }
        ],
        response_format={
            "type": "json_schema",
            "json_schema": {
                "name": "garment_attrs",
                "schema": classifier["schema"],
                "strict": True,
            },
        },
    )
    content = res.choices[0].message.content if res.choices else None
    if not content:
        raise RuntimeError("classifier returned empty content")
    attrs = json.loads(content)
    if isinstance(attrs, dict):
        fabrics = classifier.get("fabrics") or []
        # Knit-only guarantee: scrub any stray "woven" the model emitted in
        # free-text fields before it ever reaches the FE / Pratibha.
        scrub_fields = ["garment_name", "product_category", "description"]
        if not fabrics:
            # No supplied list: fabric_quality is always free-text, so scrub it.
            scrub_fields.append("fabric_quality")
        for field in scrub_fields:
            if field in attrs:
                attrs[field] = _scrub_woven(attrs[field])
        if "element_colour_hex" in attrs:
            attrs["element_colour_hex"] = _normalize_hex(attrs.get("element_colour_hex"))
        # With a supplied list, fabric_quality is HARD-LOCKED to the list via
        # the schema enum, so it always resolves to one of the supplied
        # fabrics. Attach the full {id, code, name, ...} record on
        # matched_fabric and normalise fabric_quality to its canonical name.
        # The None branch is a defensive backstop only (should not happen with
        # the enum in place).
        if fabrics and "fabric_quality" in attrs:
            matched = _resolve_fabric_match(attrs.get("fabric_quality"), fabrics)
            attrs["matched_fabric"] = matched
            if matched:
                attrs["fabric_quality"] = _fabric_label(matched)
            else:
                attrs["fabric_quality"] = _scrub_woven(attrs.get("fabric_quality"))
    return attrs


@router.post("/")
async def moodboard_extract_products(
    image: UploadFile | None = File(default=None),
    garment_styles: str | None = Form(default=None),
    selected_fabrics: str | None = Form(default=None),
    gender: str | None = Form(default=None),
    size: str | None = Form(default=None),
    quality: str | None = Form(default=None),
    season: str | None = Form(default=None),
    theme: str | None = Form(default=None),
):
    started_at = time.perf_counter()

    if image is None:
        return JSONResponse(
            status_code=400,
            content={"error": "'image' multipart file is required"},
        )

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

    image_buffer = await image.read()
    if len(image_buffer) > _MAX_BYTES:
        return JSONResponse(
            status_code=400,
            content={"error": f"image too large ({len(image_buffer)}B > {_MAX_BYTES}B)"},
        )

    styles_list = _parse_garment_styles(garment_styles)
    fabrics_list = _parse_selected_fabrics(selected_fabrics)
    genders_list = _parse_genders(gender)
    classifier = _build_classifier(styles_list, fabrics_list, genders_list)

    # Caller-overridable generation knobs, with a fallback when omitted /
    # unsupported so older payloads keep working unchanged.
    resolved_size = size if size in ALLOWED_SIZES else _DEFAULT_SIZE
    resolved_quality = quality if quality in ALLOWED_QUALITIES else _DEFAULT_QUALITY

    t = step("moodboard-extract-products", "request", {
        "model": _MODEL,
        "size": resolved_size,
        "quality": resolved_quality,
        "imageBytes": len(image_buffer),
        "imageMime": image_mime,
        "garmentStyles": len(styles_list),
        "selectedFabrics": len(fabrics_list),
        "genders": genders_list or None,
        "season": (season or "").strip() or None,
        "theme": (theme or "").strip() or None,
    })

    async def event_generator() -> AsyncGenerator[dict[str, Any], None]:
        yield {
            "event": "meta",
            "data": json.dumps(
                {"model": _MODEL, "size": resolved_size, "quality": resolved_quality}
            ),
        }

        try:
            client = _get_client()

            # 1. images.edit â†’ composite flat-lay
            edit_result = await asyncio.wait_for(
                client.images.edit(
                    model=_MODEL,
                    image=(_filename_for(image_mime), image_buffer, image_mime),
                    prompt=_PROMPT,
                    size=resolved_size,
                    quality=resolved_quality,
                    n=_N,
                    timeout=_EDIT_TIMEOUT_S,
                ),
                timeout=_EDIT_TIMEOUT_S,
            )

            data = getattr(edit_result, "data", None)
            b64 = getattr(data[0], "b64_json", None) if data else None
            if not b64:
                raise RuntimeError("images.edit returned no image data")
            composite_bytes = base64.b64decode(b64)

            # 2 + 3. mask + CCL â†’ ordered components
            components, width, height = await asyncio.to_thread(
                _find_components, composite_bytes
            )

            # 4. (parallel) brand-voice describer kicks off now
            brand_voice_task = asyncio.create_task(
                _describe_brand_voice(client, composite_bytes)
            )

            # 5. crop each component, drop visual duplicates, emit the rest.
            # `idx` is a running counter over KEPT crops so the grid stays
            # contiguous (01..N) even when duplicates are skipped mid-stream.
            crops: list[dict[str, Any]] = []
            kept_sigs: list[dict[str, Any]] = []
            skipped_duplicates = 0
            for c in components:
                crop_png, bbox = await asyncio.to_thread(
                    _crop_component, composite_bytes, c, width, height
                )
                sig = await asyncio.to_thread(_crop_signature, crop_png)
                if sig is not None and _is_duplicate_of(sig, kept_sigs):
                    skipped_duplicates += 1
                    continue
                if sig is not None:
                    kept_sigs.append(sig)
                idx = len(crops) + 1
                yield {
                    "event": "product",
                    "data": json.dumps({
                        "index": idx,
                        "src": f"data:image/png;base64,{base64.b64encode(crop_png).decode('ascii')}",
                        "bbox": bbox,
                        "area": c["area"],
                        "attrs": None,
                    }),
                }
                crops.append({"index": idx, "buffer": crop_png})

            if skipped_duplicates:
                print(
                    f"[moodboard-extract-products] dropped {skipped_duplicates} "
                    f"duplicate crop(s); {len(crops)} unique of "
                    f"{len(components)} components",
                    flush=True,
                )

            # 6. brand voice ready â€” classify with concurrency cap
            brand_voice = await brand_voice_task

            attrs_queue: asyncio.Queue[tuple[int, dict[str, Any], str | None] | None] = (
                asyncio.Queue()
            )

            sem = asyncio.Semaphore(_CLASSIFIER_CONCURRENCY)

            async def classify_one(p: dict[str, Any]) -> None:
                async with sem:
                    try:
                        attrs = await _classify_garment(
                            client, p["buffer"], brand_voice, classifier,
                            season, theme,
                        )
                        await attrs_queue.put((p["index"], attrs, None))
                    except Exception as err:
                        print(
                            f"[moodboard-extract-products] classifier failed "
                            f"for index {p['index']}: {err}",
                            flush=True,
                        )
                        # Empty attrs signals to the FE: "we tried, came up
                        # empty, the form is yours". `loading = !attrs` flips
                        # to false on {} and inputs enable.
                        await attrs_queue.put((p["index"], {}, str(err) or "classifier failed"))

            async def run_classifiers() -> None:
                try:
                    await asyncio.gather(*(classify_one(p) for p in crops))
                finally:
                    await attrs_queue.put(None)

            classifier_task = asyncio.create_task(run_classifiers())

            while True:
                item = await attrs_queue.get()
                if item is None:
                    break
                idx, attrs, err_msg = item
                payload: dict[str, Any] = {"index": idx, "attrs": attrs}
                if err_msg is not None:
                    payload["error"] = err_msg
                yield {"event": "attrs", "data": json.dumps(payload)}

            await classifier_task  # surface any unhandled exception

            elapsed_ms = int((time.perf_counter() - started_at) * 1000)
            yield {
                "event": "done",
                "data": json.dumps({
                    "count": len(crops),
                    "skipped_duplicates": skipped_duplicates,
                    "ms": elapsed_ms,
                }),
            }
            t.done({
                "ms": elapsed_ms,
                "count": len(crops),
                "components": len(components),
                "skippedDuplicates": skipped_duplicates,
            })

        except asyncio.CancelledError:
            t.fail("client disconnected")
            raise
        except asyncio.TimeoutError:
            t.fail("Edit timed out")
            yield {
                "event": "error",
                "data": json.dumps({
                    "error": "Edit timed out",
                    "retriable": True,
                }),
            }
        except Exception as err:
            t.fail(err)
            msg = str(err)
            lower = msg.lower()
            aborted = "aborted" in lower or "disconnected" in lower
            print(
                f"[moodboard-extract-products] failed: name={type(err).__name__} "
                f"message={msg}",
                flush=True,
            )
            yield {
                "event": "error",
                "data": json.dumps({
                    "error": "Extraction cancelled" if aborted else (msg or "extraction failed"),
                    "code": getattr(err, "code", None),
                    "type": getattr(err, "type", None),
                    "retriable": not aborted,
                }),
            }

    return EventSourceResponse(event_generator(), ping=15)
