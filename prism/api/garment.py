import io
import json
from datetime import timezone, timedelta

import frappe
from frappe.query_builder.functions import Count
from pypika.analytics import RowNumber

from prism.auth.authenticator import auth_required
import prism.api.llm as llm
import prism.lib.cloud as cloud
import prism.lib.esg as esg_lib
import prism.api.util as util

GARMENT_MASTER_DOCTYPE = 'Sample Request'
GARMENT_FIBER_DOCTYPE = 'Dyed Fabric Fiber'
FIBER_TYPE_DOCTYPE = 'Fiber Type'
GARMENT_IMAGE_DOCTYPE = 'Sample Request Garment Image'
GARMENT_VIDEO_DOCTYPE = 'Sample Request Product Video'

MAX_IMAGE_FILE_SIZE_MB = 5
MAX_VIDEO_FILE_SIZE_MB = 200


@frappe.whitelist(allow_guest=True)
@auth_required
def list_all(
    search_text: str = None,
    genders: list = [],
    categories: list = [],
    styles: list = [],
    withImgOnly: bool = True,

    order_by: str = 'creation',
    order_dir: str = 'desc',

    page: int = 1,
    page_size: int = 20,
):
    '''
    Returns a paginated list of inventory garments ("Sample Request" rows
    restricted to garment_element = 'Main Body'), optionally filtered by
    gender and/or product_category (styles).

    Args:
        search_text: Optional free-text term matched (case-insensitive LIKE)
                     against gsr_no, developer, and
                     buyer_style_description__pack_name.
        genders:     Optional list of GenderCategory values to filter on (IN).
        categories:  Optional list of product_group values to filter on (IN).
        styles:      Optional list of product_category values to filter on (IN).
        withImgOnly: When True, only rows that have a gsr_no are returned and
                     each row is enriched with front/back `image_urls`.
        order_by:    Column to sort by (whitelisted; defaults to 'creation').
        order_dir:   Sort direction, 'asc' or 'desc' (defaults to 'desc').
        page:        1-based page number.
        page_size:   Number of rows per page (clamped to [1, 100]).

    Returns:
        {
            'success': True,
            'data': [<garment row>, ...],
            'pagination': {
                'page', 'page_size', 'total_count', 'total_pages',
                'has_next', 'has_previous',
            }
        }
    '''

    try:
        # normalize sorting params (whitelisted to avoid injection)
        ALLOWED_ORDER_FIELDS = ['creation', 'gender', 'product_category', 'ai_description']

        page = max(1, int(page or 1))
        page_size = min(100, max(1, int(page_size or 20)))
        offset = (page - 1) * page_size

        if order_by not in ALLOWED_ORDER_FIELDS:
            order_by = 'creation'
        direction = frappe.qb.asc if str(order_dir).lower() == 'asc' else frappe.qb.desc

        # db inventory matches
        GarmentMaster = frappe.qb.DocType(GARMENT_MASTER_DOCTYPE)

        def _get_filters(q):
            q = q.where(GarmentMaster.garment_element == 'Main Body')

            if search_text:
                term = f'%{search_text.strip()}%'
                q = q.where(
                    GarmentMaster.gsr_no.like(term)
                    | GarmentMaster.ai_description.like(term)
                    | GarmentMaster.customer.like(term)
                    | GarmentMaster.developer.like(term)
                )

            if genders:
                q = q.where(GarmentMaster.gender.isin(genders))
            if categories:
                q = q.where(GarmentMaster.product_group.isin(categories))
            if styles:
                q = q.where(GarmentMaster.product_category.isin(styles))
            
            if withImgOnly:
                #q = q.where(GarmentMaster.image_urls.notnull())
                q = q.where(GarmentMaster.image_urls_3d.notnull())
        
            return q

        # ranking based on gsr_no
        row_num = (
            RowNumber()
            .over(GarmentMaster.gsr_no)
            .orderby(GarmentMaster.creation, order=frappe.qb.desc)
        )

        ranked = _get_filters(
            frappe.qb.from_(GarmentMaster)
            .select(
                GarmentMaster.name.as_('id'),
                GarmentMaster.gsr_no,
                GarmentMaster.gender,
                GarmentMaster.product_group.as_('category'),
                GarmentMaster.product_category.as_('style'),
                GarmentMaster.ai_description,
                GarmentMaster.fabric_quality,
                GarmentMaster.fabric_blend,
                GarmentMaster.element_colour,
                GarmentMaster.finished_gsm,
                #GarmentMaster.image_urls.as_('image_urls_raw'),
                GarmentMaster.image_urls_3d.as_('image_urls_raw'),
                GarmentMaster.creation,
                GarmentMaster.clean_blend,
                GarmentMaster.clean_construction,
                row_num.as_('rn'),
            )
        ).as_('ranked')

        # total count for pagination metadata
        # (one row per gender+product_category+fabric_quality+fabric_blend)
        count_query = (
            frappe.qb.from_(ranked)
            .select(Count('*'))
            .where(ranked.rn == 1)
        )
        total_count = count_query.run()[0][0]
        total_pages = (total_count + page_size - 1) // page_size

        #--- page of rows (top row per gender+product_category+fabric_quality+fabric_blend) ---
        query = (
            frappe.qb.from_(ranked)
            .select(
                ranked.id,
                ranked.gsr_no,
                ranked.gender,
                ranked.category,
                ranked.style,
                ranked.ai_description,
                ranked.fabric_quality,
                ranked.fabric_blend,
                ranked.element_colour,
                ranked.finished_gsm,
                ranked.image_urls_raw,
                ranked.clean_blend,
                ranked.clean_construction,
            )
            .where(ranked.rn == 1)
            .orderby(getattr(ranked, order_by), order=direction)
            .limit(page_size)
            .offset(offset)
        )

        rows = query.run(as_dict=True)

        # fiber types + media (images/videos) for this page (single batched query each)
        page_ids = [row['id'] for row in rows]
        fiber_map = _fiber_types_by_garment(page_ids)
        media_map = _media_by_garment(page_ids)

        # garment images + fiber types + media arrays
        for row in rows:
            #row['image_urls'] = cloud.format_garment_image_urls(row['image_urls_raw']) if row.get('image_urls_raw') else {}
            row['image_urls'] = cloud.garment_image_signed_urls(row.get('image_urls_raw'))
            row['fiber_types'] = fiber_map.get(row['id'], [])
            media = media_map.get(row['id'], {})
            row['images'] = media.get('images', [])
            row['videos'] = media.get('videos', [])

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
        }

    except Exception as ex:
        frappe.log_error(frappe.get_traceback(), 'garment.list_all()')
        return {'success': False, 'error': str(ex)}

