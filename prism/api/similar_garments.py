'''
Similar-garment matching — finds the closest garments *we have already made*
(Sample Request, garment_element = 'Main Body') for a moodboard design brief.

Sibling of, but deliberately not a replacement for,
`moodboard.get_matching_garments()`. That endpoint starts from a free-text trend
forecast and asks the LLM to *invent* garment specs, then looks each one up.
This one starts from a brief that has already been resolved into a concrete list
of garments (name + silhouette + priority), so there is nothing left to invent —
the job is purely retrieval and ranking against a hard-filtered candidate pool.

Pipeline:
    A  hard filters      gender and style category are resolved against the live
                         Sample Request vocabulary and applied in SQL. A brief
                         for Women/Tops never sees a Men's jogger.
    B  candidate fetch   one pass over the pool, deduped to the latest row per
                         gsr_no (a style is re-sampled many times).
    C  heuristic score   per requested garment: season fit (GSM band +
                         construction + fibre + silhouette words), silhouette
                         token overlap, colour-family fit, own-category fit.
                         Bounds how many candidates reach the LLM.
    D  LLM rerank        one call, all requested garments at once, so the model
                         can avoid handing the same row to two garments.
    E  response          per-garment matches plus a flat, priority-ordered
                         `top_matches` shaped exactly like
                         moodboard.get_matching_garments() so the existing
                         moodboard_v2.sync_garments() writer accepts it as-is.

Season is treated as a first-class ranking signal rather than a filter: SS pulls
toward low-GSM single jersey / pointelle / pique and sleeveless-to-short-sleeve
silhouettes, AW toward terry, fleece and heavy rib. It is scored, not gated,
because the inventory is small enough that gating on GSM would routinely return
nothing for a category.

Endpoint:
    POST /api/method/prism.api.similar_garments.get_similar_garments
    Header: X-Auth-Token: <jwt>            (required — see auth_required)
'''

import difflib
import re

import frappe
from frappe.query_builder.functions import Count

from prism.auth.authenticator import auth_required
import prism.api.llm as llm
# The style-category vocabulary ("Tops" -> tee/polo/tank/...) is a 90-line
# synonym table that already exists next door and is maintained against the same
# merchandiser-typed free text. Reused rather than forked so the two matchers
# cannot drift apart.
import prism.api.garment_matching as gm
import prism.lib.cloud as cloud

DOCTYPE = 'Sample Request'
GARMENT_ELEMENT = 'Main Body'

# Ranking, not generation — same fast model and small cap as garment_matching.
# Overridable per-site via `similar_garments_model` in site_config.json.
_RANK_MODEL = 'claude-haiku-4-5-20251001'
_RANK_MAX_TOKENS = 8000

_DEFAULT_LIMIT = 24
_MAX_LIMIT = 60
_DEFAULT_PER_GARMENT = 4
_MAX_PER_GARMENT = 10

# Cost/latency caps on the LLM prompt, not relevance filters. The per-garment
# depth is well above the handful actually returned because several garments in
# one brief usually want the same slice of the archive — their shortlists
# collapse hard on dedupe, so a shallow depth starves the LLM of alternatives.
_SHORTLIST_PER_GARMENT = 20
_MAX_LLM_CANDIDATES = 90

# Below this many in-category candidates a requested garment is allowed to look
# outside its own product_category (a "Dresses" ask falls back to the wider
# Tops pool rather than returning one weak row).
_MIN_CATEGORY_CANDIDATES = 3

_FETCH_FIELDS = [
    'name', 'gsr_no', 'ai_description', 'gender', 'product_group',
    'product_category', 'clean_construction', 'clean_blend', 'element_colour',
    'sample_colour', 'finished_gsm', 'image_urls', 'image_urls_3d',
    'image_urls_clean', 'creation',
]

_PRIORITY_WEIGHTS = {'high': 1.0, 'medium': 0.85, 'low': 0.7}


# =====================================================================
# Season profiles
# =====================================================================

# GSM bands are calibrated against the live Sample Request spread (p10=155,
# median=200, p90=280), not against textbook values — a "heavy" garment in this
# inventory is 300gsm terry, not a 600gsm melton.
_SEASON_PROFILES = {
    'SS': {
        'ideal_gsm': (110, 210),
        'tolerated_gsm': (90, 250),
        'good_construction': (
            'single jersey', 'pique', 'pointelle', 'mesh', 'popcorn',
            'raschel knit', 'woven', 'ber knit',
        ),
        'bad_construction': ('fleece', 'terry', 'ottoman'),
        'good_fibre': (
            'linen', 'cotton', 'viscose', 'modal', 'lyocell', 'tencel',
            'excel', 'liva', 'supima', 'rayon', 'silk', 'bamboo',
        ),
        'bad_fibre': ('wool', 'acrylic'),
        'good_words': (
            'crop', 'cropped', 'tank', 'sleeveless', 'short sleeve',
            'half sleeve', 'cami', 'camisole', 'strap', 'halter', 'tube',
            'mesh', 'eyelet', 'pointelle', 'lightweight', 'airy', 'vest',
            'tee', 'shorts', 'sundress', 'slip',
        ),
        'bad_words': (
            'hoodie', 'hoody', 'hooded', 'sweatshirt', 'fleece', 'quilted',
            'padded', 'puffer', 'thermal', 'full sleeve', 'long sleeve',
            'turtleneck', 'jacket', 'sherpa', 'layering', 'winter',
        ),
    },
    'AW': {
        'ideal_gsm': (250, 420),
        'tolerated_gsm': (210, 480),
        'good_construction': (
            'fleece', 'terry', 'ottoman', 'interlock', 'rib', 'waffle',
            'ber knit', 'jaquard',
        ),
        'bad_construction': ('mesh', 'pointelle'),
        'good_fibre': ('wool', 'acrylic', 'polyester', 'cotton', 'nylon'),
        'bad_fibre': ('linen',),
        'good_words': (
            'hoodie', 'hoody', 'hooded', 'sweatshirt', 'fleece', 'quilted',
            'padded', 'thermal', 'full sleeve', 'long sleeve', 'turtleneck',
            'high neck', 'jacket', 'sherpa', 'layering', 'brushed',
            'quarter zip', 'crew neck', 'knit', 'cardigan', 'sweater',
        ),
        'bad_words': (
            'sleeveless', 'tank', 'cami', 'camisole', 'strapless', 'halter',
            'tube', 'mesh', 'crop', 'cropped', 'shorts', 'lightweight',
        ),
    },
}

