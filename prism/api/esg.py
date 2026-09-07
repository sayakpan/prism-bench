'''
Moodboard ESG read/write API for the frontend.

Two endpoints, both keyed by the Moodboard Style id (ESG is 1:1 with a style):
  - get_esg(id)      -> the full ESG record (parent + garment elements)
  - update_esg(id)   -> partial edit of the human fields AND the externally
                        computed rating snapshot (overall_rating, category, ...)

The wire shape is camelCase and identical for read and write: whatever get_esg
returns can be sent back to update_esg. Only the whitelisted fields below are
writable — identity (moodboard_style / moodboard) and unknown keys are ignored.
Auth: X-Auth-Token JWT, internal / PSL users only (brands get 403).
'''

import json
import math
import re

import frappe
import requests

from prism.auth.authenticator import auth_required
import prism.api.util as util
import prism.lib.esg as esglib

ESG_DOCTYPE = 'Moodboard ESG'
STYLE_DOCTYPE = 'Moodboard Style'
SAMPLE_DOCTYPE = 'Sample Request'

# External ESG rating engine (Google Apps Script), shared with DDQ/sampling. Sent
# as a CORS "simple request" (text/plain, no auth header). Body is
# {action: "esg", ...payload}; returns {success, data:{metric:{current,...}}}.
ESG_RATING_ENDPOINT = (
    'https://script.google.com/macros/s/'
    'AKfycbyKnGra4O-a6w1pAPeg5UcukeiYw2fP2pdMMsqZfbZhBxwYaIrUfYS-t2nXjx8pZCGp/exec'
)
# Canonical AOP / Digital options (see frontend canonicalAop). Any value not
# matching one of these (case-insensitively) maps to '' before the upper-casing the
# rating API expects.
_AOP_OPTIONS = ('Yes', 'No', 'Digital Print')
# Spreadsheet "not a value" text (see frontend ratingText): #REF!, #N/A, N/A, ...
_RATING_NA_RE = re.compile(r'^#?n/?a$', re.IGNORECASE)
# Bulk scoring: stop early after this many consecutive engine failures (service is
# likely down — don't grind through the remaining ids).
_BULK_RATING_ABORT_AFTER = 5

# Writable parent fields — Order Context, Trims, and the externally-computed
# Result Snapshot. camelCase (wire) -> snake_case (fieldname). snake_case input
# is also accepted.
_PARENT_FIELDS = {
    'customer': 'customer',
    'shipCountry': 'ship_country',
    'productionCity': 'production_city',
    'customerProfile': 'customer_profile',
    'suggestionCriteria': 'suggestion_criteria',
    'numberOfTrims': 'number_of_trims',
    'elastic': 'elastic',
    'trimStyle': 'trim_style',
    'overallRating': 'overall_rating',
    'scorePercent': 'score_percent',
    'category': 'category',
    'estimatedCost': 'estimated_cost',
    'zeroToleranceStatus': 'zero_tolerance_status',
}

# Writable garment-element fields.
_ELEMENT_FIELDS = {
    'section': 'section_name',
    'consumption': 'consumption',
    'markerEfficiency': 'marker_efficiency',
    'fabricType': 'fabric_type',
    'oldFabricType': 'old_fabric_type',
    'fabricBlend': 'fabric_blend',
    'shadeCategory': 'shade_category',
    'gsm': 'gsm',
    'yarnProcessType': 'yarn_process_type',
    'yarnSpecs': 'yarn_specs',
    'dyeingType': 'dyeing_type',
    'transportMode': 'transport_mode',
    'zeroToleranceFail': 'zero_tolerance_fail',
    'finishingMechChem': 'finishing_mech_chem',
    'finishingAopDigital': 'finishing_aop_digital',
    'finishingProcess': 'finishing_process',
    'yarnComposition': 'yarn_composition',
    'elementRating': 'element_rating',
}


@frappe.whitelist(allow_guest=True)
@auth_required
def get_esg(id=None):
    '''
    The ESG record for a style (`id` = Moodboard Style id). If the style is costed
    but has no ESG yet, one is created on demand; otherwise 404.
    '''
    _require_internal()
    return _to_api(_get_esg_for_style(id))


