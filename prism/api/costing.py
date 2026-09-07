import json
import math
from concurrent.futures import ThreadPoolExecutor, as_completed

import frappe
from frappe.query_builder import DocType
from frappe.query_builder.functions import Abs

from prism.auth.authenticator import auth_required
import prism.api.blend_parser as blend_parser
import prism.api.fabric_matcher as fabric_matcher
import prism.api.llm as llm

DOCTYPE_FABRIC_MASTER = 'Fabric Master'
# The five per-kg fabric cost heads, in display order. Per-head "adjusted"
# overrides and remarks (entered on the Fabrics tab of the workbench) are keyed
# by these names.
FABRIC_HEAD_KEYS = ['yarn', 'knitting', 'dyes_and_chemicals', 'mechanical_chemical_finish', 'finishing_charges']

ADDITIONAL_LOSS_PERCENT = 2.00

# Upper bound on fabrics costed concurrently by get_fabric_cost(). Each worker
# holds its own database connection, so this also caps the connections a single
# costing request can take out of the pool.
MAX_PARALLEL_FABRICS = 8


@frappe.whitelist(allow_guest=True)
#@auth_required
def calculate(
    constructions: list,
    blends: list,
    gsm: int,
    print_type: str=None
):
    try:
        #-- RBAC check

        # print_type values check
        if print_type and print_type.lower() not in ['aop', 'digital']:
            return {'status': False, 'error': 'Allowed Values for parameter "print_type" are "aop" or "digital"!'}

        #--- get the matching dyed fabric
        fabric, dnc_cost = _get_matching_fabric(constructions, blends, gsm)
        if not fabric:
            return {'status': False, 'error': 'No matching fabric found!'}
        fabric['cost_per_kg'] = {
            'breakup': {},
            'total_fabric_cost': 0.00
        }
        breakup = fabric['cost_per_kg']['breakup']
        total_cost = 0.00

        # 01. total yarn cost
        breakup['yarn'] = _calculate_yarn_costs(fabric)
        total_cost += breakup['yarn']['cost_per_kg']

        # 02. knitting cost
        breakup['knitting'] = _get_knitting_cost(fabric['construction'])
        total_cost += breakup['knitting']['cost_per_kg']

        # 03. dyes and chemicals cost
        breakup['dyes_and_chemicals'] = _get_dyes_and_chemicals_cost(fabric, dnc_cost, print_type)
        total_cost += breakup['dyes_and_chemicals']['cost_per_kg']

        # 04. mechanical & chemical process cost
        breakup['mechanical_chemical_finish'] = _get_mechanical_chemical_process_cost(fabric['mechanical_chemical_finish'])
        total_cost += breakup['mechanical_chemical_finish']['cost_per_kg']

        # 05. finishing_charges
        breakup['finishing_charges'] = _get_finishing_charges(fabric)
        total_cost += breakup['finishing_charges']['cost_per_kg']

        #--- compute total cost after applying loss percent
        fabric['cost_per_kg']['gross_total_cost'] = round(total_cost, 2)
        final_cost = total_cost * (100 + fabric['loss_percent']) / 100
        fabric['cost_per_kg']['loss_amount'] = round((final_cost - total_cost), 2)
        fabric['cost_per_kg']['total_fabric_cost'] = round(final_cost, 2)

        return {'status': True, 'data': fabric}
    
    except Exception as ex:
        frappe.log_error(frappe.get_traceback(), 'costing.calculate')
        return {'status': False, 'error': str(ex)}

@frappe.whitelist(allow_guest=True)
#@auth_required
def get_fabric_cost(fabric_id):
    '''
    Computes the per-kg cost breakup of one or more dyed fabrics.

    `fabric_id` accepts either a single Fabric Master id, or a list of ids
    (also accepted as a JSON encoded / comma separated string, since query
    params arrive as strings). When more than one id is given the fabrics are
    costed in parallel, one worker thread per fabric.

    Returns, for a single id:        {'status': True, 'data': <fabric>}
    Returns, for two or more ids:    {'status': True, 'data': [<fabric>, ...]}
        - one entry per requested id, in the order requested
        - a fabric that could not be costed comes back as
          {'fabric_id': <id>, 'ERROR': <message>} instead, so that one bad id
          does not fail the whole batch

    A list holding a single id is treated as a single id, i.e. it returns one
    fabric rather than a one-element list.
    '''

    try:
        #-- RBAC check

        fabric_ids = _normalize_fabric_ids(fabric_id)
        if not fabric_ids:
            return {'status': False, 'error': 'No fabric id provided!'}

        if len(fabric_ids) > 1:     # batch
            return {
                'status': True, 
                'data': _compute_fabric_costs(fabric_ids)
            }
        else:
            fabric = _compute_fabric_cost(fabric_ids[0])
            if not fabric:
                return {'status': False, 'error': 'No matching fabric found!'}

            return {
                'status': True, 
                'data': fabric
            }

    except Exception as ex:
        frappe.log_error(frappe.get_traceback(), 'costing.get_fabric_cost')
        return {'status': False, 'error': str(ex)}

def _normalize_fabric_ids(fabric_id):
    ''' Normalizes the `fabric_id` parameter into (list of ids, is_batch).'''

    if isinstance(fabric_id, str):
        value = fabric_id.strip()
        # query params arrive as strings, so a list may reach us JSON encoded
        if value.startswith('['):
            try:
                items = json.loads(value)
            except Exception:
                raise Exception('Malformed fabric id list!')
        else:
            items = [value]

    elif isinstance(fabric_id, (list, tuple, set)):
        items = list(fabric_id)

    else:
        items = [fabric_id]

    fabric_ids = [str(fid).strip() for fid in (items or []) if fid and str(fid).strip()]

    return fabric_ids

def _compute_fabric_costs(fabric_ids: list):
    ''' Costs every fabric in `fabric_ids` and returns the results in the same
    order. Fabrics are costed in parallel — each one makes its own (slow) llm
    calls for the knitting code / mechanical & chemical processes whenever the
    fabric master has no cached value yet.

    Each worker thread gets its own frappe connection, since a frappe database
    connection cannot be shared across threads. '''

    site = getattr(frappe.local, 'site', None)
    sites_path = getattr(frappe.local, 'sites_path', '.')

    # unique ids only — duplicates in the request are costed once and reused
    unique_ids = list(dict.fromkeys(fabric_ids))

    costs = {}
    if site and len(unique_ids) > 1:
        max_workers = min(len(unique_ids), MAX_PARALLEL_FABRICS)
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {
                executor.submit(_compute_fabric_cost_threaded, site, sites_path, fabric_id): fabric_id
                for fabric_id in unique_ids
            }
            for future in as_completed(futures):
                costs[futures[future]] = future.result()
    else:
        # no site bound (e.g. console) or a single fabric: nothing to parallelize
        for fabric_id in unique_ids:
            costs[fabric_id] = _safe_compute_fabric_cost(fabric_id)

    return [costs[fabric_id] for fabric_id in fabric_ids]

def _compute_fabric_cost_threaded(site: str, sites_path: str, fabric_id: str):
    ''' Worker-thread entry point: brings up a frappe connection of its own,
    costs the fabric, and always tears the connection down again. '''

    ret_val = None

    try:
        frappe.init(site=site, sites_path=sites_path)
        frappe.connect()
        ret_val = _safe_compute_fabric_cost(fabric_id)

    except Exception as ex:
        # connection setup failed, so there is no db to log into either
        ret_val = {'fabric_id': fabric_id, 'ERROR': str(ex)}

    finally:
        try:
            frappe.destroy()
        except Exception:
            pass

    return ret_val

def _safe_compute_fabric_cost(fabric_id: str):
    ''' Costs a fabric, turning any failure into an inline error entry so that
    one bad fabric does not fail the rest of the batch. '''

    try:
        fabric = _compute_fabric_cost(fabric_id)
        if not fabric:
            return {'fabric_id': fabric_id, 'ERROR': 'No matching fabric found!'}
        return fabric

    except Exception as ex:
        frappe.log_error(frappe.get_traceback(), 'costing._safe_compute_fabric_cost()')
        return {'fabric_id': fabric_id, 'ERROR': str(ex)}

def _compute_fabric_cost(fabric_id: str):
    ''' Builds the full per-kg cost breakup of a single dyed fabric.
    Returns None when there is no matching fabric. '''

    # print_type values check
    print_type = None
    #if print_type and print_type.lower() not in ['aop', 'digital']:
    #    return {'status': False, 'error': 'Allowed Values for parameter "print_type" are "aop" or "digital"!'}

    #--- get the matching dyed fabric
    fabric = _get_fabric(fabric_id)
    if not fabric:
        return None

    fabric['cost_per_kg'] = {
        'breakup': {},
        'total_fabric_cost': 0.00
    }
    breakup = fabric['cost_per_kg']['breakup']
    total_cost = 0.00

    # 01. total yarn cost
    breakup['yarn'] = _calculate_yarn_costs(fabric)
    total_cost += breakup['yarn']['cost_per_kg']

    # 02. knitting cost
    # Use the knitting code stored on the fabric master if present, else fall
    # back to llm matching and cache the matched code for future calls.
    stored_knitting_code = fabric.get('costing_knitting_code')
    breakup['knitting'] = _get_knitting_cost(fabric['construction'], stored_knitting_code)
    if not stored_knitting_code and breakup['knitting']['code']:
        _store_costing_value(fabric['fabric_id'], 'costing_knitting_code', breakup['knitting']['code'])
        fabric['costing_knitting_code'] = breakup['knitting']['code']
    total_cost += breakup['knitting']['cost_per_kg']

    # 03. dyes and chemicals cost
    dnc_cost = fabric['dnc_cost']
    breakup['dyes_and_chemicals'] = _get_dyes_and_chemicals_cost(fabric, dnc_cost, print_type)
    total_cost += breakup['dyes_and_chemicals']['cost_per_kg']

    # 04. mechanical & chemical process cost
    # Use the processes stored on the fabric master if present, else fall back
    # to llm matching and cache the matched processes for future calls.
    stored_mc_processes = fabric.get('costing_mc_processes')
    breakup['mechanical_chemical_finish'] = _get_mechanical_chemical_process_cost(
        fabric['mechanical_chemical_finish'], stored_mc_processes)
    if not stored_mc_processes:
        matched_mc_processes = _matched_mc_processes(breakup['mechanical_chemical_finish'])
        if matched_mc_processes:
            _store_costing_value(fabric['fabric_id'], 'costing_mc_processes', json.dumps(matched_mc_processes))
            fabric['costing_mc_processes'] = matched_mc_processes
    total_cost += breakup['mechanical_chemical_finish']['cost_per_kg']

    # 05. finishing_charges
    breakup['finishing_charges'] = _get_finishing_charges(fabric)
    total_cost += breakup['finishing_charges']['cost_per_kg']

    #--- compute total cost after applying loss percent
    fabric['cost_per_kg']['gross_total_cost'] = round(total_cost, 2)
    final_cost = total_cost * (100 + fabric['loss_percent']) / 100
    fabric['cost_per_kg']['loss_amount'] = round((final_cost - total_cost), 2)
    fabric['cost_per_kg']['total_fabric_cost'] = round(final_cost, 2)

    return fabric


