'''
Fabric cost optimiser -- "same fabric, cheaper spec" suggestions.

Lever A of the cost optimiser: it never changes the fabric itself. Construction,
blend and GSM are held exactly as costed, so the garment is unaffected and no
garment/fabric compatibility rule is needed to keep the suggestion honest. What
it varies is the *spec* carried by the Fabric Master row -- the shade category,
the dyeing pass, the mechanical/chemical finish -- plus the print type, which is
a costing input rather than a property of the row.

Why those four: they are the only inputs to `costing.calculate()` that move the
per-kg total without changing what the fabric is. The spread is large. `DNC Rate`
runs from Rs 56.43/kg (G, R) to Rs 217.77/kg (S) on a single pass, and to
Rs 240.98/kg on a double -- so a shade band is worth more per kilo than most
fabric swaps are.

Speed: `costing._compute_fabric_cost()` resolves the knitting code and the
mechanical/chemical processes through Claude whenever the fabric master has no
cached value, and only 69 of 12,674 rows are cached today. Costing every
candidate through it would mean dozens of llm round trips per request. Instead
the baseline is costed once, in full, and each candidate is costed on a fast
path that reuses what cannot differ:

    yarn         per candidate   -- child table read, no llm
    knitting     from baseline   -- keyed on construction, which is held fixed
    dyes & chem  per candidate   -- `DNC Rate` lookup, no llm
    mech/chem    memoised by code -- llm only for an uncached code, and budgeted
    finishing    memoised        -- `Finishing Charge Rule` maths, no llm

So the common case -- a shade or dyeing-pass change, where the finish code is
held -- costs zero llm calls beyond the baseline.

Endpoint:
    GET|POST /api/method/prism.api.cost_optimiser.suggest
    Header: X-Auth-Token: <jwt>            (required -- see auth_required)
'''

import json

import frappe
from frappe.utils import cint

from prism.auth.authenticator import auth_required
import prism.api.costing as costing
import prism.api.fabric_matcher as fabric_matcher
import prism.api.surplus_recommender as sr

DOCTYPE_FABRIC_MASTER = 'Fabric Master'
DOCTYPE_GARMENT_RULE = 'Garment Fabric Rule'

# Below this a suggestion is noise: a rupee or two a piece is inside the
# rounding of the costing itself, and a list of them buries the real ones.
DEFAULT_MIN_SAVING_PER_PIECE = 2.00
DEFAULT_MIN_SAVING_PERCENT = 1.50

# A total across every lever, not a cap per lever. Three lists of five is
# fifteen options for one section, which is a search result rather than a
# recommendation -- nobody reads to the bottom of it, and the weak entries make
# the strong ones look arbitrary.
DEFAULT_MAX_SUGGESTIONS = 4

# An uncached mechanical/chemical finish code costs one llm round trip to
# decompose. A request may spend this many and no more; candidates whose code is
# still unresolved after the budget is gone are dropped rather than waited on.
# Pass deep=1 to lift it when latency does not matter.
MNC_LLM_BUDGET = 2
MNC_LLM_BUDGET_DEEP = 25

# Print type is a costing input, not a property of the fabric row, so it is
# varied separately from the spec signature.
PRINT_TYPES = (None, 'aop', 'digital')

# --- Lever B: a different fabric -------------------------------------------
#
# How far the weight may move when nothing tells us what the garment needs.
# A garment/fabric rule table would give the real band -- a hoody body cannot be
# 150 gsm whatever the catalogue offers -- and there is no such table yet. Ten
# percent is the band that needs no table to defend: no garment is reclassified
# by a 10% weight change, so the suggestion stays honest while the gate is
# missing. _gsm_band() uses a `Garment Fabric Rule` row instead the moment one
# exists.
DEFAULT_GSM_TOLERANCE_PERCENT = 10.0

# Below this the two are not the same cloth and the swap is a design decision,
# not a costing one. Sits above fabric_matcher's own COMPOSITION_FLOOR (50)
# because this lever changes what gets cut, not merely what it is called.
DEFAULT_MIN_SIMILARITY = 70.0

# Mirrors fabric_matcher's weights so one idea of "how close" holds across the
# app. Imported rather than restated wherever the module exposes them.
W_CONSTRUCTION = fabric_matcher.W_CONSTRUCTION
W_COMPOSITION = fabric_matcher.W_COMPOSITION
W_GSM = fabric_matcher.W_GSM

LEVER_SPEC = 'spec'
LEVER_FABRIC = 'fabric'

# --- Lever C: a related construction ----------------------------------------
#
# How readily one knit substitutes for another comes from
# surplus_recommender.RELATED_FAMILIES -- a hand-tuned 0-1 graph (terry/fleece
# 0.80, jersey/interlock 0.45, jersey/terry 0.30). It is curated domain
# knowledge, so it is the gate rather than something re-derived here. At 0.45
# fourteen pairs are permitted among the families the catalogue actually holds,
# and jersey/terry is correctly not one of them.
DEFAULT_MIN_SUBSTITUTABILITY = 0.45

# Crossing families costs construction points by design, so the Lever B floor
# would admit a 0.45 pair on the strength of blend and weight alone. A cross-
# family swap has to clear a higher bar than a blend change does.
DEFAULT_MIN_SIMILARITY_CROSS = 75.0

# Sections whose whole job is holding shape. A collar, cuff or waistband is
# ribbed for recovery -- it springs back -- and no other family does that,
# whatever the substitutability graph says about hand-feel. Without a
# `Garment Fabric Rule` table this is the one section rule that can be defended
# from the section name alone, and it blocks the worst failure this lever has.
RECOVERY_CRITICAL_WORDS = (
    'collar', 'cuff', 'waistband', 'neckband', 'neck band', 'neck rib',
    'binding', 'band', 'rib', 'placket', 'tape', 'drawcord', 'drawstring',
)

# Cross-family candidates span the whole catalogue in the weight band rather
# than one structure root, so the pool is costed newest-cheapest-first and
# bounded. Each costing is several DB reads; this keeps the endpoint responsive.
MAX_CROSS_FAMILY_COSTED = 40

LEVER_FAMILY = 'family'

# Which heads each spec change is allowed to move. Used to name the lever from
# the actual per-head diff rather than from what was varied.
HEAD_DNC = 'dyes_and_chemicals'
HEAD_MNC = 'mechanical_chemical_finish'
HEAD_FINISHING = 'finishing_charges'
HEAD_YARN = 'yarn'
HEAD_KNITTING = 'knitting'


