'''
Closest-Fabric-Master matching.

One matcher, two callers: `costing.get_matching_fabrics` (the API a costing
client already uses) and the "Find Closest Fabric Master" actions on the Surplus
Stock desk list.

The job is to take a fabric described in ONE vocabulary and find the nearest
record written in the OTHER. The two sides never agree on wording:

    Fabric Master   "Single Jersey"   "58.2% Cotton 38.8% Lyocell 3% Spandex"
    Surplus Stock   "SJY_EL"          "58:39:3 FTO:T:EL"

The previous implementation handed all 303 catalogue combinations to Claude as
text and asked it to pick three by index. That made the model responsible for
arithmetic it is bad at -- deciding whether 97/3 is nearer 95/5 than 94/6 is,
and whether `T` and `Lyocell` are the same fibre -- with no score to inspect and
no way to stop a drawcord matching a jersey.

So the comparison is numeric here, and the model only re-ranks a shortlist that
is already composition-verified:

    1. both vocabularies are parsed into the same fibre-group space by
       surplus_recommender's parsers, which already read both dialects
    2. every catalogue combination is scored deterministically
       (construction 40 / composition 40 / GSM 20, plus a stretch penalty)
    3. trims and yardage are gated apart, and combinations that share too little
       fibre are dropped
    4. Claude re-ranks the top few ONLY when the leader is not already clear --
       on the current catalogue that is about one row in eight, and it is the
       eighth that is genuinely arguable (no exact construction in stock, or a
       blend nothing quite carries)

Nothing here writes; `bulk_match_closest_fabric` below is the only write path.
'''

import hashlib
import re

import frappe
from frappe.utils import cint, flt, now_datetime

import prism.api.llm as llm
import prism.api.surplus_recommender as sr

DOCTYPE_FABRIC_MASTER = 'Fabric Master'
DOCTYPE_SURPLUS_STOCK = 'Surplus Stock'

# The three things a match is made of. Availability and colour -- the other two
# dimensions surplus_recommender weighs -- say nothing about which catalogue
# entry a stock roll IS, so the remaining weight is split back over construction
# and composition, the two that decide identity.
W_CONSTRUCTION = 0.40
W_COMPOSITION = 0.40
W_GSM = 0.20

# Reused rather than re-picked, so "how close is close" means one thing across
# the app: linear decay to zero at +/- this many GSM.
GSM_TOLERANCE = sr.GSM_TOLERANCE

# Neither side has an opinion worth punishing.
GSM_UNKNOWN_SCORE = 60.0
CONSTRUCTION_UNKNOWN_SCORE = 40.0

# Below this much shared fibre the two are not the same cloth, whatever the
# construction says -- a 100% cotton jersey is not a match for a 60/40 poly-
# cotton jersey. Candidates under the floor are held back and only offered when
# nothing clears it, so a weak answer is still an answer, just a flagged one.
COMPOSITION_FLOOR = 50.0

# What Claude is allowed to re-rank, and when it is asked at all. See
# _is_runaway(): an exact construction with this much fibre in common settles
# the match on its own, and the score/margin pair is the fallback test for
# everything else.
SHORTLIST_SIZE = 12
COMPOSITION_CERTAIN = 99.0
RUNAWAY_SCORE = 90.0
RUNAWAY_MARGIN = 12.0
RERANK_MAX_TOKENS = 2000

MAX_MATCHES = 3

METHOD_DETERMINISTIC = 'Deterministic'
METHOD_LLM = 'LLM Assisted'
METHOD_MANUAL = 'Manual'

# Yarn-process annotations that landed in Fabric Master's construction column.
# They name how the yarn was spun, not what was knitted, so they cannot be
# matched against and are dropped from the index rather than scored as unknowns.
IGNORED_CONSTRUCTIONS = {'EL', 'COMPACT', 'EL COMPACT', 'CC', 'SIRO', 'CTN'}

# surplus_recommender files every trim under one `trim` family, which is the
# right grain for "is this yardage or a finding" but far too coarse here: a
# drawcord and a collar are both trims, share fibre blends exactly, and are
# never substitutes. The catalogue names all five kinds as separate
# constructions, so they are separated on the way in and matched kind-for-kind.
TRIM_KINDS = (
    ('twill tape', 'tape'), ('ttp', 'tape'), ('tape', 'tape'),
    ('drawcord', 'drawcord'), ('drw cord', 'drawcord'), ('drw', 'drawcord'),
    ('dori', 'dori'),
    ('collar', 'collar'), ('clr', 'collar'),
    ('cuff', 'cuff'), ('cuf', 'cuff'),
)

# A dori IS a drawcord -- same finding, one named in Hindi -- so the catalogue
# keeping them as separate constructions should cost a little, not everything.
# Every other pair of trim kinds is a hard no.
RELATED_TRIM_KINDS = {
    frozenset(('drawcord', 'dori')): 0.85,
}

# The fibre groups compositions are compared in -- whatever the two vocabularies
# between them can name. Also the only answers the LLM may give for a fibre it
# is asked to place.
KNOWN_FIBRE_GROUPS = sorted(
    set(sr.FIBRE_GROUP_BY_CODE.values()) | set(sr.FIBRE_GROUP_BY_WORD.values())
)

# The construction families a match can be made in, and the only answers the LLM
# may give for a construction the keyword table cannot place.
KNOWN_FAMILIES = sorted({family for _keyword, family in sr.FAMILY_KEYWORDS})

# Bumped whenever the scoring changes, so stored signatures stop agreeing and a
# re-run recomputes instead of trusting a match the current rules would not make.
# v2: trim kinds separated; exact ties broken by catalogue prevalence.
# v3: "95:5 Bci Cotton:Elastane" parses as a ratio blend instead of collapsing
#     to a single fibre; unknown fibre names resolved by LLM and cached.
ALGO_VERSION = 'v3'

