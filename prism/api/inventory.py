import frappe
from frappe.utils import cint

FILTERABLE_FIELDS = [
    'material_type', 'plant', 'division', 'overall_status',
    'storage_location', 'inventory_type', 'material_group',
]
SEARCHABLE_FIELDS = ['stock_material', 'material_description', 'customer_name', 'batch_sid']
SORTABLE_FIELDS = [
    'stock_material', 'material_type', 'stock_quantity', 'plant',
    'overall_status', 'material_group', 'creation', 'modified',
]
LIST_FIELDS = [
    'name', 'stock_material', 'material_type', 'stock_quantity', 'base_unit_of_measure',
    'plant', 'division', 'overall_status', 'storage_location', 'material_description',
    'inventory_type', 'material_group', 'customer_name', 'batch_sid',
    'creation', 'modified',
]
LOW_STOCK_THRESHOLD = 10
MAX_PAGE_SIZE = 200


@frappe.whitelist()
def get_inventory_list(filters=None, search=None, sort_by='creation', sort_order='desc', page=1, page_size=25):
    try:
        filters = frappe.parse_json(filters) if isinstance(filters, str) else (filters or {})

        page = max(1, cint(page) or 1)
        page_size = min(max(1, cint(page_size) or 25), MAX_PAGE_SIZE)

        if sort_by not in SORTABLE_FIELDS:
            sort_by = 'creation'
        sort_order = 'asc' if str(sort_order).lower() == 'asc' else 'desc'

        safe_filters = {}
        for k, v in (filters or {}).items():
            if k not in FILTERABLE_FIELDS or v in (None, '', []):
                continue
            if isinstance(v, list):
                v = [x for x in v if x not in (None, '')]
                if v:
                    safe_filters[k] = ['in', v]
            else:
                safe_filters[k] = v

        or_filters = None
        if search and str(search).strip():
            term = f"%{str(search).strip()}%"
            or_filters = [[f, 'like', term] for f in SEARCHABLE_FIELDS]

        rows = frappe.get_all(
            'Inventory',
            filters=safe_filters or None,
            or_filters=or_filters,
            fields=LIST_FIELDS,
            order_by=f'{sort_by} {sort_order}',
            start=(page - 1) * page_size,
            page_length=page_size,
        )

        mt_codes = {r.get('material_type') for r in rows if r.get('material_type')}
        if mt_codes:
            descs = frappe.get_all(
                'Material Type',
                filters={'name': ['in', list(mt_codes)]},
                fields=['name', 'description'],
            )
            desc_map = {d['name']: d.get('description') for d in descs}
            for r in rows:
                r['material_type_description'] = desc_map.get(r.get('material_type'))

        if or_filters:
            total_rows = frappe.get_all(
                'Inventory',
                filters=safe_filters or None,
                or_filters=or_filters,
                fields=['name'],
                limit_page_length=0,
            )
            total = len(total_rows)
        else:
            total = frappe.db.count('Inventory', filters=safe_filters or None)

        return {
            'status': True,
            'data': {
                'rows': rows,
                'total': total,
                'page': page,
                'page_size': page_size,
                'sort_by': sort_by,
                'sort_order': sort_order,
            },
        }
    except Exception as ex:
        frappe.log_error(frappe.get_traceback(), 'inventory.get_inventory_list')
        return {'status': False, 'error': str(ex)}

@frappe.whitelist()
def get_inventory_filter_options():
    try:
        result = {}
        for field in FILTERABLE_FIELDS:
            rows = frappe.db.sql(
                f"""
                SELECT DISTINCT `{field}` AS val
                FROM `tabInventory`
                WHERE `{field}` IS NOT NULL AND `{field}` != ''
                ORDER BY `{field}` ASC
                LIMIT 500
                """,
                as_dict=True,
            )
            result[field] = [r['val'] for r in rows if r['val'] is not None]

        if result.get('material_type'):
            mt_names = result['material_type']
            descs = frappe.get_all(
                'Material Type',
                filters={'name': ['in', mt_names]},
                fields=['name', 'description'],
            )
            desc_map = {d['name']: d.get('description') for d in descs}
            result['material_type'] = [
                {'value': v, 'label': f'{v} - {desc_map[v]}' if desc_map.get(v) else v}
                for v in mt_names
            ]

        return {'status': True, 'data': result}
    except Exception as ex:
        frappe.log_error(frappe.get_traceback(), 'inventory.get_inventory_filter_options')
        return {'status': False, 'error': str(ex)}

@frappe.whitelist()
def get_inventory_summary():
    try:
        total_items = frappe.db.count('Inventory')

        total_qty_row = frappe.db.sql("SELECT COALESCE(SUM(stock_quantity), 0) FROM `tabInventory`")
        total_qty = int(total_qty_row[0][0]) if total_qty_row else 0

        distinct_materials_row = frappe.db.sql(
            "SELECT COUNT(DISTINCT stock_material) FROM `tabInventory`"
        )
        distinct_materials = int(distinct_materials_row[0][0]) if distinct_materials_row else 0

        low_stock = frappe.db.count('Inventory', filters=[['stock_quantity', '<=', LOW_STOCK_THRESHOLD]])

        by_status = frappe.db.sql(
            """
            SELECT overall_status AS label, COUNT(*) AS value
            FROM `tabInventory`
            WHERE overall_status IS NOT NULL AND overall_status != ''
            GROUP BY overall_status
            ORDER BY value DESC
            LIMIT 10
            """,
            as_dict=True,
        )

        by_material_type = frappe.db.sql(
            """
            SELECT material_type AS label, COUNT(*) AS value
            FROM `tabInventory`
            WHERE material_type IS NOT NULL AND material_type != ''
            GROUP BY material_type
            ORDER BY value DESC
            LIMIT 10
            """,
            as_dict=True,
        )

        cards = [
            {'label': 'Total Items', 'value': total_items},
            {'label': 'Total Stock Qty', 'value': total_qty},
            {'label': 'Distinct Materials', 'value': distinct_materials},
            {'label': f'Low Stock (≤ {LOW_STOCK_THRESHOLD})', 'value': low_stock, 'tone': 'warn' if low_stock else 'ok'},
        ]

        return {
            'status': True,
            'data': {
                'cards': cards,
                'by_status': by_status,
                'by_material_type': by_material_type,
            },
        }
    except Exception as ex:
        frappe.log_error(frappe.get_traceback(), 'inventory.get_inventory_summary')
        return {'status': False, 'error': str(ex)}