@frappe.whitelist(allow_guest=True)
@auth_required
def suggest(
    garment_style: str = None,
    section: str = None,
    fabric_id: str = None,
    construction: str = None,
    blend: str = None,
    gsm=None,
    shade_category: str = None,
    mnc_finish: str = None,
    print_type: str = None,
    grams_per_piece=None,
    current_cost_per_piece=None,
    max_suggestions=None,
    min_saving_per_piece=None,
    min_saving_percent=None,
    levers='all',
    gsm_tolerance_percent=None,
    min_similarity=None,
    min_similarity_cross=None,
    min_substitutability=None,
    deep=0,
):
    '''
    Cheaper ways to buy the same fabric.

    Identify the fabric either by `fabric_id` (a Fabric Master name, exact) or by
    the same (construction, blend, gsm) triple `get_garment_cost` takes, with an
    optional `shade_category` to pin which row of that triple you are on today.

    `construction` accepts either vocabulary. The catalogue stores English in
    `Fabric Master.fabric` ("Single Jersey", "Rib") and mill shorthand in
    `old_fabric_construction` ("SJY COMPACT", "RIB_1X1 EL"), and the two do not
    overlap -- so both are tried, then the shorthand is decoded to its family.

    Input:
    {
        "garment_style":  <str>,     // optional, echoed back; unused by Lever A
        "section":        <str>,     // optional, echoed back
        "fabric_id":      <str>,     // exact Fabric Master name -- wins if given
        "construction":   <str>,     // "SJY COMPACT" or "Single Jersey"
        "blend":          <str>,     // "100% Cotton"
        "gsm":            <int>,
        "shade_category": <str>,     // optional, "S" | "D" | "L" | "M" | "W" | "G" | "R"
        "mnc_finish":     <str>,     // optional, e.g. "ENZYME + SILICON SOFT".
                                     // Narrows which row is baselined on. Without
                                     // it (and without fabric_id) the commonest
                                     // spec on the triple is assumed -- see
                                     // baseline.resolved_from.
        "print_type":     <str>,     // optional, "aop" | "digital", else none
        "grams_per_piece":       <float>,   // consumption; default 100 g
        "current_cost_per_piece":<float>,   // optional, see below
        "max_suggestions":       <int>,     // TOTAL across all levers, default 4
        "min_saving_per_piece":  <float>,   // default 2.00
        "min_saving_percent":    <float>,   // default 1.50
        "levers":         <str>,  // "all" (default) | "spec" | "fabric" | "family"
        "gsm_tolerance_percent": <float>,   // weight band, default 10
        "min_similarity":        <float>,   // Lever B floor, default 70
        "min_similarity_cross":  <float>,   // Lever C floor, default 75
        "min_substitutability":  <float>,   // Lever C gate, default 0.45
        "deep":                  <0|1>      // raise the llm budget for finish swaps
    }

    Three levers, returned in three lists because they answer different
    questions and a merchandiser picks one for a given section, never several:

      `suggestions`        Lever A -- the same fabric bought on a cheaper spec.
                           Construction, blend and GSM are held exactly; shade
                           category, dyeing pass, finish and print may move.
      `fabric_suggestions` Lever B -- a different fabric of the same kind. Knit
                           structure is held; blend and weight may move.
      `family_suggestions` Lever C -- a related construction. The knit family
                           itself changes, gated on RELATED_FAMILIES; the weight
                           band is enforced. Never "Safe", and refused outright
                           on shape-holding sections.

    B and C price every candidate through costing.calculate() -- the same
    function get_garment_cost runs -- so a suggested fabric's cost_per_kg is
    exactly what that API returns when the suggestion is applied to it. The row
    calculate() picks (shade, finish, loss and all) is the suggestion's
    identity, and each difference from the baseline is a `changes` entry.
    Consumption is never derived: both sides are priced at the caller's own
    grams_per_piece, weight change or not, because how many grams a garment
    takes is a marker question this engine has no data to answer. B and C
    reject each other's candidates on family, so no swap is offered twice.

    On `current_cost_per_piece`: savings are always the *difference* between two
    fabrics costed the same way, so any manual adjustment sitting on your figure
    cancels out of the delta. When you send it, it becomes the baseline the
    suggested costs are quoted against, so the numbers line up with your screen;
    the system's own figure comes back as `baseline.computed_cost_per_piece` so a
    disagreement is visible rather than silently absorbed.

    Returns:
    {
        "status": True,
        "data": {
            "garment_style", "section",
            "baseline": { <identity>, cost_per_kg, cost_per_piece,
                          computed_cost_per_piece, grams_per_piece,
                          is_double_dyed, print_type, breakup },
            "suggestions": [                  // Lever A
                { "lever_group": "spec",
                  "lever": "shade" | "dyeing" | "finish" | "print" | combined,
                  "changes": [                  // structured, not prose
                    { "field": "shade_category", "label": "Shade category",
                      "old": "D", "new": "W" },
                    { "field": "loss_percent", "label": "Loss allowance",
                      "old": 10.38, "new": 2.0, "unit": "%" },
                    ...
                  ],
                  "fabric_id", "fabric_code", "fabric_description",
                  "construction", "blend", "gsm", "shade_category",
                  "is_double_dyed", "mechanical_chemical_finish", "print_type",
                  "cost_per_kg", "cost_per_piece",
                  "saving_per_kg", "saving_per_piece", "saving_percent",
                  "head_deltas": { <head>: <float>, ... },
                  "matching_rows": <int>,
                  "reason": <str> }
            ],
            "fabric_suggestions": [           // Lever B, same shape plus:
                { "lever_group": "fabric", "lever": "fabric",
                  "similarity": <float 0-100>,
                  "similarity_breakdown": { construction, composition, gsm,
                                            stretch_penalty },
                  ... }
            ],
            "considered": <int>,      // spec variants costed
            "notes": [ <str>, ... ]   // anything dropped, and why
        }
    }
    '''
    try:
        #-- RBAC check

        notes = []

        #--- baseline row
        baseline_row, resolve_note = _resolve_baseline(
            fabric_id=fabric_id, construction=construction, blend=blend,
            gsm=gsm, shade_category=shade_category, mnc_finish=mnc_finish,
        )
        if not baseline_row:
            return {'status': False, 'error': resolve_note or 'No matching fabric found!'}
        if resolve_note:
            notes.append(resolve_note)

        # Say out loud what the baseline was inferred from.
        #
        # Every saving is a difference against this row, so if the row was one
        # of a hundred the caller is entitled to know which heads that puts in
        # doubt. Dyes and chemicals follow the shade and are safe; the finish
        # and the loss allowance came from the chosen row and nothing else.
        if not baseline_row.get('pinned') and baseline_row.get('spec_signatures', 1) > 1:
            notes.append(
                f'Baselined on {baseline_row["fabric_id"]} -- the commonest spec among '
                f'{baseline_row["rows_matched"]} matching rows carrying '
                f'{baseline_row["spec_signatures"]} different specs '
                f'({baseline_row["rows_behind_choice"]} rows share the one chosen). '
                'Dyes and chemicals follow the shade you sent and hold regardless, but '
                'the finish and loss allowance are this row\'s. Send fabric_id, or '
                'mnc_finish, to price against your actual fabric.'
            )

        print_type = _clean_print_type(print_type)

        #--- cost the baseline in full, once. This is the only call allowed to
        #--- resolve a knitting code through the llm; every candidate reuses it.
        baseline = costing._compute_fabric_cost(baseline_row['fabric_id'])
        if not baseline:
            return {'status': False, 'error': f'Could not cost fabric [{baseline_row["fabric_id"]}].'}

        # _compute_fabric_cost hardcodes print_type=None, so re-derive the dyes
        # and chemicals head whenever a print is in play.
        if print_type:
            _reprice_dnc(baseline, print_type)

        base_breakup = (baseline.get('cost_per_kg') or {}).get('breakup') or {}
        shared = {
            'knitting': base_breakup.get(HEAD_KNITTING) or {},
            'yarn': base_breakup.get(HEAD_YARN) or {},
            'construction': baseline['construction'],
            'blend': baseline['blend'],
            'gsm': baseline['gsm'],
            'mnc_memo': {},
            'finishing_memo': {},
            'llm_budget': MNC_LLM_BUDGET_DEEP if cint(deep) else MNC_LLM_BUDGET,
            'llm_spent': 0,
            # Levers B and C price each candidate triple through
            # costing.calculate(); one triple can surface in both levers'
            # queries, and calculate() is several DB reads plus possible llm
            # resolution, so its answers are memoised per request.
            'calc_memo': {},
        }
        # The baseline's own finish is already decomposed -- seed the memo with it
        # so holding the finish never costs a round trip.
        base_mnc = (baseline.get('cost_per_kg') or {}).get('breakup', {}).get(HEAD_MNC) or {}
        if baseline.get('mechanical_chemical_finish'):
            shared['mnc_memo'][baseline['mechanical_chemical_finish']] = base_mnc

        grams = costing._to_float(grams_per_piece)
        if grams <= 0:
            grams = 100.0
        kg = grams / 1000.0

        base_net = _net_per_kg(baseline)
        stated = costing._to_float(current_cost_per_piece)
        computed_pp = round(base_net * kg, 2)
        display_base_pp = round(stated, 2) if stated > 0 else computed_pp

        #--- candidates: every distinct spec signature on the same triple,
        #--- narrowed to the baseline's own mill shorthand so a 1x1 rib is never
        #--- offered a 2x2 as though the two were the same cloth.
        shorthand = frappe.db.get_value(
            DOCTYPE_FABRIC_MASTER, baseline['fabric_id'], 'old_fabric_construction')
        shared['shorthand'] = shorthand

        wanted = str(levers or 'all').strip().lower()
        do_spec = wanted in ('all', LEVER_SPEC)
        do_fabric = wanted in ('all', LEVER_FABRIC)
        do_family = wanted in ('all', LEVER_FAMILY)

        suggestions = []
        considered = 0

        if do_spec:
            if shorthand:
                notes.append(
                    f'Compared within "{shorthand}" only. The catalogue\'s plain '
                    f'"{baseline["construction"]}" covers several finer constructions, '
                    'which do not substitute for each other.'
                )
            variants = _spec_variants(baseline, shorthand=shorthand,
                                      exclude_id=baseline['fabric_id'])

            for variant in variants:
                costed = _cost_variant(variant, shared, print_type)
                if not costed:
                    continue
                entry = _build_suggestion(baseline, costed, base_net, kg,
                                          display_base_pp, print_type)
                if entry:
                    suggestions.append(entry)

            #--- print-type variants sit on the baseline row itself
            for candidate_print in PRINT_TYPES:
                if candidate_print == print_type:
                    continue
                costed = _reprint(baseline, candidate_print)
                entry = _build_suggestion(baseline, costed, base_net, kg,
                                          display_base_pp, print_type)
                if entry:
                    suggestions.append(entry)

            considered = len(variants) + len(PRINT_TYPES) - 1

        #--- filter and rank
        floor_abs = costing._to_float(min_saving_per_piece) if min_saving_per_piece is not None \
            else DEFAULT_MIN_SAVING_PER_PIECE
        floor_pct = costing._to_float(min_saving_percent) if min_saving_percent is not None \
            else DEFAULT_MIN_SAVING_PERCENT
        cap = cint(max_suggestions) or DEFAULT_MAX_SUGGESTIONS

        suggestions = _collapse(suggestions)

        kept = [s for s in suggestions
                if s['saving_per_piece'] >= floor_abs and s['saving_percent'] >= floor_pct]
        dropped = len(suggestions) - len(kept)
        if dropped > 0:
            notes.append(
                f'{dropped} cheaper variant(s) hidden: under the '
                f'Rs {floor_abs:.2f}/piece or {floor_pct:.2f}% floor.'
            )

        kept.sort(key=lambda s: (-s['saving_per_piece'], s['fabric_id']))

        #--- Lever B: a different fabric.
        fabric_kept = []
        if do_fabric:
            fabric_all = _fabric_suggestions(
                baseline, shared, print_type, grams, display_base_pp, base_net,
                garment_style, section,
                gsm_tolerance_percent, min_similarity, notes)
            fabric_kept = [s for s in fabric_all
                           if s['saving_per_piece'] >= floor_abs
                           and s['saving_percent'] >= floor_pct]

        #--- Lever C: a related construction. Its own list again -- crossing the
        #--- knit family is a design decision, and burying it among blend swaps
        #--- would let it be accepted as though it were one.
        family_kept = []
        if do_family:
            family_all = _family_suggestions(
                baseline, shared, print_type, grams, display_base_pp, base_net,
                garment_style, section,
                gsm_tolerance_percent, min_similarity_cross,
                min_substitutability, notes)
            family_kept = [s for s in family_all
                           if s['saving_per_piece'] >= floor_abs
                           and s['saving_percent'] >= floor_pct]

        #--- One shortlist, not three.
        #
        # `max_suggestions` is a total. Applied per lever it meant "up to three
        # times this", and asking for five returned nine -- which is not a
        # shortlist, it is the whole search laid out. A merchandiser picks one
        # option for a section, so the useful answer is the few best across
        # every lever, ranked together on money.
        kept, fabric_kept, family_kept, hidden = _shortlist(
            kept, fabric_kept, family_kept, cap, notes)
        if hidden:
            notes.append(f'{hidden} further suggestion(s) not shown '
                         f'(showing the best {cap} across all levers).')

        if _yarn_unpriced(shared['yarn']):
            notes.append(
                'The baseline fabric has no usable yarn price (the code is '
                'missing from Yarn Rate, or carries a rate of 0), so its yarn '
                'cost reads as zero, and every saving is measured against an '
                'understated baseline. Fix the Yarn Rate entry before trusting '
                'the absolute figures; the ranking between suggestions still '
                'holds.'
            )

        if shared['llm_spent'] >= shared['llm_budget']:
            notes.append(
                'Finish-change suggestions were cut short by the llm budget. '
                'Retry with deep=1 for the full set.'
            )

        return {
            'status': True,
            'data': {
                'garment_style': garment_style,
                'section': section,
                'baseline': {
                    **_identity(baseline),
                    'resolved_from': {
                        'pinned': bool(baseline_row.get('pinned')),
                        'rows_matched': cint(baseline_row.get('rows_matched')),
                        'spec_signatures': cint(baseline_row.get('spec_signatures')),
                        'rows_behind_choice': cint(baseline_row.get('rows_behind_choice')),
                    },
                    'is_double_dyed': costing._is_double_dnc(baseline),
                    'print_type': _print_label(print_type),
                    'cost_per_kg': round(base_net, 2),
                    'cost_per_piece': display_base_pp,
                    'computed_cost_per_piece': computed_pp,
                    'grams_per_piece': round(grams, 4),
                    'breakup': _head_costs(baseline),
                },
                'suggestions': kept,
                'fabric_suggestions': fabric_kept,
                'family_suggestions': family_kept,
                'considered': considered,
                'notes': notes,
            },
        }

    except Exception as ex:
        frappe.log_error(frappe.get_traceback(), 'cost_optimiser.suggest')
        return {'status': False, 'error': str(ex)}