INDEX_CACHE_KEY = 'prism:fabric_matcher:index'
INDEX_CACHE_TTL = 6 * 60 * 60

# Learned fibre names and constructions outlive the catalogue cache -- a fibre
# does not stop being a fibre when Fabric Master is edited.
FIBRE_CACHE_KEY = 'prism:fabric_matcher:fibre_names'
FAMILY_CACHE_KEY = 'prism:fabric_matcher:construction_families'

BULK_COMMIT_CHUNK = 50

# Rows are matched per row, but rows sharing a (quality, blend, gsm) triple must
# get the same answer, so the work is done once per distinct triple and fanned
# back out. Across the whole table that is ~93 computations for ~580 rows.
_STOCK_FIELDS = (
    'name', 'quality', 'quality_full_name', 'blend', 'blend_full_name', 'gsm',
    'material_desc', 'fab_code', 'closest_fabric_master', 'closest_match_method',
    'closest_match_signature',
)


# --- public: matching -------------------------------------------------------

def match_fabric(fabric: dict, max_matches: int = MAX_MATCHES, use_llm: bool = True):
    '''
    A fabric -> up to `max_matches` Fabric Master records, closest first.

    `fabric` carries whatever is known, in either vocabulary:

        {"construction": "Rib" | "RIB_1X1_EL", "blend": "95% Cotton 5% Spandex"
         | "95:5 BCI:EL", "gsm": 190}

    Each returned record is the Fabric Master row itself plus `score` (0-100),
    `method`, and `reason`. Returns [] when nothing is known to match on. Pure --
    reads the catalogue, writes nothing.
    '''
    target = _normalise_target(fabric)
    if not (target['family'] or target['composition']):
        return []

    index = _fabric_master_index()
    if not index:
        return []

    scored = []
    for combo in index:
        result = _score_combo(target, combo)
        if result:
            scored.append(result)
    if not scored:
        return []

    # The floor is a preference, not a wall: if nothing clears it the best of a
    # bad field still beats returning nothing, but it says so in its reason.
    qualified = [s for s in scored if s['breakdown']['composition'] >= COMPOSITION_FLOOR]
    below_floor = not qualified
    ranked = sorted(qualified or scored, key=_rank_key)

    method = METHOD_DETERMINISTIC
    if use_llm and not _is_runaway(ranked):
        reranked = _llm_rerank(target, ranked[:SHORTLIST_SIZE])
        if reranked:
            ranked = reranked + [r for r in ranked if r not in reranked]
            method = METHOD_LLM

    matches = []
    seen = set()
    for entry in ranked:
        record = _fabric_master_row(entry['combo'], target['gsm'])
        if not record or record['fabric_id'] in seen:
            continue
        seen.add(record['fabric_id'])
        record['score'] = round(entry['score'], 2)
        record['method'] = method
        record['reason'] = entry.get('reason') or _reason(target, entry, below_floor)
        record['breakdown'] = entry['breakdown']
        matches.append(record)
        if len(matches) >= max_matches:
            break

    return matches


def match_stock_row(row: dict, use_llm: bool = True):
    ''' One Surplus Stock row (as a dict of _STOCK_FIELDS) -> match_fabric(). '''
    return match_fabric(_fabric_from_stock_row(row), use_llm=use_llm)


@frappe.whitelist()
def find_closest_fabric_master(name: str, force: int = 0):
    '''
    Match one Surplus Stock record and store the result. Backs the single-record
    button on the Surplus Stock form.
    '''
    if not frappe.has_permission(DOCTYPE_SURPLUS_STOCK, 'write'):
        raise frappe.PermissionError

    row = frappe.db.get_value(
        DOCTYPE_SURPLUS_STOCK, name, list(_STOCK_FIELDS), as_dict=True
    )
    if not row:
        frappe.throw(f'No Surplus Stock record named "{name}".')

    if row.get('closest_match_method') == METHOD_MANUAL and not cint(force):
        return {'matched': 0, 'skipped': 1, 'reason': 'Match was set manually.'}

    matches = match_stock_row(row)
    cost = _cost_per_kg(matches[0]['fabric_id']) if matches else None
    _store_match(row['name'], _signature(row), matches, cost)
    frappe.db.commit()

    return {
        'matched': 1 if matches else 0,
        'skipped': 0,
        'match': matches[0] if matches else None,
    }


# --- public: bulk -----------------------------------------------------------

@frappe.whitelist()
def bulk_match_closest_fabric(names=None, enqueue=0, force=0):
    '''
    Match many Surplus Stock rows and store the results. Backs the
    "Find Closest Fabric Master" list-view actions.

    names   : optional JSON array / list of Surplus Stock ids (a list-view
              selection). When omitted, EVERY row is considered.
    enqueue : truthy -> run in the background and return immediately.
    force   : truthy -> also recompute rows whose signature still agrees and
              rows whose match was set by hand.

    -> {enqueued, total} when backgrounded, else
       {total, matched, unchanged, skipped, failed}.
    '''
    if not frappe.has_permission(DOCTYPE_SURPLUS_STOCK, 'write'):
        raise frappe.PermissionError

    ids = _bulk_stock_ids(names)
    if not ids:
        return {'total': 0, 'matched': 0, 'unchanged': 0, 'skipped': 0, 'failed': 0}

    if cint(enqueue):
        frappe.enqueue(
            'prism.api.fabric_matcher._bulk_match_closest_fabric',
            queue='long', timeout=3600, names=ids, force=cint(force),
        )
        return {'enqueued': True, 'total': len(ids)}

    return _bulk_match_closest_fabric(ids, cint(force))


