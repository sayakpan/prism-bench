'''
Development-style matching — narrows Development Style down to the rows a brand
has actually developed in a given product category, and prices them.

The caller states a category in its own words ("tshirt", "pant", "loungewear").
Development Style stores the buyer's own merchandising vocabulary in `brick`
("Knit Tops", "Sleepset", "Tanks", "Bottom", ...), which those words do not
string-match. So the vocabulary is read off the table and an LLM maps the request
onto it — "tshirt" reaching "Knit Tops" and "Basic Tee" but not "Sleepset". A
token-overlap heuristic covers an LLM failure so the endpoint degrades rather
than errors, and `meta.matchMode` says which path ran.

Three price anchors come back, from three different sources:

    targetRetail      the brand's shelf price, in USD, from Brand Master Data
    nitva_volza_data  the brand's per-piece factory price, in USD, from customs
                      shipments (NitvaShipment)
    targetFob         the brand's per-piece ask, in INR, from this buyer's own
                      development sheet (Development Style.tentative_fob)

The first two are `garment_matching`'s own anchors, called here rather than
reimplemented, so a caller gets byte-identical objects to
`prism.api.garment_matching.match_garments`. The third is local: it is what *we*
quoted this buyer, not what the market pays, which is why it is the only one in
rupees and the only one that moves with the GSM filter below.

GSM is a secondary narrowing on the Development Style side only. A brick like
"Knit Tops" spans a 150-300 GSM range, so once a category matches more than
`_GSM_FILTER_THRESHOLD` styles the caller's `gsm` narrows it to a tolerance band
around that weight. Below the threshold it is left alone — narrowing a handful of
rows costs more coverage than it buys precision. `meta.gsmFilter` reports whether
it engaged and what it removed.

Endpoint:
    POST /api/method/prism.api.development_matching.match_styles
    Header: X-Auth-Token: <jwt>            (required — see auth_required)
'''

import json
import re
import difflib
import hashlib

import frappe

from prism.auth.authenticator import auth_required
import prism.api.llm as llm
import prism.api.garment_matching as gm

DOCTYPE = 'Development Style'

# Category mapping is a small classification task, not generation — same model
# tier and reasoning as garment_matching._RANK_MODEL.
# Overridable per-site via `development_matching_model` in site_config.json.
_MATCH_MODEL = 'claude-haiku-4-5-20251001'
_MATCH_MAX_TOKENS = 1000

_DEFAULT_LIMIT = 20
_MAX_LIMIT = 100

# Category mappings are cached so identical requests price identically — see
# _match_categories. Mirrors surplus_recommender.COLOR_MAP_CACHE_TTL.
_CATEGORY_MAP_CACHE_TTL = 60 * 60 * 6

# GSM narrowing engages only above this many category-matched styles, and spans
# this much either side of the requested weight. 15% turns a 260 GSM request
# into 221-299, which is about one fabric weight class either way.
_GSM_FILTER_THRESHOLD = 10
_GSM_TOLERANCE_PCT = 15

# Respellings that let garment_matching's style taxonomy see a glued category —
# see _market_style_category. Only for the market anchors; the development side
# uses the LLM and needs none of this.
_MARKET_CATEGORY_ALIASES = {
    'tshirt': ('t-shirt',),
    'tshirts': ('t-shirt',),
    'tees': ('tee',),
    'tanktop': ('tank top',),
    'tanktops': ('tank top',),
    'coord': ('co ord',),
    'coords': ('co ord',),
    'nightwear': ('sleepwear',),
    'loungewear': ('lounge set', 'sleepwear'),
    'sweatpant': ('jogger',),
    'sweatpants': ('jogger',),
}

# Words that carry no signal when falling back to token overlap.
_STOPWORDS = {'the', 'and', 'for', 'with', 'set', 'sets', 'wear'}

# Hand-written bridges for the fallback path only. The LLM needs none of these;
# they exist so a category whose words never overlap the buyer's vocabulary
# ("tshirt" vs "Knit Tops") still resolves when the LLM is unreachable.
_FALLBACK_HINTS = {
    'tshirt': ('knit', 'tee', 'top'),
    'tshirts': ('knit', 'tee', 'top'),
    't-shirt': ('knit', 'tee', 'top'),
    'tee': ('knit', 'tee', 'top'),
    'top': ('top', 'tee', 'knit', 'tank', 'camisole'),
    'tops': ('top', 'tee', 'knit', 'tank', 'camisole'),
    'pant': ('bottom', 'trouser', 'pant', 'capri', 'pyjama'),
    'pants': ('bottom', 'trouser', 'pant', 'capri', 'pyjama'),
    'bottom': ('bottom', 'trouser', 'pant', 'capri', 'short', 'pyjama'),
    'bottoms': ('bottom', 'trouser', 'pant', 'capri', 'short', 'pyjama'),
    'dress': ('dress',),
    'sleepwear': ('sleep', 'night', 'lounge', 'pyjama'),
    'loungewear': ('sleep', 'night', 'lounge', 'pyjama'),
}