# Scoring weights, all four capped to comparable ranges so no single signal can
# run away with the shortlist: category 0..4, season -7.5..+7.5, silhouette
# 0..8, colour 0..3. Season is deliberately the loudest *negative* — an SS brief
# should actively push a 330gsm fleece down — but it must not be able to
# outvote what the garment actually is, or an AW brief hands a "crop top" ask a
# shortlist of hoodies purely on fabric weight.
_W_CATEGORY = 4.0
_W_SEASON = 1.5
_SEASON_CLAMP = 5.0
_W_SILHOUETTE = 2.0
# Weakest signal by construction: Sample Request stores a colour *name* with no
# hex, so the brief's palette can only be met at colour-family level.
_W_COLOUR = 1.0


# =====================================================================
# Entry point
# =====================================================================

@frappe.whitelist(allow_guest=True, methods=['POST'])
@auth_required
def get_similar_garments(
    payload=None,
    styleCategories=None,
    genders=None,
    season=None,
    garmentSignals=None,
    colours=None,
    userVision=None,
    limit=None,
    perGarment=None,
    withImgOnly=True,
):
    '''
    Rank the closest-matching in-house garments (Sample Request) for a moodboard
    brief that has already been resolved into concrete garment asks.

    Accepts the brief either as a single `payload` object or as top-level
    arguments — the keys are identical either way:

        styleCategories list[str]   HARD filter, e.g. ["Tops", "Dresses"]
        genders         list[str]   HARD filter, e.g. ["Women"]
        season          str         "SS27" / "AW26" — drives the GSM/fabric bias
        garmentSignals  dict        {description, garments: [{category, name,
                                    priority, reason, silhouette}]}
        colours         list[dict]  [{name, hex, pantone, selected}] palette
        userVision      str         optional free text, extra LLM context
        limit           int         flat shortlist size (default 24, max 60)
        perGarment      int         matches per requested garment (default 4, max 10)
        withImgOnly     bool        default True — only rows with a usable image

    Returns:
        {
            'success': True,
            'match_count': int,
            'top_matches': [<row>, ...],   # flat, priority-ordered, deduped
            'data': {
                'description': str,
                'garments': [{name, category, priority, silhouette, reason,
                              matches: [<row>, ...]}],
            },
            'meta': {...},                 # what resolved, what did not
        }

    Each <row> carries the same keys moodboard.get_matching_garments() returns
    (id / gsr_no / garment_name / gender / product_category / fabric_quality /
    fabric_blend / element_colour / finished_gsm / image_urls / relevancy_score)
    so it can be handed straight to moodboard_v2.sync_garments(), plus
    `matched_garment`, `match_rationale` and `season_fit` for display.

    Never raises: any failure comes back as {'success': False, 'error': ...}.
    '''
    try:
        payload = _as_dict(payload)

        def pick(explicit, key):
            return explicit if explicit is not None else payload.get(key)

        style_categories = [s for s in _as_list(pick(styleCategories, 'styleCategories')) if s]
        gender_input = [g for g in _as_list(pick(genders, 'genders')) if g]
        season = (pick(season, 'season') or '').strip()
        signals = _as_dict(pick(garmentSignals, 'garmentSignals'))
        palette = _as_list(pick(colours, 'colours'))
        user_vision = (pick(userVision, 'userVision') or '').strip()
        limit = _clamp_int(pick(limit, 'limit'), _DEFAULT_LIMIT, _MAX_LIMIT)
        per_garment = _clamp_int(pick(perGarment, 'perGarment'), _DEFAULT_PER_GARMENT, _MAX_PER_GARMENT)
        with_img_only = _as_bool(pick(withImgOnly, 'withImgOnly'), default=True)

        # --- A: hard filters ---
        resolved_genders, unresolved_genders = _resolve_genders(gender_input)
        vocab = _category_vocabulary()
        resolved_categories, unresolved_categories = _resolve_categories(style_categories, vocab)

        season_bucket = _season_bucket(season)
        requested = _requested_garments(signals, style_categories)

        # Gender and style category are hard filters, so they must not fail
        # open: a brief that asked for something the inventory has no
        # vocabulary for ("Spacesuits") would otherwise drop the filter and
        # quietly rank the entire archive instead of saying it found nothing.
        unapplied = []
        if gender_input and not resolved_genders:
            unapplied.append('genders')
        if style_categories and not resolved_categories:
            unapplied.append('styleCategories')
        if unapplied:
            return _empty_response(
                requested, signals.get('description'),
                {
                    'season': season or None,
                    'seasonBucket': season_bucket,
                    'resolvedGenders': sorted(resolved_genders),
                    'unresolvedGenders': unresolved_genders,
                    'resolvedCategories': sorted(resolved_categories),
                    'unresolvedStyleCategories': unresolved_categories,
                    'candidatePoolSize': 0,
                    'llmCandidatesConsidered': 0,
                    'requestedGarments': len(requested),
                    'garmentsWithoutMatches': [g['name'] for g in requested],
                    'withImgOnly': with_img_only,
                    'rankMode': 'not_run',
                    'unappliedHardFilters': unapplied,
                },
            )

        # --- B: candidate pool ---
        candidates = _fetch_candidates(resolved_genders, resolved_categories, with_img_only)
        if not candidates and with_img_only:
            # The inventory only has images on ~23% of rows; an image-only pool
            # can legitimately come back empty for a narrow brief. Widen once
            # rather than returning nothing, and say so in meta.
            candidates = _fetch_candidates(resolved_genders, resolved_categories, False)
            with_img_only = False if candidates else with_img_only

        palette_colours = _palette_colours(palette)

        # --- C: heuristic score + per-garment shortlist ---
        for c in candidates:
            c['_text'] = _candidate_text(c)
            c['_season'] = _season_score(c, season_bucket)
            c['_colour'] = _colour_score(c, palette_colours)

        for g in requested:
            g['_categories'] = _resolve_categories([g['category']], vocab)[0] if g.get('category') else set()
            g['_shortlist'] = _shortlist_for(g, candidates)

        pool_for_llm = _llm_pool(requested)

        # --- D: LLM rerank ---
        ranked = _llm_rank(
            requested, pool_for_llm, per_garment,
            season=season, season_bucket=season_bucket, genders=gender_input,
            style_categories=style_categories, palette=palette_colours,
            description=signals.get('description'), user_vision=user_vision,
        )
        # A failed call degrades to the heuristic order for every garment. A
        # *successful* call that returned nothing for one garment does NOT —
        # that is the model saying the archive has no fit, and padding it with
        # the heuristic's next-best rows is how an AW brief for a pointelle crop
        # top ends up showing four fleece hoodies.
        rank_mode = 'llm'
        if ranked is None:
            rank_mode = 'heuristic_fallback'
            ranked = {
                g['name']: [
                    {'candidate': e['candidate'], 'score': _scale_score(e['score']), 'why': None}
                    for e in g['_shortlist'][:per_garment]
                ]
                for g in requested
            }

        # --- E: response ---
        out_garments = []
        for g in requested:
            picks = ranked.get(g['name']) or []
            out_garments.append({
                'name': g['name'],
                'category': g.get('category'),
                'priority': g.get('priority'),
                'silhouette': g.get('silhouette'),
                'reason': g.get('reason'),
                'matches': [
                    _public_row(p['candidate'], g, p.get('score'), p.get('why'), season_bucket)
                    for p in picks[:per_garment]
                ],
            })

        top_matches = _flatten(out_garments, requested, limit)

        return {
            'success': True,
            'match_count': len(top_matches),
            'top_matches': top_matches,
            'data': {
                'description': signals.get('description'),
                'garments': out_garments,
            },
            'meta': {
                'season': season or None,
                'seasonBucket': season_bucket,
                'resolvedGenders': sorted(resolved_genders),
                'unresolvedGenders': unresolved_genders,
                'resolvedCategories': sorted(resolved_categories),
                'unresolvedStyleCategories': unresolved_categories,
                'candidatePoolSize': len(candidates),
                'llmCandidatesConsidered': len(pool_for_llm),
                'requestedGarments': len(requested),
                # Named rather than just counted: an empty list here is a real
                # merchandising answer ("we have never made this for AW"), not
                # a fault, and the UI should be able to say which ask it was.
                'garmentsWithoutMatches': [
                    g['name'] for g in out_garments if not g['matches']
                ],
                'withImgOnly': with_img_only,
                'rankMode': rank_mode,
                'unappliedHardFilters': [],
            },
        }

    except Exception as ex:
        frappe.log_error(frappe.get_traceback(), 'similar_garments.get_similar_garments()')
        return {'success': False, 'error': str(ex)}


