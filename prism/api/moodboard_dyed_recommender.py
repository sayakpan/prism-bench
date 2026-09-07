'''
Moodboard Dyed Fabric recommendation engine.

The sibling of `surplus_recommender`, pointed at the dyed-fabric CATALOGUE
instead of at surplus stock. Given the same design/sourcing brief ("input
signal"), it returns the handful of catalogue fabrics that best serve it, in the
same response shape, so a client that already renders surplus recommendations
renders these with a field rename and nothing else.

Everything that can be shared IS shared, by import rather than by copy: the
fibre tables, the construction keywords, the family-substitution matrix, the
texture tags, the brief parser, the palette matcher and the `q` query language
all live in `surplus_recommender` and are read from there. Two catalogues that
disagreed about what "Single Jersey" or "95% Cotton/5% Spandex" means would be
two engines, not one ported to a second table.

What genuinely differs, and only this:

  1. The unit. Surplus stock is recommended per FAB CODE -- one manufacturable
     quality whose rows are its dye lots. A dyed-fabric row is not a lot of
     anything; it is one catalogue entry, so there is no fab code and no
     grouping. `catalogue()` therefore lists one item per row.

  2. Depth of stock does not exist. There is no quantity, no valuation and no
     rate on this doctype, so the availability sub-score has nowhere to come
     from. Its 10% is redistributed across the three remaining components
     (see W_CONSTRUCTION / W_COMPOSITION / W_GSM) rather than faked, and
     `withImageOnly` takes over as the eligibility gate a merchandiser actually
     applies: a moodboard built from unphotographed fabric is a blank board.

  3. Which columns carry the construction and the composition. Surplus reads
     the mill's own `quality` / `blend`; here it is `clean_quality` falling back
     to `quality`, and `clean_blend` falling back to `blend` (see
     CONSTRUCTION_FIELDS -- and read the warning above it before trusting a
     ranking).

  4. `recommend()` de-duplicates. `catalogue()` lists rows, but 24,838 rows
     collapse to ~1,400 distinct specifications, so a flat top-6 would be six
     copies of one fabric. Candidates are folded by SCORING IDENTITY -- two rows
     that compare identically on construction, composition, colour and GSM are
     the same recommendation.

  5. Colour is a SCALAR, not a list, and there is no hero. Both of those are
     surplus's answer to a problem this doctype does not have: a fab code is
     stocked in many colours at once, so surplus must return every colourway
     and nominate one to put on the card. One dyed fabric is one colour. So
     `color` is that colour, `image_url` is its image, and a fabric answers at
     most one palette entry (`matched_palette_id`) rather than a list of them.

The colour matching itself is carried in full and is dormant by design: the
`color` column is empty on every row today, so nothing matches a palette,
everything reads as undyed and the palette filter removes nothing. The moment
colour is populated the same code starts matching.
'''

from functools import lru_cache

import frappe
from frappe.utils import cint, flt

from prism.auth.authenticator import auth_required
import prism.api.llm as llm
import prism.api.surplus_recommender as sr
import prism.lib.cloud as cloud

DOCTYPE = 'Moodboard Dyed Fabric'

# In preference order: the first of these with a value is the construction the
# fabric is scored on, and likewise the composition. This is the one place that
# decides it.
#
# WARNING -- `clean_quality` and `clean_blend` are not currently faithful. On
# prism.localhost (2026-08-25) 24,099 of 24,838 rows carry clean_quality
# "Single Jersey" -- including every RIB_1X1, FLBK_RIB, TTP_HBONE and
# 3T_DIA_FLC row -- where the raw `quality` code holds 231 distinct values; and
# 24,418 rows carry clean_blend "100% Cotton" where their own `blend` column
# says "95% Cotton 5% Spandex", "60% Cotton 40% Polyester" and so on. Scored on
# the clean columns most of the catalogue is therefore indistinguishable, and
# the ranking falls to GSM alone.
#
# Putting 'quality' / 'blend' first switches the whole module over to the mill
# columns, which the shared classifier reads natively -- they are what Surplus
# Stock feeds it. Left as specified until the clean columns are repopulated.
# The same warning, and the same switch, sit in moodboard_fabric_matcher.
CONSTRUCTION_FIELDS = ('clean_quality', 'quality')
BLEND_FIELDS = ('clean_blend', 'blend')

# The columns every loader reads, so scoring one fabric reads exactly what
# scoring the whole catalogue reads -- a column missing on one path would
# silently change the item it produces, and with it the score.
_ITEM_FIELDS = [
    'name', 'code', 'batch', 'description', 'ai_description',
    'custom_fabric_name', 'quality', 'clean_quality', 'blend', 'clean_blend',
    'gsm', 'shade', 'shade_category', 'finish', 'color', 'quality_sort_order',
    'has_image_file', 'closest_fabric_master', 'closest_fabric_cost_per_kg',
]

DEFAULT_MAX_ITEMS = sr.DEFAULT_MAX_ITEMS
MAX_ITEMS_CAP = sr.MAX_ITEMS_CAP

_DEFAULT_LIMIT = 20
_MAX_LIMIT = 100

# catalogue() sort keys -> the COLUMN they order on. Unlike surplus, every one
# of these is a real column, because nothing here is a sum over child rows -- so
# sorting, paging and counting all happen in SQL and only the page is built into
# item objects. That matters at this table's size: 24,838 rows against surplus's
# 580.
#
# `quality` orders on `quality_sort_order`, the merchandising order the fabric
# masters endpoints already sort by, not alphabetically on the code.
CATALOGUE_SORT_FIELDS = {
    'creation': 'creation',
    'code': 'code',
    'gsm': 'gsm',
    'quality': 'quality_sort_order',
    'make_cost': 'closest_fabric_cost_per_kg',
    'price_from_fabric_masters': 'closest_fabric_cost_per_kg',
}
_DEFAULT_SORT_BY = 'creation'
_DEFAULT_SORT_DIR = 'desc'

# catalogue() free-text search columns -- the literal-substring `search`
# contract, mirroring surplus's fab_code / batch / quality / blend.
CATALOGUE_SEARCH_FIELDS = ['code', 'batch', 'quality', 'blend']

# The columns `q`'s leftover words are matched against. Wider than the above,
# and for the same reason surplus's is: `quality` is mill shorthand ("SJY_EL"),
# so LIKE '%single jersey%' finds nothing in it. `clean_quality` /
# `clean_blend` carry the English a person types, `description` /
# `ai_description` / `custom_fabric_name` carry how the fabric was written up,
# and `finish` / `shade` / `shade_category` / `color` are where a finish or
# colour word lands.
QUERY_SEARCH_FIELDS = CATALOGUE_SEARCH_FIELDS + [
    'clean_quality', 'clean_blend', 'description', 'ai_description',
    'custom_fabric_name', 'finish', 'shade', 'shade_category', 'color',
]

# The numeric facets `q` understands here. Both are real columns, so both are
# pushed into SQL -- `column` is what they filter on.
#
# There is no width, dia, gauge, ageing, quantity or valuation on this doctype;
# every one of those is listed under QUERY_UNSUPPORTED instead, so a caller who
# types one is told rather than handed an empty shelf.
QUERY_METRICS = {
    'gsm': {
        'field': 'gsm', 'column': 'gsm', 'cast': int, 'unit_label': 'GSM',
        'keywords': ('gsm', 'weight', 'grams'), 'units': ('gsm',),
    },
    'make_cost': {
        'field': 'price_from_fabric_masters',
        'column': 'closest_fabric_cost_per_kg',
        'cast': float, 'unit_label': 'INR/KG',
        'keywords': ('make cost', 'making cost', 'mfg cost',
                     'manufacturing cost', 'fabric cost', 'cost'),
        'units': (),
    },
}