def _bulk_match_closest_fabric(names, force=0):
    '''
    The bulk run itself.

    Matching is done once per distinct (quality, blend, gsm) signature and the
    result written to every row that shares it -- rows described identically
    cannot have different closest fabrics, and doing otherwise would repeat the
    same scoring (and the same LLM call) a few hundred times.
    '''
    rows = _stock_rows(names)
    tally = {'total': len(rows), 'matched': 0, 'unchanged': 0, 'skipped': 0, 'failed': 0}

    # signature -> rows needing it
    pending = {}
    for row in rows:
        if row.get('closest_match_method') == METHOD_MANUAL and not force:
            tally['skipped'] += 1
            continue
        signature = _signature(row)
        if (not force
                and row.get('closest_match_signature') == signature
                and row.get('closest_fabric_master')):
            tally['unchanged'] += 1
            continue
        pending.setdefault(signature, []).append(row)

    written = 0
    cost_cache = {}
    for signature, group in pending.items():
        try:
            matches = match_stock_row(group[0])
        except Exception:
            tally['failed'] += len(group)
            frappe.log_error(frappe.get_traceback(), 'fabric_matcher.bulk_match')
            continue

        # Costed once per matched fabric, not once per row: the memo is what
        # keeps 580 rows down to the 69 fabrics they actually point at.
        cost = _cost_per_kg(matches[0]['fabric_id'], cost_cache) if matches else None

        for row in group:
            try:
                _store_match(row['name'], signature, matches, cost)
                tally['matched' if matches else 'skipped'] += 1
            except Exception:
                tally['failed'] += 1
                frappe.log_error(frappe.get_traceback(), 'fabric_matcher.bulk_match')
            written += 1
            if written % BULK_COMMIT_CHUNK == 0:
                frappe.db.commit()

    frappe.db.commit()
    return tally


@frappe.whitelist()
def refresh_fabric_costs(names=None, enqueue=0, force=0):
    '''
    Recost whatever Fabric Master each row is already linked to, without
    re-matching anything. Backs the "Refresh Fabric Master Cost" list action.

    Two things need this. The stored cost is only as fresh as the run that wrote
    it, and yarn rates move; and a hand-picked match has its cost cleared on save
    (the old figure belonged to the old fabric), so a manual row needs a way to
    get one back that does not overwrite the pick.

    names   : optional JSON array / list of Surplus Stock ids.
    enqueue : truthy -> run in the background and return immediately.
    force   : truthy -> also recost rows that already carry a cost.

    -> {enqueued, total} when backgrounded, else
       {total, costed, unchanged, skipped, failed}.
    '''
    if not frappe.has_permission(DOCTYPE_SURPLUS_STOCK, 'write'):
        raise frappe.PermissionError

    ids = _bulk_stock_ids(names)
    if not ids:
        return {'total': 0, 'costed': 0, 'unchanged': 0, 'skipped': 0, 'failed': 0}

    if cint(enqueue):
        frappe.enqueue(
            'prism.api.fabric_matcher._refresh_fabric_costs',
            queue='long', timeout=3600, names=ids, force=cint(force),
        )
        return {'enqueued': True, 'total': len(ids)}

    return _refresh_fabric_costs(ids, cint(force))


def _refresh_fabric_costs(names, force=0):
    ''' The re-cost run itself, memoised per fabric exactly as the bulk match is. '''
    rows = _stock_rows(names, extra_fields=('closest_fabric_cost_per_kg',))
    tally = {'total': len(rows), 'costed': 0, 'unchanged': 0, 'skipped': 0, 'failed': 0}

    cost_cache = {}
    written = 0
    for row in rows:
        fabric_id = row.get('closest_fabric_master')
        if not fabric_id:
            tally['skipped'] += 1
            continue
        if row.get('closest_fabric_cost_per_kg') and not force:
            tally['unchanged'] += 1
            continue

        try:
            cost = _cost_per_kg(fabric_id, cost_cache)
            frappe.db.set_value(DOCTYPE_SURPLUS_STOCK, row['name'], {
                'closest_fabric_cost_per_kg': flt(cost),  # NOT NULL; see _store_match
                'closest_fabric_costed_on': now_datetime() if cost else None,
            }, update_modified=True)
            tally['costed' if cost else 'failed'] += 1
        except Exception:
            tally['failed'] += 1
            frappe.log_error(frappe.get_traceback(), 'fabric_matcher.refresh_costs')

        written += 1
        if written % BULK_COMMIT_CHUNK == 0:
            frappe.db.commit()

    frappe.db.commit()
    return tally


def _bulk_stock_ids(names):
    ''' Explicit ids (a list-view selection), or every Surplus Stock row. '''
    if names:
        if isinstance(names, str):
            names = frappe.parse_json(names)
        return [n for n in names if n]
    return frappe.get_all(DOCTYPE_SURPLUS_STOCK, pluck='name')


def _stock_rows(names, extra_fields=()):
    ''' The matchable columns for `names`, chunked so the IN(...) stays sane. '''
    fields = list(_STOCK_FIELDS) + [f for f in extra_fields if f not in _STOCK_FIELDS]
    rows = []
    for start in range(0, len(names), 500):
        rows.extend(frappe.get_all(
            DOCTYPE_SURPLUS_STOCK,
            filters={'name': ['in', names[start:start + 500]]},
            fields=fields,
            limit_page_length=0,
        ))
    return rows


