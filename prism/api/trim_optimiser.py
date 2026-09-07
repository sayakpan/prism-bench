'''
Trim optimiser -- "which trim does this brand use on this garment".

The sibling of cost_optimiser, and deliberately the opposite kind of engine.
cost_optimiser argues from the catalogue: it reprices real Fabric Master rows and
every number it quotes is one costing.calculate() would return. This one has no
catalogue to argue from. Which trim a brand puts on a garment is a positioning
judgement -- what its customer notices, what its price point can carry, how long
the piece is meant to last -- and none of that is in the database. So the whole
engine is a single llm call, and everything around it exists to make that call's
answer honest rather than to compute the answer itself.

What that means in practice:

    arithmetic     ours       cost = unit_price * units, the premium over the
                              cheapest option in the group, the totals. The model
                              may quote these; it never computes them.
    judgement      the llm's  which option, on which axis, and why.
    validation     ours       ids must exist, picks must stay inside their group,
                              a second pick in a group must do a different job,
                              and a reason that only restates the brand's tier is
                              thrown away.

The one rule the whole thing turns on: a choice is made WITHIN a trim group. The
groups are the garment's shopping list -- it needs a thread, it needs an elastic
-- and the question is only ever which row of a group to buy, never whether to
swap a group for another. A group whose options the garment does not need can be
skipped outright; that is a real answer and costs nothing to give.

Degradation is total. A missing api key, a rate limit or a malformed reply drops
the request to `_rules_pick()` -- cheapest in each group, with a reason that says
so -- and the response comes back with `source: "rules"`. This endpoint does not
have a failure mode that returns nothing.

Endpoint:
    POST /api/method/prism.api.trim_optimiser.suggest
    Header: X-Auth-Token: <jwt>            (required -- see auth_required)
'''

import hashlib
import json
import re

import frappe
from frappe.utils import cint

from prism.auth.authenticator import auth_required
import prism.api.llm as llm

# Informative, not enforced. An unknown garment type still answers -- the model
# knows what a gilet is -- so rejecting one would buy nothing and cost callers a
# 400 the first time the taxonomy grows.
GARMENT_TYPES = ('Pants', 'Dresses', 'Shorts', 'Shirts', 'Tshirt')

# The axes a pick may be decided on. Naming one is compulsory: it is what stops
# every group coming back with the same paragraph about brand positioning, and it
# gives the UI something to group and filter by.
AXIS_CONTACT = 'consumer_contact'
AXIS_POSITIONING = 'positioning'
AXIS_FUNCTION = 'function'
AXIS_COST = 'cost_discipline'
AXIS_DURABILITY = 'durability'
AXES = (AXIS_CONTACT, AXIS_POSITIONING, AXIS_FUNCTION, AXIS_COST, AXIS_DURABILITY)

# Prose caps, enforced by truncation rather than by rejection -- a slightly long
# but correct reason is worth more than no reason. The model is told these
# numbers, so a truncation is a sign the prompt is being ignored, not a routine
# outcome.
REASON_MAX_WORDS = 30
RATIONALE_MAX_WORDS = 25
BRAND_READ_MAX_WORDS = 60
WHY_NOT_MAX_WORDS = 20
ROLE_MAX_WORDS = 8

# One call carries every group, so the reply scales with the number of groups.
# Eight groups of four options is comfortably inside this.
MAX_TOKENS = 8000

# Reopening the Trims tab must not re-bill. Keyed on the trims themselves, so an
# edited unit price is a different question and misses the cache by construction.
CACHE_TTL_SEC = 24 * 60 * 60
CACHE_PREFIX = 'trim_optimiser'

# A pick past the first has to do a DIFFERENT job. Without this the model selects
# the whole group whenever two options are both defensible, which is not a
# recommendation -- it is the input handed back.
DEFAULT_MAX_PER_GROUP = 0  # 0 = unbounded; the distinct-role rule does the work