# Facets a caller may reasonably type that the dyed-fabric catalogue does not
# carry. Recognised only so the answer can SAY so: each is stripped from the
# query along with the comparison hanging off it, and echoed back under
# `unsupported`.
#
# `price` is the one worth reading twice. A catalogue entry is a specification,
# not a lot of cloth, so it has no valuation -- the only rate on it is
# `price_from_fabric_masters`, what the cloth costs to MAKE per kg. Answering
# "price < 500" from that would put a manufacturing cost behind a purchase-price
# filter, so `q` refuses and names the facet that does work ("make cost < 500").
QUERY_UNSUPPORTED = {
    'fob': 'FOB is a per-piece garment price and is not held on the dyed fabric '
           'catalogue. The only rate here is make cost (INR per KG).',
    'usd': 'The dyed fabric catalogue is costed in INR. Filter on make cost '
           '(INR per KG) instead.',
    'moq': 'A dyed fabric catalogue entry is a specification, not a stock lot, '
           'so it carries no MOQ.',
    'lead time': 'The dyed fabric catalogue holds no lead times.',
    'price': 'These are catalogue specifications, not stock, so they carry no '
             'purchase price. Filter on make cost (INR per KG) instead.',
    'rate': 'These are catalogue specifications, not stock, so they carry no '
            'rate. Filter on make cost (INR per KG) instead.',
    'available': 'A dyed fabric catalogue entry is a specification, not a stock '
                 'lot, so it has no quantity on hand.',
    'quantity': 'A dyed fabric catalogue entry is a specification, not a stock '
                'lot, so it has no quantity on hand.',
    'qty': 'A dyed fabric catalogue entry is a specification, not a stock lot, '
           'so it has no quantity on hand.',
    # The units as well as the facet names, because that is how these two are
    # actually typed -- "60 inch", "over 500 kg". Recognising only "width" and
    # "quantity" would leave "60 inch" to be split into a text search for "60"
    # and another for "inch", which returns nothing and explains nothing.
    'width': 'Width is not recorded on the dyed fabric catalogue.',
    'inch': 'Width is not recorded on the dyed fabric catalogue.',
    'inches': 'Width is not recorded on the dyed fabric catalogue.',
    'kg': 'A dyed fabric catalogue entry is a specification, not a stock lot, '
          'so it has no quantity on hand.',
    'kgs': 'A dyed fabric catalogue entry is a specification, not a stock lot, '
           'so it has no quantity on hand.',
    'dia': 'Diameter is not recorded on the dyed fabric catalogue.',
    'gauge': 'Gauge is not recorded on the dyed fabric catalogue.',
    'ageing': 'Ageing is a property of a stock lot; the dyed fabric catalogue '
              'holds none.',
}

#--- deterministic score weights (must sum to 1.0) ---
#
# Surplus splits 0.35 / 0.35 / 0.20 / 0.10, the last being depth of stock. There
# is no stock here, so that 0.10 is redistributed across the three remaining
# components in their existing proportions rather than parked on a constant -- a
# tenth of every score that means nothing is a tenth a caller cannot reason
# about. Eligibility by photograph (`withImageOnly`) takes over the job
# availability was really doing: deciding what is worth putting in front of a
# merchandiser.
W_CONSTRUCTION = 0.39
W_COMPOSITION = 0.39
W_GSM = 0.22

# How many deterministically-scored candidates the LLM gets to re-rank, and the
# reserved seats inside that. Same values, same reasons, as surplus.
LLM_SHORTLIST_SIZE = sr.LLM_SHORTLIST_SIZE
RERANK_MAX_TOKENS = sr.RERANK_MAX_TOKENS

# Sample of the codes behind a folded candidate, echoed so a caller can see what
# the de-duplication actually collapsed.
VARIANT_CODE_SAMPLE = 6


# --- `q` patterns, compiled off THIS doctype's facets ---
# The metric and unsupported vocabularies are local (they describe the columns
# this table has); the construction, fibre and texture patterns are surplus's
# own, unchanged, because those vocabularies are the shared ones.
_QUERY_METRIC_RES = sr._compile_metric_patterns(QUERY_METRICS)
_QUERY_UNSUPPORTED_RES = sr._compile_unsupported_patterns(QUERY_UNSUPPORTED)
_QUERY_NOISE = sr._query_noise_words(QUERY_METRICS)