def _cost_per_kg(fabric_id, cache=None):
    '''
    A Fabric Master's all-in per-kg cost, loss % included -- the same figure
    get_garment_cost rolls into a garment. None when the fabric cannot be costed.

    The figure has two names depending on which path produced it: `calculate()`
    writes `total_fabric_cost` and the cost_optimiser paths write `net_amount`.
    Reading only `net_amount` meant `_compute_fabric_cost` -- the one actually
    called here -- always came back empty, so every row silently costed as
    "uncostable". Both names are read, as `cost_optimiser._net_per_kg` does.

    Slow and worth caching hard: the first costing of a fabric makes LLM calls to
    match its knitting code and mechanical/chemical processes (5-11s observed),
    though it writes both back onto the Fabric Master so later calls are cheap.
    `cache` is the per-run memo -- 580 stock rows point at only 69 masters, so
    without it a bulk run would cost the same fabric dozens of times.

    Never raises: a fabric that will not cost must not cost a row its match.
    '''
    if not fabric_id:
        return None
    if cache is not None and fabric_id in cache:
        return cache[fabric_id]

    value = None
    try:
        import prism.api.costing as costing
        fabric = costing._compute_fabric_cost(fabric_id)
        cpk = (fabric or {}).get('cost_per_kg') or {}
        value = flt(cpk.get('net_amount') or cpk.get('total_fabric_cost')) or None
    except Exception:
        frappe.log_error(frappe.get_traceback(), 'fabric_matcher._cost_per_kg()')

    if cache is not None:
        cache[fabric_id] = value
    return value


def _store_match(name, signature, matches, cost=None):
    '''
    Write one row's match. `db.set_value` deliberately bypasses the document
    lifecycle: SurplusStock.validate() stamps Manual on any hand-edited link, and
    a matcher write must not trip that.
    '''
    best = matches[0] if matches else None
    alternates = [
        {'fabric_id': m['fabric_id'], 'fabric_code': m['fabric_code'],
         'construction': m['construction'], 'blend': m['blend'],
         'gsm': m['gsm'], 'score': m['score']}
        for m in matches[1:]
    ]

    frappe.db.set_value(DOCTYPE_SURPLUS_STOCK, name, {
        'closest_fabric_master': best['fabric_id'] if best else None,
        'closest_fabric_code': best['fabric_code'] if best else None,
        'closest_fabric_desc': best['fabric_description'] if best else None,
        'closest_match_score': best['score'] if best else 0,
        'closest_match_method': best['method'] if best else None,
        'closest_match_reason': best['reason'] if best else 'No comparable fabric found.',
        'closest_match_alternates': frappe.as_json(alternates) if alternates else None,
        'closest_match_signature': signature,
        'closest_matched_on': now_datetime(),
        # A Currency column is NOT NULL DEFAULT 0, and _cost_per_kg returns None
        # for a fabric that would not cost -- so the absence of a cost has to be
        # written as 0, not as NULL, or the whole write raises IntegrityError and
        # the row loses its match too. `closest_fabric_costed_on` staying empty
        # is what distinguishes "not costed" from "costs nothing".
        'closest_fabric_cost_per_kg': flt(cost),
        'closest_fabric_costed_on': now_datetime() if cost else None,
    }, update_modified=True)


# --- target normalisation ---------------------------------------------------

def _normalise_target(fabric: dict):
    '''
    A fabric in either vocabulary -> the shape the scorer compares.

    Construction text is classified through surplus_recommender, which reads mill
    shorthand and retail English with the same keyword table, so "SJY_EL" and
    "Single Jersey" both arrive as the jersey family.
    '''
    fabric = fabric if isinstance(fabric, dict) else {}

    construction = _clean(fabric.get('construction'))
    code = _clean(fabric.get('construction_code'))
    structure_label = sr._structure_label(code or construction)
    classified = sr._classify_construction(construction, structure_label, code)

    # The keyword table places every construction in the current data, including
    # the rows where the plain-language column still holds a raw code. It will
    # not place the next word the mill invents, and an unplaced construction
    # scores a flat 40 against everything -- the same answer for a jersey and a
    # drawcord. So an unknown is asked about once rather than shrugged at.
    family = classified['family']
    if not family and construction:
        family = _resolve_construction_family(construction)

    blend = _clean(fabric.get('blend')) or _clean(fabric.get('blend_code'))

    return {
        'construction': construction,
        'family': family,
        'trim_kind': _trim_kind(construction, code, structure_label),
        'tags': classified['tags'],
        'ratio': classified['ratio'],
        'blend': blend,
        'composition': _parse_any_blend(blend),
        'gsm': cint(flt(fabric.get('gsm'))),
    }


def _fabric_from_stock_row(row: dict):
    '''
    A Surplus Stock row -> the three things a match is made of, and only those:

        quality_full_name (else quality)  ->  Fabric Master `fabric`
        blend_full_name   (else blend)    ->  Fabric Master `blend`
        gsm                               ->  Fabric Master `finish_gsm`

    Nothing else on the row is matched against. The raw quality/blend codes used
    to ride along as extra evidence; they no longer do, because the full names
    are the stated input and a second opinion on the same field only muddies
    which one the score refers to.

    The single exception is GSM, and it is a recovery rather than a fourth
    input: 156 rows carry a zero in the column while `material_desc` spells the
    weight out ("SPD FLBK_RIB 100 BCI CC COMPACT 265 NAVY"). Without it those
    rows lose the weight dimension entirely -- and it is load-bearing, since it
    is what separates three otherwise identical FLBK_RIB / 100 BCI groups into
    265, 325 and 280 and sends them to different fabrics.
    '''
    return {
        'construction': _clean(row.get('quality_full_name')) or _clean(row.get('quality')),
        'blend': _clean(row.get('blend_full_name')) or _clean(row.get('blend')),
        'gsm': cint(row.get('gsm')) or cint(sr._decode_material_desc(row).get('gsm')),
    }