@frappe.whitelist(allow_guest=True, methods=['POST'])
@auth_required
def match_styles(payload=None):
    '''
    Find a brand's Development Style rows for a product category, and price them.

    payload (dict or JSON string):
        brand           dict  {"id": "shein", "name": "Shein"} — `id` is the
                              Brand docname; `name` is used only when `id`
                              misses, matched fuzzily against Brand.brand
        productCategory str   free text, e.g. "tshirt" / "top" / "pant" / "bottom"
        gsm             num   optional, secondary narrowing on the development
                              rows — see `meta.gsmFilter`
        genders         list  optional, passed to the Brand Master Data and
                              shipment anchors exactly as match_garments does
        limit           int   optional, default 20, capped at 100

    Returns {'success': True, 'styles': [...], 'targetRetail': {...},
    'nitva_volza_data': {...}, 'targetFob': {...}, 'meta': {...}}.

    `targetRetail` and `nitva_volza_data` are garment_matching's own anchors,
    called rather than reimplemented — the same objects
    `match_garments` returns under `targetRetail` and `targetFob`. They read
    Brand Master Data and NitvaShipment, so a brand present in the development
    sheet but absent from those sources comes back with a `reason` rather than a
    number, and vice versa.

    `targetFob` is the local one, off this buyer's development sheet in INR.
    `styles` is priced-first, and comes back empty rather than broadened to
    other brands or categories when nothing matches.
    '''
    payload = _as_dict(payload)
    brand_input = _as_dict(payload.get('brand'))
    product_category = (payload.get('productCategory') or '').strip()
    genders = [g for g in _as_list(payload.get('genders')) if g]
    gsm = _as_number(payload.get('gsm'))

    try:
        limit = min(int(payload.get('limit') or _DEFAULT_LIMIT), _MAX_LIMIT)
    except (TypeError, ValueError):
        limit = _DEFAULT_LIMIT

    brand = _resolve_brand(brand_input)

    vocabulary = _brick_vocabulary(brand) if brand else []
    categories, match_mode = _match_categories(product_category, vocabulary)

    # A category that maps onto nothing the buyer develops returns nothing —
    # never the brand's whole shelf. Only an *omitted* category is unfiltered.
    unmatched = bool(product_category) and bool(vocabulary) and not categories

    gsm_filter = _resolve_gsm_filter(brand, categories, gsm) if brand else _gsm_filter_off(gsm)
    band = gsm_filter['band'] if gsm_filter['applied'] else None

    styles = [] if (not brand or unmatched) else _fetch_styles(brand, categories, band, limit)
    target_fob = _target_fob(brand, categories, product_category, vocabulary, band)

    # The market-side anchors, straight from garment_matching so the objects are
    # identical to the ones match_garments returns.
    retail, nitva, anchor_meta = _market_anchors(brand, product_category, genders)

    return {
        'success': True,
        'styles': styles,
        'targetRetail': retail,
        'nitva_volza_data': nitva,
        'targetFob': target_fob,
        'meta': {
            'resolvedBrand': brand,
            'brandInput': brand_input or None,
            'productCategory': product_category or None,
            'genders': genders,
            'brickVocabulary': vocabulary,
            'matchedCategories': list(categories),
            'matchMode': match_mode,
            'gsmFilter': gsm_filter,
            'marketAnchors': anchor_meta,
            'returned': len(styles),
            'limit': limit,
        },
    }


# =====================================================================
# market anchors — Brand Master Data + customs shipments
# =====================================================================