# Words that carry no information once the brand is already known. A "reason"
# built only from these is a restatement of the brief, not an argument, and is
# replaced with the rules reason. See _is_substantive().
_TIER_WORDS = frozenset((
    'brand', 'brands', 'premium', 'mid', 'market', 'fast', 'fashion', 'luxury',
    'positioning', 'positioned', 'aspirational', 'affordable', 'elevated',
    'tier', 'customer', 'customers', 'shopper', 'shoppers', 'consumer',
    'consumers', 'buyer', 'buyers', 'because', 'suits', 'fits', 'right',
    'appropriate', 'consistent', 'aligns', 'aligned', 'expects', 'expected',
))


@frappe.whitelist(allow_guest=True)
@auth_required
def suggest(brand=None, garment_type=None, trims=None, max_per_group=None, refresh=0):
    '''
    Which trim a brand should use, group by group.

    Input:
    {
        "brand":        <str>,      // "Zara" -- taken as given, not looked up
        "garment_type": <str>,      // "Pants" | "Dresses" | "Shorts" | "Shirts" | "Tshirt"
        "trims": [                  // the options to choose among
            { "id": <str>,          // opaque; echoed back on every pick
              "trim": <str>,        // "Filament Thread"
              "trim_group": <str>,  // "Thread" -- the choice is made inside this
              "unit_price": <float>,
              "units": <float>,
              "pcs_per_carton": <int>,   // carried through, not used
              "cost": <float> },         // recomputed, see below
            ...
        ],
        "max_per_group": <int>,     // optional, 0 (default) = unbounded
        "refresh":       <0|1>      // optional, bypass the cached answer
    }

    `cost` is always recomputed as unit_price * units. A figure sent that
    disagrees is not silently accepted and not silently dropped -- the computed
    one is used and the disagreement is listed in `notes`, so an upstream bug is
    visible on the response rather than buried in a total.

    A trim with no `trim_group` is not lumped in with the other loose trims:
    it becomes a group of its own, named after the trim, and the question for it
    is include-or-skip. Loose trims have nothing to be chosen between, and
    grouping them together would invent a competition that does not exist.

    Returns:
    {
        "status": True,
        "data": {
            "brand", "garment_type",
            "brand_read": <str>,          // the model's positioning stance, stated
                                          // once and held to across every group
            "groups": [
                { "trim_group": <str>,
                  "decision": "select" | "skip",
                  "selected": [ { "id", "trim", "cost", "role" } ],
                  "rejected": [ { "id", "trim", "cost", "why_not" } ],
                  "axis": <str>,          // one of AXES
                  "reason": <str>,        // what the choice DOES for the garment
                  "brand_rationale": <str>,
                  "tradeoff": <str>|None, // present whenever the pick is dearer
                                          // than the cheapest in its group
                  "cost_impact": { "selected_cost", "cheapest_in_group",
                                   "premium_vs_cheapest", "premium_percent" },
                  "source": "llm" | "rules" },
                ...
            ],
            "totals": { "recommended_trim_cost", "all_cheapest_cost",
                        "premium_vs_all_cheapest", "skipped_groups" },
            "notes": [ <str>, ... ],
            "source": "llm" | "rules"
        }
    }

    `all_cheapest_cost` sums the cheapest option only across groups that were
    actually selected in, so the premium is a like-for-like number: what the
    recommendation costs over buying the same shopping list at its floor. A
    skipped group contributes to neither side.
    '''
    try:
        brand = _clean(brand)
        garment_type = _clean(garment_type)
        if not brand:
            return {'status': False, 'error': 'brand is required.'}
        if not garment_type:
            return {'status': False, 'error': 'garment_type is required.'}

        rows = _parse_trims(trims)
        if rows is None:
            return {'status': False, 'error': 'trims must be a list of trim objects.'}
        if not rows:
            return {'status': False, 'error': 'trims is empty -- nothing to choose between.'}

        groups, notes = _build_groups(rows)
        if not groups:
            return {'status': False, 'error': 'No usable trims: every row is missing a name.'}

        cap = cint(max_per_group) if max_per_group is not None else DEFAULT_MAX_PER_GROUP

        #--- the one judgement call. Cached on the question, not on the answer's
        #--- shape, so tightening the validation below takes effect immediately
        #--- for callers already holding a cached pick.
        picks, from_cache = _cached_picks(brand, garment_type, groups, refresh=cint(refresh))

        data = _assemble(brand, garment_type, groups, picks, cap)
        data['notes'] = notes + data.get('notes', [])
        if from_cache:
            data['notes'].append('Recommendation served from cache; pass refresh=1 to re-ask.')

        return {'status': True, 'data': data}

    except Exception as ex:
        frappe.log_error(frappe.get_traceback(), 'trim_optimiser.suggest')
        return {'status': False, 'error': str(ex)}


