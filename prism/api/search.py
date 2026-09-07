'''
Universal ("god") search + AI natural-language search.

Two JWT-protected endpoints, layered:

  global_search(q, types, limit_per_type)
      Literal search: runs `q` as a case-insensitive LIKE across every domain
      and returns matches grouped by type.

  smart_search(q, limit_per_type)
      Phase-1 natural-language search. An LLM "query planner" turns a
      human-language query ("lightweight cotton fabrics under 150 GSM in
      indigo") into the SAME structured params the list endpoints already
      accept, grounded in the live catalog vocabulary. The plan is strictly
      validated/clamped, then dispatched through the same scoped searchers as
      global_search. On any failure it falls back to a literal global_search,
      so it is never worse than today.

Access safety is the whole point of this module: it NEVER re-implements
visibility rules. For every scoped domain it drives the *existing* list
endpoint (or that endpoint's permission helper), so a result can only appear
here if the caller could already see it through the dedicated listing:

  - moodboard / style : prism.api.moodboard_v2 list endpoints, which enforce the
                        brand-link / "All Moodboard Permission" scope and pin
                        brand users to the published set.
  - request           : brand users are hard-filtered to their own brand's
                        requests (same rule as request.list_requests /
                        get_permission_query_conditions); internal/PSL see all.
  - fabric / sample   : public master/inventory data (Moodboard Dyed Fabric /
                        Sample Request), already exposed to any authenticated
                        user by fabric.list_all / garment.list_all.

The LLM only ever produces *filter params* — it never sees data, never touches
the DB, and every value it emits is clamped to the known vocabulary before use.

Endpoints:
    GET/POST /api/method/prism.api.search.global_search
    GET/POST /api/method/prism.api.search.smart_search
    Header:  X-Auth-Token: <jwt>            (required — see auth_required)
'''

import frappe

from prism.auth.authenticator import auth_required
import prism.api.util as util
import prism.api.llm as llm
import prism.api.moodboard_v2 as mb2
import prism.api.fabric as fabric_api
import prism.api.garment as garment_api

REQUEST_DOCTYPE = 'Request'
FABRIC_DOCTYPE = 'Moodboard Dyed Fabric'
STYLE_DOCTYPE = 'Moodboard Style'
SAMPLE_DOCTYPE = 'Sample Request'
MOODBOARD_DOCTYPE = 'Moodboard'
_TYPE_FROM_DB = {'Quote': 'quote', 'Sample': 'sample'}

# Aligned with moodboard_v2._SEARCH_MIN_CHARS: below this the moodboard/style
# listers ignore the search term and would return their whole visible set, so we
# refuse short queries globally to keep every group consistent.
_MIN_CHARS = 3
_DEFAULT_LIMIT = 6
_MAX_LIMIT = 20

# Fixed display order + labels for the groups.
_GROUPS = [
    ('moodboard',      'Moodboards'),
    ('style',          'Moodboard Styles'),
    ('fabric',         'Fabrics'),
    ('sample_request', 'Sample Requests'),
    ('request',        'Requests'),
]
_ALL_TYPES = [t for t, _ in _GROUPS]

# Live-vocabulary cache (distinct catalog values fed to the planner). Rebuilt
# hourly — the value set changes slowly and a stale hour is harmless.
_VOCAB_CACHE_KEY = 'prism:smart_search:vocab'
_VOCAB_TTL_SEC = 3600
_GSM_MAX = 2000.0
_SEARCH_TEXT_MAX = 200

# Planning is a tiny structured-extraction task, so use Anthropic's fast model and
# a small output cap instead of the heavy default in llm.py (which is tuned for
# long generative work). Overridable per-site via `smart_search_model` in
# site_config.json. The plan JSON is ~150 tokens; 700 leaves generous headroom.
_PLANNER_MODEL = 'claude-haiku-4-5-20251001'
_PLANNER_MAX_TOKENS = 700


# =====================================================================
# Endpoint 1 — literal grouped search
# =====================================================================