# =====================================================================
# Stage A — hard filters resolved against the live vocabulary
# =====================================================================

_GENDER_ALIASES = {
    'women': ('Women',), 'woman': ('Women',), 'womens': ('Women',),
    'ladies': ('Women',), 'female': ('Women',), 'ladieswear': ('Women',),
    'men': ('Men',), 'man': ('Men',), 'mens': ('Men',), 'male': ('Men',),
    'menswear': ('Men',),
    'girls': ('Girls',), 'girl': ('Girls',),
    'boys': ('Boys',), 'boy': ('Boys',),
    # The brief's taxonomy has a "Kids" bucket; the inventory splits it.
    'kids': ('Girls', 'Boys'), 'kid': ('Girls', 'Boys'),
    'children': ('Girls', 'Boys'), 'child': ('Girls', 'Boys'),
    'childrenswear': ('Girls', 'Boys'),
    'unisex': ('Women', 'Men'),
}


def _resolve_genders(genders):
    ''' Brief genders -> Sample Request `gender` values. Unmatched ones are
    reported rather than silently dropped, since gender is a hard filter and an
    unresolved value would otherwise widen the search without anyone noticing. '''
    available = {
        (v or '').strip(): (v or '').strip()
        for v in frappe.get_all(
            DOCTYPE, filters={'garment_element': GARMENT_ELEMENT, 'gender': ['is', 'set']},
            pluck='gender', distinct=True, ignore_permissions=True, limit_page_length=0,
        ) if v
    }
    by_norm = {_norm(k): v for k, v in available.items()}

    resolved, unresolved = set(), []
    for raw in genders:
        key = _norm(raw)
        hit = ()
        if key in by_norm:
            hit = (by_norm[key],)
        elif key in _GENDER_ALIASES:
            hit = tuple(v for v in _GENDER_ALIASES[key] if v in available)
        if hit:
            resolved.update(hit)
        else:
            unresolved.append(raw)

    return resolved, unresolved