# --- read ---
@frappe.whitelist(allow_guest=True)
@auth_required
def recommend(
    signal: dict,
    colors: list = None,
    qualities: list = None,
    blends: list = None,
    finishes: list = None,
    shade_categories: list = None,
    max_items: int = DEFAULT_MAX_ITEMS,
    withImageOnly: bool = False,
    with_image_only: bool = None,
    use_llm: bool = True,
):
    '''
    Recommends dyed-fabric catalogue entries for a design/sourcing brief.

    The payload is the surplus recommender's, unchanged, and so is the response
    -- see `surplus_recommender.recommend` for the full description of `signal`
    and `colors`. Only the differences are documented here.

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

            Only `fabrics` drives matching; the prose is passed to the LLM for
            context and for attributing each recommendation back to a brief
            line. A bare list of fabric dicts is accepted too.

        colors: the range's colour palette, as
            [{"id": "col_294", "name": "Ocean Blue", "hex": "#4A90C4",
              "pantone": "17-4139", "selected": true}, ...]
            Also read from signal['colors'] when not passed separately.

            Surplus's behaviour, narrowed to one colour per fabric: a fabric
            either is recorded in a palette colour (`matched_palette_id`) or is
            undyed and could be taken to one (`needs_dyeing`), and anything that
            is neither is dropped.

            Note what that means TODAY: the `color` column is empty on every
            dyed-fabric row, so no fabric matches any palette colour, every
            fabric reads as undyed and therefore stays eligible, and each one
            takes the same `UNDYED_ONLY_PENALTY`. Passing a palette currently
            changes the absolute scores and nothing about the order, and
            `color_coverage` comes back with every colour uncovered -- the
            honest answer, since we cannot claim to hold anything in Ocean Blue
            when we do not record colour at all. All of it starts working the
            day colour is populated, with no change here.

        qualities / blends / finishes / shade_categories: optional lists
            restricting the search, matched against `clean_quality`,
            `clean_blend`, `finish` and `shade_category` -- the same four
            filters fabric.list_all offers, so a client can hand the recommender
            the facets the user already picked in the browser.
        max_items: how many fabrics to return (clamped to [1, MAX_ITEMS_CAP],
            raised to the palette size when a bigger palette was supplied).
        withImageOnly: when True, only fabrics that carry a photograph are
            eligible to be ranked at all. This is the gate that replaces
            surplus's depth-of-stock weighting: a moodboard assembled from
            unphotographed fabric is a blank board, so the caller decides up
            front whether an unphotographed fabric is an answer. `with_image_only`
            is accepted as an alias, for parity with the surplus signature.
        use_llm: when False, skip the LLM re-rank and return the deterministic
            ranking. Faster and free; loses the written reasons.

    Returns:
        {
            'success': True,
            'data': {
                'match_count': <len(recommendations)>,
                'recommendations': [<fabric>, ...],   # best first
                'brief_coverage': [                   # per brief line
                    {'brief_index', 'brief_fabric', 'best_for',
                     'ids', 'codes'}, ...],
                'color_coverage': [                   # per palette colour
                    {'palette_id', 'palette_name', 'palette_hex',
                     'palette_pantone', 'covered',
                     'ids', 'codes'}, ...],           # [] with no palette
                'catalogue_size': <rows searched>,
                'candidate_count': <distinct specifications scored>,
                'colors_matched': <bool>,             # a palette was applied
                'llm_used': <bool>,
            },
        }
        or {'success': False, 'error': <message>} on a bad signal / on error.

    Each recommendation carries the fields catalogue() returns, plus
    `fit_score`, `score_breakdown`, `match_score`, `match_source`, `reason`,
    `caveats`, `matched_brief_fabric`, `matched_brief_index`, `best_for`, and
    `variant_count` / `variant_codes` from the de-duplication below. When a
    palette was supplied it also carries `needs_dyeing` and
    `matched_palette_id` / `matched_palette_name` -- one value each, because one
    fabric is one colour and answers at most one palette entry.

    DE-DUPLICATION. A fab code is one manufacturable quality, so surplus can
    rank rows as they come. A dyed-fabric row is one catalogue entry among
    thousands that describe the same cloth -- 24,838 rows hold roughly 1,400
    distinct specifications -- so ranking them flat returns the same fabric six
    times. Candidates are therefore folded by SCORING IDENTITY: construction
    family, gauge ratio, texture tags, composition, GSM and colour. Two rows
    that compare identically on every axis the scorer looks at are, to a
    ranking, the same answer; returning both would spend a recommendation slot
    saying nothing new.

    The fold is not a loss of detail: each candidate reports `variant_count` and
    a sample of the codes behind it. Colour is in the key precisely so that a
    folded card is never standing for two colours at once.

    `price_from_fabric_masters` is what this cloth costs to MAKE per kg, from
    the Fabric Master the row was matched to (`closest_fabric_master`): yarn +
    knitting + dyes & chemicals + M&C finish + finishing, loss % included. It is
    precomputed, because costing a fabric cold makes LLM calls, so it reads None
    until a match run has costed that fabric -- see
    moodboard_fabric_matcher.refresh_fabric_costs. Unlike surplus there is no
    `price` or `price_per_uom` to read it against: a catalogue entry is a
    specification, not a lot of cloth that was bought for a number.
    '''

    try:
        brief_fabrics = sr._brief_fabrics(signal)
        if not brief_fabrics:
            return {'success': False, 'error': 'signal has no usable "fabrics" entries!'}

        palette = sr._brief_colors(
            colors if colors is not None else sr._as_dict(signal).get('colors'))

        max_items = max(1, cint(max_items) or DEFAULT_MAX_ITEMS)
        # One fabric is one colour, so covering an 8-colour palette needs 8
        # fabrics. Capping at 6 would decide up front that most of the palette
        # goes unserved, so the palette raises the ceiling it needs.
        max_items = min(max(MAX_ITEMS_CAP, len(palette)), max(max_items, len(palette)))

        rows = _rows(
            fields=_ITEM_FIELDS,
            qualities=sr._as_list(qualities),
            blends=sr._as_list(blends),
            finishes=sr._as_list(finishes),
            shade_categories=sr._as_list(shade_categories),
            with_image_only=_image_only(withImageOnly, with_image_only),
        )
        if not rows:
            return {'success': False,
                    'error': 'no dyed fabric matched the given filters!'}

        candidates = _candidates(rows)

        #--- 0. colour pass: map fabric colours onto the palette, then filter ---
        if palette:
            color_map = sr._map_stock_colors(
                palette,
                {sr._color_key(c['color']) for c in candidates if c['color']},
            )
            for candidate in candidates:
                _annotate_colors(candidate, palette, color_map)

            eligible = [c for c in candidates
                        if c['matched_palette_id'] or c['needs_dyeing']]
            if not eligible:
                return {
                    'success': False,
                    'error': 'no dyed fabric is available in (or dyeable to) the '
                             'requested colours!',
                }
            candidates = eligible

        #--- 1. deterministic pass: score every candidate against every brief line ---
        for candidate in candidates:
            scores = [_score(brief, candidate) for brief in brief_fabrics]
            best = max(scores, key=lambda s: s['fit_score'])
            candidate['fit_score'] = best['fit_score']
            candidate['score_breakdown'] = best['breakdown']
            candidate['matched_brief_index'] = best['brief_index']
            candidate['_by_brief'] = {s['brief_index']: s['fit_score'] for s in scores}

        candidates.sort(key=lambda c: c['fit_score'], reverse=True)
        shortlist = sr._shortlist(
            candidates, brief_fabrics, palette, LLM_SHORTLIST_SIZE,
            key='id', serves=lambda c, color: c['matched_palette_id'] == color['id'])

        #--- 2. LLM pass: re-rank the shortlist and write the rationale ---
        picks, llm_used = [], False
        if sr._as_bool(use_llm, default=True):
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
                'catalogue_size': len(rows),
                'candidate_count': len(candidates),
                'colors_matched': bool(palette),
                'llm_used': llm_used,
            },
        }

    except Exception as ex:
        frappe.log_error(frappe.get_traceback(), 'moodboard_dyed_recommender.recommend()')
        return {'success': False, 'error': str(ex)}