@frappe.whitelist(allow_guest=True, methods=['POST'])
@auth_required
def update_esg(id=None, elements=None, **fields):
    '''
    Partial update of the ESG for a style (`id` = Moodboard Style id). Send any of
    the writable parent fields (Order Context / Trims / rating snapshot) and/or an
    `elements` array. Only keys present are touched; elements upsert by `section`.
    Returns the updated ESG.
    '''
    _require_internal()
    doc = _get_esg_for_style(id)

    _apply_parent(doc, fields)
    if elements is not None:
        _apply_elements(doc, elements)

    doc.save(ignore_permissions=True)
    frappe.db.commit()
    return _to_api(doc)


@frappe.whitelist(methods=['POST'])
def feed_esg_input_for_sample(sample_id=None):
    '''
    Feed (once) the ESG input record for a Sample Request and return it. Backs the
    "Feed ESG Input" Desk button. Requires the sample to have both a Fabric Master
    and a Trim Costing linked. Idempotent: if an ESG already exists it is returned
    as-is (manual edits preserved), never rebuilt.

    This only populates the ESG *inputs* (order context, trims, garment elements) —
    it does not compute a rating; scoring happens downstream.

    Desk-invoked: authenticated by the standard Frappe session (NOT the frontend's
    X-Auth-Token JWT), so it uses plain @frappe.whitelist and doctype permissions —
    the caller must be able to read the sample and create a Moodboard ESG.

    -> {esg, created, message}. `esg` is None (with a message) when the sample
    isn't ready to build.
    '''
    if not sample_id:
        frappe.throw('Missing sample id.', frappe.ValidationError)
    if not frappe.db.exists(SAMPLE_DOCTYPE, sample_id):
        frappe.throw('Sample not found', frappe.DoesNotExistError)
    if not frappe.has_permission(SAMPLE_DOCTYPE, 'read', doc=sample_id):
        raise frappe.PermissionError
    if not frappe.has_permission(ESG_DOCTYPE, 'create'):
        raise frappe.PermissionError

    name = frappe.db.get_value(
        ESG_DOCTYPE, {'source_doctype': SAMPLE_DOCTYPE, 'source_name': sample_id}, 'name')
    created = False
    if not name:
        sample = frappe.get_doc(SAMPLE_DOCTYPE, sample_id)
        name = esglib.ensure_esg_for_sample(sample)
        created = bool(name)

    if not name:
        return {'esg': None, 'created': False,
                'message': 'Link a Fabric Master and a Trim Costing on the sample first.'}

    frappe.db.commit()
    return {'esg': name, 'created': created}


@frappe.whitelist(methods=['POST'])
def feed_esg_input_bulk(names=None, enqueue=0):
    '''
    Bulk-build ESG input records for Sample Requests that have BOTH a Fabric Master
    and a Trim Costing linked. Create-once per sample (existing ESGs are left as-is).
    Backs the "Feed ESG Input (bulk)" Sample Request list-view action.

    names   : optional JSON array / list of Sample Request ids (a list-view
              selection). When omitted, EVERY qualifying sample is processed.
    enqueue : truthy -> run in the background (recommended for large sets) and
              return immediately.

    -> {enqueued, total} when backgrounded, else {total, created, skipped, failed}.
    '''
    if not frappe.has_permission(ESG_DOCTYPE, 'create'):
        raise frappe.PermissionError

    ids = _bulk_sample_ids(names)
    if not ids:
        return {'total': 0, 'created': 0, 'skipped': 0, 'failed': 0}

    if frappe.utils.cint(enqueue):
        frappe.enqueue(
            'prism.api.esg._feed_esg_input_bulk',
            queue='long', timeout=3600, names=ids,
        )
        return {'enqueued': True, 'total': len(ids)}

    return _feed_esg_input_bulk(ids)


def _bulk_sample_ids(names):
    ''' Explicit ids (list-view selection) or every sample with both links set. '''
    if names:
        if isinstance(names, str):
            names = frappe.parse_json(names)
        return [n for n in names if n]
    return frappe.get_all(
        SAMPLE_DOCTYPE,
        filters={'fabric_master': ['is', 'set'], 'trim_costing': ['is', 'set']},
        pluck='name',
    )


def _feed_esg_input_bulk(names):
    ''' Run ensure_esg_for_sample over the ids, tallying results; best-effort per row. '''
    created = skipped = failed = 0
    for i, name in enumerate(names, 1):
        try:
            if esglib.ensure_esg_for_sample(frappe.get_doc(SAMPLE_DOCTYPE, name)):
                created += 1
            else:
                skipped += 1  # already has an ESG, or missing a link
        except Exception:
            failed += 1
            frappe.log_error(frappe.get_traceback(), 'feed_esg_input_bulk')
        if i % 50 == 0:
            frappe.db.commit()
    frappe.db.commit()
    return {'total': len(names), 'created': created, 'skipped': skipped, 'failed': failed}