@frappe.whitelist(allow_guest=True)
@auth_required
def details(id: str = None):
    '''
    Returns a single inventory garment ("Sample Request") identified by its
    unique id (the document name), in the same shape used by list_all().

    Args:
        id: The unique garment id (GarmentMaster.name).

    Returns:
        {'success': True, 'data': <garment row>}
        or {'success': False, 'error': <message>} when not found / on error.
    '''

    try:
        if not id:
            return {'success': False, 'error': 'id is required'}

        GarmentMaster = frappe.qb.DocType(GARMENT_MASTER_DOCTYPE)

        row = (
            frappe.qb.from_(GarmentMaster)
            .select(
                GarmentMaster.name.as_('id'),
                GarmentMaster.gsr_no,
                GarmentMaster.gender,
                GarmentMaster.product_group.as_('category'),
                GarmentMaster.product_category.as_('style'),
                GarmentMaster.ai_description,
                GarmentMaster.fabric_quality,
                GarmentMaster.fabric_blend,
                GarmentMaster.element_colour,
                GarmentMaster.finished_gsm,
                GarmentMaster.sam,
                GarmentMaster.image_urls.as_('image_urls_raw'),
                GarmentMaster.image_urls_clean.as_('image_urls_clean_raw'),
                GarmentMaster.image_urls_3d.as_('image_urls_3d_raw'),
                GarmentMaster.clean_blend,
                GarmentMaster.clean_construction,
                GarmentMaster.fabric_master.as_('fabric_master_id'),
                GarmentMaster.trim_costing.as_('trim_costing_id'),
            )
            .where(GarmentMaster.name == id)
            .limit(1)
            .run(as_dict=True)
        )

        if not row:
            return {'success': False, 'error': 'Garment not found'}

        row = row[0]
        if row.get('image_urls_3d_raw'):
            #row['image_urls'] = cloud.format_garment_image_urls(row['image_urls_3d_raw'])
            row['image_urls'] = cloud.garment_image_signed_urls(row['image_urls_3d_raw'])
        elif row.get('image_urls_clean_raw'):
            row['image_urls'] = cloud.garment_image_signed_urls(row['image_urls_clean_raw'])
        elif row.get('image_urls_raw'):
            row['image_urls'] = cloud.garment_image_signed_urls(row['image_urls_raw'])
        else:
            row['image_urls'] = {}

        row['fiber_types'] = _fiber_types_by_garment([row['id']]).get(row['id'], [])

        media = _media_by_garment([row['id']]).get(row['id'], {})
        row['images'] = media.get('images', [])
        row['videos'] = media.get('videos', [])

        row['esg'] = _esg_snapshot(row['id'])

        row['fabric_master'] = _fabric_master_obj(row.pop('fabric_master_id', None))

        # {number_of_trims, elastic, trim_style} from the linked Trim Costing —
        # same summary the ESG build uses (elastic: "Elastic - Covered" /
        # "Elastic - Exposed" / "No"; trim_style: the costing garment type).
        row['trim_costing_details'] = esg_lib._sample_trim_info(row.pop('trim_costing_id', None))

        row['esg_input_metrics'] = _esg_input_metrics(row['id'])

        return {'success': True, 'data': row}

    except Exception as ex:
        frappe.log_error(frappe.get_traceback(), 'garment.details()')
        return {'success': False, 'error': str(ex)}

@frappe.whitelist(allow_guest=True)
@auth_required
def upload_garment_images(id: str = None):
    '''
    Append one or more images to a Sample Request's garment gallery. Each file is
    streamed straight to S3 under files/sample_request/garment_image/<sr>/<hash><ext>
    (original extension preserved) and its public URL appended as a new row in the
    `garment_images` child table; list_all / details serve these as the `images` array.

    Send as multipart/form-data with:
        id              : the Sample Request id / document name (form field or query param)
        garment_images  : one or more image file parts (repeat the field for
                          multiple; aliases: garment_image, images, image, file)

    Returns { success, data: { id, images: [{ id, url, displayOrder }, ...] } }.
    '''
    try:
        doc = _get_sample_request_or_none(id)
        if not doc:
            return {'success': False, 'error': 'Sample Request not found'}

        fileobjs = _collect_files(('garment_images', 'garment_image', 'images', 'image', 'file'))
        if not fileobjs:
            return {'success': False, 'error': 'No image uploaded (expected multipart field "garment_images").'}

        # New images append after the current max display order, preserving arrival order.
        order = max([(r.display_order or 0) for r in (doc.get('garment_images') or [])], default=0)
        for fileobj in fileobjs:
            err = _assert_image(fileobj) or _assert_size(fileobj, MAX_IMAGE_FILE_SIZE_MB, 'Image')
            if err:
                return {'success': False, 'error': err}
            filename = getattr(fileobj, 'filename', '') or ''
            key = cloud.build_sample_request_image_key(doc.name, cloud.file_ext(filename))
            cloud.upload_file(fileobj.stream, key, cloud.content_type_for(filename))
            order += 1
            doc.append('garment_images', {'image': cloud.asset_url(key), 'display_order': order})

        doc.save(ignore_permissions=True)
        frappe.db.commit()
        return {'success': True, 'data': {'id': doc.name, 'images': _media_rows(doc, 'garment_images', 'image')}}

    except Exception as ex:
        frappe.log_error(frappe.get_traceback(), 'garment.upload_garment_images()')
        return {'success': False, 'error': str(ex)}