@frappe.whitelist(allow_guest=True)
@auth_required
def catalogue(
    search: str = None,
    q: str = None,
    qualities: list = None,
    blends: list = None,
    finishes: list = None,
    shade_categories: list = None,
    withImageOnly: bool = False,
    with_image_only: bool = None,
    sort_by=_DEFAULT_SORT_BY,
    sort_dir=_DEFAULT_SORT_DIR,
    limit=_DEFAULT_LIMIT,
    offset=0,
):
    '''
    Returns the dyed fabrics the recommender searches, unranked -- the same rows
    `recommend()` scores, minus the scores and minus the de-duplication. Useful
    for browsing what is recommendable, and for checking how a fabric's
    construction and composition were read.

    ONE ITEM IS ONE ROW. Surplus folds its rows into fab codes because a fab
    code is what a buyer commits against; a dyed fabric is not a lot of anything
    and has nothing to fold into, so nothing is folded. (`recommend()` does
    collapse identical specifications, but only so a top-6 is six different
    fabrics -- see its docstring.)

    Search: `search` is a case-insensitive substring matched across
    CATALOGUE_SEARCH_FIELDS -- code, batch, quality, blend.

    Query: `q` is the same box read as a sentence -- "cotton single jersey 180
    gsm" -- parsed into facet filters plus whatever words are left over. It
    understands, in any order and any mix:

      construction  single jersey, rib, interlock, waffle, terry, fleece,
                    pique, pointelle, jacquard, mesh, woven, ...
      fibre         cotton, polyester, spandex, viscose, modal, tencel, linen,
                    ... optionally with a share -- "95% cotton"
      texture       slub, melange, striped, brushed, plated, variegated
      gsm           "180 gsm", "gsm 180", "gsm > 160", "160-200 gsm"
      make cost     "make cost < 400"  -- INR per KG, on price_from_fabric_masters

    Comparators are >, >=, <, <=, =, and the words for them (over, under, at
    least, more than, up to, ...); > and < are inclusive. Every number is taken
    literally -- "150 gsm" is 150 GSM, not a band around it -- so approximate
    weight is asked for as a range, "140-160 gsm", or as a bound, "gsm > 140".
    Fibre shares are the one exception and are always a band: "95% cotton" is
    90-100% cotton, because a blend is rescaled to total 100 before it is
    compared and a 95:5 can arrive as 94.99. A fibre named without a share only
    has to be present at all.

    Anything left after parsing becomes a text condition matched across
    QUERY_SEARCH_FIELDS (which, unlike `search`, includes `clean_quality`,
    `clean_blend`, the descriptions, `finish`, `shade` and `color`, so retail
    English, finish names and colour words land somewhere). Leftover words are
    ANDed with each other and with everything else.

    `q` is separate from `search` rather than an upgrade of it, because `search`
    is a literal substring its callers already depend on: read as a query, a
    batch number like "SJ-180" would start filtering on GSM. Send both and they
    are ANDed.

    Facets this catalogue does not carry -- price, quantity, width, dia, gauge,
    ageing, FOB, MOQ, lead time -- are recognised and reported under
    `query.unsupported` rather than searched for as words. See QUERY_UNSUPPORTED
    for why `price` in particular is refused when a per-kg rate does exist.

    A fabric whose value for a filtered facet is unknown -- no GSM recorded, no
    fabric-master cost -- does not pass that filter. It cannot be shown to
    satisfy it, and the sort already treats unknown as last rather than as zero.

    Filters: `qualities`, `blends`, `finishes` and `shade_categories` are lists
    matched against `clean_quality`, `clean_blend`, `finish` and
    `shade_category`; `withImageOnly` keeps only fabrics that carry a
    photograph. Search, query and filters are ANDed; `total` reflects everything
    applied.

    Sorting: `sort_by` is one of CATALOGUE_SORT_FIELDS -- creation (default),
    code, gsm, quality, make_cost -- with `sort_dir` asc|desc (default desc).
    `quality` orders on the merchandising sort order, not alphabetically.
    Fabrics missing the sorted value sort last in either direction.

    Pagination: limit (default 20, capped 100) + offset (>= 0). `data` is the
    same envelope the moodboard lists return -- { total, limit, offset, items }
    -- where total counts ALL matching fabrics so the client can page.
    `search` / `sort_by` / `sort_dir` are echoed back as applied, and `query`
    echoes how `q` was actually read: `applied` as display-ready labels, the
    parsed facets beside them, plus `terms` and `unsupported`. A query box has
    to be able to show its own interpretation, or a filter nobody meant is
    indistinguishable from an empty shelf.
    '''
    try:
        limit, offset = _page(limit, offset)
        sort_by, sort_dir = _sort(sort_by, sort_dir)
        search = sr._clean(search)
        query = _parse_query(q)

        filters = {
            'search': search,
            'terms': query['terms'] if query else None,
            'qualities': sr._as_list(qualities),
            'blends': sr._as_list(blends),
            'finishes': sr._as_list(finishes),
            'shade_categories': sr._as_list(shade_categories),
            'with_image_only': _image_only(withImageOnly, with_image_only),
            'ranges': query['ranges'] if query else None,
        }

        # Two paths, and the split is the whole reason this endpoint stays fast
        # over 24,838 rows. Construction, fibre and texture are DERIVED -- they
        # do not exist as columns and can only be decided once a row has been
        # classified -- so a query naming one of them has to classify the whole
        # matching set before it can count or page it. Everything else is a real
        # column, so when the query names none of them the count and the page
        # window stay in SQL and only the page itself is ever built into an item.
        #
        # Both paths sort in SQL, and that is not an optimisation but a
        # correctness point: the ORDER BY is the one below, applied identically
        # either way, so `sort_by=creation` means the same thing whether or not
        # the caller also typed a construction. A Python re-sort on this path
        # would have had to work from the item, which carries neither `creation`
        # nor `quality_sort_order`, and two sorts that disagree are worse than
        # one that costs a little.
        derived = bool(query and (query['families'] or query['fibres']
                                  or query['textures']))

        if derived:
            rows = _rows(
                fields=_ITEM_FIELDS, order_by=CATALOGUE_SORT_FIELDS[sort_by],
                order_dir=sort_dir, **filters
            )
            items = _filter_items([_item(row) for row in rows], query)
            total = len(items)
            page = items[offset:offset + limit]
        else:
            total = _rows(fields=None, count_only=True, **filters)
            rows = _rows(
                fields=_ITEM_FIELDS, order_by=CATALOGUE_SORT_FIELDS[sort_by],
                order_dir=sort_dir, limit=limit, offset=offset, **filters
            )
            page = [_item(row) for row in rows]

        return {
            'success': True,
            'data': _page_envelope(
                page, total, limit, offset,
                search=search, sort_by=sort_by, sort_dir=sort_dir,
                q=sr._clean(q), query=_query_summary(query),
            ),
        }

    except Exception as ex:
        frappe.log_error(frappe.get_traceback(), 'moodboard_dyed_recommender.catalogue()')
        return {'success': False, 'error': str(ex)}


# --- loading ---
def _rows(fields, search=None, terms=None, qualities=None, blends=None,
          finishes=None, shade_categories=None, with_image_only=False,
          ranges=None, order_by=None, order_dir='desc', limit=None, offset=0,
          count_only=False):
    '''
    Dyed-fabric rows matching every filter, or their count.

    Built with the query builder rather than frappe.get_all because the text
    conditions are an AND of ORs -- each leftover word has to hold somewhere
    among QUERY_SEARCH_FIELDS, and all the words have to hold -- which
    `or_filters` cannot express: it is a single OR group. Surplus solves the
    same problem with one resolving query per term, but it could afford to,
    because it was intersecting 124 fab codes rather than up to 24,838 rows.

    A numeric range whose metric has a `column` is pushed down here too, so
    "gsm > 200" narrows in SQL instead of loading the table to throw most of it
    away. The derived facets (construction, fibre, texture) cannot be: they are
    read out of the columns, not stored in them, so they are applied by
    _filter_items after the rows are classified.
    '''
    Fabric = frappe.qb.DocType(DOCTYPE)
    query = frappe.qb.from_(Fabric)

    if search:
        query = query.where(_any_like(Fabric, CATALOGUE_SEARCH_FIELDS, search))
    for term in terms or []:
        query = query.where(_any_like(Fabric, QUERY_SEARCH_FIELDS, term))

    for values, column in ((qualities, 'clean_quality'), (blends, 'clean_blend'),
                           (finishes, 'finish'), (shade_categories, 'shade_category')):
        if values:
            query = query.where(getattr(Fabric, column).isin(list(values)))

    if with_image_only:
        query = query.where(Fabric.has_image_file == 1)

    for key, bounds in (ranges or {}).items():
        column = QUERY_METRICS[key].get('column')
        if not column:
            continue
        field = getattr(Fabric, column)
        # An unset value is not a zero: a fabric with no GSM recorded is not
        # 0 GSM and an uncosted one is not free, so neither can satisfy a
        # numeric filter. Same call _sort_items makes when it pushes them last.
        query = query.where(field.isnotnull() & (field != 0))
        if bounds.get('min') is not None:
            query = query.where(field >= bounds['min'])
        if bounds.get('max') is not None:
            query = query.where(field <= bounds['max'])

    if count_only:
        from frappe.query_builder.functions import Count
        return cint(query.select(Count('*')).run()[0][0])

    query = query.select(*[getattr(Fabric, f) for f in fields])
    if order_by:
        direction = frappe.qb.asc if order_dir == 'asc' else frappe.qb.desc
        # Nulls and zeros last in either direction, then the value itself, then
        # `name` so two requests for different pages of one query agree.
        column = getattr(Fabric, order_by)
        query = query.orderby(
            frappe.qb.terms.Case().when(
                column.isnull() | (column == 0), 1).else_(0),
            order=frappe.qb.asc,
        ).orderby(column, order=direction).orderby(Fabric.name, order=frappe.qb.asc)
    if limit is not None:
        query = query.limit(limit).offset(offset)

    return query.run(as_dict=True)


