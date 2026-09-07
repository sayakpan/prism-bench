import json
import math
from html import escape as escape_html

import frappe
from frappe.utils import cint
from frappe.utils.pdf import get_pdf

FABRIC_IMG_BASEPATH = 'https://prism-assets-dev.pratibhasyntex.com/images/dyed-fabric'
MAX_PAGE_SIZE = 100


@frappe.whitelist()
def get_list(search=None, page=1, page_size=20):
    '''
    Paginated list of Techpack Costing records for the master panel.
    Returns: name, title, owner, creation, has_costing flag.
    '''
    try:
        page = max(1, cint(page) or 1)
        page_size = min(max(1, cint(page_size) or 20), MAX_PAGE_SIZE)

        or_filters = None
        if search and str(search).strip():
            term = f"%{str(search).strip()}%"
            or_filters = [
                ['title', 'like', term],
                ['name', 'like', term],
                ['owner', 'like', term],
            ]

        rows = frappe.get_all(
            'Techpack Costing',
            or_filters=or_filters,
            fields=['name', 'title', 'owner', 'creation', 'modified', 'costing'],
            order_by='creation desc',
            start=(page - 1) * page_size,
            page_length=page_size,
        )

        for r in rows:
            r['has_costing'] = bool(r.get('costing'))
            r.pop('costing', None)

        if or_filters:
            total = len(frappe.get_all(
                'Techpack Costing',
                or_filters=or_filters,
                fields=['name'],
                limit_page_length=0,
            ))
        else:
            total = frappe.db.count('Techpack Costing')

        return {
            'status': True,
            'data': {
                'rows': rows,
                'total': total,
                'page': page,
                'page_size': page_size,
            },
        }
    except Exception as ex:
        frappe.log_error(frappe.get_traceback(), 'techpack_costing.get_list')
        return {'status': False, 'error': str(ex)}

@frappe.whitelist()
def get_record(name):
    '''
    Full record with parsed costing JSON for the detail panel.
    Augments each fabric section with a derived `image_url` when possible.
    '''
    try:
        if not name:
            return {'status': False, 'error': 'name is required'}

        return {'status': True, 'data': _load_record_payload(name)}
    except frappe.DoesNotExistError:
        return {'status': False, 'error': 'Record not found'}
    except Exception as ex:
        frappe.log_error(frappe.get_traceback(), 'techpack_costing.get_record')
        return {'status': False, 'error': str(ex)}

FABRIC_HEAD_KEYS = ['yarn', 'knitting', 'dyes_and_chemicals', 'mechanical_chemical_finish', 'finishing_charges']


def _recalc_section_dyes_for_print_type(section_data, print_type):
    '''
    Looks up DNC rates for the section's shade_category, re-runs the
    dyes_and_chemicals cost calc for the given print_type ("AOP" | "Digital" |
    "None"/falsy), and refreshes gross/loss/total inside section_data in place.
    Also stores the chosen print_type on section_data.
    Returns the mutated section_data.
    '''
    cpk = section_data.get('cost_per_kg') or {}
    breakup = cpk.get('breakup') or {}

    shade_cat = section_data.get('shade_category')
    if not shade_cat:
        section_data['print_type'] = print_type or 'None'
        return section_data

    try:
        row = frappe.db.get_value(
            'DNC Rate',
            {'code': shade_cat},
            ['final_cost_rskg_single', 'final_cost_rskg_double', 'aop', 'digital'],
            as_dict=True,
        )
    except Exception:
        row = None

    if row:
        dnc_cost = {
            'single': _to_float(row.get('final_cost_rskg_single')),
            'double': _to_float(row.get('final_cost_rskg_double')),
            'aop': _to_float(row.get('aop')),
            'digital': _to_float(row.get('digital')),
        }
        from prism.api.costing import _get_dyes_and_chemicals_cost
        pt = (print_type or '').lower()
        pt_arg = pt if pt in ('aop', 'digital') else None
        try:
            new_dyes = _get_dyes_and_chemicals_cost(section_data, dnc_cost, pt_arg)
            breakup['dyes_and_chemicals'] = new_dyes
            cpk['breakup'] = breakup
        except Exception:
            frappe.log_error(frappe.get_traceback(), 'techpack_costing.dyes_recalc')

    # Recompute gross/loss/total based on (possibly updated) breakup heads
    gross = 0.0
    for key in FABRIC_HEAD_KEYS:
        gross += _to_float((breakup.get(key) or {}).get('cost_per_kg'))
    cpk['gross_total_cost'] = round(gross, 2)

    loss_pct = _to_float(section_data.get('loss_percent'))
    loss_amt = gross * (loss_pct / 100.0)
    cpk['loss_amount'] = round(loss_amt, 2)
    cpk['total_fabric_cost'] = round(gross + loss_amt, 2)

    section_data['cost_per_kg'] = cpk
    section_data['print_type'] = print_type or 'None'
    return section_data


@frappe.whitelist()
def recalculate_section_print_type(name, idx, print_type):
    '''
    Live recalculation when the user changes the dyes-and-chemicals print type
    radio for a fabric section. Returns the updated section dict in-memory.
    Does NOT persist — the change is committed on the next Save.
    '''
    try:
        if not name:
            return {'status': False, 'error': 'name is required'}
        idx = cint(idx)

        doc = frappe.get_doc('Techpack Costing', name)
        try:
            costing = json.loads(doc.costing) if doc.costing else []
        except Exception:
            costing = []

        if idx < 0 or idx >= len(costing):
            return {'status': False, 'error': 'Invalid section index'}

        section = costing[idx] or {}
        section_data = section.get('costing') or {}
        _recalc_section_dyes_for_print_type(section_data, print_type)
        section['costing'] = section_data

        return {'status': True, 'data': {'idx': idx, 'section': section}}
    except Exception as ex:
        frappe.log_error(frappe.get_traceback(), 'techpack_costing.recalculate_section_print_type')
        return {'status': False, 'error': str(ex)}

@frappe.whitelist()
def save_section_adjustments(name, adjustments):
    '''
    Persists per-section adjustments inside the costing JSON.
    Each payload row:
    {
        "idx": <int>,
        "adjustment_percent": <number>,
        "kg_per_piece": <number>,
        "adjusted_heads": {                 // optional per-head overrides
            "yarn": <number|null>,
            "knitting": <number|null>,
            "dyes_and_chemicals": <number|null>,
            "mechanical_chemical_finish": <number|null>,
            "finishing_charges": <number|null>
        }
    }
    '''
    try:
        if not name:
            return {'status': False, 'error': 'name is required'}

        if isinstance(adjustments, str):
            adjustments = json.loads(adjustments or '[]')
        if not isinstance(adjustments, list):
            return {'status': False, 'error': 'adjustments must be a list'}

        doc = frappe.get_doc('Techpack Costing', name)
        try:
            costing = json.loads(doc.costing) if doc.costing else []
        except Exception:
            costing = []

        for row in adjustments:
            if not isinstance(row, dict):
                continue
            idx = cint(row.get('idx'))
            if idx < 0 or idx >= len(costing):
                continue

            section = costing[idx] or {}
            data = section.get('costing') or {}

            # Apply print_type change first (re-derives dyes_and_chemicals + gross/loss/total)
            if 'print_type' in row:
                _recalc_section_dyes_for_print_type(data, row.get('print_type'))

            cpk = data.get('cost_per_kg') or {}
            breakup = cpk.get('breakup') or {}

            # Apply per-head adjusted overrides (set / unset)
            adjusted_heads = row.get('adjusted_heads') or {}
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
                cpk['breakup'] = breakup
                data['cost_per_kg'] = cpk

            # Apply per-head remarks (set / unset)
            head_remarks = row.get('head_remarks') or {}
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

            # Compute effective per-kg total (uses adjusted heads when set)
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

            adjustment_percent = round(_to_float(row.get('adjustment_percent')), 4)
            kg_per_piece = _to_float(row.get('kg_per_piece'))
            if kg_per_piece <= 0:
                kg_per_piece = 0.1  # 100 grams default
            kg_per_piece = round(kg_per_piece, 5)

            adjusted_total = round(effective_base * (1 + adjustment_percent / 100), 2)
            data['adjustment_percent'] = adjustment_percent
            data['adjusted_total_fabric_cost'] = adjusted_total
            data['kg_per_piece'] = kg_per_piece
            data['cost_per_piece'] = round(adjusted_total * kg_per_piece, 2)
            section['costing'] = data
            costing[idx] = section

        frappe.db.set_value('Techpack Costing', name, 'costing', json.dumps(costing))
        frappe.db.commit()

        return {'status': True, 'data': {'costing': costing}}
    except frappe.DoesNotExistError:
        return {'status': False, 'error': 'Record not found'}
    except Exception as ex:
        frappe.log_error(frappe.get_traceback(), 'techpack_costing.save_section_adjustments')
        return {'status': False, 'error': str(ex)}