@frappe.whitelist(methods=['POST'])
def calculate_esg_rating(esg_id=None):
    '''
    Score a Moodboard ESG through the external Apps Script rating engine and write
    the result snapshot (overall_rating / score_percent / category / estimated_cost
    / zero_tolerance_status) back onto the record. Backs the "Calculate ESG" Desk
    button.

    Desk-invoked: standard Frappe session auth (not the JWT path); caller needs
    write permission on Moodboard ESG. Refuses to run unless the Main Body element
    has both consumption > 0 and marker efficiency > 0.

    -> {success: True, rating: {...}}. Errors are surfaced via frappe.throw (shown
    as a Desk popup).
    '''
    if not esg_id:
        frappe.throw('Missing ESG id.', frappe.ValidationError)
    if not frappe.db.exists(ESG_DOCTYPE, esg_id):
        frappe.throw('ESG not found', frappe.DoesNotExistError)
    if not frappe.has_permission(ESG_DOCTYPE, 'write', doc=esg_id):
        raise frappe.PermissionError

    doc = frappe.get_doc(ESG_DOCTYPE, esg_id)
    element = _main_body_element_row(doc)
    if not element:
        frappe.throw('This ESG has no garment element to score.', frappe.ValidationError)

    consumption = _to_float(element.consumption)
    marker = _to_float(element.marker_efficiency)
    if not (consumption and consumption > 0) or not (marker and marker > 0):
        frappe.throw(
            'Set Consumption and Marker Efficiency (both greater than 0) on the '
            'Main Body element before calculating.',
            frappe.ValidationError,
        )

    rating = _run_rating(doc, element)
    frappe.db.commit()
    return {'success': True, 'rating': rating}


@frappe.whitelist(methods=['POST'])
def calculate_esg_rating_bulk(names=None, enqueue=1):
    '''
    Score many Moodboard ESG records through the Apps Script rating engine. The
    engine handles ONE request at a time (~10s each), so this runs strictly
    sequentially in a background job — never parallel. Backs the "Calculate ESG
    (bulk)" list-view action.

    names   : optional JSON array / list of Moodboard ESG ids (a list selection).
              When omitted, every ESG record is processed.
    enqueue : truthy (default) -> run in the background and return immediately. The
              caller gets live progress + a completion summary over realtime
              (events "progress" and "esg_bulk_rating_done").

    Records whose Main Body element lacks consumption/marker (> 0) are skipped.
    -> {enqueued, total} when backgrounded, else the tally.
    '''
    if not frappe.has_permission(ESG_DOCTYPE, 'write'):
        raise frappe.PermissionError

    ids = _bulk_esg_ids(names)
    if not ids:
        return {'total': 0, 'scored': 0, 'skipped': 0, 'failed': 0, 'enqueued': False}

    if frappe.utils.cint(enqueue):
        frappe.enqueue(
            'prism.api.esg._calculate_esg_rating_bulk',
            queue='long', timeout=max(600, len(ids) * 30),
            names=ids, user=frappe.session.user,
        )
        return {'enqueued': True, 'total': len(ids)}

    return _calculate_esg_rating_bulk(ids, user=frappe.session.user)


def _bulk_esg_ids(names):
    ''' Explicit ids (list selection) or every Moodboard ESG. '''
    if names:
        if isinstance(names, str):
            names = frappe.parse_json(names)
        return [n for n in names if n]
    return frappe.get_all(ESG_DOCTYPE, pluck='name')