def _resolve_categories(style_categories, vocab=None):
    '''
    Brief style categories -> Sample Request `product_category` values.

    Read off the table rather than hard-coded, so a category merchandising
    starts using is picked up without a code change. Three passes, most
    authoritative first:
      1. the value IS a product_group ("Tops") -> every category in that group,
         which is the taxonomy's own answer to what a Tops brief covers;
      2. the value IS a product_category ("Dresses");
      3. the shared synonym vocabulary ("Tees" -> T-Shirt, "Co-ords" -> Sets).

    `vocab` is the (groups, cats) pair from _category_vocabulary(); pass it in
    to reuse one lookup across the brief's own categories and each requested
    garment's, instead of re-querying per garment.
    '''
    groups, cats = vocab if vocab else _category_vocabulary()

    resolved, unresolved = set(), []
    for raw in style_categories:
        keys = {_norm(raw), gm._norm_style_key(raw)}
        keys.discard('')
        hit = set()
        for key in keys:
            if key in groups:
                hit |= groups[key]
            if key in cats:
                hit.add(cats[key])
        if not hit:
            matcher = gm._style_matcher([raw])
            if matcher:
                hit = {c for k, c in cats.items() if matcher.search(k)}
        if hit:
            resolved |= hit
        else:
            unresolved.append(raw)

    return resolved, unresolved


def _category_vocabulary():
    '''
    -> ({normalized group: {categories}}, {normalized category: category}).

    A category is assigned to the group it appears under most often, not to
    every group it has ever appeared under. The data has a handful of
    mis-grouped rows — one Pants and one Sets row filed under "Tops" against
    150+ and 149 correctly grouped ones — and without this a Tops brief would
    hard-filter its way to a pair of trousers.
    '''
    # frappe.get_all() rejects aggregate strings in `fields`, so this one goes
    # through the query builder.
    SampleRequest = frappe.qb.DocType(DOCTYPE)
    rows = (
        frappe.qb.from_(SampleRequest)
        .select(
            SampleRequest.product_group,
            SampleRequest.product_category,
            Count(SampleRequest.name).as_('n'),
        )
        .where(SampleRequest.garment_element == GARMENT_ELEMENT)
        .where(SampleRequest.product_category.notnull())
        .where(SampleRequest.product_category != '')
        .groupby(SampleRequest.product_group, SampleRequest.product_category)
    ).run(as_dict=True)

    best_group, cats = {}, {}
    for r in rows:
        group = (r.get('product_group') or '').strip()
        cat = (r.get('product_category') or '').strip()
        if not cat:
            continue
        # Indexed under both the literal and the singularised key so "Dresses"
        # and "Dress" both land on the same row.
        cats[_norm(cat)] = cat
        cats[gm._norm_style_key(cat)] = cat
        if group and r.get('n', 0) > best_group.get(cat, (None, 0))[1]:
            best_group[cat] = (group, r['n'])

    groups = {}
    for cat, (group, _n) in best_group.items():
        for key in {_norm(group), gm._norm_style_key(group)}:
            groups.setdefault(key, set()).add(cat)

    cats.pop('', None)
    groups.pop('', None)
    return groups, cats


# =====================================================================
# Stage B — candidate pool
# =====================================================================

def _fetch_candidates(genders, categories, with_img_only):
    '''
    The hard-filtered pool, deduped to the latest row per gsr_no — a style is
    re-sampled many times and every revision lands as its own Sample Request
    row, so without this the shortlist fills up with one garment.
    '''
    filters = {'garment_element': GARMENT_ELEMENT}
    if genders:
        filters['gender'] = ['in', sorted(genders)]
    if categories:
        filters['product_category'] = ['in', sorted(categories)]

    or_filters = None
    if with_img_only:
        or_filters = {'image_urls': ['is', 'set'], 'image_urls_3d': ['is', 'set']}

    rows = frappe.get_all(
        DOCTYPE, filters=filters, or_filters=or_filters, fields=_FETCH_FIELDS,
        order_by='creation desc', limit_page_length=0, ignore_permissions=True,
    )

    latest = {}
    for r in rows:
        key = r.get('gsr_no') or r.get('name')
        if key not in latest:          # already ordered creation desc
            latest[key] = r
    return list(latest.values())


# =====================================================================
# Stage C — heuristic scoring
# =====================================================================