# --- input normalisation ----------------------------------------------------

def _parse_trims(trims):
    ''' The `trims` argument -> a list of dicts, or None when it is neither. '''
    if isinstance(trims, str):
        try:
            trims = json.loads(trims or '[]')
        except Exception:
            return None
    if not isinstance(trims, list):
        return None
    return [t for t in trims if isinstance(t, dict)]


def _build_groups(rows):
    '''
    The flat trim list -> ordered groups, each with its options costed and each
    option's premium over the cheapest in that group. Returns (groups, notes).

    Group order follows first appearance, and option order follows the input, so
    the response reads in the same order as the caller's own screen.
    '''
    notes = []
    disagreed = []
    groups = []
    index = {}

    for position, row in enumerate(rows):
        name = _clean(row.get('trim'))
        if not name:
            continue

        unit_price = _to_float(row.get('unit_price'))
        units = _to_float(row.get('units'))
        cost = round(unit_price * units, 4)

        stated = _to_float(row.get('cost'))
        if stated and abs(stated - cost) > 0.01:
            disagreed.append(f'{name} (sent {stated:.2f}, computed {cost:.2f})')

        # An id is how a pick is referred to on the way back out. A row without
        # one still gets to compete, under a synthetic id, rather than being
        # dropped for a bookkeeping reason the caller did not know mattered.
        trim_id = _clean(row.get('id')) or f'_auto_{position}'

        group_name = _clean(row.get('trim_group')) or name

        option = {
            'id': trim_id,
            'trim': name,
            'unit_price': round(unit_price, 4),
            'units': round(units, 4),
            'pcs_per_carton': row.get('pcs_per_carton'),
            'cost': cost,
        }

        if group_name not in index:
            index[group_name] = {'trim_group': group_name, 'options': [], 'standalone': False}
            groups.append(index[group_name])
        index[group_name]['options'].append(option)

    for group in groups:
        options = group['options']
        # A group of one is not a choice between options -- it is a question
        # about whether the garment needs the thing at all. Flagged so the
        # prompt can ask that question instead of the comparison one.
        group['standalone'] = len(options) == 1

        cheapest = min((o['cost'] for o in options), default=0.0)
        group['cheapest_in_group'] = round(cheapest, 4)
        group['cheapest_trim'] = next(
            (o['trim'] for o in options if abs(o['cost'] - cheapest) < 1e-9), None)

        for option in options:
            premium = round(option['cost'] - cheapest, 4)
            option['premium_vs_cheapest'] = premium
            option['premium_percent'] = (
                round(premium / cheapest * 100.0, 2) if cheapest > 0 else 0.0)

    if disagreed:
        notes.append(
            'cost recomputed as unit_price x units; the figure sent disagreed for: '
            + ', '.join(disagreed[:6])
            + (f' and {len(disagreed) - 6} more.' if len(disagreed) > 6 else '.')
        )

    loose = [g['trim_group'] for g in groups if g['standalone']]
    if loose:
        notes.append(
            f'{len(loose)} group(s) hold a single option, so the question for them is '
            'include-or-skip rather than which: ' + ', '.join(loose[:6])
            + (f' and {len(loose) - 6} more.' if len(loose) > 6 else '.')
        )

    return groups, notes


# --- the judgement call -----------------------------------------------------