# --- baseline resolution ----------------------------------------------------

def _resolve_baseline(fabric_id=None, construction=None, blend=None, gsm=None,
                      shade_category=None, mnc_finish=None):
    '''
    The Fabric Master row the caller is costing today, as (row, note).

    `fabric_id` is exact. Otherwise the triple is matched, preferring the given
    shade category and then the row with the most siblings -- the catalogue holds
    up to 797 rows on one triple, and an unordered pick would make the baseline
    (and so every saving quoted against it) different call to call.
    '''
    if fabric_id and str(fabric_id).strip():
        rows = frappe.db.sql(
            f'select name as fabric_id from `tab{DOCTYPE_FABRIC_MASTER}` where name = %s',
            (str(fabric_id).strip(),), as_dict=True,
        )
        if not rows:
            return None, f'No Fabric Master row named "{fabric_id}".'
        return {'fabric_id': rows[0]['fabric_id'], 'rows_matched': 1,
                'spec_signatures': 1, 'rows_behind_choice': 1,
                'pinned': True}, None

    resolved_construction, shorthand, construction_note = _resolve_construction(construction)
    if not resolved_construction:
        return None, construction_note or 'A fabric_id, or a construction and blend, is required.'

    conditions = ['fabric = %(construction)s']
    params = {'construction': resolved_construction}

    # When the caller spoke mill shorthand, hold it: "Rib" alone would baseline a
    # 2x2 request onto whichever 1x1 row happened to be biggest.
    if shorthand:
        conditions.append('old_fabric_construction = %(shorthand)s')
        params['shorthand'] = shorthand

    if blend and str(blend).strip():
        conditions.append('blend = %(blend)s')
        params['blend'] = str(blend).strip()

    if cint(gsm):
        conditions.append('finish_gsm = %(gsm)s')
        params['gsm'] = cint(gsm)

    if mnc_finish and str(mnc_finish).strip():
        conditions.append('mechanical_chemical_finish = %(mnc)s')
        params['mnc'] = str(mnc_finish).strip()

    params['shade'] = (str(shade_category).strip().upper()
                       if shade_category and str(shade_category).strip() else None)

    # The representative row, not whichever name sorts first.
    #
    # A (construction, blend, gsm, shade) request is not one fabric: Single
    # Jersey 95/5 180 in shade D is 100 rows over 23 distinct spec signatures,
    # with finishes worth Rs 0-20/kg and loss allowances from 6.44% to 14.53%.
    # Dyes and chemicals are safe -- they follow the shade, which the caller
    # supplied -- but the finish and the loss come from whichever row is picked,
    # and every delta is measured against them. Picking by min(name) gave an
    # n=1 one-off while the modal signature had 24 rows behind it. Grouping on
    # the full signature and taking the most populous one at least makes the
    # assumption the commonest truth rather than an alphabetical accident.
    rows = frappe.db.sql(
        f'''
        select
            shade_cat,
            mechanical_chemical_finish,
            (grey_fabric_material_desc like 'DD %%'
             or grey_fabric_material_desc like 'MCD %%') as is_double,
            loss_percent,
            count(*) as n,
            min(name) as sample
        from `tab{DOCTYPE_FABRIC_MASTER}`
        where {' and '.join(conditions)}
        group by shade_cat, mechanical_chemical_finish, is_double, loss_percent
        order by (shade_cat <=> %(shade)s) desc, n desc, sample asc
        limit 1
        ''',
        params, as_dict=True,
    )
    if not rows:
        described = f'"{shorthand}"' if shorthand else f'"{resolved_construction}"'
        detail = (f' Widening to all "{resolved_construction}" was not done on purpose: '
                  f'the plain name covers finer constructions that do not substitute '
                  f'for each other.') if shorthand else ''
        return None, (f'No Fabric Master row for construction {described}'
                      f'{f", blend {blend}" if blend else ""}'
                      f'{f", {cint(gsm)} gsm" if cint(gsm) else ""}.{detail}')

    chosen = rows[0]

    # How much else the request could equally have matched. Without this the
    # caller cannot tell a fabric pinned to one row from one picked out of a
    # hundred, and the two deserve very different confidence.
    #
    # Counted over the shade actually baselined on, not over every shade. Shade
    # is a preference rather than a filter here (a request for a shade the
    # fabric is not stocked in still gets an answer), but the choice was made
    # inside one shade, so a count spanning all of them would overstate the
    # ambiguity -- 661 rows and 90 specs where the real field was 100 and 23.
    spread_conditions = conditions + ['shade_cat <=> %(chosen_shade)s']
    spread_params = dict(params, chosen_shade=rows[0]['shade_cat'])
    spread = frappe.db.sql(
        f'''
        select count(*) as rows_matched,
               count(distinct concat_ws('|', ifnull(mechanical_chemical_finish, ''),
                     (grey_fabric_material_desc like 'DD %%'
                      or grey_fabric_material_desc like 'MCD %%'),
                     loss_percent)) as signatures
        from `tab{DOCTYPE_FABRIC_MASTER}`
        where {' and '.join(spread_conditions)}
        ''',
        spread_params, as_dict=True,
    )[0]

    resolved = {
        'fabric_id': chosen['sample'],
        'rows_matched': cint(spread['rows_matched']),
        'spec_signatures': cint(spread['signatures']),
        'rows_behind_choice': cint(chosen['n']),
        'pinned': False,
    }

    note = construction_note
    if shade_category and chosen['shade_cat'] != str(shade_category).strip().upper():
        note = (f'Shade category "{shade_category}" has no row on this fabric; '
                f'baselined on "{chosen["shade_cat"]}" instead.')

    return resolved, note


def _resolve_construction(construction):
    '''
    Either construction vocabulary -> the value held in `Fabric Master.fabric`.

    Tried in order: `fabric` verbatim, `old_fabric_construction` verbatim (which
    is where the mill shorthand lives), then the shorthand decoded to a family
    and folded to its catalogue label. The last step is lossy on purpose -- the
    catalogue does not distinguish RIB_1X1 from RIB_2X2, both are "Rib" -- so it
    reports what it did.
    '''
    text = (construction or '').strip()
    if not text:
        return None, None, None

    rows = frappe.db.sql(
        f'select fabric from `tab{DOCTYPE_FABRIC_MASTER}` where fabric = %s limit 1',
        (text,), as_dict=True,
    )
    if rows:
        return rows[0]['fabric'], None, None

    rows = frappe.db.sql(
        f'''select fabric, count(*) n from `tab{DOCTYPE_FABRIC_MASTER}`
            where old_fabric_construction = %s and ifnull(fabric, '') != ''
            group by fabric order by n desc limit 1''',
        (text,), as_dict=True,
    )
    if rows:
        return rows[0]['fabric'], text, None

    classified = sr._classify_construction(text)
    family = classified.get('family')
    label = sr.QUALITY_LABELS.get(family) if family else None
    if label:
        rows = frappe.db.sql(
            f'select fabric from `tab{DOCTYPE_FABRIC_MASTER}` where fabric = %s limit 1',
            (label,), as_dict=True,
        )
        if rows:
            return rows[0]['fabric'], None, (
                f'Construction "{text}" read as "{label}". The catalogue does not '
                f'record the finer variant, so suggestions span all of it.'
            )

    return None, None, f'Construction "{text}" is not in the catalogue, under either vocabulary.'


# --- candidate generation ---------------------------------------------------

def _priced_sibling(variant, shared, exclude=None, limit=8):
    '''
    Another row of the same spec signature whose yarn actually has a rate, or
    None. Bounded: at most `limit` siblings are fetched and tried.
    '''
    conditions = [
        'fabric = %(construction)s', 'blend = %(blend)s', 'finish_gsm = %(gsm)s',
        'shade_cat = %(shade)s',
        "ifnull(mechanical_chemical_finish, '') = %(mnc)s",
        'name != %(exclude)s',
    ]
    params = {
        'construction': shared['construction'], 'blend': shared['blend'],
        'gsm': cint(shared['gsm']), 'shade': variant.get('shade_cat'),
        'mnc': variant.get('mechanical_chemical_finish') or '',
        'exclude': exclude or '',
    }
    names = frappe.db.sql(
        f'''select name from `tab{DOCTYPE_FABRIC_MASTER}`
            where {' and '.join(conditions)} order by name limit {cint(limit)}''',
        params,
    )
    for (name,) in names:
        fabric = costing._get_fabric(name)
        if fabric and not _yarn_unpriced(costing._calculate_yarn_costs(fabric)):
            return fabric
    return None


def _spec_variants(baseline, shorthand=None, exclude_id=None):
    '''
    The distinct spec signatures sharing the baseline's construction, blend and
    GSM -- one representative row each.

    A signature is (shade category, mech/chem finish, double-dyed, width). Those
    are exactly the row properties `costing.calculate()` prices; two rows
    agreeing on all four cost the same, so costing both would only produce a
    duplicate suggestion under a different fabric code. `min(name)` keeps the
    representative stable between calls.

    `shorthand` narrows the set to the baseline's own `old_fabric_construction`.
    The English `fabric` column calls every rib "Rib", so without this a 2x2 rib
    would be offered 1x1 rib rows as if they were the same cloth -- they are not
    interchangeable in a collar, and the lever's whole claim is that the fabric
    does not change. The shorthand column keeps the comparison honest.
    '''
    conditions = ['fabric = %(construction)s', 'blend = %(blend)s', 'finish_gsm = %(gsm)s']
    params = {'construction': baseline['construction'], 'blend': baseline['blend'],
              'gsm': cint(baseline['gsm'])}
    if shorthand:
        conditions.append('old_fabric_construction = %(shorthand)s')
        params['shorthand'] = shorthand

    rows = frappe.db.sql(
        f'''
        select
            shade_cat,
            mechanical_chemical_finish,
            (grey_fabric_material_desc like 'DD %%'
             or grey_fabric_material_desc like 'MCD %%') as is_double,
            finish_width,
            count(*) as n,
            min(name) as sample
        from `tab{DOCTYPE_FABRIC_MASTER}`
        where {' and '.join(conditions)}
        group by shade_cat, mechanical_chemical_finish, is_double, finish_width
        order by n desc, sample asc
        ''',
        params, as_dict=True,
    )

    variants = []
    for row in rows:
        if row['sample'] == exclude_id:
            continue
        variants.append(row)
    return variants


