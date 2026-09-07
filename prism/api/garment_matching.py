'''
Garment matching — narrows Brand Master Data down to the closest-matching
garments for a design brief (Moodboard-shaped: brand + competitors, season,
gender, style categories, free-text vision), scoped to the input brand and its
resolved competitors only.

Pipeline: resolve the brand + its `primary_competitors` free text to Brand
Master Data's `brand` strings (fuzzy match — neither side is a real foreign
key) -> structural filter (brand set + gender) -> season/style keyword
pre-score to bound how many candidates reach the LLM -> a single LLM ranking
call for the final best-first shortlist -> an own-brand quota pass.

The brief's free-text `userVision` is the designer's own words for what they
are trying to make, and it is treated as the strongest relevance signal after
gender. It is not just handed to the LLM: it is decomposed into weighted
words/phrases (`_vision_profile`) and scored against every candidate's own
product text before the shortlist cut, so vision-aligned garments survive to
the ranker instead of being cut by a season/style keyword score that never read
the vision. See `_VISION_SCORE_WEIGHT`.

Two coverage quotas are the places the ranking is overridden:

    own brand    a brief is being seeded against the brand's *own* shelf as
                 much as against the market, so the response carries at least
                 `_MIN_OWN_BRAND` of the input brand's own garments whenever
                 that many exist in the brief's style categories.
    competitors  a brief is also a read of the market, and a market read that
                 quotes four garments from one competitor and none from the
                 other five is a worse answer than one that spans them, so
                 every resolved competitor carrying an on-category garment gets
                 at least one slot.

Only on-category rows are ever forced in by either quota — see
`_enforce_own_brand_quota` and `_enforce_competitor_spread` for why they stop
short rather than padding. Both are bounded by `limit`, so a brief with more
competitors than slots covers the strongest ones and reports the rest in
`meta.competitorSpread.unrepresented` — raise `limit` (up to 20) or lower
`minOwnBrand` to widen the spread.

Alongside the ranking, two price anchors for the brief, both scoped to the
*input* brand alone — competitors are excluded from both by design, since they
answer "where does our own shelf sit", not "where does the market sit":

    targetRetail  the brand's catalogue price, averaged in USD
    targetFob     the brand's per-piece factory price from customs shipments

They are computed from unrelated tables and never join — nothing keys a
shipment to a catalogue product — so their sample counts differ, and one
resolving while the other is None is normal rather than a fault.

Deliberately no cross-brand fallback: if the input brand + its competitors
resolve to no Brand Master Data rows (e.g. a brand the scraper hasn't covered
yet), the response comes back with an empty `garments` list rather than
silently widening to the whole catalog — see `meta` for why.

Endpoint:
    POST /api/method/prism.api.garment_matching.match_garments
    Header: X-Auth-Token: <jwt>            (required — see auth_required)
'''

import re
import json
import difflib
from collections import Counter

import frappe

from prism.auth.authenticator import auth_required
import prism.api.llm as llm

DOCTYPE = 'Brand Master Data'

# Ranking is a reranking/classification task, not long-form generation, so use
# the fast model and a small output cap — mirrors search._PLANNER_MODEL.
# Overridable per-site via `garment_matching_model` in site_config.json.
_RANK_MODEL = 'claude-haiku-4-5-20251001'
_RANK_MAX_TOKENS = 3000

_DEFAULT_LIMIT = 8
_MAX_LIMIT = 20

# Bounds how many season/style-scored candidates reach the LLM prompt. Kept
# high enough that a brand+competitor pool rarely gets truncated before the
# LLM sees it — this is a cost/latency cap, not a relevance filter.
_MAX_LLM_CANDIDATES = 100

# How many of the input brand's own garments the response should carry, when
# that many exist on-category. Overridable per request via `minOwnBrand`.
_MIN_OWN_BRAND = 3

# Own-brand rows held in the LLM shortlist regardless of heuristic score. A
# brand with a handful of rows against ten competitors with thousands can lose
# every slot to the `_MAX_LLM_CANDIDATES` cut, which would leave the quota pass
# with nothing the LLM ever saw. Comfortably above `_MIN_OWN_BRAND` so the LLM
# still gets a real choice among them rather than an exact-fit list.
_OWN_BRAND_SHORTLIST_RESERVE = 20

_OWN_BRAND_BACKFILL_RATIONALE = (
    "Included as an on-category reference from the brand's own range."
)

# Whether every resolved competitor with an on-category garment is guaranteed a
# slot. Overridable per request via `competitorSpread`.
_COMPETITOR_SPREAD = True

# Competitor rows held in the LLM shortlist regardless of heuristic score, per
# brand — the same argument as `_OWN_BRAND_SHORTLIST_RESERVE`. A competitor
# whose rows all score mid-table can otherwise be cut wholesale by the
# `_MAX_LLM_CANDIDATES` truncation, and the spread pass would then be
# backfilling it from garments the ranker never saw. Above 1 so the backfill
# has a real choice within the brand rather than taking whatever survived.
_COMPETITOR_SHORTLIST_RESERVE = 4

_COMPETITOR_BACKFILL_RATIONALE = (
    "Closest on-category garment from this competitor, included so the read "
    "spans the whole competitive set."
)

_BRAND_FUZZY_CUTOFF = 0.6

_GENDER_OPTIONS = ('Women', 'Men', 'Unisex', 'Kids', 'Girls', 'Boys', 'Toddler')

# Seasonal fabric/silhouette signals, used only to pre-rank/bound the candidate
# pool before the LLM — the LLM re-applies the same SS/AW logic explicitly
# against full product text, so this heuristic never has to be exhaustive.
_SS_KEYWORDS = (
    'linen', 'cotton', 'short sleeve', 'sleeveless', 'lightweight', 'breathable',
    'summer', 'crop', 'shorts', 'tank', 'mesh', 'rayon', 'viscose', 'chiffon',
    'seersucker', 'poplin', 'airy', 'half sleeve',
)
_AW_KEYWORDS = (
    'wool', 'fleece', 'full sleeve', 'long sleeve', 'padded', 'quilted',
    'thermal', 'winter', 'corduroy', 'tweed', 'cashmere', 'flannel', 'sherpa',
    'puffer', 'heavyweight', 'layering', 'coat', 'jacket', 'sweater',
)

_STYLE_SCORE_WEIGHT = 3
_SEASON_SCORE_WEIGHT = 1

# Applied to a 0..1 coverage ratio of the vision's weighted terms, so the whole
# vision contributes at most `_VISION_SCORE_WEIGHT` however long it is — a
# rambling brief cannot outscore a tight one, and no single term can run away
# with the ranking. Set above a couple of style hits deliberately: the vision is
# the part of the brief the designer actually wrote, and it is what the caller
# asked to be weighted hardest. Style still gates both quotas' eligibility, so a
# vision-only match cannot force an off-category garment into a quota slot.
_VISION_SCORE_WEIGHT = 8

_VISION_WORD_WEIGHT = 1
# Adjacent pairs ("washed indigo", "utility pocket") say far more about intent
# than either word alone, and a phrase hit also scores both its words, so a
# garment echoing the vision's actual phrasing pulls clear of one that happens
# to share loose vocabulary.
_VISION_PHRASE_WEIGHT = 2
# Long visions are bounded so the coverage ratio stays meaningful — beyond this
# every candidate matches a small fraction and the signal flattens out.
_VISION_MAX_TERMS = 60
# Matched terms echoed back per garment, to the LLM and to the caller.
_VISION_HITS_SHOWN = 6

# Framing/intent words that carry no garment signal. Everything here is matched
# after `_singular`, so the singular form is enough.
_VISION_STOPWORDS = frozenset((
    'the', 'and', 'for', 'with', 'that', 'thi', 'from', 'into', 'onto', 'our',
    'their', 'they', 'them', 'you', 'your', 'its', 'are', 'was', 'were', 'been',
    'being', 'have', 'ha', 'had', 'not', 'but', 'all', 'any', 'can', 'will',
    'should', 'would', 'could', 'may', 'more', 'most', 'much', 'very', 'some',
    'such', 'than', 'then', 'there', 'here', 'when', 'while', 'who', 'what',
    'which', 'how', 'why', 'also', 'about', 'across', 'over', 'under', 'out',
    'off', 'per', 'via', 'want', 'need', 'like', 'feel', 'look', 'make', 'made',
    'use', 'keep', 'give', 'take', 'bring', 'season', 'collection', 'range',
    'brief', 'design', 'product', 'garment', 'piece', 'style', 'brand', 'line',
    'customer', 'consumer', 'shopper', 'market', 'wear', 'clothing', 'apparel',
    'new', 'good', 'great', 'best', 'high', 'low', 'well', 'one', 'two',
    'three', 'etc', 'women', 'woman', 'men', 'man', 'kid', 'girl', 'boy',
))

_CANDIDATE_FIELDS = [
    'name', 'brand', 'product_name', 'category', 'subcategories', 'gender',
    'selected_color', 'available_colors', 'fit', 'about', 'product_details',
    'composition', 'blend', 'image_url', 'price', 'product_url',
]


