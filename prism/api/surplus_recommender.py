'''
Surplus fabric recommendation engine.

Given a design/sourcing brief ("input signal") that names the constructions,
compositions and GSM benchmarks a range needs, this returns the handful of
surplus stock fabrics that best serve it.

Stock is recommended per **Fab Code**, not per stock row: one fab code is one
manufacturable quality, and its rows are the individual dye lots/batches of it.
So every row sharing a fab code is folded into a single group whose `available`
is the summed Total Qty. and whose `price` is the summed Total Value, in rupees.

Matching is a two-stage hybrid:

  1. A deterministic scorer compares every fab-code group against every fabric
     in the brief on construction family, fibre composition, GSM and depth of
     stock. This alone produces a usable, explainable ranking and is the
     fallback whenever the LLM is unavailable.
  2. The top `LLM_SHORTLIST_SIZE` groups are handed to the LLM together with the
     brief prose, which re-ranks them, assigns each to the brief line it serves
     and writes the merchandiser-facing reason and caveats.

The stock vocabulary is mill shorthand ("SJY_EL", "95:5 BCI:EL") while a brief
speaks retail English ("Single Jersey", "95% Cotton/5% Spandex"). Both sides are
normalised into the same construction-family + fibre-percentage space (reusing
the code tables in `dyed_decoder`) before anything is compared, which is what
lets the two meet.
'''

import hashlib
import json
import math
import re

import frappe
from frappe.utils import cint, flt

from prism.auth.authenticator import auth_required
import prism.api.llm as llm
import prism.api.dyed_decoder as dyed_decoder
import prism.lib.cloud as cloud

SURPLUS_STOCK_ITEM_DOCTYPE = 'Surplus Stock'

# The stock columns a fab-code group is built from. Shared by every loader that
# feeds _build_group, so scoring one fab code reads exactly what scoring the
# whole catalogue reads -- a column missing on one path would silently change
# the group it produces, and with it the score.
_GROUP_FIELDS = [
    'name', 'fab_code', 'material', 'material_type_desc', 'material_desc',
    'batch', 'quality', 'blend', 'gsm', 'color', 'shade_catagory',
    'fabric_type', 'total_qty', 'total_value_rs_lakh', 'base_uom', 'width',
    'dia', 'gauge', 'ageing', 'customer_name', 'stock_segment',
    'storage_location', 'has_image_file', 'image_urls',
    'closest_fabric_master', 'closest_fabric_cost_per_kg',
]

DEFAULT_MAX_ITEMS = 6
MAX_ITEMS_CAP = 12

# catalogue() page window (same contract as the moodboard list endpoints).
_DEFAULT_LIMIT = 20
_MAX_LIMIT = 100

# catalogue() sort keys -> the group field they order on. Sorting happens on the
# assembled groups, not in SQL, because `available` / `price` only exist once a
# fab code's lots have been summed. Anything not listed here falls back to the
# default rather than erroring, same as inventory.get_inventory_list.
# Sorting on price means sorting on the RATE, not the lot total: `price` is a
# summed Total Value, so ordering by it just floats the deepest lots and reads
# as a duplicate of the `available` sort. What a merchandiser compares between
# two fabrics is rupees per KG, so `price` and `price_per_uom` both order on the
# rate; the lot total is still reachable as `total_value` for the rare caller
# that wants "where is the most money sitting".
CATALOGUE_SORT_FIELDS = {
    'available': 'available',          # summed Total Qty. across the fab code
    'price': 'price_per_uom',          # value / qty -- rupees per UOM
    'price_per_uom': 'price_per_uom',  # explicit alias for the same rate
    'total_value': 'price',            # summed Total Value across the fab code, in rupees
    'gsm': 'gsm',
    'fab_code': 'fab_code',
}
_DEFAULT_SORT_BY = 'available'
_DEFAULT_SORT_DIR = 'desc'

# catalogue() free-text search columns. Matching is per stock ROW but selection
# is per fab code: a row that hits pulls in its whole group, undiminished. That
# is what makes searching a single batch number sane -- you get the fab code
# that batch belongs to, with every lot's quantity still summed into it, rather
# than a group whose `available` is one batch deep.
CATALOGUE_SEARCH_FIELDS = ['fab_code', 'batch', 'quality', 'blend']

# --- catalogue query language (`q`) ---
#
# `q` is a second, structured search on catalogue(): the caller types one line
# ("cotton single jersey 180 gsm 60 inch") and it is parsed into facet filters
# plus whatever words are left over. It is deliberately a NEW parameter rather
# than a smarter `search`: `search` is a literal substring contract clients
# already hold, and re-reading it as a query would silently change what an
# existing call returns -- a batch number like "SJ-180" would start filtering on
# GSM. The two are ANDed when both are sent.

# The columns `q`'s leftover words are matched against. Wider than
# CATALOGUE_SEARCH_FIELDS, and that width is the point: `quality` is mill
# shorthand ("SJY_EL"), so LIKE '%single jersey%' against it matches nothing.
# `quality_full_name` ("Single Jersey Elastane") and `blend_full_name` ("100
# Vasudha Primo Cotton") carry the English a person actually types, and
# `color` / `shade_catagory` / `material_desc` are where a colour word lands --
# `material_desc` included because the `color` column is empty on plenty of rows
# and the colour is only recoverable from the description (see
# _decode_material_desc).
QUERY_SEARCH_FIELDS = CATALOGUE_SEARCH_FIELDS + [
    'quality_full_name', 'blend_full_name', 'material_desc',
    'color', 'shade_catagory', 'material_type_desc', 'fabric_type',
]

# Comparators `q` accepts -> the bound they set. `>` and `>=` both mean an
# INCLUSIVE lower bound: this filters a catalogue, and nobody typing "gsm > 180"
# means to exclude the 180 GSM fabrics.
QUERY_OPERATORS = {
    '>=': 'min', '=>': 'min', '>': 'min', 'at least': 'min', 'more than': 'min',
    'greater than': 'min', 'over': 'min', 'above': 'min', 'min': 'min',
    'minimum': 'min', 'from': 'min',
    '<=': 'max', '=<': 'max', '<': 'max', 'at most': 'max', 'less than': 'max',
    'under': 'max', 'below': 'max', 'upto': 'max', 'up to': 'max', 'max': 'max',
    'maximum': 'max', 'within': 'max',
    '=': 'eq', '==': 'eq', 'is': 'eq', 'exactly': 'eq',
}

# The numeric facets `q` understands, each pointing at the assembled GROUP field
# it filters -- never at the stock column of the same name. The two differ on
# purpose: a group's `gsm` is its dominant lot's, with the `material_desc`
# fallback applied, so filtering rows in SQL would keep fab codes whose group
# then reports a GSM outside the range that was asked for. `available` and
# `price_per_uom` do not exist as columns at all until the lots are summed. So
# every numeric filter runs after the groups are built, beside the sort, which
# already works the same way and for the same reason.
#
#   keywords   written BEFORE the number  -- "gsm 180", "price < 500"
#   units      written AFTER it           -- "180 gsm", "60 inch", "500 kg"
#   multi      the group field holds a list -- the filter passes if ANY value fits
#   reader     'text' when the group field is free text a number has to be read
#              out of (`dia` is stored as typed, e.g. "34\"")
#
# Every one of them is exact. A bare "150 gsm" means 150 and not a band around
# it: a filter that quietly widens what was typed leaves a caller unable to
# state what they meant, and unable to tell a near miss from a hit in the
# results. Approximate weight is asked for as a range -- "140-160 gsm" -- which
# is both shorter to type and honest about what it will return.
#
# Ordering matters: the metrics are matched in this order and each removes what
# it claimed, so 'make cost' has to be offered before 'cost' or `price` eats it.
QUERY_METRICS = {
    'gsm': {
        'field': 'gsm', 'cast': int, 'unit_label': 'GSM',
        'keywords': ('gsm', 'weight', 'grams'), 'units': ('gsm',),
    },
    'width': {
        'field': 'widths', 'cast': int, 'multi': True, 'unit_label': 'inch',
        'keywords': ('width', 'wide'), 'units': ('inch', 'inches', 'in'),
    },
    # Both are written either way round -- "dia 34" and "34 dia", "24 gauge" and
    # "gauge 24" -- so their keywords double as units.
    'dia': {
        'field': 'dia', 'cast': int, 'reader': 'text', 'unit_label': 'dia',
        'keywords': ('dia', 'diameter'), 'units': ('dia', 'diameter'),
    },
    'gauge': {
        'field': 'gauge', 'cast': int, 'reader': 'text', 'unit_label': 'gauge',
        'keywords': ('gauge', 'gg'), 'units': ('gauge', 'gg'),
    },
    'make_cost': {
        'field': 'price_from_fabric_masters', 'cast': float, 'unit_label': 'INR/KG',
        'keywords': ('make cost', 'making cost', 'mfg cost', 'manufacturing cost'),
        'units': (),
    },
    'price': {
        'field': 'price_per_uom', 'cast': float, 'unit_label': 'INR/KG',
        'keywords': ('price', 'rate', 'cost', 'rs', 'inr', 'rupees'),
        'units': ('rs', 'inr', 'rupees'),
    },
    'available': {
        'field': 'available', 'cast': float, 'unit_label': 'KG',
        'keywords': ('available', 'availability', 'quantity', 'qty', 'stock'),
        'units': ('kgs', 'kg', 'kilos'),
    },
    'ageing': {
        'field': 'max_ageing', 'cast': int, 'unit_label': 'days',
        'keywords': ('ageing', 'aging'), 'units': ('days',),
    },
}

# Band either side of an explicit fibre percentage: "95% cotton" is a request for
# a 95/5, not for that number to the decimal.
QUERY_FIBRE_TOLERANCE = 5

# Cap on the leftover words turned into text conditions. Each one is its own
# fab-code lookup, and past a handful the caller is describing rather than
# searching.
QUERY_MAX_TERMS = 6

# Facets a caller may reasonably type that surplus stock does not carry.
# Recognised only so the answer can SAY so: each is stripped from the query,
# along with the comparison hanging off it, and echoed back under `unsupported`.
# Left in, "fob < 3" would become a text search for the word "fob" -- zero
# results, no explanation -- and dropping it silently is worse still, because
# then "single jersey fob < 3" quietly returns every single jersey.
#
# FOB is a per-piece garment price in USD, and it lives on Development Style /
# Techpack Costing. Surplus Stock has no equivalent: its only rates are
# `price_per_uom` (what this stock is valued at, INR per KG) and
# `price_from_fabric_masters` (what the cloth costs to make, INR per KG).
# Mapping either onto FOB would put a per-KG rupee figure behind a per-piece
# dollar filter, so `q` refuses rather than guesses.
QUERY_UNSUPPORTED = {
    'fob': 'FOB is a per-piece garment price and is not held on surplus stock. '
           'The stock rates are price (INR per KG) and make cost (INR per KG).',
    'usd': 'Surplus stock is valued in INR. Filter on price (INR per KG) instead.',
    'moq': 'Surplus stock has no MOQ -- `available` is the whole quantity on hand.',
    'lead time': 'Surplus stock is already on hand, so it carries no lead time.',
}

# Words dropped from the leftovers rather than searched for. Metric keywords,
# units and comparators join this set at import (see _query_noise_words): once
# "60 inch" has been read as a width, the bare word "inch" is not something to
# go looking for in a fab code.
QUERY_STOP_WORDS = frozenset({
    'a', 'an', 'the', 'and', 'or', 'of', 'for', 'in', 'on', 'to', 'with',
    'any', 'all', 'some', 'something', 'per', 'between',
    'show', 'me', 'find', 'get', 'need', 'want', 'looking', 'please',
    'fabric', 'fabrics', 'material', 'materials', 'surplus', 'stock', 'stocks',
})

# How many deterministically-scored groups the LLM gets to re-rank. Big enough
# that the LLM can overturn the scorer, small enough to keep it focused -- and
# small enough to answer inside the gateway timeout, which is what sets the
# ceiling in practice. Must stay well above the reserved seats (one per palette
# colour plus EXACT_FAMILY_SEATS per brief line) or the round-robin never runs.
LLM_SHORTLIST_SIZE = 24
# Cap on the re-rank reply. The re-rank is the longest call in the request and
# its cost is the prose it writes, so the prompt asks for short reasons and this
# stops a runaway reply from spending the whole timeout budget.
RERANK_MAX_TOKENS = 4000
# Shortlist seats held per brief line for stock in the exact construction it
# names, however poorly that stock scores overall (see _shortlist).
EXACT_FAMILY_SEATS = 2
# Shortlist seats held per palette colour, so the LLM always sees at least one
# option for every colour the brief asked for -- the colour equivalent of
# EXACT_FAMILY_SEATS, and what gives "at least one of each colour" a chance.
PALETTE_COLOR_SEATS = 1

# Multiplier applied to a group that carries none of the palette's colours and
# is only eligible because it holds undyed stock. Undyed stock CAN become any
# colour, so it stays in the running, but it must never outrank a fab code that
# is already sitting in the colour the brief asked for.
UNDYED_ONLY_PENALTY = 0.7
# Shade categories that mean "not dyed yet", so the lot can be taken to any
# colour. A lot with no colour recorded at all is treated the same way.
UNDYED_SHADES = {'RFD'}

# How long a palette -> stock-colour mapping is reused. The stock colour
# vocabulary is ~150 strings and changes only when stock is imported, so the
# same brief re-running the recommender should not pay for the LLM twice.
COLOR_MAP_CACHE_TTL = 60 * 60 * 6

# The colour map runs on the default model, and deliberately so. It was worth
# measuring: reading colour names looks like cheap classification, but a smaller
# model put JET BLACK under Chocolate Brown and CREAM under Sand Beige, which
# shows a brand a black fabric as brown. Once the prompt returns one row per
# palette colour instead of one per stock name (see _llm_color_map) the default
# model answers in ~9s anyway, so there is nothing to buy by trading accuracy.
COLOR_MAP_MAX_TOKENS = 4000

# Stock values that cannot be a colour, filtered out before the LLM is asked.
# Cheap to recognise and pure noise in the prompt.
NON_COLOR_VALUES = {'RFD', 'NA', 'N/A', 'NIL', 'NONE', 'TBD', 'DYED', 'GREY FABRIC'}

#--- deterministic score weights (must sum to 1.0) ---
W_CONSTRUCTION = 0.35
W_COMPOSITION = 0.35
W_GSM = 0.20
W_AVAILABILITY = 0.10

# GSM delta (either direction) at which the GSM sub-score decays to 0.
GSM_TOLERANCE = 60
# Sub-score used when a group's GSM could not be established at all -- slightly
# below neutral so a known-and-close weight always outranks an unknown one.
GSM_UNKNOWN_SCORE = 45

# Quantity (in the stock base UOM, in practice KG) that earns a full
# availability sub-score. Deeper stock than this is not extra credit.
QTY_FULL_SCORE = 500.0

# A brief asking for this much elastane needs real recovery; a group with no
# elastane at all gets its construction+composition subtotal scaled down.
STRETCH_REQUIRED_PCT = 3.0
NO_STRETCH_PENALTY = 0.85
# Milder, opposite case: stock carries elastane the brief never asked for.
EXTRA_STRETCH_PENALTY = 0.97

# GSM values recovered from `material_desc` outside this band are mill codes or
# truncation artefacts (drawcord tapes read as "10"), not fabric weights.
PLAUSIBLE_GSM = (80, 600)

# Yarn-process words sitting between the blend and the GSM in `material_desc`.
YARN_ANNOTATIONS = frozenset({'CC', 'COMPACT', 'SIRO', 'CTN'})

# Mill fibre shorthand -> the fibre group compositions are compared in. Keys
# mirror `dyed_decoder.FIBRE_CODES`; the grouping is what makes "95:5 BCI:EL"
# comparable with "95% Cotton/5% Spandex".
FIBRE_GROUP_BY_CODE = {
    # cotton family (incl. organic / BCI / recycled / fairtrade variants)
    'C': 'cotton', 'O': 'cotton', 'OC': 'cotton', 'ORG': 'cotton', 'B': 'cotton',
    'BCI': 'cotton', 'ROC': 'cotton', 'FTO': 'cotton', 'SUP': 'cotton',
    'F': 'cotton', 'RC': 'cotton', 'CTN': 'cotton', 'GC': 'cotton',
    'EV': 'cotton', 'LV': 'cotton', 'FT': 'cotton', 'IC2': 'cotton',
    'CIR': 'cotton', 'MM': 'modal',
    'TC': 'cotton',    # Tencel+Cotton blend yarn -- cotton-dominant
    # VPC is Vasudha Primo Cotton, not a viscose yarn: the stock rows carrying it
    # spell it out in `blend_full_name` as "100 Vasudha Primo Cotton", and
    # blend_parser has always listed it among the cotton codes. Read as viscose
    # it put those rows in the wrong fibre entirely.
    'VPC': 'cotton',
    # polyester
    'P': 'polyester', 'RP': 'polyester', 'POLY': 'polyester', 'PCL': 'polyester',
    # cellulosics
    'V': 'viscose', 'VLF': 'viscose', 'R': 'viscose',
    'T': 'tencel', 'M': 'modal', 'MDL': 'modal',
    # elastane
    'E': 'elastane', 'EL': 'elastane', 'ELR': 'elastane',
    'ROICA': 'elastane', 'LY': 'elastane',
    # everything else
    'N': 'nylon', 'W': 'wool', 'A': 'acrylic', 'L': 'linen',
    'S': 'silk', 'SK': 'silk', 'H': 'hemp',
}