# --- fast-path costing ------------------------------------------------------

def _cost_variant(variant, shared, print_type):
    '''
    One spec variant, costed without re-resolving anything that cannot differ.

    Returns a fabric dict in `costing._get_fabric()` shape with a cost_per_kg
    breakup, or None when the variant needed an llm call the budget cannot pay
    for.
    '''
    fabric = costing._get_fabric(variant['sample'])
    if not fabric:
        return None

    # If the representative row's own yarn has no rate but a sibling of the
    # same signature has one, quote the sibling. The two cost the same here --
    # yarn is held either way -- but only one of them survives being applied in
    # get_garment_cost, which prices an unrated yarn at zero. A row the user
    # can act on beats a row that needs a warning.
    if cint(variant.get('n')) > 1 \
            and _yarn_unpriced(costing._calculate_yarn_costs(fabric)):
        sibling = _priced_sibling(variant, shared, exclude=fabric['fabric_id'])
        if sibling:
            fabric = sibling

    breakup = {}
    total = 0.00

    # 01. yarn -- the row's own yarn, priced for real, with one guard.
    #
    # Yarn used to be held at the baseline's figure here, so that a suggestion
    # labelled "shade S -> W" could never smuggle a fibre-origin change in as
    # part of the saving. Right instinct, wrong mechanism: the suggestion quotes
    # a specific row, the user applies that row in get_garment_cost, and that
    # API prices the row's actual yarn -- so a held yarn meant the two screens
    # disagreed by exactly the yarn difference. Validated against real rows:
    # 163.53 suggested vs 194.83 applied, on the same fabric_id.
    #
    # Now the row's own yarn is priced, and when it differs from the baseline's
    # it is DECLARED as a yarn change (see _classify), so the fibre-origin
    # decision is visible instead of either hidden in the price or silently
    # neutralised out of it.
    #
    # The guard: `Yarn Rate` carries codes with latest_rate 0, and
    # _calculate_yarn_costs() scores those as free rather than failing --
    # Rs 300+/kg of fictional saving. An unpriced row falls back to the
    # baseline's yarn (the honest estimate for the same blend) and is flagged,
    # because get_garment_cost has no such guard and will contradict it.
    actual = costing._calculate_yarn_costs(fabric)
    if _yarn_unpriced(actual):
        fabric['yarn_rate_missing'] = True
        if not _yarn_unpriced(shared['yarn']):
            actual = dict(shared['yarn'])
            actual['held_from_baseline'] = True
    else:
        fabric['yarn_rate_missing'] = False
    breakup[HEAD_YARN] = actual
    total += costing._to_float(actual.get('cost_per_kg'))

    # 02. knitting -- keyed on construction, which is held fixed across every
    # candidate, so the baseline's figure is the answer by construction.
    breakup[HEAD_KNITTING] = dict(shared['knitting'])
    total += costing._to_float(breakup[HEAD_KNITTING].get('cost_per_kg'))

    # 03. dyes and chemicals -- `DNC Rate` lookup, the whole point of the lever.
    breakup[HEAD_DNC] = costing._get_dyes_and_chemicals_cost(
        fabric, fabric['dnc_cost'], print_type)
    total += breakup[HEAD_DNC]['cost_per_kg']

    # 04. mechanical & chemical finish -- memoised per code; an uncached code
    # costs one llm round trip, and the budget decides whether to spend it.
    mnc = _mnc_cost(fabric, shared)
    if mnc is None:
        return None
    breakup[HEAD_MNC] = mnc
    total += costing._to_float(mnc.get('cost_per_kg'))

    # 05. finishing charges -- pure rule maths, memoised on what it reads.
    breakup[HEAD_FINISHING] = _finishing_cost(fabric, shared)
    total += costing._to_float(breakup[HEAD_FINISHING].get('cost_per_kg'))

    fabric['cost_per_kg'] = {'breakup': breakup, 'gross_amount': round(total, 2)}
    net = total * (100 + fabric['loss_percent']) / 100
    fabric['cost_per_kg']['loss_amount'] = round(net - total, 2)
    fabric['cost_per_kg']['net_amount'] = round(net, 2)
    fabric['matching_rows'] = cint(variant.get('n'))
    fabric['print_type'] = print_type

    return fabric


def _mnc_cost(fabric, shared):
    '''
    The mechanical/chemical finish head for a fabric, memoised by finish code.

    Resolution order: the memo, then the decomposition already cached on this
    row, then any sibling row carrying a cached decomposition for the same code,
    and only then the llm -- which is what the budget guards. Returns None when
    the budget is spent, so the caller can drop the candidate instead of
    blocking on it.
    '''
    code = fabric.get('mechanical_chemical_finish') or ''
    if code in shared['mnc_memo']:
        return shared['mnc_memo'][code]

    processes = fabric.get('costing_mc_processes')

    if not processes and code:
        # A sibling may already carry the decomposition for this exact code.
        rows = frappe.db.sql(
            f'''select costing_mc_processes from `tab{DOCTYPE_FABRIC_MASTER}`
                where mechanical_chemical_finish = %s
                  and ifnull(costing_mc_processes, '') != ''
                limit 1''',
            (code,), as_dict=True,
        )
        if rows:
            processes = costing._parse_mc_processes(rows[0]['costing_mc_processes'])
            if processes:
                # Write the borrowed decomposition onto this row too. Without
                # it, get_garment_cost hitting this row later re-asks the llm
                # from scratch -- and that call can come back empty, pricing
                # the finish at zero and contradicting the figure quoted here.
                # Validated: same row, SILICON, Rs 10/kg here vs Rs 0 applied.
                # Seeding the row with the sibling's vetted answer makes both
                # paths read the same cache.
                costing._store_costing_value(
                    fabric['fabric_id'], 'costing_mc_processes',
                    json.dumps(processes))

    if not processes:
        if shared['llm_spent'] >= shared['llm_budget']:
            return None
        shared['llm_spent'] += 1

    cost = costing._get_mechanical_chemical_process_cost(code, processes)

    if not processes:
        matched = costing._matched_mc_processes(cost)
        if matched:
            costing._store_costing_value(
                fabric['fabric_id'], 'costing_mc_processes', json.dumps(matched))

    shared['mnc_memo'][code] = cost
    return cost


def _finishing_cost(fabric, shared):
    '''
    The finishing-charges head, memoised on everything the rules actually read:
    blend, GSM, width, and the two grey-code markers. `Finishing Charge Rule` is
    a four-row table re-read per call otherwise.
    '''
    grey = costing._grey_code(fabric)
    key = (fabric.get('blend'), cint(fabric.get('gsm')), cint(fabric.get('fabric_width')),
           'WFL' in grey, 'VER' in grey)
    if key in shared['finishing_memo']:
        return shared['finishing_memo'][key]

    cost = costing._get_finishing_charges(fabric)
    shared['finishing_memo'][key] = cost
    return cost


def _reprint(baseline, candidate_print):
    ''' The baseline row recosted under a different print type. Nothing but the
    dyes-and-chemicals head can move, so it is a copy plus one lookup. '''
    fabric = json.loads(json.dumps(baseline, default=str))
    fabric['gsm'] = baseline['gsm']
    fabric['loss_percent'] = baseline['loss_percent']
    _reprice_dnc(fabric, candidate_print)
    fabric['print_type'] = candidate_print
    fabric['matching_rows'] = 1
    return fabric


def _reprice_dnc(fabric, print_type):
    ''' Recomputes the dyes-and-chemicals head in place under `print_type` and
    re-derives the per-kg totals around it. '''
    cpk = fabric.get('cost_per_kg') or {}
    breakup = cpk.get('breakup') or {}

    dnc_cost = fabric.get('dnc_cost')
    if not dnc_cost:
        return

    breakup[HEAD_DNC] = costing._get_dyes_and_chemicals_cost(fabric, dnc_cost, print_type)

    total = 0.00
    for key in costing.FABRIC_HEAD_KEYS:
        total += costing._to_float((breakup.get(key) or {}).get('cost_per_kg'))

    cpk['breakup'] = breakup
    cpk['gross_amount'] = round(total, 2)
    net = total * (100 + costing._to_float(fabric.get('loss_percent'))) / 100
    cpk['loss_amount'] = round(net - total, 2)
    cpk['net_amount'] = round(net, 2)
    fabric['cost_per_kg'] = cpk


# --- suggestion assembly ----------------------------------------------------

def _build_suggestion(baseline, candidate, base_net, kg, display_base_pp,
                      base_print):
    '''
    One costed candidate -> a suggestion, or None when it is not cheaper.

    The saving is the difference between two fabrics costed identically, so any
    manual adjustment on the caller's own figure cancels out of it; that is what
    lets the suggested cost be quoted against their number rather than ours.
    '''
    if not candidate:
        return None

    cand_net = _net_per_kg(candidate)
    saving_per_kg = base_net - cand_net
    if saving_per_kg <= 0:
        return None

    saving_pp = round(saving_per_kg * kg, 2)
    if saving_pp <= 0:
        return None

    changes, lever, risk, buyer = _classify(baseline, candidate, base_print)
    if not changes:
        return None

    base_heads = _head_costs(baseline)
    cand_heads = _head_costs(candidate)
    head_deltas = {
        key: round(base_heads.get(key, 0.0) - cand_heads.get(key, 0.0), 2)
        for key in costing.FABRIC_HEAD_KEYS
        if abs(base_heads.get(key, 0.0) - cand_heads.get(key, 0.0)) >= 0.01
    }

    saving_percent = round((saving_pp / display_base_pp * 100), 2) if display_base_pp else 0.0
    reason = _reason(head_deltas, lever)
    yarn_missing = bool(candidate.get('yarn_rate_missing'))
    if yarn_missing:
        reason += (' CAUTION: this row\'s own yarn has no rate in Yarn Rate, so this '
                   'price carries the current fabric\'s yarn cost. The costing screen '
                   'will show a much lower figure for this row -- that figure prices '
                   'the yarn at zero and is wrong, not cheaper. Fix the Yarn Rate '
                   'entry, or pick a sibling row of the same spec.')
    return {
        'lever_group': LEVER_SPEC,
        'lever': lever,
        # Kept for _shortlist's reserved seat, stripped before the response.
        '_risk': risk,
        'changes': changes,
        **_identity(candidate),
        'is_double_dyed': costing._is_double_dnc(candidate),
        'print_type': _print_label(candidate.get('print_type')),
        'yarn_rate_missing': yarn_missing,
        'cost_per_kg': round(cand_net, 2),
        'cost_per_piece': round(display_base_pp - saving_pp, 2),
        'saving_per_kg': round(saving_per_kg, 2),
        'saving_per_piece': saving_pp,
        'saving_percent': saving_percent,
        'head_deltas': head_deltas,
        'matching_rows': cint(candidate.get('matching_rows')),
        'reason': reason,
    }