def _any_like(table, columns, term):
    ''' One search word ORed across `columns`, with the user's own %, _ and \\
        taken literally. '''
    pattern = sr._like(term)
    condition = None
    for column in columns:
        clause = getattr(table, column).like(pattern)
        condition = clause if condition is None else (condition | clause)
    return condition


# --- items ---
def _item(row):
    '''
    One dyed-fabric row -> the recommendable item object.

    Construction and composition are read through CONSTRUCTION_FIELDS /
    BLEND_FIELDS and then classified by the shared vocabularies, so a fabric
    written in retail English ("Single Jersey", "95% Cotton 5% Spandex") and a
    fabric written in mill shorthand ("RIB_1X1 EL COMPACT") land in the same
    comparison space -- which is what lets either be scored against a brief.

    Colour is a SCALAR here, not the colourway list surplus carries. A fab code
    is stocked in many colours at once, so surplus has to return an array and
    nominate a hero from it; one dyed-fabric row is one colour, so `color` is
    simply that colour, `image_url` is simply its image, and there is nothing
    for a hero to be chosen from.
    '''
    quality, _ = _preferred(row, CONSTRUCTION_FIELDS)
    blend, _ = _preferred(row, BLEND_FIELDS)

    family, tags, ratio, structure_label = _construction_of(quality)
    composition = dict(_composition_of(blend))
    gsm = cint(row.get('gsm')) or None

    # Dyed-fabric images are keyed on BATCH alone, so a URL can always be
    # constructed and proves nothing. `has_image_file` is what an actual bucket
    # listing set, so the URLs are null without it -- the same independence
    # surplus keeps between having a batch and having a photograph.
    batch = sr._clean(row.get('batch'))
    has_image = bool(cint(row.get('has_image_file')) and batch)

    return {
        # --- identity ---
        # `code` is the fabric code and is NOT unique (14,334 distinct across
        # 24,838 rows); `id` is the document name, which is. A caller fetching
        # one fabric back must use `id`.
        'id': row.get('name'),
        'code': row.get('code'),
        'batch': batch,

        # --- the fields the brief asked for ---
        'composition': blend,
        'composition_label': sr._composition_label(composition),
        'composition_pct': composition,
        'gsm': gsm,
        'quality': quality,
        'quality_label': sr._quality_label(family, structure_label),
        'construction_family': family,
        'texture_tags': list(tags),
        'structure_ratio': ratio,

        # What this cloth costs to MAKE, per kg, from the Fabric Master it was
        # matched to -- yarn + knitting + dyes & chemicals + M&C finish +
        # finishing, loss % included. Precomputed by moodboard_fabric_matcher
        # because costing a fabric cold makes LLM calls; None until it has run.
        # There is deliberately no `price` or `price_per_uom` beside it: a
        # catalogue entry is a specification, not cloth that was bought for a
        # number.
        'price_from_fabric_masters': (round(flt(row.get('closest_fabric_cost_per_kg')), 2)
                                      or None),
        'closest_fabric_master': row.get('closest_fabric_master'),

        # --- colour ---
        # Empty on every row today (see the module docstring). One value, not a
        # list: this fabric's colour.
        'color': sr._clean(row.get('color')),
        'shade': sr._clean(row.get('shade')),
        'shade_category': sr._clean(row.get('shade_category')),

        # --- supporting detail ---
        'finish': sr._clean(row.get('finish')),
        'description': sr._clean(row.get('description')),
        'ai_description': sr._clean(row.get('ai_description')),
        'custom_fabric_name': sr._clean(row.get('custom_fabric_name')),

        # --- the image the card shows ---
        'has_image': has_image,
        'image_url': cloud.fabric_image_url(batch) if has_image else None,
        'thumbnail': cloud.fabric_thumbnail_url(batch) if has_image else None,
    }


def _preferred(row, columns):
    ''' The first of `columns` with a value, and which one that was. '''
    for column in columns:
        value = sr._clean(row.get(column))
        if value:
            return value, column
    return None, None


# Classification is memoised because the inputs repeat enormously: 24,838 rows
# carry 231 distinct `quality` codes and 309 distinct `blend` strings, so
# classifying the whole table is a few hundred parses rather than fifty
# thousand. Both return immutable values -- the cache is shared, and a caller
# that mutated one would corrupt every later row that shares the string.
@lru_cache(maxsize=4096)
def _construction_of(text):
    ''' A construction string -> (family, tags, ratio, structure label). '''
    construction = sr._classify_construction(text or '', text or '')
    return (construction['family'], tuple(sorted(construction['tags'])),
            construction['ratio'], sr._structure_label(text))


@lru_cache(maxsize=4096)
def _composition_of(text):
    '''
    A blend string -> ((fibre, percent), ...).

    Read as retail English first ("95% Cotton 5% Spandex"), which is how both
    `blend` and `clean_blend` are written on this doctype -- unlike Surplus
    Stock, whose blend column is mill shorthand. The shorthand parser is still
    tried as a fallback, so a row that does arrive as "95:5 BCI:EL" is not
    silently read as having no composition at all.
    '''
    composition = sr._parse_brief_blend(text) or sr._parse_stock_blend(text)
    return tuple(sorted(composition.items()))


# --- de-duplication ---
def _candidates(rows):
    '''
    Rows -> the distinct things there are to recommend.

    Folded by SCORING IDENTITY: construction family, gauge ratio, texture tags,
    composition, GSM and colour -- every axis _score looks at, and nothing
    else. Two rows that agree on all of them cannot be told apart by any ranking this
    module can produce, so returning both would spend a recommendation slot
    twice on one answer. Rows with nothing scoreable (no construction and no
    composition read) fold under their own id and stay separate, since there is
    no evidence they are the same cloth.

    The representative row is the one whose card the fabric will be shown on:
    the first with a photograph, because a moodboard needs a picture, and
    failing that simply the first. Ties resolve on `id`, so two runs over
    unchanged data return the same fabric rather than whichever row the database
    handed back first.

    Colour is part of the key, so a fold never spans two colours and a candidate
    always has exactly one. That keeps a folded card honest -- showing one
    colour while quietly standing for several would be the group behaviour this
    doctype does not have -- and it is why nothing here needs a colourway list.
    Today the column is empty, so it partitions nothing.

    `variant_count` and `variant_codes` are added HERE and not in `_item`,
    because folding is the only thing that makes them mean anything; on the
    catalogue, where one item is one row, they would be 1 and the row's own code
    on every card.
    '''
    by_spec = {}
    for row in sorted(rows, key=lambda r: str(r.get('name') or '')):
        item = _item(row)
        by_spec.setdefault(_spec_key(item), []).append(item)

    candidates = []
    for members in by_spec.values():
        representative = next((m for m in members if m['has_image']), members[0])
        candidate = dict(representative)
        candidate['variant_count'] = len(members)
        candidate['variant_codes'] = _sample_codes(members)
        candidates.append(candidate)

    return candidates