# Retail fibre names -> the same groups, for parsing the brief side. Longest key
# wins, so "recycled polyester" is not read as "polyester" preceded by noise.
FIBRE_GROUP_BY_WORD = {
    'organic cotton': 'cotton', 'recycled cotton': 'cotton', 'bci cotton': 'cotton',
    'supima cotton': 'cotton', 'supima': 'cotton', 'cotton': 'cotton',
    'bci': 'cotton', 'ctn': 'cotton', 'cot': 'cotton',
    'recycled polyester': 'polyester', 'polyester': 'polyester',
    'rpet': 'polyester', 'poly': 'polyester', 'pes': 'polyester',
    'spandex': 'elastane', 'elastane': 'elastane', 'lycra': 'elastane',
    'roica': 'elastane', 'elastene': 'elastane', 'el': 'elastane',
    'viscose': 'viscose', 'rayon': 'viscose',
    'micromodal': 'modal', 'modal': 'modal',
    'lyocell': 'tencel', 'tencel': 'tencel',
    'polyamide': 'nylon', 'nylon': 'nylon',
    'merino wool': 'wool', 'merino': 'wool', 'wool': 'wool',
    'flax': 'linen', 'linen': 'linen',
    'acrylic': 'acrylic', 'silk': 'silk', 'hemp': 'hemp',
}

# Construction keywords -> family, scanned in order so the specific forms
# ("flatback rib", "pointelle") are claimed before the generic ones ("rib").
# Both mill shorthand and retail English are listed, because the same table
# classifies stock qualities and brief `quality` strings alike.
FAMILY_KEYWORDS = (
    # Trims come first, before any structure keyword can claim them. "TTP_HBONE"
    # is a herringbone twill TAPE and "WOVEN_1X1_PLAIN" is yardage -- the weave a
    # trim is made in does not make it fabric, and 'hbone'/'twill' sit further
    # down this table, so a trim listed later would never be reached.
    ('twill tape', 'trim'), ('ttp', 'trim'), ('tape', 'trim'),
    ('drawcord', 'trim'), ('drw cord', 'trim'), ('collar', 'trim'), ('clr', 'trim'),
    ('cuff', 'trim'), ('cuf', 'trim'), ('dori', 'trim'),

    ('pointelle', 'pointelle'), ('pntl', 'pointelle'),
    ('flatback rib', 'rib'), ('flbk', 'rib'),
    ('waffle', 'waffle'), ('honeycomb', 'waffle'), ('wfl', 'waffle'), ('hcomb', 'waffle'),
    ('ottoman', 'ottoman'), ('ottm', 'ottoman'),
    ('popcorn', 'popcorn'),
    ('purl', 'purl'),
    ('interlock', 'interlock'), ('double knit', 'interlock'),
    ('double jersey', 'interlock'), ('inl', 'interlock'), ('dj', 'interlock'),
    ('herringbone', 'herringbone'), ('hbone', 'herringbone'), ('her bone', 'herringbone'),
    ('jacquard', 'jacquard'), ('jaquard', 'jacquard'),
    ('pique', 'pique'), ('polo knit', 'pique'), ('pq', 'pique'),
    ('french terry', 'terry'), ('terry', 'terry'),
    ('fleece', 'fleece'), ('flc', 'fleece'),
    ('poplin', 'woven'), ('poplene', 'woven'), ('voile', 'woven'),
    ('woven', 'woven'), ('twill', 'woven'),
    ('mesh', 'mesh'), ('lace', 'lace'),
    ('raschel', 'raschel'),
    ('berber', 'berber'), ('ber knit', 'berber'),
    ('single jersey', 'jersey'), ('jersey', 'jersey'),
    ('sjy', 'jersey'), ('sj', 'jersey'),
    ('rib', 'rib'),
)

# The construction vocabulary `quality_label` is reported in -- the nine
# constructions a brief is actually written in.
#
# Scoring keeps the finer families below (an ottoman is not a plain jersey, and
# RELATED_FAMILIES prices that difference), so this fold is display-only: the
# two jersey-based structures the mill itself names as jersey report as Single
# Jersey, while `construction_family` still says which they are. Families with
# no entry here (woven, herringbone, berber, trim) fall back to their decoded
# structure label rather than being forced into a bucket they don't belong in.
QUALITY_LABELS = {
    'jersey': 'Single Jersey',
    'rib': 'Rib',
    'interlock': 'Interlock',
    'fleece': 'Fleece',
    'waffle': 'Waffle',
    'terry': 'Terry',
    'pointelle': 'Pointelle',
    'pique': 'Pique',
    'popcorn': 'Popcorn',
    'ottoman': 'Single Jersey',   # OTTM_SJY -- Ottoman Single Jersey
    'purl': 'Single Jersey',      # PURL_JERSEY_EL
}

# How readily one family substitutes for another, 0-1. Symmetric; an exact
# family match scores 1.0 and anything unlisted scores 0.
RELATED_FAMILIES = {
    frozenset(('jersey', 'pointelle')): 0.75,
    frozenset(('jersey', 'popcorn')): 0.60,
    frozenset(('jersey', 'purl')): 0.60,
    frozenset(('jersey', 'ottoman')): 0.50,
    frozenset(('jersey', 'pique')): 0.50,
    frozenset(('jersey', 'interlock')): 0.45,
    frozenset(('jersey', 'terry')): 0.30,
    frozenset(('rib', 'waffle')): 0.60,
    frozenset(('rib', 'ottoman')): 0.55,
    frozenset(('rib', 'pointelle')): 0.50,
    frozenset(('rib', 'interlock')): 0.45,
    frozenset(('rib', 'purl')): 0.40,
    frozenset(('interlock', 'pique')): 0.50,
    frozenset(('interlock', 'jacquard')): 0.45,
    frozenset(('terry', 'fleece')): 0.80,
    frozenset(('pique', 'waffle')): 0.40,
    frozenset(('pointelle', 'lace')): 0.45,
    frozenset(('pointelle', 'mesh')): 0.35,
}

# Texture calls a brief may make that the family alone does not carry. A tag
# shared by brief and stock is worth a small construction bonus.
TEXTURE_TAGS = {
    'variegated': ('variegated', 'verigated', 'ver rib', 'ver_rib'),
    'striped': ('stripe', 'striped', 'stp'),
    'slub': ('slub',),
    'plated': ('plated', 'pltd'),
    'melange': ('melange', 'heather', 'mlw', 'marl'),
    'structured': ('structure', 'structured', 'textured'),
    'brushed': ('brushed', 'peached'),
}
TEXTURE_TAG_BONUS = 8  # points added to the construction sub-score, capped at 100

_PCT_FIBRE_RE = re.compile(r'(\d+(?:\.\d+)?)\s*%?\s*([A-Za-z][A-Za-z\- ]*)')
_RATIO_RE = re.compile(r'\b(\d+)\s*[xX]\s*(\d+)\b')


# --- `q` patterns, compiled off the vocabularies above ---
# Every alternation is built from the same tables the scorer uses, so the words
# `q` recognises and the words a brief is scored against can never drift apart.

def _query_alternation(words, longest_first=True):
    '''
    A regex alternation over `words`.

    Longest first by default, so a multi-word form is claimed before the single
    word inside it ("organic cotton" before "cotton", "less than" before
    "less"). FAMILY_KEYWORDS passes longest_first=False because its own order is
    already meaningful -- specific constructions before the generic ones they
    contain -- and length would not reproduce it.
    '''
    words = list(dict.fromkeys(words))
    if longest_first:
        words.sort(key=len, reverse=True)
    return '|'.join(re.escape(w) for w in words)


_QUERY_NUM = r'\d+(?:\.\d+)?'
_QUERY_OP_ALT = _query_alternation(QUERY_OPERATORS)
# A number, or a range written any of the three ways people write one.
_QUERY_SPAN = rf'(?P<lo>{_QUERY_NUM})(?:\s*(?:-|to|and)\s*(?P<hi>{_QUERY_NUM}))?'


def _compile_metric_patterns(metrics=None):
    '''
    Two patterns per metric: the unit-suffixed form people write first ("180
    gsm", "under 500 kg") and the keyword-prefixed one ("gsm 180", "price <
    500"). Tried in that order, because the suffixed form is the more specific:
    matching "width 60" first would leave a bare "inch" behind to be searched
    for as text.

    `metrics` defaults to this module's QUERY_METRICS. It is a parameter so a
    sibling catalogue over a different doctype can compile the same query
    language against the facets IT carries, rather than copying the patterns
    and letting the two drift (see moodboard_dyed_recommender).
    '''
    compiled = {}
    for key, metric in (metrics or QUERY_METRICS).items():
        patterns = []
        if metric['units']:
            patterns.append(re.compile(
                rf'(?:(?P<op>{_QUERY_OP_ALT})(?![a-z])\s*)?(?:between\s+)?'
                rf'{_QUERY_SPAN}\s*'
                rf'(?:{_query_alternation(metric["units"])})(?![a-z])'
            ))
        if metric['keywords']:
            patterns.append(re.compile(
                rf'(?<![a-z])(?:{_query_alternation(metric["keywords"])})(?![a-z])'
                rf'\s*(?:(?P<op>{_QUERY_OP_ALT})(?![a-z])\s*)?(?:between\s+)?'
                rf'{_QUERY_SPAN}'
            ))
        compiled[key] = tuple(patterns)
    return compiled


def _compile_unsupported_patterns(phrases=None):
    '''
    An unsupported facet plus the comparison written around it, so "fob < 3" and
    "$3" leave nothing behind. The number is swallowed on purpose -- a stray "3"
    in the leftovers would go on to match every fab code containing a 3.

    `phrases` defaults to QUERY_UNSUPPORTED, and is a parameter for the same
    reason _compile_metric_patterns takes one.
    '''
    return {
        phrase: re.compile(
            rf'(?:{_QUERY_NUM}\s*)?(?<![a-z]){re.escape(phrase)}(?![a-z])'
            rf'(?:\s*(?:{_QUERY_OP_ALT})?(?![a-z])\s*{_QUERY_NUM})?'
        )
        for phrase in (phrases or QUERY_UNSUPPORTED)
    }


def _query_noise_words(metrics=None):
    '''
    Stop words plus every metric keyword, unit and comparator -- all of them
    meaningless on their own once the number they qualified has been read.

    Multi-word entries are split into their words as well, because the residual
    is compared token by token: "up to" is one comparator here but two tokens
    there, and half of it surviving as a text condition ("up") is enough to
    empty the results.
    '''
    words = set(QUERY_STOP_WORDS) | set(QUERY_OPERATORS)
    for metric in (metrics or QUERY_METRICS).values():
        words |= set(metric['keywords']) | set(metric['units'])
    return frozenset(words | {word for phrase in words for word in phrase.split()})


_QUERY_METRIC_RES = _compile_metric_patterns()
_QUERY_UNSUPPORTED_RES = _compile_unsupported_patterns()
_QUERY_NOISE = _query_noise_words()

# Construction: FAMILY_KEYWORDS order preserved, so "single jersey" is read as
# one construction and not as the word "jersey" with noise in front of it.
_FAMILY_BY_KEYWORD = dict(FAMILY_KEYWORDS)
_QUERY_FAMILY_RE = re.compile(
    rf'(?<![a-z])(?:{_query_alternation([k for k, _ in FAMILY_KEYWORDS], False)})(?![a-z])'
)

# Fibre, with the percentage optional: "cotton" asks for any cotton content,
# "95% cotton" asks for that much of it. The `%` is required for the numeric
# form -- without it "180 cotton" would read a GSM that got away as a percentage.
_QUERY_FIBRE_RE = re.compile(
    rf'(?:(?P<pct>{_QUERY_NUM})\s*%\s*)?'
    rf'(?<![a-z])(?P<fibre>{_query_alternation(FIBRE_GROUP_BY_WORD)})(?![a-z])'
)

_TAG_BY_KEYWORD = {k: tag for tag, keywords in TEXTURE_TAGS.items() for k in keywords}
_QUERY_TEXTURE_RE = re.compile(
    rf'(?<![a-z])(?:{_query_alternation(_TAG_BY_KEYWORD)})(?![a-z])'
)


# --- read ---
@frappe.whitelist(allow_guest=True)
@auth_required
def recommend(
    signal: dict,
    colors: list = None,
    material_types: list = None,
    max_items: int = DEFAULT_MAX_ITEMS,
    min_available: float = 0,
    with_image_only: bool = False,
    withImageOnly: bool = None,
    use_llm: bool = True,
):
    '''
    Recommends surplus stock fabrics for a design/sourcing brief.

    Args:
        signal: the input signal, shaped as

            {
                "description": "<prose rationale for the fabric choices>",
                "fabrics": [
                    {
                        "quality": "Single Jersey",
                        "blend": "95% Cotton/5% Spandex",
                        "gsm": "180",
                        "best_for": "<garment types this fabric serves>",
                        "reason": "<why the brief calls for it>"
                    },
                    ...
                ],
                "source": "mixed"
            }

            Only `fabrics` drives matching; `description` and the per-fabric
            `best_for` / `reason` prose is passed to the LLM for context and for
            attributing each recommendation back to a brief line. A bare list of
            fabric dicts is accepted too.

        colors: the range's colour palette, as
            [{"id": "col_294", "name": "Ocean Blue", "hex": "#4A90C4",
              "pantone": "17-4139", "selected": true}, ...]
            Also read from signal['colors'] when not passed separately.

            Supplying it turns colour into a FILTER: an LLM pass maps every stock
            colour name onto the palette (stock colour is free text and often
            truncated, so nothing deterministic can do this), and only fab codes
            that either stock a palette colour or hold undyed stock survive.
            Each survivor reports `matched_colors`, its hero moves to the first
            palette colour it serves, and the returned set is pushed toward
            covering one fab code per palette colour -- so `max_items` is raised
            to the palette size when the caller asked for fewer.

            Omit it and nothing about colour changes: the old construction /
            composition / GSM behaviour stands.

        material_types: optional list of "Material type desc." values to restrict
            the search to (e.g. ['DYED FABRIC']). Omit for all types. NOTE: only
            dyed stock exists today; for stock types that would be recoloured to
            order, the colour/batch behaviour is still pending, so such rows are
            returned as-is with whatever colour they carry.
        max_items: how many fab codes to return (clamped to [1, MAX_ITEMS_CAP]).
        min_available: drop fab-code groups whose summed Total Qty. is below this.
        with_image_only: keep only fab codes that can actually be SHOWN -- and,
            when a palette is given, shown in a colour the palette asked for.

            Two levels, because the card is chosen at both. First a fab code is
            kept only if one of its lots carries recorded image keys (not merely
            the `has_image_file` flag, which the superseded update_image_flag()
            writes on its own and which therefore does not guarantee a URL).
            Then, with a palette, an unphotographed colourway cannot serve a
            palette colour and cannot count as dyeable -- so `matched_colors`
            lists only colours whose own batch is photographed, and the hero the
            card opens on is that same batch. Colour and picture are never taken
            from different lots.

            Without this flag nothing changes: colour still wins the hero, and a
            matched colourway with no photograph still returns a null image_url.

            This is a hard filter on a sparsely populated field, so it can
            legitimately empty the shelf; that reads back as
            'no surplus stock matched the given filters!' or, once a palette has
            been applied, as 'no surplus stock is photographed in (or dyeable
            to) the requested colours!'.
        withImageOnly: the same flag under the camelCase name the JS clients
            send. Wins when both spellings are given.
        use_llm: when False, skip the LLM re-rank and return the deterministic
            ranking. Faster and free; loses the written reasons.

    Returns:
        {
            'success': True,
            'data': {
                'match_count': <len(recommendations)>,
                'recommendations': [<fab-code group>, ...],  # best first
                'brief_coverage': [                          # per brief line
                    {'brief_fabric': <label>, 'fab_codes': [...]}, ...
                ],
                'color_coverage': [                          # per palette colour
                    {'palette_id', 'palette_name', 'palette_hex',
                     'fab_codes': [...], 'covered': <bool>}, ...
                ],                                           # [] with no palette
                'catalogue_size': <fab-code groups searched>,
                'colors_matched': <bool>,                    # a palette was applied
                'llm_used': <bool>,
            },
        }
        or {'success': False, 'error': <message>} on a bad signal / on error.

    Two prices ride on each group, and they are not the same kind of number:

        price                      what this stock is valued at, in rupees, summed
                                   across the fab code -- a lot total
        price_per_uom              what it was valued at per KG -- price / available
        price_from_fabric_masters  what the same cloth costs to MAKE per KG, from
                                   the Fabric Master the stock was matched to
                                   (`closest_fabric_master`): yarn + knitting +
                                   dyes & chemicals + M&C finish + finishing, loss
                                   % included. The figure costing.get_garment_cost
                                   rolls into a garment.

    So `price_from_fabric_masters` compares against `price_per_uom`, not `price`,
    and the gap between the two is the case for buying the surplus.

    It is precomputed onto the stock rows (costing a fabric cold makes LLM calls),
    so it reads None until a match run has costed that fabric -- see
    fabric_matcher.refresh_fabric_costs.
    '''

    try:
        brief_fabrics = _brief_fabrics(signal)
        if not brief_fabrics:
            return {'success': False, 'error': 'signal has no usable "fabrics" entries!'}

        palette = _brief_colors(
            colors if colors is not None else _as_dict(signal).get('colors'))

        max_items = max(1, cint(max_items) or DEFAULT_MAX_ITEMS)
        # One fab code usually carries one colourway (the median across current
        # stock is exactly 1), so covering an 8-colour palette needs ~8 fab
        # codes. Capping at 6 would decide up front that most of the palette
        # goes unserved, so the palette raises the ceiling it needs.
        max_items = min(max(MAX_ITEMS_CAP, len(palette)), max(max_items, len(palette)))

        images_only = _as_bool(_sent(withImageOnly, with_image_only))
        groups = _fab_code_groups(
            material_types=_as_list(material_types),
            min_available=flt(min_available),
            with_image_only=images_only,
        )
        if not groups:
            return {'success': False, 'error': 'no surplus stock matched the given filters!'}

        #--- 0. colour pass: map stock colours onto the palette, then filter ---
        if palette:
            color_map = _map_stock_colors(
                palette,
                {_color_key(v['color']) for g in groups for v in g['colors']},
            )
            for group in groups:
                _annotate_colors(group, palette, color_map, images_only=images_only)

            eligible = [g for g in groups if g['matched_colors'] or g['needs_dyeing']]
            if not eligible:
                return {
                    'success': False,
                    'error': (
                        'no surplus stock is photographed in (or dyeable to) the '
                        'requested colours!' if images_only else
                        'no surplus stock is available in (or dyeable to) the '
                        'requested colours!'
                    ),
                }
            groups = eligible

        #--- 1. deterministic pass: score every group against every brief line ---
        for group in groups:
            scores = [_score(brief, group) for brief in brief_fabrics]
            best = max(scores, key=lambda s: s['fit_score'])
            group['fit_score'] = best['fit_score']
            group['score_breakdown'] = best['breakdown']
            group['matched_brief_index'] = best['brief_index']
            group['_by_brief'] = {s['brief_index']: s['fit_score'] for s in scores}

        groups.sort(key=lambda g: g['fit_score'], reverse=True)
        shortlist = _shortlist(groups, brief_fabrics, palette, LLM_SHORTLIST_SIZE)

        #--- 2. LLM pass: re-rank the shortlist and write the rationale ---
        picks, llm_used = [], False
        if _as_bool(use_llm, default=True):
            picks = _llm_rerank(signal, brief_fabrics, palette, shortlist, max_items)
            llm_used = bool(picks)

        recommendations = _finalise(brief_fabrics, palette, shortlist, picks, max_items)

        return {
            'success': True,
            'data': {
                'match_count': len(recommendations),
                'recommendations': recommendations,
                'brief_coverage': _brief_coverage(brief_fabrics, recommendations),
                'color_coverage': _color_coverage(palette, recommendations),
                'catalogue_size': len(groups),
                'colors_matched': bool(palette),
                'llm_used': llm_used,
            },
        }

    except Exception as ex:
        frappe.log_error(frappe.get_traceback(), 'surplus_recommender.recommend()')
        return {'success': False, 'error': str(ex)}