@frappe.whitelist(allow_guest=True)
@auth_required
def upload_product_videos(id: str = None):
    '''
    Append one or more product videos to a Sample Request. Each file is streamed
    straight to S3 under files/sample_request/video/<sr>/<hash><ext> (original
    extension preserved) and its public URL appended as a new row in the
    `product_video` child table; list_all / details serve these as the `videos` array.

    Send as multipart/form-data with:
        id             : the Sample Request id / document name (form field or query param)
        product_video  : one or more video file parts (repeat the field for
                         multiple; aliases: product_videos, video, videos, file)

    Returns { success, data: { id, videos: [{ id, url, displayOrder }, ...] } }.
    '''
    try:
        doc = _get_sample_request_or_none(id)
        if not doc:
            return {'success': False, 'error': 'Sample Request not found'}

        fileobjs = _collect_files(('product_video', 'product_videos', 'video', 'videos', 'file'))
        if not fileobjs:
            return {'success': False, 'error': 'No video uploaded (expected multipart field "product_video").'}

        order = max([(r.display_order or 0) for r in (doc.get('product_video') or [])], default=0)
        for fileobj in fileobjs:
            err = _assert_video(fileobj) or _assert_size(fileobj, MAX_VIDEO_FILE_SIZE_MB, 'Video')
            if err:
                return {'success': False, 'error': err}
            filename = getattr(fileobj, 'filename', '') or ''
            key = cloud.build_sample_request_video_key(doc.name, cloud.file_ext(filename))
            cloud.upload_file(fileobj.stream, key, cloud.content_type_for(filename))
            order += 1
            doc.append('product_video', {'video': cloud.asset_url(key), 'display_order': order})

        doc.save(ignore_permissions=True)
        frappe.db.commit()
        return {'success': True, 'data': {'id': doc.name, 'videos': _media_rows(doc, 'product_video', 'video')}}

    except Exception as ex:
        frappe.log_error(frappe.get_traceback(), 'garment.upload_product_videos()')
        return {'success': False, 'error': str(ex)}

@frappe.whitelist(allow_guest=True)
@auth_required
def reorder_garment_images(id: str = None, order=None):
    '''
    Rewrite the display order of a Sample Request's garment gallery. `order` is a
    JSON array of the child row ids (the `id` served in list_all/details or returned
    by upload) in the desired order — or a list of { id, displayOrder } objects for
    explicit values. Rows omitted from `order` keep their relative order and are
    pushed after the reordered block. Returns the updated, ordered array.

    Send as form-data / JSON:
        id     : the Sample Request id / document name
        order  : ["<rowId2>", "<rowId1>", ...]  (or [{ "id": "...", "displayOrder": 1 }, ...])
    '''
    return _reorder_media(id, order, 'garment_images', 'image', 'images')

@frappe.whitelist(allow_guest=True)
@auth_required
def reorder_product_videos(id: str = None, order=None):
    '''
    Rewrite the display order of a Sample Request's product videos. Same contract as
    reorder_garment_images, over the `product_video` child table.

    Send as form-data / JSON:
        id     : the Sample Request id / document name
        order  : ["<rowId2>", "<rowId1>", ...]  (or [{ "id": "...", "displayOrder": 1 }, ...])
    '''
    return _reorder_media(id, order, 'product_video', 'video', 'videos')

@frappe.whitelist(allow_guest=True)
@auth_required
def delete_garment_image(id: str = None, image_id: str = None, url: str = None):
    '''
    Remove a single image from a Sample Request's garment gallery, matched by its
    child row id (`image_id`, the `id` served in list_all/details or returned by
    upload) or, failing that, its stored URL. Best-effort deletes the underlying S3
    object. Returns the updated, ordered array.

    Send as form-data / query params:
        id        : the Sample Request id / document name
        image_id  : the gallery row id to remove   (or match by `url`)
    '''
    return _delete_media(id, image_id, url, 'garment_images', 'image', 'images', 'Image')

@frappe.whitelist(allow_guest=True)
@auth_required
def delete_product_video(id: str = None, video_id: str = None, url: str = None):
    '''
    Remove a single product video from a Sample Request, matched by its child row id
    (`video_id`) or its stored URL. Best-effort deletes the underlying S3 object.
    Returns the updated, ordered array.

    Send as form-data / query params:
        id        : the Sample Request id / document name
        video_id  : the video row id to remove   (or match by `url`)
    '''
    return _delete_media(id, video_id, url, 'product_video', 'video', 'videos', 'Video')

# masters
@frappe.whitelist(allow_guest=True)
@auth_required
def genders(categories:list=None, styles:list=None):
    try:
        filters = {'gender': ['is', 'set'], 'image_urls_3d': ['is', 'set']}
        if categories and isinstance(categories, list):
            filters['product_group'] = ['in', categories]
        if styles and isinstance(styles, list):
            filters['product_category'] = ['in', styles]

        rows = frappe.get_all(
            GARMENT_MASTER_DOCTYPE,
            filters=filters,
            fields=['gender'],
            distinct=True,
            order_by='gender asc',
            ignore_permissions=True
        )
        return {'success': True, 'data': rows}
    except Exception as ex:
        frappe.log_error(frappe.get_traceback(), 'garment.genders()')
        return {'success': False, 'error': str(ex)}

@frappe.whitelist(allow_guest=True)
@auth_required
def categories(genders:list=None, styles:list=None):
    try:
        filters = {'product_group': ['is', 'set'], 'image_urls_3d': ['is', 'set']}
        if genders and isinstance(genders, list):
            filters['gender'] = ['in', genders]
        if styles and isinstance(styles, list):
            filters['product_category'] = ['in', styles]

        rows = frappe.get_all(
            GARMENT_MASTER_DOCTYPE,
            filters=filters,
            fields=['product_group'],
            distinct=True,
            order_by='product_group asc',
            ignore_permissions=True
        )
        return {'success': True, 'data': rows}
    except Exception as ex:
        frappe.log_error(frappe.get_traceback(), 'garment.categories()')
        return {'success': False, 'error': str(ex)}