def _requested_garments(signals, style_categories):
    '''
    The brief's garment asks, normalised. When `garmentSignals` is absent the
    brief still has to return something, so each style category becomes one
    implicit ask — season + category matching alone, no silhouette signal.
    '''
    out = []
    for raw in _as_list(signals.get('garments')):
        g = _as_dict(raw)
        name = (g.get('name') or '').strip()
        if not name:
            continue
        out.append({
            'name': name,
            'category': (g.get('category') or '').strip(),
            'priority': (g.get('priority') or 'medium').strip().lower(),
            'reason': (g.get('reason') or '').strip(),
            'silhouette': (g.get('silhouette') or '').strip(),
        })

    if not out:
        for cat in style_categories:
            out.append({
                'name': cat, 'category': cat, 'priority': 'medium',
                'reason': '', 'silhouette': '',
            })

    return out


def _shortlist_for(garment, candidates):
    '''
    Best candidates for one requested garment, own-category first, as
    [{'candidate', 'score'}] best-first.

    Falls back to the wider pool only when the garment's own category is too
    thin to fill a shortlist, so a "Dresses" ask is never padded with tees while
    real dresses exist.

    The score is returned alongside the row rather than written onto it: every
    garment re-scores the same shared candidate dicts against its own
    silhouette, so a score stored on the row would be overwritten by whichever
    garment happened to be scored last.
    '''
    in_cat = [c for c in candidates if c.get('product_category') in garment['_categories']] \
        if garment['_categories'] else []
    pool = candidates if len(in_cat) < _MIN_CATEGORY_CANDIDATES else in_cat

    silhouette_terms = _garment_terms(garment)
    scored = []
    for c in pool:
        cat_hit = 1.0 if c.get('product_category') in garment['_categories'] else 0.0
        scored.append({
            'candidate': c,
            'score': (
                _W_CATEGORY * cat_hit
                + _W_SEASON * c['_season']
                + _W_SILHOUETTE * _term_overlap(silhouette_terms, c['_text'])
                + _W_COLOUR * c['_colour']
            ),
        })

    scored.sort(key=lambda e: e['score'], reverse=True)
    return scored[:_SHORTLIST_PER_GARMENT]


def _llm_pool(requested):
    '''
    Union of every garment's shortlist, capped, best-first. Interleaved by depth
    so that when the cap bites it trims each garment's tail evenly instead of
    starving the last garment in the brief. Deduped, so one strong row costs one
    slot in the prompt no matter how many garments want it.
    '''
    seen, pool = set(), []
    for depth in range(_SHORTLIST_PER_GARMENT):
        for g in requested:
            shortlist = g.get('_shortlist') or []
            if depth >= len(shortlist):
                continue
            c = shortlist[depth]['candidate']
            key = c['name']
            if key in seen:
                continue
            seen.add(key)
            pool.append(c)
            if len(pool) >= _MAX_LLM_CANDIDATES:
                return pool
    return pool


def _season_bucket(season):
    s = (season or '').upper()
    if 'SS' in s or 'SPRING' in s or 'SUMMER' in s:
        return 'SS'
    if 'AW' in s or 'FW' in s or 'AUTUMN' in s or 'WINTER' in s or 'FALL' in s:
        return 'AW'
    return None


def _season_score(candidate, bucket):
    '''
    How well one inventory row suits the brief's season, roughly -8..+8.

    Four independent pieces of evidence, because no single one is reliable:
    fabric weight (the strongest — a 330gsm terry is a winter garment whatever
    it is called), construction, fibre content, and silhouette words in the
    AI description.
    '''
    profile = _SEASON_PROFILES.get(bucket)
    if not profile:
        return 0.0

    score = 0.0

    gsm = _to_float(candidate.get('finished_gsm'))
    if gsm:
        lo, hi = profile['ideal_gsm']
        tlo, thi = profile['tolerated_gsm']
        if lo <= gsm <= hi:
            score += 3
        elif tlo <= gsm <= thi:
            score += 1
        else:
            outside = (tlo - gsm) if gsm < tlo else (gsm - thi)
            score -= 3 if outside > 60 else 2

    construction = _norm(candidate.get('clean_construction'))
    if construction:
        if any(k in construction for k in profile['good_construction']):
            score += 2
        elif any(k in construction for k in profile['bad_construction']):
            score -= 2

    blend = _norm(candidate.get('clean_blend'))
    if blend:
        if any(k in blend for k in profile['good_fibre']):
            score += 1
        if any(k in blend for k in profile['bad_fibre']):
            score -= 1

    text = candidate.get('_text') or _candidate_text(candidate)
    good = sum(1 for k in profile['good_words'] if k in text)
    bad = sum(1 for k in profile['bad_words'] if k in text)
    score += min(good, 2) - min(bad, 2)

    return max(-_SEASON_CLAMP, min(_SEASON_CLAMP, score))


def _candidate_text(c):
    parts = [
        c.get('ai_description'), c.get('product_category'), c.get('product_group'),
        c.get('clean_construction'), c.get('clean_blend'), c.get('element_colour'),
        c.get('sample_colour'),
    ]
    return _norm(' '.join(str(p) for p in parts if p))


# Words that appear in almost every silhouette description and would match
# everything, plus the gender words the inventory repeats in every ai_description.
_TERM_STOPWORDS = frozenset((
    'the', 'and', 'with', 'for', 'from', 'that', 'this', 'its', 'above', 'below',
    'hits', 'sits', 'length', 'style', 'fit', 'women', 'womens', 'men', 'mens',
    'girls', 'boys', 'kids', 'new', 'all', 'over', 'front', 'back', 'side',
    'short', 'long', 'neck', 'sleeve', 'sleeves', 'body', 'hem', 'panel',
    'panels', 'inch', 'knee', 'navel',
))