def _calculate_esg_rating_bulk(names, user=None):
    '''
    Sequentially score each ESG (background job). One engine call at a time. Commits
    per record so partial progress persists. Aborts early if the engine returns
    consecutive failures (it's likely down) rather than grinding through every id.
    '''
    total = len(names)
    scored = skipped = failed = 0
    consecutive_fail = 0
    aborted = False

    for i, name in enumerate(names, 1):
        try:
            result = _score_one_esg(name)
            if result == 'scored':
                scored += 1
                frappe.db.commit()
            else:
                skipped += 1
            consecutive_fail = 0
        except Exception:
            failed += 1
            consecutive_fail += 1
            frappe.db.rollback()
            frappe.log_error(frappe.get_traceback(), 'calculate_esg_rating_bulk')

        if user:
            frappe.publish_progress(
                (i / total) * 100.0,
                title='Calculating ESG ratings',
                description='{0}/{1}'.format(i, total),
            )

        if consecutive_fail >= _BULK_RATING_ABORT_AFTER:
            aborted = True
            break

    result = {'total': total, 'scored': scored, 'skipped': skipped,
              'failed': failed, 'aborted': aborted}
    if user:
        frappe.publish_realtime('esg_bulk_rating_done', result, user=user)
    return result


def _score_one_esg(name):
    '''
    Score a single ESG for the bulk loop. Returns 'scored', or 'skipped' when the
    Main Body element has no usable consumption/marker (> 0). Engine/save errors
    propagate to the caller (counted as failures).
    '''
    doc = frappe.get_doc(ESG_DOCTYPE, name)
    element = _main_body_element_row(doc)
    if not element:
        return 'skipped'
    consumption = _to_float(element.consumption)
    marker = _to_float(element.marker_efficiency)
    if not (consumption and consumption > 0) or not (marker and marker > 0):
        return 'skipped'
    _run_rating(doc, element)
    return 'scored'


# --- ESG rating engine: payload build, call, response apply ---

def _run_rating(doc, element):
    ''' Build the payload, call the engine, apply the snapshot, save. Returns rating. '''
    data = _call_esg_rating_engine(_esg_rating_payload(doc, element))
    rating = _apply_rating(doc, data)
    doc.save(ignore_permissions=True)
    return rating

def _main_body_element_row(doc):
    ''' The Main Body element row (case-insensitive), else the first element. '''
    for row in doc.elements:
        if (row.section_name or '').strip().lower() == 'main body':
            return row
    return doc.elements[0] if doc.elements else None


def _esg_rating_payload(doc, el):
    '''
    Flat Apps Script payload (mirrors the frontend esgRatingPayload). Order-context
    and trim summary come from the record; fabric/yarn/finishing from the Main Body
    element. Consumption is grams -> kg here (the element stores grams/piece).
    '''
    return {
        # Order context / trim summary (record-level)
        'suggestionCriteria': doc.suggestion_criteria or '',
        'customerProfile': doc.customer_profile or '',
        'trim1Count': doc.number_of_trims if doc.number_of_trims is not None else 0,
        'trim2Elastic': doc.elastic or '',
        'trim3Style': doc.trim_style or '',
        # Fabric / yarn / finishing (Main Body element). The element stores
        # consumption in grams/piece; the rating API wants kg/piece (_grams_to_kg).
        'consumption': _grams_to_kg(el.consumption),
        'markerEfficiency': _to_float(el.marker_efficiency) or 0,
        'fabricType': _effective_fabric_type(el) or '',
        'fabricBlend': el.fabric_blend or '',
        'fabricShadeCategory': el.shade_category or '',
        'yarnProcessType': el.yarn_process_type or '',
        'yarnSpecs': el.yarn_specs or '',
        'dyeingType': el.dyeing_type or 'SOFT',
        'yarns': _yarns_payload(el.yarn_composition),
        'finishingMechChem': el.finishing_mech_chem or '',
        'finishingAop': _canonical_aop(el.finishing_aop_digital),
        'finishingProcess': _process_for_rating(el.finishing_process),
        'transportMode': el.transport_mode or '',
        'zeroToleranceFail': 'Yes' if el.zero_tolerance_fail else 'No',
    }


def _call_esg_rating_engine(payload):
    ''' POST the payload to the Apps Script engine; return its `data` dict or throw. '''
    body = json.dumps({'action': 'esg', **payload})
    try:
        resp = requests.post(
            ESG_RATING_ENDPOINT,
            data=body.encode('utf-8'),
            headers={'Content-Type': 'text/plain;charset=utf-8'},
            timeout=30,
        )
        resp.raise_for_status()
        parsed = resp.json()
    except Exception as e:
        frappe.log_error(frappe.get_traceback(), 'ESG rating engine call')
        frappe.throw('Could not reach the ESG rating service: {0}'.format(e))

    if not parsed.get('success'):
        frappe.throw('ESG rating service error: {0}'.format(parsed.get('error') or 'unknown'))
    return parsed.get('data') or {}