@frappe.whitelist(allow_guest=True, methods=['POST'])
@auth_required
def match_garments(payload=None):
    '''
    Rank the closest-matching Brand Master Data garments for a design brief.

    payload (dict or JSON string):
        brandIds        list[str]  Brand docnames (e.g. ["zudio"])
        season          str        e.g. "SS27" / "AW27"
        genders         list[str]
        styleCategories list[str]
        userVision      str
        brandOverviews  list[dict] optional, [{brandId, overview}] — extra LLM context
        limit           int        optional, default 8, capped at 20
        minOwnBrand     int        optional, default 3 — own-brand garments to
                                   guarantee in the response (0 disables)
        competitorSpread bool      optional, default True — guarantee every
                                   resolved competitor with an on-category
                                   garment at least one slot

    Returns {'success': True, 'garments': [...], 'targetRetail': {...},
    'targetFob': {...}, 'meta': {...}}. `garments` is ranked best-first and may
    be shorter than `limit` — it is never padded out with weaker matches, and
    comes back empty (not broadened to other brands) if the brand + its
    competitors have no coverage in Brand Master Data.

    Within that list, at least `minOwnBrand` entries are the input brand's own
    on-category garments whenever it has that many, the rest being competitors;
    each entry carries `isOwnBrand` so the caller can tell the two apart.
    `meta.ownBrandQuota` reports what the reservation actually did, including
    the case where the brand simply has too few on-category rows to fill it.

    The competitor slots are then spread: every resolved competitor holding an
    on-category garment gets at least one entry, strongest-competitor-first
    when `limit` cannot cover them all. `meta.competitorSpread` reports which
    brands were covered, which have nothing on-category, and which simply did
    not fit.

    Ranking leans hardest on `userVision` after gender — it is scored against
    every candidate before the shortlist cut and is the LLM's top-priority
    signal; each garment echoes back the vision terms it matched as
    `visionMatch`.

    `targetRetail` and `targetFob` are the same garment definition priced from
    two unrelated sources — the brand's catalogue and its inbound customs
    shipments. Either can be None while the other resolves; see
    `_target_retail` and `_target_fob`.
    '''
    payload = _as_dict(payload)
    brand_ids = _as_list(payload.get('brandIds'))
    season = (payload.get('season') or '').strip()
    genders = [g for g in _as_list(payload.get('genders')) if g]
    style_categories = [s for s in _as_list(payload.get('styleCategories')) if s]
    user_vision = (payload.get('userVision') or '').strip()
    brand_overviews = _as_list(payload.get('brandOverviews'))
    try:
        limit = min(int(payload.get('limit') or _DEFAULT_LIMIT), _MAX_LIMIT)
    except (TypeError, ValueError):
        limit = _DEFAULT_LIMIT
    min_own_brand = _min_own_brand(payload, limit)
    competitor_spread = _competitor_spread(payload)

    resolution = _resolve_brand_pool(brand_ids)
    own_bmd_brands = resolution['own_bmd_brands']
    competitor_bmd_brands = resolution['matched_bmd_brands'] - own_bmd_brands

    candidates = _fetch_candidates(resolution['matched_bmd_brands'], genders)
    pool_size = len(candidates)

    # Decomposed once and threaded through scoring so the shortlist cut, the
    # LLM prompt and `meta` all read the vision the same way.
    vision = _vision_profile(user_vision)

    scored = _score_candidates(candidates, season, style_categories, vision)
    scored.sort(key=lambda c: c['_score'], reverse=True)
    shortlist = _build_shortlist(scored, own_bmd_brands, min_own_brand)

    ranked = _llm_rank(
        shortlist, limit, season=season, genders=genders,
        style_categories=style_categories, user_vision=user_vision,
        brand_overviews=brand_overviews, own_bmd_brands=own_bmd_brands,
        min_own_brand=min_own_brand,
    )
    if ranked is None:
        # LLM ranking failed outright (API error, bad JSON, ...) — degrade to
        # the heuristic score rather than error the whole request.
        ranked = shortlist[:limit]
        rank_mode = 'heuristic_fallback'
    else:
        rank_mode = 'llm'

    # The LLM is asked for the quota but not trusted to honour it — and the
    # fallback path above cannot honour it at all — so it is enforced here,
    # against the full pool rather than the shortlist.
    ranked, own_brand_quota = _enforce_own_brand_quota(
        ranked, scored, own_bmd_brands, limit, min_own_brand, style_categories,
    )

    # Runs after the own-brand quota so that quota stays the harder guarantee:
    # the spread pass may only take back own-brand slots above `min_own_brand`.
    ranked, competitor_spread_report = _enforce_competitor_spread(
        ranked, scored, own_bmd_brands, competitor_bmd_brands, limit,
        min_own_brand, competitor_spread, style_categories,
    )

    # Computed off `candidates` (the full, untruncated pool) rather than the LLM
    # shortlist: this is a price benchmark, so it wants every garment the brand
    # sells into the brief, not the eight the ranker liked best.
    target_retail = _target_retail(candidates, own_bmd_brands, style_categories)
    target_fob = _target_fob(resolution['input_brand_names'], genders, style_categories)

    brand_display = resolution['brand_display_map']

    return {
        'success': True,
        'garments': [_public_fields(c, brand_display, own_bmd_brands) for c in ranked],
        'targetRetail': target_retail,
        'targetFob': target_fob,
        'meta': {
            'resolvedBrandNames': resolution['input_brand_names'],
            'matchedBrandsInCatalog': sorted(resolution['matched_bmd_brands']),
            'unresolvedCompetitors': resolution['unresolved'],
            'candidatePoolSize': pool_size,
            'llmCandidatesConsidered': len(shortlist),
            'rankMode': rank_mode,
            'ownBrandQuota': own_brand_quota,
            'competitorSpread': competitor_spread_report,
            'visionTermsUsed': vision['terms'] if vision else [],
        },
    }


def _min_own_brand(payload, limit):
    ''' Never more than `limit` — a quota bigger than the response would push
    every competitor out, which is the opposite of what the brief is for. '''
    raw = payload.get('minOwnBrand')
    if raw in (None, ''):
        raw = _MIN_OWN_BRAND
    try:
        return max(0, min(int(raw), limit))
    except (TypeError, ValueError):
        return min(_MIN_OWN_BRAND, limit)


def _competitor_spread(payload):
    raw = payload.get('competitorSpread')
    if raw in (None, ''):
        return _COMPETITOR_SPREAD
    if isinstance(raw, str):
        return raw.strip().lower() not in ('0', 'false', 'no', 'off')
    return bool(raw)


# =====================================================================
# Stage A — brand + competitor resolution (fuzzy, no LLM)
# =====================================================================

def _resolve_brand_pool(brand_ids):
    '''
    Turn the input Brand docnames + their `primary_competitors` free text into
    the set of Brand Master Data `brand` strings to search within. Both sides
    are free text (Brand.primary_competitors is human-typed, Brand Master
    Data.brand is scraper-assigned) — there is no real foreign key here, so
    this is fuzzy string matching, not a join.
    '''
    bmd_brands = _distinct_bmd_brands()

    input_names = []
    competitor_names = []
    for bid in brand_ids:
        if not frappe.db.exists('Brand', bid):
            continue
        row = frappe.db.get_value(
            'Brand', bid, ['brand', 'primary_competitors'], as_dict=True,
        )
        if row.brand:
            input_names.append(row.brand)
        competitor_names.extend(_split_competitors(row.primary_competitors))

    matched = set()
    # The input brands' own BMD rows, tracked separately from the competitor ones:
    # ranking searches brand + competitors together, but target retail is a
    # benchmark of what *this* brand charges and must never see a competitor.
    own = set()
    # Brand Master Data's `brand` string is scraper-assigned and inconsistently
    # cased/slugged ("wearpact", "handm"), but the two things it gets fuzzy-matched
    # against here — Brand.brand and the human-typed primary_competitors names —
    # are both clean display names already. Record the first one that resolves
    # each BMD brand so callers can show that instead of the raw scraped string.
    # Input names go first so the primary brand's own name wins over a
    # competitor's if they were ever to collide.
    display_by_bmd = {}
    for name in input_names:
        hit = _fuzzy_match_brand(name, bmd_brands)
        if hit:
            matched.add(hit)
            own.add(hit)
            display_by_bmd.setdefault(hit, name)

    unresolved = []
    for name in competitor_names:
        hit = _fuzzy_match_brand(name, bmd_brands)
        if hit:
            matched.add(hit)
            display_by_bmd.setdefault(hit, name)
        else:
            unresolved.append(name)

    return {
        'input_brand_names': input_names,
        'matched_bmd_brands': matched,
        'own_bmd_brands': own,
        'unresolved': unresolved,
        'brand_display_map': display_by_bmd,
    }


def _split_competitors(text):
    if not text:
        return []
    parts = re.split(r'[,\n;/]+', text)
    return [p.strip() for p in parts if p.strip()]


def _distinct_bmd_brands():
    return frappe.get_all(DOCTYPE, pluck='brand', distinct=True, ignore_permissions=True)


def _normalize_brand(s):
    # '&' -> 'and' mirrors Brand.generate_unique_slug's own convention (e.g.
    # "H&M" -> "handm"), so the two free-text sides line up on the common case
    # of an ampersand brand name instead of just missing it.
    s = (s or '').lower().replace('&', 'and')
    return re.sub(r'[^a-z0-9]', '', s)


def _fuzzy_match_brand(name, candidates):
    norm_name = _normalize_brand(name)
    if not norm_name:
        return None

    best, best_score = None, 0.0
    for cand in candidates:
        norm_cand = _normalize_brand(cand)
        if not norm_cand:
            continue
        if norm_name == norm_cand:
            return cand
        if norm_name in norm_cand or norm_cand in norm_name:
            score = 0.85
        else:
            score = difflib.SequenceMatcher(None, norm_name, norm_cand).ratio()
        if score > best_score:
            best, best_score = cand, score

    return best if best_score >= _BRAND_FUZZY_CUTOFF else None