def _parse_any_blend(text):
    '''
    Any blend dialect on either side -> {fibre group: percent}.

    Three forms reach this, and only the first is written retail-style:

        Fabric Master     "95% Cotton 5% Spandex"
        BLEND             "95:5 BCI:EL"
        Blend full Name   "95:5 Bci Cotton:Elastane"

    The third is the one that matters and the one that used to break. It is a
    hybrid -- mill ratios, retail fibre names -- so the mill parser tore it apart
    on spaces as well as colons: "Bci Cotton" became two tokens, the percent-to-
    fibre pairing slid by one, and everything past the first fibre was dropped
    without complaint. "95:5 Bci Cotton:Elastane" read as 100% cotton, and a
    60/40 cotton-poly read as 100% cotton against a catalogue that knew better --
    which is exactly the 60% overlap that capped those rows at 84.

    So the ratio forms are parsed as what they are: two colon-separated lists of
    equal length, zipped. That covers the codes and the names in one path, since
    "95:5 BCI:EL" has the same shape as "95:5 Bci Cotton:Elastane".
    '''
    text = _clean(text)
    if not text:
        return {}
    if '%' in text:
        return sr._parse_brief_blend(text)
    return _parse_ratio_blend(text) or sr._parse_brief_blend(text)


# "<pct>[:<pct>...] <fibre>[:<fibre>...]" -- the ratio group, then the name group.
_RATIO_BLEND_RE = re.compile(r'^\s*([\d.]+(?:\s*:\s*[\d.]+)*)\s+(.+?)\s*$')


def _parse_ratio_blend(text):
    '''
    "58:39:3 Fair Trade Org Cotton:Tencel:Elastane" -> cotton/tencel/elastane.

    The two groups must have the same number of entries or the pairing is a
    guess, and a guess here is what silently produced 100%-cotton readings of
    two-fibre cloth. Mismatched input returns nothing and lets the caller fall
    back rather than inventing a composition.
    '''
    match = _RATIO_BLEND_RE.match(text)
    if not match:
        return {}

    percents = [p.strip() for p in match.group(1).split(':')]
    names = [n.strip() for n in match.group(2).split(':')]
    if len(percents) != len(names):
        return {}

    parts = [(pct, name, _fibre_group(name)) for pct, name in zip(percents, names)]

    # Anything the vocabulary does not carry is asked once and remembered.
    unknown = [name for _pct, name, group in parts if not group]
    if unknown:
        resolved = _resolve_fibre_names(unknown)
        parts = [(pct, name, group or resolved.get(name.strip().lower()))
                 for pct, name, group in parts]

    composition = {}
    for pct, _name, group in parts:
        if not group:
            continue
        try:
            composition[group] = composition.get(group, 0.0) + float(pct)
        except ValueError:
            continue

    return sr._normalised(composition)


def _fibre_group(name):
    '''
    One fibre token -> its group. Mill codes are matched whole ("O", "RP", "BCI"),
    names by longest known fibre inside them, so "Vasudha Primo Cotton" and
    "Bci Cotton" both land on cotton without needing their own entries.
    '''
    token = re.sub(r'[_\-]+', ' ', str(name or '')).strip()
    if not token:
        return None
    direct = sr.FIBRE_GROUP_BY_CODE.get(token.upper())
    if direct:
        return direct
    return sr._fibre_group_from_words(token)


def _resolve_fibre_names(names):
    '''
    Fibre names the vocabulary cannot place -> their groups, via Claude, cached
    by name forever after.

    This is where the LLM belongs in the blend path. The mill writes fibre names
    freely -- brand names ("Excel"), spelling drift ("Recycle Polyster"), house
    shorthand -- and no static table survives that. But the ratios around them
    are perfectly regular, so the model is asked the one question it is better
    at than a lookup table ("what fibre is this?") and nothing else. Unresolvable
    names are cached as None too, so a name is asked about exactly once.
    '''
    wanted = sorted({str(n or '').strip().lower() for n in names if str(n or '').strip()})
    if not wanted:
        return {}

    cache = frappe.cache().get_value(FIBRE_CACHE_KEY)
    cache = dict(cache) if isinstance(cache, dict) else {}

    missing = [n for n in wanted if n not in cache]
    if missing:
        learned = _llm_fibre_groups(missing)
        for name in missing:
            cache[name] = learned.get(name)
        frappe.cache().set_value(FIBRE_CACHE_KEY, cache)

    return {n: cache.get(n) for n in wanted}


def _llm_fibre_groups(names):
    ''' The uncached ask behind _resolve_fibre_names(). {} on any failure. '''
    try:
        system_prompt = f'''You are a textile fibre expert. You will be given fibre names taken from a knitted-fabric mill's blend descriptions. They may be brand names, regional trade names, abbreviations, or misspellings.

Map each to exactly one of these fibre groups:
{', '.join(KNOWN_FIBRE_GROUPS)}

Rules:
1. Use the group a textile technologist would file the fibre under. Brand names map to their generic fibre (for example a lyocell brand maps to tencel, an elastane brand maps to elastane).
2. Correct obvious misspellings before deciding.
3. If a name is genuinely not a fibre, or you cannot tell which fibre it is, map it to null. A wrong guess corrupts a blend; null is the safe answer.
4. Respond with ONLY a JSON object mapping each input name, verbatim and lowercased, to a group name or null. No prose, no markdown fences.

Example: {{"excel": "tencel", "recycle polyster": "polyester", "widget": null}}'''

        user_prompt = 'Fibre names:\n' + '\n'.join(f'- {n}' for n in names)

        response = llm.get_claude_response(
            system_prompt, user_prompt, ret_type='dict', max_tokens=1000
        )
    except Exception:
        frappe.log_error(frappe.get_traceback(), 'fabric_matcher._llm_fibre_groups()')
        return {}

    if not isinstance(response, dict):
        return {}

    resolved = {}
    for name, group in response.items():
        key = str(name or '').strip().lower()
        if key and group in KNOWN_FIBRE_GROUPS:
            resolved[key] = group
    return resolved