def _garment_terms(garment):
    ''' Distinctive words from a requested garment's name + silhouette. '''
    text = _norm(f"{garment.get('name', '')} {garment.get('silhouette', '')}")
    terms = set()
    for word in text.split():
        if len(word) > 2 and word not in _TERM_STOPWORDS:
            terms.add(word)
    # Two-word phrases survive the stopword cut, because "square neck" and
    # "short sleeve" are real signals even though both halves are noise alone.
    words = text.split()
    for a, b in zip(words, words[1:]):
        if len(a) > 2 and len(b) > 2:
            terms.add(f'{a} {b}')
    return terms


def _term_overlap(terms, text):
    if not terms or not text:
        return 0.0
    hits = sum(1 for t in terms if t in text)
    return float(min(hits, 4))


# --- colour ---

def _palette_colours(colours):
    ''' The brief's selected swatches as [{name, hex, pantone, family}]. '''
    out = []
    for raw in colours:
        c = _as_dict(raw)
        if 'selected' in c and not c.get('selected'):
            continue
        name = (c.get('name') or '').strip()
        hexv = (c.get('hex') or '').strip()
        out.append({
            'name': name,
            'hex': hexv or None,
            'pantone': (c.get('pantone') or '').strip() or None,
            'family': _hex_family(hexv) or _name_family(name),
        })
    return out


def _colour_score(candidate, palette):
    '''
    Sample Request stores a colour *name* ("Tapestry Navy") with no hex, and the
    brief carries hex + a marketing name, so the two sides can only meet at the
    colour-family level — with an exact name hit as a bonus when the mill and
    the brief happen to use the same word.
    '''
    if not palette:
        return 0.0

    name = _norm(candidate.get('element_colour') or candidate.get('sample_colour'))
    if not name:
        return 0.0

    palette_names = {_norm(p['name']) for p in palette if p['name']}
    if name in palette_names:
        return 3.0

    family = _name_family(name)
    if family and family in {p['family'] for p in palette if p['family']}:
        return 2.0

    # Shared distinctive word ("Cherry Red Accent" vs "Cherry Red").
    tokens = set(name.split())
    for p in palette_names:
        if tokens & {w for w in p.split() if len(w) > 3}:
            return 1.0

    return 0.0


_COLOUR_FAMILY_WORDS = {
    'black': ('black', 'jet', 'onyx', 'ink', 'coal', 'anthracite'),
    'white': ('white', 'egret', 'ecru', 'ivory', 'cream', 'snow', 'chalk', 'rfd', 'optic'),
    'grey': ('grey', 'gray', 'melange', 'charcoal', 'silver', 'slate', 'ash', 'cloud'),
    'beige': ('beige', 'sand', 'khaki', 'stone', 'oat', 'nude', 'taupe', 'camel', 'biscuit'),
    'brown': ('brown', 'chocolate', 'coffee', 'mocha', 'tan', 'walnut', 'cocoa', 'rust'),
    'red': ('red', 'crimson', 'scarlet', 'cherry', 'ruby', 'wine', 'burgundy', 'maroon', 'tuscan'),
    'pink': ('pink', 'rose', 'blush', 'fuchsia', 'magenta', 'coral', 'peach', 'quail'),
    'orange': ('orange', 'apricot', 'amber', 'tangerine', 'terracotta'),
    'yellow': ('yellow', 'lemon', 'mustard', 'gold', 'honey', 'butter', 'ochre'),
    'green': ('green', 'olive', 'sage', 'mint', 'emerald', 'moss', 'forest', 'posy', 'lime'),
    'blue': ('blue', 'sky', 'denim', 'aqua', 'teal', 'turquoise', 'cobalt', 'azure'),
    'navy': ('navy', 'indigo', 'midnight', 'marine'),
    'purple': ('purple', 'violet', 'lilac', 'lavender', 'plum', 'mauve', 'aubergine'),
}


def _name_family(name):
    n = _norm(name)
    if not n:
        return None
    words = set(n.split())
    # Longest-word-wins is wrong here; navy must beat blue on "navy blue".
    for family in ('navy', 'black', 'white', 'grey', 'beige', 'brown', 'red',
                   'pink', 'orange', 'yellow', 'green', 'blue', 'purple'):
        for keyword in _COLOUR_FAMILY_WORDS[family]:
            if keyword in words or keyword in n:
                return family
    return None


def _hex_family(hexv):
    ''' #RRGGBB -> a colour family, via coarse HSV buckets. '''
    m = re.fullmatch(r'#?([0-9a-fA-F]{6})', (hexv or '').strip())
    if not m:
        return None
    raw = m.group(1)
    r, g, b = (int(raw[i:i + 2], 16) / 255 for i in (0, 2, 4))

    mx, mn = max(r, g, b), min(r, g, b)
    v, delta = mx, mx - mn
    s = 0 if mx == 0 else delta / mx

    if v < 0.18:
        return 'black'
    if s < 0.12:
        return 'white' if v > 0.85 else 'grey'

    if delta == 0:
        hue = 0.0
    elif mx == r:
        hue = (60 * ((g - b) / delta)) % 360
    elif mx == g:
        hue = 60 * ((b - r) / delta) + 120
    else:
        hue = 60 * ((r - g) / delta) + 240

    if hue < 15 or hue >= 345:
        return 'pink' if v > 0.75 and s < 0.45 else 'red'
    if hue < 40:
        return 'brown' if v < 0.6 else ('beige' if s < 0.4 else 'orange')
    if hue < 70:
        return 'beige' if s < 0.35 else 'yellow'
    if hue < 170:
        return 'green'
    if hue < 200:
        return 'blue'
    if hue < 255:
        return 'navy' if v < 0.5 else 'blue'
    if hue < 290:
        return 'purple'
    return 'pink'