@frappe.whitelist()
def save_trim_costing(name, trim_updates):
    '''
    Persists per-trim edits inside the trim_costing JSON.
    Each payload row: {"id": <str>, "unit_price": <number>, "units": <number>, "is_selected"?: <bool>}.
    Preserves the historical original unit_price / units (first pre-edit values)
    per trim and derives cost = unit_price * units. Recomputes total_trim_cost
    from selected trims.
    '''
    try:
        if not name:
            return {'status': False, 'error': 'name is required'}

        if isinstance(trim_updates, str):
            trim_updates = json.loads(trim_updates or '[]')
        if not isinstance(trim_updates, list):
            return {'status': False, 'error': 'trim_updates must be a list'}

        doc = frappe.get_doc('Techpack Costing', name)
        try:
            tc = json.loads(doc.trim_costing) if doc.trim_costing else {}
        except Exception:
            tc = {}
        if not isinstance(tc, dict):
            tc = {}

        trims = tc.get('trims') or []
        updates_by_id = {}
        for u in trim_updates:
            if isinstance(u, dict) and u.get('id'):
                updates_by_id[u['id']] = u

        for trim in trims:
            if not isinstance(trim, dict):
                continue
            tid = trim.get('id')
            if tid not in updates_by_id:
                continue
            update = updates_by_id[tid]

            current_unit_price = _to_float(trim.get('unit_price'))
            current_units = _to_float(trim.get('units'))

            if 'unit_price' in update:
                new_unit_price = round(_to_float(update.get('unit_price')), 4)
                if abs(new_unit_price - current_unit_price) > 1e-9:
                    if _is_blankish(trim.get('original_unit_price')):
                        trim['original_unit_price'] = round(current_unit_price, 4)
                    trim['unit_price'] = new_unit_price

            if 'units' in update:
                new_units = round(_to_float(update.get('units')), 4)
                if abs(new_units - current_units) > 1e-9:
                    if _is_blankish(trim.get('original_units')):
                        trim['original_units'] = round(current_units, 4)
                    trim['units'] = new_units

            # Cost is always derived = unit_price * units
            trim['cost'] = round(_to_float(trim.get('unit_price')) * _to_float(trim.get('units')), 4)
            # Drop legacy original_cost field if present
            if 'original_cost' in trim:
                trim.pop('original_cost', None)

            if 'is_selected' in update:
                trim['is_selected'] = bool(update.get('is_selected'))

        total = 0.0
        for trim in trims:
            if not isinstance(trim, dict):
                continue
            if trim.get('is_selected') is False:
                continue
            total += _to_float(trim.get('cost'))
        tc['total_trim_cost'] = round(total, 2)
        tc['trims'] = trims

        frappe.db.set_value('Techpack Costing', name, 'trim_costing', json.dumps(tc))
        frappe.db.commit()

        return {'status': True, 'data': {'trim_costing': tc}}
    except frappe.DoesNotExistError:
        return {'status': False, 'error': 'Record not found'}
    except Exception as ex:
        frappe.log_error(frappe.get_traceback(), 'techpack_costing.save_trim_costing')
        return {'status': False, 'error': str(ex)}

@frappe.whitelist()
def save_print_costing(name, print_costing):
    '''
    Persists the Printing tab data. Re-derives all calculated fields server-side
    from the user-entered inputs (order_quantity, print_position, print_type,
    no_of_prints, length, width, coverage) so the stored JSON is always
    self-consistent.
    '''
    try:
        if not name:
            return {'status': False, 'error': 'name is required'}

        if isinstance(print_costing, str):
            print_costing = json.loads(print_costing or '{}')
        if not isinstance(print_costing, dict):
            return {'status': False, 'error': 'print_costing must be an object'}

        errors = _validate_print_costing_payload(print_costing)
        if errors:
            return {'status': False, 'error': '\n'.join(errors)}

        # Touch doc to confirm existence + permissions
        frappe.get_doc('Techpack Costing', name)

        normalized = _normalize_print_costing(print_costing)
        frappe.db.set_value('Techpack Costing', name, 'print_costing', json.dumps(normalized))
        frappe.db.commit()

        return {'status': True, 'data': {'print_costing': normalized}}
    except frappe.DoesNotExistError:
        return {'status': False, 'error': 'Record not found'}
    except Exception as ex:
        frappe.log_error(frappe.get_traceback(), 'techpack_costing.save_print_costing')
        return {'status': False, 'error': str(ex)}

@frappe.whitelist()
def save_order_quantity(name, order_quantity):
    '''
    Persists the per-record Order Quantity onto the Techpack Costing doc's
    `order_quantity` field. This is the single source of truth for the order
    quantity used by print costing.
    '''
    try:
        if not name:
            return {'status': False, 'error': 'name is required'}

        oq = int(_to_float(order_quantity))
        if oq < 1:
            return {'status': False, 'error': 'Order Quantity must be greater than 0.'}

        # Touch doc to confirm existence + permissions
        frappe.get_doc('Techpack Costing', name)
        frappe.db.set_value('Techpack Costing', name, 'order_quantity', oq)
        frappe.db.commit()

        return {'status': True, 'data': {'order_quantity': oq}}
    except frappe.DoesNotExistError:
        return {'status': False, 'error': 'Record not found'}
    except Exception as ex:
        frappe.log_error(frappe.get_traceback(), 'techpack_costing.save_order_quantity')
        return {'status': False, 'error': str(ex)}

@frappe.whitelist()
def save_embroidery_cost(name, embroidery_cost):
    '''
    Persists the Embroidery tab data. Re-derives all calculated fields server-side
    from the user-entered inputs so the stored JSON is always self-consistent.
    '''
    try:
        if not name:
            return {'status': False, 'error': 'name is required'}

        if isinstance(embroidery_cost, str):
            embroidery_cost = json.loads(embroidery_cost or '{}')
        if not isinstance(embroidery_cost, dict):
            return {'status': False, 'error': 'embroidery_cost must be an object'}

        errors = _validate_embroidery_cost_payload(embroidery_cost)
        if errors:
            return {'status': False, 'error': '\n'.join(errors)}

        frappe.get_doc('Techpack Costing', name)

        normalized = _normalize_embroidery_cost(embroidery_cost)
        frappe.db.set_value('Techpack Costing', name, 'embroidery_cost', json.dumps(normalized))
        frappe.db.commit()

        return {'status': True, 'data': {'embroidery_cost': normalized}}
    except frappe.DoesNotExistError:
        return {'status': False, 'error': 'Record not found'}
    except Exception as ex:
        frappe.log_error(frappe.get_traceback(), 'techpack_costing.save_embroidery_cost')
        return {'status': False, 'error': str(ex)}

@frappe.whitelist()
def save_sam_cost(name, sam_cost):
    '''
    Persists the SAM tab data. The factory cost per minute is read from the
    "SAM Costing Rules" doctype, so the stored JSON only carries the
    user-entered SAM minutes plus the snapshotted factory rate and computed
    final SAM cost.
    '''
    try:
        if not name:
            return {'status': False, 'error': 'name is required'}

        if isinstance(sam_cost, str):
            sam_cost = json.loads(sam_cost or '{}')
        if not isinstance(sam_cost, dict):
            return {'status': False, 'error': 'sam_cost must be an object'}

        errors = _validate_sam_cost_payload(sam_cost)
        if errors:
            return {'status': False, 'error': '\n'.join(errors)}

        frappe.get_doc('Techpack Costing', name)

        normalized = _normalize_sam_cost(sam_cost)
        frappe.db.set_value('Techpack Costing', name, 'sam_cost', json.dumps(normalized))
        frappe.db.commit()

        return {'status': True, 'data': {'sam_cost': normalized}}
    except frappe.DoesNotExistError:
        return {'status': False, 'error': 'Record not found'}
    except Exception as ex:
        frappe.log_error(frappe.get_traceback(), 'techpack_costing.save_sam_cost')
        return {'status': False, 'error': str(ex)}

@frappe.whitelist()
def save_final_rollup(name, final_rollup):
    '''
    Persists the Final Roll-Up tab data. The frontend supplies the 3
    percentages plus the component costs (fabric / trim / print / embroidery /
    sam) computed from current in-memory state, and the server validates,
    re-derives base and final, and persists the snapshot.
    '''
    try:
        if not name:
            return {'status': False, 'error': 'name is required'}

        if isinstance(final_rollup, str):
            final_rollup = json.loads(final_rollup or '{}')
        if not isinstance(final_rollup, dict):
            return {'status': False, 'error': 'final_rollup must be an object'}

        errors = _validate_final_rollup_payload(final_rollup)
        if errors:
            return {'status': False, 'error': '\n'.join(errors)}

        frappe.get_doc('Techpack Costing', name)

        normalized = _normalize_final_rollup(final_rollup)
        frappe.db.set_value('Techpack Costing', name, 'final_rollup', json.dumps(normalized))
        frappe.db.commit()

        return {'status': True, 'data': {'final_rollup': normalized}}
    except frappe.DoesNotExistError:
        return {'status': False, 'error': 'Record not found'}
    except Exception as ex:
        frappe.log_error(frappe.get_traceback(), 'techpack_costing.save_final_rollup')
        return {'status': False, 'error': str(ex)}

@frappe.whitelist()
def download_workbench_pdf(name):
    '''
    Renders a print-friendly PDF for the Techpack Costing workbench detail panel.
    '''
    try:
        if not name:
            frappe.throw('name is required')

        data = _load_record_payload(name)
        html = _render_workbench_pdf_html(data)
        pdf_bytes = get_pdf(html)

        title = (data.get('title') or data.get('name') or 'techpack-costing').strip()
        filename = f"{title.replace('/', '-').replace('\\\\', '-')}-cost-sheet.pdf"

        frappe.local.response.filename = filename
        frappe.local.response.filecontent = pdf_bytes
        frappe.local.response.type = 'pdf'
    except Exception:
        frappe.log_error(frappe.get_traceback(), 'techpack_costing.download_workbench_pdf')
        raise

@frappe.whitelist()
def get_print_options():
    try:
        rules = _get_print_costing_rules()
        return {
            'status': True,
            'data': {
                'options': _get_print_options(),
                'mesh_cost_per_screen': rules['mesh_cost_per_screen'],
                'default_coverage_percent': rules['default_coverage_percent'],
                'default_order_quantity': rules['default_order_quantity'],
            },
        }
    except Exception as ex:
        return {'status': False, 'error': str(ex)}


# --- Helpers ---
def _get_print_options():
    '''
    Returns the print-type rate card used by the Printing tab.
    Each entry: print_type, cost_per_inch, manpower_cost.
    Pulls from the "Print Type Master" doctype when available; otherwise
    returns the built-in defaults.
    Extra % and Rejection % are global; see _get_print_costing_rules().
    '''
    rows = None
    try:
        if frappe.db.exists('DocType', 'Print Type Master'):
            rows = frappe.get_all(
                'Print Type Master',
                fields=['print_type_name as print_type', 'cost_per_inch', 'manpower_cost'],
                order_by='print_type_name asc',
                ignore_permissions=True,
            )
    except Exception:
        rows = None

    if rows:
        return rows
    else:
        return [
            {'print_type': 'Non-PVC',       'cost_per_inch': 0.17, 'manpower_cost': 6.00},
            {'print_type': 'Pigment',       'cost_per_inch': 0.01, 'manpower_cost': 5.00},
            {'print_type': 'Gel',           'cost_per_inch': 0.50, 'manpower_cost': 6.00},
            {'print_type': 'Puff',          'cost_per_inch': 0.25, 'manpower_cost': 6.00},
            {'print_type': 'Metalic',       'cost_per_inch': 0.50, 'manpower_cost': 6.00},
            #{'print_type': 'Pearlescent',   'cost_per_inch': 0.35, 'manpower_cost': 6.00},
            #{'print_type': 'Suede',         'cost_per_inch': 0.50, 'manpower_cost': 6.00},
            #{'print_type': 'Foil',          'cost_per_inch': 0.50, 'manpower_cost': 8.00},
            #{'print_type': 'Discharge',     'cost_per_inch': 0.25, 'manpower_cost': 6.00},
            {'print_type': 'Glow in Dark',  'cost_per_inch': 1.50, 'manpower_cost': 7.00},
            {'print_type': 'Stiff Crackle', 'cost_per_inch': 0.50, 'manpower_cost': 7.00},
            #{'print_type': 'Glitter',       'cost_per_inch': 0.50, 'manpower_cost': 6.00},
        ]