# =====================================================================
# Stage B — structural filter
# =====================================================================

def _fetch_candidates(bmd_brands, genders):
    if not bmd_brands:
        return []

    filters = {'brand': ['in', list(bmd_brands)]}
    mapped_genders = _map_genders(genders)
    if mapped_genders:
        filters['gender'] = ['in', mapped_genders]

    return frappe.get_all(
        DOCTYPE, filters=filters, fields=_CANDIDATE_FIELDS,
        limit_page_length=0, ignore_permissions=True,
    )


def _map_genders(genders):
    options_lower = {g.lower(): g for g in _GENDER_OPTIONS}
    mapped = []
    for g in genders:
        hit = options_lower.get((g or '').strip().lower())
        if hit:
            mapped.append(hit)
    return mapped


# =====================================================================
# Stage C — vision/season/style pre-score (orders the pool, bounds LLM volume)
# =====================================================================

def _score_candidates(candidates, season, style_categories, vision=None):
    '''
    Score every candidate on three signals — the brief's free-text vision
    (weighted hardest), its style categories, and its season — and stash the
    matched vision terms on each row for the prompt and the response.

    The vision component is why this stage is no longer only a cost cap: the
    shortlist cut and both quota backfills read this order, so a garment the
    vision describes has to be able to out-rank one that merely shares a
    category word.
    '''
    season_bucket = _season_bucket(season)
    style_terms = _style_terms(style_categories)

    for c in candidates:
        text = _candidate_text(c)
        season_score = _season_score(text, season_bucket)
        sleeve_note = _sleeve_note(c.get('product_name'), c.get('fit'))
        # `fit` rarely uses words like "long sleeve" — brands like Zara instead
        # list (or omit) a "Sleeve length" row in a dimension table, which none
        # of the SS/AW keyword phrases above would ever match. Without this, a
        # sleeveless/strapless top has no seasonal signal at all and can rank
        # for an AW brief purely on colour/category.
        if season_bucket == 'AW':
            if sleeve_note == 'has sleeves':
                season_score += 1
            elif sleeve_note == 'sleeveless/strapless':
                season_score -= 1
        style_score = sum(1 for term in style_terms if term in text)
        vision_hits, vision_ratio = _vision_match(text, vision)
        c['_score'] = round(
            style_score * _STYLE_SCORE_WEIGHT
            + season_score * _SEASON_SCORE_WEIGHT
            + vision_ratio * _VISION_SCORE_WEIGHT,
            3,
        )
        c['_sleeve_note'] = sleeve_note
        c['_vision_hits'] = vision_hits

    return candidates


def _vision_profile(user_vision):
    '''
    The free-text vision as weighted match terms — every content word plus
    every adjacent content-word pair, in first-appearance order — or None when
    the brief carries no vision at all.

    Stopwords break the pair chain rather than being skipped over, so "washed
    linen in earth tones" yields the phrases "washed linen" and "earth tone"
    but never the meaningless "linen earth". Terms are singularised on both
    sides of the comparison (see `_text_terms`) so "utility pockets" in the
    vision matches "utility pocket" in a product description.
    '''
    if not (user_vision or '').strip():
        return None

    weights, order, prev = {}, [], None
    for token in re.findall(r"[a-z][a-z0-9']*", user_vision.lower()):
        word = _singular(token)
        if len(word) < 3 or word in _VISION_STOPWORDS:
            prev = None
            continue
        if word not in weights:
            weights[word] = _VISION_WORD_WEIGHT
            order.append(word)
        if prev:
            phrase = f'{prev} {word}'
            if phrase not in weights:
                weights[phrase] = _VISION_PHRASE_WEIGHT
                order.append(phrase)
        prev = word

    if not order:
        return None

    terms = order[:_VISION_MAX_TERMS]
    return {
        'terms': terms,
        'weights': weights,
        'total': sum(weights[t] for t in terms),
    }


def _vision_match(text, vision):
    '''
    (top matched terms, 0..1 weighted coverage) for one candidate's text.

    Coverage is a ratio of the vision's own total weight, not a raw hit count,
    so the vision's contribution to `_score` is bounded at
    `_VISION_SCORE_WEIGHT` no matter how long the brief runs.
    '''
    if not vision:
        return [], 0.0

    present = _text_terms(text)
    hits = [t for t in vision['terms'] if t in present]
    if not hits:
        return [], 0.0

    weight = sum(vision['weights'][t] for t in hits)
    # Phrases before single words, longer before shorter: the most specific
    # evidence is what the ranker and the caller want to read first.
    hits.sort(key=lambda t: (-vision['weights'][t], -len(t)))
    return hits[:_VISION_HITS_SHOWN], round(weight / vision['total'], 4)


def _text_terms(text):
    '''
    Candidate text as the same word/adjacent-pair term set `_vision_profile`
    builds, so matching is a set intersection rather than a substring scan —
    exact on word boundaries ("tee" cannot match "canteen") and cheap enough to
    run over a few thousand rows.
    '''
    words = [_singular(w) for w in re.findall(r"[a-z][a-z0-9']*", text)]
    terms = set(words)
    terms.update(f'{a} {b}' for a, b in zip(words, words[1:]))
    return terms


def _sleeve_note(product_name, fit_text):
    ''' 'has sleeves' / 'sleeveless/strapless' when the product name or fit/
    dimensions text gives real evidence either way, else None — never guess. '''
    t = f'{product_name or ""} {fit_text or ""}'.lower()
    if any(k in t for k in ('sleeve length', 'long sleeve', 'full sleeve', 'short sleeve', 'half sleeve')):
        return 'has sleeves'
    if any(k in t for k in ('sleeveless', 'strapless', 'tank', 'camisole', 'corset', 'halter')):
        return 'sleeveless/strapless'
    return None


def _candidate_text(c):
    # Colours are in here for the vision's sake: a brief's own words are as
    # likely to be about palette ("washed indigo", "bone and clay") as about
    # silhouette, and without these fields that half of the vision could never
    # match anything. Harmless to the season/style scores, which have no
    # colour terms to hit.
    parts = [
        c.get('product_name'), c.get('category'), c.get('subcategories'),
        c.get('about'), _strip_html(c.get('product_details')),
        c.get('composition'), c.get('blend'), c.get('fit'),
        c.get('selected_color'), c.get('available_colors'),
    ]
    return ' '.join(str(p) for p in parts if p).lower()


def _season_bucket(season):
    s = (season or '').upper()
    if 'SS' in s or 'SPRING' in s or 'SUMMER' in s:
        return 'SS'
    if 'AW' in s or 'FW' in s or 'AUTUMN' in s or 'WINTER' in s or 'FALL' in s:
        return 'AW'
    return None


def _season_score(text, bucket):
    if not bucket:
        return 0
    ss_hits = sum(1 for kw in _SS_KEYWORDS if kw in text)
    aw_hits = sum(1 for kw in _AW_KEYWORDS if kw in text)
    return (ss_hits - aw_hits) if bucket == 'SS' else (aw_hits - ss_hits)


def _style_terms(style_categories):
    terms = set()
    for sc in style_categories:
        for word in re.findall(r"[a-zA-Z']+", (sc or '').lower()):
            if len(word) > 2:
                terms.add(word)
    return terms


def _strip_html(value):
    if not value:
        return ''
    return re.sub(r'<[^>]+>', ' ', value)


def _build_shortlist(scored, own_bmd_brands, min_own_brand):
    """
    The top `_MAX_LLM_CANDIDATES` by score, except that every brand keeps a
    per-brand reserve of its own best rows through the cut —
    `_OWN_BRAND_SHORTLIST_RESERVE` for the input brand,
    `_COMPETITOR_SHORTLIST_RESERVE` for each competitor.

    `scored` must already be sorted best-first. A pure top-N cut is
    brand-blind: one competitor with thousands of rows can take most of the
    shortlist and push a small brand — the input brand, or a competitor whose
    catalogue simply scores mid-table — out of it entirely. Both quota passes
    would then be backfilling from garments the ranker never saw, and the LLM
    would never have had the option of spreading its picks in the first place.

    Promotions are paid for by dropping the weakest rows from brands already
    above their own reserve, so the shortlist stays exactly
    `_MAX_LLM_CANDIDATES` long and the cost cap still holds.
    """
    shortlist = scored[:_MAX_LLM_CANDIDATES]
    if len(scored) <= _MAX_LLM_CANDIDATES:
        return shortlist

    own_bmd_brands = own_bmd_brands or set()
    # No own-brand quota means no reason to hold own-brand rows back either;
    # they compete on score like everybody else.
    own_reserve = _OWN_BRAND_SHORTLIST_RESERVE if min_own_brand else _COMPETITOR_SHORTLIST_RESERVE

    def reserve_for(brand):
        return own_reserve if brand in own_bmd_brands else _COMPETITOR_SHORTLIST_RESERVE

    counts = Counter(c.get('brand') for c in shortlist)

    by_brand = {}
    for c in scored[_MAX_LLM_CANDIDATES:]:
        brand = c.get('brand')
        if counts[brand] < reserve_for(brand):
            counts[brand] += 1
            by_brand.setdefault(brand, []).append(c)
    if not by_brand:
        return shortlist

    # Round-robin rather than straight score order: when there is only room to
    # pay for some of the promotions, a first slot for every under-represented
    # brand beats a fourth slot for one of them — that is the whole point of
    # the reserve.
    promotable = []
    for rank in range(max(len(rows) for rows in by_brand.values())):
        for rows in by_brand.values():
            if rank < len(rows):
                promotable.append(rows[rank])

    # Rescan the surviving rows for what can pay: a brand is droppable only
    # down to its own reserve, worst-scoring row first.
    held = Counter(c.get('brand') for c in shortlist)
    droppable = []
    for i in range(len(shortlist) - 1, -1, -1):
        if len(droppable) >= len(promotable):
            break
        brand = shortlist[i].get('brand')
        if held[brand] > reserve_for(brand):
            held[brand] -= 1
            droppable.append(i)
    if not droppable:
        return shortlist

    dropped = set(droppable)
    merged = [c for i, c in enumerate(shortlist) if i not in dropped]
    merged += promotable[:len(droppable)]
    merged.sort(key=lambda c: c['_score'], reverse=True)
    return merged