def _cached_picks(brand, garment_type, groups, refresh=0):
    '''
    _llm_pick() behind a cache keyed on the question. Returns (picks, from_cache).
    A cache miss that also fails the llm returns ({}, False) and the caller falls
    through to the rules.
    '''
    key = _cache_key(brand, garment_type, groups)
    cache = frappe.cache()

    if not refresh:
        try:
            hit = cache.get_value(key)
            if hit:
                parsed = json.loads(hit) if isinstance(hit, (str, bytes)) else hit
                if isinstance(parsed, dict) and parsed.get('groups'):
                    return parsed, True
        except Exception:
            pass  # a corrupt cache entry is a reason to re-ask, not to fail

    picks = _llm_pick(brand, garment_type, groups)

    if picks.get('groups'):
        try:
            cache.set_value(key, json.dumps(picks), expires_in_sec=CACHE_TTL_SEC)
        except Exception:
            pass  # answering matters; remembering the answer does not

    return picks, False


def _cache_key(brand, garment_type, groups):
    ''' Same brand, same garment, same trims at the same prices -> same key. '''
    payload = json.dumps({
        'brand': brand.lower(),
        'garment_type': garment_type.lower(),
        'groups': sorted(
            [g['trim_group'], sorted([o['id'], o['trim'], o['cost']] for o in g['options'])]
            for g in groups
        ),
    }, sort_keys=True, default=str)
    digest = hashlib.sha1(payload.encode('utf-8')).hexdigest()
    return f'{CACHE_PREFIX}:{digest}'


def _llm_pick(brand, garment_type, groups):
    '''
    One call, every group. Returns the raw parsed reply, or {} on any failure --
    which leaves the rules standing rather than costing the caller its answer.

    All groups go in one call because the trim budget is shared: a premium zip is
    paid for by pulling back on the label, and a per-group call cannot see that
    trade. It is also one round trip instead of N.
    '''
    try:
        system_prompt = _system_prompt(brand, garment_type, groups)
        user_prompt = (
            f'Choose the trims for a {garment_type} for {brand}, following the '
            'format and rules above. Write `brand_read` first, then hold to it.'
        )
        reply = llm.get_claude_response(system_prompt, user_prompt, 'dict',
                                        max_tokens=MAX_TOKENS)
        return reply if isinstance(reply, dict) else {}

    except Exception:
        frappe.log_error(frappe.get_traceback(), 'trim_optimiser._llm_pick()')
        return {}