def _spec_key(item):
    '''
    Everything the scorer compares, and only that.

    Falls back to the construction and composition text when neither classified,
    so two unreadable fabrics are not declared identical on the strength of both
    being unreadable -- and finally to `id`, which keeps a wholly blank row
    distinct from every other wholly blank row.

    Fibre shares are folded at whole percent. Blends are rescaled to total 100
    before they are compared, and plenty of rows do not add up in the first
    place -- "96% Cotton 5% Spandex" is 101 and comes back as 95.05/4.95 -- so
    an exact test on the share splits one fabric into two candidates that differ
    by five hundredths of a composition point. That is inside the noise of every
    score in this module, and outside it a top-six reads as two rows of
    "95% Cotton 5% Elastane, 180 GSM".

    Colour joins the key for the reason given in `_candidates`: two fabrics in
    two colours are two answers, and a card that folded them could only show
    one of them.
    '''
    color = sr._color_key(item['color'])
    if not (item['construction_family'] or item['composition_pct']):
        return ('?', item['quality'] or item['id'], item['composition'],
                item['gsm'], color)
    return (
        item['construction_family'],
        item['structure_ratio'],
        tuple(item['texture_tags']),
        tuple(sorted((fibre, round(percent))
                     for fibre, percent in item['composition_pct'].items())),
        item['gsm'] or 0,
        color,
    )


def _sample_codes(members):
    ''' Distinct fabric codes behind a folded candidate, capped. '''
    seen, codes = set(), []
    for member in members:
        code = member['code']
        if not code or code in seen:
            continue
        seen.add(code)
        codes.append(code)
        if len(codes) >= VARIANT_CODE_SAMPLE:
            break
    return codes


# --- palette ---
def _annotate_colors(candidate, palette, color_map):
    '''
    Which palette colour this fabric serves, if any.

    Surplus needs an ARRAY here, and a hero chosen out of it, because one fab
    code is stocked in many colours and can answer several palette entries at
    once. One dyed fabric is one colour, so it answers at most one -- and the
    card it is shown on is already that colour, with nothing to re-point.

    Sets, and only these:
      matched_palette_id / matched_palette_name
                    the palette colour this fabric is recorded in, or None.
      needs_dyeing  no colour recorded, or an RFD shade, so it could be taken
                    to any palette colour. TRUE FOR EVERY ROW TODAY, because
                    `color` is empty across the table -- which is what makes
                    the palette filter a no-op rather than a wall.
    '''
    key = sr._color_key(candidate['color'])
    palette_id = color_map.get(key) if key else None
    matched = next((c for c in palette if c['id'] == palette_id), None)

    candidate['matched_palette_id'] = matched['id'] if matched else None
    candidate['matched_palette_name'] = matched['name'] if matched else None
    candidate['needs_dyeing'] = (
        not candidate['color']
        or (candidate['shade_category'] or '').strip().upper() in sr.UNDYED_SHADES
    )
    return candidate


# --- scoring ---
def _score(brief, item):
    '''
    One brief line against one fabric -> a 0-100 fit score plus the sub-scores
    it was built from, so a recommendation can always explain itself.

    Surplus's `_score` without the availability component, whose weight is
    redistributed across the three that remain (see W_CONSTRUCTION). The
    sub-scores themselves are surplus's own functions, unchanged: a construction
    or a blend must not mean one thing on one catalogue and another thing here.
    '''
    construction = sr._construction_score(brief, item)
    composition = sr._composition_score(brief['composition'], item['composition_pct'])
    gsm = sr._gsm_score(brief['gsm'], item['gsm'])

    fit = (
        construction * W_CONSTRUCTION
        + composition * W_COMPOSITION
        + gsm * W_GSM
    )

    # Stretch is a functional requirement, not just a percentage: a 95/5 target
    # met by a 100% cotton rib is the right hand-feel and the wrong garment.
    brief_el = brief['composition'].get('elastane', 0.0)
    fabric_el = item['composition_pct'].get('elastane', 0.0)
    stretch_penalty = 1.0
    if brief_el >= sr.STRETCH_REQUIRED_PCT and fabric_el <= 0:
        stretch_penalty = sr.NO_STRETCH_PENALTY
    elif fabric_el >= sr.STRETCH_REQUIRED_PCT and brief_el <= 0:
        stretch_penalty = sr.EXTRA_STRETCH_PENALTY
    fit *= stretch_penalty

    # Colour is a filter, not a weight. A fabric only reaches the scorer if it
    # already passed the palette; the one thing left to express is that a fabric
    # which merely COULD be dyed to a palette colour must sit below one already
    # recorded in it. Today that is every fabric, so this is a constant factor
    # that lowers every score and reorders nothing -- correct on both counts,
    # since every one of them would in fact have to be dyed.
    color_penalty = 1.0
    if item.get('needs_dyeing') and not item.get('matched_palette_id'):
        color_penalty = sr.UNDYED_ONLY_PENALTY
    fit *= color_penalty

    return {
        'brief_index': brief['index'],
        'fit_score': round(fit, 2),
        'breakdown': {
            'construction': round(construction, 1),
            'composition': round(composition, 1),
            'gsm': round(gsm, 1),
            'stretch_penalty': stretch_penalty,
            'color_penalty': color_penalty,
        },
    }


# --- catalogue query language ---
def _parse_query(text):
    '''
    One typed line -> the facets it asked for, plus the words left over.

    Surplus's parser over this doctype's facets: same passes, same order, same
    reasons. Read strictly left to right through the vocabularies, most specific
    first, each pass consuming what it claimed so the next never sees it again.
    Unsupported facets go first so the comparison attached to them ("price <
    500") is removed whole, rather than leaving a "< 500" to be misread as a GSM
    or a "500" to be searched for as text.

    Returns None for an empty query, so callers can skip the whole pass.
    '''
    raw = sr._clean(text)
    if not raw:
        return None

    parsed = {
        'raw': raw,
        'ranges': {},       # metric key -> {min, max}
        'families': [],     # construction families
        'fibres': {},       # fibre group -> required percent (0 = any)
        'textures': [],     # texture tags
        'terms': [],        # leftover words, matched as text
        'unsupported': [],  # recognised, but not answerable from this catalogue
    }

    working = sr._normalise_query(raw)
    working = _extract_unsupported(working, parsed)
    working = _extract_metrics(working, parsed)
    working = sr._extract_families(working, parsed)
    working = sr._extract_textures(working, parsed)
    working = sr._extract_fibres(working, parsed)
    parsed['terms'] = _residual_terms(working)
    return parsed


def _extract_unsupported(working, parsed):
    ''' Strip the facets this catalogue cannot answer, recording why. '''
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
                bounds = sr._query_bounds(match, metric)
                if bounds:
                    parsed['ranges'][key] = sr._merge_bounds(
                        parsed['ranges'].get(key), bounds)
            working = pattern.sub(' ', working)
    return working


def _residual_terms(working):
    '''
    What is left once every vocabulary has taken its share: the words to match
    as text. Stop words, stranded units and comparators are dropped; single
    characters go too (they are the debris of "2x2" and "95%"), but bare numbers
    stay -- a number nothing claimed is usually a code or a batch, which is
    exactly what a text match is for.
    '''
    terms, seen = [], set()
    for token in working.split():
        token = token.strip('%<>=-.').strip()
        if len(token) < 2 or token in _QUERY_NOISE or token in seen:
            continue
        seen.add(token)
        terms.append(token)
    return terms[:sr.QUERY_MAX_TERMS]


def _filter_items(items, query):
    '''
    The DERIVED facets applied to built items -- construction, fibre and
    texture, the three that are read out of the columns rather than stored in
    them. The numeric ranges and every column filter were already applied in
    SQL, so they are not re-tested here.
    '''
    return [item for item in items if _item_matches(item, query)]