@frappe.whitelist(allow_guest=True, methods=['GET', 'POST'])
@auth_required
def global_search(q=None, types=None, limit_per_type=_DEFAULT_LIMIT):
    '''
    Run `q` as a literal LIKE across every (requested) domain, grouped by type.
    See module docstring for the response shape. Short queries (< 3 chars) return
    success with empty groups. A failure in one domain never breaks the others.
    '''
    query = (q or '').strip()
    limit = _clamp_limit(limit_per_type)
    wanted = _wanted_types(types)

    if len(query) < _MIN_CHARS:
        return {
            'success': True,
            'query': query,
            'minChars': _MIN_CHARS,
            'groups': _empty_groups(wanted),
        }

    groups = _run_groups(wanted, limit, search_text=query, plan_filters={})
    return {'success': True, 'query': query, 'groups': groups}


# =====================================================================
# Endpoint 2 — AI natural-language search (Phase 1: query planner)
# =====================================================================

@frappe.whitelist(allow_guest=True, methods=['GET', 'POST'])
@auth_required
def smart_search(q=None, limit_per_type=_DEFAULT_LIMIT, debug=False):
    '''
    Natural-language search. Plans the query with an LLM into structured filters,
    validates them against the live vocabulary, then dispatches through the same
    scoped searchers as global_search.

    Returns global_search's envelope plus:
        'mode'           : 'ai' | 'literal' (literal = planner skipped or failed)
        'interpretation' : human sentence describing what was searched (or None)
        'appliedFilters' : the validated per-type filter dict the UI can render
                           as editable chips
        'confidence'     : planner self-reported confidence (0-1), when in ai mode
    With debug truthy, also returns 'rawPlan' (the pre-validation LLM output).
    '''
    query = (q or '').strip()
    limit = _clamp_limit(limit_per_type)
    debug = frappe.parse_json(debug) if isinstance(debug, str) else bool(debug)

    if len(query) < _MIN_CHARS:
        return {
            'success': True, 'mode': 'literal', 'query': query,
            'minChars': _MIN_CHARS, 'interpretation': None,
            'appliedFilters': {}, 'groups': _empty_groups(set(_ALL_TYPES)),
        }

    # Single-word queries carry nothing to parse — a literal LIKE is as good and
    # far cheaper/faster than a round-trip to the model.
    if len(query.split()) < 2:
        return _literal_fallback(query, limit)

    plan, raw_plan = _plan_query(query)
    if not plan or not plan.get('types'):
        out = _literal_fallback(query, limit)
        if debug:
            out['rawPlan'] = raw_plan
        return out

    groups = _run_groups(set(plan['types']), limit,
                         search_text=None, plan_filters=plan['filters'])
    out = {
        'success': True,
        'mode': 'ai',
        'query': query,
        'interpretation': plan.get('interpretation'),
        'appliedFilters': plan['filters'],
        'confidence': plan.get('confidence'),
        'groups': groups,
    }
    if debug:
        out['rawPlan'] = raw_plan
    return out


def _literal_fallback(query, limit):
    ''' Literal grouped search wrapped in the smart_search envelope. '''
    groups = _run_groups(set(_ALL_TYPES), limit, search_text=query, plan_filters={})
    return {
        'success': True, 'mode': 'literal', 'query': query,
        'interpretation': None, 'appliedFilters': {}, 'groups': groups,
    }


# =====================================================================
# Shared dispatch — build groups from a search_text and/or planned filters
# =====================================================================

def _run_groups(wanted, limit, search_text, plan_filters):
    '''
    For each requested type, resolve (total, items). `search_text` is the literal
    term (literal mode); `plan_filters[type]` holds the AI-derived structured
    filters (ai mode), each possibly carrying its own residual 'search_text'.
    '''
    searchers = {
        'moodboard': _search_moodboards,
        'style': _search_styles,
        'fabric': _search_fabrics,
        'sample_request': _search_sample_requests,
        'request': _search_requests,
    }
    groups = []
    for t, label in _GROUPS:
        if t not in wanted:
            continue
        extra = (plan_filters or {}).get(t) or {}
        # AI mode: use the residual search_text the planner assigned to this type
        # (may be ''); literal mode: the raw query for every type.
        text = search_text if search_text is not None else (extra.get('search_text') or '')
        try:
            total, items = searchers[t](text, limit, extra)
        except Exception:
            frappe.log_error(frappe.get_traceback(), f'search._run_groups/{t}')
            total, items = 0, []
        groups.append({'type': t, 'label': label, 'total': total, 'items': items})
    return groups


