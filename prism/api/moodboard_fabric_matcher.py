'''
Closest-Fabric-Master matching for Moodboard Dyed Fabric.

The scoring is not repeated here. `fabric_matcher` already parses both fabric
vocabularies into one comparison space, scores every catalogue combination
(construction 40 / composition 40 / GSM 20), gates trims apart from yardage and
asks Claude to re-rank only when the leader is not already clear. That module is
the matcher; this one is the Moodboard Dyed Fabric side of it -- which three
columns are read, how a match is written back, and the bulk/background plumbing
behind the desk buttons.

The mapping, which is the only thing that differs from Surplus Stock:

    Clean Quality  ->  Fabric Master `fabric`      (construction)
    Clean Blend    ->  Fabric Master `blend`       (composition)
    GSM            ->  Fabric Master `finish_gsm`  (weight)

WARNING -- `clean_quality` is not currently a faithful construction. On
prism.localhost (2026-08-25) 24,099 of 24,838 rows carry "Single Jersey",
including every RIB_1X1, FLBK_RIB, TTP_HBONE, CLR and 3T_DIA_FLC row; the column
holds 9 distinct values where the raw `quality` code holds 231. Matching on it
therefore sends most of the table to a Single Jersey Fabric Master, and trims
(collars, tapes, drawcords) to knit yardage rather than to their own kind.
`CONSTRUCTION_FIELDS` below is the one place that decides this: putting
'quality' in front of 'clean_quality' switches the whole module over to the mill
code, which `fabric_matcher` reads natively (it is what Surplus Stock feeds it).
Left as specified until `clean_quality` is repopulated.

Rows are matched once per distinct (clean quality, clean blend, GSM) triple and
the answer copied across everything that shares it -- 24,838 rows resolve to 498
triples, so the scoring, the LLM calls and the fabric costing each happen a few
hundred times rather than twenty-odd thousand. Note that a hand-picked SELECTION
does not collapse nearly as well: the first 300 rows by name hold 259 distinct
triples.

Matching runs in one of two modes, meant to be used cheap-first:

    deterministic   the scorer alone. No model calls anywhere -- no re-rank, and
                    no costing of the matched fabric. Seconds over the table.
    LLM assisted    the full pass. Claude re-ranks the shortlist on the rows
                    where the leader is unclear (about one in eight), and each
                    matched fabric is costed.

Both write the same fields; the mode is recorded on the stored signature so a
later LLM run knows to revisit what the deterministic run did, while a repeat
deterministic run knows to leave LLM-matched rows alone. See _is_current().
'''

import hashlib

import frappe
from frappe.utils import cint, flt, now_datetime

import prism.api.fabric_matcher as fm

DOCTYPE = 'Moodboard Dyed Fabric'

# In preference order: the first of these with a value is the construction the
# match is made on. See the warning above before reordering.
CONSTRUCTION_FIELDS = ('clean_quality',)
BLEND_FIELDS = ('clean_blend',)

# Bumped when the field mapping changes, so stored signatures stop agreeing and
# a re-run recomputes instead of trusting a match made from other columns.
# Independent of fabric_matcher.ALGO_VERSION, which covers the scoring; both go
# into the signature.
MAPPING_VERSION = 'mdf-v1'

# The two ways a row can be matched, in increasing order of expense. They are
# the same scorer -- LLM mode only adds a re-rank of the top few when the
# deterministic leader is not already clear, and lets the matched fabric be
# costed. Deterministic mode makes no model calls at all, so a full pass over
# the table is seconds rather than hours.
#
# The mode is recorded as a prefix on the stored signature rather than in a
# column of its own, because it is part of the same question the signature
# answers: "would a re-run of THIS produce what is already there". Note that it
# records the mode that was ASKED FOR, which is not the same as
# `closest_match_method` -- most rows are clear enough that LLM mode never
# reaches the model and still stores 'Deterministic'. Keying the staleness check
# on the method would put those rows back in the queue on every LLM run.
MODE_DETERMINISTIC = 'd'
MODE_LLM = 'l'

# One UPDATE per this many rows. Rows sharing a signature get identical values,
# so a group is written as a single statement rather than a row at a time --
# the difference between ~500 statements and ~25,000 on a full run.
WRITE_CHUNK = 500
BULK_COMMIT_CHUNK = 2000

_ROW_FIELDS = (
    'name', 'code', 'quality', 'clean_quality', 'blend', 'clean_blend', 'gsm',
    'closest_fabric_master', 'closest_match_method', 'closest_match_signature',
)


# --- public: matching -------------------------------------------------------