def _item_matches(item, query):
    ''' One fabric against every derived facet in the query. All, or none. '''
    if query['families'] and not _query_family_matches(item, query['families']):
        return False

    for fibre, required in query['fibres'].items():
        percent = flt((item.get('composition_pct') or {}).get(fibre))
        if percent <= 0:
            return False
        if required and not (
            required - sr.QUERY_FIBRE_TOLERANCE <= percent
            <= required + sr.QUERY_FIBRE_TOLERANCE
        ):
            return False

    if query['textures'] and not set(query['textures']).issubset(
            item.get('texture_tags') or ()):
        return False

    return True


def _query_family_matches(item, families):
    '''
    A construction filter passes on the fabric's own family, or on the label the
    card shows -- QUALITY_LABELS folds the jersey-based structures together for
    display, and matching only the family would hide a card from the very words
    printed on it.

    Several constructions in one query are alternatives, the way a multi-select
    filter behaves. Nothing is both, so ANDing them would only ever return
    nothing.
    '''
    family = item.get('construction_family')
    label = item.get('quality_label')
    for wanted in families:
        if family == wanted:
            return True
        if label and sr.QUALITY_LABELS.get(wanted) == label:
            return True
    return False


def _query_summary(query):
    '''
    The parsed query, shaped for the caller to render.

    `applied` is the readable version -- the chips a filter box puts above the
    results -- and the parsed facets sit beside it for anything that wants to
    edit rather than display. Echoing this is not decoration: a query that
    quietly read a word as a filter, or quietly read nothing at all, is
    otherwise indistinguishable from a catalogue that does not hold the fabric.
    '''
    if not query:
        return None

    # Ordered as the fabric is described rather than as it was parsed --
    # construction, then what it is made of, then its numbers, then the words
    # nothing claimed.
    applied = []
    for family in query['families']:
        applied.append(sr.QUALITY_LABELS.get(family)
                       or family.replace('_', ' ').title())
    for fibre, required in query['fibres'].items():
        applied.append(f'{sr._pct(required)}% {fibre.title()}'
                       if required else fibre.title())
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
    shelf looks like a missing fabric instead of an impossible request.
    '''
    unit = QUERY_METRICS[key]['unit_label']
    low, high = bounds.get('min'), bounds.get('max')
    if low is not None and high is not None:
        if low > high:
            return f'{unit}: nothing can be both {sr._pct(low)} and {sr._pct(high)}'
        return (f'{sr._pct(low)} {unit}' if low == high
                else f'{sr._pct(low)}-{sr._pct(high)} {unit}')
    if low is not None:
        return f'>= {sr._pct(low)} {unit}'
    return f'<= {sr._pct(high)} {unit}'


# --- LLM re-rank ---
def _llm_rerank(signal, brief_fabrics, palette, shortlist, max_items):
    '''
    Hands the shortlist to the LLM to pick and rank the final set, attribute
    each pick to the brief line it serves and write the reason. Returns [] on
    any failure, which leaves the deterministic ranking standing.

    The prompt is surplus's, re-pointed at a catalogue rather than at stock, and
    the difference is not cosmetic. Surplus is cloth that already exists in a
    warehouse, so its prompt can talk about depth of stock and about not
    knitting anything new. These are catalogue specifications the mill knows how
    to make, so quantity is not a fact the model has, and telling it otherwise
    would invite reasons that cite a stock position nobody holds.
    '''
    try:
        catalogue_lines = '\n'.join(_shortlist_line(c, bool(palette)) for c in shortlist)
        brief_lines = '\n'.join(
            f'[{f["index"]}] {f["label"]}'
            + (f' | best for: {f["best_for"]}' if f['best_for'] else '')
            + (f' | brief rationale: {f["reason"]}' if f['reason'] else '')
            for f in brief_fabrics
        )
        valid_ids = ', '.join(c['id'] for c in shortlist)

        # With a palette in play the shortlist carries a Serves column naming the
        # palette colours each fabric is already recorded in, and covering the
        # palette becomes part of the task. Without one, the prompt is unchanged.
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
Every fabric below is either already recorded in at least one of them, or carries no colour yet and could be dyed to any of them.

'''
            palette_column = ' Serves <TAB>'
            palette_rules = (
                '\n- `Serves` names the palette colour this fabric is ALREADY recorded in. '
                'One fabric is one colour, so it serves at most one. "undyed" means the '
                'fabric carries no colour yet and would have to be dyed.\n'
                '- Cover the palette: prefer a set that brings as many DIFFERENT palette colours '
                'as possible over several fabrics serving the same colour. A fabric that is the '
                'only one serving a colour is worth more than a marginally better second option '
                'for a colour already served.\n'
                '- Rank a fabric already in a palette colour above an undyed one for the same '
                'brief line. Undyed is a real option, but it is extra work and lead time.\n'
                '- When a pick is undyed, say so in Caveats.'
            )

        system_prompt = f'''You are a textile merchandiser choosing which fabrics from a mill's dyed-fabric catalogue a brand should build a range from. You are given a design brief that specifies the fabrics the range wants, and a shortlist of catalogue fabrics the mill knows how to make. Your job is to pick the ones that let the brand hit the brief.

# THE BRIEF'S FABRICS
Each line is one fabric the brief calls for, with its index in brackets:
---
{brief_lines}
---

{palette_block}
# OUR DYED FABRIC SHORTLIST
One line per fabric. Fields are tab-separated:
Id <TAB> Code <TAB> Construction <TAB> Composition <TAB> GSM <TAB> Finish <TAB>{palette_column} PreScore
`Id` is the fabric's internal identifier and is what you must return. `Code` is the mill's fabric code, shown so you can name it in a reason; several codes can describe the same specification, in which case `Code` is one of them.
`PreScore` is our own 0-100 fit estimate against the closest brief line; treat it as a strong hint, not an instruction — you may overturn it when the brief's prose says otherwise.
These are catalogue specifications, not stock on a shelf: there is no quantity, no price and no lead time here, so never claim, imply or reason about how much of anything is available.
Whatever the catalogue holds in the exact construction each brief line names is guaranteed to appear below, so if a construction is absent here we do not make it — but say only that, and never assert anything else about the catalogue beyond these lines.
---
{catalogue_lines}
---

# YOUR TASK
Return the {max_items} strongest fabrics for this brief, best first.

Return a JSON array using exactly this shape:
[
    {{
        "Id": "a1b2c3d4e5",
        "BriefFabricIndex": 0,
        "MatchScore": 88,
        "Reason": "ONE sentence, 25 words maximum: why this fabric serves that brief line, in merchandiser language",
        "Caveats": ["concrete gap, 8 words maximum"]
    }}
]

# RULES
- Return at most {max_items} recommendations, sorted by MatchScore descending.
- Id MUST be copied verbatim from the shortlist above. Valid values: {valid_ids}. Drop any pick you cannot match to that list.
- Never return the same Id twice.
- BriefFabricIndex is the bracketed index of the brief fabric this pick serves — the line it actually serves, which need not be the one PreScore assumed.
- MatchScore is an integer 0-100 for how well this fabric serves that brief line. Weigh construction first, then composition, then GSM; finish breaks ties. Be honest — a substitute knit or a missing elastane content should not score in the 90s.
- Reason is ONE sentence of at most 25 words, citing the concrete attributes (construction, blend, GSM, finish). No marketing language, no invented facts, nothing not present in the shortlist line or the brief. Do not restate the id or repeat the caveats.
- Caveats lists real gaps (wrong weight, no stretch, wrong blend ratio) — at most 2, each at most 8 words, no sentences. Use an empty array when the fabric genuinely matches.
- Prefer covering several different brief lines over stacking near-identical fabrics against one line, unless one line is clearly the whole range.{palette_rules}
- Output raw, valid JSON only: a single array, double-quoted keys/strings, no trailing commas, no markdown fences, no commentary.
'''

        payload = sr._as_dict(signal)
        description = payload.get('description') if isinstance(payload, dict) else None
        user_prompt = f'''Here is the brief's own rationale for its fabric choices:
---
{description or '(none supplied)'}
---

Pick the dyed fabrics that best deliver this brief, following the format and rules above.'''

        picks = llm.get_claude_response(system_prompt, user_prompt, 'list',
                                        max_tokens=RERANK_MAX_TOKENS)
        return [p for p in picks if isinstance(p, dict)] if isinstance(picks, list) else []

    except Exception:
        # A missing API key, a rate limit or a malformed reply must not cost the
        # caller its recommendations -- the deterministic ranking still stands.
        frappe.log_error(frappe.get_traceback(),
                         'moodboard_dyed_recommender._llm_rerank()')
        return []