@frappe.whitelist(allow_guest=True)
@auth_required
def styles(genders:list=None, categories:list=None):
    try:
        filters = {'product_category': ['is', 'set'], 'image_urls_3d': ['is', 'set']}
        if genders and isinstance(genders, list):
            filters['gender'] = ['in', genders]
        if categories and isinstance(categories, list):
            filters['product_group'] = ['in', categories]

        rows = frappe.get_all(
            GARMENT_MASTER_DOCTYPE,
            filters=filters,
            fields=['product_category as style'],
            distinct=True,
            order_by='product_category asc',
            ignore_permissions=True
        )
        return {'success': True, 'data': rows}
    except Exception as ex:
        frappe.log_error(frappe.get_traceback(), 'garment.styles()')
        return {'success': False, 'error': str(ex)}

# utilities
@frappe.whitelist(allow_guest=True)
def update_image_urls():
    '''
    Refreshes the `image_urls` and `image_urls_3d` JSON fields on every
    'Main Body' Sample Request row from the images currently present in S3.

    Optimised for scale (~20k rows / ~20k images):
      1. Each S3 folder (the 2D `gsr_images` and the 3D `gsr_images_3D`) is
         listed ONCE (paginated) and turned into an in-memory
         gsr_no -> {front, back} index, instead of one S3 request per row.
      2. Rows are read in a single query and only those whose stored value
         actually differs from the freshly-computed value are written, via
         frappe.db.bulk_update (chunked, multi-row UPDATEs).

    Returns a summary dict with the number of rows scanned / updated.
    '''
    try:
        #--- 1. build the gsr_no -> {front, back} indexes from single S3 listings ---
        image_index = _build_garment_image_index(
            cloud.get_all_latest_style_image_keys()
        )
        image_index_3d = _build_garment_image_index(
            cloud.get_all_latest_style_image_keys(cloud.STYLE_IMG_3D_FOLDER_NAME)
        )

        #--- 2. read all target rows (name, gsr_no, current image_urls) ---
        GarmentMaster = frappe.qb.DocType(GARMENT_MASTER_DOCTYPE)

        rows = (
            frappe.qb
            .from_(GarmentMaster)
            .select(
                GarmentMaster.name,
                GarmentMaster.gsr_no,
                GarmentMaster.image_urls,
                GarmentMaster.image_urls_3d,
            )
            .where(GarmentMaster.garment_element == 'Main Body')
            .where(GarmentMaster.gsr_no.notnull())
            .run(as_dict=True)
        )

        #--- 3. collect only the rows whose image_urls / image_urls_3d changed ---
        doc_updates = {}
        for row in rows:
            update = {}
            # 2D images
            current = _parse_image_urls(row.get('image_urls'))
            desired = image_index.get(row['gsr_no'], {})
            if desired != current:
                update['image_urls'] = json.dumps(desired) if desired else None
            # 3D images
            current_3d = _parse_image_urls(row.get('image_urls_3d'))
            desired_3d = image_index_3d.get(row['gsr_no'], {})
            if desired_3d != current_3d:
                update['image_urls_3d'] = json.dumps(desired_3d) if desired_3d else None

            if update:
                doc_updates[row['name']] = update

        #--- 4. persist in chunked, multi-row UPDATEs ---
        if doc_updates:
            frappe.db.bulk_update(
                GARMENT_MASTER_DOCTYPE,
                doc_updates,
                chunk_size=500,
                update_modified=False,
            )
            frappe.db.commit()

        return {
            'success': True,
            'stats': {
                'total_db_rows': len(rows),
                'gsr_with_images': len(image_index),
                'gsr_with_3d_images': len(image_index_3d),
                'rows_updated': len(doc_updates),
            }
        }

    except Exception as ex:
        frappe.log_error(frappe.get_traceback(), 'garment.update_image_urls()')
        return {'success': False, 'error': str(ex)}

@frappe.whitelist(allow_guest=True)
def get_image_stat():
    '''
    Retuens the distinct gsr numbers with and without images as the following json
    {
        "total_db": <count of distinct gsr numbers in the db>,
        "total_s3": <count of distinct gsr numbers in the s3 bucket>,
        "total_matches": <count of distinct db gsr numbers with matching s3 image files>,
        "total_missing_in_s3": <count of distinct db gsr numbers without matching s3 files>,
        "total_missing_in_db": <count of distinct s3 gsr numbers without matching db rows>,
        "missing_in_s3": <sorted list of distinct db gsr numbers without matching s3 image files>,
        "missing_in_db": <sorted list of distinct s3 gsr numbers without matching db rows>,
        "matches": <sorted list of distinct db gsr numbers with matching s3 image files>
    }

    A gsr number "exists in S3" when a front and/or back image for it currently
    exists in the bucket (the same source update_image_urls() draws from). The
    two sets are compared both ways to surface DB rows missing their S3 images
    and orphan S3 images with no matching DB row.

    The same set of counts is computed for the 3D images (the `gsr_images_3D`
    folder) and returned under the `3d` key.
    '''
    try:
        #--- 1. distinct gsr numbers that actually have images in S3 ---
        s3_gsr = set(
            _build_garment_image_index(cloud.get_all_latest_style_image_keys()).keys()
        )
        s3_gsr_3d = set(
            _build_garment_image_index(
                cloud.get_all_latest_style_image_keys(cloud.STYLE_IMG_3D_FOLDER_NAME)
            ).keys()
        )

        #--- 2. distinct gsr numbers across all 'Main Body' rows ---
        GarmentMaster = frappe.qb.DocType(GARMENT_MASTER_DOCTYPE)

        rows = (
            frappe.qb
            .from_(GarmentMaster)
            .select(GarmentMaster.gsr_no)
            .distinct()
            .where(GarmentMaster.garment_element == 'Main Body')
            .where(GarmentMaster.gsr_no.notnull())
            .run(as_dict=True)
        )
        db_gsr = {row['gsr_no'] for row in rows}

        #--- 3. compare the DB and S3 gsr sets both ways (2D and 3D) ---
        def _compare(s3_set):
            matches = db_gsr & s3_set
            missing_in_s3 = db_gsr - s3_set
            missing_in_db = s3_set - db_gsr
            return {
                'total_in_s3': len(s3_set),
                'total_matches': len(matches),
                'total_missing_in_s3': len(missing_in_s3),
                'total_missing_in_db': len(missing_in_db),
                'missing_in_s3': sorted(missing_in_s3),
                'missing_in_db': sorted(missing_in_db),
                'matches': sorted(matches),
            }

        data_2d = _compare(s3_gsr)
        data_3d = _compare(s3_gsr_3d)
        data = {
            'total_in_db': len(db_gsr),
            '2d': data_2d,
            '3d': data_3d
        }

        return {
            'success': True,
            'data': data,
        }

    except Exception as ex:
        frappe.log_error(frappe.get_traceback(), 'garment.get_image_stat()')
        return {'success': False, 'error': str(ex)}

