'''
Auto-populate a Moodboard ESG record from its source: a Moodboard Style's costing,
or a Sample Request's linked Fabric Master + Trim Costing.

The ESG record is keyed by (source_doctype, source_name) and created ONCE per
source — if one already exists it is left untouched, so later re-saves and any
manual ESG edits are preserved. The shared writer is `_assemble_esg`; each source
has its own collector (`ensure_esg_for_style` / `ensure_esg_for_sample`).

Style path — triggered when a style's cost is saved (see the MoodboardStyle
controller); reads the costing JSON (cost_inputs / cost_result).
Sample path — triggered on Sample Request save (doc_events -> on_sample_update) or
the "Feed ESG Input" Desk button; reads the sample's direct Fabric Master / Trim
Costing links. Samples have no costing run, so finishing_process is left blank and
consumption / marker efficiency fall back to house defaults.

Mapping (per spec):
  Order Context:
    customer            -> first Brand linked to the style's moodboard
    customer_profile    -> "Generic"
    ship_country        -> "India"
    production_city     -> "Indore"
    suggestion_criteria -> "ESG Sensitive"
  Main Body garment element (only one, for now):
    consumption, marker_efficiency -> the style's BOM-extracted fields
    transport_mode      -> "Sea"
    zero_tolerance_fail -> unchecked
    finishing_process   -> the finishing rule that fired in costing (cost_result),
        Compactor ignored, precedence Stenter 2nd > Dryer > Stenter 1st, mapped to
        Stentor 2 / Dryer / Stentor 1
    fabric_type / old_fabric_type / blend / shade_category / gsm / dyeing_type /
        finishing_mech_chem -> Fabric Master (old_fabric_type <-
        old_fabric_construction, shade_category <- shade_cat), matched by the Main
        Body fabric's `fabricId` in cost_inputs.fabrics. dyeing_type falls back to
        "SOFT" when fab_dyeing_type is empty.
    finishing_aop_digital -> the Main Body fabric's costing print_type
        (aop -> YES, digital -> Digital Print, else NO)
    yarn_process_type / yarn_specs -> the primary yarn's (idx 1) technology_desc /
        quality_desc on Fabric Master Yarn Detail
    yarn_composition    -> [{name: yarn.blend, pct: yarn.yarn_percent}, ...]
  Trims (from the "Trim Costing" master, keyed by cost_inputs.style):
    number_of_trims     -> trim rows, collapsing each trim_group to one
    elastic             -> "Elastic - Covered" / "Elastic - Exposed" if that trim
                           is present (Covered wins if both), else "No"
    trim_style          -> cost_inputs.style
'''

import json

import frappe

ESG_DOCTYPE = 'Moodboard ESG'
STYLE_DOCTYPE = 'Moodboard Style'
SAMPLE_DOCTYPE = 'Sample Request'
FABRIC_MASTER = 'Fabric Master'
FABRIC_YARN = 'Fabric Master Yarn Detail'
ELASTIC_COVERED = 'Elastic - Covered'
ELASTIC_EXPOSED = 'Elastic - Exposed'

# A Sample Request carries no costing run, so consumption / marker efficiency may
# be blank. Fall back to these house defaults when they are.
DEFAULT_SAMPLE_CONSUMPTION = 220
DEFAULT_SAMPLE_MARKER_EFFICIENCY = 82

# Finishing – Process is picked from the finishing-charge rules that fired during
# costing (already stored in cost_result). Compactor is ignored; the highest-
# precedence fired process wins, mapped to the field's kept labels. Slugs are the
# rule process_name lower-cased with spaces -> underscores.
_FINISHING_PROCESS_PRECEDENCE = (
    ('stenter_2nd_pass', 'Stentor 2'),
    ('dryer', 'Dryer'),
    ('stenter_1st_pass', 'Stentor 1'),
)


def ensure_esg_for_style(style):
    '''
    Create (once) a Moodboard ESG for a costed style. No-op if the style isn't
    costed yet or an ESG already exists. Returns the new ESG name, or None.
    '''
    if not style or style.get('cost_status') != 'Computed':
        return None
    if _esg_exists(STYLE_DOCTYPE, style.name):
        return None  # create-once: leave an existing ESG (and any manual edits) alone

    inputs = _load_json(style.get('cost_inputs')) or {}
    order_ctx = {
        'customer': _first_brand(style.get('moodboard')),
        'customer_profile': 'Generic',
        'ship_country': 'India',
        'production_city': 'Indore',
        'suggestion_criteria': 'ESG Sensitive',
    }
    return _assemble_esg(
        STYLE_DOCTYPE, style.name,
        order_ctx=order_ctx,
        trims=_trim_info(inputs),
        elements=[_main_body_element(style, inputs)],
        moodboard=style.get('moodboard'),
        moodboard_style=style.name,
    )


