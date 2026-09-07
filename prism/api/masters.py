import json
import re

import frappe
from frappe.query_builder import DocType
from frappe.query_builder.functions import Count, Lower

from pypika import Order

from prism.auth.authenticator import auth_required


@frappe.whitelist(allow_guest=True)
@auth_required
def brand_style_genders(brands: list):
    ''' 
    Returns distinct genders for the given brands, 
    from doctype "Brand Gender Style Category". 
    '''
    try:
        if isinstance(brands, str):
            brands = frappe.parse_json(brands)

        if not brands or not isinstance(brands, list):
            return {'success': True, 'genders': []}

        rows = frappe.get_all(
            'Brand Gender Style Category',
            filters={'brand': ['in', brands]},
            fields=['gender'],
        )

        parsed_genders = {}
        for row in rows:
            value = (row.get('gender') or '').strip()
            if value and value.lower() not in parsed_genders:
                parsed_genders[value.lower()] = value

        genders = sorted(parsed_genders.values(), key=lambda g: g.lower())
        return {'success': True, 'genders': genders}

    except Exception as ex:
        frappe.log_error(frappe.get_traceback(), 'masters.brand_style_genders()')
        return {'success': False, 'error': str(ex)}

@frappe.whitelist(allow_guest=True)
@auth_required
def brand_style_seasons():
    ''' 
    Returns all seasons from the doctype "Season". 
    '''
    try:
        seasons = frappe.get_all(
            'Season',
            fields=['code', 'description', 'display_order'],
            order_by='display_order asc, code asc',
        )

        return {'success': True, 'seasons': seasons}

    except Exception as ex:
        frappe.log_error(frappe.get_traceback(), 'masters.brand_style_seasons()')
        return {'success': False, 'error': str(ex)}

@frappe.whitelist(allow_guest=True)
@auth_required
def brand_style_categories(brands: list, genders: list=None):
    ''' 
    Returns distinct style categories for the given brands, optionally 
    filtered by genders, from doctype "Brand Gender Style Category". 
    '''
    try:
        if not brands or not isinstance(brands, list):
            return {'success': True, 'style_categories': []}

        filters = {'brand': ['in', brands]}
        if genders:
            filters['gender'] = ['in', genders]

        rows = frappe.get_all(
            'Brand Gender Style Category',
            filters=filters,
            fields=['style_categories'],
        )

        parsed_categories = {}

        for row in rows:
            raw = (row.get('style_categories') or '').strip()
            if not raw:
                continue

            # Accept json arrays, newline-separated, comma-separated and semicolon-separated values.
            values = []
            try:
                as_json = json.loads(raw)
                if isinstance(as_json, list):
                    values = [str(v).strip() for v in as_json]
            except Exception:
                values = [v.strip() for v in re.split(r'[\n,;]+', raw)]

            for value in values:
                if value and value.lower() not in parsed_categories:
                    parsed_categories[value.lower()] = value

        categories = sorted(parsed_categories.values(), key=lambda c: c.lower())
        return {'success': True, 'style_categories': categories}

    except Exception as ex:
        frappe.log_error(frappe.get_traceback(), 'masters.brand_style_categories()')
        return {'success': False, 'error': str(ex)}

@frappe.whitelist(allow_guest=True)
#@auth_required
def search_fabric_constractions(search_str: str):
    ''' Returns max 10 closest matching (like) 'fabric' values from doctype "Fabric Master". '''
    try:
        search_str = (search_str or '').strip()
        if not search_str:
            return {'success': True, 'data': []}

        rows = frappe.get_all(
            'Fabric Master',
            filters={'fabric': ['like', f'%{search_str}%']},
            fields=['fabric as construction'],
            distinct=True,
            order_by='fabric asc',  # improve order by
            limit_page_length=10,
        )

        return {'success': True, 'data': rows}

    except Exception as ex:
        frappe.log_error(frappe.get_traceback(), 'masters.search_fabric_constractions()')
        return {'success': False, 'error': str(ex)}

@frappe.whitelist(allow_guest=True)
#@auth_required
def search_fabric_blends(search_str: str):
    ''' Returns max 10 closest matching (like) 'blend' values from doctype "Fabric Master". '''
    try:
        search_str = (search_str or '').strip()
        if not search_str:
            return {'success': True, 'data': []}

        rows = frappe.get_all(
            'Fabric Master',
            filters={'blend': ['like', f'%{search_str}%']},
            fields=['blend'],
            distinct=True,
            order_by='blend asc',  # improve order by
            limit_page_length=10,
        )

        return {'success': True, 'data': rows}

    except Exception as ex:
        frappe.log_error(frappe.get_traceback(), 'masters.search_fabric_blends()')
        return {'success': False, 'error': str(ex)}