def _fabric_suggestions(baseline, shared, print_type, grams, display_base_pp,
                        base_net, garment_style, section,
                        gsm_tolerance_percent, min_similarity, notes):
    '''
    Lever B -- a different fabric, same kind of cloth.

    Holds the knit structure and varies blend and weight. Candidates are priced
    by costing.calculate() itself -- see _cost_fabric_candidate() -- so the
    quoted cost is exactly what get_garment_cost returns when the suggestion is
    applied. That parity means the candidate is priced in whatever shade
    calculate() picks for its triple; a shade difference from the baseline is
    declared in `changes`, not neutralised.

    Unlike Lever A, yarn is priced for real here -- a blend change IS a yarn
    change, so it is the whole point. A candidate whose yarn cannot be priced is
    dropped rather than assumed, because assuming it would hide the one number
    the suggestion turns on.
    '''
    root = _structure_root(shared.get('shorthand'))
    if not root:
        notes.append(
            'No mill construction code on the baseline row, so a fabric swap '
            'cannot be scoped to the same knit structure. Spec suggestions only.'
        )
        return []

    rule = _garment_rule(garment_style, section)
    base_gsm = cint(baseline['gsm'])
    low, high, band_source = _gsm_band(base_gsm, garment_style, section,
                                       gsm_tolerance_percent, rule)
    notes.append(band_source)
    needs_stretch = bool(rule and cint(rule.get('requires_stretch')))

    # Grouped to the triple and nothing finer. The candidate used to be a
    # hand-picked representative row (same shade preferred, same finish
    # preferred); pricing now goes through costing.calculate(), which picks its
    # own row for a triple exactly as get_garment_cost will, so choosing one
    # here would only disagree with it.
    rows = frappe.db.sql(
        f'''
        select fabric, blend, finish_gsm, count(*) as n
        from `tab{DOCTYPE_FABRIC_MASTER}`
        where substring_index(ifnull(old_fabric_construction, ''), ' ', 1) = %(root)s
          and finish_gsm between %(low)s and %(high)s
          and not (blend = %(blend)s and finish_gsm = %(gsm)s)
        group by fabric, blend, finish_gsm
        order by n desc, fabric, blend, finish_gsm
        ''',
        {'root': root, 'low': low, 'high': high,
         'blend': baseline['blend'], 'gsm': base_gsm},
        as_dict=True,
    )
    if not rows:
        return []

    by_triple = {(row['fabric'], row['blend'], cint(row['finish_gsm'])): row
                 for row in rows}

    target = fabric_matcher._normalise_target({
        'construction': baseline['construction'],
        'construction_code': shared.get('shorthand'),
        'blend': baseline['blend'],
        'gsm': base_gsm,
    })

    floor = costing._to_float(min_similarity) if min_similarity is not None \
        else DEFAULT_MIN_SIMILARITY

    out = []
    below_floor = 0
    unpriced = 0
    not_cheaper = 0
    costed = 0
    truncated = 0
    for (construction, blend, gsm), row in by_triple.items():
        scored = _score_fabric_candidate(target, construction, blend, gsm,
                                         shared.get('shorthand'),
                                         requires_stretch=needs_stretch)
        if scored is None:
            continue
        if scored['score'] < floor:
            below_floor += 1
            continue

        # Costing now runs through calculate(), which is DB reads plus possible
        # llm resolution per triple -- bounded here as Lever C already bounds
        # its own pool. Iteration order is by catalogue prevalence, so what
        # gets cut is the thin tail.
        if costed >= MAX_CROSS_FAMILY_COSTED:
            truncated += 1
            continue
        costed += 1

        candidate = _cost_fabric_candidate(construction, blend, gsm,
                                           shared, print_type)
        if candidate is None:
            unpriced += 1
            continue
        candidate['matching_rows'] = cint(row['n'])

        entry = _build_fabric_suggestion(
            baseline, candidate, base_net, grams, display_base_pp,
            print_type, scored)
        if entry:
            out.append(entry)
        else:
            not_cheaper += 1

    if below_floor:
        notes.append(
            f'{below_floor} fabric(s) in the weight band were rejected as too '
            f'different (similarity under {floor:.0f}).'
        )
    if unpriced:
        notes.append(
            f'{unpriced} fabric(s) were dropped: their yarn has no usable price '
            'in Yarn Rate, and a blend change cannot be costed without it.'
        )
    if not_cheaper:
        notes.append(
            f'{not_cheaper} suitable fabric(s) were costed and came out dearer '
            'than the current one, so they are not offered.'
        )

    out.sort(key=lambda s: (-s['saving_per_piece'], s['fabric_id']))
    return out


def _family_suggestions(baseline, shared, print_type, grams, display_base_pp,
                        base_net, garment_style, section,
                        gsm_tolerance_percent, min_similarity,
                        min_substitutability, notes):
    '''
    Lever C -- a related construction.

    Crosses the knit family, which is the largest change the optimiser makes and
    the one with the least data behind it. Three things keep it honest:
    RELATED_FAMILIES decides which crossings are even considered, the weight
    band is enforced, and recovery-critical sections are refused outright.
    Pricing goes through costing.calculate(), exactly as in Lever B.

    Nothing here is ever "Safe". A different knit is a design decision that
    happens to save money, not a costing decision.
    '''
    target_family = fabric_matcher._normalise_target({
        'construction': baseline['construction'],
        'construction_code': shared.get('shorthand'),
        'blend': baseline['blend'],
        'gsm': cint(baseline['gsm']),
    })

    if not target_family['family']:
        notes.append(
            f'Construction "{baseline["construction"]}" is not placed in any knit '
            'family, so a related-construction swap cannot be scored. Skipped.'
        )
        return []

    rule = _garment_rule(garment_style, section)

    # A rule may always forbid a construction change. It may only *permit* one
    # against the name-reading guard when it was written about this section
    # specifically.
    #
    # A rule on section "Any" says nothing about collars -- it is the garment's
    # default, written with the body in mind, and every seeded rule is one.
    # Letting it override the guard is how "T-shirt-Any permits construction
    # changes" turned into "the collar may be swapped for a jersey", which is
    # the exact failure the guard exists to stop. A row naming the section is a
    # considered decision about that section and does outrank the keyword list.
    if rule is not None and not cint(rule.get('allow_family_change')):
        notes.append(
            f'{DOCTYPE_GARMENT_RULE} "{rule["name"]}" does not permit a '
            f'construction change on this section.'
        )
        return []

    section_specific = bool(rule and section and rule.get('section') == section)
    if not section_specific:
        blocked = _recovery_critical(section)
        if blocked:
            where = (f'Rule "{rule["name"]}" covers every section of this style '
                     f'rather than this one' if rule else
                     f'No reviewed {DOCTYPE_GARMENT_RULE} covers this section')
            notes.append(
                f'Section "{section}" holds the garment\'s shape, so its construction '
                f'is not substituted. A {target_family["family"]} {blocked} recovers '
                f'because of how it is knitted, and no related family does that. '
                f'({where} -- add a "{garment_style}-{section}" rule to override.)'
            )
            return []

    allowed = set(rule.get('allowed_families') or []) if rule else set()

    base_gsm = cint(baseline['gsm'])
    low, high, _ = _gsm_band(base_gsm, garment_style, section,
                             gsm_tolerance_percent, rule)

    # Triples only -- costing.calculate() picks the row, as in Lever B.
    rows = frappe.db.sql(
        f'''
        select fabric, blend, finish_gsm, count(*) as n
        from `tab{DOCTYPE_FABRIC_MASTER}`
        where finish_gsm between %(low)s and %(high)s
          and ifnull(fabric, '') != ''
          and fabric != %(construction)s
        group by fabric, blend, finish_gsm
        order by n desc, fabric, blend, finish_gsm
        ''',
        {'low': low, 'high': high,
         'construction': baseline['construction']},
        as_dict=True,
    )
    if not rows:
        return []

    floor = costing._to_float(min_similarity) if min_similarity is not None \
        else DEFAULT_MIN_SIMILARITY_CROSS
    min_sub = costing._to_float(min_substitutability) if min_substitutability is not None \
        else DEFAULT_MIN_SUBSTITUTABILITY

    # Score everything first, cost only what survives -- scoring is arithmetic,
    # costing is several DB reads per candidate.
    scored_rows = []
    below_floor = 0
    not_permitted = 0
    for row in rows:
        scored = _score_fabric_candidate(
            target_family, row['fabric'], row['blend'], cint(row['finish_gsm']),
            shared.get('shorthand'), allow_cross_family=True,
            min_substitutability=min_sub,
            requires_stretch=bool(rule and cint(rule.get('requires_stretch'))))
        if scored is None:
            continue
        # An explicit list narrows the substitutability graph; an empty one
        # leaves the graph as the only opinion. Matched on the catalogue's own
        # construction name rather than on the knit family, because that is what
        # the rule names and it is the finer of the two -- a rule can permit
        # Interlock without thereby permitting everything interlock-like.
        if allowed and row['fabric'] not in allowed:
            not_permitted += 1
            continue
        # Floored on blend and weight alone; the family crossing is already
        # gated by min_substitutability above.
        if scored['blend_weight'] < floor:
            below_floor += 1
            continue
        scored_rows.append((scored, row))

    scored_rows.sort(key=lambda pair: (
        -pair[0]['score'],
        -cint(pair[1]['n']),
        pair[1]['fabric'], pair[1]['blend'], cint(pair[1]['finish_gsm']),
    ))

    truncated = max(0, len(scored_rows) - MAX_CROSS_FAMILY_COSTED)
    out = []
    unpriced = 0
    not_cheaper = 0
    for scored, row in scored_rows[:MAX_CROSS_FAMILY_COSTED]:
        candidate = _cost_fabric_candidate(
            row['fabric'], row['blend'], cint(row['finish_gsm']),
            shared, print_type)
        if candidate is None:
            unpriced += 1
            continue
        candidate['matching_rows'] = cint(row['n'])
        entry = _build_fabric_suggestion(
            baseline, candidate, base_net, grams, display_base_pp,
            print_type, scored, lever=LEVER_FAMILY)
        if entry:
            out.append(entry)
        else:
            not_cheaper += 1

    if not_permitted:
        notes.append(
            f'{not_permitted} related-construction candidate(s) rejected: their '
            f'family is not in the Allowed Families list on '
            f'{DOCTYPE_GARMENT_RULE} "{rule["name"]}".'
        )
    if below_floor:
        notes.append(
            f'{below_floor} related-construction candidate(s) rejected: blend and '
            f'weight agreement under {floor:.0f}. A further set was rejected for '
            f'substitutability under {min_sub}.'
        )
    if unpriced:
        notes.append(
            f'{unpriced} related-construction candidate(s) dropped: yarn not '
            'priced in Yarn Rate.'
        )
    if not_cheaper:
        notes.append(
            f'{not_cheaper} related construction(s) passed every gate but cost '
            'more than the current fabric, so they are not offered.'
        )
    if truncated:
        notes.append(
            f'{truncated} lower-scoring related construction(s) were not costed '
            f'(pool capped at {MAX_CROSS_FAMILY_COSTED}).'
        )

    out.sort(key=lambda s: (-s['saving_per_piece'], s['fabric_id']))
    return out


