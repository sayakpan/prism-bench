'''
Style category -> garment style.

`masters.garment_styles()` serves the trim-costing vocabulary — distinct
`Trim Costing.style_name`, currently 21 values ("Dress", "T-shirt", "Hoody",
"Full Zip Hoody", ...). Briefs, brand sheets and moodboards speak a different
dialect for the same things: plural, merchandiser-typed, and not always a
garment shape at all ("Dresses", "T-Shirts", "Jeans", "Sets"). Nothing keys the
two sides together, so this resolves one onto the other.

Cheapest resolution that works, in order — only the last one costs anything:

    1. exact match, normalised            "Tops"     -> "Tops"
    2. exact match, singularised          "Dresses"  -> "Dress"
    3. the shared garment synonym table   "Tees"     -> "T-shirt"
    4. one Haiku call for the remainder   "Jeans"    -> "Trousers"

Step 3 stands down when it matches more than one style, rather than guessing
between them — the ambiguity goes to the LLM with the full vocabulary, which is
better placed to break the tie than "first hit wins" is.

LLM verdicts are cached per (input, vocabulary). The cache key carries a hash of
the vocabulary itself, so adding a Trim Costing style invalidates every cached
answer on its own without anyone remembering to flush.

Endpoint:
    GET|POST /api/method/prism.api.garment_style_map.map_style_category
    Header: X-Auth-Token: <jwt>            (required — see auth_required)
'''

import hashlib
import re

import frappe

from prism.auth.authenticator import auth_required
import prism.api.llm as llm
# Same synonym vocabulary the garment matchers use ("Tees" -> t shirt / tee),
# reused rather than forked so all three stay consistent.
import prism.api.garment_matching as gm

DOCTYPE = 'Trim Costing'
FIELD = 'style_name'

# Classification against a 21-value vocabulary — the cheapest model is the right
# one. Overridable per-site via `garment_style_map_model` in site_config.json.
_MAP_MODEL = 'claude-haiku-4-5-20251001'
_MAP_MAX_TOKENS = 1500

_CACHE_PREFIX = 'garment_style_map'
_CACHE_TTL = 24 * 60 * 60

# Below this the LLM's own answer is discarded as a non-match: a forced pick
# between 21 styles is worse than admitting the vocabulary has no equivalent.
_MIN_LLM_CONFIDENCE = 50

_MAX_INPUTS = 50


@frappe.whitelist(allow_guest=True)
@auth_required
def map_style_category(styleCategory=None, styleCategories=None, useLlm=True):
    '''
    Map merchandiser style categories onto `Trim Costing.style_name` values.

    Args:
        styleCategory:   one value, e.g. "Dresses"
        styleCategories: or many, e.g. ["Dresses", "T-Shirts", "Jeans"] —
                         batched into a single LLM call
        useLlm:          default True. False resolves only what the exact and
                         synonym passes can, and reports the rest as unmatched.

    Returns:
        {
            'success': True,
            'match': 'Dress',              # convenience: first result's match
            'data': [{'input': 'Dresses', 'match': 'Dress',
                      'method': 'singular', 'confidence': 100}],
            'meta': {'vocabulary': [...], 'llmCalled': False},
        }

    `method` is one of exact | singular | synonym | llm | cached | none.
    `match` is None when the vocabulary has no equivalent — that is an answer,
    not a failure, and the call still returns success.
    '''
    try:
        inputs = _inputs(styleCategory, styleCategories)
        if not inputs:
            return {'success': False, 'error': 'Pass styleCategory or styleCategories.'}

        vocabulary = _vocabulary()
        if not vocabulary:
            return {'success': False, 'error': f'No {DOCTYPE}.{FIELD} values to map onto.'}

        exact, singular = _vocabulary_index(vocabulary)

        results, unresolved = {}, []
        for raw in inputs:
            hit = _resolve_locally(raw, vocabulary, exact, singular)
            if hit:
                results[raw] = hit
            else:
                unresolved.append(raw)

        llm_called = False
        if unresolved and _as_bool(useLlm, default=True):
            vocab_hash = _vocabulary_hash(vocabulary)

            still_open = []
            for raw in unresolved:
                cached = frappe.cache().get_value(_cache_key(vocab_hash, raw))
                if cached is None:
                    still_open.append(raw)
                else:
                    results[raw] = {
                        'match': cached or None,
                        'method': 'cached',
                        'confidence': 100 if cached else 0,
                    }

            if still_open:
                llm_called = True
                for raw, hit in _llm_map(still_open, vocabulary).items():
                    results[raw] = hit
                    frappe.cache().set_value(
                        _cache_key(vocab_hash, raw), hit['match'] or '',
                        expires_in_sec=_CACHE_TTL,
                    )

        # Ordered to match the inputs, with a row for every one of them — an
        # unmatched input is still a row, so the caller can zip the response
        # against what it sent.
        data = []
        for raw in inputs:
            hit = results.get(raw) or {'match': None, 'method': 'none', 'confidence': 0}
            data.append({'input': raw, **hit})

        return {
            'success': True,
            'match': data[0]['match'] if data else None,
            'data': data,
            'meta': {'vocabulary': vocabulary, 'llmCalled': llm_called},
        }

    except Exception as ex:
        frappe.log_error(frappe.get_traceback(), 'garment_style_map.map_style_category()')
        return {'success': False, 'error': str(ex)}


# =====================================================================
# Vocabulary
# =====================================================================