@frappe.whitelist(allow_guest=True)
@auth_required
def catalogue(
    search: str = None,
    q: str = None,
    material_types: list = None,
    min_available: float = 0,
    with_image_only: bool = False,
    withImageOnly: bool = None,
    sort_by=_DEFAULT_SORT_BY,
    sort_dir=_DEFAULT_SORT_DIR,
    limit=_DEFAULT_LIMIT,
    offset=0,
):
    '''
    Returns the fab-code groups the recommender searches, unranked -- the same
    aggregation `recommend()` scores, minus the scores. Useful for browsing what
    is groupable, and for checking how a group's GSM / colours were resolved.

    Search: `search` is a case-insensitive substring matched across
    CATALOGUE_SEARCH_FIELDS -- fab code, batch, quality, blend -- returning the
    whole fab-code group behind any matching row.

    Query: `q` is the same box read as a sentence -- "cotton single jersey 180
    gsm 60 inch" -- parsed into facet filters plus whatever words are left over.
    It understands, in any order and any mix:

      construction  single jersey, rib, interlock, waffle, terry, fleece,
                    pique, pointelle, jacquard, mesh, woven, ... (FAMILY_KEYWORDS)
      fibre         cotton, polyester, spandex, viscose, modal, tencel, linen,
                    ... optionally with a share -- "95% cotton"
      texture       slub, melange, striped, brushed, plated, variegated
      gsm           "180 gsm", "gsm 180", "gsm > 160", "160-200 gsm"
      width         "60 inch", "width 60", "58-60 inch"
      dia / gauge   "dia 34", "24 gauge"
      price         "price < 500", "under 500 rs"  -- INR per KG, on price_per_uom
      make cost     "make cost < 400"              -- INR per KG, on price_from_fabric_masters
      availability  "over 500 kg", "qty > 1000"
      ageing        "ageing < 180 days"

    Comparators are >, >=, <, <=, =, and the words for them (over, under, at
    least, more than, up to, ...); > and < are inclusive. Every number is taken
    literally -- "150 gsm" is 150 GSM, not a band around it -- so approximate
    weight is asked for as a range, "140-160 gsm", or as a bound, "gsm > 140".

    Fibre shares are the one exception, and are always a band rather than a
    comparison: "95% cotton" is 90-100% cotton. Stock blends are rescaled to
    total 100 and rounded to two places before they are compared, so a 95:5
    cotton-elastane can arrive as 94.99, and an exact test on the share would
    reject the very cloth it describes. A fibre named without a share only has
    to be present at all.

    Anything left after parsing becomes a text condition,
    matched across QUERY_SEARCH_FIELDS (which, unlike `search`, includes
    `quality_full_name`, `blend_full_name`, `color` and `material_desc`, so
    colour words and retail English land somewhere). Leftover words are ANDed
    with each other and with everything else.

    `q` is separate from `search` rather than an upgrade of it, because `search`
    is a literal substring its callers already depend on: read as a query, a
    batch number like "SJ-180" would start filtering on GSM. Send both and they
    are ANDed. Facets stock does not carry (FOB, USD prices, MOQ, lead time) are
    recognised and reported under `query.unsupported` instead of being searched
    for as words -- see QUERY_UNSUPPORTED for why FOB in particular cannot be
    answered from stock.

    A group whose value for a filtered facet is unknown -- no GSM decoded, no
    price, no width recorded -- does not pass that filter. It cannot be shown to
    satisfy it, and the sort already treats unknown as last rather than as zero.

    When a leftover word turns out to be a colour the group stocks, that
    colourway leads the card (`batch`, `hero_color`, `image_url`) and is named in
    `matched_color`; searching "navy" and being shown the ecru photograph is
    worse than not filtering at all.

    Filters: `material_types` (list, matched against `material_type_desc`),
    `min_available` (drops groups whose summed Total Qty. is below it) and
    `with_image_only` / `withImageOnly` -- the same flag `recommend()` takes,
    under either spelling, keeping only fab codes with at least one photographed
    lot so a picture-led grid never has to render a hole. Search, query and
    filters are ANDed; `total` reflects everything applied, so an image-only
    listing still pages correctly rather than paging over holes.

    Sorting: `sort_by` is one of CATALOGUE_SORT_FIELDS -- available (default),
    price, price_per_uom, total_value, gsm, fab_code -- with `sort_dir` asc|desc
    (default desc). Groups missing the sorted value sort last in either
    direction. `price` orders on the per-UOM rate, not the lot total; sort by
    `total_value` for that.

    Pagination: limit (default 20, capped 100) + offset (>= 0). `data` is the
    same envelope the moodboard lists return -- { total, limit, offset, items }
    -- where total counts ALL matching groups (ignoring the page window) so the
    client can page. `search` / `sort_by` / `sort_dir` are echoed back as
    applied, and `query` echoes how `q` was actually read -- `applied` as
    display-ready labels, the parsed facets beside them, plus `terms` and
    `unsupported`. A query box has to be able to show its own interpretation,
    or a filter nobody meant is indistinguishable from an empty shelf.

    Prices: each group carries `price` (rupee total for the fab code),
    `price_per_uom` (that total per KG) and `price_from_fabric_masters` (what the
    cloth costs to MAKE per KG, from `closest_fabric_master`). The last compares
    against `price_per_uom`, not `price` -- see recommend() for the full note.
    Sorting is unchanged: `price` still orders on the per-UOM rate.
    '''
    try:
        limit, offset = _page(limit, offset)
        sort_by, sort_dir = _sort(sort_by, sort_dir)
        search = _clean(search)
        query = _parse_query(q)
        groups = _fab_code_groups(
            search=search,
            terms=query['terms'] if query else None,
            material_types=_as_list(material_types),
            min_available=flt(min_available),
            with_image_only=_as_bool(_sent(withImageOnly, with_image_only)),
        )
        if query:
            groups = _filter_groups(groups, query)
        _sort_groups(groups, sort_by, sort_dir)
        return {
            'success': True,
            'data': _page_envelope(
                groups[offset:offset + limit], len(groups), limit, offset,
                search=search, sort_by=sort_by, sort_dir=sort_dir,
                q=_clean(q), query=_query_summary(query),
            ),
        }

    except Exception as ex:
        frappe.log_error(frappe.get_traceback(), 'surplus_recommender.catalogue()')
        return {'success': False, 'error': str(ex)}


@frappe.whitelist(allow_guest=True)
@auth_required
def batches(fab_code: str = None):
    '''
    Every individual stock lot under one fab code -- the level `recommend()` and
    `catalogue()` deliberately collapse.

    Both of those return a fab code as ONE group, because a buyer commits against
    the fab code; `colors[]` narrows that only as far as one representative batch
    per colourway (see _color_variants), and the other lots of that colourway are
    not reachable from either response. A UI that lets a user choose WHICH batch
    the board shows therefore needs this: the un-collapsed list, each lot with its
    own image, colour, depth and age.

    Ordered deepest stock first -- the order _hero_lot picks from -- so the
    automatic hero is simply the first entry with `has_image`, and the list reads
    as "most representative first" rather than as whatever order the query
    returned.

    `image_url` / `thumbnail` are public asset URLs, the same ones catalogue()
    returns: unsigned and non-expiring, so a picker rendered from a stored board
    keeps working.
    '''
    try:
        fab_code = _clean(fab_code)
        if not fab_code:
            return {'success': False, 'error': 'fab_code is required'}

        lots = frappe.get_all(
            SURPLUS_STOCK_ITEM_DOCTYPE,
            filters={'fab_code': fab_code},
            fields=[
                'name', 'fab_code', 'batch', 'material_desc', 'fabric_type',
                'quality', 'blend', 'gsm', 'color', 'shade_catagory', 'total_qty',
                'total_value_rs_lakh', 'base_uom', 'width', 'dia', 'gauge',
                'ageing', 'customer_name', 'storage_location', 'stock_segment',
                'has_image_file', 'image_urls',
            ],
            limit_page_length=0,
        )
        if not lots:
            return {'success': False, 'error': f'No surplus stock found for fab code {fab_code}'}

        hero = _hero_lot(lots)
        auto_batch = hero.get('batch') if hero else None

        items = [_batch_obj(lot, auto_batch) for lot in lots]
        items.sort(key=lambda b: flt(b['available']), reverse=True)

        return {
            'success': True,
            'data': {
                'fab_code': fab_code,
                # What the recommender would pick on its own -- the board falls
                # back to this whenever no batch has been pinned, so a picker can
                # mark it "default" and offer a reset to it.
                'auto_batch': auto_batch,
                'lot_count': len(items),
                'batch_count': len({b['batch'] for b in items if b['batch']}),
                'imaged_count': sum(1 for b in items if b['has_image']),
                'available': round(sum(flt(b['available']) for b in items), 2),
                'available_uom': _dominant(lots, 'base_uom') or 'KG',
                'batches': items,
            },
        }

    except Exception as ex:
        frappe.log_error(frappe.get_traceback(), 'surplus_recommender.batches()')
        return {'success': False, 'error': str(ex)}


def _batch_obj(lot, auto_batch=None):
    '''
    One stock row -> the batch a picker offers.

    GSM and colour fall back to what `material_desc` encodes when the dedicated
    columns are empty, exactly as _build_group does, so a lot whose columns the
    mill left blank is not offered as an unlabelled row.
    '''
    derived = _decode_material_desc(lot)
    column_color = _clean(lot.get('color'))
    batch = _clean(lot.get('batch'))
    qty = flt(lot.get('total_qty'))
    value = flt(lot.get('total_value_rs_lakh'))

    return {
        'batch': batch,
        'lot_id': lot.get('name'),
        'color': column_color or derived['color'],
        'color_source': 'field' if column_color else ('material_desc' if derived['color'] else None),
        'shade_category': lot.get('shade_catagory'),
        'gsm': cint(lot.get('gsm')) or derived['gsm'] or None,
        'available': round(qty, 2),
        'available_uom': lot.get('base_uom') or 'KG',
        'price_per_uom': round(value / qty, 2) if value and qty else None,
        'width': cint(lot.get('width')) or None,
        'ageing': cint(lot.get('ageing')) or None,
        'customer_name': lot.get('customer_name'),
        'storage_location': lot.get('storage_location'),
        'stock_segment': lot.get('stock_segment'),
        # Read from the keys update_image_urls() recorded off an actual S3
        # listing, NOT from a constructed URL (every batch yields one of those,
        # image or not). This is the same test _hero_lot applies and the same one
        # moodboard_v2 validates a pin against, so what the picker shows as
        # choosable is exactly what the board will accept.
        'has_image': bool(_lot_images(lot)),
        'image_url': _image_url(lot, 'image'),
        'thumbnail': _image_url(lot, 'thumbnail'),
        'is_auto_hero': bool(batch and auto_batch and batch == auto_batch),
    }


def find_batch(fab_code, batch):
    '''
    One batch of one fab code, in the same shape batches() lists -- or None if
    that batch is not stocked under that fab code.

    Public because the moodboard validates a pinned hero batch through it: the
    rule for "does this batch exist, and does it have an image" belongs next to
    the rule that builds the list the user picked from, not copied into the
    caller where the two can drift.

    `is_auto_hero` is not meaningful for a single row (it would need the whole
    group to know), so it is reported as False rather than guessed.
    '''
    fab_code, batch = _clean(fab_code), _clean(batch)
    if not fab_code or not batch:
        return None

    rows = frappe.get_all(
        SURPLUS_STOCK_ITEM_DOCTYPE,
        filters={'fab_code': fab_code, 'batch': batch},
        fields=[
            'name', 'fab_code', 'batch', 'material_desc', 'fabric_type', 'quality',
            'blend', 'gsm', 'color', 'shade_catagory', 'total_qty',
            'total_value_rs_lakh', 'base_uom', 'width', 'dia', 'gauge', 'ageing',
            'customer_name', 'storage_location', 'stock_segment', 'has_image_file',
            'image_urls',
        ],
        limit_page_length=0,
    )
    if not rows:
        return None

    # A batch number can repeat across rows of the same fab code (one dye lot
    # split over storage locations). Any of them proves the batch belongs here,
    # so report the deepest -- and prefer one that carries the image keys, since
    # that is what decides whether the pin is usable.
    rows.sort(key=lambda r: (bool(_lot_images(r)), flt(r.get('total_qty'))), reverse=True)
    return _batch_obj(rows[0])