def match_row(row: dict, use_llm: bool = True):
    ''' One Moodboard Dyed Fabric row (a dict of _ROW_FIELDS) -> matches. '''
    return fm.match_fabric(_fabric_from_row(row), use_llm=use_llm)


@frappe.whitelist()
def find_closest_fabric_master(name: str, force: int = 0, use_llm: int = 1):
    '''
    Match one Moodboard Dyed Fabric record and store the result. Backs the
    single-record button on the form, which asks for the full LLM-assisted pass
    -- one row is not where the model calls hurt.
    '''
    if not frappe.has_permission(DOCTYPE, 'write'):
        raise frappe.PermissionError

    row = frappe.db.get_value(DOCTYPE, name, list(_ROW_FIELDS), as_dict=True)
    if not row:
        frappe.throw(f'No Moodboard Dyed Fabric record named "{name}".')

    if row.get('closest_match_method') == fm.METHOD_MANUAL and not cint(force):
        return {'matched': 0, 'skipped': 1, 'reason': 'Match was set manually.'}

    use_llm = bool(cint(use_llm))
    matches = match_row(row, use_llm=use_llm)
    cost = fm._cost_per_kg(matches[0]['fabric_id']) if (matches and use_llm) else None
    _store_match([row['name']], _signature(row, use_llm), matches, cost)
    frappe.db.commit()

    return {
        'matched': 1 if matches else 0,
        'skipped': 0,
        'match': matches[0] if matches else None,
    }


@frappe.whitelist()
def calculate_fabric_cost(name: str):
    '''
    Cost the Fabric Master one Moodboard Dyed Fabric row is matched to, and
    store the figure. Backs the "Calculate Cost" button on the form.

    Always recomputes: a person pressing this on one record is asking for a
    fresh number, not for the cached one to be left alone. The bulk action is
    where "only the rows without a cost" is the useful default.

    -> {costed, cost, fabric, reason}.
    '''
    if not frappe.has_permission(DOCTYPE, 'write'):
        raise frappe.PermissionError

    fabric_id = frappe.db.get_value(DOCTYPE, name, 'closest_fabric_master')
    if not fabric_id:
        return {'costed': 0, 'cost': None, 'fabric': None,
                'reason': 'No Fabric Master matched yet, so there is nothing to cost.'}

    cost = fm._cost_per_kg(fabric_id)
    _set_values([name], {
        'closest_fabric_cost_per_kg': flt(cost),  # NOT NULL; see _store_match
        'closest_fabric_costed_on': now_datetime() if cost else None,
    })
    frappe.db.commit()

    return {
        'costed': 1 if cost else 0,
        'cost': cost,
        'fabric': fabric_id,
        'reason': None if cost else (
            'The matched Fabric Master would not cost -- most often one of its '
            'yarns is missing from Yarn Rate or carries a zero rate.'
        ),
    }


# --- public: bulk -----------------------------------------------------------

@frappe.whitelist()
def bulk_match_closest_fabric(names=None, enqueue=0, force=0, use_llm=1):
    '''
    Match many Moodboard Dyed Fabric rows and store the results. Backs the
    "Find Closest Fabric Master" list-view actions.

    names   : optional JSON array / list of ids (a list-view selection). When
              omitted, EVERY row is considered.
    enqueue : truthy -> run in the background and return immediately.
    force   : truthy -> also recompute rows whose signature still agrees and
              rows whose match was set by hand.
    use_llm : truthy (default) -> the full pass: Claude re-ranks the shortlist
              where the leader is unclear, and the matched fabric is costed.
              Falsy -> the deterministic pass, which makes no model calls at all
              and leaves costing to "Refresh Fabric Master Cost".

    The two are meant to be run in that order, cheap first: a deterministic
    sweep puts a defensible match on every row in seconds, and a later LLM run
    revisits them. See _is_current() for why the second run does not consider
    the first one's work already done, and why the reverse is not true.

    -> {enqueued, total} when backgrounded, else
       {total, matched, unchanged, skipped, failed}.
    '''
    if not frappe.has_permission(DOCTYPE, 'write'):
        raise frappe.PermissionError

    ids = _bulk_ids(names)
    if not ids:
        return {'total': 0, 'matched': 0, 'unchanged': 0, 'skipped': 0, 'failed': 0}

    if cint(enqueue):
        frappe.enqueue(
            'prism.api.moodboard_fabric_matcher._bulk_match_closest_fabric',
            queue='long', timeout=7200, names=ids, force=cint(force),
            use_llm=bool(cint(use_llm)),
        )
        return {'enqueued': True, 'total': len(ids)}

    return _bulk_match_closest_fabric(ids, cint(force), bool(cint(use_llm)))