@frappe.whitelist(allow_guest=True)
@auth_required
def get_garment_cost(
    fabrics: list,
    style: str,
    trim_sections: dict = None,
    print_sections: dict = None,
    embroidery_sections: dict = None,
    sam_input: dict = None,
    final_rollup_input: dict = None,
):
    '''
    Computes the full garment cost stack: fabric (with consumption), trim,
    print, embroidery, SAM, and the final roll-up (base cost + rejection /
    testing / profit buffers).

    Mirrors every user-editable input exposed by the Techpack Costing
    Workbench page and reproduces its calculation logic exactly:
      - Fabrics tab: per-section consumption (kg/piece), an adjustment %, and
        optional per-head "adjusted" cost/kg overrides + remarks.
      - Trims tab:   per-trim unit price / units edits and selection toggles.
      - Final Roll-Up tab: rejection / testing / profit %, target currency and
        its conversion factor.

    Input schema:
    {
        "fabrics": [
            {
                "section": <str>,            // e.g. "Body", "Sleeve"
                "construction": <str>,       // e.g. "SJY", "SJY COMPACT"
                "blend": <str>,              // e.g. "100% COTTON"
                "gsm": <int>,
                "print_type": <str>,         // optional: "aop" | "digital"
                "grams_per_piece": <float>,  // consumption per garment in grams (default 100)
                                             // legacy: "kg_per_piece" (= grams_per_piece / 1000) is still accepted
                "adjustment_percent": <float>,   // optional, applied to the per-kg total
                "adjusted_heads": {              // optional per-head cost/kg overrides
                    "yarn": <float|null>,
                    "knitting": <float|null>,
                    "dyes_and_chemicals": <float|null>,
                    "mechanical_chemical_finish": <float|null>,
                    "finishing_charges": <float|null>
                },
                "head_remarks": {                // optional free-text per head
                    "yarn": <str>, 
                    "knitting": <str>, 
                    ...
                }
            }
        ],
        "style": <str>,                      // trim master lookup key

        "trim_sections": {                   // optional per-trim overrides
            "trims": [
                {
                    "id": <str>,             // Trim Costing Item id (from a prior call)
                    "unit_price": <float>,
                    "units": <float>,
                    "is_selected": <bool>    // only meaningful for grouped trims
                }
            ]
        },

        "print_sections": {
            "order_quantity": <int>,
            "sections": [
                {
                    "position": <str>,
                    "type": <str>,           // must match "Print Type Master".print_type_name
                    "no_of_prints": <int>,
                    "length": <float>,
                    "width": <float>,
                    "coverage_percent": <int>
                }
            ]
        },

        "embroidery_sections": {
            "sections": [
                {
                    "section_name": <str>,                // optional, free text
                    "thread_type": <str>,                  // "Embroidery Thread Master".thread_type
                    "emb_type": <str>,                     // "Embroidery Type Master".emb_type
                    "no_of_stitches": <int>,
                    "no_of_thread_colours": <int>,         // optional, informational only
                    "needle_thread_avg": <float>,
                    "letter_design_for_laser": <int>,      // only used for Applique embroidery types
                    "length": <float>,
                    "width": <float>
                }
            ]
        },

        "sam_input": {
            "sam_minutes": <float>
        },

        "final_rollup_input": {
            "rejection_percent": <float>,    // applied to base cost
            "testing_percent":   <float>,    // applied to base cost
            "profit_percent":    <float>,    // applied to base cost
            "currency_code":     <str>,      // optional, default "INR"
            "conversion_factor": <float>     // optional, INR per 1 unit of currency_code (default 1.0)
        }
    }

    Returns: {
        "fabric":      [ { section, costing { ...,
                            // per-head breakup carries optional adjusted_cost_per_kg + remark,
                            adjustment_percent, effective_base_total,
                            adjusted_total_fabric_cost, grams_per_piece, kg_per_piece, cost_per_piece } } ],
        "trim":        { style, total_trim_cost, trims: [ { ..., cost, is_selected,
                            original_unit_price?, original_units? } ] },
        "print":       { ..., total_print_cost },
        "embroidery":  { ..., total_embroidery_cost },
        "sam":         { sam_minutes, factory_cost_per_minute, sam_cost },
        "final_rollup":{ fabric_cost, trim_cost, print_cost, embroidery_cost, sam_cost,
                         base_cost, rejection_percent, testing_percent, profit_percent,
                         rejection_amount, testing_amount, profit_amount, final_cost,
                         currency_code, conversion_factor, final_cost_in_currency }
    }
    '''
    try:
        #-- RBAC check

        result = {'fabric': []}

        #--- fabric ---
        fabric_total = 0.0
        for fabric in (fabrics or []):
            fabric_id = fabric.get('id')
            if fabric_id:
                cost = get_fabric_cost(fabric_id)
            else:
                cost = calculate(
                    constructions=[fabric.get('construction')],
                    blends=[fabric.get('blend')],
                    gsm=int(fabric.get('gsm') or 0),
                    print_type=fabric.get('print_type'),
                )
            if not cost.get('status'):
                return cost
            fabric_data = cost.get('data') or {}
            # Record the chosen print type on the section (display form), matching
            # the Fabrics-tab radio ("None" | "AOP" | "Digital").
            pt = (fabric.get('print_type') or '').lower()
            fabric_data['print_type'] = {'aop': 'AOP', 'digital': 'Digital'}.get(pt, 'None')
            # Apply the per-section adjustment % + per-head overrides/remarks and
            # derive adjusted_total_fabric_cost / cost_per_piece.
            _apply_fabric_section_adjustments(
                fabric_data,
                adjustment_percent=fabric.get('adjustment_percent'),
                adjusted_heads=fabric.get('adjusted_heads'),
                head_remarks=fabric.get('head_remarks'),
                grams_per_piece=fabric.get('grams_per_piece'),
                kg_per_piece=fabric.get('kg_per_piece'),
            )
            result['fabric'].append({
                'section': fabric.get('section'),
                'costing': fabric_data,
            })
            fabric_total += fabric_data['cost_per_piece']

        #--- trim ---
        trim_overrides = (trim_sections or {}).get('trims') if isinstance(trim_sections, dict) else None
        result['trim'] = get_trim_cost(style, trim_overrides)

        #--- print ---
        result['print'] = get_print_cost(print_sections or {})

        #--- embroidery ---
        result['embroidery'] = get_embroidery_cost(embroidery_sections or {})

        #--- SAM ---
        result['sam'] = get_sam_cost(sam_input or {})

        #--- final roll-up ---
        trim_total = float((result['trim'] or {}).get('total_trim_cost') or 0)
        print_total = float((result['print'] or {}).get('total_print_cost') or 0)
        embroidery_total = float((result['embroidery'] or {}).get('total_embroidery_cost') or 0)
        sam_total = float((result['sam'] or {}).get('sam_cost') or 0)
        result['final_rollup'] = get_final_rollup(
            component_costs={
                'fabric_cost': fabric_total,
                'trim_cost': trim_total,
                'print_cost': print_total,
                'embroidery_cost': embroidery_total,
                'sam_cost': sam_total,
            },
            percentages=final_rollup_input or {},
        )

        return {'status': True, 'data': result}

    except Exception as ex:
        frappe.log_error(frappe.get_traceback(), 'costing.get_garment_cost')
        return {'status': False, 'error': str(ex)}

@frappe.whitelist(allow_guest=True)
@auth_required
def fabrics(search_str: str = None):
    try:
        FabricMaster = frappe.qb.DocType(DOCTYPE_FABRIC_MASTER)

        query = (
            frappe.qb.from_(FabricMaster)
            .select(
                    FabricMaster.name.as_('fabric_id'),
                    FabricMaster.dyed_fabric_material.as_('fabric_code'),
                    FabricMaster.dyed_fabric_material_desc.as_('fabric_description'),
                    FabricMaster.fabric.as_('construction'),
                    FabricMaster.blend,
                    FabricMaster.finish_gsm.as_('gsm'),
                    FabricMaster.shade_cat.as_('shade_category'),
            )
            .distinct()
            .orderby(FabricMaster.dyed_fabric_material)
            .limit(20)
        )

        if search_str:
            query = query.where(FabricMaster.dyed_fabric_material_desc.like(f'%{search_str}%'))

        rows = query.run(as_dict=True)

        return {
            'status': True,
            'data': rows
        }

    except Exception as ex:
        frappe.log_error(frappe.get_traceback(), 'costing.fabrics')
        return {'status': False, 'error': str(ex)}

@frappe.whitelist(allow_guest=True)
@auth_required
def get_matching_fabrics(fabric: dict = None, fabCode: str = None):
    '''
    Accepts a single garment fabric and returns up to 3 of the closest matching
    fabrics in the doctype DOCTYPE_FABRIC_MASTER, ranked best-first.

    The fabric can be given two ways:

    1. Spelled out (unchanged, the original contract):
    ---
    {
        "construction": "Rib",
        "blend": "80% cotton 20% nylon",
        "gsm": 180
    }

    2. By `fabCode` — a Surplus Stock fab code, from which the same three
    values are read (see _fabric_from_fab_code). Passing both is allowed: the
    fab code supplies the base and any key present in `fabric` overrides it,
    so a caller can take a stock fabric but ask for a different weight.

    Ranking is fabric_matcher.match_fabric(): both vocabularies are parsed into
    one fibre-group space and scored numerically (construction 40 / composition
    40 / GSM 20), with Claude re-ranking the shortlist only when the leader is
    not already clear. See prism/api/fabric_matcher.py.

    Returns:
    {
        "status": True,
        "data": [                                 // up to 3 records, closest first
            {
                "fabric_id": ...,
                "fabric_code": ...,
                "fabric_description": ...,
                "construction": ...,
                "blend": ...,
                "gsm": ...,
                "shade_category": ...,
                "score": <float 0-100>,           // added by fabric_matcher
                "method": "Deterministic" | "LLM Assisted",
                "reason": <str>,                  // why this one, in prose
                "breakdown": {construction, composition, gsm, stretch_penalty}
            },
            ...
        ],
        "source": {...}                           // only when fabCode was used:
                                                  // what was read off the stock
                                                  // and which field it came from
    }
    '''
    try:
        #-- RBAC check

        fabric = fabric if isinstance(fabric, dict) else {}

        source = None
        if fabCode and str(fabCode).strip():
            resolved, source = _fabric_from_fab_code(str(fabCode).strip())
            if not resolved:
                return {'status': False,
                        'error': f'No Surplus Stock record found for fab code "{fabCode}".'}
            # Explicit keys win, so the fab code stays a starting point rather
            # than an override of whatever the caller also sent.
            overrides = {k: v for k, v in fabric.items() if v not in (None, '')}
            fabric = {**resolved, **overrides}
            # `source` describes the stock; `effective` describes what was
            # actually matched on. They differ exactly when the caller overrode
            # something, and a caller reading only `source` would otherwise be
            # looking at a gsm the search never used.
            source['overridden'] = sorted(overrides)
            source['effective'] = {k: fabric.get(k) for k in ('construction', 'blend', 'gsm')}

        if not (fabric.get('construction') or fabric.get('blend')):
            return {'status': True, 'data': [], **({'source': source} if source else {})}

        # The ranking itself lives in fabric_matcher, which is also what the
        # Surplus Stock desk buttons call — one matcher, so a fab code cannot be
        # told two different things depending on which door it came in by.
        data = fabric_matcher.match_fabric(fabric, max_matches=3)

        return {'status': True, 'data': data, **({'source': source} if source else {})}

    except Exception as ex:
        frappe.log_error(frappe.get_traceback(), 'costing.get_matching_fabrics')
        return {'status': False, 'error': str(ex)}