def _resolve_construction_family(construction):
    '''
    A construction the keyword table cannot place -> its family, via Claude,
    cached by the construction text forever after. None when even Claude
    declines, which leaves it scoring as unknown exactly as before.
    '''
    key = str(construction or '').strip().lower()
    if not key:
        return None

    cache = frappe.cache().get_value(FAMILY_CACHE_KEY)
    cache = dict(cache) if isinstance(cache, dict) else {}
    if key in cache:
        return cache[key]

    cache[key] = _llm_construction_family(construction)
    frappe.cache().set_value(FAMILY_CACHE_KEY, cache)
    return cache[key]


def _llm_construction_family(construction):
    ''' The uncached ask behind _resolve_construction_family(). None on failure. '''
    try:
        system_prompt = f'''You are a knitted-fabric technologist. You will be given one construction name from a mill's records. It may be plain English, mill shorthand, an abbreviation, or a misspelling.

Classify it into exactly one of these construction families:
{', '.join(KNOWN_FAMILIES)}

Notes:
- `trim` covers findings rather than yardage: collars, cuffs, drawcords, tapes, dori.
- `jersey` means single jersey and its close variants; `interlock` is double knit.
- Ignore yarn and finish annotations (elastane, compact, siro, CC) -- classify the STRUCTURE.

If you cannot tell which family it is, answer null. A wrong family is worse than
no family, because it makes unlike fabrics look like substitutes.

Respond with ONLY a JSON object, no prose and no markdown fences:
{{"family": "<one of the families above>" | null}}'''

        response = llm.get_claude_response(
            system_prompt, f'Construction: {construction}', ret_type='dict', max_tokens=500
        )
    except Exception:
        frappe.log_error(frappe.get_traceback(), 'fabric_matcher._llm_construction_family()')
        return None

    if not isinstance(response, dict):
        return None
    family = response.get('family')
    return family if family in KNOWN_FAMILIES else None


def _signature(row):
    ''' Digest of what a match was computed from, for staleness detection. '''
    fabric = _fabric_from_stock_row(row)
    raw = '|'.join([
        ALGO_VERSION,
        str(fabric.get('construction') or ''),
        str(fabric.get('blend') or ''),
        str(fabric.get('gsm') or 0),
    ])
    return hashlib.sha1(raw.encode('utf-8')).hexdigest()[:16]


# --- the catalogue index ----------------------------------------------------

def _fabric_master_index():
    '''
    Fabric Master's distinct (construction, blend) combinations, each parsed into
    the comparison space once and cached. ~300 combinations over ~12.7k rows, so
    parsing them per match would be the whole cost of matching.

    The cache key carries a digest of the table's size and last modification, so
    an edited catalogue rebuilds itself without anyone remembering to clear it.
    '''
    digest = _catalogue_digest()
    cached = frappe.cache().get_value(INDEX_CACHE_KEY)
    if isinstance(cached, dict) and cached.get('digest') == digest:
        return cached['combos']

    combos = _build_index()
    frappe.cache().set_value(
        INDEX_CACHE_KEY, {'digest': digest, 'combos': combos},
        expires_in_sec=INDEX_CACHE_TTL,
    )
    return combos


def _catalogue_digest():
    row = frappe.db.sql(
        f'select count(*), max(modified) from `tab{DOCTYPE_FABRIC_MASTER}`'
    )[0]
    return f'{ALGO_VERSION}:{row[0]}:{row[1]}'


def _build_index():
    ''' The uncached build behind _fabric_master_index(). '''
    rows = frappe.db.sql(
        f'''
        select fabric, blend, finish_gsm, count(*) as n
        from `tab{DOCTYPE_FABRIC_MASTER}`
        where ifnull(fabric, '') != '' and ifnull(blend, '') != ''
        group by fabric, blend, finish_gsm
        ''',
        as_dict=True,
    )

    grouped = {}
    for row in rows:
        construction = (row['fabric'] or '').strip()
        if construction.upper() in IGNORED_CONSTRUCTIONS:
            continue
        key = (construction, row['blend'])
        entry = grouped.setdefault(key, {'gsm_values': set(), 'rows': 0})
        entry['rows'] += cint(row['n'])
        if cint(row['finish_gsm']):
            entry['gsm_values'].add(cint(row['finish_gsm']))

    combos = []
    for (construction, blend), entry in grouped.items():
        classified = sr._classify_construction(construction)
        combos.append({
            'construction': construction,
            'blend': blend,
            'family': classified['family'],
            'trim_kind': _trim_kind(construction),
            'tags': sorted(classified['tags']),
            'ratio': classified['ratio'],
            'composition': sr._parse_brief_blend(blend),
            'gsm_values': sorted(entry['gsm_values']),
            'rows': entry['rows'],
        })
    return combos