# =====================================================================
# Per-domain searchers — (search_text, limit, extra) -> (total, [items]).
# Scoping is delegated to the existing endpoints/helpers; we only pass through
# validated structured filters and reshape the results.
# =====================================================================

def _search_moodboards(search_text, limit, extra=None):
    extra = extra or {}
    filters = {}
    if search_text:
        filters['search'] = search_text
    if extra.get('season'):
        filters['season'] = extra['season']
    if extra.get('gender'):
        filters['gender'] = extra['gender']
    if extra.get('status'):
        filters['status'] = extra['status']

    res = mb2.list_moodboards(filters=filters, limit=limit, offset=0)
    items = [{
        'type': 'moodboard',
        'id': it.get('id'),
        'title': it.get('title') or '(untitled)',
        'subtitle': _join(it.get('season'), it.get('status')),
        'thumbnail': it.get('thumbnail'),
        'meta': {
            'status': it.get('status'),
            'season': it.get('season'),
            'brands': it.get('brands') or [],
            'access': it.get('access'),
        },
    } for it in (res.get('items') or [])]
    return res.get('total', len(items)), items


def _search_styles(search_text, limit, extra=None):
    extra = extra or {}
    filters = {}
    if search_text:
        filters['search'] = search_text
    for key in ('gender', 'productCategory', 'fabricQuality', 'colour'):
        if extra.get(key):
            filters[key] = extra[key]     # list — list_moodboard_styles handles IN

    res = mb2.list_moodboard_styles(filters=filters, limit=limit, offset=0)
    items = []
    for it in (res.get('items') or []):
        attrs = it.get('attrs') or {}
        title = attrs.get('garment_name') or attrs.get('product_category') \
            or it.get('moodboardTitle') or '(untitled style)'
        items.append({
            'type': 'style',
            'id': it.get('id'),
            'title': title,
            'subtitle': _join(attrs.get('product_category'), attrs.get('element_colour')),
            'thumbnail': it.get('image') or None,
            'meta': {
                'moodboard': it.get('moodboard'),
                'moodboardTitle': it.get('moodboardTitle'),
                'gender': attrs.get('gender'),
                'fabricQuality': attrs.get('fabric_quality'),
            },
        })
    return res.get('total', len(items)), items


def _search_fabrics(search_text, limit, extra=None):
    extra = extra or {}
    res = fabric_api.list_all(
        search_text=search_text or None,
        qualities=extra.get('qualities') or [],
        blends=extra.get('blends') or [],
        finishes=extra.get('finishes') or [],
        shade_codes=extra.get('shade_codes') or [],
        min_gsm=extra.get('min_gsm'),
        max_gsm=extra.get('max_gsm'),
        page=1, page_size=limit,
    )
    if not res.get('success'):
        return 0, []
    items = [{
        'type': 'fabric',
        'id': row.get('id'),
        'title': row.get('custom_fabric_name') or row.get('code') or row.get('quality') or '(fabric)',
        'subtitle': _join(row.get('quality'), row.get('blend'),
                          f"{row.get('gsm')} GSM" if row.get('gsm') else None),
        'thumbnail': row.get('thumbnail') or row.get('image_url'),
        'meta': {
            'code': row.get('code'),
            'quality': row.get('quality'),
            'blend': row.get('blend'),
            'gsm': row.get('gsm'),
            'finish': row.get('finish'),
        },
    } for row in (res.get('data') or [])]
    total = (res.get('pagination') or {}).get('total_count', len(items))
    return total, items