def _recovery_critical(section):
    '''
    The word in a section name that makes its construction non-negotiable, or
    None.

    Sections are free text -- the 39 techpacks on file spell the neck seven ways
    ("Neck Tape", "Neckband", "Neck Band", "Neck Binding", "Neck Rib", "Neck
    Trim", "Neck Trim (Collar)") -- so this reads the name rather than matching
    it. Over-matching is the safe direction: the cost of wrongly protecting a
    section is one missed suggestion, and the cost of wrongly substituting a
    collar is a garment that will not hold its shape.
    '''
    text = (section or '').strip().lower()
    if not text:
        return None
    for word in RECOVERY_CRITICAL_WORDS:
        if word in text:
            return word
    return None


def _structure_root(shorthand):
    '''
    The knit structure a mill code names, without its yarn/finish qualifiers:
    "RIB_1X1 EL COMPACT" -> "RIB_1X1", "SJY COMPACT" -> "SJY".

    This is what a fabric swap must hold. Matching the full code instead would
    block the swaps worth making -- adding elastane changes both the blend and
    the code -- while matching only the English name would let a 1x1 rib be
    replaced by a 2x2. The first token is exactly the structure and nothing else.
    '''
    text = (shorthand or '').strip()
    if not text:
        return None
    return text.split(' ')[0].upper()


def _garment_rule(garment_style, section):
    '''
    The reviewed `Garment Fabric Rule` governing this section, or None.

    A section-specific row wins over the style's "Any" row. Unreviewed rows are
    invisible here: the table is seeded with drafts carrying *proposed* weight
    bands, and a proposal read as policy would widen every suggestion on nobody's
    authority. Ticking Reviewed is the whole act of adopting a rule.
    '''
    if not garment_style or not frappe.db.exists('DocType', DOCTYPE_GARMENT_RULE):
        return None

    fields = ['name', 'section', 'min_gsm', 'max_gsm', 'requires_stretch',
              'allow_family_change', 'notes']
    for wanted in ([section, 'Any'] if section and section != 'Any' else ['Any']):
        rule = frappe.db.get_value(
            DOCTYPE_GARMENT_RULE,
            {'garment_style': garment_style, 'section': wanted, 'is_reviewed': 1},
            fields, as_dict=True,
        )
        if rule:
            rule['allowed_families'] = frappe.db.sql_list(
                '''select family from `tabGarment Fabric Rule Family`
                   where parent = %s''', rule['name'])
            return rule
    return None


def _gsm_band(base_gsm, garment_style, section, tolerance_percent, rule=None):
    '''
    The weight range a candidate may sit in, as (low, high, note).

    A reviewed `Garment Fabric Rule` is the real answer -- it knows a hoody body
    cannot be 150 gsm however cheap. Without one the band is a percentage of the
    current weight, which needs no garment knowledge to defend: nothing is
    reclassified by a 10% weight change. The note says which was used, so a wide
    suggestion is never mistaken for a sanctioned one.
    '''
    if rule and cint(rule.get('min_gsm')) and cint(rule.get('max_gsm')):
        return (cint(rule['min_gsm']), cint(rule['max_gsm']),
                f'Weight band {rule["min_gsm"]}-{rule["max_gsm"]} gsm, from the reviewed '
                f'{DOCTYPE_GARMENT_RULE} "{rule["name"]}".')

    tol = costing._to_float(tolerance_percent)
    if tol <= 0:
        tol = DEFAULT_GSM_TOLERANCE_PERCENT
    low = int(base_gsm * (1 - tol / 100.0))
    high = int(base_gsm * (1 + tol / 100.0))
    reason = ('No reviewed rule covers it' if rule is None
              else f'"{rule["name"]}" sets no weight band')
    return (low, high,
            f'Weight held within {tol:.0f}% of {base_gsm} gsm ({low}-{high}). '
            f'{reason}, so the band is relative rather than what the garment '
            'actually allows.')


def _score_fabric_candidate(target, construction, blend, gsm, base_shorthand,
                            allow_cross_family=False,
                            min_substitutability=DEFAULT_MIN_SUBSTITUTABILITY,
                            requires_stretch=False):
    '''
    How close a candidate fabric is to the baseline, 0-100.

    Mirrors fabric_matcher._score_combo() -- same weights, same composition
    scorer, same stretch rule -- but scores one exact weight rather than the
    nearest weight a whole combination is stocked in. Returns None when a gate
    rejects the candidate outright.

    `allow_cross_family` picks the lever. Off (Lever B), a candidate from another
    family is rejected outright. On (Lever C), it must instead clear
    `min_substitutability` on RELATED_FAMILIES, and a same-family candidate is
    rejected as belonging to Lever B -- so the two never return the same swap
    twice under different headings.
    '''
    combo_classified = sr._classify_construction(construction)
    combo = {
        'construction': construction,
        'family': combo_classified['family'],
        'ratio': combo_classified['ratio'],
        'tags': sorted(combo_classified['tags']),
        'composition': sr._parse_brief_blend(blend) or fabric_matcher._parse_any_blend(blend),
    }

    # Yardage and findings are different kinds of thing, never substitutes.
    if fabric_matcher._is_trim(target['family']) != fabric_matcher._is_trim(combo['family']):
        return None
    if fabric_matcher._is_trim(combo['family']):
        return None

    # Which lever this candidate belongs to. Same family is a blend/weight
    # change (B); a different family is a construction change (C). Each rejects
    # the other's candidates so nothing is offered twice.
    same_family = bool(target['family']) and target['family'] == combo['family']
    substitutability = 1.0 if same_family else sr.RELATED_FAMILIES.get(
        frozenset((target['family'], combo['family'])), 0.0)

    if allow_cross_family:
        if same_family:
            return None
        if substitutability < min_substitutability:
            return None
    elif not same_family:
        return None

    construction_score = fabric_matcher._construction_score(target, combo)
    composition_score = sr._composition_score(target['composition'], combo['composition'])
    gsm_score = 100.0 * max(
        0.0, 1.0 - abs(cint(target['gsm']) - cint(gsm)) / float(fabric_matcher.GSM_TOLERANCE))

    score = (construction_score * W_CONSTRUCTION
             + composition_score * W_COMPOSITION
             + gsm_score * W_GSM)

    # Stretch is a hard gate, not a penalty.
    #
    # fabric_matcher scales the score by 0.85 for a lost elastane, which is right
    # when ranking near-misses -- something must still come back. Here nothing
    # has to: 95/5 cotton-elastane against 100% cotton scores 95 on composition
    # (they share 95% of their fibre), so 0.85 leaves it at 83 and it arrives as
    # the top recommendation. Measured, not assumed: that is exactly what it did.
    # A fabric specced with elastane was specced with it for a reason, and taking
    # it out changes what the garment does rather than what it costs.
    target_el = target['composition'].get('elastane', 0.0)
    combo_el = combo['composition'].get('elastane', 0.0)
    if target_el >= sr.STRETCH_REQUIRED_PCT and combo_el <= 0:
        return None

    # A reviewed rule can require stretch the current fabric does not itself
    # have -- the section may be specced to gain it, or the baseline may be the
    # thing being corrected.
    if requires_stretch and combo_el < sr.STRETCH_REQUIRED_PCT:
        return None

    # Adding stretch where there was none is a real change but not a loss of
    # function, so it is priced rather than gated -- and flagged in `changes`.
    stretch_penalty = 1.0
    if combo_el >= sr.STRETCH_REQUIRED_PCT and target_el <= 0:
        stretch_penalty = sr.EXTRA_STRETCH_PENALTY
    score *= stretch_penalty

    # Blend and weight agreement on their own, with the construction term taken
    # out. Crossing a family already costs construction points, so flooring a
    # cross-family candidate on the combined score charges it for the crossing
    # twice -- and it is charged once already, by the substitutability gate. At
    # a 0.50 pairing the combined score cannot exceed 80 even with a perfect
    # blend and weight, so a floor of 75 admits only exact blend matches and
    # Lever C returns nothing. This is what the cross-family floor tests.
    blend_weight = ((composition_score * W_COMPOSITION + gsm_score * W_GSM)
                    / (W_COMPOSITION + W_GSM))

    return {
        'score': round(score, 1),
        'blend_weight': round(blend_weight, 1),
        'family': combo['family'],
        'substitutability': round(substitutability, 2),
        'breakdown': {
            'construction': round(construction_score, 1),
            'composition': round(composition_score, 1),
            'gsm': round(gsm_score, 1),
            'stretch_penalty': stretch_penalty,
        },
        'gains_stretch': combo_el >= sr.STRETCH_REQUIRED_PCT and target_el <= 0,
    }