def _fabric_master_row(combo, gsm):
    '''
    The Fabric Master record inside a combination whose finish_gsm is nearest
    `gsm`. Ordered by name after the GSM distance so the same input always
    returns the same record -- the catalogue holds thousands of rows per
    combination and an unordered LIMIT 1 is a different answer each time.
    '''
    rows = frappe.db.sql(
        f'''
        select
            name              as fabric_id,
            dyed_fabric_material      as fabric_code,
            dyed_fabric_material_desc as fabric_description,
            fabric            as construction,
            blend,
            finish_gsm        as gsm,
            shade_cat         as shade_category
        from `tab{DOCTYPE_FABRIC_MASTER}`
        where fabric = %(construction)s and blend = %(blend)s
        order by abs(ifnull(finish_gsm, 0) - %(gsm)s) asc, name asc
        limit 1
        ''',
        {'construction': combo['construction'], 'blend': combo['blend'], 'gsm': cint(gsm)},
        as_dict=True,
    )
    return rows[0] if rows else None


# --- scoring ----------------------------------------------------------------

def _score_combo(target, combo):
    '''
    One target against one catalogue combination -> its score and sub-scores, or
    None when a gate rejects it outright.
    '''
    # Trims and yardage are different kinds of thing, not near misses. A collar
    # tape and a jersey can share a fibre blend exactly and still be the one
    # answer that is never useful, so neither may stand in for the other.
    if _is_trim(target['family']) != _is_trim(combo['family']):
        return None

    # Within the trims, kind decides. All five share the family, so without this
    # a drawcord scores a perfect construction match against a collar and then
    # wins on blend -- which is how "100 BCI drawcord" came back as a Collar.
    trim_factor = 1.0
    if _is_trim(combo['family']):
        target_kind, combo_kind = target['trim_kind'], combo['trim_kind']
        if target_kind and combo_kind and target_kind != combo_kind:
            trim_factor = RELATED_TRIM_KINDS.get(frozenset((target_kind, combo_kind)), 0.0)
            if not trim_factor:
                return None

    construction = _construction_score(target, combo) * trim_factor
    composition = sr._composition_score(target['composition'], combo['composition'])
    gsm, matched_gsm = _gsm_score(target['gsm'], combo['gsm_values'])

    score = (construction * W_CONSTRUCTION
             + composition * W_COMPOSITION
             + gsm * W_GSM)

    # Stretch is functional, not decorative: a 95/5 target met by a 100% cotton
    # rib is the right hand-feel and the wrong garment.
    target_el = target['composition'].get('elastane', 0.0)
    combo_el = combo['composition'].get('elastane', 0.0)
    stretch_penalty = 1.0
    if target_el >= sr.STRETCH_REQUIRED_PCT and combo_el <= 0:
        stretch_penalty = sr.NO_STRETCH_PENALTY
    elif combo_el >= sr.STRETCH_REQUIRED_PCT and target_el <= 0:
        stretch_penalty = sr.EXTRA_STRETCH_PENALTY
    score *= stretch_penalty

    return {
        'combo': combo,
        'score': score,
        'matched_gsm': matched_gsm,
        'breakdown': {
            'construction': round(construction, 1),
            'composition': round(composition, 1),
            'gsm': round(gsm, 1),
            'stretch_penalty': stretch_penalty,
        },
    }


def _construction_score(target, combo):
    ''' Family match, with partial credit for a plausible substitute knit. '''
    a, b = target['family'], combo['family']
    if not a or not b:
        return CONSTRUCTION_UNKNOWN_SCORE

    if a == b:
        score = 100.0
        # A named rib gauge ("2x1 Rib") is meant; agreeing is worth a little and
        # disagreeing costs a little.
        if target['ratio'] and combo['ratio']:
            score = 100.0 if target['ratio'] == combo['ratio'] else 88.0
    else:
        score = 100.0 * sr.RELATED_FAMILIES.get(frozenset((a, b)), 0.0)

    if target['tags'] & set(combo['tags']):
        score = min(100.0, score + sr.TEXTURE_TAG_BONUS)

    return score


def _gsm_score(target_gsm, gsm_values):
    '''
    Against the nearest weight the combination is actually stocked in ->
    (score, that weight). A combination spanning 140-220 should not be judged on
    an average nobody knits.
    '''
    if not target_gsm:
        return GSM_UNKNOWN_SCORE, None
    if not gsm_values:
        return float(sr.GSM_UNKNOWN_SCORE), None

    nearest = min(gsm_values, key=lambda g: abs(g - target_gsm))
    score = 100.0 * max(0.0, 1.0 - abs(target_gsm - nearest) / float(GSM_TOLERANCE))
    return score, nearest


def _rank_key(entry):
    '''
    Score first, then how much of the catalogue is written that way.

    Exact ties are common and not cosmetic: "95% Cotton 5% Spandex" (370 rows)
    and "76% Cotton 19% Cotton 5% Spandex" (1 row) normalise to the same fibres
    and score identically, so without a second term the answer came down to dict
    order. Prevalence picks the spelling the mill actually uses, and the trailing
    text keys make the result stable rather than merely plausible.
    '''
    combo = entry['combo']
    return (-entry['score'], -combo['rows'], combo['construction'], combo['blend'])


def _is_trim(family):
    return family == 'trim'


def _trim_kind(*texts):
    ''' Which trim a construction names -- collar, cuff, drawcord, dori, tape. '''
    joined = ' '.join(str(t or '') for t in texts).replace('_', ' ').lower()
    joined = re.sub(r'\s+', ' ', joined).strip()
    for keyword, kind in TRIM_KINDS:
        if re.search(rf'(?<![a-z]){re.escape(keyword)}(?![a-z])', joined):
            return kind
    return None