@frappe.whitelist(allow_guest=True)
def get_distinct_values(doctype_name: str, field_name: str):
    ret_val = {
        'count': 0,
        'values': []
    }

    rows = frappe.get_all(
        doctype_name,
        filters={field_name: ['is', 'set']},
        fields=[field_name],
        distinct=True,
        order_by=f'{field_name} asc',
        ignore_permissions=True
    )

    if rows:
        ret_val['count'] = len(rows)
        ret_val['values'] = [row.get(field_name or '') for row in rows]

    #return '\n'.join([row.get(field_name or '') for row in rows])
    return ret_val

@frappe.whitelist(allow_guest=True)
@auth_required
def get_style_trims(style: str):
    try:
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

        if rows:
            for row in rows:
                row['cost'] = round((row['unit_price'] * row['units']), 2)

        return {'success': True, 'data': rows}

    except Exception as ex:
        frappe.log_error(frappe.get_traceback(), 'masters.brand_style_categories()')
        return {'success': False, 'error': str(ex)}

@frappe.whitelist(allow_guest=True)
@auth_required
def garment_print_types():
    try:
        rows = frappe.get_all(
            'Print Type Master',
            fields=['print_type_name as print_type', 'cost_per_inch', 'manpower_cost'],
            order_by='print_type_name asc',
            ignore_permissions=True,
        )
        return {'success': True, 'data': rows}
    except Exception as ex:
        frappe.log_error(frappe.get_traceback(), 'masters.garment_print_types()')
        return {'success': False, 'error': str(ex)}

@frappe.whitelist(allow_guest=True)
@auth_required
def garment_styles():
    try:
        rows = frappe.get_all(
            'Trim Costing',
            fields=['style_name'],
            distinct=True,
            ignore_permissions=True
        )
        return {'success': True, 'data': rows}
    except Exception as ex:
        frappe.log_error(frappe.get_traceback(), 'masters.garment_styles()')
        return {'success': False, 'error': str(ex)}

@frappe.whitelist(allow_guest=True)
@auth_required
def thread_types():
    try:
        rows = frappe.get_all(
            'Embroidery Thread Master',
            fields=['thread_type'],
            distinct=True,
            ignore_permissions=True
        )
        return {'success': True, 'data': rows}
    except Exception as ex:
        frappe.log_error(frappe.get_traceback(), 'masters.thread_types()')
        return {'success': False, 'error': str(ex)}

@frappe.whitelist(allow_guest=True)
@auth_required
def embroidery_types():
    try:
        rows = frappe.get_all(
            'Embroidery Type Master',
            fields=['emb_type'],
            distinct=True,
            ignore_permissions=True
        )
        return {'success': True, 'data': rows}
    except Exception as ex:
        frappe.log_error(frappe.get_traceback(), 'masters.embroidery_types()')
        return {'success': False, 'error': str(ex)}

#--- moodboard ---
DOCTYPE_STYLE_MASTER = 'Sample Request'

@frappe.whitelist(allow_guest=True)
def list_all_styles(
    search_text: str=None,

    product_category: str=None,
    gender: str=None,
    fabric_quality: str=None,
    fabric_blend: str=None,
    element_colour: str=None,

    page: int=1,
    page_size: int=20,
    order_by: str='creation',
    order_dir: str='desc'
):
    '''
    Returns a paginated list of the "Sample Request" doctype docs with filtering and sorting.
    '''
    try:
        # pagination sanitization
        page = max(1, int(page))
        page_size = min(max(1, int(page_size)), 100)
        offset = (page - 1) * page_size

        # ordering validation
        order_dir = order_dir.lower()
        order = Order.asc if order_dir == 'asc' else Order.desc

        allowed_order_fields = ['creation', 'gender', 'product_category']
        if order_by not in allowed_order_fields:
            order_by = 'creation'

        # DocTypes
        SampleRequest = DocType(DOCTYPE_STYLE_MASTER)


        # ----------------------------
        #  queries
        # ----------------------------
        count_query = frappe.qb.from_(SampleRequest).select(Count(SampleRequest.name).as_('total'))

        base_query = frappe.qb.from_(SampleRequest)


        # build and apply filters
        conditions = []
        conditions.append(SampleRequest.garment_element == 'Main Body')

        if search_text:
            search_text = re.sub(r'\s+', '%', search_text)
            like_pattern = f'%{search_text.lower()}%'

            conditions.append(
                (Lower(SampleRequest.gsr_no).like(like_pattern)) |
                (Lower(SampleRequest.product_category).like(like_pattern))
            )

        if gender is not None:
            conditions.append(SampleRequest.gender == gender)

        if product_category is not None:
            conditions.append(SampleRequest.product_category == product_category)

        if fabric_quality is not None:
            conditions.append(SampleRequest.fabric_quality == fabric_quality)

        if fabric_blend is not None:
            conditions.append(SampleRequest.fabric_blend == fabric_blend)

        if element_colour is not None:
            conditions.append(SampleRequest.element_colour == element_colour)

        for condition in conditions:
            base_query = base_query.where(condition)
            count_query = count_query.where(condition)

        # run count query
        total_count = count_query.run(as_dict=True)[0]['total']

        # run main query
        data_query = (
            base_query
            .select(
                SampleRequest.name.as_('id'),
                SampleRequest.gsr_no,
                SampleRequest.gender,
                SampleRequest.product_category,
                SampleRequest.fabric_quality,
                SampleRequest.fabric_blend,
                SampleRequest.element_colour,
                SampleRequest.finished_gsm,
                SampleRequest.garment_element,
                SampleRequest.creation,
            )
            .orderby(getattr(SampleRequest, order_by), order=order)
            .limit(page_size)
            .offset(offset)
        )

        rows = data_query.run(as_dict=True)

        # pagination metadata
        total_pages = (total_count + page_size - 1) // page_size if total_count > 0 else 0

        return {
            'success': True,
            'data': rows,
            'pagination': {
                'page': page,
                'page_size': page_size,
                'total_count': total_count,
                'total_pages': total_pages,
                'has_next': page < total_pages,
                'has_previous': page > 1,
            }
            #'sql': data_query.get_sql()
        }

    except Exception as ex:
        frappe.log_error(frappe.get_traceback(), 'garment.list_all()')
        return {'success': False, 'error': str(ex)}