def _vocabulary():
    ''' The same list masters.garment_styles() serves, cleaned of blanks. '''
    rows = frappe.get_all(
        DOCTYPE, fields=[FIELD], distinct=True,
        ignore_permissions=True, limit_page_length=0,
    )
    seen, out = set(), []
    for r in rows:
        value = (r.get(FIELD) or '').strip()
        if value and value.lower() not in seen:
            seen.add(value.lower())
            out.append(value)
    return sorted(out)


def _vocabulary_index(vocabulary):
    '''
    -> ({normalised: style}, {singularised: style}), kept apart so an exact hit
    always beats a singularised one — "Tops" must resolve to "Tops", never to
    whatever else happens to singularise to "top".
    '''
    exact, singular = {}, {}
    for value in vocabulary:
        exact.setdefault(_norm(value), value)
        singular.setdefault(gm._norm_style_key(value), value)
    return exact, singular


def _vocabulary_hash(vocabulary):
    joined = '|'.join(vocabulary).encode('utf-8')
    return hashlib.sha1(joined).hexdigest()[:12]


# =====================================================================
# Local resolution (free)
# =====================================================================

def _resolve_locally(raw, vocabulary, exact, singular):
    key = _norm(raw)
    if not key:
        return None

    if key in exact:
        return {'match': exact[key], 'method': 'exact', 'confidence': 100}

    skey = gm._norm_style_key(raw)
    if skey in exact:
        return {'match': exact[skey], 'method': 'singular', 'confidence': 100}
    if skey in singular:
        return {'match': singular[skey], 'method': 'singular', 'confidence': 100}

    matcher = gm._style_matcher([raw])
    if matcher:
        hits = [v for v in vocabulary if matcher.search(_norm(v))]
        # Exactly one hit is a real answer. Several means the synonym set was
        # broader than the vocabulary is granular ("Tops" reaching Polo, Tank
        # Top and Crop Top at once) — hand that to the LLM instead of taking
        # whichever happened to sort first.
        if len(hits) == 1:
            return {'match': hits[0], 'method': 'synonym', 'confidence': 90}

    return None


# =====================================================================
# LLM fallback
# =====================================================================

def _llm_map(inputs, vocabulary):
    ''' -> {input: {match, method, confidence}} for every input, matched or not. '''
    numbered = '\n'.join(f'- {v}' for v in vocabulary)
    system_prompt = f'''You map a garment merchandising category onto our internal garment \
style vocabulary.

Our vocabulary (these are the ONLY values you may return):
{numbered}

For each input, return the single vocabulary value that names the same kind of garment. \
The two lists are written by different teams, so expect plural/singular differences, \
spelling variants, and category names that are broader or narrower than any single \
vocabulary value.

Rules:
- Return a vocabulary value copied EXACTLY as written above, or null when the vocabulary \
genuinely has no equivalent. Never invent, translate or reword a value.
- When the input is broader than the vocabulary (a category covering several styles), pick \
the vocabulary value that is the most typical, highest-volume member of it.
- confidence is an integer 0-100. Use below 50 when you are guessing rather than mapping — \
a low-confidence answer is discarded, which is the correct outcome for a real non-match.
- Output ONLY a JSON array, one object per input, same order as given:
[{{"input": "<the input, echoed exactly>", "match": "<vocabulary value or null>", \
"confidence": 95}}]
No prose outside the JSON.'''

    user_prompt = 'Map these:\n' + '\n'.join(f'- {v}' for v in inputs)

    model = frappe.get_site_config().get('garment_style_map_model') or _MAP_MODEL
    try:
        raw = llm.get_claude_response(
            system_prompt, user_prompt, ret_type='list',
            model=model, max_tokens=_MAP_MAX_TOKENS,
        )
    except Exception:
        frappe.log_error(frappe.get_traceback(), 'garment_style_map._llm_map/llm')
        return {}

    by_norm = {_norm(v): v for v in vocabulary}
    wanted = {_norm(v): v for v in inputs}

    out = {}
    for item in raw or []:
        if not isinstance(item, dict):
            continue
        source = wanted.get(_norm(item.get('input')))
        if not source:
            continue
        # The model is told to copy a value verbatim; normalising on the way back
        # in means a stray case or hyphen difference still lands, and anything
        # outside the vocabulary is dropped rather than passed through.
        match = by_norm.get(_norm(item.get('match')))
        confidence = _clamp_confidence(item.get('confidence'))
        if match and confidence >= _MIN_LLM_CONFIDENCE:
            out[source] = {'match': match, 'method': 'llm', 'confidence': confidence}
        else:
            out[source] = {'match': None, 'method': 'llm', 'confidence': confidence}

    return out


# =====================================================================
# helpers
# =====================================================================

def _inputs(one, many):
    values = []
    for raw in (_as_list(many) or []):
        if isinstance(raw, str) and raw.strip():
            values.append(raw.strip())
    if isinstance(one, str) and one.strip():
        values.insert(0, one.strip())

    seen, out = set(), []
    for v in values:
        if v.lower() not in seen:
            seen.add(v.lower())
            out.append(v)
    return out[:_MAX_INPUTS]


def _cache_key(vocab_hash, raw):
    return f'{_CACHE_PREFIX}:{vocab_hash}:{_norm(raw)}'


def _norm(text):
    return re.sub(r'[^a-z0-9]+', ' ', str(text or '').lower()).strip()


def _clamp_confidence(value):
    try:
        return max(0, min(100, int(float(value))))
    except (TypeError, ValueError):
        return 0


def _as_bool(value, default=False):
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() not in ('0', 'false', 'no', 'none', '')


def _as_list(value):
    if isinstance(value, list):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = frappe.parse_json(value)
            return parsed if isinstance(parsed, list) else [value]
        except Exception:
            return [value]
    return []