def _is_runaway(ranked):
    '''
    Whether the leader is clear enough that asking Claude could only agree.

    Margin alone is the wrong test, and measuring it taught that the hard way:
    the catalogue carries 95/5, 96/4 and 94/6 cotton-elastane side by side, so an
    exactly-right leader still sits ~0.4 ahead of its neighbours and 95% of rows
    were being sent for adjudication between answers that differ by a rounding.

    What actually settles a match is whether the leader is right, not whether
    the runners-up are far away. An exact family with essentially all the fibre
    in common is right, and nothing 0.02 behind it makes that less true. GSM is
    deliberately not required: when the stock has no weight recorded, the model
    has no more to go on than the scorer does.
    '''
    if not ranked:
        return True

    breakdown = ranked[0]['breakdown']
    if (breakdown['construction'] >= 100.0
            and breakdown['composition'] >= COMPOSITION_CERTAIN
            and breakdown['stretch_penalty'] == 1.0):
        return True

    if len(ranked) == 1:
        return True
    if ranked[0]['score'] < RUNAWAY_SCORE:
        return False
    return (ranked[0]['score'] - ranked[1]['score']) >= RUNAWAY_MARGIN


def _reason(target, entry, below_floor):
    ''' The score in the sentence a merchandiser would write under it. '''
    combo = entry['combo']
    breakdown = entry['breakdown']
    bits = []

    if target['family'] and combo['family']:
        if target['family'] == combo['family']:
            bits.append(f'{combo["construction"]} matches the {target["family"]} construction')
        else:
            bits.append(
                f'{combo["construction"]} substitutes for a {target["family"]} construction'
            )
    else:
        bits.append(f'construction read as {combo["construction"]}')

    bits.append(f'{breakdown["composition"]:.0f}% shared fibre with "{combo["blend"]}"')

    if entry.get('matched_gsm') and target['gsm']:
        delta = abs(entry['matched_gsm'] - target['gsm'])
        bits.append(f'{entry["matched_gsm"]} GSM vs {target["gsm"]} (off by {delta})')
    elif not target['gsm']:
        bits.append('no GSM on the stock to compare')

    if breakdown['stretch_penalty'] < 1.0:
        bits.append('elastane content differs')
    if below_floor:
        bits.append('NOTE: nothing shared enough fibre to clear the match floor')

    return '; '.join(bits) + '.'


# --- LLM re-rank ------------------------------------------------------------

def _llm_rerank(target, shortlist):
    '''
    Claude re-orders a shortlist the scorer already built, and says why.

    It never sees the full catalogue and never names a fabric: it returns
    positions in the list it was given, so the worst it can do is prefer a
    candidate that already passed the gates and the composition floor. A failure
    here is not an error -- the deterministic order stands.
    '''
    if len(shortlist) < 2:
        return None

    try:
        lines = []
        for i, entry in enumerate(shortlist):
            combo = entry['combo']
            lines.append(
                f'{i}\t{combo["construction"]}\t{combo["blend"]}'
                f'\tparsed={_composition_text(combo["composition"])}'
                f'\tgsm={entry.get("matched_gsm") or "?"}'
                f'\tscore={entry["score"]:.1f}'
            )

        system_prompt = f'''You are a textile technologist at a knitted-garment manufacturer, matching a fabric to the closest entry in the company's Fabric Master catalogue.

A deterministic scorer has already filtered the catalogue down to the candidates below and ranked them. Your job is ONLY to re-order these candidates using textile judgement the scorer cannot apply -- for example that a flatback rib is a poor stand-in for a plain rib at high elastane, or that a fleece and a terry differ in face finish even at the same weight.

Each candidate is one tab-separated line:
<index>\t<construction>\t<blend>\tparsed=<normalised fibre percentages>\tgsm=<nearest stocked weight>\tscore=<deterministic score 0-100>

Rules:
1. Return ONLY indices from the list. Never invent a construction or blend.
2. `parsed=` is the authoritative fibre reading -- both the input and the catalogue have already been normalised into the same fibre groups, so "Spandex" and "EL" are both elastane, and "Lyocell" and "T" are both tencel. Do not re-litigate the arithmetic.
3. Keep the scorer's order unless you have a specific textile reason to change it. Returning it unchanged is a valid and common answer.
4. Return at most {MAX_MATCHES} indices, best first, no duplicates.
5. Respond with ONLY a JSON object, no prose and no markdown fences:
   {{"ranking": [<index>, ...], "reason": "<one sentence on why the leader wins>"}}
'''

        user_prompt = f'''Fabric to match:
  construction: {target['construction'] or '(unknown)'}
  construction family: {target['family'] or '(unclassified)'}
  blend: {target['blend'] or '(unknown)'}
  blend parsed: {_composition_text(target['composition'])}
  gsm: {target['gsm'] or '(unknown)'}

Candidates:
{chr(10).join(lines)}

Return the JSON object described above.'''

        response = llm.get_claude_response(
            system_prompt, user_prompt, ret_type='dict', max_tokens=RERANK_MAX_TOKENS
        )
    except Exception:
        frappe.log_error(frappe.get_traceback(), 'fabric_matcher._llm_rerank()')
        return None

    if not isinstance(response, dict):
        return None

    reason = _clean(response.get('reason'))
    ordered = []
    for i in response.get('ranking') or []:
        if isinstance(i, int) and 0 <= i < len(shortlist) and shortlist[i] not in ordered:
            ordered.append(shortlist[i])
        if len(ordered) >= MAX_MATCHES:
            break

    if not ordered:
        return None
    if reason:
        ordered[0] = {**ordered[0], 'reason': reason}
    return ordered


def _composition_text(composition):
    ''' {'cotton': 95.0, 'elastane': 5.0} -> "95% cotton, 5% elastane". '''
    if not composition:
        return '(unreadable)'
    ordered = sorted(composition.items(), key=lambda kv: -kv[1])
    return ', '.join(f'{sr._pct(v)}% {k}' for k, v in ordered)


def _clean(value):
    text = str(value or '').strip()
    return re.sub(r'\s+', ' ', text) or None