def _search_sample_requests(search_text, limit, extra=None):
    extra = extra or {}
    res = garment_api.list_all(
        search_text=search_text or None,
        genders=extra.get('genders') or [],
        categories=extra.get('categories') or [],
        styles=extra.get('styles') or [],
        page=1, page_size=limit,
    )
    if not res.get('success'):
        return 0, []
    items = [{
        'type': 'sample_request',
        'id': row.get('id'),
        'title': row.get('gsr_no') or row.get('style') or '(sample request)',
        'subtitle': _join(row.get('style'), row.get('fabric_quality'), row.get('element_colour')),
        'thumbnail': _first_image(row.get('image_urls')),
        'meta': {
            'gsrNo': row.get('gsr_no'),
            'gender': row.get('gender'),
            'category': row.get('category'),
            'style': row.get('style'),
            'fabricQuality': row.get('fabric_quality'),
        },
    } for row in (res.get('data') or [])]
    total = (res.get('pagination') or {}).get('total_count', len(items))
    return total, items


def _search_requests(search_text, limit, extra=None):
    '''
    Requests have no free-text lister today, so search directly — but under the
    SAME scope rule as request.list_requests: a brand caller is hard-filtered to
    its own brand's requests; internal/PSL callers (no brand) see all.
    '''
    if not search_text:
        return 0, []
    _, brand = util.get_current_user_id(), util.get_current_brand()

    filters = {}
    if brand:
        filters['brand'] = brand['id']       # forced — never another brand's

    term = f'%{search_text}%'
    or_filters = {
        'name': ['like', term],
        'source_title': ['like', term],
        'notes': ['like', term],
        'brand_contact_name': ['like', term],
        'brand_email': ['like', term],
    }

    names = frappe.get_all(
        REQUEST_DOCTYPE, filters=filters, or_filters=or_filters,
        order_by='modified desc', pluck='name', ignore_permissions=True,
    )
    total = len(names)
    if not names:
        return 0, []

    rows = frappe.get_all(
        REQUEST_DOCTYPE, filters={'name': ['in', names[:limit]]},
        fields=['name', 'request_type', 'status', 'source_title',
                'source_thumbnail', 'brand', 'modified'],
        order_by='modified desc', ignore_permissions=True,
    )
    items = [{
        'type': 'request',
        'id': r.get('name'),
        'title': r.get('source_title') or r.get('name'),
        'subtitle': _join(_TYPE_FROM_DB.get(r.get('request_type'), 'quote'),
                          (r.get('status') or 'Pending')),
        'thumbnail': r.get('source_thumbnail') or None,
        'meta': {
            'requestType': _TYPE_FROM_DB.get(r.get('request_type'), 'quote'),
            'status': (r.get('status') or 'Pending'),
            'brand': r.get('brand'),
        },
    } for r in rows]
    return total, items


# =====================================================================
# AI query planner — NL -> validated structured plan
# =====================================================================

def _plan_query(query):
    '''
    Call the LLM to turn `query` into a search plan, then validate/clamp it.
    Returns (validated_plan | None, raw_plan | None). Any failure -> (None, ...)
    so the caller can fall back to a literal search.
    '''
    vocab = _search_vocab()
    system_prompt = _planner_system_prompt(vocab)
    model = frappe.get_site_config().get('smart_search_model') or _PLANNER_MODEL
    try:
        raw = llm.get_claude_response(
            system_prompt, query, ret_type='dict',
            model=model, max_tokens=_PLANNER_MAX_TOKENS,
        )
    except Exception:
        frappe.log_error(frappe.get_traceback(), 'search._plan_query/llm')
        return None, None

    try:
        return _validate_plan(raw, vocab), raw
    except Exception:
        frappe.log_error(frappe.get_traceback(), 'search._plan_query/validate')
        return None, raw