# =====================================================================
# Stage D — LLM ranking
# =====================================================================

def _llm_rank(candidates, limit, season, genders, style_categories, user_vision,
              brand_overviews, own_bmd_brands=None, min_own_brand=0):
    if not candidates:
        return []

    own_bmd_brands = own_bmd_brands or set()
    own_available = any(c.get('brand') in own_bmd_brands for c in candidates)
    # Only ask for the quota when there is something to fill it with, so the
    # prompt never pushes the model toward inventing or padding.
    quota = min_own_brand if (min_own_brand and own_available) else 0

    brands_present = sorted({c.get('brand') for c in candidates if c.get('brand')})

    model = frappe.get_site_config().get('garment_matching_model') or _RANK_MODEL
    system_prompt = _rank_system_prompt(
        limit, quota, has_vision=bool(user_vision), brand_count=len(brands_present),
    )
    user_prompt = _rank_user_prompt(
        candidates, limit, season, genders, style_categories, user_vision,
        brand_overviews, own_bmd_brands, brands_present,
    )

    try:
        raw = llm.get_claude_response(
            system_prompt, user_prompt, ret_type='list',
            model=model, max_tokens=_RANK_MAX_TOKENS,
        )
    except Exception:
        frappe.log_error(frappe.get_traceback(), 'garment_matching._llm_rank/llm')
        return None

    by_id = {c['name']: c for c in candidates}
    picked, seen = [], set()
    for item in raw or []:
        cid = (item or {}).get('id')
        if cid in by_id and cid not in seen:
            seen.add(cid)
            c = dict(by_id[cid])
            c['_rationale'] = item.get('rationale')
            picked.append(c)
        if len(picked) >= limit:
            break

    return picked


def _rank_system_prompt(limit, min_own_brand=0, has_vision=False, brand_count=0):
    own_brand_rule = ''
    if min_own_brand:
        own_brand_rule = f"""
- Candidates marked own=yes belong to the brand the brief is being designed for; the rest are \
competitors. Include at least {min_own_brand} own=yes garments among your picks, choosing the \
best-fitting ones — the studio needs its own shelf represented alongside the market. Rank them \
on merit like any other candidate; do not move a weak own=yes garment to the top just to \
satisfy this. If fewer than {min_own_brand} own=yes candidates genuinely fit the brief's \
garment type, return only the ones that do."""

    spread_rule = ''
    if brand_count > 1:
        spread_rule = f"""
- The candidates span {brand_count} brands, and this list is read as a view of the market as \
much as a set of references. Spread your picks across brands: where a brand has a candidate \
that genuinely fits the brief, prefer giving that brand a slot over taking a third or fourth \
garment from a brand you have already picked. This breaks ties between comparable garments — \
it is not a licence to include a poor one, so never pick a brand's garment that does not fit \
the brief merely to have that brand represented."""

    vision_rule = ''
    priority = ('Priority order when trading off matches: (1) gender fit, (2) style category '
                'match, (3) seasonal fabric/silhouette fit as above, (4) brand DNA.')
    if has_vision:
        priority = ('Priority order when trading off matches: (1) gender fit, (2) fidelity to '
                    'the Vision, (3) style category match, (4) seasonal fabric/silhouette fit '
                    'as above, (5) brand DNA.')
        vision_rule = """
- The Vision is the designer's own words for what they are trying to make, and it is the \
strongest signal in this brief after gender. Read it closely for the specifics it names — \
fabric and handle, silhouette and fit, colour palette, detailing and trims, mood, and end use — \
and prefer a garment that echoes those specifics over one that merely sits in the right \
category. Where the Vision explicitly asks for something the seasonal or style-category logic \
above would rank down, the Vision wins; say so in that garment's rationale.
- Each candidate may carry a visionMatch list: the Vision's own words that appear in that \
garment's product text. Treat it as a hint about where to look, never as a score — a long \
visionMatch on a garment that is plainly wrong for the brief counts for nothing, and a garment \
with a thin description can still be the best match. Judge the garment, not the list.
- Where the Vision is specific, make the rationale specific back: name the shared fabric, \
colour, silhouette or detail rather than saying it "matches the vision"."""

    return f'''You are an expert fashion buyer and stylist for an apparel design studio, \
selecting reference garments to seed a new design brief.

You will be given a design brief and a numbered list of candidate garments (each with an \
id, brand, an own=yes marker on the brief's own brand, product name, category, colour, fabric \
composition, a sleeves note when the data gives real evidence either way ("has sleeves" / \
"sleeveless/strapless" — absent means unknown, not sleeveless), a short description, and a \
visionMatch list where the garment's own text uses the brief's vision words). Pick \
the {limit} candidates that best match the brief and rank them best-first.

Seasonal fabric logic (apply this explicitly, it matters as much as category/style):
- SS (Spring/Summer): favour lightweight, breathable fabrics (cotton, linen, viscose, \
chiffon, mesh), short-sleeve/sleeveless/half-sleeve silhouettes, brighter/lighter constructions.
- AW (Autumn/Winter): favour heavier, insulating fabrics (wool, fleece, cashmere, tweed, \
quilted/padded constructions), full-sleeve/long-sleeve silhouettes, layering pieces.
A garment that clashes with the brief's season (e.g. a padded wool coat for an SS brief) \
should rank low even if it matches the style category, unless nothing better is available.

{priority}

Rules:
- Only choose from the given candidate ids. Never invent an id.
- Return between 0 and {limit} results — fewer is fine if few candidates truly fit; do not \
pad the list with weak matches just to reach {limit}.{vision_rule}{own_brand_rule}{spread_rule}
- Output ONLY a JSON array, each item: {{"id": "<candidate id>", "rationale": "<one short \
sentence, specific to this garment>"}}. No prose outside the JSON.'''


def _rank_user_prompt(candidates, limit, season, genders, style_categories, user_vision,
                      brand_overviews, own_bmd_brands=None, brands_present=None):
    own_bmd_brands = own_bmd_brands or set()

    lines = ['DESIGN BRIEF']
    # The vision leads, on its own lines and untruncated. It is the top-weighted
    # signal, and a paragraph of intent flattened into a one-line `Vision: ...`
    # field reads as one more attribute rather than as the point of the brief.
    if user_vision:
        lines += [
            "Vision — the designer's own words for this brief. Weigh this above everything "
            'except gender:',
            user_vision,
            '',
        ]
    else:
        lines.append('Vision: (not specified)')
    lines += [
        f'Season: {season or "(not specified)"}',
        f'Genders: {", ".join(genders) or "(not specified)"}',
        f'Style categories: {", ".join(style_categories) or "(not specified)"}',
    ]
    if brands_present and len(brands_present) > 1:
        lines.append(
            f'Brands in the candidate list ({len(brands_present)}): '
            + ', '.join(brands_present)
        )
    if brand_overviews:
        lines.append('Brand DNA:')
        for bo in brand_overviews:
            bo = _as_dict(bo)
            overview = bo.get('overview')
            if overview:
                lines.append(f'- {bo.get("brandId", "brand")}: {json.dumps(overview)[:1200]}')

    lines += ['', f'CANDIDATES (pick up to {limit})']
    for c in candidates:
        fields = {
            'id': c.get('name'),
            'brand': c.get('brand'),
            'own': 'yes' if c.get('brand') in own_bmd_brands else None,
            'product': c.get('product_name'),
            'category': '/'.join(x for x in [c.get('category'), c.get('subcategories')] if x),
            'gender': c.get('gender'),
            'colour': c.get('selected_color'),
            'fabric': c.get('composition') or c.get('blend'),
            'sleeves': c.get('_sleeve_note'),
            'about': (c.get('about') or '')[:160] or None,
            'visionMatch': ', '.join(c.get('_vision_hits') or []) or None,
        }
        bits = [f'{k}={v}' for k, v in fields.items() if v]
        lines.append('- ' + ' | '.join(bits))

    if user_vision:
        # Restated after the candidates so the Vision frames the actual decision
        # rather than sitting a hundred candidate lines upstream of it.
        lines += [
            '',
            'Rank primarily on how closely each garment realises the Vision above, within '
            "the brief's gender and style categories.",
        ]

    return '\n'.join(lines)


# =====================================================================
# Stage D2 — own-brand quota
# =====================================================================