def _system_prompt(brand, garment_type, groups):
    ''' The prompt is the product. Everything else in this module protects it. '''
    option_lines = []
    for group in groups:
        option_lines.append(f'[{group["trim_group"]}]')
        for option in group['options']:
            if group['standalone']:
                delta = 'the only option in this group'
            elif option['premium_vs_cheapest'] > 0:
                delta = (f'+Rs {option["premium_vs_cheapest"]:.2f}, '
                         f'+{option["premium_percent"]:.1f}% vs cheapest')
            else:
                delta = 'cheapest in group'
            option_lines.append(
                f'  {option["id"]}\t{option["trim"]}\tRs {option["cost"]:.2f}/pc\t({delta})'
            )
    options_block = '\n'.join(option_lines)

    valid_ids = ', '.join(o['id'] for g in groups for o in g['options'])

    standalone = [g['trim_group'] for g in groups if g['standalone']]
    standalone_rule = ''
    if standalone:
        standalone_rule = (
            '\n- These groups hold a single option, so the question is whether the garment '
            'needs it at all, not which to take: ' + ', '.join(standalone) + '.'
        )

    return f'''You are a trim merchandiser. You are given a brand, a garment type, and the trim options available in each trim group. For each group, choose which option(s) that brand should use on that garment -- one, several, or none -- and say why.

BRAND: {brand}
GARMENT: {garment_type}

# TRIM OPTIONS
Tab-separated: id, trim, cost per piece, how it compares to the cheapest option in its group.
The choice is made WITHIN a group. Never move a trim between groups and never compare across them.
---
{options_block}
---

# HOW TO DECIDE
Start from what this brand actually is: its price point, who buys it, what that customer notices and what they do not, how long the garment is expected to last, and what the brand's direct peers do. State that stance once in `brand_read`, in at most {BRAND_READ_MAX_WORDS} words, and then hold to it for every group -- a stance that would produce the same answer for a value retailer and a contemporary label is not a stance.

Then decide each group on exactly ONE of these axes, and name it in `axis`:
  {AXIS_CONTACT} -- will the customer see or feel this trim? Visible and hand-contact trims carry brand weight; buried trims carry cost weight. This axis decides most groups.
  {AXIS_POSITIONING} -- does this finish read as this brand's tier, on the shelf and in the hand?
  {AXIS_FUNCTION} -- does the garment need it to work at all? A waistband must recover; a fly must take load.
  {AXIS_COST} -- is the premium defensible at this price point, at this share of trim spend?
  {AXIS_DURABILITY} -- will it survive the wash life this brand's customer expects of this garment?

# OUTPUT
Return a single JSON object in exactly this shape:
{{
  "brand_read": "one paragraph, {BRAND_READ_MAX_WORDS} words maximum, this brand's positioning as it bears on trims",
  "groups": [
    {{
      "trim_group": "Thread",
      "selected": [
        {{"id": "1rnlh7et2r", "role": "overlock and coverstitch seams"}}
      ],
      "rejected": [
        {{"id": "1rnb7i8f37", "why_not": "concrete, {WHY_NOT_MAX_WORDS} words maximum"}}
      ],
      "axis": "{AXIS_CONTACT}",
      "reason": "ONE sentence, {REASON_MAX_WORDS} words maximum",
      "brand_rationale": "{RATIONALE_MAX_WORDS} words maximum, the brand angle specifically",
      "tradeoff": "what the dearer pick costs, or null when it is the cheapest"
    }}
  ]
}}

# RULES
- Return one entry for EVERY group listed above, in the same order. None may be omitted.
- Pick only from within the group. Copy ids verbatim. Valid ids: {valid_ids}. Drop any pick whose id is not in that list.
- Selecting nothing in a group is a valid answer when the garment genuinely does not need it -- return an empty `selected` and say why in `reason`.
- Selecting several is allowed, but EVERY pick past the first MUST carry a distinct `role` of at most {ROLE_MAX_WORDS} words naming the different job it does. Two options doing the same job is not an answer; if you cannot name a second job, select one.
- `role` describes what the trim does on this garment ("waistband casing", "topstitch"), not why it was chosen.
- A pick that is not the cheapest in its group MUST carry a `tradeoff` naming what the premium buys, in cash terms. Use null when the pick is the cheapest.
- `reason` must say what the choice DOES for the garment -- the finish it gives, the failure it avoids, the cost it earns back. A reason that only restates the brand's tier will be discarded. Do not name the brand's tier and stop there.
- `brand_rationale` is where the positioning argument goes. Keep it out of `reason`.
- Merchandiser language. No marketing copy, no adjective stacks.
- Do not invent facts about this brand's suppliers, catalogue, factories or certifications. Reason from positioning, which you may state as judgement, not from specifics you would have to have looked up.{standalone_rule}
- Output raw, valid JSON only: one object, double-quoted keys and strings, no trailing commas, no markdown fences, no commentary.
'''


# --- validation and assembly ------------------------------------------------