def _apply_rating(doc, data):
    '''
    Reduce the engine response onto the record (mirrors the frontend normalizeRating).
    Numeric metrics tolerate "%"/junk; text metrics collapse spreadsheet non-values
    ("#N/A", "#REF!", "") to None. Each metric's `.current` is the live figure.
    '''
    overall = _rating_num(_pick_metric(data.get('currentEsgRating')))
    score = _rating_num(_pick_metric(data.get('currentEsgScorePct')))
    category = _rating_text(_pick_metric(data.get('esgCategory')))
    zero_tolerance = _rating_text(_pick_metric(data.get('zeroToleranceStatus')))
    cost = _rating_num(_pick_metric(data.get('currentCostPerPc')))
    consumption_kg = _rating_num(_pick_metric(data.get('totalConsumptionKgPerPc')))

    # score_percent is an Int field; the engine may send a fractional/"85%" figure.
    score_int = int(round(score)) if score is not None else None

    doc.overall_rating = overall
    doc.score_percent = score_int
    doc.category = category
    doc.zero_tolerance_status = zero_tolerance
    doc.estimated_cost = cost

    return {
        'overallRating': overall,
        'scorePercent': score_int,
        'category': category,
        'zeroToleranceStatus': zero_tolerance,
        'estimatedCost': cost,
        'consumptionKgPerPc': consumption_kg,
    }


def _pick_metric(metric):
    ''' A metric's `.current` when it's a {current, ...} object, else the value itself. '''
    return metric.get('current') if isinstance(metric, dict) else metric


def _rating_num(value):
    ''' Numeric metric: strip all but [0-9.-] (so "85%" -> 85); blanks/"#REF!" -> None. '''
    if value is None or value == '':
        return None
    cleaned = re.sub(r'[^0-9.\-]', '', str(value))
    if cleaned in ('', '-', '.'):
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def _rating_text(value):
    ''' Text metric: trim; spreadsheet non-values (leading "#", "N/A" variants) -> None. '''
    s = value.strip() if isinstance(value, str) else ''
    if not s or _RATING_NA_RE.match(s) or s.startswith('#'):
        return None
    return s


def _grams_to_kg(value):
    '''
    Grams/piece -> kg/piece, rounding to the nearest whole gram FIRST (matches the
    frontend gramsToKg: Math.round(g)/1000) to avoid float noise like 185.01/1000.
    '''
    n = _to_float(value)
    if n is None:
        return 0
    return math.floor(n + 0.5) / 1000


def _effective_fabric_type(el):
    ''' Backend override wins: old_fabric_type if present (not null), else fabric_type. '''
    return el.old_fabric_type if el.old_fabric_type is not None else el.fabric_type


def _yarns_payload(yarn_composition):
    ''' [{name, pct}] from the element's yarn_composition JSON; entries with a name only. '''
    out = []
    for y in _load_json(yarn_composition) or []:
        if isinstance(y, dict) and y.get('name'):
            out.append({'name': y.get('name'), 'pct': _to_float(y.get('pct')) or 0})
    return out


def _canonical_aop(value):
    '''
    Map any-case AOP value to its canonical option, then upper-case as the rating API
    wants (e.g. "digital print" -> "DIGITAL PRINT"). Unknown values -> "".
    '''
    if not value:
        return ''
    s = str(value).strip().lower()
    for option in _AOP_OPTIONS:
        if option.lower() == s:
            return option.upper()
    return ''


def _process_for_rating(value):
    ''' The rating API spells out the Stentor passes; other values pass through. '''
    if value == 'Stentor 1':
        return 'Stentor 1 Pass'
    if value == 'Stentor 2':
        return 'Stentor 2 Pass'
    return value or ''


def _to_float(value):
    try:
        return float(str(value).replace(',', '').strip())
    except (TypeError, ValueError, AttributeError):
        return None


# --- core ---

def _get_esg_for_style(style_id):
    if not style_id:
        frappe.throw('Missing style id.', frappe.ValidationError)
    name = frappe.db.get_value(ESG_DOCTYPE, {'moodboard_style': style_id}, 'name')
    if name:
        return frappe.get_doc(ESG_DOCTYPE, name)
    # Fallback: create on demand if the style exists and is costed.
    if not frappe.db.exists(STYLE_DOCTYPE, style_id):
        frappe.throw('Style not found', frappe.DoesNotExistError)
    esg_name = esglib.ensure_esg_for_style(frappe.get_doc(STYLE_DOCTYPE, style_id))
    if not esg_name:
        frappe.throw('No ESG for this style yet — cost the style first.', frappe.DoesNotExistError)
    return frappe.get_doc(ESG_DOCTYPE, esg_name)