@frappe.whitelist(allow_guest=True)
def list_all_3d_images():
    '''
    Lists every file in the 3D image S3 folder (cloud.STYLE_IMG_3D_FOLDER_NAME),
    ordered by upload date/time (most recent first).

    Returns a JSON array of objects:
        [
            {
                "file_name": <name of the file>,
                "upload_dtm_IST": <upload date and time (with milliseconds) in IST>
            },
            ...
        ]
    '''
    IST = timezone(timedelta(hours=5, minutes=30))

    objects = cloud.list_folder_objects(cloud.STYLE_IMG_3D_FOLDER_NAME)

    # newest upload first (LastModified is a tz-aware UTC datetime)
    objects.sort(key=lambda obj: obj['last_modified'], reverse=True)

    return [
        {
            'file_name': obj['key'].rsplit('/', 1)[-1],
            'upload_dtm_IST': obj['last_modified']
                .astimezone(IST)
                .strftime('%Y-%m-%d %H:%M:%S.%f')[:-3],
        }
        for obj in objects
    ]

@frappe.whitelist(allow_guest=True)
def clean_all_2d_images():
    '''
    Starts a long running background job to clean the 2D images of every
    'Main Body' garment that has a 2D image but no 3D image. Garments that
    already carry cleaned images (image_urls_clean set) are skipped, so the job
    is resumable and won't re-pay for the (expensive) AI edits on a re-run.

    Returns the number of garments queued for cleaning.
    '''
    try:
        GarmentMaster = frappe.qb.DocType(GARMENT_MASTER_DOCTYPE)

        rows = (
            frappe.qb
            .from_(GarmentMaster)
            .select(GarmentMaster.name)
            .where(GarmentMaster.garment_element == 'Main Body')
            .where(GarmentMaster.gsr_no.notnull())
            .where(GarmentMaster.image_urls.notnull())
            .where(GarmentMaster.image_urls_3d.isnull())
            .where(GarmentMaster.image_urls_clean.isnull())
            .run(as_dict=True)
        )

        garment_ids = [row['name'] for row in rows]

        if not garment_ids:
            return {'success': True, 'data': {'queued': 0, 'message': 'No 2D images need cleaning.'}}

        #--- hand the (slow, AI-bound) cleaning work off to a long-running background job ---
        frappe.enqueue(
            'prism.api.garment._clean_all_2d_images_job',
            queue='long',
            timeout=10800,
            garment_ids=garment_ids,
        )

        return {
            'success': True, 
            'data': {
                'queued': len(garment_ids)
            }
        }

    except Exception as ex:
        frappe.log_error(frappe.get_traceback(), 'garment.clean_all_2d_images()')
        return {'success': False, 'error': str(ex)}


#--- helpers ---
def _build_garment_image_index(keys: list):
    '''
    Turns a flat list of S3 object keys into a gsr_no -> {front, back} map,
    applying the same front/back matching rules as
    cloud.get_all_latest_style_image_keys().

    The gsr_no is the first 11 characters of the filename. The keys come from
    cloud.get_all_latest_style_image_keys(), which already returns at most one
    front and one back key per gsr_no (the latest upload).
    '''
    index = {}

    for key in keys:
        filename = key.rsplit('/', 1)[-1]

        if 'front' in filename.lower():
            side = 'front'
        elif 'back' in filename.lower():
            side = 'back'
        else:
            continue

        gsr_no = filename[:11]
        if not gsr_no:
            continue

        index.setdefault(gsr_no, {})[side] = key

    return index

def _parse_image_urls(value):
    '''Normalises a stored `image_urls` value to a dict for comparison.'''
    if not value:
        return {}
    if isinstance(value, dict):
        return value
    try:
        parsed = json.loads(value)
        return parsed if isinstance(parsed, dict) else {}
    except (ValueError, TypeError):
        return {}

def _clean_all_2d_images_job(garment_ids: list):
    '''
    Background worker for clean_all_2d_images(): clean the 2D images of each of
    the given garment ids, tolerating individual failures so one bad garment
    does not abort the whole run.
    '''
    cleaned, failed = 0, 0
    for garment_id in (garment_ids or []):
        result = _clean_2d_images(garment_id)
        if result.get('success'):
            cleaned += 1
        else:
            failed += 1
    return {'total': len(garment_ids or []), 'cleaned': cleaned, 'failed': failed}