@frappe.whitelist(allow_guest=True)
@auth_required
def get_matching_fabrics_by_id(fabricId: str = None, fabricType: str = None):
    '''
    The same answer as get_matching_fabrics(), for a fabric that has already
    been matched — a Surplus Stock lot or a Moodboard Dyed Fabric row, named
    either by its record id or by its code.

    Nothing is scored here. Both doctypes carry a `closest_fabric_master` link
    written by a prior matching run (fabric_matcher / moodboard_fabric_matcher),
    together with the score, method and reason that run produced; this reads
    that back. So it is cheap, it never calls the model, and — unlike
    get_matching_fabrics() — it returns the match this fabric is *recorded as
    having*, including a Manual one a person picked by hand, rather than the
    match the scorer would make for it today.

    `fabricId` accepts four things, tried in that order:

        Surplus Stock          record id  (e.g. "c9a2bma9h1")
        Moodboard Dyed Fabric  record id
        Surplus Stock          fab_code   (e.g. "1600007260")
        Moodboard Dyed Fabric  code       (e.g. "1800006955-A0000-...-NO32")

    Record ids in both doctypes are 10-character hashes, so an id cannot be told
    apart from its doctype by looking at it. They are checked against both
    tables; in the (vanishingly unlikely) event the id exists in both, the call
    fails asking to be told which. `fabricType` — "surplus" or "dyed" — skips
    the guessing and restricts the lookup to one doctype.

    A code is a GROUP, not a row: 47 lots share a fab code and up to 756 rows
    share a dyed fabric code, and a handful of those groups disagree about which
    Fabric Master they point at. The match returned for a code is therefore the
    dominant one across the group — by stocked quantity for Surplus Stock, which
    has a quantity to weigh by, and by row count for Moodboard Dyed Fabric,
    which does not. `source.dissenting_rows` says how many rows were outvoted,
    so a caller can tell a unanimous group from a split one.

    Returns get_matching_fabrics()'s shape exactly — the recorded best match
    first, then the alternates stored alongside it:
    {
        "status": True,
        "data": [                                 // up to 3 records, closest first
            {
                "fabric_id": ...,
                "fabric_code": ...,
                "fabric_description": ...,
                "construction": ...,
                "blend": ...,
                "gsm": ...,
                "shade_category": ...,
                "score": <float 0-100>,
                "method": "Deterministic" | "LLM Assisted" | "Manual",
                "reason": <str>,
                "breakdown": None                 // see below
            },
            ...
        ],
        "source": {...}                           // which record was read, what
                                                  // it was matched on, and when
    }

    `breakdown` is always None. The sub-scores are a working of the scorer and
    are not persisted on either doctype, so there is nothing to read back; the
    key is kept so the record shape does not change between the two endpoints.
    Alternates carry no `reason` for the same reason — only the winner's is
    stored.

    `data` is [] when the record exists but carries no match. The call only
    fails when nothing answers to `fabricId` at all.
    '''
    try:
        #-- RBAC check

        fabric_id = (fabricId or '').strip()
        if not fabric_id:
            return {'status': False, 'error': 'fabricId is required.'}

        kinds = _fabric_ref_kinds(fabricType)
        if not kinds:
            return {'status': False,
                    'error': f'Unknown fabricType "{fabricType}". '
                             f'Expected one of: {", ".join(sorted(FABRIC_REF_SOURCES))}.'}

        # Ids before codes: an id is a primary key and cannot mean two rows,
        # whereas a code always means a group. Checking codes first would let a
        # code that happens to look like an id shadow the record it names.
        hits = []
        for by in ('name', 'code'):
            hits = [(kind, rows) for kind, rows in
                    ((kind, _fabric_ref_rows(kind, fabric_id, by)) for kind in kinds) if rows]
            if hits:
                break

        if not hits:
            # Names the doctypes actually looked in, so a fabricType that ruled
            # the right one out reads as that rather than as a missing record.
            looked_in = ' or '.join(FABRIC_REF_SOURCES[kind]['doctype'] for kind in kinds)
            return {'status': False,
                    'error': f'No {looked_in} record found for "{fabric_id}".'}
        if len(hits) > 1:
            found = ', '.join(FABRIC_REF_SOURCES[kind]['doctype'] for kind, _ in hits)
            return {'status': False,
                    'error': f'"{fabric_id}" exists in more than one doctype ({found}). '
                             f'Pass fabricType to say which is meant.'}

        kind, rows = hits[0]
        return {'status': True, **_stored_match_response(kind, fabric_id, by, rows)}

    except Exception as ex:
        frappe.log_error(frappe.get_traceback(), 'costing.get_matching_fabrics_by_id')
        return {'status': False, 'error': str(ex)}


def get_trim_cost(style: str, overrides: list = None):
    '''
    Returns the trim costing JSON for a style. Trims are read fresh from the
    "Trim Costing" master; the optional `overrides` list lets the caller apply
    the same per-trim edits the workbench Trims tab supports — a unit price, a
    unit count, and a selection toggle — matched by the trim's `id` (as returned
    by a prior call). Mirrors techpack_costing.save_trim_costing: the original
    pre-edit unit price / units are preserved, cost = unit_price * units, and
    the total only sums selected trims.
    '''
    TrimCosting = frappe.qb.DocType('Trim Costing')
    TrimCostingItem = frappe.qb.DocType('Trim Costing Item')

    query = (
        frappe.qb.from_(TrimCosting)
        .inner_join(TrimCostingItem)
            .on(TrimCostingItem.parent == TrimCosting.name)
        .select(
            TrimCostingItem.name.as_('id'),
            TrimCostingItem.trim,
            TrimCostingItem.trim_group,
            TrimCostingItem.price.as_('unit_price'),
            TrimCostingItem.value.as_('units'),
            TrimCosting.pcs_per_carton,
        )
        .where(TrimCosting.style_name == style)
        .orderby(TrimCostingItem.trim_group, order=frappe.qb.desc)
        .orderby(TrimCostingItem.creation, order=frappe.qb.desc)
    )

    rows = query.run(as_dict=True)

    updates_by_id = {}
    if isinstance(overrides, list):
        for u in overrides:
            if isinstance(u, dict) and u.get('id'):
                updates_by_id[u['id']] = u

    tot_cost = 0.00
    for row in (rows or []):
        row['is_selected'] = True
        update = updates_by_id.get(row.get('id'))
        if update:
            current_unit_price = _to_float(row.get('unit_price'))
            current_units = _to_float(row.get('units'))
            if 'unit_price' in update:
                new_unit_price = round(_to_float(update.get('unit_price')), 4)
                if abs(new_unit_price - current_unit_price) > 1e-9:
                    row['original_unit_price'] = round(current_unit_price, 4)
                    row['unit_price'] = new_unit_price
            if 'units' in update:
                new_units = round(_to_float(update.get('units')), 4)
                if abs(new_units - current_units) > 1e-9:
                    row['original_units'] = round(current_units, 4)
                    row['units'] = new_units
            if 'is_selected' in update:
                row['is_selected'] = bool(update.get('is_selected'))

        row['cost'] = round(_to_float(row.get('unit_price')) * _to_float(row.get('units')), 4)
        if row['is_selected'] is not False:
            tot_cost += row['cost']

    return {
        'style': style,
        'total_trim_cost': round(tot_cost, 2),
        'trims': rows
    }


# --- helpers ---
SURPLUS_STOCK_DOCTYPE = 'Surplus Stock'


def _fabric_from_fab_code(fab_code: str):
    '''
    A Surplus Stock fab code -> the {construction, blend, gsm} the matcher above
    already understands, plus a `source` block recording where each value came
    from. Returns (None, None) when the code has no stock rows.

    A fab code is a group, not a row — up to 47 rows share one code here, and a
    handful disagree on blend — so the three values are the quantity-dominant
    ones across the group, via surplus_recommender._build_group(). That is the
    same rule the surplus recommender and catalogue report, so a fab code reads
    identically wherever it is quoted.

    Each field falls back until something is set:

        construction  quality_full_name -> decoded quality -> raw quality
        blend         blend_full_name   -> decoded blend   -> raw blend
        gsm           gsm column        -> gsm encoded in material_desc

    The middle step is the useful one. `quality_full_name` and `blend_full_name`
    are empty on every row on this site, so without it the matcher would be
    handed mill shorthand ("SJY_EL", "95:5 BCI:EL") to match against a Fabric
    Master catalog written in plain language ("Single Jersey", "95% Cotton 5%
    Spandex"). The decoders that already turn one into the other live in
    surplus_recommender, so they are reused rather than re-derived. The raw
    codes still ride along in the returned dict as `construction_code` /
    `blend_code`, since the prompt serialises the whole fabric object and the
    shorthand is extra evidence for the model, not noise.
    '''
    import prism.api.surplus_recommender as surplus_recommender

    lots = frappe.get_all(
        SURPLUS_STOCK_DOCTYPE,
        filters={'fab_code': fab_code},
        fields=[
            'name', 'fab_code', 'material', 'material_type_desc', 'material_desc',
            'batch', 'quality', 'quality_full_name', 'blend', 'blend_full_name',
            'gsm', 'color', 'shade_catagory', 'fabric_type', 'total_qty',
            'total_value_rs_lakh', 'base_uom', 'width', 'dia', 'gauge', 'ageing',
            'customer_name', 'stock_segment', 'storage_location', 'has_image_file',
            'image_urls',
        ],
        limit_page_length=0,
        ignore_permissions=True,
    )
    if not lots:
        return None, None

    group = surplus_recommender._build_group(fab_code, lots)

    quality_full = _dominant_text(lots, 'quality_full_name')
    blend_full = _dominant_text(lots, 'blend_full_name')

    construction = quality_full or group.get('quality_label') or group.get('quality')
    blend = blend_full or group.get('composition_label') or group.get('composition')
    gsm = _to_float(group.get('gsm'))

    fabric = {
        'construction': construction,
        'blend': blend,
        'gsm': gsm,
    }
    # Only worth sending when they say something the decoded values do not.
    if group.get('quality') and group['quality'] != construction:
        fabric['construction_code'] = group['quality']
    if group.get('composition') and group['composition'] != blend:
        fabric['blend_code'] = group['composition']

    source = {
        'fab_code': fab_code,
        'lots': len(lots),
        'construction': construction,
        'construction_from': ('quality_full_name' if quality_full
                              else ('quality_decoded' if group.get('quality_label') else 'quality')),
        'blend': blend,
        'blend_from': ('blend_full_name' if blend_full
                       else ('blend_decoded' if group.get('composition_label') else 'blend')),
        'gsm': gsm,
        'quality': group.get('quality'),
        'raw_blend': group.get('composition'),
        'material_type': group.get('material_type'),
        'available': group.get('available'),
        'available_uom': group.get('available_uom'),
    }
    return fabric, source