def _enforce_own_brand_quota(ranked, pool, own_bmd_brands, limit, min_own_brand, style_categories):
    '''
    Guarantee `min_own_brand` of the input brand's own garments in the ranking,
    backfilling from `pool` (score-sorted, the full untruncated candidate set)
    when the ranker returned fewer, and paying for them with the weakest
    competitor entries so the response still holds at `limit`.

    Only garments matching the brief's style categories are eligible, on the
    same `_classification_text` test target retail uses — a quota filled with
    the brand's socks for a Dresses brief is worse than an unfilled one. When
    the brief carries no style signal at all there is nothing to narrow by, so
    the brand's whole gender-filtered pool is eligible.

    The quota therefore stops short rather than padding: a brand with one
    on-category garment contributes one, and `quota['reason']` says so. Returns
    (garments, quota_report).
    '''
    report = {
        'target': 0,
        'eligible': 0,
        'fromRanking': 0,
        'backfilled': 0,
        'final': sum(1 for c in ranked if c.get('brand') in own_bmd_brands),
        'reason': None,
    }

    if not own_bmd_brands:
        report['reason'] = 'brand_not_in_catalog'
        return ranked, report
    if not min_own_brand:
        report['reason'] = 'quota_disabled'
        return ranked, report

    report['target'] = min_own_brand

    matcher = _style_matcher(style_categories)
    eligible = [
        c for c in pool
        if c.get('brand') in own_bmd_brands
        and (matcher is None or matcher.search(_classification_text(c)))
    ]
    report['eligible'] = len(eligible)

    present = {c.get('name') for c in ranked if c.get('brand') in own_bmd_brands}
    report['fromRanking'] = len(present)
    if len(present) >= min_own_brand:
        report['reason'] = 'satisfied_by_ranking'
        return ranked, report
    if not eligible:
        report['reason'] = 'no_on_category_own_garments'
        return ranked, report

    seen = {c.get('name') for c in ranked}
    backfill = []
    for c in eligible:
        if len(present) + len(backfill) >= min_own_brand:
            break
        if c.get('name') in seen:
            continue
        item = dict(c)
        item['_rationale'] = item.get('_rationale') or _OWN_BRAND_BACKFILL_RATIONALE
        backfill.append(item)

    if not backfill:
        report['reason'] = 'no_on_category_own_garments'
        return ranked, report

    result = list(ranked)
    # Appended rather than promoted: these are heuristic picks the ranker did
    # not choose, so they sit below the ones it did instead of claiming a
    # best-first position they have not earned.
    overflow = len(result) + len(backfill) - limit
    for i in range(len(result) - 1, -1, -1):
        if overflow <= 0:
            break
        if result[i].get('brand') not in own_bmd_brands:
            del result[i]
            overflow -= 1
    result += backfill

    report['backfilled'] = len(backfill)
    report['final'] = sum(1 for c in result if c.get('brand') in own_bmd_brands)
    report['reason'] = (
        'satisfied_by_backfill' if report['final'] >= min_own_brand
        else 'too_few_on_category_own_garments'
    )
    return result[:limit], report


# =====================================================================
# Stage D3 — competitor spread
# =====================================================================

def _enforce_competitor_spread(ranked, pool, own_bmd_brands, competitor_brands, limit,
                               min_own_brand, enabled, style_categories):
    '''
    Guarantee every resolved competitor at least one entry in the ranking,
    backfilling its best on-category garment from `pool` (score-sorted, the
    full untruncated candidate set) when the ranker gave it none.

    A shortlist is read as a market view, and one where four of eight garments
    come from a single competitor and five other competitors are absent is a
    worse answer than one spanning them — the absent brands look uncovered
    rather than unpicked. Left to itself the ranker skews this way often: brands
    differ hugely in catalogue size and in how much descriptive text the scraper
    captured, so the deepest, wordiest catalogue wins slots on volume.

    Eligibility uses the same `_style_matcher`/`_classification_text` test as
    the own-brand quota, so this never forces a competitor's socks into a
    Dresses brief; a competitor with nothing on-category is reported under
    `noOnCategoryGarments` and skipped rather than filled with something wrong.

    Room comes from the weakest end of the ranking, and only from entries that
    can spare it: a brand that would be left with nothing is never the one that
    pays, and own-brand entries are only taken back above `min_own_brand`, so
    the own-brand quota stays the harder of the two guarantees. When `limit`
    cannot cover every competitor the strongest-scoring ones win the slots and
    the rest come back in `unrepresented`.

    Returns (garments, spread_report).
    '''
    report = {
        'enforced': bool(enabled),
        'competitorsInCatalog': len(competitor_brands),
        'eligible': 0,
        'fromRanking': 0,
        'backfilled': 0,
        'covered': 0,
        'noOnCategoryGarments': [],
        'unrepresented': [],
        'reason': None,
    }

    if not enabled:
        report['reason'] = 'disabled'
        return ranked, report
    if not competitor_brands:
        report['reason'] = 'no_competitors_in_catalog'
        return ranked, report

    matcher = _style_matcher(style_categories)
    # `pool` is best-first, so each brand's list is too and [0] is its best row.
    by_brand = {}
    for c in pool:
        brand = c.get('brand')
        if brand not in competitor_brands:
            continue
        if matcher is not None and not matcher.search(_classification_text(c)):
            continue
        by_brand.setdefault(brand, []).append(c)

    report['eligible'] = len(by_brand)
    report['noOnCategoryGarments'] = sorted(competitor_brands - set(by_brand))
    if not by_brand:
        report['reason'] = 'no_on_category_competitor_garments'
        return ranked, report

    result = list(ranked)
    covered = {c.get('brand') for c in result} & set(by_brand)
    report['fromRanking'] = len(covered)

    # Strongest-first, so a tight `limit` spends its slots on the competitors
    # with the closest garment to the brief rather than on alphabetical luck.
    missing = sorted(
        (b for b in by_brand if b not in covered),
        key=lambda b: by_brand[b][0]['_score'], reverse=True,
    )

    seen = {c.get('name') for c in result}
    for i, brand in enumerate(missing):
        pick = next((c for c in by_brand[brand] if c.get('name') not in seen), None)
        if pick is None:
            continue
        if len(result) >= limit:
            drop = _spread_drop_index(result, own_bmd_brands, min_own_brand)
            if drop is None:
                # Nothing left that can spare a slot — every later brand is in
                # the same position, so stop rather than retrying each one.
                report['unrepresented'] = sorted(missing[i:])
                break
            seen.discard(result[drop].get('name'))
            del result[drop]
        item = dict(pick)
        # Appended rather than promoted, like the own-brand backfill: the ranker
        # did not choose these, so they sit below the ones it did.
        item['_rationale'] = item.get('_rationale') or _COMPETITOR_BACKFILL_RATIONALE
        result.append(item)
        seen.add(item.get('name'))
        covered.add(brand)
        report['backfilled'] += 1

    report['covered'] = len(covered)
    if report['unrepresented']:
        report['reason'] = 'limit_reached'
    elif report['backfilled']:
        report['reason'] = 'satisfied_by_backfill'
    else:
        report['reason'] = 'satisfied_by_ranking'
    return result[:limit], report


def _spread_drop_index(result, own_bmd_brands, min_own_brand):
    '''
    Index of the entry that can best spare its slot for an uncovered
    competitor, or None when nothing can.

    Scanned worst-ranked first, since that is the pick with least to lose. A
    brand holding a single entry is never touched — taking it would uncover one
    competitor to cover another — and own-brand entries are only available
    above `min_own_brand`, which keeps the own-brand quota intact.
    '''
    counts = Counter(c.get('brand') for c in result)
    own_total = sum(n for b, n in counts.items() if b in own_bmd_brands)

    for i in range(len(result) - 1, -1, -1):
        brand = result[i].get('brand')
        if brand in own_bmd_brands:
            if own_total > min_own_brand:
                return i
            continue
        if counts.get(brand, 0) > 1:
            return i
    return None

# =====================================================================
# Stage E — target retail (input brand only, normalised to USD)
# =====================================================================