@frappe.whitelist(allow_guest=True)
def _clean_2d_images(garment_id: str):
    '''
    Generates the clean images from the 2d using function _clean_one_2d_image() and 
    saves the cleaned image in cloud.STYLE_IMG_CLEAN_FOLDER_NAME folder of S3 bucket.
    It should clean both front and back images, if available. Send 'front'/'back' in
    the 'view' parameter of _clean_one_2d_image().
    Also, save the url json in the 'image_urls_clean' field of the doc.
    '''
    try:
        MIME_TYPE = 'image/jpeg'

        if not garment_id:
            return {'success': False, 'error': 'garment_id is required'}

        #--- 1. load the garment's source 2D images + type ---
        garment = frappe.db.get_value(
            GARMENT_MASTER_DOCTYPE,
            garment_id,
            ['product_category', 'image_urls'],
            as_dict=True,
        )
        if not garment:
            return {'success': False, 'error': 'Garment not found'}

        source_keys = _parse_image_urls(garment.get('image_urls'))
        if not source_keys:
            return {'success': False, 'error': 'Garment has no 2D images to clean'}

        # 'Garment Category' link, e.g. 'T-Shirt' — used to guide the AI cleanup.
        garment_type = garment.get('product_category') or 'garment'

        #--- 2. clean each available view and upload it to the clean folder ---
        clean_keys = {}
        errors = {}
        for view in ('front', 'back'):
            src_key = source_keys.get(view)
            if not src_key:
                continue

            image_bytes = cloud.download_object(src_key)
            if not image_bytes:
                errors[view] = f'source image not found in S3: {src_key}'
                continue

            #image_mime = cloud.content_type_for(src_key)
            result = llm.clean_style_2d_image(image_bytes, MIME_TYPE, garment_type, view)
            if not result.get('success'):
                errors[view] = (result or {}).get('error', 'image cleanup failed')
                continue

            # gpt-image edits come back as PNG; convert to JPEG and store under the clean
            # folder, keyed off the source filename so re-runs overwrite in place.
            jpg_bytes = util.png_to_jpeg(result.get('data'))
            filename = src_key.rsplit('/', 1)[-1]
            clean_key = f'{cloud.STYLE_IMG_CLEAN_FOLDER_NAME}/{filename}'

            cloud.upload_file(io.BytesIO(jpg_bytes), clean_key, MIME_TYPE)
            clean_keys[view] = clean_key

        if not clean_keys:
            return {
                'success': False,
                'error': 'No images could be cleaned',
                'errors': errors,
            }

        #--- 3. persist the clean image keys on the doc ---
        frappe.db.set_value(
            GARMENT_MASTER_DOCTYPE,
            garment_id,
            'image_urls_clean',
            json.dumps(clean_keys),
            update_modified=False,
        )
        frappe.db.commit()

        return {
            'success': True,
            'data': {
                'image_urls_clean': clean_keys,
                'image_urls_clean_signed': cloud.garment_image_signed_urls(
                    json.dumps(clean_keys)
                ),
                'errors': errors,
            },
        }

    except Exception as ex:
        frappe.log_error(frappe.get_traceback(), 'garment._clean_2d_images()')
        return {'success': False, 'error': str(ex)}

# upload helpers
def _get_sample_request_or_none(name):
    ''' The Sample Request doc for `name`, or None when it doesn't exist. '''
    if not name or not frappe.db.exists(GARMENT_MASTER_DOCTYPE, name):
        return None
    return frappe.get_doc(GARMENT_MASTER_DOCTYPE, name)

def _collect_files(field_aliases):
    '''
    All uploaded file parts across the given multipart field names, in order. Uses
    werkzeug's getlist so several files sent under the same field are all returned,
    not just the first. Mirrors moodboard_style._collect_files.
    '''
    files = getattr(frappe.request, 'files', None)
    if not files:
        return []
    getlist = getattr(files, 'getlist', None)
    out = []
    for fld in field_aliases:
        items = getlist(fld) if getlist else ([files.get(fld)] if files.get(fld) else [])
        out.extend([f for f in items if f])
    return out

def _assert_image(fileobj):
    ''' Reject a non-image upload (by MIME / filename), so the gallery holds images only. '''
    filename = getattr(fileobj, 'filename', '') or ''
    content_type = (getattr(fileobj, 'content_type', '') or cloud.content_type_for(filename) or '')
    if not content_type.startswith('image/'):
        return f'"{filename or "file"}" is not an image. Only image files (PNG, JPG, etc.) are allowed.'
    return None

def _assert_video(fileobj):
    ''' Reject a non-video upload (by MIME / filename); tolerant of a missing content type. '''
    filename = getattr(fileobj, 'filename', '') or ''
    content_type = (getattr(fileobj, 'content_type', '') or cloud.content_type_for(filename) or '')
    if content_type and not content_type.startswith('video/'):
        return f'"{filename or "file"}" is not a video. Only video files (MP4, MOV, WEBM, etc.) are allowed.'
    return None

def _assert_size(fileobj, max_mb, label='File'):
    ''' Reject uploads over `max_mb` by measuring the stream without buffering it. '''
    stream = fileobj.stream
    stream.seek(0, 2)  # SEEK_END
    size = stream.tell()
    stream.seek(0)
    if size > max_mb * 1024 * 1024:
        return f'{label} exceeds the maximum allowed limit of {max_mb} MB.'
    return None

def _media_rows(doc, tablefield, url_field):
    ''' The saved child rows as [{ id, url, displayOrder }, ...], ascending by display order. '''
    rows = [r for r in (doc.get(tablefield) or []) if r.get(url_field)]
    rows.sort(key=lambda r: ((r.display_order if r.display_order is not None else 0), r.idx or 0))
    return [
        {'id': r.name, 'url': cloud.asset_url(r.get(url_field)), 'displayOrder': r.display_order or 0}
        for r in rows
    ]