def _shortlist_line(candidate, with_palette=False):
    '''
    One candidate -> the tab-separated line the LLM prompt carries.

    There is no colour column without a palette, and deliberately: the `color`
    column is empty on every row, so a Colour field would be "unspecified" on
    all twenty-four lines -- prompt the caller waits on that says nothing. With
    a palette, `Serves` says which palette colour this fabric answers, or that
    it would have to be dyed.
    '''
    serves = ''
    if with_palette:
        matched = candidate.get('matched_palette_name') or candidate.get('matched_palette_id')
        serves = (matched or ('undyed' if candidate.get('needs_dyeing') else 'none')) + '\t'

    return '\t'.join([
        candidate['id'],
        candidate['code'] or '?',
        candidate['quality_label'] or candidate['quality'] or '?',
        candidate['composition_label'] or candidate['composition'] or '?',
        str(candidate['gsm'] or 'unknown'),
        candidate['finish'] or 'unspecified',
        serves + str(int(candidate['fit_score'])),
    ])


# --- assembly ---
def _finalise(brief_fabrics, palette, shortlist, picks, max_items):
    '''
    Merges the LLM's picks over the deterministic ranking.

    The LLM decides order, score and rationale for what it picked; anything it
    dropped is still available to backfill, in deterministic order, so the
    caller always gets `max_items` recommendations when the catalogue holds
    enough to fill them.

    Backfill runs in three passes, most specific first: palette colours nothing
    serves yet, then brief lines nothing covers yet, then anything. The colour
    pass goes first because that is what "at least one of each colour" means --
    a second jersey for a line already served is worth less than the only fabric
    that comes in Soft Coral.
    '''
    by_id = {c['id']: c for c in shortlist}
    by_index = {f['index']: f for f in brief_fabrics}

    recommendations, used = [], set()

    for pick in picks:
        candidate = by_id.get(str(pick.get('Id') or '').strip())
        if not candidate or candidate['id'] in used:
            continue

        brief = (by_index.get(cint(pick.get('BriefFabricIndex')))
                 or by_index.get(candidate['matched_brief_index']))
        recommendations.append(_recommendation(
            candidate,
            brief,
            match_score=cint(pick.get('MatchScore')) or int(candidate['fit_score']),
            reason=sr._clean(pick.get('Reason')),
            caveats=[c for c in (pick.get('Caveats') or []) if isinstance(c, str)],
            source='llm',
        ))
        used.add(candidate['id'])
        if len(recommendations) >= max_items:
            return recommendations

    covered = {r['matched_brief_index'] for r in recommendations}
    colors_covered = {r.get('matched_palette_id') for r in recommendations}
    wanted_colors = {c['id'] for c in palette}

    # pass 1: fabrics that bring a palette colour nothing serves yet
    # pass 2: fabrics for a brief line nothing covers yet
    # pass 3: anything left, best score first
    for stage in ('color', 'brief', 'any'):
        for candidate in shortlist:
            if len(recommendations) >= max_items:
                return recommendations
            if candidate['id'] in used:
                continue
            if stage == 'color':
                served = candidate.get('matched_palette_id')
                if served not in (wanted_colors - colors_covered):
                    continue
                colors_covered.add(served)
            elif stage == 'brief' and candidate['matched_brief_index'] in covered:
                continue
            recommendations.append(_recommendation(
                candidate,
                by_index.get(candidate['matched_brief_index']),
                match_score=int(round(candidate['fit_score'])),
                reason=None,
                caveats=[],
                source='score',
            ))
            used.add(candidate['id'])
            covered.add(candidate['matched_brief_index'])

    return recommendations


def _recommendation(candidate, brief, match_score, reason, caveats, source):
    ''' A scored candidate + the brief line it serves -> the returned object. '''
    recommendation = dict(candidate)
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
            'ids': [r['id'] for r in recommendations
                    if r['matched_brief_index'] == fabric['index']],
            'codes': [r['code'] for r in recommendations
                      if r['matched_brief_index'] == fabric['index']],
        }
        for fabric in brief_fabrics
    ]


def _color_coverage(palette, recommendations):
    '''
    Which palette colours the returned set actually serves, and with what.

    The point of the report is the colours that came back EMPTY: "we hold
    nothing in Soft Coral" is the answer a merchandiser needs in order to plan a
    dye lot, and it is invisible from the recommendations alone. Empty list when
    no palette was supplied -- and, until the `color` column is populated, every
    entry reads uncovered, which is the honest answer rather than a failure.
    '''
    coverage = []
    for color in palette:
        serving = [r for r in recommendations
                   if r.get('matched_palette_id') == color['id']]
        coverage.append({
            'palette_id': color['id'],
            'palette_name': color['name'],
            'palette_hex': color['hex'],
            'palette_pantone': color['pantone'],
            'covered': bool(serving),
            'ids': [r['id'] for r in serving],
            'codes': [r['code'] for r in serving],
        })
    return coverage


# --- helpers ---
def _image_only(primary, alias):
    '''
    `withImageOnly` as the client sends it, with `with_image_only` accepted as
    an alias so a caller written against the surplus signature keeps working.
    The alias only speaks when it was actually sent, so passing neither is False
    and passing the camelCase one alone is not overridden by the other's default.
    '''
    if alias is not None and alias != '':
        return sr._as_bool(alias)
    return sr._as_bool(primary)


def _page(limit, offset):
    ''' Coerce request limit/offset to safe ints: limit in [1, _MAX_LIMIT],
        offset >= 0. '''
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        limit = _DEFAULT_LIMIT
    try:
        offset = int(offset)
    except (TypeError, ValueError):
        offset = 0
    return max(1, min(limit, _MAX_LIMIT)), max(0, offset)


def _sort(sort_by, sort_dir):
    ''' Coerce request sort_by/sort_dir to a supported pair, never throwing. '''
    sort_by = str(sort_by or '').strip().lower()
    if sort_by not in CATALOGUE_SORT_FIELDS:
        sort_by = _DEFAULT_SORT_BY
    sort_dir = 'asc' if str(sort_dir or '').strip().lower() == 'asc' else 'desc'
    return sort_by, sort_dir


def _page_envelope(items, total, limit, offset, **extra):
    return {'total': total, 'limit': limit, 'offset': offset, 'items': items, **extra}