def _target_retail(candidates, own_bmd_brands, style_categories):
    '''
    The input brand's own average shelf price for the brief, in USD.

    Scope is deliberately narrower than the ranking above: only the brand's own
    Brand Master Data rows (never a competitor's), already gender-filtered by
    `_fetch_candidates`, then narrowed again to the rows whose garment type
    matches the brief's style categories.

    Returns a dict that is always the same shape. `value` is None whenever the
    number would be misleading, with `reason` saying which of the four ways it
    fell through: the brand isn't in the catalog, it has no rows for this
    gender, nothing it sells matches the style categories, or the rows that do
    match carry no price we can read a currency off. Callers should render the
    reason rather than a zero.
    '''
    result = {
        'value': None,
        'currency': 'USD',
        'basis': None,
        'reason': None,
        'stats': None,
        'coverage': {
            'brandGarments': 0,
            'styleMatched': 0,
            'priced': 0,
            'unpriced': 0,
            'duplicatesCollapsed': 0,
        },
        'currencyMix': {},
        'ratesUsed': {},
    }

    if not own_bmd_brands:
        result['reason'] = 'brand_not_in_catalog'
        return result

    own = [c for c in candidates if c.get('brand') in own_bmd_brands]
    result['coverage']['brandGarments'] = len(own)
    if not own:
        result['reason'] = 'no_garments_for_brand_and_gender'
        return result

    matcher = _style_matcher(style_categories)
    if matcher is None:
        # No style categories on the brief (or none of them carried a usable
        # word) — there is nothing to narrow by, so the brand+gender pool IS
        # the answer. Flagged so the caller can see the looser basis.
        matched = own
        result['basis'] = 'brand_gender'
    else:
        matched = [c for c in own if matcher.search(_classification_text(c))]
        result['basis'] = 'brand_gender_style'
    result['coverage']['styleMatched'] = len(matched)

    if not matched:
        result['reason'] = 'no_style_match'
        return result

    master = _currency_master()
    brand_currencies = _brand_currency_overrides()

    priced, seen, mix, used = [], set(), {}, {}
    for c in matched:
        amount, code = _price_in_usd(c, master, brand_currencies)
        if amount is None:
            continue
        # Brand Master Data carries the same product once per collection it sits
        # in (and the scraper occasionally double-writes a row), which would let
        # one style pull the average toward itself. Collapse on what actually
        # identifies a shelf price: brand, product name, and the price itself.
        key = (c.get('brand'), (c.get('product_name') or '').strip().lower(), round(amount, 2))
        if key in seen:
            result['coverage']['duplicatesCollapsed'] += 1
            continue
        seen.add(key)
        priced.append(amount)
        mix[code] = mix.get(code, 0) + 1
        used[code] = master['rates'][code]

    result['coverage']['priced'] = len(priced)
    result['coverage']['unpriced'] = len(matched) - len(priced) - result['coverage']['duplicatesCollapsed']
    result['currencyMix'] = dict(sorted(mix.items()))
    result['ratesUsed'] = used

    if not priced:
        result['reason'] = 'no_readable_prices'
        return result

    priced.sort()
    n = len(priced)
    mid = n // 2
    result['value'] = round(sum(priced) / n, 2)
    result['stats'] = {
        'count': n,
        # Median travels with the mean because scraped catalogs carry the odd
        # order-of-magnitude typo, which the mean alone would hide.
        'median': round(priced[mid] if n % 2 else (priced[mid - 1] + priced[mid]) / 2, 2),
        'min': round(priced[0], 2),
        'max': round(priced[-1], 2),
    }
    return result


# --- style category matching ---

# Style categories are free text a merchandiser typed into `Brand Gender Style
# Category.style_categories` ("Tops", "Co-ords", "Sleeve Less Top", "Long Nighty").
# Brand Master Data's own `category` is scraper-assigned and just as free
# ("Clothing", "Camisa", "Current Rotation", "Women's Hoodies & Sweatshirts"), so
# the two sides almost never match literally. This maps each style category onto
# the garment words that do turn up in scraped category/product-name text —
# including the Spanish ones Zara's and Mango's feeds use, and the parent/child
# relationships from Garment Group -> Garment Category (a "Tops" brief legitimately
# covers tees, shirts and polos; a "T-Shirts" brief does not cover shirts).
_STYLE_SYNONYMS = {
    # tops
    'top': ('top', 'tee', 't shirt', 'tshirt', 'shirt', 'blouse', 'polo', 'tank',
            'camisole', 'cami', 'henley', 'bodysuit', 'camisa', 'camiseta', 'blusa'),
    'tee': ('tee', 't shirt', 'tshirt', 'camiseta'),
    'long tee': ('tee', 't shirt', 'tshirt', 'camiseta'),
    't shirt': ('t shirt', 'tshirt', 'tee', 'camiseta'),
    'shirt': ('shirt', 'camisa', 'blouse', 'blusa'),
    'blouse': ('blouse', 'blusa', 'camisa'),
    'polo': ('polo',),
    'henley': ('henley',),
    'tank': ('tank', 'tank top', 'camisole', 'cami', 'sleeveless'),
    'tank top': ('tank', 'tank top', 'camisole', 'cami', 'sleeveless'),
    'sleeve less top': ('sleeveless', 'strapless', 'tank', 'camisole', 'cami'),
    'camisole': ('camisole', 'cami', 'tank'),
    'crop top': ('crop', 'crop top'),
    'vest': ('vest', 'gilet', 'tank', 'chaleco'),

    # bottoms
    'bottom': ('bottom', 'pant', 'trouser', 'jean', 'denim', 'short', 'skirt',
               'legging', 'jogger', 'chino', 'capri', 'pantalon', 'falda',
               'bermuda', 'culotte'),
    'pant': ('pant', 'trouser', 'chino', 'slack', 'pantalon'),
    'trouser': ('trouser', 'pant', 'chino', 'slack', 'pantalon'),
    'jean': ('jean', 'denim', 'vaquero'),
    'short': ('short', 'bermuda'),
    'bermuda': ('bermuda', 'short'),
    'capri': ('capri', 'cropped pant', 'three quarter'),
    'skirt': ('skirt', 'falda'),
    'legging': ('legging', 'jegging', 'tight'),
    'jogger': ('jogger', 'jogging', 'sweatpant', 'track pant'),

    # dresses / one-pieces
    'dress': ('dress', 'vestido', 'gown', 'one piece', 'frock'),
    'jumpsuit': ('jumpsuit', 'romper', 'playsuit', 'dungaree', 'overall', 'mono'),

    # outerwear / knitwear
    'jacket': ('jacket', 'blazer', 'bomber', 'parka', 'anorak', 'windbreaker',
               'cazadora', 'chaqueta'),
    'outerwear': ('outerwear', 'jacket', 'coat', 'parka', 'puffer', 'anorak',
                  'blazer', 'gilet', 'cazadora', 'abrigo', 'windbreaker'),
    'coat': ('coat', 'overcoat', 'trench', 'abrigo'),
    'knitwear': ('knit', 'knitwear', 'sweater', 'jumper', 'cardigan', 'pullover',
                 'jersey', 'punto'),
    'sweater': ('sweater', 'jumper', 'pullover', 'cardigan', 'knit', 'jersey'),
    'cardigan': ('cardigan',),
    'hoody': ('hoody', 'hoodie', 'hooded', 'sudadera'),
    'sweatshirt': ('sweatshirt', 'crewneck', 'crew neck', 'sudadera'),

    # sets
    'set': ('set', 'co ord', 'coord', 'two piece', 'matching set', 'conjunto',
            'lounge set', 'tracksuit'),
    'co ord': ('co ord', 'coord', 'set', 'two piece', 'matching set'),
    'lounge set': ('lounge', 'lounge set', 'loungewear', 'set'),
    'tracksuit': ('tracksuit', 'track suit', 'sweatsuit', 'chandal'),

    # intimates
    'intimate': ('intimate', 'innerwear', 'underwear', 'lingerie', 'bra',
                 'bralette', 'bratop', 'brief', 'boxer', 'trunk', 'thong',
                 'panty', 'panties'),
    'underwear': ('underwear', 'innerwear', 'brief', 'boxer', 'trunk', 'thong',
                  'panty', 'panties'),
    'innerwear': ('innerwear', 'underwear', 'brief', 'boxer', 'trunk', 'vest'),
    'brief': ('brief', 'panty', 'panties'),
    'boxer': ('boxer', 'trunk'),
    'trunk': ('trunk', 'boxer'),
    'thong': ('thong',),
    'bra': ('bra', 'bralette', 'bratop', 'sujetador'),
    'sports bra': ('sports bra', 'sport bra', 'bra', 'bralette'),
    'bikni': ('bikini', 'bikni', 'swim', 'swimsuit', 'swimwear'),
    'bikini': ('bikini', 'swim', 'swimsuit', 'swimwear'),

    # sleep / lounge
    'sleep': ('sleep', 'sleepwear', 'nightwear', 'pyjama', 'pajama', 'pijama',
              'nighty', 'nightdress', 'loungewear', 'robe'),
    'sleepwear': ('sleepwear', 'sleep', 'nightwear', 'pyjama', 'pajama', 'pijama',
                  'nighty', 'nightdress', 'robe'),
    'pyjama': ('pyjama', 'pajama', 'pijama', 'sleepwear', 'nightwear'),
    'nighty': ('nighty', 'nightdress', 'nightgown', 'nightwear', 'sleepwear'),
    'long nighty': ('nighty', 'nightdress', 'nightgown', 'nightwear', 'sleepwear'),

    # merchandising tiers rather than silhouettes — match the collection naming
    # brands actually ship these under, since there is no garment shape to match
    'basic': ('basic', 'essential'),
    'essential': ('essential', 'basic'),
    'activewear': ('activewear', 'active', 'sport', 'performance', 'training', 'gym'),
}

# Dropped from the free-word fallback: they carry no garment signal and would
# match most of the catalog on their own.
_STYLE_STOPWORDS = frozenset((
    'and', 'the', 'for', 'all', 'new', 'women', 'womens', 'men', 'mens', 'kid',
    'kids', 'girl', 'girls', 'boy', 'boys', 'wear', 'clothing', 'apparel',
    'collection', 'current', 'rotation', 'long', 'short', 'less', 'sleeve',
))

# "Short Sleeve Tee" is a tee, not a pair of shorts. Sleeve phrasing is stripped
# from the classification text before matching so it cannot satisfy a Shorts brief.
_SLEEVE_PHRASE_RE = re.compile(r'\b(?:short|long|full|half|three quarter|elbow) sleeve\w*')