def _media_by_garment(garment_ids):
    '''
    Batched fetch of the Sample Request media child rows — the `garment_images`
    gallery and the `product_video` table — for the given garment ids, returned as
    ordered arrays of media objects: { id, url, displayOrder }. The `id` is the child
    row name, so a client can reorder / delete a specific row after a page reload.

    Both child tables are dedicated to Sample Request (unlike the shared fiber
    table), so filtering by parent + parenttype selects exactly the right rows. The
    stored value is already a full S3/CDN URL (media is offloaded to S3 on save by
    prism.lib.sample_request_media); it's passed through cloud.asset_url (idempotent
    for full URLs). Rows are ordered by the manual `display_order`, then the grid
    `idx` for ties.

    Returns { <garment_id>: {'images': [{id,url,displayOrder}, ...], 'videos': [...]},
    ... } for every requested id (empty arrays when a garment has no media).
    '''
    garment_ids = list({gid for gid in (garment_ids or []) if gid})
    if not garment_ids:
        return {}

    def _collect(doctype, url_field):
        rows = frappe.get_all(
            doctype,
            filters={'parent': ['in', garment_ids], 'parenttype': GARMENT_MASTER_DOCTYPE},
            fields=['name', 'parent', url_field, 'display_order', 'idx'],
            order_by='display_order asc, idx asc',
            ignore_permissions=True,
        )
        out = {}
        for r in rows:
            url = (r.get(url_field) or '').strip()
            if url:
                out.setdefault(r['parent'], []).append({
                    'id': r['name'],
                    'url': cloud.asset_url(url),
                    'displayOrder': r.get('display_order') or 0,
                })
        return out

    images = _collect(GARMENT_IMAGE_DOCTYPE, 'image')
    videos = _collect(GARMENT_VIDEO_DOCTYPE, 'video')

    return {
        gid: {'images': images.get(gid, []), 'videos': videos.get(gid, [])}
        for gid in garment_ids
    }

def _reorder_media(id, order, tablefield, url_field, out_key):
    '''
    Shared reorder flow: assign `display_order` to the named rows from `order`
    (position-based 1..N, or an explicit displayOrder per item), then append any
    omitted rows after the reordered block preserving their current relative order.
    '''
    try:
        doc = _get_sample_request_or_none(id)
        if not doc:
            return {'success': False, 'error': 'Sample Request not found'}

        order = frappe.parse_json(order) if isinstance(order, str) else order
        if not isinstance(order, list) or not order:
            return {'success': False, 'error': '`order` must be a non-empty JSON array of row ids (or { id, displayOrder } objects).'}

        # Build { rowId: desiredDisplayOrder }. Bare ids -> position-based (1..N);
        # dicts -> explicit displayOrder (falls back to position when absent).
        wanted = {}
        for i, item in enumerate(order):
            if isinstance(item, dict):
                rid = (item.get('id') or item.get('name') or '').strip()
                pos = item.get('displayOrder')
                pos = int(pos) if pos not in (None, '') else i + 1
            else:
                rid = str(item).strip()
                pos = i + 1
            if rid:
                wanted[rid] = pos

        rows = doc.get(tablefield) or []
        known = {r.name for r in rows}
        unknown = [rid for rid in wanted if rid not in known]
        if unknown:
            return {'success': False, 'error': f'Unknown {out_key} row id(s) for this Sample Request: {", ".join(unknown)}'}

        # Named rows take the requested positions; omitted rows keep their current
        # relative order (by display_order, then idx) and follow the reordered block.
        for r in rows:
            if r.name in wanted:
                r.display_order = wanted[r.name]
        tail = max(wanted.values(), default=0)
        omitted = sorted(
            (r for r in rows if r.name not in wanted),
            key=lambda r: ((r.display_order if r.display_order is not None else 0), r.idx or 0),
        )
        for r in omitted:
            tail += 1
            r.display_order = tail

        doc.save(ignore_permissions=True)
        frappe.db.commit()
        return {'success': True, 'data': {'id': doc.name, out_key: _media_rows(doc, tablefield, url_field)}}

    except Exception as ex:
        frappe.log_error(frappe.get_traceback(), f'garment._reorder_media({tablefield})')
        return {'success': False, 'error': str(ex)}

def _esg_snapshot(garment_id):
    '''
    Live ESG rating/category/score for a Sample Request garment, read fresh from the
    Moodboard ESG record keyed by its polymorphic source. None until an ESG exists
    (i.e. until "Feed ESG Input" + "Calculate ESG" have run). Mirrors the shape used
    by moodboard_style._esg_snapshot.
    '''
    row = frappe.db.get_value(
        'Moodboard ESG',
        {'source_doctype': GARMENT_MASTER_DOCTYPE, 'source_name': garment_id},
        ['overall_rating', 'category', 'score_percent'], as_dict=True,
    )
    if not row:
        return None
    return {
        'rating': row.get('overall_rating'),
        'category': row.get('category'),
        'score': row.get('score_percent'),
    }


def _esg_input_metrics(garment_id):
    '''
    Full field dump of the garment's Moodboard ESG record — every data field of
    the doc (inputs like ship_country/number_of_trims plus computed outputs like
    overall_rating) and its `elements` child rows. Complements _esg_snapshot,
    which returns only the three headline outputs. Returns None when no ESG has
    been generated for the sample yet.

    Field lists come from the live meta so ESG fields added later flow through
    automatically.
    '''
    esg_name = frappe.db.get_value(
        'Moodboard ESG',
        {'source_doctype': GARMENT_MASTER_DOCTYPE, 'source_name': garment_id},
    )
    if not esg_name:
        return None

    esg = frappe.get_doc('Moodboard ESG', esg_name)
    layout = ('Section Break', 'Column Break', 'Tab Break', 'Table')

    out = {'id': esg.name}
    for f in frappe.get_meta('Moodboard ESG').fields:
        if f.fieldtype not in layout:
            out[f.fieldname] = esg.get(f.fieldname)

    element_fields = [
        f.fieldname for f in frappe.get_meta('Moodboard ESG Element').fields
        if f.fieldtype not in layout
    ]
    out['elements'] = [
        {fieldname: e.get(fieldname) for fieldname in element_fields}
        for e in (esg.get('elements') or [])
    ]
    return out