def _dominant_text(lots, field):
    ''' The quantity-dominant non-blank value of `field` across a fab code's
    lots, or None. Mirrors surplus_recommender._dominant, which ignores blanks
    but is not exported for arbitrary text fields. '''
    weights = {}
    for lot in lots:
        value = (lot.get(field) or '').strip()
        if value:
            weights[value] = weights.get(value, 0.0) + _to_float(lot.get('total_qty'))
    if not weights:
        return None
    return max(weights.items(), key=lambda kv: kv[1])[0]


# --- helpers: stored closest-fabric-master lookup ---
MOODBOARD_DYED_FABRIC_DOCTYPE = 'Moodboard Dyed Fabric'

# The match columns. Both doctypes carry the same set under the same names --
# that is what lets one reader serve them, rather than one per doctype.
CLOSEST_MATCH_FIELDS = (
    'closest_fabric_master', 'closest_fabric_code', 'closest_fabric_desc',
    'closest_match_score', 'closest_match_method', 'closest_match_reason',
    'closest_match_alternates', 'closest_matched_on',
    'closest_fabric_cost_per_kg', 'closest_fabric_costed_on',
)

# What differs between the two, and only that.
#
# `describe` names the columns whose values are reported back as what the match
# was made on. They are read through each matcher's own row -> fabric mapper, so
# the answer is the mapper's, not a second guess at it: Surplus Stock resolves
# quality_full_name -> quality and recovers a missing GSM out of material_desc,
# Moodboard Dyed Fabric reads the clean_* columns and takes GSM as it stands.
#
# `weight` is the column a group vote is weighed by. Surplus Stock lots carry a
# quantity, so the fabric most of the stock points at wins. Moodboard Dyed
# Fabric rows have no quantity, so None means one row one vote.
FABRIC_REF_SOURCES = {
    'surplus': {
        'doctype': SURPLUS_STOCK_DOCTYPE,
        'code_field': 'fab_code',
        'weight': 'total_qty',
        'describe': ('quality', 'quality_full_name', 'blend', 'blend_full_name',
                     'gsm', 'material_desc'),
    },
    'dyed': {
        'doctype': MOODBOARD_DYED_FABRIC_DOCTYPE,
        'code_field': 'code',
        'weight': None,
        'describe': ('quality', 'clean_quality', 'blend', 'clean_blend', 'gsm'),
    },
}


def _fabric_ref_kinds(fabric_type):
    ''' The source keys a lookup may consider. None/blank means both. '''
    if not (fabric_type and str(fabric_type).strip()):
        return list(FABRIC_REF_SOURCES)
    kind = str(fabric_type).strip().lower()
    return [kind] if kind in FABRIC_REF_SOURCES else []


def _fabric_ref_rows(kind, value, by):
    ''' Every row of one doctype answering to `value`, by 'name' or by 'code'. '''
    config = FABRIC_REF_SOURCES[kind]
    field = 'name' if by == 'name' else config['code_field']
    fields = ['name', config['code_field'], *config['describe'], *CLOSEST_MATCH_FIELDS]
    if config['weight']:
        fields.append(config['weight'])

    return frappe.get_all(
        config['doctype'],
        filters={field: value},
        fields=list(dict.fromkeys(fields)),
        limit_page_length=0,
        ignore_permissions=True,
    )


def _stored_match_response(kind, value, by, rows):
    ''' The {data, source} half of get_matching_fabrics_by_id(). '''
    config = FABRIC_REF_SOURCES[kind]
    winner, agreeing = _dominant_stored_match(rows, config['weight'])

    source = {
        'type': kind,
        'doctype': config['doctype'],
        'matched_by': by,
        'id': value,
        'code': (winner or rows[0]).get(config['code_field']),
        'record': winner['name'] if winner else None,
        'rows': len(rows),
        'agreeing_rows': agreeing,
        'dissenting_rows': len([r for r in rows if r.get('closest_fabric_master')]) - agreeing,
        # Read back rather than rescored: this is the match on record, which for
        # a Manual one is not what the scorer would say today.
        'stored': True,
    }

    if not winner:
        source.update({'construction': None, 'blend': None, 'gsm': None,
                       'method': None, 'score': 0, 'matched_on': None})
        return {'data': [], 'source': source}

    matched_on = _fabric_ref_matched_on(kind, winner)
    source.update({
        'construction': matched_on.get('construction'),
        'blend': matched_on.get('blend'),
        'gsm': matched_on.get('gsm'),
        'method': winner.get('closest_match_method'),
        'score': _to_float(winner.get('closest_match_score')),
        'matched_on': winner.get('closest_matched_on'),
        'cost_per_kg': _to_float(winner.get('closest_fabric_cost_per_kg')),
        'costed_on': winner.get('closest_fabric_costed_on'),
    })

    return {'data': _stored_match_records(winner), 'source': source}


def _fabric_ref_matched_on(kind, row):
    '''
    The {construction, blend, gsm} the stored match was made on, through the
    matcher that made it. Each matcher owns which of its columns feed a match
    and how they fall back, so asking it is the only way this cannot drift out
    of step with what was actually scored.
    '''
    if kind == 'surplus':
        return fabric_matcher._fabric_from_stock_row(row)

    import prism.api.moodboard_fabric_matcher as moodboard_fabric_matcher
    return moodboard_fabric_matcher._fabric_from_row(row)


def _dominant_stored_match(rows, weight_field):
    '''
    The Fabric Master a group of rows points at, and how many of them do.

    A record id gives one row and the vote is trivial. A code gives a group, and
    a few groups are split -- so the winner is the fabric carrying the most
    weight (stocked quantity where there is one, rows otherwise), and the row
    returned is the best-scoring of the ones pointing at it. Ties fall back to
    row count and then to the id, so the same code always answers the same way.

    Returns (row, agreeing row count), or (None, 0) when nothing is matched.
    '''
    matched = [r for r in rows if r.get('closest_fabric_master')]
    if not matched:
        return None, 0

    tally = {}
    for row in matched:
        fabric_id = row['closest_fabric_master']
        weight, count = tally.get(fabric_id, (0.0, 0))
        tally[fabric_id] = (weight + (_to_float(row.get(weight_field)) if weight_field else 1.0),
                            count + 1)

    winning_id = max(tally.items(), key=lambda kv: (kv[1][0], kv[1][1], kv[0]))[0]
    agreeing = [r for r in matched if r['closest_fabric_master'] == winning_id]

    return max(agreeing, key=lambda r: (_to_float(r.get('closest_match_score')), r['name'])), len(agreeing)


def _stored_match_records(row):
    '''
    One matched row -> the match records get_matching_fabrics() would return:
    the stored best first, then the alternates stored beside it.

    The catalogue columns are re-read from Fabric Master rather than taken from
    the stored `closest_fabric_code` / `closest_fabric_desc`, so a record that
    has since been edited reads as it is now. Those two stored columns are the
    fallback for a link left dangling by a deleted Fabric Master -- the match
    still describes something, and dropping it silently would look like an
    unmatched row.
    '''
    alternates = _stored_match_alternates(row)
    wanted = [row['closest_fabric_master']] + [a['fabric_id'] for a in alternates]
    catalogue = _fabric_master_records(wanted)

    best = catalogue.get(row['closest_fabric_master']) or {
        'fabric_id': row['closest_fabric_master'],
        'fabric_code': row.get('closest_fabric_code'),
        'fabric_description': row.get('closest_fabric_desc'),
        'construction': None, 'blend': None, 'gsm': None, 'shade_category': None,
    }

    method = row.get('closest_match_method')
    records = [{
        **best,
        'score': _to_float(row.get('closest_match_score')),
        'method': method,
        'reason': row.get('closest_match_reason'),
        # Not persisted by either matcher; kept so the shape does not change
        # between this endpoint and get_matching_fabrics().
        'breakdown': None,
    }]

    seen = {row['closest_fabric_master']}
    for alternate in alternates:
        record = catalogue.get(alternate['fabric_id'])
        if not record or alternate['fabric_id'] in seen:
            continue
        seen.add(alternate['fabric_id'])
        records.append({
            **record,
            'score': _to_float(alternate.get('score')),
            # The same run produced these, so they were reached the same way.
            # Only the winner's reason is stored.
            'method': method,
            'reason': None,
            'breakdown': None,
        })

    return records


def _stored_match_alternates(row):
    ''' The runners-up stored on a row, as [{fabric_id, score}, ...]. '''
    raw = row.get('closest_match_alternates')
    if not raw:
        return []
    try:
        parsed = json.loads(raw) if isinstance(raw, str) else raw
    except ValueError:
        return []
    return [a for a in (parsed or []) if isinstance(a, dict) and a.get('fabric_id')]


def _fabric_master_records(fabric_ids):
    '''
    Fabric Master rows by id, selecting exactly the columns
    fabric_matcher._fabric_master_row() does so a record from either endpoint
    carries the same keys.
    '''
    fabric_ids = [f for f in dict.fromkeys(fabric_ids) if f]
    if not fabric_ids:
        return {}

    FabricMaster = frappe.qb.DocType(DOCTYPE_FABRIC_MASTER)
    rows = (
        frappe.qb.from_(FabricMaster)
        .select(
            FabricMaster.name.as_('fabric_id'),
            FabricMaster.dyed_fabric_material.as_('fabric_code'),
            FabricMaster.dyed_fabric_material_desc.as_('fabric_description'),
            FabricMaster.fabric.as_('construction'),
            FabricMaster.blend,
            FabricMaster.finish_gsm.as_('gsm'),
            FabricMaster.shade_cat.as_('shade_category'),
        )
        .where(FabricMaster.name.isin(fabric_ids))
    ).run(as_dict=True)

    return {r['fabric_id']: r for r in rows}