def _market_anchors(brand, product_category, genders):
    '''
    (targetRetail, nitva_volza_data, meta) from garment_matching.

    Both are called, not reimplemented, so the objects match what
    match_garments returns for the same brand. Scope is deliberately the input
    brand alone — `_resolve_brand_pool` also resolves competitors, and those are
    dropped here because these anchors answer "where does our own shelf sit".

    The product category is passed through as a single style category, which is
    the shape both functions already take from a Moodboard brief.
    '''
    empty_retail = {
        'value': None, 'currency': 'USD', 'basis': None,
        'reason': 'brand_not_resolved', 'stats': None,
        'coverage': {}, 'currencyMix': {}, 'ratesUsed': {},
    }
    empty_nitva = {
        'value': None, 'currency': 'USD', 'basis': None,
        'reason': 'brand_not_resolved', 'stats': None, 'funnel': {},
        'consigneePattern': None, 'matchedCategories': [],
    }
    if not brand:
        return empty_retail, empty_nitva, {'brandNamesUsed': [], 'bmdBrandsMatched': []}

    market_category = _market_style_category(product_category)
    style_categories = [market_category] if market_category else []

    try:
        resolution = gm._resolve_brand_pool([brand])
        own = resolution['own_bmd_brands']

        # Only the brand's own rows are fetched: _target_retail filters to `own`
        # anyway, so pulling competitors would be wasted IO.
        candidates = gm._fetch_candidates(own, genders)

        retail = gm._target_retail(candidates, own, style_categories)
        nitva = gm._target_fob(resolution['input_brand_names'], genders, style_categories)

        return retail, nitva, {
            'brandNamesUsed': resolution['input_brand_names'],
            'bmdBrandsMatched': sorted(own),
            'bmdRowsScanned': len(candidates),
            'styleCategoryUsed': market_category,
        }

    except Exception:
        # The market anchors must never cost the caller its development styles.
        frappe.log_error(frappe.get_traceback(), 'development_matching._market_anchors')
        empty_retail['reason'] = 'market_anchor_failed'
        empty_nitva['reason'] = 'market_anchor_failed'
        return empty_retail, empty_nitva, {'error': 'market_anchor_failed'}


def _market_style_category(product_category):
    '''
    The caller's category respelled so garment_matching's style taxonomy can see
    it, or unchanged when it already can.

    `_STYLE_SYNONYMS` is keyed on separated spellings ("t shirt", "tank top"), so
    a glued one resolves to nothing and the anchors come back `no_style_match` —
    "tshirt" is the one word in the documented input set that hits this, while
    "t-shirt", "tee", "top", "pant", "bottom", "sleepwear" and "dress" all pass
    through fine. Respelling here rather than extending `_STYLE_SYNONYMS` keeps
    `match_garments` behaviour untouched.

    Falls back to the original when nothing resolves, so an unknown category
    still reports the honest `no_style_match` instead of being coerced.
    '''
    raw = (product_category or '').strip()
    if not raw:
        return raw

    try:
        if gm._norm_style_key(raw) in gm._STYLE_SYNONYMS:
            return raw

        candidates = list(_MARKET_CATEGORY_ALIASES.get(raw.lower(), ()))
        # Generic rescue for the same shape the aliases cover: a single leading
        # letter glued to a word ("tshirt" -> "t shirt", "vneck" -> "v neck").
        glued = re.fullmatch(r'([a-z])([a-z]{3,})', raw.lower())
        if glued:
            candidates.append(f'{glued.group(1)} {glued.group(2)}')

        for candidate in candidates:
            if gm._norm_style_key(candidate) in gm._STYLE_SYNONYMS:
                return candidate
    except Exception:
        frappe.log_error(frappe.get_traceback(), 'development_matching._market_style_category')

    return raw


# =====================================================================
# brand + category resolution
# =====================================================================

def _resolve_brand(brand_input):
    '''
    Brand docname for the request, or None. `id` wins when it exists; `name`
    is a fuzzy fallback because callers hold a display string ("Shein") more
    often than the slug ("shein").
    '''
    if not brand_input:
        return None

    brand_id = (brand_input.get('id') or '').strip()
    if brand_id and frappe.db.exists('Brand', brand_id):
        return brand_id

    name = (brand_input.get('name') or '').strip()
    if not name:
        return None

    # Exact title match first, then closest slug — cheaper and more predictable
    # than going straight to difflib on the whole table.
    exact = frappe.db.get_value('Brand', {'brand': name}, 'name')
    if exact:
        return exact

    all_brands = frappe.get_all('Brand', pluck='name', ignore_permissions=True)
    close = difflib.get_close_matches(_slug(name), all_brands, n=1, cutoff=0.85)
    return close[0] if close else None