def _planner_system_prompt(vocab):
    '''
    System prompt: describes the plan schema + injects the live vocabulary the
    model must map onto. Sent as the cached system block (see llm.get_claude_response).
    '''
    return (
        'You are a search query planner for a fashion/textile PLM catalog. Convert '
        "the user's natural-language query into a JSON search plan that selects one "
        'or more result TYPES and, per type, structured FILTERS.\n\n'
        'Result types and their allowed filters:\n'
        '- "fabric": qualities[], blends[], finishes[], shade_codes[] (each value '
        'MUST come from the vocab below), min_gsm (number), max_gsm (number), '
        'search_text (leftover keywords, e.g. a fabric name).\n'
        '- "style": gender[], productCategory[], fabricQuality[], colour[] (from '
        'vocab), search_text.\n'
        '- "sample_request": genders[], categories[], styles[] (from vocab), '
        'search_text.\n'
        '- "moodboard": season (single, from vocab), gender (single, from vocab), '
        'status (single: Draft | Published | Unpublished Changes), search_text '
        '(matches the board TITLE only).\n'
        '- "request": search_text only.\n\n'
        'Rules:\n'
        '1. gender, season and status MUST be exact values from the vocab below. '
        'For descriptive fields (colour, productCategory, fabricQuality, and the '
        'fabric qualities/blends/finishes/shade_codes, and sample categories/styles) '
        'output the plain BASE keyword the user used — lowercase and singular, e.g. '
        'colour "blue" (not "Bonnie Blue"), productCategory "shirt" (not "Shirts"). '
        'Do NOT snap these to a full catalog value; the system fuzzy-matches the '
        'keyword to the catalog, so "shirt" already covers "T-shirt".\n'
        '2. Understand intent/typos/other languages (e.g. Hinglish "blue colr ka '
        'shirt" = blue shirts). Map weight words to GSM (lightweight => max_gsm 150, '
        'midweight => 150-250, heavyweight => min_gsm 250) ONLY when weight is implied.\n'
        '3. Include only the types the user is asking about. If ambiguous, pick the '
        '1-2 most likely. If a concept fits no field, put it in search_text.\n'
        '4. Omit empty filter keys. Keep search_text short (just residual words).\n'
        '5. Respond with STRICT JSON only, no prose, in this shape:\n'
        '{"types":["style"],"filters":{"style":{"colour":["blue"],'
        '"productCategory":["shirt"]}},'
        '"interpretation":"Blue shirts","confidence":0.9}\n\n'
        'Vocabularies (for context — descriptive values may be broader than listed):\n'
        + frappe.as_json(vocab)
    )


def _validate_plan(raw, vocab):
    '''
    Clamp the LLM output to a safe plan: known types only, known filter keys only,
    every categorical value snapped to the vocab (case-insensitive, dropped if
    unknown), numeric GSM bounded, search_text trimmed. Returns a plan with at
    least one type, or None if nothing survived.
    '''
    if not isinstance(raw, dict):
        return None

    raw_filters = raw.get('filters') if isinstance(raw.get('filters'), dict) else {}
    types_in = [t for t in _as_list(raw.get('types')) if t in _ALL_TYPES]

    filters = {}
    for t in types_in:
        src = raw_filters.get(t) if isinstance(raw_filters.get(t), dict) else {}
        clean = _clean_type_filters(t, src, vocab)
        # A surviving type with no usable constraint at all falls back to matching
        # its residual/raw text literally, so it still returns relevant rows.
        if not _has_constraint(clean):
            clean['search_text'] = _clip_text(src.get('search_text')) or ''
        filters[t] = clean

    types = [t for t in types_in if t in filters]
    if not types:
        return None

    return {
        'types': types,
        'filters': filters,
        'interpretation': _clip_text(raw.get('interpretation'), 300),
        'confidence': _num(raw.get('confidence'), lo=0.0, hi=1.0),
    }