def _apply_parent(doc, fields):
    snake_targets = set(_PARENT_FIELDS.values())
    for key, value in (fields or {}).items():
        target = _PARENT_FIELDS.get(key) or (key if key in snake_targets else None)
        if not target:
            continue
        if target == 'customer' and isinstance(value, dict):
            value = value.get('id')  # accept the {id, name} object back
        doc.set(target, value)


def _apply_elements(doc, elements):
    '''Upsert element rows by `section` (case-insensitive); existing rows not in
    the payload are left as-is (no delete).'''
    snake_targets = set(_ELEMENT_FIELDS.values())
    existing = {_norm(row.section_name): row for row in doc.elements if row.section_name}

    for raw in _as_list(elements):
        if not isinstance(raw, dict):
            continue
        section = raw.get('section') or raw.get('sectionName') or raw.get('section_name')
        key = _norm(section)
        row = existing.get(key) if key else None
        if row is None:
            row = doc.append('elements', {})
            if section:
                row.section_name = section
            if key:
                existing[key] = row

        for wkey, value in raw.items():
            field = _ELEMENT_FIELDS.get(wkey) or (wkey if wkey in snake_targets else None)
            if not field:
                continue
            if field == 'yarn_composition' and not isinstance(value, str):
                value = json.dumps(value)
            row.set(field, value)


# --- serialization ---

def _to_api(doc):
    return {
        'id': doc.name,
        'moodboardStyle': doc.moodboard_style,
        'moodboard': doc.moodboard,
        'customer': _brand_obj(doc.customer),
        'shipCountry': doc.ship_country,
        'productionCity': doc.production_city,
        'customerProfile': doc.customer_profile,
        'suggestionCriteria': doc.suggestion_criteria,
        'numberOfTrims': doc.number_of_trims,
        'elastic': doc.elastic,
        'trimStyle': doc.trim_style,
        'overallRating': doc.overall_rating,
        'scorePercent': doc.score_percent,
        'category': doc.category,
        'estimatedCost': doc.estimated_cost,
        'zeroToleranceStatus': doc.zero_tolerance_status,
        'elements': [_element_to_api(e) for e in doc.elements],
    }


def _element_to_api(e):
    return {
        'section': e.section_name,
        'consumption': e.consumption,
        'markerEfficiency': e.marker_efficiency,
        'fabricType': e.fabric_type,
        'oldFabricType': e.old_fabric_type,
        'fabricBlend': e.fabric_blend,
        'shadeCategory': e.shade_category,
        'gsm': e.gsm,
        'yarnProcessType': e.yarn_process_type,
        'yarnSpecs': e.yarn_specs,
        'dyeingType': e.dyeing_type,
        'transportMode': e.transport_mode,
        'zeroToleranceFail': bool(e.zero_tolerance_fail),
        'finishingMechChem': e.finishing_mech_chem,
        'finishingAopDigital': e.finishing_aop_digital,
        'finishingProcess': e.finishing_process,
        'yarnComposition': _load_json(e.yarn_composition) or [],
        'elementRating': e.element_rating,
    }


@frappe.whitelist(allow_guest=True)
@auth_required
def list_countries():
    '''
    All countries (Frappe's Country master) for the ship_country dropdown, sorted
    alphabetically. -> {total, items:[str]} (each item is the country name, which
    is what the ship_country Link stores).
    '''
    _require_internal()
    rows = frappe.get_all('Country', fields=['name'], order_by='name asc',
                          ignore_permissions=True)
    items = [r['name'] for r in rows]
    return {'total': len(items), 'items': items}


@frappe.whitelist(allow_guest=True)
@auth_required
def list_mech_chem_finishes():
    '''
    Distinct "Mechanical & Chemical Finish" values across Fabric Master — the
    option list backing the ESG element's finishing_mech_chem field (mirrors the
    Fabric Master report grouped by mechanical_chemical_finish). -> {total, items}.
    '''
    _require_internal()
    FM = frappe.qb.DocType('Fabric Master')
    rows = (
        frappe.qb.from_(FM)
        .select(FM.mechanical_chemical_finish)
        .distinct()
        .where(FM.mechanical_chemical_finish.isnotnull())
        .where(FM.mechanical_chemical_finish != '')
        .orderby(FM.mechanical_chemical_finish)
    ).run(as_dict=True)
    items = [
        r['mechanical_chemical_finish'] for r in rows
        if (r.get('mechanical_chemical_finish') or '').strip()
    ]
    return {'total': len(items), 'items': items}