def _brick_vocabulary(brand):
    '''
    The distinct `brick` values this brand actually has rows for.

    Read off the table rather than hard-coded, so a bucket the buyer starts
    using is picked up without a code change — same reasoning as
    garment_matching._nitva_categories.
    '''
    values = frappe.get_all(
        DOCTYPE, filters={'brand': brand}, pluck='brick',
        distinct=True, ignore_permissions=True,
    )
    return sorted({v.strip() for v in values if v and v.strip()})


def _match_categories(product_category, vocabulary):
    '''
    ( matched vocabulary values, mode ) for a free-text category.

    Returns every bucket the request plausibly covers, not just the best one:
    "top" legitimately spans "Knit Tops", "Tanks" and "Camisole", and the price
    anchor is more honest over all of them than over an arbitrary pick.
    '''
    if not product_category or not vocabulary:
        return (), 'unfiltered'

    # An exact hit needs no model.
    for value in vocabulary:
        if value.lower() == product_category.lower():
            return (value,), 'exact'

    # Cached on the request + the vocabulary it was matched against, so a new
    # brick invalidates it. Not just a latency saving: the model is sampled at
    # its default temperature, so identical requests otherwise drift — measured
    # at roughly one call in five pulling "tshirt" into Tanks as well, which
    # moves the anchors by ~6%. Cached, a category prices the same all day.
    cache_key = 'dev_match_categories:' + _digest(product_category, vocabulary)
    try:
        cached = frappe.cache().get_value(cache_key)
        if isinstance(cached, dict) and 'categories' in cached:
            return tuple(cached['categories']), cached.get('mode', 'llm') + '_cached'
    except Exception:
        pass

    matched = _llm_match_categories(product_category, vocabulary)
    mode = 'llm'
    if matched is None:
        matched = _fallback_match_categories(product_category, vocabulary)
        mode = 'heuristic_fallback'

    try:
        frappe.cache().set_value(
            cache_key, {'categories': list(matched), 'mode': mode},
            expires_in_sec=_CATEGORY_MAP_CACHE_TTL,
        )
    except Exception:
        pass

    return tuple(matched), mode


def _llm_match_categories(product_category, vocabulary):
    '''
    Map the requested category onto the buyer's vocabulary. Returns None on any
    LLM failure so the caller can fall back — an empty list is a real answer
    ("nothing in this vocabulary covers that"), None is a broken call.
    '''
    model = frappe.get_site_config().get('development_matching_model') or _MATCH_MODEL

    system_prompt = (
        'You map a garment product category onto a buyer\'s own merchandising '
        'vocabulary.\n\n'
        'You are given a requested category and a list of vocabulary values. '
        'Return every value the request plausibly covers — a request can span '
        'several. Be inclusive within reason and strict across garment '
        'boundaries: a top request must never return bottoms or sleepwear sets, '
        'and vice versa.\n\n'
        'Respond with ONLY a JSON array, no prose:\n'
        '[{"category": "<exact value from the list>", "why": "<short reason>"}]\n\n'
        'Use the vocabulary values verbatim. Return [] if none apply.'
    )
    user_prompt = (
        f'Requested category: {product_category}\n\n'
        f'Vocabulary values:\n{json.dumps(vocabulary, indent=1)}'
    )

    try:
        raw = llm.get_claude_response(
            system_prompt, user_prompt, ret_type='list',
            model=model, max_tokens=_MATCH_MAX_TOKENS,
        )
    except Exception:
        frappe.log_error(frappe.get_traceback(), 'development_matching._llm_match_categories')
        return None

    # Only values that are actually in the vocabulary survive — the model is not
    # trusted to avoid inventing a bucket.
    allowed = {v.lower(): v for v in vocabulary}
    matched, seen = [], set()
    for item in raw or []:
        value = (item or {}).get('category') if isinstance(item, dict) else item
        key = (value or '').strip().lower()
        if key in allowed and key not in seen:
            seen.add(key)
            matched.append(allowed[key])
    return matched


def _fallback_match_categories(product_category, vocabulary):
    ''' Token overlap plus _FALLBACK_HINTS, for when the LLM call fails. '''
    words = _words(product_category)
    probes = set(words)
    for word in words:
        probes.update(_FALLBACK_HINTS.get(word, ()))
    probes -= _STOPWORDS
    if not probes:
        return []

    matched = []
    for value in vocabulary:
        tokens = _words(value)
        if any(
            probe == token or token.startswith(probe) or probe.startswith(token)
            for probe in probes for token in tokens
            if len(probe) >= 3 and len(token) >= 3
        ):
            matched.append(value)
    return matched