def _apply_fabric_section_adjustments(data: dict, adjustment_percent=None, adjusted_heads=None,
                                      head_remarks=None, grams_per_piece=None, kg_per_piece=None):
    '''
    Applies the Fabrics-tab user inputs to one fabric section's costing dict in
    place and derives the adjusted totals. Reproduces the logic in
    techpack_costing.save_section_adjustments exactly:
      - per-head `adjusted_cost_per_kg` overrides and `remark`s are written into
        the cost_per_kg breakup,
      - the effective per-kg base uses the adjusted heads (re-applying loss %)
        when any override is set, otherwise the system total_fabric_cost,
      - adjustment % scales that base into adjusted_total_fabric_cost,
      - consumption is captured in grams (default 100 g); the internal kg value
        is grams / 1000, and
      - cost_per_piece = adjusted_total_fabric_cost * grams_per_piece / 1000.

    Callers may pass either `grams_per_piece` (preferred) or the legacy
    `kg_per_piece`; the latter is auto-converted (×1000).
    '''
    cpk = data.get('cost_per_kg') or {}
    breakup = cpk.get('breakup') or {}

    # per-head adjusted cost/kg overrides (set / unset)
    if isinstance(adjusted_heads, dict):
        for key in FABRIC_HEAD_KEYS:
            head = breakup.get(key) or {}
            if key in adjusted_heads:
                v = adjusted_heads.get(key)
                if v in (None, ''):
                    head.pop('adjusted_cost_per_kg', None)
                else:
                    head['adjusted_cost_per_kg'] = round(_to_float(v), 4)
            breakup[key] = head

    # per-head remarks (set / unset)
    if isinstance(head_remarks, dict):
        for key in FABRIC_HEAD_KEYS:
            head = breakup.get(key) or {}
            if key in head_remarks:
                v = head_remarks.get(key)
                s = str(v).strip() if v is not None else ''
                if s == '':
                    head.pop('remark', None)
                else:
                    head['remark'] = s
            breakup[key] = head

    cpk['breakup'] = breakup
    data['cost_per_kg'] = cpk

    # effective per-kg base — uses adjusted heads (with loss re-applied) when set
    system_total = _to_float(cpk.get('total_fabric_cost'))
    loss_pct = _to_float(data.get('loss_percent'))
    has_any_adjusted = any(
        'adjusted_cost_per_kg' in (breakup.get(k) or {}) for k in FABRIC_HEAD_KEYS
    )
    if has_any_adjusted:
        adj_gross = 0.0
        for k in FABRIC_HEAD_KEYS:
            head = breakup.get(k) or {}
            if 'adjusted_cost_per_kg' in head:
                adj_gross += _to_float(head.get('adjusted_cost_per_kg'))
            else:
                adj_gross += _to_float(head.get('cost_per_kg'))
        effective_base = adj_gross * (1 + loss_pct / 100.0)
    else:
        effective_base = system_total

    adjustment_percent = round(_to_float(adjustment_percent), 4)

    # Consumption: prefer grams_per_piece; fall back to kg_per_piece (legacy);
    # default to 100 g (= 0.1 kg) when both are blank/invalid.
    grams = _to_float(grams_per_piece)
    if grams <= 0:
        kg_fallback = _to_float(kg_per_piece)
        if kg_fallback > 0:
            grams = kg_fallback * 1000.0
    if grams <= 0:
        grams = 100.0
    grams = round(grams, 4)
    kg = round(grams / 1000.0, 5)

    adjusted_total = round(effective_base * (1 + adjustment_percent / 100), 2)
    data['adjustment_percent'] = adjustment_percent
    data['effective_base_total'] = round(effective_base, 2)
    data['adjusted_total_fabric_cost'] = adjusted_total
    data['grams_per_piece'] = grams
    data['kg_per_piece'] = kg
    data['cost_per_piece'] = round(adjusted_total * kg, 2)

    return data

def get_print_cost(print_sections: dict):
    '''
    Returns the costing JSON for the print sections of a garment.
    The schema of the 'print_sections' parameter is as follows,
    {
        "order_quantity": <int>,
        "sections": [
            {
                "position": <str>,
                "type": <str>,
                "no_of_prints": <int>,
                "length": <float>,
                "width": <float>,
                "coverage_percent": <int>
            }
        ]
    }
    Fetches the per-type rate card from "Print Type Master" and the global
    buffers / defaults from the single "Print Costing Rules" doctype.
    Returns the same structure as saved in the 'print_costing' field of
    "Techpack Costing".
    '''
    payload = print_sections if isinstance(print_sections, dict) else {}

    def _to_float(v):
        if v in (None, ''):
            return 0.0
        try:
            return float(v)
        except Exception:
            return 0.0

    # --- global rules (with fallbacks if doctype/field is missing or blank) ---
    extra_percent, rejection_percent = 5.0, 10.0
    mesh_cost_per_screen, default_coverage, default_order_quantity = 750, 70, 1500
    try:
        rules = frappe.db.get_singles_dict('Print Costing Rules') or {}
        if rules:
            extra_percent = _to_float(rules.get('default_extra_percentage')) or extra_percent
            rejection_percent = _to_float(rules.get('default_rejection_percentage')) or rejection_percent
            mesh_cost_per_screen = _to_float(rules.get('default_mesh_cost_per_screen')) or mesh_cost_per_screen
            default_coverage = _to_float(rules.get('default_coverage_percentage')) or default_coverage
            default_order_quantity = int(_to_float(rules.get('default_order_quantity')) or default_order_quantity)
    except Exception:
        pass

    # --- print type rate card from "Print Type Master" ---
    rate_card = {}
    try:
        if frappe.db.exists('DocType', 'Print Type Master'):
            rows = frappe.get_all(
                'Print Type Master',
                fields=['print_type_name', 'cost_per_inch', 'manpower_cost'],
                ignore_permissions=True,
            )
            for r in rows:
                if r.get('print_type_name'):
                    rate_card[r['print_type_name']] = {
                        'cost_per_inch': _to_float(r.get('cost_per_inch')),
                        'manpower_cost': _to_float(r.get('manpower_cost')),
                    }
    except Exception:
        rate_card = {}

    # --- order quantity ---
    order_quantity = int(_to_float(payload.get('order_quantity')) or default_order_quantity)
    if order_quantity < 1:
        order_quantity = default_order_quantity

    # --- per section ---
    out_prints = []
    grand_total = 0
    for item in payload.get('sections') or []:
        if not isinstance(item, dict):
            continue

        pt = item.get('type') or ''
        rate = rate_card.get(pt) or {}
        no_of_prints = _to_float(item.get('no_of_prints'))
        length = _to_float(item.get('length'))
        width = _to_float(item.get('width'))
        coverage_raw = item.get('coverage_percent')
        coverage = _to_float(coverage_raw) if coverage_raw not in (None, '') else default_coverage

        cost_per_inch = _to_float(rate.get('cost_per_inch'))
        manpower_cost = _to_float(rate.get('manpower_cost'))

        area = length * width
        ink_cost = cost_per_inch * area * (no_of_prints + 1) * (coverage / 100.0)
        mesh_cost_per_garment = (no_of_prints * mesh_cost_per_screen) / order_quantity if order_quantity else 0
        material_cost = (ink_cost + mesh_cost_per_garment) * (1 + extra_percent / 100.0)
        gross_total_cost = material_cost + manpower_cost
        rejection_amount = gross_total_cost * (rejection_percent / 100.0)
        
        final_cost = int(math.ceil(gross_total_cost + rejection_amount)) if pt else 0

        out_prints.append({
            'id': frappe.generate_hash(length=10),
            'print_position': item.get('position') or '',
            'print_type': pt,
            'no_of_prints': no_of_prints,
            'length': length,
            'width': width,
            'coverage': coverage,
            'cost_per_inch': cost_per_inch,
            'manpower_cost': manpower_cost,
            'area': round(area, 4),
            'ink_cost': round(ink_cost, 4),
            'mesh_cost_per_garment': round(mesh_cost_per_garment, 4),
            'material_cost': round(material_cost, 4),
            'gross_total_cost': round(gross_total_cost, 4),
            'rejection_amount': round(rejection_amount, 4),
            'final_cost': final_cost,
        })
        grand_total += final_cost

    return {
        'order_quantity': order_quantity,
        'mesh_cost_per_screen': mesh_cost_per_screen,
        'default_coverage_percent': default_coverage,
        'extra_percent': extra_percent,
        'rejection_percent': rejection_percent,
        'prints': out_prints,
        'total_print_cost': grand_total,
    }