# =====================================================================
# Stage D — LLM rerank
# =====================================================================

def _llm_rank(requested, pool, per_garment, season, season_bucket, genders,
              style_categories, palette, description, user_vision):
    ''' -> {requested garment name: [{candidate, score, why}]}, or None if the
    LLM call failed outright (caller degrades to the heuristic order). '''
    if not pool or not requested:
        return {}

    # Surrogate ids: the docnames are long hashes, and a short opaque id both
    # cuts prompt tokens and makes a hallucinated id trivially detectable.
    by_id = {f'c{i}': c for i, c in enumerate(pool, start=1)}

    model = frappe.get_site_config().get('similar_garments_model') or _RANK_MODEL
    system_prompt = _rank_system_prompt(per_garment)
    user_prompt = _rank_user_prompt(
        requested, by_id, per_garment, season, season_bucket, genders,
        style_categories, palette, description, user_vision,
    )

    try:
        raw = llm.get_claude_response(
            system_prompt, user_prompt, ret_type='list',
            model=model, max_tokens=_RANK_MAX_TOKENS,
        )
    except Exception:
        frappe.log_error(frappe.get_traceback(), 'similar_garments._llm_rank/llm')
        return None

    out = {}
    for block in raw or []:
        block = _as_dict(block)
        garment = _match_requested(block.get('garment'), requested)
        if not garment:
            continue
        # This garment's own heuristic scores, to stand in when the model omits
        # or mangles the score field.
        heuristic = {
            e['candidate']['name']: e['score'] for e in (garment.get('_shortlist') or [])
        }

        picks, seen = [], set()
        for item in _as_list(block.get('matches')):
            item = _as_dict(item)
            cid = (item.get('id') or '').strip()
            if cid not in by_id or cid in seen:
                continue
            seen.add(cid)
            candidate = by_id[cid]
            score = _clamp_score(item.get('score'))
            if score is None:
                score = _scale_score(heuristic.get(candidate['name']))
            picks.append({
                'candidate': candidate,
                'score': score,
                'why': (item.get('why') or '').strip() or None,
            })
            if len(picks) >= per_garment:
                break
        if picks:
            out[garment['name']] = picks

    return out


def _match_requested(echoed, requested):
    '''
    Map the garment name the model echoed back onto the one we asked for.
    Tolerant of punctuation and near-misses ("Cherry Print Mini Dress" for
    "Cherry-Print Mini Dress"), because an exact-string miss here silently drops
    every match found for that garment — and now that nothing pads an empty
    garment, that would read as "the archive has nothing" when it does.
    '''
    key = _norm(echoed)
    if not key:
        return None

    by_norm = {_norm(g['name']): g for g in requested}
    if key in by_norm:
        return by_norm[key]

    hit = difflib.get_close_matches(key, list(by_norm), n=1, cutoff=0.8)
    return by_norm[hit[0]] if hit else None


def _rank_system_prompt(per_garment):
    return f'''You are an expert apparel merchandiser choosing which garments from our own \
production archive best match a new season's design brief.

You will be given a design brief, the list of garments the brief asks for (each with a name, \
category, silhouette description and priority), and a numbered list of candidate garments from \
our archive (id, category, fabric construction, blend, finished GSM, colour and the archive's \
own description). For EACH requested garment, pick up to {per_garment} archive candidates that \
best match it, ranked best-first.

Season is a primary criterion, not a tiebreaker:
- SS (Spring/Summer): favour low finished GSM (roughly 110-210), single jersey, pointelle, \
pique and mesh constructions, cotton/linen/viscose blends, and cropped, sleeveless or \
short-sleeve silhouettes. A 330gsm terry hoodie is wrong for SS even if the category matches.
- AW (Autumn/Winter): favour higher finished GSM (roughly 250+), terry, fleece, interlock, \
heavy rib and waffle constructions, and full-sleeve, layered or brushed silhouettes. A 150gsm \
sleeveless jersey tank is wrong for AW even if the category matches.

Priority order when trading off: (1) garment category/silhouette match, (2) seasonal fabric \
weight and construction fit, (3) colour alignment with the brief's palette, (4) overall \
commercial coherence with the brief.

Rules:
- Only use candidate ids from the list given. Never invent an id.
- Prefer not to give the same candidate to two different requested garments; do so only when \
there is genuinely no other reasonable match.
- Return fewer than {per_garment} matches — or an empty list — when the archive has nothing \
that truly fits. Do not pad.
- score is an integer 0-100 for how well that candidate fits that requested garment.
- why is one short sentence naming the concrete reason (weight, construction, silhouette, colour).
- Output ONLY a JSON array, one object per requested garment:
[{{"garment": "<exact requested garment name>", "matches": [{{"id": "c12", "score": 88, \
"why": "..."}}]}}]
No prose outside the JSON.'''