def _clean_type_filters(t, src, vocab):
    v = vocab.get(t, {})
    out = {}
    # Descriptive/open fields (colour, category, fabric quality/blend/finish/shade)
    # use _expand (fuzzy substring) so "shirt" catches "T-shirt" and "blue" catches
    # "Bonnie Blue (16-4134)". Enumerated fields (gender/season/status) stay EXACT
    # via _canon — fuzzy there is unsafe ("men" is a substring of "women").
    if t == 'fabric':
        out['qualities'] = _expand(src.get('qualities'), v.get('qualities'))
        out['blends'] = _expand(src.get('blends'), v.get('blends'))
        out['finishes'] = _expand(src.get('finishes'), v.get('finishes'))
        out['shade_codes'] = _expand(src.get('shade_codes'), v.get('shade_codes'))
        out['min_gsm'] = _num(src.get('min_gsm'), lo=0.0, hi=_GSM_MAX)
        out['max_gsm'] = _num(src.get('max_gsm'), lo=0.0, hi=_GSM_MAX)
    elif t == 'style':
        out['gender'] = _canon(src.get('gender'), v.get('gender'))
        out['productCategory'] = _expand(src.get('productCategory'), v.get('productCategory'))
        out['fabricQuality'] = _expand(src.get('fabricQuality'), v.get('fabricQuality'))
        out['colour'] = _expand(src.get('colour'), v.get('colour'))
    elif t == 'sample_request':
        out['genders'] = _canon(src.get('genders'), v.get('genders'))
        out['categories'] = _expand(src.get('categories'), v.get('categories'))
        out['styles'] = _expand(src.get('styles'), v.get('styles'))
    elif t == 'moodboard':
        out['season'] = _canon_one(src.get('season'), v.get('season'))
        out['gender'] = _canon_one(src.get('gender'), v.get('gender'))
        out['status'] = _canon_one(src.get('status'),
                                   ['Draft', 'Published', 'Unpublished Changes'])
    # residual free-text is allowed for every type
    txt = _clip_text(src.get('search_text'))
    if txt:
        out['search_text'] = txt
    # drop empty/None keys so appliedFilters is clean for the UI
    return {k: val for k, val in out.items() if val not in (None, '', [], {})}


def _has_constraint(clean):
    ''' True if the cleaned filter carries any real constraint (ignoring search_text). '''
    return any(k != 'search_text' and v not in (None, '', [], {}) for k, v in clean.items())


# =====================================================================
# Live vocabulary
# =====================================================================

def _search_vocab():
    ''' Distinct catalog values fed to the planner, cached hourly. '''
    cache = frappe.cache()
    cached = cache.get_value(_VOCAB_CACHE_KEY)
    if cached:
        parsed = frappe.parse_json(cached)
        if isinstance(parsed, dict):
            return parsed

    vocab = {
        'fabric': {
            'qualities': _distinct(FABRIC_DOCTYPE, 'clean_quality'),
            'blends': _distinct(FABRIC_DOCTYPE, 'clean_blend'),
            'finishes': _distinct(FABRIC_DOCTYPE, 'finish'),
            'shade_codes': _distinct(FABRIC_DOCTYPE, 'shade_category'),
        },
        'style': {
            'gender': _distinct(STYLE_DOCTYPE, 'gender'),
            'productCategory': _distinct(STYLE_DOCTYPE, 'product_category'),
            'fabricQuality': _distinct(STYLE_DOCTYPE, 'fabric_quality'),
            # Colour is open-vocabulary (Pantone-style names); keep a fuller list so
            # _expand can reach specific values like "Bonnie Blue (16-4134)".
            'colour': _distinct(STYLE_DOCTYPE, 'element_colour', cap=300),
        },
        'sample_request': {
            'genders': _distinct(SAMPLE_DOCTYPE, 'gender'),
            'categories': _distinct(SAMPLE_DOCTYPE, 'product_group'),
            'styles': _distinct(SAMPLE_DOCTYPE, 'product_category', cap=300),
        },
        'moodboard': {
            'season': _distinct(MOODBOARD_DOCTYPE, 'season'),
            # Moodboard.genders is a JSON array column; the Sample Request gender
            # set is the closest flat vocabulary and stays small/stable.
            'gender': _distinct(SAMPLE_DOCTYPE, 'gender'),
        },
    }
    cache.set_value(_VOCAB_CACHE_KEY, frappe.as_json(vocab), expires_in_sec=_VOCAB_TTL_SEC)
    return vocab


