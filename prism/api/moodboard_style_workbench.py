import json

import frappe
from frappe.utils import cint

import prism.lib.cloud as cloud

MAX_PAGE_SIZE = 100

# The Style3D file slots tracked by the workbench (n/4 pill).
FILE_FIELDS = ['model_3d', 'garment_file', 'bom', 'product_video']


@frappe.whitelist()
def get_list(search=None, page=1, page_size=20):
    '''
    Paginated list of Moodboard Style records for the master panel.
    Returns: name (PRA number), garment_name, product_category, moodboard,
    creation, and a files_count (0..3) for the upload-progress pill.
    '''
    try:
        page = max(1, cint(page) or 1)
        page_size = min(max(1, cint(page_size) or 20), MAX_PAGE_SIZE)

        or_filters = None
        if search and str(search).strip():
            term = f"%{str(search).strip()}%"
            or_filters = [
                ['name', 'like', term],
                ['garment_name', 'like', term],
                ['product_category', 'like', term],
                ['fabric_quality', 'like', term],
            ]

        rows = frappe.get_all(
            'Moodboard Style',
            or_filters=or_filters,
            fields=[
                'name', 'garment_name', 'product_category', 'moodboard',
                'gender', 'creation', 'modified', 'model_3d', 'garment_file', 'bom',
                'product_video',
            ],
            order_by='creation desc',
            start=(page - 1) * page_size,
            page_length=page_size,
        )

        for r in rows:
            r['files_count'] = sum(1 for f in FILE_FIELDS if r.get(f))
            for f in FILE_FIELDS:
                r.pop(f, None)

        if or_filters:
            total = len(frappe.get_all(
                'Moodboard Style',
                or_filters=or_filters,
                fields=['name'],
                limit_page_length=0,
            ))
        else:
            total = frappe.db.count('Moodboard Style')

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
        frappe.log_error(frappe.get_traceback(), 'moodboard_style_workbench.get_list')
        return {'status': False, 'error': str(ex)}


@frappe.whitelist()
def get_record(name):
    '''
    Full Moodboard Style record for the detail panel. Parses the `cost_inputs`
    JSON so the front end can render the fabric elements table, and resolves the
    Style3D file fields (model_3d / garment_file / bom) to servable URLs.
    '''
    try:
        if not name:
            return {'status': False, 'error': 'name is required'}

        doc = frappe.get_doc('Moodboard Style', name)

        try:
            cost_inputs = json.loads(doc.cost_inputs) if doc.cost_inputs else {}
        except Exception:
            cost_inputs = {}
        if not isinstance(cost_inputs, dict):
            cost_inputs = {}

        fabrics = cost_inputs.get('fabrics')
        if not isinstance(fabrics, list):
            fabrics = []

        moodboard_title = ''
        if doc.moodboard:
            moodboard_title = frappe.db.get_value(
                'Moodboard', doc.moodboard, 'moodboard_title'
            ) or ''

        data = {
            'name': doc.name,
            'moodboard': doc.moodboard,
            'moodboard_title': moodboard_title,
            'idx': doc.idx or 0,
            'gender': doc.gender or '',
            'product_category': doc.product_category or '',
            'garment_name': doc.garment_name or '',
            'description': doc.description or '',
            'image': doc.image or '',
            'fabric_quality': doc.fabric_quality or '',
            'element_colour': doc.element_colour or '',
            'element_colour_hex': doc.element_colour_hex or '',
            'element_colour_tcx': doc.element_colour_tcx or '',
            'moq': doc.moq or 0,
            'owner': doc.owner,
            'creation': doc.creation,
            'style': cost_inputs.get('style') or '',
            'fabrics': fabrics,
            'files': {
                'model_3d': cloud.asset_url(doc.model_3d),
                'garment_file': cloud.asset_url(doc.garment_file),
                'bom': cloud.asset_url(doc.bom),
                'product_video': cloud.asset_url(doc.product_video),
            },
        }

        return {'status': True, 'data': data}
    except frappe.DoesNotExistError:
        return {'status': False, 'error': 'Record not found'}
    except Exception as ex:
        frappe.log_error(frappe.get_traceback(), 'moodboard_style_workbench.get_record')
        return {'status': False, 'error': str(ex)}