@frappe.whitelist(allow_guest=True)
@auth_required
def moodboard_genders(categories:list=None, styles:list=None):
    try:
        filters = {'gender': ['is', 'set']}
        if categories and isinstance(categories, list):
            filters['product_group'] = ['in', categories]
        if styles and isinstance(styles, list):
            filters['product_category'] = ['in', styles]

        rows = frappe.get_all(
            DOCTYPE_STYLE_MASTER,
            filters=filters,
            fields=['gender'],
            distinct=True,
            order_by='gender asc',
            ignore_permissions=True
        )
        return {'success': True, 'data': rows}
    except Exception as ex:
        frappe.log_error(frappe.get_traceback(), 'masters.moodboard_genders()')
        return {'success': False, 'error': str(ex)}

@frappe.whitelist(allow_guest=True)
@auth_required
def moodboard_style_categories(genders:list=None, styles:list=None):
    try:
        filters = {'product_group': ['is', 'set']}
        if genders and isinstance(genders, list):
            filters['gender'] = ['in', genders]
        if styles and isinstance(styles, list):
            filters['product_category'] = ['in', styles]

        rows = frappe.get_all(
            DOCTYPE_STYLE_MASTER,
            filters=filters,
            fields=['product_group'],
            distinct=True,
            order_by='product_group asc',
            ignore_permissions=True
        )
        return {'success': True, 'data': rows}
    except Exception as ex:
        frappe.log_error(frappe.get_traceback(), 'masters.moodboard_style_categories()')
        return {'success': False, 'error': str(ex)}

@frappe.whitelist(allow_guest=True)
@auth_required
def moodboard_styles(genders:list=None, categories:list=None):
    try:
        filters = {'product_category': ['is', 'set']}
        if genders and isinstance(genders, list):
            filters['gender'] = ['in', genders]
        if categories and isinstance(categories, list):
            filters['product_group'] = ['in', categories]

        rows = frappe.get_all(
            DOCTYPE_STYLE_MASTER,
            filters=filters,
            fields=['product_category as style'],
            distinct=True,
            order_by='product_category asc',
            ignore_permissions=True
        )
        return {'success': True, 'data': rows}
    except Exception as ex:
        frappe.log_error(frappe.get_traceback(), 'masters.moodboard_styles()')
        return {'success': False, 'error': str(ex)}

#--- fabric ---
DOCTYPE_FABRIC_MASTER = 'Moodboard Dyed Fabric'

@frappe.whitelist(allow_guest=True)
@auth_required
def fabric_qualities(blends:list=None):
    try:
        filters = {'clean_quality': ['is', 'set']}
        if blends and isinstance(blends, list):
            filters['clean_blend'] = ['in', blends]

        rows = frappe.get_all(
            DOCTYPE_FABRIC_MASTER,
            filters=filters,
            fields=['clean_quality as quality'],
            distinct=True,
            order_by='clean_quality asc',
            ignore_permissions=True
        )
        return {'success': True, 'data': rows}
    except Exception as ex:
        frappe.log_error(frappe.get_traceback(), 'masters.fabric_qualities()')
        return {'success': False, 'error': str(ex)}

@frappe.whitelist(allow_guest=True)
@auth_required
def fabric_blends(qualities:list=None):
    try:
        filters = {'clean_blend': ['is', 'set']}
        if qualities and isinstance(qualities, list):
            filters['clean_quality'] = ['in', qualities]

        rows = frappe.get_all(
            DOCTYPE_FABRIC_MASTER,
            filters=filters,
            fields=['clean_blend as blend'],
            distinct=True,
            order_by='quality_sort_order asc, clean_blend asc',
            ignore_permissions=True
        )
        return {'success': True, 'data': rows}
    except Exception as ex:
        frappe.log_error(frappe.get_traceback(), 'masters.fabric_blends()')
        return {'success': False, 'error': str(ex)}