def _rank_user_prompt(requested, by_id, per_garment, season, season_bucket, genders,
                      style_categories, palette, description, user_vision):
    lines = [
        'DESIGN BRIEF',
        f'Season: {season or "(not specified)"}'
        + (f'  [{season_bucket}: {"lightweight spring/summer" if season_bucket == "SS" else "heavier autumn/winter"}]'
           if season_bucket else ''),
        f'Genders: {", ".join(genders) or "(not specified)"}',
        f'Style categories: {", ".join(style_categories) or "(not specified)"}',
    ]
    if palette:
        swatches = ', '.join(
            p['name'] + (' (' + p['hex'] + ')' if p['hex'] else '')
            for p in palette if p['name']
        )
        if swatches:
            lines.append(f'Colour palette: {swatches}')
    if description:
        lines.append(f'Garment direction: {description}')
    if user_vision:
        lines.append(f'Vision: {user_vision}')

    lines += ['', f'REQUESTED GARMENTS (match each one, up to {per_garment} candidates)']
    for g in requested:
        bits = [f'name={g["name"]}']
        for key in ('category', 'priority', 'silhouette', 'reason'):
            if g.get(key):
                bits.append(f'{key}={g[key]}')
        lines.append('- ' + ' | '.join(bits))

    lines += ['', 'ARCHIVE CANDIDATES']
    for cid, c in by_id.items():
        fields = {
            'id': cid,
            'category': c.get('product_category'),
            'desc': c.get('ai_description'),
            'construction': c.get('clean_construction'),
            'blend': c.get('clean_blend'),
            'gsm': c.get('finished_gsm'),
            'colour': c.get('element_colour') or c.get('sample_colour'),
            'gender': c.get('gender'),
        }
        lines.append('- ' + ' | '.join(f'{k}={v}' for k, v in fields.items() if v))

    return '\n'.join(lines)


# =====================================================================
# Stage E — response shaping
# =====================================================================

def _public_row(c, garment, score, why, season_bucket):
    '''
    Keys mirror moodboard.get_matching_garments()'s top_matches so the existing
    moodboard_v2.sync_garments() writer takes this response unchanged.
    '''
    return {
        'id': c.get('name'),
        'gsr_no': c.get('gsr_no'),
        'garment_name': c.get('ai_description'),
        'gender': c.get('gender'),
        'product_group': c.get('product_group'),
        'product_category': c.get('product_category'),
        'fabric_quality': c.get('clean_construction'),
        'fabric_blend': c.get('clean_blend'),
        'element_colour': c.get('element_colour'),
        'finished_gsm': c.get('finished_gsm'),
        'image_urls': _image_urls(c),
        'relevancy_score': score if score is not None else 0,
        'matched_garment': garment['name'],
        'matched_priority': garment.get('priority'),
        'match_rationale': why,
        'season_fit': _season_fit_label(c, season_bucket),
    }


def _image_urls(c):
    ''' Same precedence as get_matching_garments: 3D render, then cleaned, then raw. '''
    for field in ('image_urls_3d', 'image_urls_clean', 'image_urls'):
        raw = c.get(field)
        if raw:
            urls = cloud.format_garment_image_urls(raw)
            if urls:
                return urls
    return {}


def _season_fit_label(c, bucket):
    ''' 'good' / 'ok' / 'poor' / None — the heuristic verdict, exposed so the UI
    can flag a match the LLM kept for category reasons despite the season. '''
    if not bucket:
        return None
    score = c.get('_season')
    if score is None:
        score = _season_score(c, bucket)
    if score >= 3:
        return 'good'
    if score >= 0:
        return 'ok'
    return 'poor'


def _empty_response(requested, description, meta):
    ''' The full response shape with every garment present but unmatched, so a
    caller never has to special-case "no result" against a different schema. '''
    return {
        'success': True,
        'match_count': 0,
        'top_matches': [],
        'data': {
            'description': description,
            'garments': [
                {
                    'name': g['name'],
                    'category': g.get('category'),
                    'priority': g.get('priority'),
                    'silhouette': g.get('silhouette'),
                    'reason': g.get('reason'),
                    'matches': [],
                }
                for g in requested
            ],
        },
        'meta': meta,
    }


def _flatten(out_garments, requested, limit):
    '''
    One deduped, priority-weighted shortlist across all requested garments — a
    high-priority garment's third match outranks a low-priority garment's first.
    '''
    priority_by_name = {g['name']: g.get('priority') for g in requested}

    rows, seen = [], set()
    for block in out_garments:
        weight = _PRIORITY_WEIGHTS.get(priority_by_name.get(block['name']), 0.85)
        for row in block['matches']:
            key = row.get('gsr_no') or row.get('id')
            if key in seen:
                continue
            seen.add(key)
            rows.append((weight * (row.get('relevancy_score') or 0), row))

    rows.sort(key=lambda pair: pair[0], reverse=True)
    return [row for _, row in rows[:limit]]


# =====================================================================
# helpers
# =====================================================================

def _norm(text):
    return re.sub(r'[^a-z0-9]+', ' ', str(text or '').lower()).strip()


def _to_float(value):
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


def _scale_score(heuristic):
    ''' Heuristic score (roughly -15..+25) -> a 0-100 relevancy, so a fallback
    row is still comparable against an LLM-scored one. '''
    if heuristic is None:
        return 0
    return max(0, min(100, int(round(50 + heuristic * 2.5))))


def _clamp_score(value):
    try:
        return max(0, min(100, int(float(value))))
    except (TypeError, ValueError):
        return None


def _clamp_int(value, default, maximum):
    try:
        n = int(value)
    except (TypeError, ValueError):
        return default
    return max(1, min(n, maximum))


def _as_bool(value, default=False):
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() not in ('0', 'false', 'no', 'none', '')


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