def _style_matcher(style_categories):
    '''
    One compiled regex matching any garment word implied by the brief's style
    categories, or None when the brief carries no usable style signal at all.
    '''
    terms = set()
    for raw in style_categories:
        # "Hoodies & Sweatshirts" and "Tops/Tees" are two categories in one cell.
        for part in re.split(r'[&/,+]|\band\b', (raw or '').lower()):
            key = _norm_style_key(part)
            if not key:
                continue
            synonyms = _STYLE_SYNONYMS.get(key)
            if synonyms:
                terms.update(synonyms)
                continue
            # Unknown category ("Current Rotation", a brand's own coinage). Try
            # each of its words against the map, and fall back to the word
            # itself so an exact hit in the scraped text still counts.
            for word in key.split():
                if len(word) < 3 or word in _STYLE_STOPWORDS:
                    continue
                singular = _singular(word)
                terms.update(_STYLE_SYNONYMS.get(singular) or (singular,))

    if not terms:
        return None

    alternatives = '|'.join(re.escape(t) for t in sorted(terms, key=len, reverse=True))
    # Trailing (?:e?s)? so a singular term still matches the plural the scraped
    # text usually uses ("dress" -> "dresses", "top" -> "tops").
    return re.compile(rf'\b(?:{alternatives})(?:e?s)?\b')


def _norm_style_key(text):
    ''' "T-Shirts" -> "t shirt", "Sleeve Less Tops" -> "sleeve less top". '''
    words = re.sub(r'[^a-z0-9]+', ' ', (text or '').lower()).split()
    if not words:
        return ''
    words[-1] = _singular(words[-1])
    return ' '.join(words)


def _singular(word):
    if word.endswith('ies') and len(word) > 4:
        return word[:-3] + 'y'
    if word.endswith(('sses', 'shes', 'ches', 'xes')):
        return word[:-2]
    if word.endswith('s') and not word.endswith('ss') and len(word) > 3:
        return word[:-1]
    return word


def _classification_text(c):
    '''
    Product name + scraped category/subcategory, flattened to plain lowercase
    words. Deliberately NOT the `about`/`product_details` blob that
    `_candidate_text` uses — marketing copy like "great to dress up" would
    otherwise pull a shirt into a Dresses brief.
    '''
    raw = ' '.join(str(x) for x in (
        c.get('product_name'), c.get('category'), c.get('subcategories')) if x)
    return _SLEEVE_PHRASE_RE.sub(' ', re.sub(r'[^a-z0-9]+', ' ', raw.lower())).strip()


# --- price -> USD ---

_PRICE_AMOUNT_RE = re.compile(r'\d[\d,]*(?:\.\d+)?')

# Path segment / ccTLD -> currency, for scraped prices that carry no symbol at
# all. The scraper hits a brand's localised storefront, so the locale in the
# product URL is the currency the price was shown in: shop.mango.com/gb/... is
# GBP, uniqlo.com/in/... is INR, pucci.com/en-us/... is USD.
_REGION_CURRENCY = {
    'in': 'INR', 'gb': 'GBP', 'uk': 'GBP', 'us': 'USD', 'ca': 'CAD',
    'au': 'AUD', 'nz': 'NZD', 'jp': 'JPY', 'sg': 'SGD', 'ae': 'AED',
    'de': 'EUR', 'fr': 'EUR', 'es': 'EUR', 'it': 'EUR', 'nl': 'EUR',
    'ie': 'EUR', 'pt': 'EUR', 'at': 'EUR', 'be': 'EUR', 'fi': 'EUR',
}


def _price_in_usd(c, master, brand_currencies):
    '''
    (amount_in_usd, currency_code) for one candidate, or (None, None) when the
    price can't be read or its currency can't be established. Never guesses a
    currency: an unattributable price is dropped from the average rather than
    silently counted as dollars.
    '''
    raw = c.get('price')
    if raw in (None, ''):
        return None, None

    match = _PRICE_AMOUNT_RE.search(str(raw))
    if not match:
        return None, None
    try:
        # Talbots and Lacoste store list and sale price in one cell
        # ("\n$79.50\n\n\n$64.99"). The first number is the list price, which is
        # the shelf price a retail benchmark is asking about.
        amount = float(match.group().replace(',', ''))
    except ValueError:
        return None, None
    if amount <= 0:
        return None, None

    rates = master['rates']
    code = _detect_currency(raw, c.get('product_url'), c.get('brand'), master, brand_currencies)
    if not code or code not in rates:
        return None, None

    return amount * rates[code], code


def _detect_currency(raw, product_url, brand, master, brand_currencies):
    ''' Symbol in the price text first, then the URL's locale, then a
    per-brand override — evidence on the row always beats configuration. '''
    text = str(raw)
    for symbol, code in master['symbols']:
        if symbol in text:
            return code

    code = _currency_from_url(product_url)
    if code:
        return code

    return brand_currencies.get(brand)


def _currency_from_url(url):
    if not url:
        return None
    match = re.match(r'https?://([^/]+)(/.*)?$', str(url).strip().lower())
    if not match:
        return None
    host, path = match.group(1), match.group(2) or '/'

    for segment in path.split('/'):
        # "/gb/en/..." or "/en-us/..."
        if len(segment) == 2 and segment.isalpha():
            code = _REGION_CURRENCY.get(segment)
        elif len(segment) == 5 and segment[2] == '-' and segment.replace('-', '').isalpha():
            code = _REGION_CURRENCY.get(segment[3:])
        else:
            continue
        if code:
            return code

    # ccTLD, e.g. muji.in
    tld = host.rsplit('.', 1)[-1]
    return _REGION_CURRENCY.get(tld) if len(tld) == 2 else None


def _currency_master():
    '''
    The Currency Rate table read once per request, as
    {'rates': {code: usd_per_unit}, 'symbols': [(symbol, code), ...]}.

    Both halves come from the same fetch so a currency added in desk teaches
    detection and conversion at once, and so a pool of a few thousand garments
    costs one query rather than one per row. `symbols` is sorted longest-first
    so "C$" is matched before "$" swallows it.

    Falls back to USD-only if the doctype has not been migrated yet or the table
    is empty — the endpoint's job is ranking garments, and a missing rate master
    should cost it the target retail figure, not the whole response.
    '''
    try:
        if not frappe.db.exists('DocType', 'Currency Rate'):
            return _fallback_currency_master()
        rows = frappe.get_all(
            'Currency Rate',
            fields=['currency_code', 'currency_symbol', 'rate_to_usd'],
            ignore_permissions=True,
        )
    except Exception:
        frappe.log_error(frappe.get_traceback(), 'garment_matching._currency_master')
        return _fallback_currency_master()

    rates, symbols = {}, {}
    for r in rows:
        code = (r.get('currency_code') or '').strip().upper()
        rate = float(r.get('rate_to_usd') or 0)
        if not code or rate <= 0:
            continue
        rates[code] = rate
        symbol = (r.get('currency_symbol') or '').strip()
        if symbol:
            symbols.setdefault(symbol, code)

    rates.setdefault('USD', 1.0)
    return {
        'rates': rates,
        'symbols': sorted(symbols.items(), key=lambda kv: -len(kv[0])),
    }


def _fallback_currency_master():
    ''' USD-only, so a missing rate master degrades to "dollar-priced brands
    still work, everything else is dropped as unattributable". '''
    return {'rates': {'USD': 1.0}, 'symbols': [('$', 'USD')]}


def _brand_currency_overrides():
    '''
    Last-resort {bmd_brand: currency_code} from site_config's
    `garment_matching_brand_currency`, for brands whose scraped prices carry
    neither a symbol nor a locale in the URL (Vince and MATE the Label today).
    Only consulted after both of those, so it can never override real evidence.
    '''
    raw = frappe.get_site_config().get('garment_matching_brand_currency') or {}
    if not isinstance(raw, dict):
        return {}
    return {k: str(v).strip().upper() for k, v in raw.items() if v}


# =====================================================================
# Stage F — target FOB (the brand's own customs shipments, per piece, USD)
# =====================================================================
#
# Target retail and target FOB are parallel samples of the same garment
# definition, one at factory cost and one at shelf price. They never join —
# there is no key linking a shipment to a catalogue product — so their counts
# differing is expected, not a bug.

SHIPMENT_DOCTYPE = 'NitvaShipment'

# One rule decides every exclusion in this stage:
#
#     A row counts only if its price is quoted per production unit of a
#     finished garment.
#
# Same test each time, applied to a different column. Sets and packs price per
# pack; KGS prices per kilo and CTN per carton; a sample prices a development
# piece; and a multi-piece set or an accessory is not a finished garment.
# RMG passes — it is a finished garment quoted per piece.
#
# Not academic: `standard_unit_rate` averaged across units reads $49.87 against
# $5.30 for pieces alone, because ~2k per-kilo fabric shipments sit in the same
# column. Old Navy comes out at $6.28 instead of $2.13.
_PER_PIECE_UNIT = 'NOS'
_PRODUCTION_ORDER_TYPES = ('Production', 'RMG (Ready-Made Garment)')
_NON_GARMENT_CATEGORIES = ('Multi-piece Sets', 'Accessories', 'Non-Product / Reference Only')
_SET_OR_PACK_PATTERN = r'\b(set|pack)\b'

# What may sit between two characters of a brand name in a consignee string:
# whitespace, and the punctuation trade names actually use ("J.Crew", "H&M",
# "Levi's", "Toad&Co"). Kept to this set rather than [^a-zA-Z0-9] so the pattern
# stays a name matcher rather than an arbitrary-gap matcher.
_CONSIGNEE_SEPARATOR = r"[\s._&'-]*"