@frappe.whitelist(allow_guest=True)
@auth_required
def score_fabric(
    signal: dict,
    fabric: dict = None,
    fab_code: str = None,
    colors: list = None,
    use_llm: bool = True,
):
    '''
    Scores ONE fabric against a brief -- the single-candidate view of what
    recommend() does across the whole catalogue.

    recommend() answers "what should we use"; this answers "how does THIS fabric
    do, and why". A merchandiser who added a fab code by hand, or who wants to
    argue with the ranking, needs the second question answered on its own, and
    re-running the recommender to find one fab code buried in the ranking does
    not answer it -- the fabric may not even survive the shortlist.

    The number returned is the number recommend() ranks on: the deterministic fit
    score, from the same _score(), on a group derived by the same functions.
    Nothing here re-implements the scoring, so this cannot quietly disagree with
    the ranking it explains. The LLM re-rank is deliberately NOT run -- it ranks
    a shortlist against itself, so it has no meaning for a single candidate.

    Args:
        signal: the brief, in the shape recommend() takes -- {"description",
            "fabrics": [{"quality", "blend", "gsm", "best_for", "reason"}, ...]}.
            EVERY fabric line is scored, not just the best-fitting one, because
            "which of the three constructions is this fabric actually for" is
            half the question.

        fabric: the fabric to score, as the group object catalogue() /
            recommend() return -- pass one straight back. Only its raw mill
            fields are read (`quality`, `composition`/`blend`, `gsm`,
            `available`, `colors[]`); every derived field the scorer looks at is
            recomputed here, so a stale or hand-edited `composition_pct` cannot
            score itself.

        fab_code: alternative to `fabric` -- the fab code to load from stock and
            fold into a group exactly as recommend() folds it. Use this when the
            caller holds only the code. Ignored when `fabric` is given.

        colors: the palette, as recommend() takes it. Supplying it adds the
            colour view: `matched_colors`, `needs_dyeing`, the undyed-only
            penalty on the score, and `eligible` -- whether this fabric would
            have survived recommend()'s colour filter at all. Omit it and colour
            plays no part, same as recommend().

        use_llm: whether the palette -> stock-colour mapping may use the LLM. It
            is the only LLM call this endpoint can make. False falls back to
            conservative string matching: instant and free, and it misses the
            spellings only a model resolves. No effect without `colors`.

    Returns:
        {
            'success': True,
            'data': {
                'fab_code': <str or None>,
                'fit_score': <0-100, the best brief line's score>,
                'match_score': <fit_score rounded, as recommendations carry it>,
                'matched_brief_index': <the brief line it serves best>,
                'matched_brief_fabric': <that line's label>,
                'best_for': <that line's garment types>,
                'score_breakdown': {construction, composition, gsm,
                                    availability, stretch_penalty, color_penalty},
                'notes': [<plain-English reasons the score landed here>, ...],
                'scores': [<the same per brief line, best first, each with the
                            side-by-side it was computed from>, ...],
                'weights': {<the weight each sub-score carries>},
                'eligible': <would survive recommend()'s colour filter>,
                'ineligible_reason': <str or None>,
                'colors_matched': <a palette was applied>,
                'matched_colors': [...],   # palette colours this fab code stocks
                'unmatched_colors': [...], # palette colours it does not
                'needs_dyeing': <holds undyed stock>,
                'fabric': <the normalised group the score was computed on>,
                'source': 'payload' | 'stock',
            },
        }
        or {'success': False, 'error': <message>}.
    '''

    try:
        brief_fabrics = _brief_fabrics(signal)
        if not brief_fabrics:
            return {'success': False, 'error': 'signal has no usable "fabrics" entries!'}

        palette = _brief_colors(
            colors if colors is not None else _as_dict(signal).get('colors'))

        group, source, error = _resolve_group(fabric, fab_code)
        if error:
            return {'success': False, 'error': error}

        #--- 0. colour pass: the annotation recommend() runs, on one group ---
        # The vocabulary is this fabric's own colourways, not the catalogue's, so
        # the map recommend() cached is not reusable here and this pays for its
        # own -- much smaller -- call.
        if palette:
            vocabulary = {_color_key(v['color']) for v in group['colors'] if v['color']}
            if _as_bool(use_llm, default=True):
                color_map = _map_stock_colors(palette, vocabulary)
            else:
                color_map = _fallback_color_map(palette, _matchable_colors(vocabulary))
            _annotate_colors(group, palette, color_map)

        # recommend() drops a fabric that neither stocks a palette colour nor
        # could be dyed to one, before it is ever scored. Here it is scored
        # anyway: "it would have been filtered out" is itself the answer the
        # caller asked for, and it is more use reported alongside a score than
        # returned as an error instead of one.
        eligible, ineligible_reason = True, None
        if palette and not (group['matched_colors'] or group['needs_dyeing']):
            eligible = False
            ineligible_reason = ('carries none of the palette colours and holds no '
                                 'undyed stock, so recommend() would filter it out')

        #--- 1. deterministic pass: this group against every brief line ---
        scores = [_explain_score(brief, group) for brief in brief_fabrics]
        scores.sort(key=lambda s: s['fit_score'], reverse=True)
        best = scores[0]

        matched_ids = {m['palette_id'] for m in (group.get('matched_colors') or [])}

        return {
            'success': True,
            'data': {
                'fab_code': group.get('fab_code'),
                'fit_score': best['fit_score'],
                'match_score': int(round(best['fit_score'])),
                'matched_brief_index': best['brief_index'],
                'matched_brief_fabric': best['brief_fabric'],
                'best_for': best['best_for'],
                'score_breakdown': best['breakdown'],
                'notes': best['notes'],
                'scores': scores,
                'weights': {
                    'construction': W_CONSTRUCTION,
                    'composition': W_COMPOSITION,
                    'gsm': W_GSM,
                    'availability': W_AVAILABILITY,
                },
                'eligible': eligible,
                'ineligible_reason': ineligible_reason,
                'colors_matched': bool(palette),
                'matched_colors': group.get('matched_colors') or [],
                'unmatched_colors': [
                    {
                        'palette_id': color['id'],
                        'palette_name': color['name'],
                        'palette_hex': color['hex'],
                        'palette_pantone': color['pantone'],
                    }
                    for color in palette if color['id'] not in matched_ids
                ],
                'needs_dyeing': bool(group.get('needs_dyeing')),
                'fabric': group,
                'source': source,
            },
        }

    except Exception as ex:
        frappe.log_error(frappe.get_traceback(), 'surplus_recommender.score_fabric()')
        return {'success': False, 'error': str(ex)}


# --- grouping ---
def _fab_code_groups(material_types=None, min_available=0, with_image_only=False,
                     search=None, terms=None):
    '''
    Folds Surplus Stock rows into one group per Fab Code.

    Quality and GSM are consistent within a fab code, so they are taken from the
    dominant row; blend and colour are not (one quality gets dyed many ways), so
    every distinct value is kept, ordered by the quantity behind it. Quantities
    and values are summed across the group -- that is the whole point of the
    grouping, since a buyer commits against the fab code, not one dye lot.

    `search` narrows to the fab codes a matching row belongs to, resolved in a
    separate pass so the group itself is still built from ALL of that fab code's
    lots (see _search_fab_codes).

    `terms` are the leftover words from a `q` query, narrowing the same way but
    over the wider QUERY_SEARCH_FIELDS. They are ANDed with each other and with
    `search`: two words typed together are two conditions on one fab code, not
    alternatives. Each word is still ORed across the columns, and each is
    resolved per fab code, so "navy slub" is satisfied by a fab code whose
    colour column says navy and whose quality says slub -- even on different
    lots of it, which is the same reasoning that lets a batch number pull back
    its whole group.
    '''
    filters = {'fab_code': ['is', 'set']}
    if material_types:
        filters['material_type_desc'] = ['in', material_types]

    fab_codes = None
    if search:
        fab_codes = set(_search_fab_codes(search))
    for term in terms or []:
        matched = set(_search_fab_codes(term, QUERY_SEARCH_FIELDS))
        fab_codes = matched if fab_codes is None else (fab_codes & matched)
        if not fab_codes:
            return []
    if fab_codes is not None:
        if not fab_codes:
            return []
        filters['fab_code'] = ['in', sorted(fab_codes)]

    rows = frappe.get_all(
        SURPLUS_STOCK_ITEM_DOCTYPE,
        filters=filters,
        fields=_GROUP_FIELDS,
        limit_page_length=0,
    )

    by_code = {}
    for row in rows:
        by_code.setdefault(row['fab_code'], []).append(row)

    groups = []
    for fab_code, lots in by_code.items():
        group = _build_group(fab_code, lots)
        if min_available and (group['available'] or 0) < min_available:
            continue
        # Both tests, because the two can disagree and only one of them is what
        # a caller gets to render. `has_image_file` says an image EXISTS for the
        # batch; `image_urls` holds the object keys read off an actual S3
        # listing, and _lot_images / _image_url return a URL from those alone.
        # The superseded update_image_flag() writes the flag without the keys,
        # so a flag-only row would pass this filter and then render nothing.
        # Requiring the keys makes the flag mean what the caller asked it to.
        if with_image_only and not any(
            lot.get('has_image_file') and _lot_images(lot) for lot in lots
        ):
            continue
        groups.append(group)

    return groups


def _search_fab_codes(term, fields=None):
    '''
    The fab codes with at least one row matching `term` on any of `fields`,
    defaulting to CATALOGUE_SEARCH_FIELDS.

    Deliberately a separate query from the one that loads the lots: filtering
    the main fetch by the search would sum only the matching rows, so a batch
    search would report a fab code as one lot deep. This resolves WHICH codes
    match; the caller then loads all their lots.
    '''
    rows = frappe.get_all(
        SURPLUS_STOCK_ITEM_DOCTYPE,
        filters={'fab_code': ['is', 'set']},
        or_filters=[[field, 'like', _like(term)]
                    for field in (fields or CATALOGUE_SEARCH_FIELDS)],
        fields=['fab_code'],
        distinct=True,
        limit_page_length=0,
    )
    return sorted({row['fab_code'] for row in rows if row.get('fab_code')})


def _like(term):
    ''' A LIKE pattern with the user's own %, _ and \\ taken literally. '''
    escaped = str(term).replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')
    return f'%{escaped}%'


# --- catalogue query language ---
def _parse_query(text):
    '''
    One typed line -> the facets it asked for, plus the words left over.

    Read strictly left to right through the vocabularies, most specific first,
    each pass consuming what it claimed so the next one never sees it again.
    That ordering is the whole design: "cotton single jersey 180 gsm 60 inch"
    only parses because "180 gsm" and "60 inch" are taken as numbers before
    anything can read 180 as a percentage, and "single jersey" is taken as one
    construction before "jersey" can be read on its own.

    Unsupported facets go first so the comparison attached to them ("fob < 3")
    is removed whole -- otherwise the "< 3" survives to be misread as some other
    metric, or the "3" survives to be searched for as text.

    Deterministic on purpose. This is a filter box: it has to answer instantly,
    identically every time, and offline. The LLM in this module is for reading a
    brief's prose, where the input really is ambiguous; a caller typing
    "180 gsm" has already been unambiguous.

    Returns None for an empty query, so callers can skip the whole pass.
    '''
    raw = _clean(text)
    if not raw:
        return None

    parsed = {
        'raw': raw,
        'ranges': {},      # metric key -> {min, max}
        'families': [],    # construction families
        'fibres': {},      # fibre group -> required percent (0 = any)
        'textures': [],    # texture tags
        'terms': [],       # leftover words, matched as text
        'unsupported': [], # recognised, but not answerable from stock
    }

    working = _normalise_query(raw)
    working = _extract_unsupported(working, parsed)
    working = _extract_metrics(working, parsed)
    working = _extract_families(working, parsed)
    working = _extract_textures(working, parsed)
    working = _extract_fibres(working, parsed)
    parsed['terms'] = _residual_terms(working)
    return parsed


def _normalise_query(text):
    '''
    The typed line in the one shape the patterns expect: lower case, currency
    and inch marks spelled out, separators reduced to spaces.

    Comparators survive as themselves -- <, >, = and % are the query's own
    punctuation, not noise.
    '''
    text = str(text).lower()
    text = text.replace('₹', ' rs ').replace('$', ' usd ')
    text = text.replace('"', ' inch ').replace('”', ' inch ')
    text = re.sub(r'[–—]', '-', text)
    text = re.sub(r'[,;/|()\[\]_]+', ' ', text)
    return re.sub(r'\s+', ' ', text).strip()


def _extract_unsupported(working, parsed):
    ''' Strip the facets stock cannot answer, recording why. '''
    for phrase, reason in QUERY_UNSUPPORTED.items():
        pattern = _QUERY_UNSUPPORTED_RES[phrase]
        if pattern.search(working):
            parsed['unsupported'].append({'term': phrase, 'reason': reason})
            working = pattern.sub(' ', working)
    return working


def _extract_metrics(working, parsed):
    '''
    Every numeric facet, in QUERY_METRICS order, each pattern consuming its
    match. Repeated mentions of one metric intersect rather than overwrite, so
    "gsm > 160 gsm < 200" is the same request as "160-200 gsm".
    '''
    for key, metric in QUERY_METRICS.items():
        for pattern in _QUERY_METRIC_RES[key]:
            for match in pattern.finditer(working):
                bounds = _query_bounds(match, metric)
                if bounds:
                    parsed['ranges'][key] = _merge_bounds(parsed['ranges'].get(key), bounds)
            working = pattern.sub(' ', working)
    return working


def _query_bounds(match, metric):
    ''' One matched number (or range) + its comparator -> {min, max}. '''
    cast = metric['cast']
    try:
        lo = cast(float(match.group('lo')))
    except (TypeError, ValueError):
        return None

    hi = match.groupdict().get('hi')
    if hi is not None:
        try:
            hi = cast(float(hi))
        except (TypeError, ValueError):
            return None
        return {'min': min(lo, hi), 'max': max(lo, hi)}

    operator = QUERY_OPERATORS.get((match.groupdict().get('op') or '').strip(), 'eq')
    if operator == 'min':
        return {'min': lo, 'max': None}
    if operator == 'max':
        return {'min': None, 'max': lo}

    # A bare equality, taken literally: "150 gsm" is 150. Anyone wanting the
    # weights around it says so -- "140-160 gsm", "gsm > 140".
    return {'min': lo, 'max': lo}


def _merge_bounds(existing, bounds):
    ''' Two bounds on one metric, intersected -- the tighter of each side wins. '''
    if not existing:
        return bounds
    lows = [v for v in (existing.get('min'), bounds.get('min')) if v is not None]
    highs = [v for v in (existing.get('max'), bounds.get('max')) if v is not None]
    return {
        'min': max(lows) if lows else None,
        'max': min(highs) if highs else None,
    }


def _extract_families(working, parsed):
    ''' Construction words -> families, in FAMILY_KEYWORDS' own precedence. '''
    for match in _QUERY_FAMILY_RE.finditer(working):
        family = _FAMILY_BY_KEYWORD.get(match.group(0))
        if family and family not in parsed['families']:
            parsed['families'].append(family)
    return _QUERY_FAMILY_RE.sub(' ', working)


def _extract_textures(working, parsed):
    '''
    Texture words -> tags. After the families, so "variegated rib" is a rib that
    is variegated; the cost is that the mill's own "ver rib" reads as rib alone,
    since 'rib' is claimed before 'ver rib' can be. Anyone typing the shorthand
    still gets the right construction, only without the tag.
    '''
    for match in _QUERY_TEXTURE_RE.finditer(working):
        tag = _TAG_BY_KEYWORD.get(match.group(0))
        if tag and tag not in parsed['textures']:
            parsed['textures'].append(tag)
    return _QUERY_TEXTURE_RE.sub(' ', working)


def _extract_fibres(working, parsed):
    '''
    Fibre words -> the fibre groups compositions are compared in, with the
    percentage when one was written. The largest percentage asked for wins if a
    fibre is named twice.
    '''
    for match in _QUERY_FIBRE_RE.finditer(working):
        fibre = FIBRE_GROUP_BY_WORD.get(match.group('fibre'))
        if not fibre:
            continue
        percent = flt(match.group('pct')) if match.group('pct') else 0
        parsed['fibres'][fibre] = max(parsed['fibres'].get(fibre, 0), percent)
    return _QUERY_FIBRE_RE.sub(' ', working)


def _residual_terms(working):
    '''
    What is left once every vocabulary has taken its share: the words to match
    as text. Stop words, stranded units and comparators are dropped -- once
    "60 inch" has been read as a width, hunting for the word "inch" in a fab
    code would return nothing and take the whole query down with it.

    Single characters go too (they are the debris of "2x2" and "95%"), but bare
    numbers stay: a number nothing claimed is usually a batch or a fab code,
    which is exactly what a text match is for.
    '''
    terms, seen = [], set()
    for token in working.split():
        token = token.strip('%<>=-.').strip()
        if len(token) < 2 or token in _QUERY_NOISE or token in seen:
            continue
        seen.add(token)
        terms.append(token)
    return terms[:QUERY_MAX_TERMS]


def _filter_groups(groups, query):
    '''
    The parsed facets applied to assembled groups.

    Runs here rather than in SQL because the values being filtered only exist
    here: `available` and `price_per_uom` are sums over the fab code's lots, and
    `gsm` is the dominant lot's with the `material_desc` fallback applied. A row
    filter would keep fab codes whose group then reports a value outside the
    range that was asked for.
    '''
    kept = []
    for group in groups:
        if not _group_matches(group, query):
            continue
        group['matched_color'] = None
        _promote_matching_variant(group, query['terms'])
        kept.append(group)
    return kept


def _group_matches(group, query):
    ''' One group against every facet in the query. All of them, or none. '''
    for key, bounds in query['ranges'].items():
        if not _in_bounds(group, QUERY_METRICS[key], bounds):
            return False

    if query['families'] and not _query_family_matches(group, query['families']):
        return False

    for fibre, required in query['fibres'].items():
        percent = flt((group.get('composition_pct') or {}).get(fibre))
        if percent <= 0:
            return False
        if required and not (
            required - QUERY_FIBRE_TOLERANCE <= percent <= required + QUERY_FIBRE_TOLERANCE
        ):
            return False

    if query['textures'] and not set(query['textures']).issubset(group.get('texture_tags') or ()):
        return False

    return True