@frappe.whitelist(allow_guest=True)
@auth_required
def list_yarn_blends():
    '''
    Distinct yarn blend / fibre values across Fabric Master Yarn Detail — the
    option list for ESG yarn composition names (mirrors the Fabric Master Yarn
    Detail report grouped by blend). -> {total, items}.
    '''
    _require_internal()
    Yarn = frappe.qb.DocType('Fabric Master Yarn Detail')
    rows = (
        frappe.qb.from_(Yarn)
        .select(Yarn.blend)
        .distinct()
        .where(Yarn.blend.isnotnull())
        .where(Yarn.blend != '')
        .orderby(Yarn.blend)
    ).run(as_dict=True)
    items = [r['blend'] for r in rows if (r.get('blend') or '').strip()]
    return {'total': len(items), 'items': items}


@frappe.whitelist(allow_guest=True)
@auth_required
def list_yarn_process_types():
    '''
    Distinct Yarn Process Type values across Fabric Master Yarn Detail — the option
    list for the ESG element's yarn_process_type field (mirrors the report grouped
    by technology_desc). -> {total, items}.
    '''
    _require_internal()
    Yarn = frappe.qb.DocType('Fabric Master Yarn Detail')
    rows = (
        frappe.qb.from_(Yarn)
        .select(Yarn.technology_desc)
        .distinct()
        .where(Yarn.technology_desc.isnotnull())
        .where(Yarn.technology_desc != '')
        .orderby(Yarn.technology_desc)
    ).run(as_dict=True)
    items = [r['technology_desc'] for r in rows if (r.get('technology_desc') or '').strip()]
    return {'total': len(items), 'items': items}


@frappe.whitelist(allow_guest=True)
@auth_required
def list_yarn_specs():
    '''
    Distinct Yarn Specs values across Fabric Master Yarn Detail — the option list
    for the ESG element's yarn_specs field (mirrors the report grouped by
    quality_desc). -> {total, items}.
    '''
    _require_internal()
    Yarn = frappe.qb.DocType('Fabric Master Yarn Detail')
    rows = (
        frappe.qb.from_(Yarn)
        .select(Yarn.quality_desc)
        .distinct()
        .where(Yarn.quality_desc.isnotnull())
        .where(Yarn.quality_desc != '')
        .orderby(Yarn.quality_desc)
    ).run(as_dict=True)
    items = [r['quality_desc'] for r in rows if (r.get('quality_desc') or '').strip()]
    return {'total': len(items), 'items': items}


@frappe.whitelist(allow_guest=True)
@auth_required
def list_shade_categories():
    '''
    Distinct Shade Category values across Fabric Master — the option list for the
    ESG element's shade_category field (mirrors the Fabric Master report grouped by
    shade_cat). -> {total, items}.
    '''
    _require_internal()
    FM = frappe.qb.DocType('Fabric Master')
    rows = (
        frappe.qb.from_(FM)
        .select(FM.shade_cat)
        .distinct()
        .where(FM.shade_cat.isnotnull())
        .where(FM.shade_cat != '')
        .orderby(FM.shade_cat)
    ).run(as_dict=True)
    items = [r['shade_cat'] for r in rows if (r.get('shade_cat') or '').strip()]
    return {'total': len(items), 'items': items}


# --- helpers ---

def _require_internal():
    if util.get_current_brand():
        frappe.throw('Only internal users can access this resource.', frappe.PermissionError)


def _brand_obj(brand_id):
    ''' A Brand link value as {id, name}; None when unset. '''
    if not brand_id:
        return None
    return {'id': brand_id, 'name': frappe.db.get_value('Brand', brand_id, 'brand') or brand_id}


def _as_list(value):
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except Exception:
            return []
    if isinstance(value, dict):
        return [value]
    return value if isinstance(value, list) else []


def _load_json(value):
    if not value:
        return None
    if isinstance(value, (list, dict)):
        return value
    try:
        return json.loads(value)
    except Exception:
        return None


def _norm(value):
    return value.strip().lower() if isinstance(value, str) else ''