def get_embroidery_cost(embroidery_sections: dict):
    '''
    Returns the costing JSON for the embroidery sections of a garment.
    Input schema:
    {
        "sections": [
            {
                "section_name": <str>,                // optional, free text
                "thread_type": <str>,                  // must match "Embroidery Thread Master".thread_type
                "emb_type": <str>,                     // must match "Embroidery Type Master".emb_type
                "no_of_stitches": <int>,
                "no_of_thread_colours": <int>,         // optional, informational
                "needle_thread_avg": <float>,
                "letter_design_for_laser": <int>,      // used only for Applique types
                "length": <float>,
                "width": <float>
            }
        ]
    }
    Fetches the rate card from "Embroidery Thread Master", the type list from
    "Embroidery Type Master", the per-stitch-range rate from
    "Embroidery Cost Per 1000ST Master", and the global buffers from the
    single "Embroidery Costing Rules" doctype.
    Returns the same structure as saved in the 'embroidery_cost' field of
    "Techpack Costing".
    '''
    payload = embroidery_sections if isinstance(embroidery_sections, dict) else {}

    def to_float(v):
        if v in (None, ''):
            return 0.0
        try:
            return float(v)
        except Exception:
            return 0.0

    # --- global rules (fallbacks per spec) ---
    canvas_cost_per_sq_inch = 0.0004
    canvas_layers = 3
    manpower_cost = 4.0
    overhead_percent = 5.0
    rejection_percent = 10.0
    laser_cost_per_letter = 2.0
    try:
        values = frappe.db.get_singles_dict('Embroidery Costing Rules') or {}
        canvas_cost_per_sq_inch = to_float(values.get('canvas_cost_per_sq_inch')) or canvas_cost_per_sq_inch
        canvas_layers = int(to_float(values.get('canvas_layers')) or canvas_layers)
        manpower_cost = to_float(values.get('manpower_cost')) or manpower_cost
        overhead_percent = to_float(values.get('overhead_percent')) or overhead_percent
        rejection_percent = to_float(values.get('rejection_percent')) or rejection_percent
        laser_cost_per_letter = to_float(values.get('laser_cost_per_letter')) or laser_cost_per_letter
    except Exception:
        pass

    # --- thread rate card ---
    threads_by_type = {}
    try:
        if frappe.db.exists('DocType', 'Embroidery Thread Master'):
            rows = frappe.get_all(
                'Embroidery Thread Master',
                fields=['thread_type', 'cost', 'mtr', 'cost_per_mtr'],
                ignore_permissions=True,
            )
            for r in rows:
                if r.get('thread_type'):
                    threads_by_type[r['thread_type']] = {
                        'cost_per_mtr': to_float(r.get('cost_per_mtr')),
                    }
    except Exception:
        threads_by_type = {}

    # --- per-1000ST rate lookup ---
    stitch_rates = []
    try:
        if frappe.db.exists('DocType', 'Embroidery Cost Per 1000ST Master'):
            rows = frappe.get_all(
                'Embroidery Cost Per 1000ST Master',
                fields=['from_stitches', 'to_stitches', 'rate'],
                order_by='from_stitches asc',
                ignore_permissions=True,
            )
            stitch_rates = [
                {
                    'from_stitches': to_float(r.get('from_stitches')),
                    'to_stitches': to_float(r.get('to_stitches')) if r.get('to_stitches') not in (None, '') else None,
                    'rate': to_float(r.get('rate')),
                }
                for r in rows
            ]
    except Exception:
        stitch_rates = []

    def lookup_stitch_rate(stitches):
        n = to_float(stitches)
        for row in stitch_rates:
            frm = to_float(row.get('from_stitches'))
            to = row.get('to_stitches')
            if to is None:
                if n >= frm:
                    return to_float(row.get('rate'))
            else:
                if frm <= n <= to_float(to):
                    return to_float(row.get('rate'))
        return 0.0

    out_sections = []
    grand_total = 0.0
    for item in payload.get('sections') or []:
        if not isinstance(item, dict):
            continue

        tt = item.get('thread_type') or ''
        et = item.get('emb_type') or ''
        thread = threads_by_type.get(tt) or {}

        no_of_stitches = to_float(item.get('no_of_stitches'))
        needle = to_float(item.get('needle_thread_avg'))
        no_of_thread_colours = to_float(item.get('no_of_thread_colours'))
        letters = to_float(item.get('letter_design_for_laser'))
        length = to_float(item.get('length'))
        width = to_float(item.get('width'))

        cost_per_mtr = to_float(thread.get('cost_per_mtr'))
        bobbin = needle / 3.0 if needle else 0.0
        thread_cost = (needle + bobbin) * cost_per_mtr
        stitch_rate = lookup_stitch_rate(no_of_stitches)
        cost_per_1000st = (no_of_stitches / 1000.0) * stitch_rate
        area = length * width
        canvas_cost = area * canvas_cost_per_sq_inch * canvas_layers
        is_applique = 'applique' in et.lower()
        laser_cost = (laser_cost_per_letter * letters) if is_applique else 0.0
        cost = thread_cost + cost_per_1000st + canvas_cost + manpower_cost + laser_cost
        overhead_amount = cost * (overhead_percent / 100.0)
        rejection_amount = (cost + overhead_amount) * (rejection_percent / 100.0)
        final_cost = cost + overhead_amount + rejection_amount

        out_sections.append({
            'id': frappe.generate_hash(length=10),
            'section_name': item.get('section_name') or '',
            'thread_type': tt,
            'emb_type': et,
            'no_of_stitches': no_of_stitches,
            'no_of_thread_colours': no_of_thread_colours,
            'needle_thread_avg': needle,
            'letter_design_for_laser': letters,
            'length': length,
            'width': width,
            'is_applique': is_applique,
            'cost_per_mtr': cost_per_mtr,
            'bobbin_thread_avg': round(bobbin, 4),
            'thread_cost': round(thread_cost, 4),
            'stitch_rate': stitch_rate,
            'cost_per_1000st': round(cost_per_1000st, 4),
            'area': round(area, 4),
            'canvas_cost': round(canvas_cost, 4),
            'laser_cost': round(laser_cost, 4),
            'manpower_cost': manpower_cost,
            'cost': round(cost, 4),
            'overhead_amount': round(overhead_amount, 4),
            'rejection_amount': round(rejection_amount, 4),
            'final_cost': round(final_cost, 2),
        })
        grand_total += final_cost

    return {
        'canvas_cost_per_sq_inch': canvas_cost_per_sq_inch,
        'canvas_layers': canvas_layers,
        'manpower_cost': manpower_cost,
        'overhead_percent': overhead_percent,
        'rejection_percent': rejection_percent,
        'laser_cost_per_letter': laser_cost_per_letter,
        'sections': out_sections,
        'total_embroidery_cost': round(grand_total, 2),
    }

def get_sam_cost(sam_input: dict):
    '''
    Returns the SAM costing JSON for a garment.
    Input schema:
    {
        "sam_minutes": <float>
    }
    Reads factory_cost_per_minute from the single "SAM Costing Rules" doctype
    (falls back to 13 if missing). Returns the same structure as saved in the
    'sam_cost' field of "Techpack Costing".
    '''
    payload = sam_input if isinstance(sam_input, dict) else {}

    def to_float(v):
        if v in (None, ''):
            return 0.0
        try:
            return float(v)
        except Exception:
            return 0.0

    factory_cost = 13.0
    try:
        values = frappe.db.get_singles_dict('SAM Costing Rules') or {}
        factory_cost = to_float(values.get('factory_cost_per_minute')) or factory_cost
    except Exception:
        pass

    sam_minutes = to_float(payload.get('sam_minutes'))
    sam_cost = round(sam_minutes * factory_cost, 2)
    return {
        'sam_minutes': round(sam_minutes, 4),
        'factory_cost_per_minute': factory_cost,
        'sam_cost': sam_cost,
    }

def get_final_rollup(component_costs: dict, percentages: dict):
    '''
    Returns the final roll-up JSON applying rejection / testing / profit
    buffers to the base cost (sum of all component costs).

    component_costs schema:
    {
        "fabric_cost":     <float>,   // sum of cost_per_piece across fabric sections
        "trim_cost":       <float>,   // total_trim_cost
        "print_cost":      <float>,   // total_print_cost
        "embroidery_cost": <float>,   // total_embroidery_cost
        "sam_cost":        <float>    // SAM cost per piece
    }

    percentages schema:
    {
        "rejection_percent": <float>,
        "testing_percent":   <float>,
        "profit_percent":    <float>,
        "currency_code":     <str>,    // optional, default "INR"
        "conversion_factor": <float>   // optional, INR per 1 unit of currency_code (default 1.0)
    }

    Returns the same structure as saved in the 'final_rollup' field of
    "Techpack Costing", including the target currency and the final cost
    converted into it (final_cost_in_currency = final_cost / conversion_factor).
    '''
    def to_float(v):
        if v in (None, ''):
            return 0.0
        try:
            return float(v)
        except Exception:
            return 0.0

    cc = component_costs if isinstance(component_costs, dict) else {}
    pp = percentages if isinstance(percentages, dict) else {}

    fabric_cost = to_float(cc.get('fabric_cost'))
    trim_cost = to_float(cc.get('trim_cost'))
    print_cost = to_float(cc.get('print_cost'))
    embroidery_cost = to_float(cc.get('embroidery_cost'))
    sam_cost = to_float(cc.get('sam_cost'))
    base_cost = fabric_cost + trim_cost + print_cost + embroidery_cost + sam_cost

    rejection_percent = to_float(pp.get('rejection_percent'))
    testing_percent = to_float(pp.get('testing_percent'))
    profit_percent = to_float(pp.get('profit_percent'))

    rejection_amount = base_cost * rejection_percent / 100.0
    testing_amount = base_cost * testing_percent / 100.0
    profit_amount = base_cost * profit_percent / 100.0
    final_cost = base_cost + rejection_amount + testing_amount + profit_amount

    currency_code = (pp.get('currency_code') or 'INR').strip() or 'INR'
    conversion_factor = to_float(pp.get('conversion_factor'))
    if conversion_factor <= 0:
        conversion_factor = 1.0
    final_cost_in_currency = round(final_cost / conversion_factor, 2)

    return {
        'fabric_cost': round(fabric_cost, 2),
        'trim_cost': round(trim_cost, 2),
        'print_cost': round(print_cost, 2),
        'embroidery_cost': round(embroidery_cost, 2),
        'sam_cost': round(sam_cost, 2),
        'base_cost': round(base_cost, 2),
        'rejection_percent': round(rejection_percent, 4),
        'testing_percent': round(testing_percent, 4),
        'profit_percent': round(profit_percent, 4),
        'rejection_amount': round(rejection_amount, 2),
        'testing_amount': round(testing_amount, 2),
        'profit_amount': round(profit_amount, 2),
        'final_cost': round(final_cost, 2),
        'currency_code': currency_code,
        'conversion_factor': round(conversion_factor, 6),
        'final_cost_in_currency': final_cost_in_currency,
    }

def _get_matching_fabric(
    constructions: list,
    blends: list,
    gsm: int
):
    fabric = None
    dnc_cost = None

    try:
        FabricMaster = DocType(DOCTYPE_FABRIC_MASTER)
        DNCRate = DocType('DNC Rate')

        max_gsm = int(gsm * 1.2)
        min_gsm = int(gsm * 0.8)

        def build_query(gsm_condition):
            return (
                frappe.qb.from_(FabricMaster)
                .inner_join(DNCRate)
                    .on(DNCRate.code == FabricMaster.shade_cat)
                .select(
                    FabricMaster.name.as_('fabric_id'),
                    FabricMaster.dyed_fabric_material.as_('fabric_code'),
                    FabricMaster.dyed_fabric_material_desc.as_('fabric_description'),
                    FabricMaster.fabric.as_('construction'),
                    FabricMaster.blend,
                    FabricMaster.finish_gsm.as_('gsm'),
                    FabricMaster.shade_cat.as_('shade_category'),
                    FabricMaster.aop,
                    FabricMaster.mechanical_chemical_finish,
                    FabricMaster.grey_fabric_material.as_('grey_fabric_id'),
                    FabricMaster.grey_fabric_material_desc.as_('grey_fabric_code'),
                    FabricMaster.finish_width,
                    FabricMaster.loss_percent,
                    DNCRate.final_cost_rskg_single,
                    DNCRate.final_cost_rskg_double,
                    DNCRate.aop,
                    DNCRate.digital,
                )
                .where(FabricMaster.fabric.isin(constructions))
                .where(FabricMaster.blend.isin(blends))
                .where(gsm_condition)
                .orderby(Abs(FabricMaster.finish_gsm - gsm), order=frappe.qb.asc)
                .orderby(DNCRate.priority, order=frappe.qb.asc)
                .limit(1)
            )

        # first try exact gsm match, if not found, apply max/min range
        db_fabrics = build_query(FabricMaster.finish_gsm == gsm).run(as_dict=True)
        if not db_fabrics:
            db_fabrics = build_query(FabricMaster.finish_gsm.between(min_gsm, max_gsm)).run(as_dict=True)
        
        if db_fabrics:
            db_fabric = db_fabrics[0]
            fabric = {
                'fabric_id': db_fabric['fabric_id'],
                'fabric_code': db_fabric['fabric_code'],
                'fabric_description': db_fabric['fabric_description'],
                'construction': db_fabric['construction'],
                'blend': db_fabric['blend'],
                'gsm': db_fabric['gsm'],
                'gsm_needed': gsm,
                'grey_fabric': {
                    'id': db_fabric['grey_fabric_id'],
                    'code': db_fabric['grey_fabric_code'],
                },
                'shade_category': db_fabric['shade_category'],
                'mechanical_chemical_finish': db_fabric['mechanical_chemical_finish'],
                'fabric_width': _fabric_width_int(db_fabric['finish_width']),
                'loss_percent': round((db_fabric['loss_percent'] + ADDITIONAL_LOSS_PERCENT), 2),
            }
            dnc_cost = {
                'single': db_fabric['final_cost_rskg_single'],
                'double': db_fabric['final_cost_rskg_double'],
                'aop': db_fabric['aop'],
                'digital': db_fabric['digital']
            }
    
    except Exception:
        frappe.log_error(frappe.get_traceback(), 'costing._get_matching_fabric')
    
    return fabric, dnc_cost