def _in_bounds(group, metric, bounds):
    '''
    Whether a group's value for one metric sits inside the requested bounds.

    A group with no value for the metric fails, and deliberately: an
    undecodable GSM is not 0 GSM and an unpriced group is not free, so neither
    can be shown to satisfy a filter. It is the same call `_sort_groups` makes
    when it pushes missing values to the end instead of sorting them as zero.
    '''
    value = group.get(metric['field'])

    if metric.get('multi'):
        values = [flt(v) for v in (value or [])]
    elif metric.get('reader') == 'text':
        # Stored as typed ("34\"", "24 GG") -- read the number out of it.
        values = [flt(_first_int(value))] if _clean(value) else []
    else:
        values = [] if value in (None, '') else [flt(value)]

    values = [v for v in values if v]
    if not values:
        return False

    low, high = bounds.get('min'), bounds.get('max')
    return any(
        (low is None or v >= low) and (high is None or v <= high)
        for v in values
    )


def _query_family_matches(group, families):
    '''
    A construction filter passes on the group's own family, or on the label the
    card shows.

    Both, because QUALITY_LABELS folds the jersey-based structures together for
    display: an OTTM_SJY group has family 'ottoman' but reads "Single Jersey" on
    the card. Matching only the family would hide a card from the very words
    printed on it.

    Several constructions in one query are alternatives -- "rib jersey" is
    either of them, the way a multi-select filter behaves. Nothing is both, so
    ANDing them would only ever return nothing.
    '''
    family = group.get('construction_family')
    label = group.get('quality_label')
    for wanted in families:
        if family == wanted:
            return True
        if label and QUALITY_LABELS.get(wanted) == label:
            return True
    return False


def _promote_matching_variant(group, terms):
    '''
    Lead the card with the colourway a leftover word named, when it named one.

    A fab code is a group of colourways and its hero is just "the first lot with
    a photograph" (see _hero_lot), so a group that survives a search for "navy"
    because one of its lots is navy would still show whichever colour happened
    to be photographed first. This is the same swap `_annotate_colors` makes for
    a palette match, driven by the typed word instead.

    Only fires on an actual colour hit, so a word that is not a colour changes
    nothing. `matched_color` records that it happened, and stays None otherwise.
    '''
    for variant in group.get('colors') or []:
        colour = (variant.get('color') or '').lower()
        if not colour or not any(term in colour for term in terms):
            continue
        group['batch'] = variant['batch']
        group['hero_color'] = variant['color']
        group['hero_shade_category'] = variant['shade_category']
        group['image_url'] = variant['image']
        group['thumbnail'] = variant['thumbnail']
        group['matched_color'] = variant['color']
        return group
    return group


def _query_summary(query):
    '''
    The parsed query, shaped for the caller to render.

    `applied` is the readable version -- the chips a filter box puts above the
    results -- and the parsed facets sit beside it for anything that wants to
    edit rather than display. Echoing this is not decoration: a query that
    quietly read a word as a filter, or quietly read nothing at all, is
    otherwise indistinguishable from stock that does not exist.
    '''
    if not query:
        return None

    # Ordered as the fabric is described rather than as it was parsed --
    # construction, then what it is made of, then its numbers, then the words
    # nothing claimed. "Single Jersey - Cotton - 165-195 GSM - "navy"".
    applied = []
    for family in query['families']:
        applied.append(QUALITY_LABELS.get(family) or family.replace('_', ' ').title())
    for fibre, required in query['fibres'].items():
        applied.append(f'{_pct(required)}% {fibre.title()}' if required else fibre.title())
    for tag in query['textures']:
        applied.append(tag.title())
    for key, bounds in query['ranges'].items():
        applied.append(_range_label(key, bounds))
    applied.extend(f'"{term}"' for term in query['terms'])

    return {
        'raw': query['raw'],
        'applied': applied,
        'ranges': query['ranges'],
        'families': query['families'],
        'fibres': query['fibres'],
        'textures': query['textures'],
        'terms': query['terms'],
        'unsupported': query['unsupported'],
    }


def _range_label(key, bounds):
    '''
    {min: 165, max: 195} on gsm -> "165-195 GSM".

    Contradictory bounds are labelled as contradictory rather than printed
    backwards: intersecting "gsm 180" with "gsm 220" leaves min above max, which
    correctly matches nothing, but a chip reading "205-195 GSM" beside an empty
    shelf looks like missing stock instead of an impossible request.
    '''
    unit = QUERY_METRICS[key]['unit_label']
    low, high = bounds.get('min'), bounds.get('max')
    if low is not None and high is not None:
        if low > high:
            return f'{unit}: nothing can be both {_pct(low)} and {_pct(high)}'
        return f'{_pct(low)} {unit}' if low == high else f'{_pct(low)}-{_pct(high)} {unit}'
    if low is not None:
        return f'>= {_pct(low)} {unit}'
    return f'<= {_pct(high)} {unit}'


def _build_group(fab_code, lots):
    ''' One fab code's stock rows -> the recommendable group object. '''
    qty_total = sum(flt(l.get('total_qty')) for l in lots)
    # `total_value_rs_lakh` is named for lakh but carries plain rupees -- it
    # matches the retired `total_value` column to the paisa on every row, and
    # the rate it yields (~463/KG) only reads as a fabric price in rupees. No
    # conversion, or `price` would be off by 100000.
    value_total = sum(flt(l.get('total_value_rs_lakh')) for l in lots)

    # Per-lot GSM and colour, falling back to what `material_desc` encodes when
    # the dedicated columns are empty (see _decode_material_desc).
    for lot in lots:
        derived = _decode_material_desc(lot)
        lot['_gsm'] = cint(lot.get('gsm')) or derived['gsm']
        lot['_color'] = _clean(lot.get('color')) or derived['color']
        lot['_color_source'] = 'field' if _clean(lot.get('color')) else (
            'material_desc' if derived['color'] else None
        )

    quality = _dominant(lots, 'quality', qty_key='total_qty')
    blend = _dominant(lots, 'blend', qty_key='total_qty')
    gsm = _dominant(lots, '_gsm', qty_key='total_qty')

    composition = _parse_stock_blend(blend)
    structure_label = _structure_label(quality)
    construction = _classify_construction(structure_label, quality)

    variants = _color_variants(lots)
    hero = _hero_lot(lots)
    closest_master, master_cost = _closest_fabric_master(lots)

    return {
        'fab_code': fab_code,

        # --- the fields the brief asked for ---
        'composition': blend,
        'composition_label': _composition_label(composition),
        'gsm': gsm,
        'available': round(qty_total, 2),
        'available_uom': _dominant(lots, 'base_uom') or 'KG',
        'price': round(value_total, 2) if value_total else None,
        # What the same cloth would cost to MAKE, per kg, from the Fabric Master
        # this stock was matched to -- yarn + knitting + dyes & chemicals + M&C
        # finish + finishing charges, loss % included. The figure
        # costing.get_garment_cost rolls into a garment.
        #
        # Reads against `price_per_uom`, not `price`: `price` is what this whole
        # group of stock is valued at in rupees, while both of these are rates
        # per kg. The gap between them is the point -- surplus below its own
        # make-cost is the case for buying it.
        #
        # Precomputed on the stock rows by fabric_matcher, because costing a
        # fabric cold makes LLM calls; None until a match run has costed it.
        'price_from_fabric_masters': master_cost,
        'color': ', '.join(v['color'] for v in variants if v['color']) or None,

        # --- supporting detail ---
        'colors': variants,
        'price_per_uom': round(value_total / qty_total, 2) if value_total and qty_total else None,
        # Which Fabric Master `price_from_fabric_masters` was costed from, so a
        # caller can follow the number back rather than take it on trust.
        'closest_fabric_master': closest_master,
        'quality': quality,
        'quality_label': _quality_label(construction['family'], structure_label),
        'construction_family': construction['family'],
        'texture_tags': sorted(construction['tags']),
        'structure_ratio': construction['ratio'],
        'composition_pct': composition,
        'material_type': _dominant(lots, 'material_type_desc'),
        'fabric_types': _distinct(lots, 'fabric_type'),
        'blends': _distinct(lots, 'blend'),
        'shade_categories': _distinct(lots, 'shade_catagory'),
        'widths': sorted({cint(l.get('width')) for l in lots if cint(l.get('width'))}),
        'dia': _dominant(lots, 'dia') or None,
        'gauge': _dominant(lots, 'gauge') or None,
        'max_ageing': max((cint(l.get('ageing')) for l in lots), default=0) or None,
        'customers': _distinct(lots, 'customer_name'),
        'lot_count': len(lots),
        'batch_count': len({l['batch'] for l in lots if l.get('batch')}),

        # --- the hero batch: what the group card actually shows ---
        # One batch stands for the group (see _hero_lot), and the card shows ITS
        # colour and ITS image -- not the group-wide colour list above, which is
        # every colourway the fab code comes in and belongs on the swatch row.
        #
        # The image is read from the keys surplus_stock.update_image_urls()
        # recorded off S3, so a URL here is a URL that resolves, never a guess
        # from the batch number. `batch` is set whenever the group has stock;
        # `image_url` is null when that batch has no photograph, so the two must
        # be read independently -- a batch does not imply an image.
        'batch': hero.get('batch') if hero else None,
        'hero_color': hero.get('_color') if hero else None,
        'hero_shade_category': hero.get('shade_catagory') if hero else None,
        'image_url': _image_url(hero, 'image'),
        'thumbnail': _image_url(hero, 'thumbnail'),
    }



def _closest_fabric_master(lots):
    '''
    A fab code's matched Fabric Master and its stored per-kg cost.

    The match is made per stock row, but a fab code is quoted as one group, so
    the quantity-dominant master stands for it -- the same rule quality, blend
    and GSM already follow. In practice this only arbitrates one fab code out of
    124; the rest agree across every lot.

    The cost is read from a lot pointing at THAT master, never merely the first
    lot with a cost, or a disagreeing group would report one fabric's identity
    beside another's price.
    '''
    master = _dominant(lots, 'closest_fabric_master', qty_key='total_qty')
    if not master:
        return None, None

    for lot in lots:
        if lot.get('closest_fabric_master') != master:
            continue
        cost = flt(lot.get('closest_fabric_cost_per_kg'))
        if cost:
            return master, round(cost, 2)

    return master, None


def _resolve_group(fabric, fab_code):
    '''
    The group score_fabric() scores -> (group, source, error).

    Either side of the same object: `fabric` is one the caller already holds and
    hands back, `fab_code` is one this loads and folds itself. A bare fab-code
    string passed as `fabric` is read as the code, since that is plainly what a
    caller who sent it meant.
    '''
    if isinstance(fabric, str) and not fabric.strip().startswith('{'):
        fabric, fab_code = None, fabric

    payload = _as_dict(fabric) if fabric else {}
    if isinstance(payload, dict) and payload:
        group = _group_from_payload(payload)
        if not (group['quality'] or group['composition']):
            return None, None, ('fabric needs at least a "quality" or a '
                                '"composition"/"blend" to be scorable!')
        return group, 'payload', None

    code = _clean(fab_code)
    if not code:
        return None, None, 'pass either a "fabric" object or a "fab_code" to score!'

    lots = frappe.get_all(
        SURPLUS_STOCK_ITEM_DOCTYPE,
        filters={'fab_code': code},
        fields=_GROUP_FIELDS,
        limit_page_length=0,
    )
    if not lots:
        return None, None, f'No surplus stock found for fab code {code}'

    return _build_group(code, lots), 'stock', None


def _group_from_payload(payload):
    '''
    A fabric supplied in the request body -> the group object _build_group()
    would have produced for it.

    A caller passing a fabric back from catalogue() already holds
    `construction_family`, `composition_pct`, `texture_tags` and the rest, and it
    is tempting to trust them. They are not trusted: they ARE the score, so
    reading them back would let a hand-edited payload set its own result and
    would drift silently the day the derivation changes. Only the raw mill
    fields are read, and everything the scorer touches is recomputed here by the
    same functions the stock path uses.

    Everything else the payload carries -- images, price, widths, customers --
    is passed through untouched, so the caller gets its own object back, scored.
    '''
    payload = _as_dict(payload)

    blend = (_clean(payload.get('composition'))
             or _clean(payload.get('blend'))
             or next((_clean(b) for b in _as_list(payload.get('blends')) if _clean(b)), None))
    # `quality` is the mill code ("SJY"); `quality_label` is the retail reading
    # of it ("Single Jersey"). Either classifies, so a leaner caller that only
    # kept the label still scores.
    quality = _clean(payload.get('quality')) or _clean(payload.get('quality_label'))

    composition = _parse_stock_blend(blend)
    structure_label = _structure_label(quality)
    construction = _classify_construction(structure_label, quality)
    variants = _payload_variants(payload)

    group = dict(payload)
    # Anything a previous scoring run stamped on this object is stale the moment
    # it comes back in: it was scored against a different brief.
    for stale in ('fit_score', 'score_breakdown', 'matched_brief_index',
                  'matched_brief_fabric', 'match_score', 'match_source',
                  'reason', 'caveats', '_by_brief'):
        group.pop(stale, None)

    group.update({
        'fab_code': _clean(payload.get('fab_code')) or None,
        'composition': blend,
        'composition_label': _composition_label(composition),
        'composition_pct': composition,
        'gsm': cint(_first_int(payload.get('gsm'))) or None,
        'available': round(flt(payload.get('available')), 2),
        'available_uom': _clean(payload.get('available_uom')) or 'KG',
        'quality': quality,
        'quality_label': _quality_label(construction['family'], structure_label),
        'construction_family': construction['family'],
        'texture_tags': sorted(construction['tags']),
        'structure_ratio': construction['ratio'],
        'colors': variants,
        'color': ', '.join(v['color'] for v in variants if v['color']) or None,
    })
    return group


def _payload_variants(payload):
    '''
    The colourways a supplied fabric carries, in the shape _color_variants()
    builds them -- which is the shape _annotate_colors() reads.

    `colors[]` is the real input and the one catalogue() hands back. The
    comma-joined `color` string is a fallback for a leaner caller, and only that:
    stock colour names contain commas of their own ("BLACK BEAUTY ,19-3911 TCX"),
    so splitting on them recovers the list approximately, never exactly.
    '''
    variants = []
    for entry in _as_list(payload.get('colors')):
        if not isinstance(entry, dict):
            entry = {'color': entry}
        color = _clean(entry.get('color'))
        image = _clean(entry.get('image')) or _clean(entry.get('image_url'))
        thumbnail = _clean(entry.get('thumbnail'))
        variants.append({
            'color': color or None,
            'color_source': _clean(entry.get('color_source')) or ('field' if color else None),
            'shade_category': _clean(entry.get('shade_category')) or None,
            'available': round(flt(entry.get('available')), 2),
            'lot_count': cint(entry.get('lot_count')),
            'batch': _clean(entry.get('batch')) or None,
            'has_image': _as_bool(entry.get('has_image')) or bool(image or thumbnail),
            'image': image,
            'thumbnail': thumbnail,
            'image_url': thumbnail or image,
        })

    if variants:
        return variants

    names = [_clean(n) for n in str(payload.get('color') or '').split(',')]
    names = [n for n in names if n] or [_clean(payload.get('hero_color'))]
    return [
        {
            'color': name or None,
            'color_source': 'field' if name else None,
            # A shade only belongs to a named colour when there is exactly one:
            # with several, `hero_shade_category` describes one of them and
            # attaching it to all would invent undyed stock (or hide it).
            'shade_category': (_clean(payload.get('hero_shade_category'))
                               if len(names) == 1 else None),
            'available': round(flt(payload.get('available')), 2) if len(names) == 1 else 0.0,
            'lot_count': 0,
            'batch': _clean(payload.get('batch')) or None,
            'has_image': bool(_clean(payload.get('image_url'))),
            'image': _clean(payload.get('image_url')),
            'thumbnail': _clean(payload.get('thumbnail')),
            'image_url': _clean(payload.get('thumbnail')) or _clean(payload.get('image_url')),
        }
        for name in names
    ]


def _color_variants(lots):
    '''
    The colourways a fab code is available in, deepest stock first. Each carries
    its own batch and image so a UI can render the group as a swatch row -- and
    so a colourway picked to serve a brief's palette can become the group's hero
    without going back to the lots.

    Which lot represents a colourway: the first one, upgraded to the first lot
    that actually has an image if the first has none. So "the one with a picture"
    when only some are photographed, and simply the first when all or none are.

    `image` and `thumbnail` are the lot's two sizes; `image_url` keeps its older
    meaning -- the swatch-sized URL, thumbnail preferred -- because the colour
    row renders small.
    '''
    by_color = {}
    for lot in lots:
        key = _color_key(lot.get('_color')) or f"__{lot.get('shade_catagory') or 'UNSPECIFIED'}"
        variant = by_color.setdefault(key, {
            'color': lot.get('_color'),
            'color_source': lot.get('_color_source'),
            'shade_category': lot.get('shade_catagory'),
            'available': 0.0,
            'lot_count': 0,
            'batch': None,
            'has_image': False,
            'image': None,
            'thumbnail': None,
            'image_url': None,
        })
        variant['available'] += flt(lot.get('total_qty'))
        variant['lot_count'] += 1
        # first batch seen wins, unless a later one is the first with an image
        if variant['batch'] is None or (_lot_images(lot) and not variant['has_image']):
            variant['batch'] = lot.get('batch')
            variant['has_image'] = bool(_lot_images(lot))
            variant['image'] = _image_url(lot, 'image')
            variant['thumbnail'] = _image_url(lot, 'thumbnail')
            variant['image_url'] = variant['thumbnail'] or variant['image']

    variants = list(by_color.values())
    for variant in variants:
        variant['available'] = round(variant['available'], 2)
    variants.sort(key=lambda v: v['available'], reverse=True)
    return variants


