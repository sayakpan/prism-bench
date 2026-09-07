import frappe

DOCTYPE_FABRIC_MASTER = 'Moodboard Dyed Fabric'
DOCTYPE_GARMENT_MASTER = 'Sample Request'

DOCTYPE_GARMENT_GENDER = 'Garment Gender'
DOCTYPE_GARMENT_GROUP = 'Garment Group'
DOCTYPE_GARMENT_CATEGORY = 'Garment Category'


@frappe.whitelist(allow_guest=True)
def fix_style_blend_sort_order(blend_name: str, sort_order: int, is_like:bool=False):
    '''
    Replaces all 'quality_sort_order' in doctype 'Moodboard Dyed Fabric' with
    'sort_order' for which the 'clean_blend' field equals to the 'blend_name'.
    '''
    try:
        blend_filter = {'clean_blend': ['like', f'{blend_name}%'] if is_like else blend_name}
        num_rows = frappe.db.count(
            DOCTYPE_FABRIC_MASTER,
            blend_filter,
        )
        frappe.db.set_value(
            DOCTYPE_FABRIC_MASTER,
            blend_filter,
            'quality_sort_order',
            sort_order,
            update_modified=False,
        )
        frappe.db.commit()
        return {'success': True, 'num_rows_updated': num_rows}
    except Exception as ex:
        frappe.log_error(frappe.get_traceback(), 'patches.fix_style_blend_sort_order()')
        return {'success': False, 'error': str(ex)}

@frappe.whitelist(allow_guest=True)
def clean_garment_image_urls():
    '''
    Update the 'image_urls' and 'image_urls_3d' fields in doctype 'Sample Request' with NULL.

    Older runs of garment.update_image_urls() stored an empty JSON object ('{}')
    on rows that had no matching S3 image. This backfills those leftover values
    to NULL so the two fields are consistently NULL when no image exists.
    '''
    try:
        # value the old code stored when no S3 image matched (now we want NULL instead)
        EMPTY_IMAGE_URLS = '{}'

        GarmentMaster = frappe.qb.DocType(DOCTYPE_GARMENT_MASTER)

        stats = {}
        for field_name in ('image_urls', 'image_urls_3d'):
            column = getattr(GarmentMaster, field_name)

            rows_updated = frappe.db.count(
                DOCTYPE_GARMENT_MASTER, {field_name: EMPTY_IMAGE_URLS}
            )
            if rows_updated:
                (
                    frappe.qb
                    .update(GarmentMaster)
                    .set(column, None)
                    .where(column == EMPTY_IMAGE_URLS)
                    .run()
                )
            stats[field_name] = f'{rows_updated} rows updated.'

        frappe.db.commit()
        return {'success': True, 'stats': stats}

    except Exception as ex:
        frappe.log_error(frappe.get_traceback(), 'patches.clean_garment_image_urls()')
        return {'success': False, 'error': str(ex)}

@frappe.whitelist(allow_guest=True)
def create_garment_masters():
    '''
    1. Insert all distinct values from the "gender" field of doctype "Sample Request" 
       into the "gender_name" field of doctype "Garment Gender".
    2. Insert all distinct values from the "product_group" field of doctype "Sample Request" 
       into the "group_name" field of doctype "Garment Group".
    3. Insert all distinct values from the "product_category" field of doctype "Sample Request" 
       into the "category_name" field of doctype "Garment Category".
    '''
    try:
        # (source field on Sample Request, target doctype, target field)
        mappings = (
            ('gender', DOCTYPE_GARMENT_GENDER, 'gender_name'),
            ('product_group', DOCTYPE_GARMENT_GROUP, 'group_name'),
            ('product_category', DOCTYPE_GARMENT_CATEGORY, 'category_name'),
        )

        stats = {}
        for source_field, target_doctype, target_field in mappings:
            # distinct, non-empty values from Sample Request
            rows = frappe.get_all(
                DOCTYPE_GARMENT_MASTER,
                filters={source_field: ['is', 'set']},
                fields=[source_field],
                distinct=True,
                pluck=source_field,
            )
            values = sorted({v.strip() for v in rows if v and v.strip()})

            # values that already exist as masters (skip to avoid duplicates)
            existing = set(
                frappe.get_all(
                    target_doctype,
                    filters={target_field: ['in', values]} if values else {},
                    pluck=target_field,
                )
            )

            inserted = 0
            for value in values:
                if value in existing:
                    continue
                frappe.get_doc({
                    'doctype': target_doctype,
                    target_field: value,
                }).insert(ignore_permissions=True)
                inserted += 1

            stats[target_doctype] = {
                'distinct_values': len(values),
                'inserted': inserted,
                'skipped_existing': len(values) - inserted,
            }

        frappe.db.commit()
        return {'success': True, 'stats': stats}

    except Exception as ex:
        frappe.log_error(frappe.get_traceback(), 'patches.create_garment_masters()')
        return {'success': False, 'error': str(ex)}