# =====================================================================
# GSM narrowing
# =====================================================================

def _gsm_filter_off(gsm, reason='no_brand'):
    return {
        'applied': False, 'gsm': gsm, 'tolerancePct': _GSM_TOLERANCE_PCT,
        'band': None, 'threshold': _GSM_FILTER_THRESHOLD,
        'candidates': 0, 'remaining': 0, 'droppedNoGsm': 0, 'reason': reason,
    }


def _resolve_gsm_filter(brand, categories, gsm):
    '''
    Decide once whether GSM narrows this request, so the styles list and the
    development FOB anchor stay in step.

    Only engages above `_GSM_FILTER_THRESHOLD` category matches: below it,
    narrowing costs more coverage than it buys precision. Styles carrying no GSM
    at all are dropped when it does engage — the point is narrowing, and an
    unknown weight cannot be said to be in band — so `droppedNoGsm` reports how
    many, which is the number that explains a surprisingly small result.
    '''
    if not gsm or gsm <= 0:
        return _gsm_filter_off(gsm, 'no_gsm_supplied')

    result = _gsm_filter_off(gsm, None)

    conds, vals = ['brand = %s'], [brand]
    if categories:
        conds.append('brick IN %s')
        vals.append(tuple(categories))

    candidates = _style_count(conds, vals)
    result['candidates'] = candidates

    if candidates <= _GSM_FILTER_THRESHOLD:
        result['reason'] = 'below_threshold'
        return result

    low = gsm * (1 - _GSM_TOLERANCE_PCT / 100)
    high = gsm * (1 + _GSM_TOLERANCE_PCT / 100)

    remaining = _style_count(conds + ['gsm BETWEEN %s AND %s'], vals + [low, high])
    no_gsm = _style_count(conds + ['(gsm IS NULL OR gsm <= 0)'], vals)

    result.update({
        'applied': True,
        'band': [_round2(low), _round2(high)],
        'remaining': remaining,
        'droppedNoGsm': no_gsm,
        'reason': None,
    })
    return result


# =====================================================================
# styles
# =====================================================================

def _fetch_styles(brand, categories, band, limit):
    '''
    The matched rows, priced ones first. An unpriced style is still a real
    development for this category, so it is returned rather than filtered out —
    it just contributes nothing to the anchor.
    '''
    filters = {'brand': brand}
    if categories:
        filters['brick'] = ['in', list(categories)]
    if band:
        filters['gsm'] = ['between', band]

    rows = frappe.get_all(
        DOCTYPE, filters=filters,
        fields=[
            'name', 'style_name', 'brick', 'buyer_brick_path', 'collection',
            'development_date', 'month', 'actual_image', 'fabric', 'shade',
            'gsm', 'quantity_kg', 'mrp', 'tentative_fob', 'design_status',
            'development_status', 'source_ref',
        ],
        order_by='tentative_fob desc, creation asc',
        limit_page_length=limit, ignore_permissions=True,
    )

    return [{
        'id': r['name'],
        'styleName': r['style_name'],
        'brick': r['brick'],
        'buyerBrickPath': r['buyer_brick_path'],
        'collection': r['collection'],
        'developmentDate': str(r['development_date']) if r['development_date'] else None,
        'month': r['month'],
        'actualImage': r['actual_image'],
        'fabric': r['fabric'],
        'shade': r['shade'],
        'gsm': _num(r['gsm']),
        'quantityKg': _num(r['quantity_kg']),
        'mrp': _num(r['mrp']),
        'tentativeFob': _num(r['tentative_fob']),
        'designStatus': r['design_status'],
        'developmentStatus': r['development_status'],
        'sourceRef': r['source_ref'],
    } for r in rows]


# =====================================================================
# target FOB — this buyer's own development sheet, INR
# =====================================================================