def _color_key(value):
    ''' The form a stock colour is matched on: trimmed, upper-cased, inner
        whitespace collapsed. Stock writes the same colour many ways, and the
        column is truncated on top of that ("BLAC" for "BLACK"), so this only
        normalises punctuation-free casing -- resolving the rest is the LLM's
        job (see _map_stock_colors). '''
    text = _clean(value)
    return re.sub(r'\s+', ' ', text).upper() if text else None


def _hero_lot(lots):
    '''
    The group's default hero: the lot whose batch, colour and image the card
    shows. First lot that has an image; if none of them do, simply the first lot.

    So a group ALWAYS names a hero batch when it has any stock at all -- the card
    still has a batch and a colour to show when the fab code is unphotographed,
    it just has no image. (`image_url` stays null in that case: it is read from
    the recorded image keys, never guessed from the batch number.)

    Deliberately simple, and a placeholder: the real rule -- deepest stock, best
    shade coverage, most recent lot -- is still to be decided. Ties are broken by
    the order _fab_code_groups fetched the lots in, which is not a meaningful
    ranking; whatever replaces this should impose its own order.
    '''
    lots = [lot for lot in lots if lot.get('batch')]
    if not lots:
        return None
    return next((lot for lot in lots if _lot_images(lot)), lots[0])


def _lot_images(lot):
    ''' The {image, thumbnail} object keys update_image_urls() stored on a lot. '''
    if not lot:
        return {}
    value = lot.get('image_urls')
    if not isinstance(value, dict):
        value = _as_dict(value)
    return value if isinstance(value, dict) else {}


def _image_url(lot, kind):
    ''' Public URL for a lot's stored `image` / `thumbnail` key, else None. '''
    return cloud.public_asset_url(_lot_images(lot).get(kind))


def _decode_material_desc(lot):
    '''
    Recovers GSM and colour from `material_desc`, which the mill writes as

        <fabric type> <quality> <blend> [CC] [COMPACT] <gsm> <colour>

    e.g. "SPD RIB_2X2 100 BCI CC COMPACT 300 Veiled Rose".

    The row's own `fabric_type` / `quality` / `blend` columns are used to strip
    the prefix, so the split needs no grammar guessing. `material_desc` is stored
    truncated to 40 characters, so on long descriptions the GSM (or the colour)
    is simply cut off -- that reads back as None rather than as a wrong value.

    This is a fallback only: the `gsm` and `color` columns win whenever set.
    '''
    desc = _clean(lot.get('material_desc'))
    if not desc:
        return {'gsm': 0, 'color': None}

    tokens = desc.split()
    prefix = [
        t for part in (lot.get('fabric_type'), lot.get('quality'), lot.get('blend'))
        for t in str(part or '').split()
    ]

    index = 0
    if prefix and [t.upper() for t in tokens[:len(prefix)]] == [t.upper() for t in prefix]:
        index = len(prefix)
    else:
        # Prefix drifted from the columns -- fall back to scanning past the last
        # yarn-process annotation, which always sits directly before the GSM.
        last = max(
            (i for i, t in enumerate(tokens) if t.upper() in YARN_ANNOTATIONS),
            default=None,
        )
        if last is None:
            return {'gsm': 0, 'color': None}
        index = last + 1

    while index < len(tokens) and tokens[index].upper() in YARN_ANNOTATIONS:
        index += 1

    if index >= len(tokens) or not tokens[index].isdigit():
        return {'gsm': 0, 'color': None}

    gsm = int(tokens[index])
    if not (PLAUSIBLE_GSM[0] <= gsm <= PLAUSIBLE_GSM[1]):
        gsm = 0

    color = ' '.join(tokens[index + 1:]).strip(' -')
    return {'gsm': gsm, 'color': color or None}


# --- normalisation ---
def _brief_fabrics(signal):
    '''
    The input signal -> the fabric lines to match on, each normalised into the
    construction/composition/GSM space the stock side is normalised into.
    '''
    signal = _as_dict(signal)
    raw = signal.get('fabrics') if isinstance(signal, dict) else signal
    if not isinstance(raw, list):
        return []

    fabrics = []
    for index, entry in enumerate(raw):
        if not isinstance(entry, dict):
            continue

        quality = _clean(entry.get('quality'))
        blend = _clean(entry.get('blend'))
        if not (quality or blend):
            continue

        construction = _classify_construction(quality or '', quality or '')
        composition = _parse_brief_blend(blend)

        fabrics.append({
            'index': index,
            'label': ' / '.join(p for p in (quality, blend, _gsm_label(entry.get('gsm'))) if p),
            'quality': quality,
            'blend': blend,
            'gsm': cint(_first_int(entry.get('gsm'))),
            'best_for': _clean(entry.get('best_for')),
            'reason': _clean(entry.get('reason')),
            'family': construction['family'],
            'tags': construction['tags'],
            'ratio': construction['ratio'],
            'composition': composition,
        })

    return fabrics


def _brief_colors(colors):
    '''
    The colour palette the range is built on -> the colours to match stock
    against. Entries look like
        {"id": "col_294", "name": "Ocean Blue", "hex": "#4A90C4",
         "pantone": "17-4139", "selected": true}

    Only selected colours count; `selected` missing is read as selected, so a
    caller that does not carry the flag still works. An entry needs at least one
    of name / hex / pantone to be matchable.
    '''
    palette = []
    for position, entry in enumerate(_as_list(colors)):
        if not isinstance(entry, dict):
            continue
        if entry.get('selected') is False:
            continue

        name = _clean(entry.get('name'))
        hex_code = _hex(entry.get('hex'))
        pantone = _clean(entry.get('pantone'))
        if not (name or hex_code or pantone):
            continue

        palette.append({
            'index': len(palette),
            'id': _clean(entry.get('id')) or f'color_{position}',
            'name': name,
            'hex': hex_code,
            'pantone': pantone,
            'label': name or pantone or hex_code,
        })
    return palette


def _hex(value):
    ''' '#RRGGBB' upper-case, or None. A 3-digit shorthand is expanded so the
        LLM and any downstream distance maths see one format. '''
    text = _clean(value)
    if not text:
        return None
    body = text[1:] if text.startswith('#') else text
    if not all(c in '0123456789abcdefABCDEF' for c in body):
        return None
    if len(body) == 3:
        body = ''.join(c * 2 for c in body)
    return '#' + body.upper() if len(body) == 6 else None


# --- palette -> stock colour matching ---
def _map_stock_colors(palette, vocabulary):
    '''
    Map every stock colour string onto the palette colour it serves, or to None.

    This has to be an LLM call. Stock colour is free text typed by a mill and
    then truncated by the column: "BLACK", "BLAC", "JET BLACK" and "Rich Black"
    are one colour in four spellings, "ECRU M" and "WHT-WHI" are cut off
    mid-word, and "803", "RFD" and "B" are not colours at all. Pantone codes
    would have made this deterministic, but only ~1% of stock colours carry one,
    so there is nothing reliable to key on.

    One call, and deliberately shaped to keep it short: the answer is one row per
    PALETTE colour listing the stock names that serve it, not one row per stock
    name. Asked the other way round the model has to echo all ~150 stock strings
    back before it is done, and output length is what the caller waits on -- that
    version took ~39s, this one is a fraction of it, on the same input.

    Cached on (palette, vocabulary) so re-running a brief does not pay again.

    Returns {stock colour key -> palette id}. Anything the LLM cannot place is
    simply absent. Falls back to conservative string matching when the LLM is
    unavailable, so colour matching degrades instead of disappearing.
    '''
    vocabulary = _matchable_colors(vocabulary)
    if not palette or not vocabulary:
        return {}

    cache_key = 'surplus_color_map:' + _digest(palette, vocabulary)
    try:
        cached = frappe.cache().get_value(cache_key)
        if isinstance(cached, dict):
            return cached
    except Exception:
        pass

    mapping = _llm_color_map(palette, vocabulary) or _fallback_color_map(palette, vocabulary)

    try:
        frappe.cache().set_value(cache_key, mapping, expires_in_sec=COLOR_MAP_CACHE_TTL)
    except Exception:
        pass
    return mapping


def _matchable_colors(vocabulary):
    '''
    The stock colour strings worth showing an LLM, sorted.

    Stock carries plenty of values that cannot be a colour under any reading --
    internal codes ("803", "823-NO"), single stray letters ("B", "CA"), process
    states ("RFD"). They are recognisable without a model, and every one of them
    left in is prompt the caller waits on for a guaranteed null.
    '''
    keep = []
    for value in {v for v in vocabulary if v}:
        if len(value) < 3 or value in NON_COLOR_VALUES:
            continue
        if not re.search(r'[A-Z]{3}', value):   # no word in it, just digits/codes
            continue
        keep.append(value)
    return sorted(keep)


def _digest(palette, vocabulary):
    raw = json.dumps(
        [[c['id'], c['name'], c['hex'], c['pantone']] for c in palette] + vocabulary,
        sort_keys=True,
    )
    return hashlib.sha1(raw.encode('utf-8')).hexdigest()


def _llm_color_map(palette, vocabulary):
    ''' The LLM half of _map_stock_colors. Returns {} on any failure. '''
    try:
        palette_lines = '\n'.join(
            f'[{c["id"]}] {c["name"] or "(unnamed)"}'
            + (f' | hex {c["hex"]}' if c['hex'] else '')
            + (f' | pantone {c["pantone"]}' if c['pantone'] else '')
            for c in palette
        )
        valid_ids = ', '.join(c['id'] for c in palette)

        system_prompt = f'''You are matching a fashion brand's colour palette against the colour names recorded on surplus fabric stock, so the brand can be shown fabric it already holds in (or closest to) the colours its range calls for.

# THE PALETTE
---
{palette_lines}
---

# THE STOCK COLOUR NAMES
These are typed by the mill and are messy. Expect:
- the same colour spelled several ways ("BLACK", "JET BLACK", "Rich Black")
- names truncated mid-word by the database column ("BLAC" = black, "ECRU M" = ecru melange, "WHT-WHI" = white, "CLOUD" = cloud dancer, "VEILE" = veiled rose)
- values that are not colours at all: internal codes ("803", "823-NO"), process states ("RFD" = ready for dyeing), single stray letters ("B"), customer names
- embedded pantone codes ("CLOUD DANCER 11-4201 TCX", "WHITE 11-0601 TCX")
---
{chr(10).join(vocabulary)}
---

# YOUR TASK
For each palette colour, list the stock colour names that serve it.

Return a JSON array with exactly one entry per palette colour, in the order given:
[
    {{"PaletteId": "col_301", "StockColors": ["BLACK", "BLAC", "JET BLACK"]}},
    {{"PaletteId": "col_295", "StockColors": []}}
]

# RULES
- One entry per palette colour, PaletteId copied from: {valid_ids}.
- Every StockColors value MUST be copied verbatim (same spelling, same case) from the stock list above. Never invent, correct or complete a name.
- List a stock name under AT MOST ONE palette colour — the one it serves best.
- Judge by the colour a merchandiser would perceive: hue first, then depth. "NAVY BLAZER" serves a mid blue far better than a turquoise; an off-white does not serve a black.
- Resolve truncations to the most likely complete name ("BLAC" is black, "ECRU M" is ecru melange). If you are not reasonably sure what a truncated name is, LEAVE IT OUT — a fabric shown in the wrong colour is worse than one not shown.
- Leave out anything that is not a colour: internal codes, process states, stray letters, customer names. Most of the list will not be used, and that is correct.
- Do not force coverage. An empty StockColors is the right answer when we hold nothing in that colour.
- Output raw, valid JSON only: a single array, double-quoted keys/strings, no trailing commas, no markdown fences, no commentary.
'''

        user_prompt = ('For each palette colour, list the stock colour names that serve '
                       'it, following the format and rules above.')

        rows = llm.get_claude_response(system_prompt, user_prompt, 'list',
                                       max_tokens=COLOR_MAP_MAX_TOKENS)
        if not isinstance(rows, list):
            return {}

        valid = {c['id'] for c in palette}
        known = set(vocabulary)
        by_key = {}
        for row in rows:
            if not isinstance(row, dict):
                continue
            palette_id = _clean(row.get('PaletteId'))
            if palette_id not in valid:
                continue
            for stock in _as_list(row.get('StockColors')):
                key = _color_key(stock)
                # Only names that are actually in stock, and first claim wins --
                # the model was told to place each name once, and a hallucinated
                # or duplicated name must not quietly become a recommendation.
                if key in known and key not in by_key:
                    by_key[key] = palette_id
        return by_key

    except Exception:
        frappe.log_error(frappe.get_traceback(), 'surplus_recommender._llm_color_map()')
        return {}


def _fallback_color_map(palette, vocabulary):
    '''
    Conservative string matching for when the LLM is unavailable.

    Only exact and prefix matches on the palette's own words, which is enough for
    the truncation case ("BLAC" -> "Black") without inventing anything. Nothing
    fuzzy: a wrong colour is worse than a missing one.
    '''
    mapping = {}
    for stock in vocabulary:
        words = set(re.findall(r'[A-Z]+', stock.upper()))
        if not words:
            continue
        for color in palette:
            targets = set(re.findall(r'[A-Z]+', (color['name'] or '').upper()))
            if not targets:
                continue
            hit = any(
                word == target or target.startswith(word) or word.startswith(target)
                for word in words for target in targets
                if len(word) >= 3 and len(target) >= 3
            )
            if hit:
                mapping[stock] = color['id']
                break
    return mapping


def _annotate_colors(group, palette, color_map, images_only=False):
    '''
    Attach the palette view to one fab-code group, and hand the group's hero to
    the colourway that serves the brief.

    Sets:
      matched_colors  one entry per palette colour this fab code stocks, in
                      palette order, each naming the stock colourway and the
                      batch/image that colour is shown from.
      needs_dyeing    the fab code holds undyed stock (no colour recorded, or an
                      RFD shade), so it could be taken to any palette colour.
      batch / hero_color / hero_shade_category / image_url / thumbnail
                      re-pointed at the first matched colour, so the card opens
                      showing a colour the brand actually asked for rather than
                      whatever lot happened to be photographed first.

    A fab code with no matched colour keeps the default hero _hero_lot chose.

    `images_only` carries the caller's with_image_only flag down to the COLOUR,
    and it has to come this far to mean anything. The group-level filter keeps a
    fab code on the strength of any one of its lots being photographed, but the
    hero below is re-pointed at the colourway the palette matched -- so a fab
    code photographed only in ecru, matched to the palette on its navy lot,
    passes the filter and then returns a card with no picture at all. With this
    set, an unphotographed colourway cannot serve a palette colour and cannot
    count as dyeable, so colour and photograph are always the SAME batch and a
    group that survives always has an image to show.
    '''
    variants = group['colors']
    if images_only:
        variants = [v for v in variants if v['has_image']]

    best = {}
    for variant in variants:
        key = _color_key(variant['color'])
        palette_id = color_map.get(key) if key else None
        if not palette_id:
            continue
        # Several stock colourways can serve one palette colour ("BLACK" and
        # "JET BLACK"). Same rule as within a colourway: prefer the one with a
        # picture, otherwise keep the first (variants are deepest stock first).
        current = best.get(palette_id)
        if current is None or (variant['has_image'] and not current['has_image']):
            best[palette_id] = variant

    group['matched_colors'] = [
        {
            'palette_id': color['id'],
            'palette_name': color['name'],
            'palette_hex': color['hex'],
            'palette_pantone': color['pantone'],
            'color': best[color['id']]['color'],
            'shade_category': best[color['id']]['shade_category'],
            'batch': best[color['id']]['batch'],
            'available': best[color['id']]['available'],
            'lot_count': best[color['id']]['lot_count'],
            'has_image': best[color['id']]['has_image'],
            'image_url': best[color['id']]['image'],
            'thumbnail': best[color['id']]['thumbnail'],
        }
        for color in palette if color['id'] in best
    ]

    # Read off the same (possibly image-filtered) list: undyed stock is offered
    # as "we can dye this to your colour", and the card that offers it still has
    # to show the greige lot it would be dyed FROM. An unphotographed RFD lot is
    # no more showable than an unphotographed navy one.
    undyed = [
        variant for variant in variants
        if not variant['color']
        or (variant['shade_category'] or '').strip().upper() in UNDYED_SHADES
    ]
    group['needs_dyeing'] = bool(undyed)

    if group['matched_colors']:
        hero = group['matched_colors'][0]
        group['batch'] = hero['batch']
        group['hero_color'] = hero['color']
        group['hero_shade_category'] = hero['shade_category']
        group['hero_palette_id'] = hero['palette_id']
        group['image_url'] = hero['image_url']
        group['thumbnail'] = hero['thumbnail']
    else:
        group['hero_palette_id'] = None
        # No matched colour, so the hero stays whatever _hero_lot chose -- except
        # under images_only, where that lot may be one _hero_lot fell back to
        # unphotographed. The undyed variant this group is being offered on is
        # photographed by construction, so show that one instead.
        if images_only and undyed:
            hero = undyed[0]
            group['batch'] = hero['batch']
            group['hero_color'] = hero['color']
            group['hero_shade_category'] = hero['shade_category']
            group['image_url'] = hero['image']
            group['thumbnail'] = hero['thumbnail']

    return group