def _bulk_match_closest_fabric(names, force=0, use_llm=True):
    '''
    The bulk run itself.

    Matching is done once per distinct signature and the result written to every
    row that shares it -- rows described identically cannot have different
    closest fabrics, and doing otherwise would repeat the same scoring (and the
    same LLM call) tens of thousands of times.
    '''
    rows = _rows(names)
    tally = {'total': len(rows), 'matched': 0, 'unchanged': 0, 'skipped': 0, 'failed': 0}

    # signature -> ids needing it
    pending = {}
    for row in rows:
        if row.get('closest_match_method') == fm.METHOD_MANUAL and not force:
            tally['skipped'] += 1
            continue
        if (not force
                and row.get('closest_fabric_master')
                and _is_current(row.get('closest_match_signature'), row, use_llm)):
            tally['unchanged'] += 1
            continue
        signature = _signature(row, use_llm)
        pending.setdefault(signature, {'ids': [], 'row': row})['ids'].append(row['name'])

    since_commit = 0
    cost_cache = {}
    for signature, group in pending.items():
        ids = group['ids']
        try:
            matches = match_row(group['row'], use_llm=use_llm)
        except Exception:
            tally['failed'] += len(ids)
            frappe.log_error(frappe.get_traceback(), 'moodboard_fabric_matcher.bulk_match')
            continue

        # Costed once per matched fabric, not once per row: the memo is what
        # keeps 24.8k rows down to the handful of masters they point at. Skipped
        # entirely in deterministic mode -- costing a fabric for the first time
        # is itself several model calls, and is the larger half of what makes a
        # full LLM pass slow. "Refresh Fabric Master Cost" fills it in later.
        cost = (fm._cost_per_kg(matches[0]['fabric_id'], cost_cache)
                if (matches and use_llm) else None)

        try:
            _store_match(ids, signature, matches, cost)
            tally['matched' if matches else 'skipped'] += len(ids)
        except Exception:
            tally['failed'] += len(ids)
            frappe.log_error(frappe.get_traceback(), 'moodboard_fabric_matcher.bulk_match')

        since_commit += len(ids)
        if since_commit >= BULK_COMMIT_CHUNK:
            frappe.db.commit()
            since_commit = 0

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

    names   : optional JSON array / list of Moodboard Dyed Fabric ids.
    enqueue : truthy -> run in the background and return immediately.
    force   : truthy -> also recost rows that already carry a cost.

    -> {enqueued, total} when backgrounded, else
       {total, costed, unchanged, skipped, failed}.
    '''
    if not frappe.has_permission(DOCTYPE, 'write'):
        raise frappe.PermissionError

    ids = _bulk_ids(names)
    if not ids:
        return {'total': 0, 'costed': 0, 'unchanged': 0, 'skipped': 0, 'failed': 0}

    if cint(enqueue):
        frappe.enqueue(
            'prism.api.moodboard_fabric_matcher._refresh_fabric_costs',
            queue='long', timeout=7200, names=ids, force=cint(force),
        )
        return {'enqueued': True, 'total': len(ids)}

    return _refresh_fabric_costs(ids, cint(force))


def _refresh_fabric_costs(names, force=0):
    '''
    The re-cost run itself.

    Grouped by the fabric being costed rather than walked row by row: costing is
    the slow part (LLM calls on a fabric's first costing), and every row pointing
    at the same master gets the same figure, so each master is costed once and
    its rows updated in one statement.
    '''
    rows = _rows(names, extra_fields=('closest_fabric_cost_per_kg',))
    tally = {'total': len(rows), 'costed': 0, 'unchanged': 0, 'skipped': 0, 'failed': 0}

    # fabric_id -> ids needing a cost
    pending = {}
    for row in rows:
        fabric_id = row.get('closest_fabric_master')
        if not fabric_id:
            tally['skipped'] += 1
            continue
        if row.get('closest_fabric_cost_per_kg') and not force:
            tally['unchanged'] += 1
            continue
        pending.setdefault(fabric_id, []).append(row['name'])

    since_commit = 0
    cost_cache = {}
    for fabric_id, ids in pending.items():
        try:
            cost = fm._cost_per_kg(fabric_id, cost_cache)
            _set_values(ids, {
                'closest_fabric_cost_per_kg': flt(cost),  # NOT NULL; see _store_match
                'closest_fabric_costed_on': now_datetime() if cost else None,
            })
            tally['costed' if cost else 'failed'] += len(ids)
        except Exception:
            tally['failed'] += len(ids)
            frappe.log_error(frappe.get_traceback(), 'moodboard_fabric_matcher.refresh_costs')

        since_commit += len(ids)
        if since_commit >= BULK_COMMIT_CHUNK:
            frappe.db.commit()
            since_commit = 0

    frappe.db.commit()
    return tally


# --- reading and writing ----------------------------------------------------

def _bulk_ids(names):
    ''' Explicit ids (a list-view selection), or every row. '''
    if names:
        if isinstance(names, str):
            names = frappe.parse_json(names)
        return [n for n in names if n]
    return frappe.get_all(DOCTYPE, pluck='name')


def _rows(names, extra_fields=()):
    ''' The matchable columns for `names`, chunked so the IN(...) stays sane. '''
    fields = list(_ROW_FIELDS) + [f for f in extra_fields if f not in _ROW_FIELDS]
    rows = []
    for start in range(0, len(names), 500):
        rows.extend(frappe.get_all(
            DOCTYPE,
            filters={'name': ['in', names[start:start + 500]]},
            fields=fields,
            limit_page_length=0,
        ))
    return rows


def _fabric_from_row(row: dict):
    '''
    A Moodboard Dyed Fabric row -> the three things a match is made of, and only
    those:

        clean_quality  ->  Fabric Master `fabric`
        clean_blend    ->  Fabric Master `blend`
        gsm            ->  Fabric Master `finish_gsm`

    Nothing else on the row is matched against. GSM is taken as it stands: 2,205
    rows carry a zero and, unlike Surplus Stock's `material_desc`, no other
    column on this doctype spells the weight out, so those rows lose the weight
    dimension and score the neutral GSM_UNKNOWN rather than a guess.
    '''
    return {
        'construction': _first(row, CONSTRUCTION_FIELDS),
        'blend': _first(row, BLEND_FIELDS),
        'gsm': cint(row.get('gsm')),
    }


def _first(row, fieldnames):
    ''' The first of `fieldnames` with a non-blank value. '''
    for fieldname in fieldnames:
        value = fm._clean(row.get(fieldname))
        if value:
            return value
    return None


def _digest(row):
    ''' Hash of the three columns a match is made from. '''
    fabric = _fabric_from_row(row)
    raw = '|'.join([
        MAPPING_VERSION,
        fm.ALGO_VERSION,
        str(fabric.get('construction') or ''),
        str(fabric.get('blend') or ''),
        str(fabric.get('gsm') or 0),
    ])
    return hashlib.sha1(raw.encode('utf-8')).hexdigest()[:16]


def _signature(row, use_llm=True):
    '''
    What a match was computed from, for staleness detection:
    "<mode>:<digest>", e.g. "d:8c1f0b3a19d24e77".
    '''
    return f'{MODE_LLM if use_llm else MODE_DETERMINISTIC}:{_digest(row)}'


def _is_current(stored, row, use_llm):
    '''
    Whether `stored` already answers what this run would ask, so the row can be
    left alone.

    The asymmetry is the point of having two modes. An LLM run must redo a row
    that only ever had the deterministic pass -- that is the whole reason for
    running it second. A deterministic run must NOT redo a row that already
    carries an LLM-mode answer, or the cheap pass would quietly undo the
    expensive one every time someone reached for it.
    '''
    mode, separator, digest = (stored or '').partition(':')
    if not separator:
        return False  # never matched, or written before modes existed
    if digest != _digest(row):
        return False  # the columns changed underneath the match
    return mode == MODE_LLM or not use_llm


def _store_match(names, signature, matches, cost=None):
    '''
    Write one match onto every row in `names`.

    They are written together because they earned the match together -- a
    signature group is by definition rows the scorer cannot tell apart.
    '''
    best = matches[0] if matches else None
    alternates = [
        {'fabric_id': m['fabric_id'], 'fabric_code': m['fabric_code'],
         'construction': m['construction'], 'blend': m['blend'],
         'gsm': m['gsm'], 'score': m['score']}
        for m in matches[1:]
    ]

    _set_values(names, {
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
        # written as 0, not as NULL. `closest_fabric_costed_on` staying empty is
        # what distinguishes "not costed" from "costs nothing".
        'closest_fabric_cost_per_kg': flt(cost),
        'closest_fabric_costed_on': now_datetime() if cost else None,
    })


def _set_values(names, values):
    '''
    One UPDATE per WRITE_CHUNK ids.

    `db.set_value` deliberately bypasses the document lifecycle:
    MoodboardDyedFabric.validate() stamps Manual on any hand-edited link, and a
    matcher write must not trip that. A filter dict rather than a single name
    makes it one statement for the whole chunk.
    '''
    for start in range(0, len(names), WRITE_CHUNK):
        chunk = names[start:start + WRITE_CHUNK]
        frappe.db.set_value(
            DOCTYPE, {'name': ['in', chunk]}, values, update_modified=True
        )