def _get_fabric(fabric_id: str):
    fabric = None

    try:
        FabricMaster = DocType(DOCTYPE_FABRIC_MASTER)
        DNCRate = DocType('DNC Rate')

        db_fabrics =  (
            frappe.qb.from_(FabricMaster)
                .inner_join(DNCRate)
                    .on(DNCRate.code == FabricMaster.shade_cat)
                .select(
                    FabricMaster.name.as_('fabric_id'),
                    FabricMaster.dyed_fabric_material.as_('fabric_code'),
                    FabricMaster.dyed_fabric_material_desc.as_('fabric_description'),
                    FabricMaster.fabric.as_('construction'),
                    FabricMaster.blend,
                    FabricMaster.finish_gsm.as_('gsm'),
                    FabricMaster.shade_cat.as_('shade_category'),
                    FabricMaster.aop,
                    FabricMaster.mechanical_chemical_finish,
                    FabricMaster.grey_fabric_material.as_('grey_fabric_id'),
                    FabricMaster.grey_fabric_material_desc.as_('grey_fabric_code'),
                    FabricMaster.finish_width,
                    FabricMaster.loss_percent,
                    DNCRate.final_cost_rskg_single,
                    DNCRate.final_cost_rskg_double,
                    DNCRate.aop,
                    DNCRate.digital,
                    FabricMaster.costing_knitting_code,
                    FabricMaster.costing_mc_processes,
                )
                .where(FabricMaster.name == fabric_id)
                .orderby(FabricMaster.creation, order=frappe.qb.desc)
                .limit(1)
            ).run(as_dict=True)

        if db_fabrics:
            db_fabric = db_fabrics[0]
            fabric = {
                'fabric_id': db_fabric['fabric_id'],
                'fabric_code': db_fabric['fabric_code'],
                'fabric_description': db_fabric['fabric_description'],
                'construction': db_fabric['construction'],
                'blend': db_fabric['blend'],
                'gsm': db_fabric['gsm'],
                'grey_fabric': {
                    'id': db_fabric['grey_fabric_id'],
                    'code': db_fabric['grey_fabric_code'],
                },
                'shade_category': db_fabric['shade_category'],
                'mechanical_chemical_finish': db_fabric['mechanical_chemical_finish'],
                'fabric_width': _fabric_width_int(db_fabric['finish_width']),
                'loss_percent': round((db_fabric['loss_percent'] + ADDITIONAL_LOSS_PERCENT), 2),
                'costing_knitting_code': db_fabric['costing_knitting_code'],
                'costing_mc_processes': _parse_mc_processes(db_fabric['costing_mc_processes']),
                'dnc_cost': {
                    'single': db_fabric['final_cost_rskg_single'],
                    'double': db_fabric['final_cost_rskg_double'],
                    'aop': db_fabric['aop'],
                    'digital': db_fabric['digital']
                }
            }

            return fabric

    except:
        frappe.log_error(frappe.get_traceback(), 'costing._get_fabric')
        raise

def _fabric_width_int(width):
    ret_val = 0
    try:
        ret_val = int(width)
    except Exception:
        pass
    return ret_val

# yarn cost
def _calculate_yarn_costs(fabric: dict):
    ret_val = {
        'yarns': [],
        'cost_per_kg': 0.00
    }

    yarns = _get_fabric_yarns(fabric)

    YarnRate = DocType('Yarn Rate')

    total_cost_per_kg = 0.00

    for yarn in yarns:
        rate_per_kg = 0
        db_yarns = (
            frappe.qb.from_(YarnRate)
            .select(
                YarnRate.material_code,
                YarnRate.description,
                YarnRate.latest_rate,
            )
            .where(YarnRate.material_code == yarn['code'])
            .limit(1)
        ).run(as_dict=True)
        
        if db_yarns:
            db_yarn = db_yarns[0]
            rate_per_kg = db_yarn['latest_rate'] or 0

            yarn['description'] = db_yarn['description']
            yarn['rate_per_kg'] = rate_per_kg
            yarn['effective_cost_per_kg'] = (rate_per_kg * (float(yarn.get('percent') or 0) / 100))

            total_cost_per_kg += yarn['effective_cost_per_kg']
        else:
            yarn['ERROR'] = 'Yarn not found in rate master!'
            #raise Exception(f'Cannot find the yarn [{yarn['code']}] in the yarn rate master!')

    ret_val['yarns'] = yarns
    ret_val['cost_per_kg'] = total_cost_per_kg

    return ret_val

def _get_fabric_yarns(fabric: dict):
    ''' Returns all child yarn records of this fabric. '''

    FabricMasterYarnDetail = DocType('Fabric Master Yarn Detail')

    db_yarns = (
            frappe.qb.from_(FabricMasterYarnDetail)
            .select(
                FabricMasterYarnDetail.yarn.as_('code'),
                FabricMasterYarnDetail.yarn_percent.as_('percent'),
            )
            .where(FabricMasterYarnDetail.parent == fabric['fabric_id'])
            .orderby(FabricMasterYarnDetail.idx)
        ).run(as_dict=True)

    return db_yarns

# knitting cost
def _get_knitting_cost(construction: str, knitting_code: str=None):
    ''' Computes the knitting cost for a construction.

    When `knitting_code` is supplied (e.g. the code already stored on the fabric
    master) it is used as-is; otherwise the code is resolved via llm matching.
    '''

    ret_val = {
        'code': None,
        'cost_per_kg': 0.00
    }

    try:
        if not knitting_code:
            knitting_code = _get_matching_knitting(construction)

        if knitting_code:
            KnittingCharge = DocType('Knitting Charge')

            rows = (frappe.qb.from_(KnittingCharge)
                .select(
                    KnittingCharge.fabric_quality,
                    KnittingCharge.per_kg_cost,
                )
                .where(KnittingCharge.fabric_quality == knitting_code)
                .orderby(KnittingCharge.creation, order=frappe.qb.desc)
                .limit(1)
            ).run(as_dict=True)

            if rows:
                ret_val['code'] = rows[0]['fabric_quality']
                ret_val['cost_per_kg'] = rows[0]['per_kg_cost']
            else:
                ret_val['ERROR'] = f'Knitting code [{knitting_code}] not found in database!'
        else:
            ret_val['ERROR'] = f'No llm matching knitting code for construction [{construction}]!'

    except Exception as ex:
        frappe.log_error(frappe.get_traceback(), 'costing._get_knitting_cost()')

    return ret_val

def _store_costing_value(fabric_id: str, fieldname: str, value):
    ''' Caches an llm matched costing value on the fabric master so that
    subsequent costing calls skip the llm matching. Never breaks costing. '''

    try:
        frappe.db.set_value(
            DOCTYPE_FABRIC_MASTER,
            fabric_id,
            fieldname,
            value,
            update_modified=False,
        )
        # costing is served over GET, which frappe rolls back by default
        frappe.db.commit()
    except Exception:
        frappe.log_error(frappe.get_traceback(), 'costing._store_costing_value()')

def _get_matching_knitting(construction: str):
    knitting_code = None
    try:
        system_prompt = f'''You are an expert assistant for a textile manufacturing company that produces knitted fabrics for garments.

Your task:
Given a fabric construction (also called "fabric quality") provided by the user, identify the single closest-matching knitting type from the catalog of knitting types that our company supports (provided below).

Rules:
1. You MUST pick exactly one knitting type, and its code MUST be copied verbatim from the "code" column of the catalog below. Do not invent, abbreviate, or modify codes.
2. Match based on textile-domain similarity: structure (single jersey, interlock, rib, pique, fleece, etc.), stretch/elastane content, special yarns or finishes implied by the construction string, and any other relevant signals.
3. If multiple knitting types could fit, prefer the one that most specifically matches the construction (e.g. an elastane-blend construction should map to a knitting type that explicitly supports elastane over a plain one).
4. If no knitting type is a reasonable match, return null for "matching_knitting_type".
5. Respond with ONLY a JSON object — no prose, no markdown fences, no explanations.

Output format (strict JSON):
{{
    "matching_knitting_type": "<code from catalog, or null>"
}}

Catalog of supported knitting types (code only):
---
{_get_all_knitting_types()}
---
'''
        user_prompt = f'''Fabric construction: "{construction}"

Return the closest-matching knitting type code from the catalog, in the JSON format specified.'''
        
        llm_response = llm.get_claude_response(system_prompt, user_prompt)
        knitting_code = llm_response.get('matching_knitting_type')

    except Exception as ex:
        frappe.log_error(frappe.get_traceback(), 'costing._get_matching_knitting()')

    return knitting_code

def _get_all_knitting_types():
    rows = frappe.get_all(
        'Knitting Charge',
        fields=['fabric_quality'],
        distinct=True,
    )
    return '\n'.join([row.get('fabric_quality' or '') for row in rows])

# dyes and chemicals cost
def _get_dyes_and_chemicals_cost(fabric: dict, dnc_cost: dict, print_type: str):
    ret_val = {
        'breakup': {}
    }
    breakup = ret_val['breakup']

    cost_per_kg = 0.00

    is_double_dnc = _is_double_dnc(fabric)

    if is_double_dnc:
        cost_per_kg = dnc_cost['double']
        breakup['dnc_double_pass'] = cost_per_kg
    else:
        cost_per_kg = dnc_cost['single']
        breakup['dnc_single_pass'] = cost_per_kg

    print_type = print_type.lower() if print_type else '<unknown>'

    if print_type == 'aop':  # All-Over-Print
        cost_per_kg += dnc_cost['aop']
        breakup['aop'] = dnc_cost['aop']
    else:
        if print_type == 'digital':  # Digital print
            cost_per_kg += dnc_cost['digital']
            breakup['digital'] = dnc_cost['digital']

    ret_val['cost_per_kg'] = cost_per_kg

    return ret_val