def _classify_construction(*texts):
    '''
    Any construction string -- mill shorthand ("RIB_1X1_EL", "VER_RIB_2X1X1X1")
    or retail English ("Pointelle", "Variegated Rib") -- into
    {family, tags, ratio}. Underscores are read as spaces so both forms hit the
    same keywords.
    '''
    joined = ' '.join(str(t or '') for t in texts).replace('_', ' ').lower()
    joined = re.sub(r'\s+', ' ', joined).strip()

    family = None
    for keyword, candidate in FAMILY_KEYWORDS:
        if re.search(rf'(?<![a-z]){re.escape(keyword)}(?![a-z])', joined):
            family = candidate
            break

    tags = {
        tag for tag, keywords in TEXTURE_TAGS.items()
        if any(k in joined for k in keywords)
    }

    ratio_match = _RATIO_RE.search(joined)
    ratio = f'{ratio_match.group(1)}x{ratio_match.group(2)}' if ratio_match else None

    return {'family': family, 'tags': tags, 'ratio': ratio}


def _parse_stock_blend(blend):
    '''
    Mill blend shorthand -> {fibre group: percent}.

    "95:5 BCI:EL"       -> {'cotton': 95.0, 'elastane': 5.0}
    "90:5:5 BCI:RC:EL"  -> {'cotton': 95.0, 'elastane': 5.0}   (RC is cotton too)
    "100 O"             -> {'cotton': 100.0}
    '''
    text = _clean(blend)
    if not text:
        return {}

    parts = [p for p in re.split(r'[\s:]+', text) if p]
    percents = [p for p in parts if re.fullmatch(r'\d+(?:\.\d+)?', p)]
    codes = [p for p in parts if not re.fullmatch(r'\d+(?:\.\d+)?', p)]

    composition = {}
    for percent, code in zip(percents, codes):
        group = FIBRE_GROUP_BY_CODE.get(code.upper())
        if not group:
            continue
        composition[group] = composition.get(group, 0.0) + float(percent)

    return _normalised(composition)


def _parse_brief_blend(blend):
    '''
    Retail blend text -> {fibre group: percent}.

    "95% Cotton/5% Spandex"  -> {'cotton': 95.0, 'elastane': 5.0}
    "60% Cotton/40% Polyester" -> {'cotton': 60.0, 'polyester': 40.0}
    "100% Organic Cotton"    -> {'cotton': 100.0}
    '''
    text = _clean(blend)
    if not text:
        return {}

    composition = {}
    for percent, name in _PCT_FIBRE_RE.findall(text):
        group = _fibre_group_from_words(name)
        if not group:
            continue
        composition[group] = composition.get(group, 0.0) + float(percent)

    return _normalised(composition)


def _fibre_group_from_words(text):
    ''' Longest known fibre name inside a free-text fragment -> its fibre group. '''
    cleaned = re.sub(r'[^a-z ]+', ' ', str(text or '').lower()).strip()
    if not cleaned:
        return None
    for word in sorted(FIBRE_GROUP_BY_WORD, key=len, reverse=True):
        if re.search(rf'(?<![a-z]){re.escape(word)}(?![a-z])', cleaned):
            return FIBRE_GROUP_BY_WORD[word]
    return None


def _normalised(composition):
    ''' Rescale a composition to 100 so blends that don't quite add up still compare. '''
    total = sum(composition.values())
    if total <= 0:
        return {}
    if abs(total - 100) < 0.01:
        return {k: round(v, 2) for k, v in composition.items()}
    return {k: round(v * 100.0 / total, 2) for k, v in composition.items()}


def _structure_label(quality):
    '''
    Mill quality code -> the structure it decodes to ("PNTL_RIB" -> "Pointelle
    Rib"), via the dyed-fabric code tables. Feeds construction classification,
    and is the display fallback for the constructions QUALITY_LABELS does not
    cover. Undecodable codes come back unchanged.
    '''
    text = str(quality or '').replace('_', ' ').strip().upper()
    if not text:
        return None

    tokens = text.split()
    code, _used = dyed_decoder._longest_match(tokens, 0, dyed_decoder.STRUCTURE_CODES, max_span=5)
    return dyed_decoder.STRUCTURE_CODES[code] if code else quality


def _quality_label(family, structure_label):
    '''
    The construction as a merchandiser reads it, drawn from QUALITY_LABELS.

    Gauge, elastane and modifiers are deliberately not written into this label --
    they are already carried by `structure_ratio`, `composition_pct` and
    `texture_tags`, and a brief says "Rib", not "Rib knit 1x1 (with elastane)".
    '''
    return QUALITY_LABELS.get(family) or structure_label


def _composition_label(composition):
    ''' {'cotton': 95.0, 'elastane': 5.0} -> "95% Cotton 5% Elastane". '''
    if not composition:
        return None
    ordered = sorted(composition.items(), key=lambda kv: kv[1], reverse=True)
    return ' '.join(f'{_pct(v)}% {k.title()}' for k, v in ordered)


# --- scoring ---
def _score(brief, group):
    '''
    One brief line against one fab-code group -> a 0-100 fit score plus the
    sub-scores it was built from, so a recommendation can always explain itself.
    '''
    construction = _construction_score(brief, group)
    composition = _composition_score(brief['composition'], group['composition_pct'])
    gsm = _gsm_score(brief['gsm'], group['gsm'])
    availability = _availability_score(group['available'])

    fit = (
        construction * W_CONSTRUCTION
        + composition * W_COMPOSITION
        + gsm * W_GSM
        + availability * W_AVAILABILITY
    )

    # Stretch is a functional requirement, not just a percentage: a 95/5 target
    # met by a 100% cotton rib is the right hand-feel and the wrong garment.
    brief_el = brief['composition'].get('elastane', 0.0)
    stock_el = group['composition_pct'].get('elastane', 0.0)
    stretch_penalty = 1.0
    if brief_el >= STRETCH_REQUIRED_PCT and stock_el <= 0:
        stretch_penalty = NO_STRETCH_PENALTY
    elif stock_el >= STRETCH_REQUIRED_PCT and brief_el <= 0:
        stretch_penalty = EXTRA_STRETCH_PENALTY
    fit *= stretch_penalty

    # Colour is a filter, not a weight -- the four components above stay exactly
    # as they were, and a group only reaches the scorer if it already passed the
    # palette. The one thing left to express is that stock which merely COULD be
    # dyed to a palette colour must sit below stock already in one.
    color_penalty = 1.0
    if group.get('needs_dyeing') and not group.get('matched_colors'):
        color_penalty = UNDYED_ONLY_PENALTY
    fit *= color_penalty

    return {
        'brief_index': brief['index'],
        'fit_score': round(fit, 2),
        'breakdown': {
            'construction': round(construction, 1),
            'composition': round(composition, 1),
            'gsm': round(gsm, 1),
            'availability': round(availability, 1),
            'stretch_penalty': stretch_penalty,
            'color_penalty': color_penalty,
        },
    }


def _construction_score(brief, group):
    ''' Family match, with partial credit for a plausible substitute knit. '''
    brief_family, stock_family = brief['family'], group['construction_family']
    if not brief_family or not stock_family:
        return 40.0  # unclassifiable on one side -- neither reward nor punish

    if brief_family == stock_family:
        score = 100.0
        # A brief that names a rib gauge ("2x1 Rib") means it; matching the
        # stock's own ratio is worth a little, mismatching it costs a little.
        if brief['ratio'] and group['structure_ratio']:
            score = 100.0 if brief['ratio'] == group['structure_ratio'] else 88.0
    else:
        score = 100.0 * RELATED_FAMILIES.get(frozenset((brief_family, stock_family)), 0.0)

    if brief['tags'] & set(group['texture_tags']):
        score = min(100.0, score + TEXTURE_TAG_BONUS)

    return score


def _composition_score(brief_composition, stock_composition):
    '''
    Overlap between two normalised compositions, as a percentage. Identical
    blends score 100; 95/5 cotton-elastane against 94/6 scores 99; against
    60/40 cotton-poly, 60.
    '''
    if not brief_composition or not stock_composition:
        return 40.0
    return sum(
        min(brief_composition.get(fibre, 0.0), stock_composition.get(fibre, 0.0))
        for fibre in set(brief_composition) | set(stock_composition)
    )


def _gsm_score(brief_gsm, stock_gsm):
    ''' Linear decay to 0 at GSM_TOLERANCE either side of the brief's weight. '''
    if not brief_gsm:
        return 60.0  # brief has no weight opinion -- weight can't disqualify
    if not stock_gsm:
        return float(GSM_UNKNOWN_SCORE)
    return 100.0 * max(0.0, 1.0 - abs(brief_gsm - stock_gsm) / float(GSM_TOLERANCE))



def _explain_score(brief, group):
    '''
    _score() plus the side-by-side it was computed from.

    The scorer answers "how well"; an endpoint whose whole job is to justify one
    number has to answer "against what" too, so each sub-score is returned next
    to the two values it compared and the weighted points it actually contributed
    (a 100 on GSM is worth 20 of the 100, and reading the sub-scores without that
    is how a caller talks itself into the wrong conclusion).

    The arithmetic stays _score()'s alone -- this only re-reads its inputs, so an
    explanation can never drift from the ranking it explains.
    '''
    scored = _score(brief, group)
    breakdown = scored['breakdown']

    return {
        'brief_index': brief['index'],
        'brief_fabric': brief['label'],
        'best_for': brief['best_for'],
        'fit_score': scored['fit_score'],
        'breakdown': breakdown,
        'contributions': {
            'construction': round(breakdown['construction'] * W_CONSTRUCTION, 2),
            'composition': round(breakdown['composition'] * W_COMPOSITION, 2),
            'gsm': round(breakdown['gsm'] * W_GSM, 2),
            'availability': round(breakdown['availability'] * W_AVAILABILITY, 2),
        },
        'comparison': {
            'construction': {
                'brief': brief['quality'],
                'stock': group.get('quality_label'),
                'brief_family': brief['family'],
                'stock_family': group.get('construction_family'),
                'family_match': _family_match(brief['family'], group.get('construction_family')),
                'brief_ratio': brief['ratio'],
                'stock_ratio': group.get('structure_ratio'),
                'shared_texture_tags': sorted(brief['tags'] & set(group.get('texture_tags') or [])),
            },
            'composition': {
                'brief': brief['blend'],
                'stock': group.get('composition'),
                'brief_pct': brief['composition'],
                'stock_pct': group.get('composition_pct'),
            },
            'gsm': {
                'brief': brief['gsm'] or None,
                'stock': group.get('gsm') or None,
                'delta': (abs(brief['gsm'] - cint(group.get('gsm')))
                          if brief['gsm'] and group.get('gsm') else None),
                'tolerance': GSM_TOLERANCE,
            },
            'availability': {
                'available': group.get('available'),
                'uom': group.get('available_uom'),
                'full_score_at': QTY_FULL_SCORE,
            },
        },
        'notes': _score_notes(brief, group, breakdown),
    }


def _family_match(brief_family, stock_family):
    ''' How the two constructions relate, as the score read them. '''
    if not brief_family or not stock_family:
        return 'unknown'
    if brief_family == stock_family:
        return 'exact'
    if RELATED_FAMILIES.get(frozenset((brief_family, stock_family))):
        return 'related'
    return 'unrelated'


def _score_notes(brief, group, breakdown):
    '''
    The sub-scores in plain English -- the sentences a merchandiser would write
    under the number, so a UI can show WHY without re-deriving the rules.

    Only what actually moved this score: a note per component that says something
    the number alone does not, and nothing for the components that behaved.
    '''
    notes = []

    match = _family_match(brief['family'], group.get('construction_family'))
    stock_construction = group.get('quality_label') or group.get('construction_family') or 'stock'
    if match == 'unknown':
        notes.append('Construction could not be classified on one side, so it scored neutral.')
    elif match == 'exact':
        if brief['ratio'] and group.get('structure_ratio') and brief['ratio'] != group['structure_ratio']:
            notes.append(f"Right construction, different gauge: the brief names "
                         f"{brief['ratio']}, the stock is {group['structure_ratio']}.")
        else:
            notes.append(f'Exact construction match on {stock_construction}.')
    elif match == 'related':
        notes.append(f'{stock_construction} is a partial substitute for the '
                     f'{brief["quality"] or brief["family"]} the brief asks for.')
    else:
        notes.append(f'{stock_construction} does not substitute for the '
                     f'{brief["quality"] or brief["family"]} the brief asks for.')

    if brief['composition'] and group.get('composition_pct'):
        notes.append(f'Composition overlaps {_pct(breakdown["composition"])}%: the brief '
                     f'asks for {brief["blend"]}, the stock is '
                     f'{group.get("composition_label") or group.get("composition")}.')

    if brief['gsm'] and group.get('gsm'):
        delta = abs(brief['gsm'] - cint(group['gsm']))
        if not delta:
            notes.append(f"GSM is exactly the brief's {brief['gsm']}.")
        elif breakdown['gsm'] <= 0:
            notes.append(f"GSM is {delta} off the brief's {brief['gsm']}, past the "
                         f'{GSM_TOLERANCE} tolerance, so weight scores nothing.')
        else:
            notes.append(f"GSM is {delta} off the brief's {brief['gsm']} "
                         f'(tolerance {GSM_TOLERANCE}).')
    elif brief['gsm'] and not group.get('gsm'):
        notes.append("Stock GSM is unknown, so weight cannot be checked against the brief's "
                     f"{brief['gsm']}.")

    if breakdown['stretch_penalty'] < 1.0:
        brief_el = brief['composition'].get('elastane', 0.0)
        if brief_el >= STRETCH_REQUIRED_PCT:
            notes.append(f'Scaled down {_cut(breakdown["stretch_penalty"])}%: the brief needs '
                         f'{_pct(brief_el)}% elastane and this stock has none.')
        else:
            notes.append(f'Scaled down {_cut(breakdown["stretch_penalty"])}%: the stock carries '
                         'elastane the brief never asked for.')

    if breakdown['color_penalty'] < 1.0:
        notes.append(f'Scaled down {_cut(breakdown["color_penalty"])}%: it holds none of the '
                     'palette colours and only qualifies as undyed stock.')

    return notes


def _serves_palette_color(group, color):
    ''' Whether a group is already stocked in one palette colour -- the default
        test _shortlist reserves its colour seats by. '''
    return any(m['palette_id'] == color['id']
               for m in group.get('matched_colors') or [])


def _shortlist(groups, brief_fabrics, palette, size, key='fab_code',
               serves=_serves_palette_color):
    '''
    The groups the LLM gets to see.

    Two things have to be true of this set, and a flat "global top N" gives
    neither:

      - Every brief line must arrive with its own best candidates. A range whose
        jersey is deeply stocked would otherwise fill the whole shortlist and
        the LLM would never see a rib or a pointelle option at all. So the bulk
        of the shortlist is taken round-robin, one pick per brief line per pass.

      - Whatever we hold in the exact construction a brief line names must be
        present even when weight or quantity sinks its overall score. Six kilos
        of 220 GSM pointelle rib is a poor answer to a 160 GSM pointelle brief,
        but it is the answer, and it has to be visible so the LLM reports it
        instead of concluding we hold no pointelle. Hence the reserved seats.

    `groups` must already be sorted by `fit_score` descending.

    `key` names the field that identifies one candidate -- 'fab_code' here --
    and `serves` decides whether a candidate already holds a given palette
    colour. Both are parameters only so a sibling recommender over another
    doctype can reuse this seat allocation with its own identity field and its
    own colour shape (a dyed fabric is one colour, so it answers at most one
    palette entry and has no `matched_colors` list to search).
    '''
    picked, seen = [], set()

    def take(group):
        if group[key] in seen:
            return
        picked.append(group)
        seen.add(group[key])

    #--- reserved seats: at least one option per palette colour ---
    # Taken before the construction seats: a colour with only one fab code
    # behind it loses every other contest, and if the LLM never sees that fab
    # code the colour cannot be covered at all.
    for color in palette:
        if len(picked) >= size:
            break
        serving = sorted(
            (g for g in groups if serves(g, color)),
            key=lambda g: g['fit_score'],
            reverse=True,
        )
        for group in serving[:PALETTE_COLOR_SEATS]:
            if len(picked) >= size:
                break
            take(group)

    #--- reserved seats: the exact construction each brief line asks for ---
    for fabric in brief_fabrics:
        if not fabric['family'] or len(picked) >= size:
            continue
        exact = sorted(
            (g for g in groups if g['construction_family'] == fabric['family']),
            key=lambda g: g['_by_brief'][fabric['index']],
            reverse=True,
        )
        for group in exact[:EXACT_FAMILY_SEATS]:
            if len(picked) >= size:
                break
            take(group)

    #--- the rest, round-robin across the brief lines ---
    ranked = {
        fabric['index']: sorted(
            groups, key=lambda g: g['_by_brief'][fabric['index']], reverse=True
        )
        for fabric in brief_fabrics
    }
    cursors = {index: 0 for index in ranked}

    while len(picked) < size:
        progressed = False
        for index, queue in ranked.items():
            cursor = cursors[index]
            while cursor < len(queue) and queue[cursor][key] in seen:
                cursor += 1
            cursors[index] = cursor + 1
            if cursor >= len(queue):
                continue

            take(queue[cursor])
            progressed = True
            if len(picked) >= size:
                break
        if not progressed:
            break

    picked.sort(key=lambda g: g['fit_score'], reverse=True)
    return picked