def ensure_esg_for_sample(sample):
    '''
    Create (once) a Moodboard ESG for a Sample Request. Requires both a Fabric
    Master and a Trim Costing to be linked on the sample (the ESG needs their
    data); no-op otherwise or if an ESG already exists. Returns the new ESG name,
    or None.

    Unlike a style, a sample has no costing run — so there is no finishing-rule
    breakup (finishing_process is left blank) and consumption / marker efficiency
    fall back to house defaults when the sample leaves them empty.
    '''
    if not sample or not sample.get('fabric_master') or not sample.get('trim_costing'):
        return None
    if _esg_exists(SAMPLE_DOCTYPE, sample.name):
        return None

    order_ctx = {
        'customer': None,               # sample.customer is free text, not a Brand link
        'customer_profile': 'Generic',
        'ship_country': 'India',
        'production_city': 'Indore',
        'suggestion_criteria': 'ESG Sensitive',
    }
    return _assemble_esg(
        SAMPLE_DOCTYPE, sample.name,
        order_ctx=order_ctx,
        trims=_sample_trim_info(sample.get('trim_costing')),
        elements=[_sample_main_body_element(sample)],
    )


def on_sample_update(doc, method=None):
    '''
    doc_events hook (Sample Request.on_update): auto-build the ESG once both a
    Fabric Master and a Trim Costing are linked. Create-once and best-effort — a
    failure is logged and never blocks the sample save.
    '''
    try:
        ensure_esg_for_sample(doc)
    except Exception:
        frappe.log_error(frappe.get_traceback(), 'SampleRequest ESG auto-create')


def _assemble_esg(source_doctype, source_name, order_ctx, trims, elements,
                  moodboard=None, moodboard_style=None):
    '''
    Write a new Moodboard ESG for a given source from already-collected inputs.
    Shared by the style and sample paths; the source-specific gathering happens in
    the callers. Returns the new ESG name, or None if one already exists.
    '''
    if _esg_exists(source_doctype, source_name):
        return None

    esg = frappe.new_doc(ESG_DOCTYPE)
    esg.source_doctype = source_doctype
    esg.source_name = source_name
    esg.moodboard_style = moodboard_style
    esg.moodboard = moodboard

    for field, value in (order_ctx or {}).items():
        esg.set(field, value)

    esg.number_of_trims = trims['number_of_trims']
    esg.elastic = trims['elastic']
    esg.trim_style = trims['trim_style']

    for element in elements:
        esg.append('elements', element)

    esg.insert(ignore_permissions=True)
    return esg.name


def _esg_exists(source_doctype, source_name):
    ''' Whether an ESG already exists for a given source (create-once guard). '''
    return bool(frappe.db.exists(
        ESG_DOCTYPE, {'source_doctype': source_doctype, 'source_name': source_name}
    ))


def _first_brand(moodboard):
    ''' The first Brand linked to the moodboard (earliest Brand Moodboard row). '''
    if not moodboard:
        return None
    rows = frappe.get_all(
        'Brand Moodboard', filters={'moodboard': moodboard}, fields=['brand'],
        order_by='creation asc', limit=1, ignore_permissions=True,
    )
    return rows[0]['brand'] if rows else None


def _trim_info(inputs):
    '''
    {number_of_trims, elastic, trim_style} from the Trim Costing master, matched by
    the style's costing garment type (cost_inputs.style). Used by the style path.
    '''
    style = _clean(inputs.get('style'))
    if not style:
        return _summarize_trims([], trim_style=None)

    TrimCosting = frappe.qb.DocType('Trim Costing')
    TrimCostingItem = frappe.qb.DocType('Trim Costing Item')
    rows = (
        frappe.qb.from_(TrimCosting)
        .inner_join(TrimCostingItem).on(TrimCostingItem.parent == TrimCosting.name)
        .select(TrimCostingItem.trim, TrimCostingItem.trim_group)
        .where(TrimCosting.style_name == style)
    ).run(as_dict=True)
    return _summarize_trims(rows, trim_style=style)


def _sample_trim_info(trim_costing):
    '''
    {number_of_trims, elastic, trim_style} from a Trim Costing linked directly on a
    Sample Request (its `trim_costing` field), read via the child rows' parent.
    trim_style is the Trim Costing's own style_name (the costing garment type).
    '''
    if not trim_costing:
        return _summarize_trims([], trim_style=None)

    trim_style = frappe.db.get_value('Trim Costing', trim_costing, 'style_name') or trim_costing
    TrimCostingItem = frappe.qb.DocType('Trim Costing Item')
    rows = (
        frappe.qb.from_(TrimCostingItem)
        .select(TrimCostingItem.trim, TrimCostingItem.trim_group)
        .where(TrimCostingItem.parent == trim_costing)
    ).run(as_dict=True)
    return _summarize_trims(rows, trim_style=trim_style)