def _is_double_dnc(fabric: dict):
    return _grey_code(fabric).startswith(('DD ', 'MCD '))

def _grey_code(fabric: dict) -> str:
    ''' The matched fabric's grey fabric code, as a string that is always safe to
    read.

    `grey_fabric_material_desc` is a non-mandatory Small Text on the fabric
    master, so plenty of rows carry nothing -- and every rule that reads the code
    is a prefix or substring test. Missing therefore has to mean "matches
    nothing" rather than an exception: the code is what marks the EXTRA charges
    (a double dyeing pass, a waffle or variegated-rib finish), so an unknown code
    falls to the cheaper branch. Reading it as a premium instead would silently
    inflate every cost built on an incomplete master row. '''
    return (fabric.get('grey_fabric') or {}).get('code') or ''

# mechanical and chemical process cost
def _get_mechanical_chemical_process_cost(mnc_finish_code: str, processes: dict=None):
    ''' Computes the mechanical & chemical process cost for a combined finish code.

    When `processes` is supplied (e.g. the decomposition already stored on the
    fabric master, as {'mechanical_processes': [...], 'chemical_processes': [...]})
    it is used as-is; otherwise it is resolved via llm matching.
    '''

    try:
        ret_val = {
            'processes': {
                'mechanical': [],
                'chemical': []
            },
            'cost_per_kg': 0.00
        }

        total_cost = 0.00

        if not processes:
            processes = _get_matching_mnc_proccesses(mnc_finish_code)
        
        if processes:
            # mechanical
            m_prcesses = processes['mechanical_processes']
            MechanicalFinish = DocType('Mechanical Finish')
            for m_process_code in m_prcesses:
                rows = (frappe.qb.from_(MechanicalFinish)
                    .select(
                        MechanicalFinish.finish_type,
                        MechanicalFinish.price,
                    )
                    .where(MechanicalFinish.finish_type == m_process_code)
                    .orderby(MechanicalFinish.creation, order=frappe.qb.desc)
                    .limit(1)
                ).run(as_dict=True)

                if rows:
                    ret_val['processes']['mechanical'].append({
                        'process_type': rows[0]['finish_type'],
                        'cost': rows[0]['price'],
                    })
                    total_cost += rows[0]['price']

            # chemical
            c_prcesses = processes['chemical_processes']
            ChemicalFinish = DocType('Chemical Finish')
            for c_process_code in c_prcesses:
                rows = (frappe.qb.from_(ChemicalFinish)
                    .select(
                        ChemicalFinish.finish_type,
                        ChemicalFinish.cost,
                    )
                    .where(ChemicalFinish.finish_type == c_process_code)
                    .orderby(ChemicalFinish.creation, order=frappe.qb.desc)
                    .limit(1)
                ).run(as_dict=True)

                if rows:
                    ret_val['processes']['chemical'].append({
                        'process_type': rows[0]['finish_type'],
                        'cost': rows[0]['cost'],
                    })
                    total_cost += rows[0]['cost']

        ret_val['cost_per_kg'] = total_cost

    except Exception as ex:
        frappe.log_error(frappe.get_traceback(), 'costing._get_mechanical_chemical_process_cost()')

    return ret_val

def _parse_mc_processes(value):
    ''' Reads the 'costing_mc_processes' field (JSON) of the fabric master into
    {'mechanical_processes': [...], 'chemical_processes': [...]}. Returns None
    when nothing usable is stored, so that llm matching kicks in. '''

    try:
        if isinstance(value, str):
            value = json.loads(value) if value.strip() else None

        if isinstance(value, dict):
            processes = {
                'mechanical_processes': list(value.get('mechanical_processes') or []),
                'chemical_processes': list(value.get('chemical_processes') or []),
            }
            if processes['mechanical_processes'] or processes['chemical_processes']:
                return processes

    except Exception:
        frappe.log_error(frappe.get_traceback(), 'costing._parse_mc_processes()')

    return None

def _matched_mc_processes(mnc_cost: dict):
    ''' Extracts the process codes that actually resolved to a rate row out of a
    _get_mechanical_chemical_process_cost() result, in storable form. Returns
    None when nothing resolved, so that no empty match gets cached. '''

    processes = (mnc_cost or {}).get('processes') or {}
    ret_val = {
        'mechanical_processes': [p['process_type'] for p in (processes.get('mechanical') or [])],
        'chemical_processes': [p['process_type'] for p in (processes.get('chemical') or [])],
    }

    if not (ret_val['mechanical_processes'] or ret_val['chemical_processes']):
        return None

    return ret_val

def _get_matching_mnc_proccesses(mnc_process_code: str):
    processes = {
        'mechanical_processes': [],
        'chemical_processes': [],
    }

    try:
        system_prompt = f'''You are an expert assistant for a textile manufacturing company that produces knitted fabrics for garments.

Your task:
The user will provide a single combined "mechanical and chemical process code" — a short string that may concatenate or abbreviate one or more mechanical processes and/or one or more chemical processes that have been applied to a fabric (e.g. "PEACH FACE+HEIQ", "BB+ENZYME", "SUEDING+WICKING").

Decompose this code into its constituent processes, and for each process map it to the closest matching entry from our company's catalog of supported mechanical processes and supported chemical processes (provided below). Return the matches grouped into two separate lists.

Rules:
1. Each returned value MUST be copied verbatim from the catalogs below — do NOT invent, paraphrase, or reformat names.
2. A single input code may contain multiple processes (joined by "+", "&", "/", spaces, or just concatenated). Identify ALL of them.
3. Mechanical processes go ONLY in "mechanical_processes". Chemical processes go ONLY in "chemical_processes". Never mix them across lists.
4. Match using textile-domain knowledge: recognise common abbreviations (e.g. "BB" → "BRUSHED BACK", "PF" → "PEACH FACE"), synonyms, and partial spellings.
5. Deduplicate — each catalog entry should appear at most once across the output.
6. If a token in the input has no reasonable match in either catalog, omit it (do not guess).
7. If no matches are found for a category, return an empty list for that category.
8. Respond with ONLY a JSON object — no prose, no markdown fences, no explanations.

Output format (strict JSON):
{{
    "mechanical_processes": ["<exact catalog entry>", ...],
    "chemical_processes": ["<exact catalog entry>", ...]
}}

Catalog of supported mechanical processes (one per line):
---
{_get_all_mechanical_processes()}
---

Catalog of supported chemical processes (one per line):
---
{_get_all_chemical_processes()}
---
'''
        
        user_prompt = f'''Combined mechanical & chemical process code: "{mnc_process_code}"

Decompose this code and return the matching mechanical and chemical processes from the catalogs above, in the JSON format specified.'''
        
        processes = llm.get_claude_response(system_prompt, user_prompt)

    except Exception as ex:
        frappe.log_error(frappe.get_traceback(), 'costing._get_matching_mnc_proccesses()')

    return processes

def _get_all_mechanical_processes():
    rows = frappe.get_all(
        'Mechanical Finish',
        fields=['finish_type'],
        distinct=True,
    )
    return '\n'.join([row.get('finish_type' or '') for row in rows])

def _get_all_chemical_processes():
    rows = frappe.get_all(
        'Chemical Finish',
        fields=['finish_type'],
        distinct=True,
    )
    return '\n'.join([row.get('finish_type' or '') for row in rows])

# finishing charges
#   Each [Finishing Charge Rule] row represents one finishing process. The row
#   carries a `cost_per_kg` and a set of boolean flags. For every flag set on
#   the row, the corresponding check below is evaluated against the fabric;
#   the first passing check applies the process's cost_per_kg and the rule
#   evaluation moves on to the next process.
# Flag fieldname on the rule row -> check function name.
FINISHING_CHECK_MAP = {
	'poly':              '_check_poly',
	'nylon':             '_check_nylon',
	'spandex_elastane':  '_check_spandex_elastane',
	'other_blend':       '_check_other_blend',
	'gsm_below_200':     '_check_gsm_below_200',
	'gsm_200_above':     '_check_gsm_200_above',
	'width_below_40':    '_check_width_below_40',
	'width_40_above':    '_check_width_40_above',
	'waffle_wfl':        '_check_waffle_wfl',
	'verigated_rib_ver': '_check_verigated_rib_ver',
}

def _get_finishing_charges(fabric: dict):
    '''
    Returns the total fabric finishing cost (per kg) for a fabric, driven
    by the rules stored in the "Finishing Charge Rule" doctype.

    For each rule row, evaluates the checks whose flags are enabled and
    charges that process's `cost_per_kg` on the first passing check. If no
    enabled check passes for a row, that process is skipped.
    '''
    charges = {
        'blend': None,
        'breakup': {},
        'cost_per_kg': 0.00
    }

    # parse blend to get fibers
    blend = blend_parser.analyze_blend(fabric['blend'])
    fibers = list(blend.keys())
    charges['blend'] = fibers
    fabric['fibers'] = fibers

    rules = frappe.get_all(
        'Finishing Charge Rule',
        fields=['process_name', 'cost_per_kg', *FINISHING_CHECK_MAP.keys()],
    )

    total = 0.00
    for rule in rules:
        for flag, check_name in FINISHING_CHECK_MAP.items():
            if rule.get(flag) == 'Y':
                check_fn = globals().get(check_name)
                if check_fn and check_fn(fabric):
                    cost = float(rule.get('cost_per_kg') or 0)
                    total += cost
                    json_tag = rule.get('process_name').lower().replace(' ', '_')
                    charges['breakup'][json_tag] = cost
                    break  # rule fired — move on to the next process

    charges['cost_per_kg'] = round(total, 2)

    return charges

# Blend-based checks
def _check_poly(fabric: dict) -> bool:
	return 'poly' in fabric['fibers']

def _check_nylon(fabric: dict) -> bool:
	return 'nylon' in fabric['fibers']

def _check_spandex_elastane(fabric: dict) -> bool:
	return 'elastane' in fabric['fibers']

def _check_other_blend(fabric: dict) -> bool:
    allowed_fibers = {'poly', 'nylon', 'elastane'}
    return all(fabric_fiber not in allowed_fibers for fabric_fiber in fabric['fibers'])

# GSM-based checks
def _check_gsm_below_200(fabric: dict) -> bool:
	return fabric['gsm'] < 200

def _check_gsm_200_above(fabric: dict) -> bool:
	return fabric['gsm'] >= 200

# Width-based checks
def _check_width_below_40(fabric: dict) -> bool:
	return fabric['fabric_width'] < 40

def _check_width_40_above(fabric: dict) -> bool:
	return fabric['fabric_width'] >= 40

# Construction/structure-based checks
def _check_waffle_wfl(fabric: dict) -> bool:
	return 'WFL' in _grey_code(fabric)

def _check_verigated_rib_ver(fabric: dict) -> bool:
	return 'VER' in _grey_code(fabric)

def _to_float(value):
    try:
        if value in (None, ''):
            return 0.0
        return float(value)
    except Exception:
        return 0.0