def _fabric_master_obj(fabric_master_id):
    '''
    Snapshot of the Sample Request's linked Fabric Master for details().

    Returns None when the garment has no (or a dangling) fabric_master link,
    otherwise:
        {
            'id': <Fabric Master name>,
            'blend': '60% Cotton 40% Polyester',   # "<percent>% <blend>" per yarn row
            'yarnlist': [ {<every Fabric Master Yarn Detail data field>}, ... ],
        }

    Fabric Master and its yarn child table are Desk-managed custom doctypes, so
    the yarn columns are read from the live meta rather than a hardcoded list —
    fields added later (e.g. fiber_category) flow through automatically.
    '''
    if not fabric_master_id or not frappe.db.exists('Fabric Master', fabric_master_id):
        return None

    fm = frappe.get_doc('Fabric Master', fabric_master_id)
    yarns = fm.get('yarn_details') or []

    yarn_fields = [
        f.fieldname for f in frappe.get_meta('Fabric Master Yarn Detail').fields
        if f.fieldtype not in ('Section Break', 'Column Break', 'Tab Break')
    ]
    yarnlist = [{fieldname: y.get(fieldname) for fieldname in yarn_fields} for y in yarns]

    def _pct(value):
        # 60.0 -> "60%", 32.5 -> "32.5%", None -> ""
        if value is None:
            return ''
        return (str(int(value)) if float(value) == int(value) else str(value)) + '%'

    blend = ' '.join(
        part for y in yarns
        for part in (f"{_pct(y.get('yarn_percent'))} {y.get('blend') or ''}".strip(),)
        if part
    )

    return {'id': fm.name, 'blend': blend, 'yarnlist': yarnlist}


def _fiber_types_by_garment(garment_ids):
    '''
    Fetch the fiber-type child rows for the given Sample Request (garment) ids in
    a single query and return them grouped by category, ready to render the
    per-category chip tabs (Cotton, Lyocell, ...). Mirrors
    fabric._fiber_types_by_fabric().

    The fiber rows live in the shared "Dyed Fabric Fiber" child table; they are
    isolated from the fabric rows by `parenttype`. The child field was added to
    Sample Request as a Custom Field via Desk, so we intentionally do NOT filter
    on `parentfield` (its scrubbed name may be `fiber_types` or
    `custom_fiber_types`) — parenttype + parent already selects the right rows.

    Returns:
        {
            <garment_id>: [
                {'category': 'Cotton',  'types': ['BCI Cotton', 'Organic Cotton']},
                {'category': 'Lyocell', 'types': ['Tencel']},
            ],
            ...
        }
        Garments with no fiber types simply don't appear in the map.
    '''
    garment_ids = list({gid for gid in (garment_ids or []) if gid})
    if not garment_ids:
        return {}

    Fiber = frappe.qb.DocType(GARMENT_FIBER_DOCTYPE)
    rows = (
        frappe.qb.from_(Fiber)
        .select(
            Fiber.parent,
            Fiber.category,
            Fiber.fiber_type,
            Fiber.idx,
        )
        .where(Fiber.parent.isin(garment_ids))
        .where(Fiber.parenttype == GARMENT_MASTER_DOCTYPE)
        .run(as_dict=True)
    )
    # Resolve each row's stored `fiber_type` to the Fiber Type master's display
    # label + `sort_order`. Rows may store either the master's docname or its
    # human `type_name`, so we look up by both (name wins); unmatched/orphaned
    # rows keep their raw label and sort as unset. See util.fiber_type_lookup.
    by_name, by_label = util.fiber_type_lookup()
    for r in rows:
        raw = r['fiber_type']
        r['fiber_type'], r['type_sort_order'] = (
            by_name.get(raw) or by_label.get(raw) or (raw, 0)
        )

    # Order by the Fiber Type master's `sort_order` (0/unset falls back to row order via idx).
    rows.sort(key=util.fiber_sort_key)

    # group: {parent: {category: [type, ...]}} preserving first-seen order (by type_sort_order, idx)
    grouped = {}
    for r in rows:
        by_cat = grouped.setdefault(r['parent'], {})
        by_cat.setdefault(r['category'], []).append(r['fiber_type'])

    return {
        parent: [{'category': cat, 'types': types} for cat, types in by_cat.items()]
        for parent, by_cat in grouped.items()
    }

def _delete_media(id, row_id, url, tablefield, url_field, out_key, label):
    '''
    Shared per-row delete: drop the row matched by id (or URL) from the child table,
    then best-effort delete its S3 object. Mirrors
    moodboard_style.delete_style_garment_image — a bucket failure is logged, never
    blocks the removal; a missing row is a not-found error.
    '''
    try:
        doc = _get_sample_request_or_none(id)
        if not doc:
            return {'success': False, 'error': 'Sample Request not found'}

        row_id = (row_id or '').strip()
        target = (url or '').strip()
        if not row_id and not target:
            return {'success': False, 'error': f'`{out_key[:-1]}_id` or `url` of the {label.lower()} to remove is required.'}

        def _matches(r):
            if row_id:
                return r.name == row_id
            value = (r.get(url_field) or '').strip()
            return value == target or cloud.asset_url(r.get(url_field)) == target

        rows = doc.get(tablefield) or []
        removed = [r for r in rows if _matches(r)]
        kept = [r for r in rows if not _matches(r)]
        if not removed:
            return {'success': False, 'error': f'{label} not found in this Sample Request.'}

        doc.set(tablefield, kept)
        doc.save(ignore_permissions=True)
        frappe.db.commit()

        # Best-effort S3 cleanup — never block the removal on a bucket failure.
        for r in removed:
            key = cloud.asset_key(r.get(url_field))
            if not key:
                continue
            try:
                cloud.delete_object(key)
            except Exception:
                frappe.log_error(frappe.get_traceback(), f'garment._delete_media S3 cleanup ({tablefield})')

        return {'success': True, 'data': {'id': doc.name, out_key: _media_rows(doc, tablefield, url_field)}}

    except Exception as ex:
        frappe.log_error(frappe.get_traceback(), f'garment._delete_media({tablefield})')
        return {'success': False, 'error': str(ex)}