def _cost_fabric_candidate(construction, blend, gsm, shared, print_type):
    '''
    A candidate triple priced by `costing.calculate()` -- the same function
    `get_garment_cost` runs -- or None when it cannot be priced honestly.

    This lever used to cost candidates itself: the baseline's shade substituted
    in, the baseline's knitting reused, a hand-picked representative row. Every
    one of those choices was defensible in isolation and together they meant the
    quoted price could never be reproduced: a merchandiser who took the
    suggested fabric to `get_garment_cost` got a different number, because that
    API re-picks its own row for the triple (exact GSM, then DNC Rate priority)
    and prices it in that row's own shade. A suggestion whose price the costing
    API then contradicts is worse than no suggestion.

    So the pricing IS the costing API now: same row pick, same heads, same shade
    handling, memoised per triple. Whatever calculate() answers here is exactly
    what it will answer when the suggestion is applied. The row it picks --
    shade, finish, loss and all -- becomes the suggestion's identity, and any
    shade difference from the baseline is declared in `changes` rather than
    neutralised.

    Yarn is still guarded: `Yarn Rate` scores a missing or zero-rated code as
    free, which would show the entire yarn head as a saving, so an unpriced
    candidate is dropped rather than offered.
    '''
    key = (construction, blend, cint(gsm), _clean_print_type(print_type))
    if key in shared['calc_memo']:
        return shared['calc_memo'][key]

    # calculate() resolves the knitting code and any uncached finish code
    # through Claude on every call -- that path never reads the codes cached on
    # the fabric master row. One garment section at a time, as get_garment_cost
    # uses it, that is a tolerable few seconds; a candidate sweep asking the
    # identical "which knitting code is Single Jersey" question twenty times
    # over is ten minutes of wall clock for one answer. So the two resolvers
    # are memoised for the duration of this one call: the same answers
    # calculate() would get, asked once. Restored immediately -- gunicorn
    # workers serve one request at a time, so the window is this call only.
    orig_knit = costing._get_matching_knitting
    orig_mnc = costing._get_matching_mnc_proccesses
    knit_memo = shared.setdefault('knit_llm_memo', {})
    mnc_memo = shared.setdefault('mnc_llm_memo', {})

    def memo_knit(c, _orig=orig_knit, _m=knit_memo):
        if c not in _m:
            _m[c] = _cached_llm('knit', c, _orig)
        return _m[c]

    def memo_mnc(code, _orig=orig_mnc, _m=mnc_memo):
        if code not in _m:
            _m[code] = _cached_llm('mnc', code, _orig)
        return _m[code]

    costing._get_matching_knitting = memo_knit
    costing._get_matching_mnc_proccesses = memo_mnc
    try:
        result = costing.calculate(
            constructions=[construction], blends=[blend], gsm=cint(gsm),
            print_type=print_type)
    finally:
        costing._get_matching_knitting = orig_knit
        costing._get_matching_mnc_proccesses = orig_mnc

    fabric = (result or {}).get('data') if (result or {}).get('status') else None
    if fabric:
        yarn = (fabric.get('cost_per_kg') or {}).get('breakup', {}).get(HEAD_YARN)
        if _yarn_unpriced(yarn):
            fabric = None
    if fabric:
        fabric['print_type'] = print_type

    shared['calc_memo'][key] = fabric
    return fabric


_LLM_CACHE_TTL = 24 * 60 * 60


def _cached_llm(kind, key, resolver):
    '''
    One llm resolution, cached in redis for a day.

    The request-level memo stops the same question being asked twice in one
    sweep; this stops it being asked again on the next request. A knitting code
    for "Single Jersey" or the decomposition of "ENZYME + GOLD FINISH" does not
    change between calls -- the vocabulary behind them changes on the order of
    the masters being edited, which the TTL comfortably covers. A None answer
    (llm failed or found nothing) is never cached, so a transient failure does
    not become a day of wrong answers.
    '''
    cache_key = f'cost_optimiser:llm:{kind}:{key}'
    cached = frappe.cache().get_value(cache_key)
    if cached is not None:
        return cached

    answer = resolver(key)
    if answer is not None:
        try:
            frappe.cache().set_value(cache_key, answer, expires_in_sec=_LLM_CACHE_TTL)
        except Exception:
            pass  # a cold cache is a slow answer, never a wrong one
    return answer


def _build_fabric_suggestion(baseline, candidate, base_net, grams, display_base_pp,
                             base_print, scored, lever=LEVER_FABRIC):
    '''
    A costed fabric candidate -> a suggestion, or None when it is not cheaper.

    Consumption is NOT rescaled with the weight. It was, once -- a lighter
    fabric does take fewer grams -- but the grams a garment takes is a marker
    and pattern question this engine has no data to answer, and a derived
    consumption presented next to real catalogue prices reads as equally real.
    The caller owns consumption; both sides are priced at the same
    `grams_per_piece` they sent, so `cost_per_piece` here is what
    `get_garment_cost` returns for this fabric at that consumption, exactly.
    '''
    base_gsm = cint(baseline['gsm'])
    cand_gsm = cint(candidate['gsm'])
    if not base_gsm or not cand_gsm:
        return None

    cand_net = _net_per_kg(candidate)
    kg = grams / 1000.0

    saving_pp = round((base_net - cand_net) * kg, 2)
    if saving_pp <= 0:
        return None

    changes = []
    if baseline['blend'] != candidate['blend']:
        changes.append(_change('blend', 'Blend', baseline['blend'], candidate['blend']))
    if scored.get('gains_stretch'):
        changes.append(_change('stretch', 'Stretch', 'None', 'Elastane'))

    # Two rows can carry the same blend string and still be knitted from
    # different yarn -- "60% Cotton 40% Polyester" says nothing about whether the
    # cotton is BCI, organic or FTO, and they price differently. Left unstated,
    # a Rs 73/kg yarn difference rides along inside a suggestion headlined
    # "190 gsm -> 180 gsm", and a merchandiser accepting it on the weight change
    # is unknowingly also accepting a change of fibre origin. Named here because
    # it is often the largest single component of the saving.
    yarn_change = _yarn_change(baseline, candidate)
    if yarn_change:
        changes.append(yarn_change)
        yarn_change = True

    if base_gsm != cand_gsm:
        changes.append(_change('gsm', 'Weight', base_gsm, cand_gsm, unit='gsm',
                               delta_percent=round((cand_gsm - base_gsm)
                                                   / base_gsm * 100, 1)))
    # The candidate is priced in its own row's shade -- whatever shade
    # calculate() lands on when this fabric is applied. A shade difference is
    # therefore part of what the caller would be accepting, and it is declared
    # like any other change rather than adjusted away.
    if baseline.get('shade_category') != candidate.get('shade_category'):
        changes.append(_change('shade_category', 'Shade category',
                               baseline.get('shade_category'),
                               candidate.get('shade_category')))
    if baseline['construction'] != candidate['construction']:
        changes.append(_change('construction', 'Construction',
                               baseline['construction'], candidate['construction']))
    base_finish = baseline.get('mechanical_chemical_finish') or None
    cand_finish = candidate.get('mechanical_chemical_finish') or None
    if base_finish != cand_finish:
        changes.append(_change('mechanical_chemical_finish', 'Finish',
                               base_finish, cand_finish))
    base_loss = round(costing._to_float(baseline.get('loss_percent')), 2)
    cand_loss = round(costing._to_float(candidate.get('loss_percent')), 2)
    if abs(base_loss - cand_loss) >= 0.01:
        changes.append(_change('loss_percent', 'Loss allowance',
                               base_loss, cand_loss, unit='%'))

    if not changes:
        return None

    base_heads = _head_costs(baseline)
    cand_heads = _head_costs(candidate)
    head_deltas = {
        key: round(base_heads.get(key, 0.0) - cand_heads.get(key, 0.0), 2)
        for key in costing.FABRIC_HEAD_KEYS
        if abs(base_heads.get(key, 0.0) - cand_heads.get(key, 0.0)) >= 0.01
    }

    # "Safe" has to mean the same thing it means in Lever A, where any finish
    # change is Review -- a finish alters how the cloth feels. A yarn change is
    # a change of fibre origin, which carries sourcing and compliance weight,
    # and a shade change is a commercial decision. None is safe however well
    # the fabric scores.
    changed_finish = base_finish != cand_finish
    same_shade = baseline.get('shade_category') == candidate.get('shade_category')
    risk = 'Review'
    if (lever == LEVER_FABRIC and scored['score'] >= 90
            and not scored['gains_stretch'] and same_shade
            and not changed_finish and not yarn_change):
        risk = 'Safe'

    return {
        'lever_group': lever,
        'lever': lever,
        '_risk': risk,
        'similarity': scored['score'],
        'substitutability': scored.get('substitutability'),
        'construction_family': scored.get('family'),
        'similarity_breakdown': scored['breakdown'],
        'changes': changes,
        **_identity(candidate),
        'is_double_dyed': costing._is_double_dnc(candidate),
        'print_type': _print_label(candidate.get('print_type')),
        'cost_per_kg': round(cand_net, 2),
        'cost_per_piece': round(display_base_pp - saving_pp, 2),
        'saving_per_kg': round(base_net - cand_net, 2),
        'saving_per_piece': saving_pp,
        'saving_percent': round(saving_pp / display_base_pp * 100, 2) if display_base_pp else 0.0,
        'head_deltas': head_deltas,
        'matching_rows': cint(candidate.get('matching_rows')),
        'reason': _fabric_reason(head_deltas, scored, lever),
    }


def _fabric_reason(head_deltas, scored, lever=LEVER_FABRIC):
    ''' One line of prose, built from the numbers rather than asked for. '''
    b = scored['breakdown']
    if lever == LEVER_FAMILY:
        head = (f'A {scored["family"]} in place of the current knit -- rated '
                f'{scored["substitutability"]:.2f} substitutable, '
                f'{b["composition"]:.0f}% fibre overlap at '
                f'{b["gsm"]:.0f}% weight agreement. ')
    else:
        head = (f'Same knit structure; {b["composition"]:.0f}% fibre overlap '
                f'at {b["gsm"]:.0f}% weight agreement. ')
    head += 'Priced exactly as the costing API prices this fabric. '
    if scored['gains_stretch']:
        head += 'Adds elastane the current fabric does not have. '
    biggest = max(head_deltas.items(), key=lambda kv: kv[1]) if head_deltas else None
    if biggest and biggest[1] > 0:
        head += f'The saving is {biggest[0].replace("_", " ")}, down Rs {biggest[1]:.2f}/kg.'
    return head.strip()