def _distinct(doctype, field, cap=200):
    ''' Distinct, non-empty, case-deduped values of a column (capped). '''
    try:
        rows = frappe.get_all(
            doctype, filters={field: ['is', 'set']}, pluck=field,
            distinct=True, order_by=f'{field} asc',
            ignore_permissions=True, limit_page_length=cap,
        )
    except Exception:
        frappe.log_error(frappe.get_traceback(), f'search._distinct/{doctype}.{field}')
        return []
    out, seen = [], set()
    for v in rows:
        if v is None:
            continue
        s = str(v).strip()
        key = s.lower()
        if s and key not in seen:
            seen.add(key)
            out.append(s)
    return out


# =====================================================================
# Small helpers
# =====================================================================

def _wanted_types(types):
    ''' Normalize the `types` param (CSV string, JSON array, or list) to a set. '''
    if types is None or types == '':
        return set(_ALL_TYPES)
    if isinstance(types, str):
        parsed = frappe.parse_json(types) if types.strip().startswith('[') else None
        values = parsed if isinstance(parsed, list) else types.split(',')
    elif isinstance(types, (list, tuple)):
        values = types
    else:
        values = [types]
    wanted = {str(v).strip() for v in values if str(v).strip()}
    wanted &= set(_ALL_TYPES)
    return wanted or set(_ALL_TYPES)


def _empty_groups(wanted):
    return [
        {'type': t, 'label': label, 'total': 0, 'items': []}
        for t, label in _GROUPS if t in wanted
    ]


def _clamp_limit(value):
    try:
        n = int(value)
    except (TypeError, ValueError):
        n = _DEFAULT_LIMIT
    return max(1, min(_MAX_LIMIT, n))


def _as_list(value):
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return list(value)
    return [value]


def _canon(values, vocab_list):
    ''' Snap each value to its canonical vocab spelling; drop unknowns. -> list. '''
    if not vocab_list:
        return []
    lut = {str(v).strip().lower(): v for v in vocab_list}
    out, seen = [], set()
    for v in _as_list(values):
        if v is None:
            continue
        c = lut.get(str(v).strip().lower())
        if c and c not in seen:
            seen.add(c)
            out.append(c)
    return out


def _expand(values, vocab_list):
    '''
    Fuzzy, recall-oriented match for open descriptive fields: return every vocab
    value that shares a case-insensitive substring with any term. So the planner's
    base keyword "shirt" -> ["T-shirt", "Shirts"] and "blue" -> ["Bonnie Blue
    (16-4134)", "Cerulean Blue", ...]. Terms under 3 chars are ignored to avoid
    runaway matches. The expanded set then feeds the endpoint's exact IN filter.
    '''
    if not vocab_list:
        return []
    terms = [s for s in (str(v).strip().lower() for v in _as_list(values) if v is not None)
             if len(s) >= 3]
    if not terms:
        return []
    out, seen = [], set()
    for voc in vocab_list:
        vl = str(voc).lower()
        if any(t in vl or vl in t for t in terms) and voc not in seen:
            seen.add(voc)
            out.append(voc)
    return out


def _canon_one(value, vocab_list):
    ''' Single-value variant of _canon; first match or None. '''
    if isinstance(value, (list, tuple)):
        value = next((v for v in value if v), None)
    got = _canon(value, vocab_list)
    return got[0] if got else None


def _num(value, lo=None, hi=None):
    ''' Parse a bounded float; None if invalid or out of range. '''
    try:
        n = float(value)
    except (TypeError, ValueError):
        return None
    if lo is not None and n < lo:
        return None
    if hi is not None and n > hi:
        return None
    return n


def _clip_text(value, cap=_SEARCH_TEXT_MAX):
    if not isinstance(value, str):
        return None
    s = value.strip()
    return s[:cap] if s else None


def _join(*parts):
    ''' Non-empty parts joined with a middot; None if nothing to show. '''
    vals = [str(p).strip() for p in parts if p not in (None, '', 0)]
    return ' · '.join(vals) if vals else None


def _first_image(value):
    '''
    garment.list_all returns image_urls as a dict ({front,back,...}) or list of
    signed URLs; pull the first usable URL for a thumbnail. None if empty.
    '''
    if not value:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        for v in value.values():
            url = _first_image(v)
            if url:
                return url
        return None
    if isinstance(value, (list, tuple)):
        for v in value:
            url = _first_image(v)
            if url:
                return url
    return None