# Brief gender -> the values the shipment decoder emits. Deliberately excludes
# 'Unspecified': a gender the decoder could not read is not evidence of a match,
# and folding those 8k rows in would quietly widen every gendered brief.
_NITVA_GENDERS = {
    'Women': ('Women', 'Men/Women'),
    'Men': ('Men', 'Men/Women'),
    'Unisex': ('Men/Women',),
    'Kids': ('Kids',),
    'Girls': ('Kids',),
    'Boys': ('Kids',),
    'Toddler': ('Toddler',),
}


def _target_fob(input_brand_names, genders, style_categories):
    '''
    The input brand's average per-piece factory price, in USD, for the brief.

    Reads customs shipment records (`NitvaShipment`) consigned to the brand,
    narrowed by the same gender and style categories the retail side uses, and
    restricted to rows that satisfy the production-unit rule above.

    `funnel` reports the row count surviving each stage. It is the thing that
    explains a zero: a brand absent from the shipment data, a brand that only
    ships packs, or a style category nobody has shipped all read as `value:
    None` otherwise, and the funnel says which.
    '''
    result = {
        'value': None,
        'currency': 'USD',
        'basis': None,
        'reason': None,
        'stats': None,
        'funnel': {},
        'consigneePattern': None,
        'matchedCategories': [],
    }

    try:
        if not frappe.db.exists('DocType', SHIPMENT_DOCTYPE):
            result['reason'] = 'shipment_data_unavailable'
            return result

        pattern = _consignee_pattern(input_brand_names)
        if not pattern:
            result['reason'] = 'brand_not_in_catalog'
            return result
        result['consigneePattern'] = pattern

        # Each stage adds predicates to the one before it, so the counts read as
        # a funnel rather than as five unrelated numbers.
        stages = [('allShipments', [], [])]

        conds, vals = ['consignee_name REGEXP %s'], [pattern]
        stages.append(('brandMatched', list(conds), list(vals)))

        conds += [
            'standard_unit_rate > 0',
            'standard_unit = %s',
            'product_description NOT REGEXP %s',
            'decoded_order_type IN %s',
            'decoded_garment_category NOT IN %s',
        ]
        vals += [
            _PER_PIECE_UNIT,
            _SET_OR_PACK_PATTERN,
            _PRODUCTION_ORDER_TYPES,
            _NON_GARMENT_CATEGORIES,
        ]
        stages.append(('productionUnits', list(conds), list(vals)))

        basis = 'brand'
        nitva_genders = _nitva_genders(genders)
        if nitva_genders:
            conds.append('decoded_gender_age_group IN %s')
            vals.append(nitva_genders)
            basis = 'brand_gender'
        stages.append(('genderMatched', list(conds), list(vals)))

        categories = _nitva_categories(style_categories)
        result['matchedCategories'] = list(categories)
        if categories:
            conds.append('decoded_garment_category IN %s')
            vals.append(categories)
            basis = f'{basis}_style'
        stages.append(('styleMatched', list(conds), list(vals)))

        result['basis'] = basis
        for label, stage_conds, stage_vals in stages:
            result['funnel'][label] = _shipment_count(stage_conds, stage_vals)

        # A style brief whose categories map onto nothing the decoder emits is a
        # different failure from one where the brand simply never shipped them.
        if style_categories and not categories:
            result['reason'] = 'style_not_in_shipment_taxonomy'
            return result

        reason = _first_empty_stage(result['funnel'])
        if reason:
            result['reason'] = reason
            return result

        row = frappe.db.sql(
            f'''SELECT COUNT(*) n,
                       MIN(standard_unit_rate) mn,
                       MAX(standard_unit_rate) mx,
                       AVG(standard_unit_rate) av,
                       SUM(standard_unit_rate * standard_qty)
                           / NULLIF(SUM(standard_qty), 0) avw
                FROM `tab{SHIPMENT_DOCTYPE}`
                WHERE {" AND ".join(conds)}''',
            vals, as_dict=True,
        )[0]

        result['value'] = _round4(row['av'])
        result['stats'] = {
            'count': int(row['n'] or 0),
            'min': _round4(row['mn']),
            'max': _round4(row['mx']),
            'avg': _round4(row['av']),
            # Every shipment counts equally in `avg`, so a 50-piece order pulls
            # as hard as a 50,000-piece one. The weighted figure is the same
            # population priced by volume — carried alongside rather than
            # replacing `avg`, which is what the costing screens already quote.
            'avgWeighted': _round4(row['avw']),
        }
        return result

    except Exception:
        # A shipment-side failure must not cost the caller its garment ranking.
        frappe.log_error(frappe.get_traceback(), 'garment_matching._target_fob')
        result['reason'] = 'shipment_query_failed'
        return result


def _shipment_count(conds, vals):
    where = f'WHERE {" AND ".join(conds)}' if conds else ''
    return frappe.db.sql(
        f'SELECT COUNT(*) FROM `tab{SHIPMENT_DOCTYPE}` {where}', vals,
    )[0][0]


def _first_empty_stage(funnel):
    ''' The earliest funnel stage that emptied out, as a reason code — that is
    the stage worth reporting, since everything after it is empty by consequence. '''
    for label, reason in (
        ('allShipments', 'shipment_data_unavailable'),
        ('brandMatched', 'brand_not_in_shipments'),
        ('productionUnits', 'no_production_unit_shipments'),
        ('genderMatched', 'no_gender_match'),
        ('styleMatched', 'no_style_match'),
    ):
        if not funnel.get(label):
            return reason
    return None


def _consignee_pattern(brand_names):
    '''
    Word-bounded alternation over the brand's display names, tolerant of where
    each side happens to put its separators:

        H&M       -> \\bH[\\s._&'-]*M\\b            reaches "H M Hennes Mauritz GBC AB"
        Wearpact  -> \\bW[\\s._&'-]*e[...]t\\b      reaches "WEAR PACT LLC"

    The separator is optional *between every character*, because the two sides
    disagree in both directions — the brand name carries a separator the
    consignee lacks as often as the reverse. Matching word-for-word only handles
    the first case, which is how Wearpact's 649 shipments went missing.

    The word boundaries are what keeps that safe. An unbounded LIKE '%gap%' also
    matches "ADIDAS SINGAPORE PTE LTD" and "NIKE Global Trading BV Singapore" —
    the substring bug this codebase has already been bitten by once — whereas
    \\b...\\b cannot match mid-word however loose the middle is.
    '''
    alternatives = []
    for name in brand_names:
        chars = [c for c in (name or '') if c.isalnum()]
        # Two is the floor, not three: "H&M" carries exactly two alphanumerics
        # and is a real brand with 3.1k shipments. A single character would be a
        # coincidence waiting to happen once the separators are optional.
        if len(chars) >= 2:
            alternatives.append(_CONSIGNEE_SEPARATOR.join(re.escape(c) for c in chars))
    if not alternatives:
        return None
    return r'\b(?:' + '|'.join(alternatives) + r')\b'


def _nitva_genders(genders):
    mapped = []
    for g in _map_genders(genders):
        for value in _NITVA_GENDERS.get(g, ()):
            if value not in mapped:
                mapped.append(value)
    return tuple(mapped)


def _nitva_categories(style_categories):
    '''
    The shipment decoder's `decoded_garment_category` values the brief's style
    categories imply, matched with the same tolerant vocabulary the retail side
    uses — "Tops" reaches "Tops — T-Shirts" and "Tops — Polo Shirts" but not
    "Knitwear / Sweaters".

    Read off the table rather than hard-coded, so a category the decoder starts
    emitting is picked up without a code change.
    '''
    matcher = _style_matcher(style_categories)
    if matcher is None:
        return ()

    values = frappe.get_all(
        SHIPMENT_DOCTYPE, pluck='decoded_garment_category',
        distinct=True, ignore_permissions=True,
    )
    matched = [
        v for v in values
        if v and v not in _NON_GARMENT_CATEGORIES
        and matcher.search(re.sub(r'[^a-z0-9]+', ' ', v.lower()))
    ]
    return tuple(sorted(matched))


def _round4(value):
    return round(float(value), 4) if value is not None else None


# =====================================================================
# helpers
# =====================================================================

def _public_fields(c, brand_display=None, own_bmd_brands=None):
    brand_display = brand_display or {}
    return {
        'id': c.get('name'),
        'brand': brand_display.get(c.get('brand'), c.get('brand')),
        'isOwnBrand': c.get('brand') in (own_bmd_brands or set()),
        'productName': c.get('product_name'),
        'category': c.get('category'),
        'subcategories': c.get('subcategories'),
        'gender': c.get('gender'),
        'selectedColor': c.get('selected_color'),
        'availableColors': c.get('available_colors'),
        'fit': c.get('fit'),
        'composition': c.get('composition'),
        'imageUrl': c.get('image_url'),
        'price': c.get('price'),
        'productUrl': c.get('product_url'),
        'matchRationale': c.get('_rationale'),
        # The brief's own vision words this garment's text uses — the evidence
        # behind its placement, for a caller that wants to show why.
        'visionMatch': c.get('_vision_hits') or [],
    }


def _as_dict(value):
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        try:
            p = frappe.parse_json(value)
            return p if isinstance(p, dict) else {}
        except Exception:
            return {}
    return {}


def _as_list(value):
    if isinstance(value, list):
        return value
    if isinstance(value, str) and value.strip():
        try:
            p = frappe.parse_json(value)
            return p if isinstance(p, list) else []
        except Exception:
            return []
    return []