def _summarize_trims(rows, trim_style=None):
    '''
    Collapse Trim Costing Item rows into {number_of_trims, elastic, trim_style}.
    Each trim_group counts once; ungrouped trims count singly. Elastic reflects
    which elastic trim is present ("Covered" wins if both are).
    '''
    info = {'number_of_trims': 0, 'elastic': 'No', 'trim_style': trim_style or None}
    if not rows:
        return info

    groups = set()
    ungrouped = 0
    names = set()
    for r in rows:
        names.add(_clean(r.get('trim')))
        group = _clean(r.get('trim_group'))
        if group:
            groups.add(group)
        else:
            ungrouped += 1

    info['number_of_trims'] = len(groups) + ungrouped
    if ELASTIC_COVERED in names:
        info['elastic'] = ELASTIC_COVERED
    elif ELASTIC_EXPOSED in names:
        info['elastic'] = ELASTIC_EXPOSED
    return info


def _main_body_element(style, inputs):
    ''' The single Main Body ESG element row (a dict for esg.append). '''
    element = {
        'section_name': 'Main Body',
        'consumption': style.get('consumption'),            # BOM-extracted on the style
        'marker_efficiency': style.get('marker_efficiency'),
        'transport_mode': 'Sea',
        'zero_tolerance_fail': 0,
        'dyeing_type': 'SOFT',                               # default until sourced below
        'finishing_process': _finishing_process(style),     # from the costed finishing rules
    }

    fabric_input = _main_body_fabric(inputs)
    if not fabric_input:
        return element

    # AOP / Digital reflects the print applied to this fabric in costing.
    element['finishing_aop_digital'] = _aop_digital(fabric_input.get('print_type'))

    fabric_id = fabric_input.get('fabricId')
    fm = frappe.db.get_value(
        FABRIC_MASTER, fabric_id,
        ['fabric', 'blend', 'finish_gsm', 'fab_dyeing_type',
         'mechanical_chemical_finish', 'shade_cat'], as_dict=True,
    ) if fabric_id else None

    if fm:
        element['fabric_type'] = fm.get('fabric')            # construction
        element['old_fabric_type'] = _old_fabric_construction(fabric_id)
        element['fabric_blend'] = fm.get('blend')
        element['shade_category'] = fm.get('shade_cat')
        element['gsm'] = _to_float(fm.get('finish_gsm'))
        element['dyeing_type'] = fm.get('fab_dyeing_type') or 'SOFT'
        element['finishing_mech_chem'] = fm.get('mechanical_chemical_finish')

        yarns = _fabric_yarns(fabric_id)
        if yarns:
            primary = yarns[0]                               # idx 1
            element['yarn_process_type'] = primary.get('technology_desc')
            element['yarn_specs'] = primary.get('quality_desc')
            element['yarn_composition'] = json.dumps(
                [{'name': y.get('blend'), 'pct': _to_float(y.get('yarn_percent'))} for y in yarns]
            )
    else:
        # No matching Fabric Master row — fall back to the cost-input fabric fields.
        element['fabric_type'] = fabric_input.get('construction')
        element['fabric_blend'] = fabric_input.get('blend')
        element['gsm'] = _to_float(fabric_input.get('gsm'))

    return element