def _target_fob(brand, categories, product_category, vocabulary, band):
    '''
    The brand's average Tentative FOB per piece, in rupees, for the category.

    `funnel` reports the row count surviving each stage. It is the thing that
    explains a zero: a brand with no development rows, a category nobody has
    costed yet, a GSM band nothing sits in, and a category outside the buyer's
    vocabulary all read as `value: None` otherwise, and the funnel says which.
    '''
    result = {
        'value': None,
        'currency': 'INR',
        'basis': None,
        'reason': None,
        'stats': None,
        'funnel': {},
        'matchedCategories': list(categories),
    }

    try:
        if not frappe.db.exists('DocType', DOCTYPE):
            result['reason'] = 'development_data_unavailable'
            return result

        if not brand:
            result['reason'] = 'brand_not_resolved'
            return result

        # Each stage adds predicates to the one before it, so the counts read as
        # a funnel rather than as five unrelated numbers.
        stages = [('allStyles', [], [])]

        conds, vals = ['brand = %s'], [brand]
        stages.append(('brandMatched', list(conds), list(vals)))

        conds += ['tentative_fob > 0']
        stages.append(('pricedStyles', list(conds), list(vals)))

        basis = 'brand'
        if categories:
            conds.append('brick IN %s')
            vals.append(tuple(categories))
            basis = 'brand_category'
        stages.append(('categoryMatched', list(conds), list(vals)))

        if band:
            conds.append('gsm BETWEEN %s AND %s')
            vals += list(band)
            basis = f'{basis}_gsm'
        stages.append(('gsmMatched', list(conds), list(vals)))

        result['basis'] = basis
        for label, stage_conds, stage_vals in stages:
            result['funnel'][label] = _style_count(stage_conds, stage_vals)

        # A request whose words map onto nothing in the buyer's vocabulary is a
        # different failure from one where the brand simply never costed them.
        if product_category and vocabulary and not categories:
            result['reason'] = 'category_not_in_development_taxonomy'
            return result

        reason = _first_empty_stage(result['funnel'])
        if reason:
            result['reason'] = reason
            return result

        row = frappe.db.sql(
            f'''SELECT COUNT(*) n,
                       MIN(tentative_fob) mn,
                       MAX(tentative_fob) mx,
                       AVG(tentative_fob) av,
                       SUM(tentative_fob * quantity_kg)
                           / NULLIF(SUM(CASE WHEN quantity_kg > 0
                                             THEN quantity_kg END), 0) avw
                FROM `tab{DOCTYPE}`
                WHERE {" AND ".join(conds)}''',
            vals, as_dict=True,
        )[0]

        result['value'] = _round2(row['av'])
        result['stats'] = {
            'count': int(row['n'] or 0),
            'min': _round2(row['mn']),
            'max': _round2(row['mx']),
            'avg': _round2(row['av']),
            # Every style counts equally in `avg`, so a 26 kg trial pulls as hard
            # as a 337 kg buy. The weighted figure is the same population priced
            # by volume — carried alongside rather than replacing `avg`. Styles
            # with no quantity are outside it entirely, which is why its
            # population can be smaller than `count`.
            'avgWeighted': _round2(row['avw']),
        }
        return result

    except Exception:
        # A pricing failure must not cost the caller its style list.
        frappe.log_error(frappe.get_traceback(), 'development_matching._target_fob')
        result['reason'] = 'development_query_failed'
        return result


def _style_count(conds, vals):
    where = f'WHERE {" AND ".join(conds)}' if conds else ''
    return frappe.db.sql(
        f'SELECT COUNT(*) FROM `tab{DOCTYPE}` {where}', vals,
    )[0][0]


def _first_empty_stage(funnel):
    ''' The earliest funnel stage that emptied out, as a reason code — that is
    the stage worth reporting, since everything after it is empty by consequence. '''
    for label, reason in (
        ('allStyles', 'development_data_unavailable'),
        ('brandMatched', 'brand_not_in_development_styles'),
        ('pricedStyles', 'no_priced_styles'),
        ('categoryMatched', 'no_category_match'),
        ('gsmMatched', 'no_gsm_match'),
    ):
        if not funnel.get(label):
            return reason
    return None


# =====================================================================
# helpers
# =====================================================================

def _as_dict(value):
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            return {}
    return value if isinstance(value, dict) else {}


def _as_list(value):
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            return [value] if value.strip() else []
    if isinstance(value, (list, tuple)):
        return list(value)
    return [value] if value else []


def _as_number(value):
    try:
        return float(value) if value not in (None, '') else None
    except (TypeError, ValueError):
        return None


def _digest(product_category, vocabulary):
    raw = json.dumps([(product_category or '').strip().lower()] + list(vocabulary), sort_keys=True)
    return hashlib.sha1(raw.encode('utf-8')).hexdigest()


def _words(text):
    return [w for w in re.split(r'[^a-z0-9]+', (text or '').lower()) if w]


def _slug(text):
    return re.sub(r'[^a-z0-9]+', '-', (text or '').lower()).strip('-')


def _num(value):
    return float(value) if value is not None else None


def _round2(value):
    return round(float(value), 2) if value is not None else None