def _get_print_costing_rules():
    '''
    Reads the global print-costing buffers and defaults from the single
    "Print Costing Rules" doctype. Falls back to module-level defaults if
    the doc is missing or values are blank/invalid.
    '''
    extra = 6.0
    rejection = 12.0
    mesh_cost = 700
    coverage = 60
    order_qty = 1200

    try:
        values = frappe.db.get_singles_dict('Print Costing Rules') or {}
        extra = _to_float(values.get('default_extra_percentage')) or extra
        rejection = _to_float(values.get('default_rejection_percentage')) or rejection
        mesh_cost = _to_float(values.get('default_mesh_cost_per_screen')) or mesh_cost
        coverage = _to_float(values.get('default_coverage_percentage')) or coverage
        order_qty = _to_float(values.get('default_order_quantity')) or order_qty
    except Exception:
        pass

    return {
        'extra_percent': extra,
        'rejection_percent': rejection,
        'mesh_cost_per_screen': mesh_cost,
        'default_coverage_percent': coverage,
        'default_order_quantity': int(order_qty),
    }

def _validate_print_costing_payload(payload):
    errors = []

    oq_raw = payload.get('order_quantity')
    try:
        oq = float(oq_raw) if not _is_blankish(oq_raw) else 0
    except Exception:
        oq = 0
    if oq <= 0:
        errors.append('Order Quantity must be greater than 0.')

    options_set = {opt['print_type'] for opt in _get_print_options()}
    prints = payload.get('prints') or []
    for idx, item in enumerate(prints):
        label = f"Section {idx + 1}"
        if not isinstance(item, dict):
            errors.append(f"{label}: Invalid section data.")
            continue
        if _is_blankish(item.get('print_position')):
            errors.append(f"{label}: Print Position is required.")
        pt = (item.get('print_type') or '').strip()
        if not pt:
            errors.append(f"{label}: Print Type is required.")
        elif pt not in options_set:
            errors.append(f"{label}: Print Type '{pt}' is not a recognized option.")
        for field, fname in (
            ('no_of_prints', 'No. of Prints'),
            ('length', 'Length'),
            ('width', 'Width'),
            ('coverage', 'Coverage'),
        ):
            if _to_float(item.get(field)) <= 0:
                errors.append(f"{label}: {fname} must be greater than 0.")

    return errors

def _normalize_print_costing(payload):
    options_by_type = {opt['print_type']: opt for opt in _get_print_options()}
    rules = _get_print_costing_rules()
    extra_percent = rules['extra_percent']
    rejection_percent = rules['rejection_percent']
    mesh_cost_per_screen = rules['mesh_cost_per_screen']
    default_coverage = rules['default_coverage_percent']
    default_order_quantity = rules['default_order_quantity']

    order_quantity = int(_to_float(payload.get('order_quantity')) or default_order_quantity)
    if order_quantity < 1:
        order_quantity = default_order_quantity

    out_prints = []
    grand_total = 0
    for item in payload.get('prints') or []:
        if not isinstance(item, dict):
            continue

        pt = item.get('print_type') or ''
        rate = options_by_type.get(pt) or {}
        no_of_prints = _to_float(item.get('no_of_prints'))
        length = _to_float(item.get('length'))
        width = _to_float(item.get('width'))
        coverage_raw = item.get('coverage')
        coverage = _to_float(coverage_raw) if not _is_blankish(coverage_raw) else default_coverage

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
            'id': item.get('id') or frappe.generate_hash(length=10),
            'print_position': item.get('print_position') or '',
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


# ===================== Embroidery helpers =====================
def _get_embroidery_costing_rules():
    '''
    Reads the global embroidery-costing buffers / defaults from the single
    "Embroidery Costing Rules" doctype. Falls back to spec defaults if the
    doc is missing or values are blank/invalid.
    '''
    canvas_cost = 0.0004
    canvas_layers = 3
    manpower_cost = 4.0
    overhead_percent = 5.0
    rejection_percent = 10.0
    laser_cost_per_letter = 2.0

    try:
        values = frappe.db.get_singles_dict('Embroidery Costing Rules') or {}
        canvas_cost = _to_float(values.get('canvas_cost_per_sq_inch')) or canvas_cost
        canvas_layers = int(_to_float(values.get('canvas_layers')) or canvas_layers)
        manpower_cost = _to_float(values.get('manpower_cost')) or manpower_cost
        overhead_percent = _to_float(values.get('overhead_percent')) or overhead_percent
        rejection_percent = _to_float(values.get('rejection_percent')) or rejection_percent
        laser_cost_per_letter = _to_float(values.get('laser_cost_per_letter')) or laser_cost_per_letter
    except Exception:
        pass

    return {
        'canvas_cost_per_sq_inch': canvas_cost,
        'canvas_layers': canvas_layers,
        'manpower_cost': manpower_cost,
        'overhead_percent': overhead_percent,
        'rejection_percent': rejection_percent,
        'laser_cost_per_letter': laser_cost_per_letter,
    }

def _get_embroidery_thread_options():
    '''
    Returns the thread rate card from "Embroidery Thread Master".
    Each entry: thread_type, cost, mtr, cost_per_mtr.
    '''
    try:
        if frappe.db.exists('DocType', 'Embroidery Thread Master'):
            rows = frappe.get_all(
                'Embroidery Thread Master',
                fields=['thread_type', 'cost', 'mtr', 'cost_per_mtr'],
                ignore_permissions=True,
            )
            return [
                {
                    'thread_type': r.get('thread_type'),
                    'cost': _to_float(r.get('cost')),
                    'mtr': _to_float(r.get('mtr')),
                    'cost_per_mtr': _to_float(r.get('cost_per_mtr')),
                }
                for r in rows
                if r.get('thread_type')
            ]
    except Exception:
        pass
    return []

def _get_embroidery_type_options():
    '''
    Returns the embroidery type list from "Embroidery Type Master".
    Each entry: emb_type.
    '''
    try:
        if frappe.db.exists('DocType', 'Embroidery Type Master'):
            rows = frappe.get_all(
                'Embroidery Type Master',
                fields=['emb_type'],
                ignore_permissions=True,
            )
            return [{'emb_type': r.get('emb_type')} for r in rows if r.get('emb_type')]
    except Exception:
        pass
    return []

def _get_embroidery_stitch_rates():
    '''
    Returns the per-1000ST rate lookup table from "Embroidery Cost Per 1000ST Master".
    Each entry: from_stitches, to_stitches, rate. to_stitches may be None for "above".
    '''
    try:
        if frappe.db.exists('DocType', 'Embroidery Cost Per 1000ST Master'):
            rows = frappe.get_all(
                'Embroidery Cost Per 1000ST Master',
                fields=['from_stitches', 'to_stitches', 'rate'],
                order_by='from_stitches asc',
                ignore_permissions=True,
            )
            return [
                {
                    'from_stitches': _to_float(r.get('from_stitches')),
                    'to_stitches': _to_float(r.get('to_stitches')) if r.get('to_stitches') not in (None, '') else None,
                    'rate': _to_float(r.get('rate')),
                }
                for r in rows
            ]
    except Exception:
        pass
    return []

def _lookup_stitch_rate(stitches, rates):
    n = _to_float(stitches)
    for row in rates or []:
        frm = _to_float(row.get('from_stitches'))
        to = row.get('to_stitches')
        if to is None:
            if n >= frm:
                return _to_float(row.get('rate'))
        else:
            if frm <= n <= _to_float(to):
                return _to_float(row.get('rate'))
    return 0.0

def _is_applique(emb_type):
    return 'applique' in (emb_type or '').lower()

def _validate_embroidery_cost_payload(payload):
    errors = []
    thread_set = {opt['thread_type'] for opt in _get_embroidery_thread_options()}
    type_set = {opt['emb_type'] for opt in _get_embroidery_type_options()}

    sections = payload.get('sections') or []
    if not isinstance(sections, list):
        errors.append('Sections must be a list.')
        return errors

    for idx, item in enumerate(sections):
        label = f"Section {idx + 1}"
        if not isinstance(item, dict):
            errors.append(f"{label}: Invalid section data.")
            continue

        tt = (item.get('thread_type') or '').strip()
        if not tt:
            errors.append(f"{label}: Thread Type is required.")
        elif thread_set and tt not in thread_set:
            errors.append(f"{label}: Thread Type '{tt}' is not a recognized option.")

        et = (item.get('emb_type') or '').strip()
        if not et:
            errors.append(f"{label}: EMB Type is required.")
        elif type_set and et not in type_set:
            errors.append(f"{label}: EMB Type '{et}' is not a recognized option.")

        for field, fname in (
            ('no_of_stitches', 'No. of Stitches'),
            ('needle_thread_avg', 'Needle Thread Avg.'),
            ('length', 'Length'),
            ('width', 'Width'),
        ):
            if _to_float(item.get(field)) <= 0:
                errors.append(f"{label}: {fname} must be greater than 0.")

        if _is_applique(et):
            letters = _to_float(item.get('letter_design_for_laser'))
            if letters < 0:
                errors.append(f"{label}: Letter / Design for Laser must be 0 or greater.")

    return errors