def _sample_main_body_element(sample):
    '''
    The single Main Body ESG element for a Sample Request. Fabric/yarn data comes
    from the linked Fabric Master (same lookups as the style path); consumption and
    marker efficiency come off the sample with house defaults; finishing_process is
    left blank (no costing run on a sample).
    '''
    element = {
        'section_name': 'Main Body',
        'consumption': _to_float(sample.get('consumption')) or DEFAULT_SAMPLE_CONSUMPTION,
        'marker_efficiency': (_to_float(sample.get('marker_efficiency'))
                              or DEFAULT_SAMPLE_MARKER_EFFICIENCY),
        'transport_mode': 'Sea',
        'zero_tolerance_fail': 0,
        'dyeing_type': 'SOFT',
        'finishing_process': None,
        'finishing_aop_digital': _sample_aop_digital(sample),
    }

    fabric_id = sample.get('fabric_master')
    fm = frappe.db.get_value(
        FABRIC_MASTER, fabric_id,
        ['fabric', 'blend', 'finish_gsm', 'fab_dyeing_type',
         'mechanical_chemical_finish', 'shade_cat'], as_dict=True,
    ) if fabric_id else None

    if fm:
        element['fabric_type'] = fm.get('fabric')
        element['old_fabric_type'] = _old_fabric_construction(fabric_id)
        element['fabric_blend'] = fm.get('blend')
        element['shade_category'] = fm.get('shade_cat')
        element['gsm'] = _to_float(fm.get('finish_gsm'))
        element['dyeing_type'] = fm.get('fab_dyeing_type') or 'SOFT'
        element['finishing_mech_chem'] = fm.get('mechanical_chemical_finish')

        yarns = _fabric_yarns(fabric_id)
        if yarns:
            primary = yarns[0]                               # idx 1
            element['yarn_process_type'] = primary.get('technology_desc')
            element['yarn_specs'] = primary.get('quality_desc')
            element['yarn_composition'] = json.dumps(
                [{'name': y.get('blend'), 'pct': _to_float(y.get('yarn_percent'))} for y in yarns]
            )
    else:
        # No Fabric Master row — fall back to the sample's own flat fabric fields.
        element['fabric_type'] = sample.get('fabric_type')
        element['fabric_blend'] = sample.get('fabric_blend') or sample.get('clean_blend')
        element['gsm'] = _to_float(sample.get('finished_gsm'))

    return element


def _sample_aop_digital(sample):
    '''
    Map a sample's print fields to the AOP/Digital select value. An explicit "Yes"
    in the `aop` field wins; otherwise fall back to the garment print type.
    '''
    if _clean(sample.get('aop')).strip().lower() in ('yes', 'y', 'true', '1'):
        return 'YES'
    return _aop_digital(sample.get('garment_print_type'))


def _finishing_process(style):
    '''
    Single Finishing – Process for the Main Body, read from the finishing charges
    the cost engine already computed and stored in cost_result. Ignores Compactor;
    precedence: Stenter 2nd Pass > Dryer > Stenter 1st Pass. None if unavailable.
    '''
    result = _load_json(style.get('cost_result')) or {}
    for f in (result.get('fabric') or []):
        if not isinstance(f, dict) or _clean(f.get('section')).lower() != 'main body':
            continue
        fired = set(_finishing_breakup(f).keys())
        for slug, label in _FINISHING_PROCESS_PRECEDENCE:
            if slug in fired:
                return label
        return None
    return None


def _finishing_breakup(fabric):
    '''
    The finishing-charge breakup dict for a fabric section, tolerant of result
    shape: the frontend-stored form `fabric.breakup.finishing.breakup` and the raw
    engine form `fabric.costing.cost_per_kg.breakup.finishing_charges.breakup`.
    '''
    outer_candidates = [
        fabric.get('breakup'),
        ((fabric.get('costing') or {}).get('cost_per_kg') or {}).get('breakup'),
    ]
    for outer in outer_candidates:
        if not isinstance(outer, dict):
            continue
        finishing = outer.get('finishing') or outer.get('finishing_charges')
        inner = finishing.get('breakup') if isinstance(finishing, dict) else None
        if isinstance(inner, dict):
            return inner
    return {}


def _main_body_fabric(inputs):
    ''' The cost_inputs fabric whose section is "Main Body" (case-insensitive). '''
    for f in (inputs.get('fabrics') or []):
        if isinstance(f, dict) and _clean(f.get('section')).lower() == 'main body':
            return f
    return None


def _old_fabric_construction(fabric_id):
    '''
    Fabric Master.old_fabric_construction for the fabric, tolerant of sites where
    that column doesn't exist yet (returns None instead of erroring).
    '''
    if 'old_fabric_construction' not in frappe.db.get_table_columns(FABRIC_MASTER):
        return None
    return frappe.db.get_value(FABRIC_MASTER, fabric_id, 'old_fabric_construction')


def _fabric_yarns(fabric_id):
    ''' Yarn child rows of a Fabric Master, ordered by idx. '''
    Yarn = frappe.qb.DocType(FABRIC_YARN)
    return (
        frappe.qb.from_(Yarn)
        .select(Yarn.blend, Yarn.yarn_percent, Yarn.technology_desc, Yarn.quality_desc)
        .where(Yarn.parent == fabric_id)
        .orderby(Yarn.idx)
    ).run(as_dict=True)


# --- helpers ---

def _load_json(value):
    if not value:
        return None
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except Exception:
        return None


def _clean(value):
    return value.strip() if isinstance(value, str) else (value or '')


def _to_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _aop_digital(print_type):
    ''' Map a fabric's costing print type to the AOP/Digital select value. '''
    pt = (print_type or '').strip().lower()
    if pt == 'aop':
        return 'YES'
    if pt == 'digital':
        return 'Digital Print'
    return 'NO'