def _shortlist(spec, fabric, family, cap, notes):
    '''
    The best `cap` suggestions across all three levers, split back into their
    lists -> (spec, fabric, family, hidden count).

    Ranked on saving alone, with one seat reserved: if nothing "Safe" makes the
    cut, the best Safe suggestion takes the last place. Money alone fills a
    short list with finish and fabric changes -- they move more rupees than a
    shade change does, and every one of them alters the garment. The Safe option
    is the one that can be acted on today, so it is always visible.
    '''
    pool = list(spec) + list(fabric) + list(family)
    pool.sort(key=lambda s: (-s['saving_per_piece'], s['fabric_id']))

    if cap > 0 and len(pool) > cap:
        chosen = pool[:cap]
        if not any(s.get('_risk') == 'Safe' for s in chosen):
            safest = next((s for s in pool if s.get('_risk') == 'Safe'), None)
            if safest:
                chosen = chosen[:cap - 1] + [safest]
                notes.append(
                    'The last suggestion is the least-altering one, kept in place of a '
                    'higher-saving option: it leaves the fabric itself unchanged.'
                )
    else:
        chosen = pool

    hidden = len(pool) - len(chosen)
    keep = {id(s) for s in chosen}
    for entry in pool:
        entry.pop('_risk', None)
    return (
        [s for s in spec if id(s) in keep],
        [s for s in fabric if id(s) in keep],
        [s for s in family if id(s) in keep],
        hidden,
    )


def _collapse(suggestions):
    '''
    One suggestion per actionable change, not one per catalogue row.

    Spec variants are grouped by width as well as by shade / finish / dyeing
    pass, because width is part of what identifies a row -- but it does not move
    the cost at any width the catalogue actually holds. Left uncollapsed, a
    single idea ("shade S to G") arrives three times over, differing only in
    which roll width the representative came from, and fills the cap with
    itself.

    The cheapest member wins the group and inherits the whole group's row count,
    so `matching_rows` still says how much of the catalogue backs the suggestion.
    '''
    best = {}
    for entry in suggestions:
        key = (entry.get('shade_category'), entry.get('mechanical_chemical_finish'),
               entry.get('is_double_dyed'), entry.get('print_type'))
        current = best.get(key)
        if current is None or entry['saving_per_piece'] > current['saving_per_piece']:
            carried = (current or {}).get('matching_rows', 0)
            entry['matching_rows'] = entry.get('matching_rows', 0) + carried
            best[key] = entry
        else:
            current['matching_rows'] = current.get('matching_rows', 0) + entry.get('matching_rows', 0)
    return list(best.values())


def _change(field, label, old, new, **extra):
    ''' One difference, as a pair a caller can render without parsing prose. '''
    return {'field': field, 'label': label, 'old': old, 'new': new, **extra}


def _classify(baseline, candidate, base_print):
    '''
    What actually differs, as (changes, lever, risk, buyer). `risk` and `buyer`
    are internal: risk reserves the least-altering suggestion a seat in the
    shortlist, and neither reaches the response.

    Read off the rows rather than from what was varied, so a candidate that
    happens to differ in two things is described as differing in two things.
    '''
    changes = []
    levers = []

    base_shade = baseline.get('shade_category')
    cand_shade = candidate.get('shade_category')
    if base_shade != cand_shade:
        changes.append(_change('shade_category', 'Shade category',
                               base_shade, cand_shade))
        levers.append('shade')

    base_double = costing._is_double_dnc(baseline)
    cand_double = costing._is_double_dnc(candidate)
    if base_double != cand_double:
        changes.append(_change('is_double_dyed', 'Dyeing pass',
                               'Double' if base_double else 'Single',
                               'Double' if cand_double else 'Single'))
        levers.append('dyeing')

    base_mnc = baseline.get('mechanical_chemical_finish') or None
    cand_mnc = candidate.get('mechanical_chemical_finish') or None
    if base_mnc != cand_mnc:
        changes.append(_change('mechanical_chemical_finish', 'Finish',
                               base_mnc, cand_mnc))
        levers.append('finish')

    cand_print = candidate.get('print_type')
    if _clean_print_type(base_print) != _clean_print_type(cand_print):
        changes.append(_change('print_type', 'Print',
                               _print_label(base_print), _print_label(cand_print)))
        levers.append('print')

    # Loss allowance is stored per row and is applied on top of every head, so
    # two rows differing only in shade can still land a couple of rupees apart
    # from this alone. It is listed because without it the per-head deltas do
    # not reconcile to the total, and a merchandiser checking the arithmetic
    # would find it short.
    base_loss = round(costing._to_float(baseline.get('loss_percent')), 2)
    cand_loss = round(costing._to_float(candidate.get('loss_percent')), 2)
    if abs(base_loss - cand_loss) >= 0.01:
        changes.append(_change('loss_percent', 'Loss allowance',
                               base_loss, cand_loss, unit='%'))

    # Width is deliberately not listed as a change. It is carried in the
    # identity block, but the finishing rules only test above/below 40" and the
    # catalogue never goes below 76", so a differing width here costs nothing --
    # listing it would read as an action to take when it is only which roll the
    # representative row happened to come from.

    # The row's own yarn is priced (see _cost_variant), so two rows of the same
    # blend string can differ in fibre origin -- BCI vs organic vs FTO cotton --
    # and that difference is part of the money quoted. It is declared here so a
    # suggestion headlined "shade S -> G" cannot carry a yarn swap silently.
    yarn_changed = False
    if not candidate.get('yarn_rate_missing'):
        yarn_change = _yarn_change(baseline, candidate)
        if yarn_change:
            changes.append(yarn_change)
            yarn_changed = True

    lever = '+'.join(levers) if levers else 'spec'

    # A shade or dyeing change leaves the fabric untouched but is almost always
    # the buyer's call, so it is flagged rather than called risky. A finish or
    # print change alters how the garment performs, and a yarn change is a
    # sourcing and compliance decision.
    risky = any(l in ('finish', 'print') for l in levers) or yarn_changed
    buyer = any(l in ('shade', 'dyeing', 'print') for l in levers)

    return changes, lever, ('Review' if risky else 'Safe'), buyer


def _reason(head_deltas, lever):
    ''' One line of prose, built from the numbers rather than asked for. '''
    if lever == 'shade':
        head = 'The fabric is identical -- construction, blend, GSM and finish all hold. '
    elif lever == 'print':
        head = 'Same fabric row; only the print treatment changes. '
    else:
        head = 'Construction, blend and GSM all hold. '

    biggest = max(head_deltas.items(), key=lambda kv: kv[1]) if head_deltas else None
    if biggest:
        label = biggest[0].replace('_', ' ')
        head += f'The saving is {label}, down Rs {biggest[1]:.2f}/kg.'

    return head.strip()


# --- small helpers ----------------------------------------------------------

def _identity(fabric):
    ''' The fabric identity block, in the shape the costing responses use. '''
    return {
        'fabric_id': fabric.get('fabric_id'),
        'fabric_code': fabric.get('fabric_code'),
        'fabric_description': fabric.get('fabric_description'),
        'construction': fabric.get('construction'),
        'blend': fabric.get('blend'),
        'gsm': fabric.get('gsm'),
        'shade_category': fabric.get('shade_category'),
        'mechanical_chemical_finish': fabric.get('mechanical_chemical_finish'),
        'fabric_width': cint(fabric.get('fabric_width')),
        'loss_percent': round(costing._to_float(fabric.get('loss_percent')), 2),
    }


def _yarn_change(baseline, candidate):
    '''
    A one-line description of a yarn swap, or None when the yarn is the same.

    Compared on yarn code rather than on the blend string, because the blend
    string is what hides the difference: BCI, organic and FTO cotton all read as
    "Cotton" and price apart.
    '''
    base_yarns = ((baseline.get('cost_per_kg') or {}).get('breakup', {})
                  .get(HEAD_YARN) or {}).get('yarns') or []
    cand_yarns = ((candidate.get('cost_per_kg') or {}).get('breakup', {})
                  .get(HEAD_YARN) or {}).get('yarns') or []

    base_codes = sorted(str(y.get('code')) for y in base_yarns if y.get('code'))
    cand_codes = sorted(str(y.get('code')) for y in cand_yarns if y.get('code'))
    if not base_codes or not cand_codes or base_codes == cand_codes:
        return None

    base_desc = (base_yarns[0].get('description') or ', '.join(base_codes)).strip()
    cand_desc = (cand_yarns[0].get('description') or ', '.join(cand_codes)).strip()
    return _change('yarn', 'Yarn', base_desc, cand_desc,
                   old_code=', '.join(base_codes), new_code=', '.join(cand_codes))


def _yarn_unpriced(yarn):
    '''
    True when a yarn head cannot be trusted.

    Two different holes produce the same silent zero. A code missing from
    `Yarn Rate` is marked with an 'ERROR' key and contributes nothing; a code
    that is present but carries `latest_rate` 0 contributes nothing and is
    marked with nothing at all. Both have to count, because no yarn is actually
    free -- a zero is always an unpriced yarn, never a cheap one.
    '''
    if not yarn:
        return True
    yarns = yarn.get('yarns')
    if not yarns:
        return True
    if costing._to_float(yarn.get('cost_per_kg')) <= 0:
        return True
    return any(
        'ERROR' in (y or {}) or costing._to_float((y or {}).get('rate_per_kg')) <= 0
        for y in yarns
    )


def _net_per_kg(fabric):
    ''' The per-kg total after loss. `calculate()` names it total_fabric_cost and
    `_compute_fabric_cost()` names it net_amount; both are read. '''
    cpk = fabric.get('cost_per_kg') or {}
    return costing._to_float(cpk.get('net_amount') or cpk.get('total_fabric_cost'))


def _head_costs(fabric):
    ''' The five per-kg heads as plain floats. '''
    breakup = (fabric.get('cost_per_kg') or {}).get('breakup') or {}
    return {
        key: costing._to_float((breakup.get(key) or {}).get('cost_per_kg'))
        for key in costing.FABRIC_HEAD_KEYS
    }


def _clean_print_type(print_type):
    value = (print_type or '').strip().lower()
    return value if value in ('aop', 'digital') else None


def _print_label(print_type):
    return {'aop': 'AOP', 'digital': 'Digital'}.get(_clean_print_type(print_type), 'None')