def _normalize_embroidery_cost(payload):
    rules = _get_embroidery_costing_rules()
    threads_by_type = {t['thread_type']: t for t in _get_embroidery_thread_options()}
    stitch_rates = _get_embroidery_stitch_rates()

    canvas_cost_per_sq_inch = rules['canvas_cost_per_sq_inch']
    canvas_layers = rules['canvas_layers']
    manpower_cost = rules['manpower_cost']
    overhead_percent = rules['overhead_percent']
    rejection_percent = rules['rejection_percent']
    laser_cost_per_letter = rules['laser_cost_per_letter']

    out_sections = []
    grand_total = 0.0
    for item in payload.get('sections') or []:
        if not isinstance(item, dict):
            continue

        tt = item.get('thread_type') or ''
        et = item.get('emb_type') or ''
        thread = threads_by_type.get(tt) or {}

        no_of_stitches = _to_float(item.get('no_of_stitches'))
        needle = _to_float(item.get('needle_thread_avg'))
        no_of_thread_colours = _to_float(item.get('no_of_thread_colours'))
        letters = _to_float(item.get('letter_design_for_laser'))
        length = _to_float(item.get('length'))
        width = _to_float(item.get('width'))

        cost_per_mtr = _to_float(thread.get('cost_per_mtr'))
        bobbin = needle / 3.0 if needle else 0.0
        thread_cost = (needle + bobbin) * cost_per_mtr
        stitch_rate = _lookup_stitch_rate(no_of_stitches, stitch_rates)
        cost_per_1000st = (no_of_stitches / 1000.0) * stitch_rate
        area = length * width
        canvas_cost = area * canvas_cost_per_sq_inch * canvas_layers
        applique = _is_applique(et)
        laser_cost = (laser_cost_per_letter * letters) if applique else 0.0
        cost = thread_cost + cost_per_1000st + canvas_cost + manpower_cost + laser_cost
        overhead_amount = cost * (overhead_percent / 100.0)
        rejection_amount = (cost + overhead_amount) * (rejection_percent / 100.0)
        final_cost = cost + overhead_amount + rejection_amount

        out_sections.append({
            'id': item.get('id') or frappe.generate_hash(length=10),
            'section_name': item.get('section_name') or '',
            'thread_type': tt,
            'emb_type': et,
            'no_of_stitches': no_of_stitches,
            'no_of_thread_colours': no_of_thread_colours,
            'needle_thread_avg': needle,
            'letter_design_for_laser': letters,
            'length': length,
            'width': width,
            'is_applique': applique,
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


# ===================== SAM helpers =====================
def _get_sam_costing_rules():
    '''
    Reads the global factory cost / minute from the single "SAM Costing Rules"
    doctype. Falls back to the spec default (13) if the doc is missing or the
    field is blank/invalid.
    '''
    factory_cost = 13.0
    try:
        values = frappe.db.get_singles_dict('SAM Costing Rules') or {}
        factory_cost = _to_float(values.get('factory_cost_per_minute')) or factory_cost
    except Exception:
        pass
    return {'factory_cost_per_minute': factory_cost}

def _validate_sam_cost_payload(payload):
    errors = []
    sam_minutes = _to_float(payload.get('sam_minutes'))
    if sam_minutes <= 0:
        errors.append('SAM (minutes) must be greater than 0.')
    return errors

def _normalize_sam_cost(payload):
    rules = _get_sam_costing_rules()
    factory_cost = rules['factory_cost_per_minute']
    sam_minutes = _to_float(payload.get('sam_minutes'))
    sam_cost = round(sam_minutes * factory_cost, 2)
    return {
        'sam_minutes': round(sam_minutes, 4),
        'factory_cost_per_minute': factory_cost,
        'sam_cost': sam_cost,
    }


# ===================== Final Roll-Up helpers =====================
def _validate_final_rollup_payload(payload):
    errors = []
    for field, label in (
        ('rejection_percent', 'Rejection %'),
        ('testing_percent', 'Testing %'),
        ('profit_percent', 'Profit %'),
    ):
        if _to_float(payload.get(field)) < 0:
            errors.append(f"{label} must be 0 or greater.")
    currency_code = (payload.get('currency_code') or 'INR').strip()
    if currency_code and currency_code.upper() != 'INR':
        if _to_float(payload.get('conversion_factor')) <= 0:
            errors.append('Conversion Factor must be greater than 0.')
    return errors

def _get_costing_currency_options():
    '''
    Returns the available costing currency options from the "Costing Currency"
    doctype. Falls back to a single INR entry if the doctype is missing or empty.
    '''
    try:
        if frappe.db.exists('DocType', 'Costing Currency'):
            rows = frappe.get_all(
                'Costing Currency',
                fields=['currency_code', 'description', 'conversion_factor', 'currency_symbol'],
                ignore_permissions=True,
            )
            options = [
                {
                    'currency_code': r.get('currency_code'),
                    'description': r.get('description') or '',
                    'conversion_factor': _to_float(r.get('conversion_factor')) or 1.0,
                    'currency_symbol': r.get('currency_symbol') or '',
                }
                for r in rows
                if r.get('currency_code')
            ]
            if options:
                # Ensure INR is at the top of the list
                options.sort(key=lambda o: (o['currency_code'].upper() != 'INR', o['currency_code']))
                return options
    except Exception:
        pass
    return [{
        'currency_code': 'INR',
        'description': 'Indian Rupee',
        'conversion_factor': 1.0,
        'currency_symbol': '₹',
    }]

def _normalize_final_rollup(payload):
    fabric_cost = _to_float(payload.get('fabric_cost'))
    trim_cost = _to_float(payload.get('trim_cost'))
    print_cost = _to_float(payload.get('print_cost'))
    embroidery_cost = _to_float(payload.get('embroidery_cost'))
    sam_cost = _to_float(payload.get('sam_cost'))
    base_cost = fabric_cost + trim_cost + print_cost + embroidery_cost + sam_cost

    rejection_percent = _to_float(payload.get('rejection_percent'))
    testing_percent = _to_float(payload.get('testing_percent'))
    profit_percent = _to_float(payload.get('profit_percent'))

    rejection_amount = base_cost * rejection_percent / 100.0
    testing_amount = base_cost * testing_percent / 100.0
    profit_amount = base_cost * profit_percent / 100.0
    final_cost = base_cost + rejection_amount + testing_amount + profit_amount

    currency_code = (payload.get('currency_code') or 'INR').strip() or 'INR'
    conversion_factor = _to_float(payload.get('conversion_factor'))
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

def _load_record_payload(name):
    doc = frappe.get_doc('Techpack Costing', name)

    try:
        costing = json.loads(doc.costing) if doc.costing else []
    except Exception:
        costing = []

    try:
        fabrics = json.loads(doc.fabrics) if doc.fabrics else []
    except Exception:
        fabrics = []

    try:
        trim_costing = json.loads(doc.trim_costing) if doc.trim_costing else None
    except Exception:
        trim_costing = None

    try:
        print_costing = json.loads(doc.print_costing) if getattr(doc, 'print_costing', None) else None
    except Exception:
        print_costing = None

    try:
        embroidery_cost = json.loads(doc.embroidery_cost) if getattr(doc, 'embroidery_cost', None) else None
    except Exception:
        embroidery_cost = None

    try:
        sam_cost = json.loads(doc.sam_cost) if getattr(doc, 'sam_cost', None) else None
    except Exception:
        sam_cost = None

    try:
        final_rollup = json.loads(doc.final_rollup) if getattr(doc, 'final_rollup', None) else None
    except Exception:
        final_rollup = None

    try:
        market_fob_and_retail = json.loads(doc.market_fob_and_retail) if getattr(doc, 'market_fob_and_retail', None) else None
    except Exception:
        market_fob_and_retail = None

    return {
        'name': doc.name,
        'title': doc.title,
        'tech_pack': doc.tech_pack,
        'artwork': getattr(doc, 'artwork', None),
        'owner': doc.owner,
        'creation': doc.creation,
        'modified': doc.modified,
        'garment_style': getattr(doc, 'garment_style', None),
        'garment_gender': getattr(doc, 'garment_gender', None),
        'garment_brand': getattr(doc, 'garment_brand', None),
        'order_quantity': getattr(doc, 'order_quantity', None),
        'fabrics': fabrics,
        'costing': costing,
        'trim_costing': trim_costing,
        'print_costing': print_costing,
        'print_costing_rules': _get_print_costing_rules(),
        'print_options': _get_print_options(),
        'embroidery_cost': embroidery_cost,
        'embroidery_costing_rules': _get_embroidery_costing_rules(),
        'embroidery_thread_options': _get_embroidery_thread_options(),
        'embroidery_type_options': _get_embroidery_type_options(),
        'embroidery_stitch_rates': _get_embroidery_stitch_rates(),
        'sam_cost': sam_cost,
        'sam_costing_rules': _get_sam_costing_rules(),
        'final_rollup': final_rollup,
        'costing_currency_options': _get_costing_currency_options(),
        'market_fob_and_retail': market_fob_and_retail,
    }

def _to_float(value):
    try:
        if value in (None, ''):
            return 0.0
        return float(value)
    except Exception:
        return 0.0

def _render_workbench_pdf_html(data):
    title = escape_html(data.get('title') or data.get('name') or 'Techpack Costing')
    owner = escape_html(_short_owner(data.get('owner')))
    created = escape_html(_fmt_date(data.get('creation')))
    docname = escape_html(data.get('name') or '')

    sections = data.get('costing') or []
    sections_html = ''.join(_render_section_for_pdf(section, idx) for idx, section in enumerate(sections))
    if not sections_html:
        sections_html = '<div class="empty">No costing data is available for this record yet.</div>'

    trim_html = _render_trim_costing_for_pdf(data.get('trim_costing'))
    print_html = _render_print_costing_for_pdf(data.get('print_costing'))
    embroidery_html = _render_embroidery_cost_for_pdf(data.get('embroidery_cost'))
    sam_html = _render_sam_cost_for_pdf(data.get('sam_cost'))
    rollup_html = _render_final_rollup_for_pdf(data.get('final_rollup'))

    return f'''
    <html>
    <head>
        <meta charset="utf-8">
        <style>
            @page {{ size: A4; margin: 10mm; }}
            * {{ box-sizing: border-box; }}
            body {{ font-family: Arial, sans-serif; color: #1f2937; font-size: 11px; }}
            h1 {{ margin: 0; font-size: 18px; }}
            .meta {{ margin-top: 4px; color: #6b7280; font-size: 10px; }}
            .section {{ margin-top: 14px; border: 1px solid #d1d5db; border-radius: 6px; overflow: hidden; page-break-inside: auto; break-inside: auto; }}
            .section-head {{ display: table; width: 100%; background: #f8fafc; padding: 8px 10px; border-bottom: 1px solid #e5e7eb; }}
            .section-title {{ display: table-cell; font-size: 13px; font-weight: 700; vertical-align: middle; }}
            .section-total {{ display: table-cell; font-size: 12px; font-weight: 700; text-align: right; white-space: nowrap; vertical-align: middle; }}
            .section-total-line {{ display: block; line-height: 1.35; }}
            .section-total-label {{ color: #6b7280; }}
            .section-total-adjust {{ color: #92400e; }}
            .section-total-value {{ color: #15803d; }}
            .section-total-base {{ color: #111827; }}
            .section-body {{ padding: 10px; }}
            .desc {{ font-size: 12px; font-weight: 700; background: #eef7ff; border: 1px solid #dbeafe; border-radius: 4px; padding: 8px; }}
            .fab-grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 10px; margin-top: 10px; }}
            .kv {{ border: 1px solid #e5e7eb; border-radius: 4px; overflow: hidden; }}
            .kv table {{ width: 100%; border-collapse: collapse; }}
            .kv td {{ padding: 4px 6px; border-bottom: 1px solid #f1f5f9; vertical-align: top; font-size: 10px; }}
            .kv td:first-child {{ width: 40%; color: #6b7280; font-weight: 600; }}
            .kv td:last-child {{ font-weight: 600; }}
            .gsm-mismatch {{ color: #c65a00; font-weight: 700; }}
            .calc {{ border: 1px solid #e5e7eb; border-radius: 4px; background: #fffceb; padding: 6px 8px; }}
            .calc table {{ width: 100%; border-collapse: collapse; }}
            .calc td {{ padding: 2px 0; font-size: 10px; }}
            .calc td:nth-child(1) {{ width: 14px; color: #6b7280; text-align: center; }}
            .calc td:nth-child(2) {{ color: #111827; }}
            .calc td:nth-child(3) {{ text-align: right; font-weight: 700; }}
            .calc .subtotal td {{ border-top: none; padding-top: 4px; color: #2563eb; }}
            .calc .warning td:nth-child(2), .calc .warning td:nth-child(3) {{ color: #b7791f; }}
            .calc .total td {{ border-top: 1px solid #d1d5db; padding-top: 3px; font-size: 11px; }}
            .calc .total td:nth-child(3) {{ color: #15803d; }}
            .heads {{ margin-top: 12px; }}
            .card {{ border: 1px solid #e5e7eb; border-radius: 4px; margin-bottom: 8px; overflow: hidden; page-break-inside: avoid; }}
            .card-head {{ display: flex; justify-content: space-between; gap: 8px; background: #f9fafb; border-bottom: 1px solid #e5e7eb; padding: 6px 8px; font-weight: 700; }}
            .card-body {{ padding: 6px 8px; }}
            table.tbl {{ width: 100%; border-collapse: collapse; font-size: 9px; }}
            .tbl th, .tbl td {{ border: 1px solid #e5e7eb; padding: 4px 5px; text-align: left; }}
            .tbl th {{ background: #f8fafc; }}
            .num {{ text-align: right; white-space: nowrap; }}
            .muted {{ color: #6b7280; }}
            .empty {{ padding: 12px; border: 1px dashed #d1d5db; border-radius: 4px; color: #6b7280; }}
            .chips {{ margin-bottom: 6px; }}
            .chip {{ display: inline-block; border: 1px solid #dbeafe; background: #eff6ff; border-radius: 10px; padding: 1px 7px; margin-right: 4px; font-size: 10px; }}
            .trim-section {{ margin-top: 18px; border: 1px solid #d1d5db; border-radius: 6px; overflow: hidden; page-break-inside: auto; break-inside: auto; }}
            .trim-head {{ display: table; width: 100%; background: #eef7ff; padding: 8px 10px; border-bottom: 1px solid #dbeafe; }}
            .trim-head-title {{ display: table-cell; font-size: 13px; font-weight: 700; vertical-align: middle; }}
            .trim-head-style {{ display: table-cell; font-size: 11px; color: #1f2937; vertical-align: middle; padding-left: 12px; }}
            .trim-head-total {{ display: table-cell; font-size: 12px; font-weight: 700; text-align: right; white-space: nowrap; vertical-align: middle; color: #15803d; }}
            .trim-head-total-label {{ color: #6b7280; font-weight: 600; margin-right: 4px; }}
            .trim-body {{ padding: 10px; }}
            .trim-cost-orig {{ display: block; font-size: 8px; color: #6b7280; font-weight: 400; }}
            .trim-cost-pos {{ color: #15803d; font-weight: 700; }}
            .trim-cost-neg {{ color: #c0392b; font-weight: 700; }}
            .print-section {{ margin-top: 18px; border: 1px solid #d1d5db; border-radius: 6px; overflow: hidden; page-break-inside: auto; break-inside: auto; }}
            .print-head {{ display: table; width: 100%; background: #eef7ff; padding: 8px 10px; border-bottom: 1px solid #dbeafe; }}
            .print-head-title {{ display: table-cell; font-size: 13px; font-weight: 700; vertical-align: middle; }}
            .print-head-meta {{ display: table-cell; font-size: 11px; color: #1f2937; vertical-align: middle; padding-left: 12px; }}
            .print-head-total {{ display: table-cell; font-size: 12px; font-weight: 700; text-align: right; white-space: nowrap; vertical-align: middle; color: #15803d; }}
            .print-head-total-label {{ color: #6b7280; font-weight: 600; margin-right: 4px; }}
            .print-body {{ padding: 10px; }}
            .print-card {{ border: 1px solid #e5e7eb; border-radius: 4px; margin-bottom: 8px; overflow: hidden; page-break-inside: avoid; }}
            .print-card-head {{ display: table; width: 100%; background: #f9fafb; border-bottom: 1px solid #e5e7eb; padding: 6px 8px; }}
            .print-card-title {{ display: table-cell; font-size: 12px; font-weight: 700; vertical-align: middle; }}
            .print-card-final {{ display: table-cell; text-align: right; font-size: 11px; vertical-align: middle; white-space: nowrap; }}
            .print-card-body {{ padding: 8px; }}
            .print-inputs {{ padding-bottom: 6px; margin-bottom: 6px; border-bottom: 1px dashed #e5e7eb; }}
            .print-inputs span {{ display: inline-block; margin-right: 12px; font-size: 10px; }}
            .sam-section {{ margin-top: 18px; border: 1px solid #d1d5db; border-radius: 6px; overflow: hidden; }}
            .sam-head {{ display: table; width: 100%; background: #eef7ff; padding: 8px 10px; border-bottom: 1px solid #dbeafe; }}
            .sam-head-title {{ display: table-cell; font-size: 13px; font-weight: 700; vertical-align: middle; }}
            .sam-head-meta {{ display: table-cell; font-size: 11px; color: #1f2937; vertical-align: middle; padding-left: 12px; }}
            .sam-head-total {{ display: table-cell; font-size: 12px; font-weight: 700; text-align: right; white-space: nowrap; vertical-align: middle; color: #15803d; }}
            .sam-head-total-label {{ color: #6b7280; font-weight: 600; margin-right: 4px; }}
            .final-section {{ margin-top: 18px; border: 1px solid #d1d5db; border-radius: 6px; overflow: hidden; page-break-inside: auto; break-inside: auto; }}
            .final-head {{ display: table; width: 100%; background: #eef7ff; padding: 8px 10px; border-bottom: 1px solid #dbeafe; }}
            .final-head-title {{ display: table-cell; font-size: 13px; font-weight: 700; vertical-align: middle; }}
            .final-head-total {{ display: table-cell; font-size: 12px; font-weight: 700; text-align: right; white-space: nowrap; vertical-align: middle; color: #15803d; }}
            .final-head-total-label {{ color: #6b7280; font-weight: 600; margin-right: 4px; }}
            .final-body {{ padding: 10px; }}
            .currency-conv {{ margin-top: 10px; padding-top: 8px; border-top: 1px dashed #e5e7eb; display: table; width: 100%; }}
            .currency-conv-cell {{ display: table-cell; font-size: 11px; padding-right: 16px; vertical-align: middle; }}
            .currency-conv-label {{ color: #6b7280; font-weight: 600; margin-right: 4px; }}
            .currency-conv-final {{ color: #15803d; font-weight: 700; font-size: 14px; }}
        </style>
    </head>
    <body>
        <h1>{title}</h1>
        <div class="meta">{owner} · {created} · {docname}</div>
        {sections_html}
        {trim_html}
        {print_html}
        {embroidery_html}
        {sam_html}
        {rollup_html}
    </body>
    </html>
    '''

def _render_trim_costing_for_pdf(trim_costing):
    if not isinstance(trim_costing, dict):
        return ''

    trims = trim_costing.get('trims') or []
    visible = []
    for trim in trims:
        if not isinstance(trim, dict):
            continue
        has_group = not _is_blankish(trim.get('trim_group'))
        if has_group and trim.get('is_selected') is False:
            continue
        visible.append(trim)

    style = trim_costing.get('style')
    style_html = (
        f"<div class='trim-head-style'><span style='color:#6b7280;'>Style:</span> {escape_html(str(style))}</div>"
        if not _is_blankish(style) else ''
    )
    total = _to_num(trim_costing.get('total_trim_cost'))
    total_fmt = _fmt_currency(total)

    if not visible:
        body = "<div class='empty'>No trims listed.</div>"
    else:
        rows = []
        for trim in visible:
            unit_price = _to_num(trim.get('unit_price'))
            units = _to_num(trim.get('units'))
            cost = unit_price * units
            orig_up_raw = trim.get('original_unit_price')
            orig_un_raw = trim.get('original_units')
            orig_unit_price = _to_num(orig_up_raw) if not _is_blankish(orig_up_raw) else unit_price
            orig_units = _to_num(orig_un_raw) if not _is_blankish(orig_un_raw) else units
            orig = orig_unit_price * orig_units
            cost_cls = ''
            if abs(cost - orig) > 0.0001:
                cost_cls = 'trim-cost-pos' if cost > orig else 'trim-cost-neg'
            cost_cell = (
                f"<span class='{cost_cls}'>{_fmt_currency(cost)}</span>"
                f"<span class='trim-cost-orig'>orig: {_fmt_currency(orig)}</span>"
                if cost_cls else _fmt_currency(cost)
            )
            units_fmt = _fmt_number(units)
            rows.append(
                f"<tr>"
                f"<td>{escape_html(str(trim.get('trim') or '—'))}</td>"
                f"<td>{_safe_text(trim.get('trim_group'))}</td>"
                f"<td class='num'>{_fmt_currency(trim.get('unit_price'))}</td>"
                f"<td class='num'>{units_fmt}</td>"
                f"<td class='num'>{cost_cell}</td>"
                f"</tr>"
            )
        body = (
            "<table class='tbl'>"
            "<thead><tr>"
            "<th>Trim</th><th>Trim Group</th>"
            "<th class='num'>Unit Price</th>"
            "<th class='num'>Units</th>"
            "<th class='num'>Cost</th>"
            "</tr></thead>"
            f"<tbody>{''.join(rows)}</tbody>"
            "</table>"
        )

    return f'''
    <section class="trim-section">
        <div class="trim-head">
            <div class="trim-head-title">Trim Costing</div>
            {style_html}
            <div class="trim-head-total"><span class="trim-head-total-label">Total Trim Cost:</span>{total_fmt}</div>
        </div>
        <div class="trim-body">{body}</div>
    </section>
    '''

def _render_print_costing_for_pdf(print_costing):
    if not isinstance(print_costing, dict):
        return ''
    prints = print_costing.get('prints') or []
    if not prints:
        return ''

    rules = _get_print_costing_rules()
    extra_percent = rules['extra_percent']
    rejection_percent = rules['rejection_percent']

    order_quantity = _to_num(print_costing.get('order_quantity'))
    grand_total = _to_num(print_costing.get('total_print_cost'))
    cards_html = ''.join(
        _render_print_card_for_pdf(s, idx, extra_percent, rejection_percent)
        for idx, s in enumerate(prints)
    )

    return f'''
    <section class="print-section">
        <div class="print-head">
            <div class="print-head-title">Print Costing</div>
            <div class="print-head-meta">
                <span style="color:#6b7280;">Order Quantity:</span> {int(order_quantity) if order_quantity else 0}
                &nbsp;|&nbsp; <span style="color:#6b7280;">Extra:</span> {_fmt_percent(extra_percent)}
                &nbsp;|&nbsp; <span style="color:#6b7280;">Rejection:</span> {_fmt_percent(rejection_percent)}
            </div>
            <div class="print-head-total"><span class="print-head-total-label">Print Cost / Garment:</span>{_fmt_currency(grand_total)}</div>
        </div>
        <div class="print-body">{cards_html}</div>
    </section>
    '''

def _render_print_card_for_pdf(section, idx, extra_percent, rejection_percent):
    if not isinstance(section, dict):
        return ''
    pos = section.get('print_position') or f'Print {idx + 1}'
    pt = section.get('print_type') or '—'
    no_of_prints = _to_num(section.get('no_of_prints'))
    length = _to_num(section.get('length'))
    width = _to_num(section.get('width'))
    coverage = _to_num(section.get('coverage'))
    area = _to_num(section.get('area'))
    cost_per_inch = _to_num(section.get('cost_per_inch'))
    manpower_cost = _to_num(section.get('manpower_cost'))
    ink_cost = _to_num(section.get('ink_cost'))
    mesh_cost_per_garment = _to_num(section.get('mesh_cost_per_garment'))
    material_cost = _to_num(section.get('material_cost'))
    gross_total = _to_num(section.get('gross_total_cost'))
    rejection_amount = _to_num(section.get('rejection_amount'))
    final_cost = _to_num(section.get('final_cost'))

    np_fmt = str(int(no_of_prints)) if no_of_prints == int(no_of_prints) else _fmt_number(no_of_prints)

    return f'''
    <div class="print-card">
        <div class="print-card-head">
            <div class="print-card-title">#{idx + 1} &nbsp; {escape_html(str(pos))}</div>
            <div class="print-card-final"><span style="color:#6b7280;">Section Cost:</span> <strong style="color:#15803d;">{_fmt_currency(final_cost)}</strong></div>
        </div>
        <div class="print-card-body">
            <div class="print-inputs">
                <span><strong>Type:</strong> {escape_html(str(pt))}</span>
                <span><strong># Prints:</strong> {np_fmt}</span>
                <span><strong>Size:</strong> {_fmt_number(length)}" &times; {_fmt_number(width)}"</span>
                <span><strong>Coverage:</strong> {_fmt_percent(coverage)}</span>
                <span><strong>Area:</strong> {_fmt_number(area)} sqin</span>
                <span><strong>Ink Cost/in:</strong> {_fmt_currency(cost_per_inch)}</span>
            </div>
            <div class="calc">
                <table>
                    <tr><td></td><td>Ink Cost</td><td class="num">{_fmt_currency(ink_cost)}</td></tr>
                    <tr><td>+</td><td>Mesh / garment</td><td class="num">{_fmt_currency(mesh_cost_per_garment)}</td></tr>
                    <tr class="subtotal"><td>=</td><td>Material (incl. {_fmt_percent(extra_percent)} extra)</td><td class="num">{_fmt_currency(material_cost)}</td></tr>
                    <tr><td>+</td><td>Manpower</td><td class="num">{_fmt_currency(manpower_cost)}</td></tr>
                    <tr class="subtotal"><td>=</td><td>Gross Total</td><td class="num">{_fmt_currency(gross_total)}</td></tr>
                    <tr class="warning"><td>+</td><td>Rejection ({_fmt_percent(rejection_percent)})</td><td class="num">{_fmt_currency(rejection_amount)}</td></tr>
                    <tr class="total"><td>=</td><td>Final / garment (rounded)</td><td class="num">{_fmt_currency(final_cost)}</td></tr>
                </table>
            </div>
        </div>
    </div>
    '''

def _render_embroidery_cost_for_pdf(embroidery_cost):
    if not isinstance(embroidery_cost, dict):
        return ''
    sections = embroidery_cost.get('sections') or []
    if not sections:
        return ''

    canvas_cost = _to_num(embroidery_cost.get('canvas_cost_per_sq_inch'))
    canvas_layers = int(_to_num(embroidery_cost.get('canvas_layers')) or 3)
    manpower = _to_num(embroidery_cost.get('manpower_cost'))
    overhead_pct = _to_num(embroidery_cost.get('overhead_percent'))
    rejection_pct = _to_num(embroidery_cost.get('rejection_percent'))
    laser_per_letter = _to_num(embroidery_cost.get('laser_cost_per_letter'))
    grand_total = _to_num(embroidery_cost.get('total_embroidery_cost'))

    cards_html = ''.join(
        _render_embroidery_card_for_pdf(s, idx, overhead_pct, rejection_pct)
        for idx, s in enumerate(sections)
    )

    return f'''
    <section class="print-section">
        <div class="print-head">
            <div class="print-head-title">Embroidery Costing</div>
            <div class="print-head-meta">
                <span style="color:#6b7280;">Canvas:</span> {_fmt_currency(canvas_cost)}/sqin × {canvas_layers}
                &nbsp;|&nbsp; <span style="color:#6b7280;">Manpower:</span> {_fmt_currency(manpower)}
                &nbsp;|&nbsp; <span style="color:#6b7280;">Over Head:</span> {_fmt_percent(overhead_pct)}
                &nbsp;|&nbsp; <span style="color:#6b7280;">Rejection:</span> {_fmt_percent(rejection_pct)}
                &nbsp;|&nbsp; <span style="color:#6b7280;">Laser/letter:</span> {_fmt_currency(laser_per_letter)}
            </div>
            <div class="print-head-total"><span class="print-head-total-label">Embroidery Cost / Garment:</span>{_fmt_currency(grand_total)}</div>
        </div>
        <div class="print-body">{cards_html}</div>
    </section>
    '''

def _render_embroidery_card_for_pdf(section, idx, overhead_pct, rejection_pct):
    if not isinstance(section, dict):
        return ''
    name = section.get('section_name') or f'Embroidery {idx + 1}'
    thread_type = section.get('thread_type') or '—'
    emb_type = section.get('emb_type') or '—'
    stitches = _to_num(section.get('no_of_stitches'))
    needle = _to_num(section.get('needle_thread_avg'))
    bobbin = _to_num(section.get('bobbin_thread_avg'))
    cost_per_mtr = _to_num(section.get('cost_per_mtr'))
    thread_cost = _to_num(section.get('thread_cost'))
    stitch_rate = _to_num(section.get('stitch_rate'))
    cost_per_1000st = _to_num(section.get('cost_per_1000st'))
    length = _to_num(section.get('length'))
    width = _to_num(section.get('width'))
    canvas_cost = _to_num(section.get('canvas_cost'))
    laser_cost = _to_num(section.get('laser_cost'))
    manpower_cost = _to_num(section.get('manpower_cost'))
    cost = _to_num(section.get('cost'))
    overhead_amount = _to_num(section.get('overhead_amount'))
    rejection_amount = _to_num(section.get('rejection_amount'))
    final_cost = _to_num(section.get('final_cost'))

    return f'''
    <div class="print-card">
        <div class="print-card-head">
            <div class="print-card-title">#{idx + 1} &nbsp; {escape_html(str(name))}</div>
            <div class="print-card-final"><span style="color:#6b7280;">Section Cost:</span> <strong style="color:#15803d;">{_fmt_currency(final_cost)}</strong></div>
        </div>
        <div class="print-card-body">
            <div class="print-inputs">
                <span><strong>Thread:</strong> {escape_html(str(thread_type))}</span>
                <span><strong>Type:</strong> {escape_html(str(emb_type))}</span>
                <span><strong>Stitches:</strong> {_fmt_number(stitches)}</span>
                <span><strong>Needle:</strong> {_fmt_number(needle)}</span>
                <span><strong>Bobbin:</strong> {_fmt_number(bobbin)}</span>
                <span><strong>Size:</strong> {_fmt_number(length)}" &times; {_fmt_number(width)}"</span>
                <span><strong>Cost/Mtr:</strong> {_fmt_currency(cost_per_mtr)}</span>
                <span><strong>Stitch Rate:</strong> {_fmt_currency(stitch_rate)}</span>
            </div>
            <div class="calc">
                <table>
                    <tr><td></td><td>Thread Cost</td><td class="num">{_fmt_currency(thread_cost)}</td></tr>
                    <tr><td>+</td><td>Cost / 1000ST</td><td class="num">{_fmt_currency(cost_per_1000st)}</td></tr>
                    <tr><td>+</td><td>Canvas Cost</td><td class="num">{_fmt_currency(canvas_cost)}</td></tr>
                    <tr><td>+</td><td>Manpower</td><td class="num">{_fmt_currency(manpower_cost)}</td></tr>
                    <tr><td>+</td><td>Laser Cost</td><td class="num">{_fmt_currency(laser_cost)}</td></tr>
                    <tr class="subtotal"><td>=</td><td>Cost</td><td class="num">{_fmt_currency(cost)}</td></tr>
                    <tr><td>+</td><td>Over Head ({_fmt_percent(overhead_pct)})</td><td class="num">{_fmt_currency(overhead_amount)}</td></tr>
                    <tr class="warning"><td>+</td><td>Rejection ({_fmt_percent(rejection_pct)})</td><td class="num">{_fmt_currency(rejection_amount)}</td></tr>
                    <tr class="total"><td>=</td><td>Final / garment</td><td class="num">{_fmt_currency(final_cost)}</td></tr>
                </table>
            </div>
        </div>
    </div>
    '''

def _render_sam_cost_for_pdf(sam_cost):
    if not isinstance(sam_cost, dict):
        return ''
    sam_minutes = _to_num(sam_cost.get('sam_minutes'))
    factory_cost = _to_num(sam_cost.get('factory_cost_per_minute'))
    cost = _to_num(sam_cost.get('sam_cost'))
    if sam_minutes <= 0 and cost <= 0:
        return ''
    return f'''
    <section class="sam-section">
        <div class="sam-head">
            <div class="sam-head-title">SAM Cost</div>
            <div class="sam-head-meta">
                <span style="color:#6b7280;">SAM:</span> {_fmt_number(sam_minutes)} min
                &nbsp;|&nbsp; <span style="color:#6b7280;">Factory Cost:</span> {_fmt_currency(factory_cost)}/min
            </div>
            <div class="sam-head-total"><span class="sam-head-total-label">SAM Cost / Garment:</span>{_fmt_currency(cost)}</div>
        </div>
    </section>
    '''

def _render_final_rollup_for_pdf(final_rollup):
    if not isinstance(final_rollup, dict):
        return ''

    fabric = _to_num(final_rollup.get('fabric_cost'))
    trim = _to_num(final_rollup.get('trim_cost'))
    print_c = _to_num(final_rollup.get('print_cost'))
    embroidery = _to_num(final_rollup.get('embroidery_cost'))
    sam = _to_num(final_rollup.get('sam_cost'))
    base = _to_num(final_rollup.get('base_cost'))
    rej_pct = _to_num(final_rollup.get('rejection_percent'))
    tst_pct = _to_num(final_rollup.get('testing_percent'))
    prf_pct = _to_num(final_rollup.get('profit_percent'))
    rej_amt = _to_num(final_rollup.get('rejection_amount'))
    tst_amt = _to_num(final_rollup.get('testing_amount'))
    prf_amt = _to_num(final_rollup.get('profit_amount'))
    final = _to_num(final_rollup.get('final_cost'))

    currency_code = (final_rollup.get('currency_code') or 'INR').strip() or 'INR'
    conversion_factor = _to_num(final_rollup.get('conversion_factor')) or 1
    final_in_currency = _to_num(final_rollup.get('final_cost_in_currency')) or final
    is_inr = currency_code.upper() == 'INR'
    symbol = _lookup_currency_symbol(currency_code)

    final_display = _fmt_currency(final) if is_inr else f"{symbol}{final_in_currency:,.2f}"

    conv_html = ''
    if not is_inr:
        conv_html = f'''
        <div class="currency-conv">
            <div class="currency-conv-cell"><span class="currency-conv-label">Currency:</span>{escape_html(currency_code)}</div>
            <div class="currency-conv-cell"><span class="currency-conv-label">Conversion Factor:</span>{_fmt_number(conversion_factor)}</div>
            <div class="currency-conv-cell">
                <span class="currency-conv-label">Final Cost ({escape_html(currency_code)}):</span>
                <span class="currency-conv-final">{symbol}{final_in_currency:,.2f}</span>
            </div>
        </div>
        '''

    return f'''
    <section class="final-section">
        <div class="final-head">
            <div class="final-head-title">Final Roll-Up</div>
            <div class="final-head-total"><span class="final-head-total-label">Final Cost / Garment:</span>{final_display}</div>
        </div>
        <div class="final-body">
            <div class="calc">
                <table>
                    <tr><td></td><td>Fabric (per piece)</td><td class="num">{_fmt_currency(fabric)}</td></tr>
                    <tr><td>+</td><td>Trim</td><td class="num">{_fmt_currency(trim)}</td></tr>
                    <tr><td>+</td><td>Print</td><td class="num">{_fmt_currency(print_c)}</td></tr>
                    <tr><td>+</td><td>Embroidery</td><td class="num">{_fmt_currency(embroidery)}</td></tr>
                    <tr><td>+</td><td>SAM</td><td class="num">{_fmt_currency(sam)}</td></tr>
                    <tr class="subtotal"><td>=</td><td>Base Cost</td><td class="num">{_fmt_currency(base)}</td></tr>
                    <tr><td>+</td><td>Rejection ({_fmt_percent(rej_pct)})</td><td class="num">{_fmt_currency(rej_amt)}</td></tr>
                    <tr><td>+</td><td>Testing ({_fmt_percent(tst_pct)})</td><td class="num">{_fmt_currency(tst_amt)}</td></tr>
                    <tr class="warning"><td>+</td><td>Profit ({_fmt_percent(prf_pct)})</td><td class="num">{_fmt_currency(prf_amt)}</td></tr>
                    <tr class="total"><td>=</td><td>Final Cost / Garment (INR)</td><td class="num">{_fmt_currency(final)}</td></tr>
                </table>
            </div>
            {conv_html}
        </div>
    </section>
    '''

def _lookup_currency_symbol(code):
    if not code:
        return '₹'
    code_upper = (code or '').strip().upper()
    if code_upper == 'INR':
        return '₹'
    try:
        if frappe.db.exists('DocType', 'Costing Currency'):
            symbol = frappe.db.get_value('Costing Currency', {'currency_code': code}, 'currency_symbol')
            if symbol:
                return symbol
    except Exception:
        pass
    return code + ' '

def _fmt_number(value):
    if _is_blankish(value):
        return '—'
    try:
        n = float(value)
    except Exception:
        return '—'
    if n == int(n):
        return f"{int(n):,}"
    return f"{n:,.4f}".rstrip('0').rstrip('.')

def _render_section_for_pdf(section, idx):
    row = section or {}
    data = row.get('costing') or {}
    section_name = row.get('section')
    name = escape_html(section_name) if not _is_blankish(section_name) else f'Section {idx + 1}'
    cpk = data.get('cost_per_kg') or {}
    breakup = cpk.get('breakup') or {}
    base_total = _to_num(cpk.get('total_fabric_cost'))
    adjustment_percent = _to_num(data.get('adjustment_percent')) if data.get('adjustment_percent') not in (None, '') else 0.0
    adjustment_amount = base_total * adjustment_percent / 100.0
    adjusted_total_raw = data.get('adjusted_total_fabric_cost')
    adjusted_total = _to_num(adjusted_total_raw) if adjusted_total_raw not in (None, '') else (base_total + adjustment_amount)

    base_total_fmt = _fmt_currency(base_total)
    adjustment_amount_fmt = _fmt_signed_currency(adjustment_amount)
    adjusted_total_fmt = _fmt_currency(adjusted_total)
    adjustment_pct_fmt = _fmt_percent(adjustment_percent)

    kg_per_piece = _to_num(data.get('kg_per_piece'))
    if kg_per_piece <= 0:
        kg_per_piece = 0.1
    grams_per_piece = kg_per_piece * 1000
    cost_per_piece_raw = data.get('cost_per_piece')
    cost_per_piece = _to_num(cost_per_piece_raw) if cost_per_piece_raw not in (None, '') else (adjusted_total * kg_per_piece)
    grams_fmt = str(int(grams_per_piece)) if grams_per_piece == int(grams_per_piece) else f"{grams_per_piece:.2f}"
    cost_per_piece_fmt = _fmt_currency(cost_per_piece)

    gsm_value = data.get('gsm')
    gsm_needed_value = data.get('gsm_needed')
    gsm_display = gsm_value
    if _is_gsm_mismatch(gsm_value, gsm_needed_value):
        gsm = escape_html(str(gsm_value)) if gsm_value not in (None, '') else '—'
        gsm_needed = escape_html(str(gsm_needed_value)) if gsm_needed_value not in (None, '') else '—'
        gsm_display = f"<span class='gsm-mismatch'>{gsm}</span> ({gsm_needed})"

    loss_value = data.get('loss_percent')
    left_rows = [
        ('Fabric Code', data.get('fabric_code')),
        ('Construction', data.get('construction')),
        ('Blend', data.get('blend')),
        ('GSM', gsm_display),
        ('Shade', data.get('shade_category')),
        ('Grey Fabric', (data.get('grey_fabric') or {}).get('code') or (data.get('grey_fabric') or {}).get('id')),
        ('Finish', data.get('mechanical_chemical_finish')),
        ('Width', data.get('fabric_width')),
        ('Loss', f"{loss_value}%" if not _is_blankish(loss_value) else None),
    ]
    kv_rows = ''.join(
        f"<tr><td>{escape_html(label)}</td><td>{value if label == 'GSM' and _is_gsm_mismatch(gsm_value, gsm_needed_value) else _safe_text(value)}</td></tr>"
        for label, value in left_rows
    )

    calc_rows = _render_calc_rows(data, cpk, breakup)
    heads = _render_cost_heads_for_pdf(breakup)
    desc = _safe_text(data.get('fabric_description')) if data.get('fabric_description') else '<span class="muted">No description</span>'

    return f'''
    <section class="section">
        <div class="section-head">
            <div class="section-title">{name}</div>
            <div class="section-total">
                <span class="section-total-line"><span class="section-total-label">Base / kg:</span> <span class="section-total-base">{base_total_fmt}</span></span>
                <span class="section-total-line section-total-adjust"><span class="section-total-label">Adjustment ({adjustment_pct_fmt}):</span> <span>{adjustment_amount_fmt}</span></span>
                <span class="section-total-line"><span class="section-total-label">Final / kg:</span> <span class="section-total-value">{adjusted_total_fmt}</span></span>
                <span class="section-total-line"><span class="section-total-label">Consumption (Grams / Piece):</span> <span>{grams_fmt}</span></span>
                <span class="section-total-line"><span class="section-total-label">Cost / Piece:</span> <span class="section-total-value">{cost_per_piece_fmt}</span></span>
            </div>
        </div>
        <div class="section-body">
            <div class="desc">{desc}</div>
            <div class="fab-grid">
                <div class="kv"><table>{kv_rows}</table></div>
                <div class="calc"><table>{calc_rows}</table></div>
            </div>
            <div class="heads">{heads}</div>
        </div>
    </section>
    '''

def _render_calc_rows(data, cpk, breakup):
    head_keys = [
        ('yarn', 'Yarn'),
        ('knitting', 'Knitting'),
        ('dyes_and_chemicals', 'Dyes & Chemicals'),
        ('mechanical_chemical_finish', 'Mech / Chem Finish'),
        ('finishing_charges', 'Finishing'),
    ]

    sum_heads = sum(_to_num((breakup.get(key) or {}).get('cost_per_kg')) for key, _ in head_keys)
    gross_raw = cpk.get('gross_total_cost')
    gross = _to_num(gross_raw) if gross_raw not in (None, '') else sum_heads
    loss_amt = _to_num(cpk.get('loss_amount'))
    total = _to_num(cpk.get('total_fabric_cost'))
    loss_pct = _to_num(data.get('loss_percent')) if data.get('loss_percent') is not None else None

    rows = []
    for i, (key, label) in enumerate(head_keys):
        val = _to_num((breakup.get(key) or {}).get('cost_per_kg'))
        rows.append(
            f"<tr><td>{'' if i == 0 else '+'}</td><td>{escape_html(label)}</td><td class='num'>{_fmt_currency(val)}</td></tr>"
        )
    rows.append(f"<tr class='subtotal'><td>=</td><td>Gross / kg</td><td class='num'>{_fmt_currency(gross)}</td></tr>")
    loss_label = f"Loss ({_fmt_percent(loss_pct)})" if loss_pct is not None else 'Loss'
    rows.append(f"<tr class='warning'><td>+</td><td>{escape_html(loss_label)}</td><td class='num'>{_fmt_currency(loss_amt)}</td></tr>")
    rows.append(f"<tr class='total'><td>=</td><td>Total / kg</td><td class='num'>{_fmt_currency(total)}</td></tr>")
    return ''.join(rows)

def _render_cost_heads_for_pdf(breakup):
    return ''.join([
        _render_yarn_card(breakup.get('yarn') or {}),
        _render_knitting_card(breakup.get('knitting') or {}),
        _render_dyes_card(breakup.get('dyes_and_chemicals') or {}),
        _render_mnc_card(breakup.get('mechanical_chemical_finish') or {}),
        _render_finishing_card(breakup.get('finishing_charges') or {}),
    ])

def _render_card_shell(title, total, body):
    return f'''
    <div class="card">
        <div class="card-head">
            <span>{escape_html(title)}</span>
            <span>{_fmt_currency(total)}</span>
        </div>
        <div class="card-body">{body}</div>
    </div>
    '''

def _render_yarn_card(data):
    yarns = data.get('yarns') or []
    rows = []
    for y in yarns:
        has_err = bool(y.get('ERROR'))
        desc = escape_html(y.get('ERROR') if has_err else (y.get('description') or '—'))
        code = escape_html(y.get('code') or '—')
        rows.append(
            f"<tr><td>{code}</td><td>{desc}</td><td class='num'>{_fmt_percent(y.get('percent'))}</td><td class='num'>{_fmt_currency(y.get('rate_per_kg'))}</td><td class='num'>{_fmt_currency(y.get('effective_cost_per_kg'))}</td></tr>"
        )
    body = f'''
    <table class="tbl">
        <thead><tr><th>Code</th><th>Description</th><th class="num">%</th><th class="num">Rate / kg</th><th class="num">Eff. / kg</th></tr></thead>
        <tbody>{''.join(rows) or '<tr><td colspan="5" class="muted">No yarn entries.</td></tr>'}</tbody>
    </table>
    '''
    return _render_card_shell('Yarn', data.get('cost_per_kg'), body)

def _render_knitting_card(data):
    code = _safe_text(data.get('code'))
    err = f"<div class='muted' style='margin-top:4px;'>{escape_html(data.get('ERROR'))}</div>" if data.get('ERROR') else ''
    body = f"<div><strong>Code:</strong> {code}{err}</div>"
    return _render_card_shell('Knitting', data.get('cost_per_kg'), body)

def _render_dyes_card(data):
    breakup = data.get('breakup') or {}
    known = [
        ('dnc_single_pass', 'Single Pass'),
        ('dnc_double_pass', 'Double Pass'),
        ('aop', 'AOP'),
        ('digital', 'Digital'),
    ]
    rows = []
    for key, label in known:
        if breakup.get(key) is not None:
            rows.append(f"<tr><td>{escape_html(label)}</td><td class='num'>{_fmt_currency(breakup.get(key))}</td></tr>")
    for key, val in breakup.items():
        if key not in [k for k, _ in known]:
            rows.append(f"<tr><td>{escape_html(_humanize(key))}</td><td class='num'>{_fmt_currency(val)}</td></tr>")
    body = f"<table class='tbl'><thead><tr><th>Component</th><th class='num'>Cost / kg</th></tr></thead><tbody>{''.join(rows) or '<tr><td colspan=\"2\" class=\"muted\">No breakup available.</td></tr>'}</tbody></table>"
    return _render_card_shell('Dyes & Chemicals', data.get('cost_per_kg'), body)

def _render_mnc_card(data):
    procs = data.get('processes') or {}
    mech_rows = ''.join(
        f"<tr><td>{escape_html((p or {}).get('process_type') or '—')}</td><td class='num'>{_fmt_currency((p or {}).get('cost'))}</td></tr>"
        for p in (procs.get('mechanical') or [])
    ) or "<tr><td colspan='2' class='muted'>No mechanical processes.</td></tr>"
    chem_rows = ''.join(
        f"<tr><td>{escape_html((p or {}).get('process_type') or '—')}</td><td class='num'>{_fmt_currency((p or {}).get('cost'))}</td></tr>"
        for p in (procs.get('chemical') or [])
    ) or "<tr><td colspan='2' class='muted'>No chemical processes.</td></tr>"
    body = f"""
    <div><strong>Mechanical</strong></div>
    <table class='tbl'><thead><tr><th>Process</th><th class='num'>Cost / kg</th></tr></thead><tbody>{mech_rows}</tbody></table>
    <div style='height:6px;'></div>
    <div><strong>Chemical</strong></div>
    <table class='tbl'><thead><tr><th>Process</th><th class='num'>Cost / kg</th></tr></thead><tbody>{chem_rows}</tbody></table>
    """
    return _render_card_shell('Mech / Chem Finish', data.get('cost_per_kg'), body)

def _render_finishing_card(data):
    blend = data.get('blend') or []
    breakup = data.get('breakup') or {}
    known = [
        ('dryer', 'Dryer'),
        ('stentor_1st_pass', 'Stentor 1st Pass'),
        ('stentor_2nd_pass', 'Stentor 2nd Pass'),
        ('compactor', 'Compactor'),
    ]
    rows = []
    for key, label in known:
        if breakup.get(key) is not None:
            rows.append(f"<tr><td>{escape_html(label)}</td><td class='num'>{_fmt_currency_or_na(breakup.get(key))}</td></tr>")
    for key, val in breakup.items():
        if key not in [k for k, _ in known]:
            rows.append(f"<tr><td>{escape_html(_humanize(key))}</td><td class='num'>{_fmt_currency_or_na(val)}</td></tr>")

    chips = ''.join(f"<span class='chip'>{escape_html(str(b))}</span>" for b in blend)
    chips_html = f"<div class='chips'>{chips}</div>" if chips else ''
    body = f"{chips_html}<table class='tbl'><thead><tr><th>Process</th><th class='num'>Cost / kg</th></tr></thead><tbody>{''.join(rows) or '<tr><td colspan=\"2\" class=\"muted\">No breakup available.</td></tr>'}</tbody></table>"
    return _render_card_shell('Finishing', data.get('cost_per_kg'), body)

def _to_num(value):
    if _is_blankish(value):
        return 0.0
    try:
        return float(value)
    except Exception:
        return 0.0

def _fmt_currency(value):
    if _is_blankish(value):
        return '—'
    try:
        return f"₹{float(value):,.2f}"
    except Exception:
        return '—'

def _fmt_signed_currency(value):
    if _is_blankish(value):
        return '—'
    try:
        n = float(value)
        if abs(n) < 0.0001:
            return _fmt_currency(0)
        sign = '+' if n > 0 else '-'
        return f"{sign}{_fmt_currency(abs(n))}"
    except Exception:
        return '—'

def _fmt_currency_or_na(value):
    if _is_blankish(value):
        return 'N/A'
    try:
        n = float(value)
    except Exception:
        return 'N/A'
    if n == 0:
        return 'N/A'
    return _fmt_currency(n)

def _fmt_percent(value):
    if _is_blankish(value):
        return '—'
    try:
        return f"{float(value):,.2f}%"
    except Exception:
        return '—'

def _fmt_date(value):
    if not value:
        return ''
    return frappe.utils.format_datetime(value, "dd MMM yyyy")

def _safe_text(value):
    if _is_blankish(value):
        return '<span class="muted">—</span>'
    return escape_html(str(value))

def _is_blankish(value):
    if value is None:
        return True
    if isinstance(value, str):
        v = value.strip().lower()
        return v in ('', 'none', 'null', 'nan')
    return False

def _short_owner(owner):
    if not owner:
        return '—'
    owner = str(owner)
    idx = owner.find('@')
    return owner[:idx] if idx > 0 else owner

def _humanize(key):
    return str(key or '').replace('_', ' ').title()

def _is_gsm_mismatch(gsm, gsm_needed):
    if _is_blankish(gsm) or _is_blankish(gsm_needed):
        return False
    try:
        return abs(float(gsm) - float(gsm_needed)) > 0.0001
    except Exception:
        return str(gsm).strip() != str(gsm_needed).strip()