def _assemble(brand, garment_type, groups, picks, cap):
    '''
    The model's reply, checked against the input it was given, and every number
    recomputed here. A group the model omitted, or answered unusably, falls to
    _rules_pick() on its own -- one bad group does not cost the caller the rest.
    '''
    by_group = {}
    if isinstance(picks.get('groups'), list):
        for entry in picks['groups']:
            if isinstance(entry, dict) and _clean(entry.get('trim_group')):
                by_group.setdefault(_clean(entry.get('trim_group')), entry)

    notes = []
    out_groups = []
    used_llm = False

    for group in groups:
        entry = by_group.get(group['trim_group'])
        built = _build_group(group, entry, brand, garment_type, cap, notes) if entry else None
        if built is None:
            built = _rules_pick(group)
        else:
            used_llm = True
        out_groups.append(built)

    missing = [g['trim_group'] for g in groups if g['trim_group'] not in by_group]
    if missing and used_llm:
        notes.append(
            'No recommendation came back for: ' + ', '.join(missing[:6])
            + (f' and {len(missing) - 6} more' if len(missing) > 6 else '')
            + '; those groups fell back to the cheapest option.'
        )

    brand_read = _cap_words(_clean(picks.get('brand_read')), BRAND_READ_MAX_WORDS)
    if not used_llm:
        brand_read = ('No AI recommendation available; every group fell back to the '
                      'cheapest option in it.')

    return {
        'brand': brand,
        'garment_type': garment_type,
        'brand_read': brand_read,
        'groups': out_groups,
        'totals': _totals(out_groups),
        'notes': notes,
        'source': 'llm' if used_llm else 'rules',
    }


def _build_group(group, entry, brand, garment_type, cap, notes):
    '''
    One group's reply -> the response entry, or None when nothing usable survives
    and the group should fall to the rules.
    '''
    options = {o['id']: o for o in group['options']}

    #--- selections: inside the group, deduped, and each pick past the first
    #--- doing a job no earlier pick already does.
    selected = []
    seen_ids = set()
    seen_roles = set()
    for raw in (entry.get('selected') or []):
        if isinstance(raw, str):
            raw = {'id': raw}
        if not isinstance(raw, dict):
            continue
        trim_id = _clean(raw.get('id'))
        option = options.get(trim_id)
        if not option or trim_id in seen_ids:
            continue

        role = _cap_words(_clean(raw.get('role')), ROLE_MAX_WORDS)
        role_key = re.sub(r'[^a-z0-9 ]', '', (role or '').lower()).strip()
        if selected and (not role_key or role_key in seen_roles):
            notes.append(
                f'[{group["trim_group"]}] "{option["trim"]}" dropped: a second pick '
                'must do a different job, and none was named.'
            )
            continue

        if cap and len(selected) >= cap:
            notes.append(
                f'[{group["trim_group"]}] "{option["trim"]}" dropped: over the '
                f'max_per_group limit of {cap}.'
            )
            continue

        if role_key:
            seen_roles.add(role_key)
        selected.append({
            'id': option['id'],
            'trim': option['trim'],
            'cost': option['cost'],
            'role': role,
        })
        seen_ids.add(trim_id)

    #--- a reason that only restates the brief is not a reason. Losing it costs
    #--- the group its llm answer entirely, because the picks are only worth as
    #--- much as the argument for them.
    reason = _cap_words(_clean(entry.get('reason')), REASON_MAX_WORDS)
    if not _is_substantive(reason, brand, garment_type):
        notes.append(
            f'[{group["trim_group"]}] AI reason discarded: it restated the brand '
            'without saying what the choice does.'
        )
        return None

    #--- everything the model did not pick is a rejection, whether it said so or
    #--- not, so the UI can render a state against every option it offered.
    why_not = {}
    for raw in (entry.get('rejected') or []):
        if isinstance(raw, dict) and _clean(raw.get('id')) in options:
            why_not[_clean(raw['id'])] = _cap_words(_clean(raw.get('why_not')), WHY_NOT_MAX_WORDS)

    rejected = [{
        'id': option['id'],
        'trim': option['trim'],
        'cost': option['cost'],
        'why_not': why_not.get(option['id']),
    } for option in group['options'] if option['id'] not in seen_ids]

    selected_cost = round(sum(s['cost'] for s in selected), 4)
    cheapest = group['cheapest_in_group']
    premium = round(selected_cost - cheapest, 4) if selected else 0.0

    #--- a dearer pick without a stated tradeoff gets the arithmetic one. The
    #--- number is ours either way, so a silent premium is never returned.
    tradeoff = _cap_words(_clean(entry.get('tradeoff')), REASON_MAX_WORDS)
    if premium > 0.005:
        if not tradeoff:
            tradeoff = (f'Rs {premium:.2f}/pc over {group["cheapest_trim"]}, '
                        f'the cheapest in this group.')
    else:
        tradeoff = None

    axis = _clean(entry.get('axis')).lower().replace(' ', '_')

    return {
        'trim_group': group['trim_group'],
        'decision': 'select' if selected else 'skip',
        'selected': selected,
        'rejected': rejected,
        'axis': axis if axis in AXES else None,
        'reason': reason,
        'brand_rationale': _cap_words(_clean(entry.get('brand_rationale')), RATIONALE_MAX_WORDS),
        'tradeoff': tradeoff,
        'cost_impact': {
            'selected_cost': selected_cost,
            'cheapest_in_group': cheapest,
            'premium_vs_cheapest': premium,
            'premium_percent': (round(premium / cheapest * 100.0, 2)
                                if (selected and cheapest > 0) else 0.0),
        },
        'source': 'llm',
    }