def _availability_score(available):
    '''
    Depth of stock, log-scaled so the first few hundred kg count for most of it.
    Only a tie-breaker (W_AVAILABILITY), never a reason to recommend.
    '''
    qty = flt(available)
    if qty <= 0:
        return 0.0
    return min(100.0, 100.0 * math.log1p(qty) / math.log1p(QTY_FULL_SCORE))


# --- LLM re-rank ---
def _llm_rerank(signal, brief_fabrics, palette, shortlist, max_items):
    '''
    Hands the shortlist to the LLM to pick and rank the final set, attribute each
    pick to the brief line it serves and write the reason. Returns [] on any
    failure, which leaves the deterministic ranking standing.
    '''
    try:
        catalogue_lines = '\n'.join(_shortlist_line(g, bool(palette)) for g in shortlist)
        brief_lines = '\n'.join(
            f'[{f["index"]}] {f["label"]}'
            + (f' | best for: {f["best_for"]}' if f['best_for'] else '')
            + (f' | brief rationale: {f["reason"]}' if f['reason'] else '')
            for f in brief_fabrics
        )
        valid_codes = ', '.join(g['fab_code'] for g in shortlist)

        # With a palette in play the shortlist carries a Serves column naming the
        # palette colours each fab code already sits in, and covering the palette
        # becomes part of the task. Without one, the prompt is unchanged.
        palette_block, palette_column, palette_rules = '', '', ''
        if palette:
            palette_lines = '\n'.join(
                f'[{c["id"]}] {c["label"]}' + (f' ({c["hex"]})' if c['hex'] else '')
                for c in palette
            )
            palette_block = f'''# THE RANGE'S COLOUR PALETTE
The range is built on these colours:
---
{palette_lines}
---
Every fab code below is either already stocked in at least one of them, or is undyed and could be taken to any of them.

'''
            palette_column = ' Serves <TAB>'
            palette_rules = (
                '\n- `Serves` lists the palette colours this fab code is ALREADY stocked in, '
                'as "palette colour = the mill\'s own colour name". "undyed" means the stock '
                'carries no colour yet and would have to be dyed.\n'
                '- Cover the palette: prefer a set that brings as many DIFFERENT palette colours '
                'as possible over several fab codes serving the same colour. A fabric that is the '
                'only one serving a colour is worth more than a marginally better second option '
                'for a colour already served.\n'
                '- Rank stock already in a palette colour above undyed stock for the same brief '
                'line. Undyed is a real option, but it is extra work and lead time.\n'
                '- When a pick is undyed, say so in Caveats.'
            )

        system_prompt = f'''You are a textile merchandiser choosing which surplus (already-woven, already-dyed) stock a brand should build a range from. You are given a design brief that specifies the fabrics the range wants, and a shortlist of surplus fabrics we actually hold. Your job is to pick the stock that lets the brand hit the brief without knitting anything new.

# THE BRIEF'S FABRICS
Each line is one fabric the brief calls for, with its index in brackets:
---
{brief_lines}
---

{palette_block}
# OUR SURPLUS SHORTLIST
One line per fab code — the unit a buyer commits against. Fields are tab-separated:
FabCode <TAB> Construction <TAB> Composition <TAB> GSM <TAB> Available <TAB> Colours <TAB>{palette_column} PreScore
`Colours` is a SAMPLE — the deepest few colourways only, not the full list. Never treat a colour's absence from it as evidence the fab code lacks that colour.
`PreScore` is our own 0-100 fit estimate against the closest brief line; treat it as a strong hint, not an instruction — you may overturn it when the brief's prose says otherwise.
Whatever we hold in the exact construction each brief line names is guaranteed to appear below, so if a construction is absent here we do not stock it — but say only that, and never assert anything else about what is or is not in our inventory beyond these lines.
---
{catalogue_lines}
---

# YOUR TASK
Return the {max_items} strongest fab codes for this brief, best first.

Return a JSON array using exactly this shape:
[
    {{
        "FabCode": "1600007259",
        "BriefFabricIndex": 0,
        "MatchScore": 88,
        "Reason": "ONE sentence, 25 words maximum: why this stock serves that brief line, in merchandiser language",
        "Caveats": ["concrete gap, 8 words maximum"]
    }}
]

# RULES
- Return at most {max_items} recommendations, sorted by MatchScore descending.
- FabCode MUST be copied verbatim from the shortlist above. Valid values: {valid_codes}. Drop any pick you cannot match to that list.
- Never return the same FabCode twice.
- BriefFabricIndex is the bracketed index of the brief fabric this stock is being recommended for — the line it actually serves, which need not be the one PreScore assumed.
- MatchScore is an integer 0-100 for how well this stock serves that brief line. Weigh construction first, then composition, then GSM; depth of stock and colour range break ties. Be honest — a substitute knit or a missing elastane content should not score in the 90s.
- Reason is ONE sentence of at most 25 words, citing the concrete attributes (construction, blend, GSM, quantity, colours). No marketing language, no invented facts, nothing not present in the shortlist line or the brief. Do not restate the fab code or repeat the caveats.
- Caveats lists real gaps (wrong weight, no stretch, limited colours, thin quantity) — at most 2, each at most 8 words, no sentences. Use an empty array when the stock genuinely matches.
- Prefer covering several different brief lines over stacking near-identical fab codes against one line, unless one line is clearly the whole range.{palette_rules}
- Output raw, valid JSON only: a single array, double-quoted keys/strings, no trailing commas, no markdown fences, no commentary.
'''

        description = _as_dict(signal).get('description') if isinstance(_as_dict(signal), dict) else None
        user_prompt = f'''Here is the brief's own rationale for its fabric choices:
---
{description or '(none supplied)'}
---

Pick the surplus stock that best delivers this brief, following the format and rules above.'''

        picks = llm.get_claude_response(system_prompt, user_prompt, 'list',
                                        max_tokens=RERANK_MAX_TOKENS)
        return [p for p in picks if isinstance(p, dict)] if isinstance(picks, list) else []

    except Exception:
        # A missing API key, a rate limit or a malformed reply must not cost the
        # caller its recommendations -- the deterministic ranking still stands.
        frappe.log_error(frappe.get_traceback(), 'surplus_recommender._llm_rerank()')
        return []


def _shortlist_line(group, with_palette=False):
    ''' One fab-code group -> the tab-separated line the LLM prompt carries. '''
    colors = ', '.join(
        v['color'] or v['shade_category'] or '?' for v in group['colors'][:6]
    ) or 'unspecified'

    # What this fab code already serves from the palette, in the LLM's own terms:
    # the palette colour it answers and the mill's name for the stock sitting in
    # it, so the reason it writes can cite a real colourway.
    serves = ''
    if with_palette:
        matched = ', '.join(
            f"{m['palette_name'] or m['palette_id']} = {m['color']}"
            for m in (group.get('matched_colors') or [])
        )
        serves = (matched or ('undyed' if group.get('needs_dyeing') else 'none')) + '\t'

    # `quality_label` is folded to the nine display constructions, which drops
    # gauge and elastane -- so the raw mill code rides along and the LLM can
    # still tell a 1x1 rib from a 2x2, or an ottoman jersey from a plain one.
    construction = group['quality_label'] or group['quality'] or '?'
    if group['quality'] and group['quality'] != construction:
        construction = f"{construction} ({group['quality']})"

    return '\t'.join([
        group['fab_code'],
        construction,
        group['composition_label'] or group['composition'] or '?',
        str(group['gsm'] or 'unknown'),
        f"{group['available']:.0f} {group['available_uom']}",
        colors,
        serves + str(int(group['fit_score'])),
    ])


# --- assembly ---
def _finalise(brief_fabrics, palette, shortlist, picks, max_items):
    '''
    Merges the LLM's picks over the deterministic ranking.

    The LLM decides order, score and rationale for what it picked; anything it
    dropped is still available to backfill, in deterministic order, so the caller
    always gets `max_items` recommendations when the stock exists to fill them.

    Backfill runs in three passes, most specific first: palette colours nothing
    serves yet, then brief lines nothing covers yet, then anything. The colour
    pass goes first because that is what "at least one of each colour" means --
    a second jersey for a line already served is worth less than the only fabric
    that comes in Soft Coral.
    '''
    by_code = {g['fab_code']: g for g in shortlist}
    by_index = {f['index']: f for f in brief_fabrics}

    recommendations, used = [], set()

    for pick in picks:
        group = by_code.get(str(pick.get('FabCode') or '').strip())
        if not group or group['fab_code'] in used:
            continue

        brief = by_index.get(cint(pick.get('BriefFabricIndex'))) or by_index.get(group['matched_brief_index'])
        recommendations.append(_recommendation(
            group,
            brief,
            match_score=cint(pick.get('MatchScore')) or int(group['fit_score']),
            reason=_clean(pick.get('Reason')),
            caveats=[c for c in (pick.get('Caveats') or []) if isinstance(c, str)],
            source='llm',
        ))
        used.add(group['fab_code'])
        if len(recommendations) >= max_items:
            return recommendations

    def palette_ids(item):
        return {m['palette_id'] for m in (item.get('matched_colors') or [])}

    covered = {r['matched_brief_index'] for r in recommendations}
    colors_covered = set().union(*(palette_ids(r) for r in recommendations)) \
        if recommendations else set()
    wanted_colors = {c['id'] for c in palette}

    # pass 1: fabrics that bring a palette colour nothing serves yet
    # pass 2: fabrics for a brief line nothing covers yet
    # pass 3: anything left, best score first
    for stage in ('color', 'brief', 'any'):
        for group in shortlist:
            if len(recommendations) >= max_items:
                return recommendations
            if group['fab_code'] in used:
                continue
            if stage == 'color':
                if not (palette_ids(group) & (wanted_colors - colors_covered)):
                    continue
            elif stage == 'brief' and group['matched_brief_index'] in covered:
                continue
            recommendations.append(_recommendation(
                group,
                by_index.get(group['matched_brief_index']),
                match_score=int(round(group['fit_score'])),
                reason=None,
                caveats=[],
                source='score',
            ))
            used.add(group['fab_code'])
            covered.add(group['matched_brief_index'])

    return recommendations


def _recommendation(group, brief, match_score, reason, caveats, source):
    ''' A scored group + the brief line it serves -> the returned object. '''
    recommendation = dict(group)
    recommendation.pop('matched_brief_index', None)
    recommendation.pop('_by_brief', None)

    recommendation['match_score'] = max(0, min(100, match_score))
    recommendation['match_source'] = source
    recommendation['reason'] = reason
    recommendation['caveats'] = caveats
    recommendation['matched_brief_fabric'] = brief['label'] if brief else None
    recommendation['matched_brief_index'] = brief['index'] if brief else None
    recommendation['best_for'] = brief['best_for'] if brief else None

    return recommendation


def _brief_coverage(brief_fabrics, recommendations):
    ''' Which brief lines the returned set actually covers, and with what. '''
    return [
        {
            'brief_index': fabric['index'],
            'brief_fabric': fabric['label'],
            'best_for': fabric['best_for'],
            'fab_codes': [
                r['fab_code'] for r in recommendations
                if r['matched_brief_index'] == fabric['index']
            ],
        }
        for fabric in brief_fabrics
    ]


def _color_coverage(palette, recommendations):
    '''
    Which palette colours the returned set actually serves, and with what.

    The point of the report is the colours that came back EMPTY: "we hold nothing
    in Soft Coral" is the answer a merchandiser needs to plan a dye lot, and it
    is invisible from the recommendations alone. Empty list when no palette was
    supplied.
    '''
    coverage = []
    for color in palette:
        serving = [
            {
                'fab_code': r['fab_code'],
                'color': next((m['color'] for m in r.get('matched_colors') or []
                               if m['palette_id'] == color['id']), None),
            }
            for r in recommendations
            if any(m['palette_id'] == color['id']
                   for m in r.get('matched_colors') or [])
        ]
        coverage.append({
            'palette_id': color['id'],
            'palette_name': color['name'],
            'palette_hex': color['hex'],
            'palette_pantone': color['pantone'],
            'covered': bool(serving),
            'fab_codes': [s['fab_code'] for s in serving],
            'stock_colors': [s['color'] for s in serving if s['color']],
        })
    return coverage


# --- helpers ---
def _dominant(lots, field, qty_key=None):
    '''
    The value of `field` backed by the most stock (or, without `qty_key`, by the
    most rows). Used for the attributes that are meant to be constant within a
    fab code but occasionally are not.
    '''
    weights = {}
    for lot in lots:
        value = lot.get(field)
        if value in (None, '', 0):
            continue
        weights[value] = weights.get(value, 0.0) + (
            flt(lot.get(qty_key)) if qty_key else 1.0
        )
    if not weights:
        return None
    return max(weights.items(), key=lambda kv: kv[1])[0]


def _distinct(lots, field):
    ''' Distinct non-empty values of `field`, in first-seen order. '''
    seen, values = set(), []
    for lot in lots:
        value = _clean(lot.get(field))
        if value and value not in seen:
            seen.add(value)
            values.append(value)
    return values


def _clean(value):
    text = str(value).strip() if value is not None else ''
    return text or None


def _pct(value):
    return int(value) if float(value).is_integer() else round(float(value), 1)


def _cut(multiplier):
    ''' A penalty multiplier as the percentage it takes off. Rounded before it
        is read, or 1 - 0.85 prints as 15.0% instead of 15%. '''
    return _pct(round((1.0 - float(multiplier)) * 100, 2))


def _first_int(value):
    match = re.search(r'\d+', str(value or ''))
    return int(match.group()) if match else 0


def _gsm_label(value):
    gsm = _first_int(value)
    return f'{gsm} GSM' if gsm else None


def _as_dict(value):
    ''' Frappe hands JSON body params through as strings when form-encoded. '''
    if isinstance(value, str):
        try:
            return json.loads(value)
        except Exception:
            return {}
    return value if isinstance(value, (dict, list)) else {}


def _sent(*values):
    '''
    The first of several spellings of one parameter that the caller actually
    sent, so a camelCase alias can sit beside its snake_case original without
    either one's default masking the other. An omitted param arrives as None and
    a form-encoded empty one as '', so both count as unsent.
    '''
    for value in values:
        if value is not None and value != '':
            return value
    return None


def _as_bool(value, default=False):
    '''
    Query-string flags arrive as text, and `bool('false')` is True -- so read the
    words a caller actually sends rather than the truthiness of the string.
    '''
    if value is None or value == '':
        return default
    if isinstance(value, str):
        return value.strip().lower() not in ('0', 'false', 'no', 'none', 'null')
    return bool(value)


def _as_list(value):
    value = _as_dict(value) if isinstance(value, str) else value
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _page(limit, offset):
    ''' Coerce request limit/offset to safe ints: limit in [1, _MAX_LIMIT], offset >= 0. '''
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        limit = _DEFAULT_LIMIT
    try:
        offset = int(offset)
    except (TypeError, ValueError):
        offset = 0
    limit = max(1, min(limit, _MAX_LIMIT))
    offset = max(0, offset)
    return limit, offset


def _sort(sort_by, sort_dir):
    ''' Coerce request sort_by/sort_dir to a supported pair, never throwing. '''
    sort_by = str(sort_by or '').strip().lower()
    if sort_by not in CATALOGUE_SORT_FIELDS:
        sort_by = _DEFAULT_SORT_BY
    sort_dir = 'asc' if str(sort_dir or '').strip().lower() == 'asc' else 'desc'
    return sort_by, sort_dir


def _sort_groups(groups, sort_by, sort_dir):
    '''
    Orders assembled fab-code groups in place.

    Three stable passes, lowest precedence first: fab code breaks ties so the
    order is identical between two requests for different pages of the same
    query; then the sorted value; then a null pass that pushes groups with no
    value to the end whichever direction was asked for -- an unpriced group is
    not "the cheapest", and a group whose GSM could not be decoded is not the
    lightest.
    '''
    field = CATALOGUE_SORT_FIELDS[sort_by]
    reverse = sort_dir == 'desc'

    groups.sort(key=lambda g: str(g.get('fab_code') or ''))
    if field == 'fab_code':
        if reverse:
            groups.reverse()
        return groups

    groups.sort(key=lambda g: flt(g.get(field)), reverse=reverse)
    groups.sort(key=lambda g: g.get(field) is None)
    return groups


def _page_envelope(items, total, limit, offset, **extra):
    return {'total': total, 'limit': limit, 'offset': offset, 'items': items, **extra}