def _rules_pick(group):
    '''
    The floor answer for one group: its cheapest option, and a reason that says
    exactly that. Used when the llm is unavailable, skipped the group, or gave an
    argument that did not survive _is_substantive().
    '''
    options = sorted(group['options'], key=lambda o: (o['cost'], o['trim']))
    pick = options[0]

    return {
        'trim_group': group['trim_group'],
        'decision': 'select',
        'selected': [{'id': pick['id'], 'trim': pick['trim'],
                      'cost': pick['cost'], 'role': None}],
        'rejected': [{
            'id': o['id'], 'trim': o['trim'], 'cost': o['cost'],
            'why_not': f'Rs {o["premium_vs_cheapest"]:.2f}/pc dearer.',
        } for o in options[1:]],
        'axis': AXIS_COST,
        'reason': (f'Cheapest option in this group at Rs {pick["cost"]:.2f}/pc. '
                   'No AI recommendation available, so no brand judgement was applied.'),
        'brand_rationale': None,
        'tradeoff': None,
        'cost_impact': {
            'selected_cost': pick['cost'],
            'cheapest_in_group': group['cheapest_in_group'],
            'premium_vs_cheapest': 0.0,
            'premium_percent': 0.0,
        },
        'source': 'rules',
    }


def _totals(out_groups):
    '''
    Like for like: the cheapest floor is summed only over groups something was
    actually selected in, so the premium is what the recommendation costs over
    buying the same shopping list at its floor. A skipped group is on neither side.
    '''
    recommended = 0.0
    cheapest = 0.0
    skipped = 0

    for group in out_groups:
        if group['decision'] != 'select':
            skipped += 1
            continue
        recommended += group['cost_impact']['selected_cost']
        cheapest += group['cost_impact']['cheapest_in_group']

    return {
        'recommended_trim_cost': round(recommended, 2),
        'all_cheapest_cost': round(cheapest, 2),
        'premium_vs_all_cheapest': round(recommended - cheapest, 2),
        'skipped_groups': skipped,
    }


# --- helpers ----------------------------------------------------------------

def _is_substantive(text, brand, garment_type):
    '''
    True when a reason says something beyond who the brand is.

    "Covered elastic because Zara is a mid-premium fast fashion brand" is a
    restatement of the input wearing the grammar of an argument: strip the brand,
    the garment and the tier vocabulary and nothing is left. A real reason names a
    finish, a failure, a seam or a number, and survives the strip.
    '''
    if not text:
        return False
    noise = set(re.findall(r'[a-z0-9]+', f'{brand} {garment_type}'.lower())) | _TIER_WORDS
    kept = [w for w in re.findall(r'[a-z0-9]+', text.lower())
            if len(w) > 2 and w not in noise]
    return len(kept) >= 6


def _cap_words(text, limit):
    ''' Truncate on a word boundary. None and blank pass through as None. '''
    if not text:
        return None
    words = text.split()
    if len(words) <= limit:
        return text
    return ' '.join(words[:limit]).rstrip(',;:.') + '...'


def _clean(value):
    return str(value).strip() if value not in (None, '') else ''


def _to_float(value):
    try:
        if value in (None, ''):
            return 0.0
        return float(value)
    except Exception:
        return 0.0
