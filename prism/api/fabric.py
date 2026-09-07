import io

import frappe
from frappe.query_builder.functions import Count
from pypika.analytics import RowNumber

from prism.auth.authenticator import auth_required
import prism.lib.cloud as cloud
import prism.api.util as util

DYED_FABRIC_MASTER_DOCTYPE = 'Moodboard Dyed Fabric'
DYED_FABRIC_FIBER_DOCTYPE = 'Dyed Fabric Fiber'
FIBER_TYPE_DOCTYPE = 'Fiber Type'
IMG_THUMBNAIL_CUT_SIZE = 1500
IMG_THUMBNAIL_FINAL_SIZE = 250


@frappe.whitelist(allow_guest=True)
@auth_required
def list_all(
    search_text: str = None,
    code: str = None,
    qualities: list = [],
    blends: list = [],
    finishes: list = [],
    shade_codes: list = [],
    min_gsm: float = None,
    max_gsm: float = None,
    withImgOnly: bool = True,

    order_by: str = 'creation',
    order_dir: str = 'desc',

    page: int = 1,
    page_size: int = 20,
):
    '''
    Returns a paginated list of inventory fabrics ("Moodboard Dyed Fabric"),
    optionally filtered by quality, blend, finish and/or shade category.

    Args:
        search_text: Optional free-text term matched (case-insensitive LIKE)
                     against custom_fabric_name, description,
                     ai_description, clean_quality, clean_blend, and finish.
        code:        Optional fabric code term matched (case-insensitive LIKE)
                     against `code` only. When provided, it is an EXCLUSIVE
                     search: every other filter/search arg is ignored and rows
                     are matched solely on code.
        qualities:   Optional list of quality values to filter on (IN).
        blends:      Optional list of blend values to filter on (IN).
        finishes:    Optional list of finish values to filter on (IN).
        shade_codes: Optional list of shade_category values to filter on (IN).
        min_gsm:     Optional lower bound (inclusive) on gsm.
        max_gsm:     Optional upper bound (inclusive) on gsm.
        withImgOnly: When True, only rows that have a batch are returned and
                     each row is enriched with an `image_url`.
        order_by:    Column to sort by (whitelisted; defaults to 'creation').
        order_dir:   Sort direction, 'asc' or 'desc' (defaults to 'desc').
        page:        1-based page number.
        page_size:   Number of rows per page (clamped to [1, 100]).

    Returns:
        {
            'success': True,
            'data': [<fabric row>, ...],
            'pagination': {
                'page', 'page_size', 'total_count', 'total_pages',
                'has_next', 'has_previous',
            }
        }
    '''

    try:
        #--- normalize paging / sorting params (whitelisted to avoid injection) ---
        ALLOWED_ORDER_FIELDS = ['creation', 'quality', 'blend', 'gsm', 'shade_category', 'finish', 'custom_fabric_name']

        page = max(1, int(page or 1))
        page_size = min(100, max(1, int(page_size or 20)))
        offset = (page - 1) * page_size

        if order_by not in ALLOWED_ORDER_FIELDS:
            order_by = 'creation'
        direction = frappe.qb.asc if str(order_dir).lower() == 'asc' else frappe.qb.desc

        #--- db inventory matches ---
        DyedFabric = frappe.qb.DocType(DYED_FABRIC_MASTER_DOCTYPE)

        def _get_filters(q):
            # Code search is exclusive: when a code term is given, match solely
            # on `code` and ignore every other filter / free-text search.
            if code and str(code).strip():
                return q.where(DyedFabric.code.like(f'%{str(code).strip()}%'))
            if qualities:
                q = q.where(DyedFabric.clean_quality.isin(qualities))
            if blends:
                q = q.where(DyedFabric.clean_blend.isin(blends))
            if finishes:
                q = q.where(DyedFabric.finish.isin(finishes))
            if shade_codes:
                q = q.where(DyedFabric.shade_category.isin(shade_codes))
            if min_gsm is not None:
                q = q.where(DyedFabric.gsm >= float(min_gsm))
            if max_gsm is not None:
                q = q.where(DyedFabric.gsm <= float(max_gsm))
            if withImgOnly:
                q = q.where(DyedFabric.has_image_file == 1)
            if search_text:
                term = f'%{search_text.strip()}%'
                q = q.where(
                    DyedFabric.custom_fabric_name.like(term)
                    | DyedFabric.description.like(term)
                    | DyedFabric.ai_description.like(term)
                    | DyedFabric.clean_quality.like(term)
                    | DyedFabric.clean_blend.like(term)
                    | DyedFabric.finish.like(term)
                )
            return q

        # Rank rows within each (quality, blend, gsm) group by creation desc,
        # so we keep only the latest row per combination (rn == 1).
        row_num = (
            RowNumber()
            #.over(DyedFabric.quality, DyedFabric.blend, DyedFabric.gsm)
            .over(DyedFabric.batch)
            .orderby(DyedFabric.creation, order=frappe.qb.desc)
        )

        ranked = _get_filters(
            frappe.qb.from_(DyedFabric)
            .select(
                DyedFabric.name.as_('id'),
                DyedFabric.code,
                DyedFabric.description,
                DyedFabric.ai_description,
                DyedFabric.custom_fabric_name,
                DyedFabric.clean_quality.as_('quality'),
                DyedFabric.clean_blend.as_('blend'),
                DyedFabric.gsm,
                DyedFabric.shade_category,
                DyedFabric.finish,
                DyedFabric.batch,
                DyedFabric.creation,
                row_num.as_('rn'),
            )
        ).as_('ranked')

        #--- total count for pagination metadata (one row per quality+blend+gsm) ---
        count_query = (
            frappe.qb.from_(ranked)
            .select(Count('*'))
            .where(ranked.rn == 1)
        )
        total_count = count_query.run()[0][0]
        total_pages = (total_count + page_size - 1) // page_size

        #--- page of rows (top row per quality+blend+gsm combination) ---
        query = (
            frappe.qb.from_(ranked)
            .select(
                ranked.id,
                ranked.code,
                ranked.description,
                ranked.ai_description,
                ranked.custom_fabric_name,
                ranked.quality,
                ranked.blend,
                ranked.gsm,
                ranked.shade_category,
                ranked.finish,
                ranked.batch,
            )
            .where(ranked.rn == 1)
            .orderby(getattr(ranked, order_by), order=direction)
            .limit(page_size)
            .offset(offset)
        )

        rows = query.run(as_dict=True)

        # fiber types for this page (single batched query, grouped by category)
        fiber_map = _fiber_types_by_fabric([row['id'] for row in rows])

        # fabric images + fiber types
        for row in rows:
            row['image_url'] = cloud.fabric_image_signed_url(row.get('batch'))
            row['thumbnail'] = thumbnail_signed_url(row.get('batch'))
            row['fiber_types'] = fiber_map.get(row['id'], [])

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
        frappe.log_error(frappe.get_traceback(), 'fabric.list_all()')
        return {'success': False, 'error': str(ex)}

@frappe.whitelist(allow_guest=True)
@auth_required
def details(id: str = None):
    '''
    Returns a single inventory fabric ("Moodboard Dyed Fabric") identified by
    its unique id (the document name), in the same shape used by list_all().

    Args:
        id: The unique fabric id (DyedFabric.name).

    Returns:
        {'success': True, 'data': <fabric row>}
        or {'success': False, 'error': <message>} when not found / on error.
    '''

    try:
        if not id:
            return {'success': False, 'error': 'id is required'}

        DyedFabric = frappe.qb.DocType(DYED_FABRIC_MASTER_DOCTYPE)

        row = (
            frappe.qb.from_(DyedFabric)
            .select(
                DyedFabric.name.as_('id'),
                DyedFabric.code,
                DyedFabric.description,
                DyedFabric.ai_description,
                DyedFabric.custom_fabric_name,
                DyedFabric.clean_quality.as_('quality'),
                DyedFabric.clean_blend.as_('blend'),
                DyedFabric.gsm,
                DyedFabric.shade_category,
                DyedFabric.finish,
                DyedFabric.batch,
            )
            .where(DyedFabric.name == id)
            .limit(1)
            .run(as_dict=True)
        )

        if not row:
            return {'success': False, 'error': 'Fabric not found'}

        row = row[0]
        row['image_url'] = cloud.fabric_image_signed_url(row.get('batch'))
        #row['image_url'] = cloud.fabric_image_url(row.get('batch'))
        row['thumbnail'] = thumbnail_signed_url(row.get('batch'))
        row['fiber_types'] = _fiber_types_by_fabric([row['id']]).get(row['id'], [])

        return {'success': True, 'data': row}

    except Exception as ex:
        frappe.log_error(frappe.get_traceback(), 'fabric.details()')
        return {'success': False, 'error': str(ex)}

# utility
@frappe.whitelist(allow_guest=True)
def generate_thumbnails():
    '''
    Starts a long running background job to generate the thumbnail image of the
    fabrics for which 'has_image_file' is 1 and the thumbnail image is either
    missing or stale (its creation date-time is prior to the upload date-time of
    the original image).
    '''
    try:
        img_prefix = f'{cloud.DYED_FABRIC_IMG_FOLDER_NAME}/'
        thumb_folder = f'{cloud.DYED_FABRIC_IMG_FOLDER_NAME}/{cloud.DYED_FABRIC_THUMBNAIL_FOLDER_NAME}'

        #--- 1. upload time of each original image, batch -> last_modified ---
        # (the main folder listing also contains the `thumb/` subfolder, so keep
        #  only the keys that sit directly under it: <batch>.jpg, no extra slash)
        original_dtm = {}
        for obj in cloud.list_folder_objects(cloud.DYED_FABRIC_IMG_FOLDER_NAME):
            relative = obj['key'][len(img_prefix):]
            if '/' in relative or not relative.lower().endswith(cloud.DYED_FABRIC_IMG_EXTENSION):
                continue
            batch = relative[:-len(cloud.DYED_FABRIC_IMG_EXTENSION)]
            if batch:
                original_dtm[batch] = obj['last_modified']

        #--- 2. creation time of each existing thumbnail, batch -> last_modified ---
        thumb_dtm = {}
        for obj in cloud.list_folder_objects(thumb_folder):
            filename = obj['key'].rsplit('/', 1)[-1]
            if filename.lower().endswith(cloud.DYED_FABRIC_IMG_EXTENSION):
                batch = filename[:-len(cloud.DYED_FABRIC_IMG_EXTENSION)]
                if batch:
                    thumb_dtm[batch] = obj['last_modified']

        #--- 3. fabrics whose thumbnail is missing or stale (older than original) ---
        DyedFabric = frappe.qb.DocType(DYED_FABRIC_MASTER_DOCTYPE)
        rows = (
            frappe.qb
            .from_(DyedFabric)
            .select(DyedFabric.name, DyedFabric.batch)
            .where(DyedFabric.has_image_file == 1)
            .where(DyedFabric.batch.notnull())
            .run(as_dict=True)
        )

        # one fabric per batch needing work (several rows can share a batch / image)
        todo_by_batch = {}
        for row in rows:
            batch = row['batch']
            src_dtm = original_dtm.get(batch)
            if not src_dtm:
                continue  # no source image in S3 -> nothing to generate from

            existing_dtm = thumb_dtm.get(batch)
            # (re)generate when the thumbnail is absent or predates the original
            if existing_dtm is None or existing_dtm < src_dtm:
                todo_by_batch.setdefault(batch, row['name'])

        fabric_ids = list(todo_by_batch.values())

        if not fabric_ids:
            return {'success': True, 'data': {'queued': 0, 'message': 'All thumbnails are up to date.'}}

        #--- 4. hand the generation work off to a long-running background job ---
        frappe.enqueue(
            'prism.api.fabric._generate_thumbnails_job',
            queue='long',
            timeout=10800,
            fabric_ids=fabric_ids,
        )

        return {'success': True, 'data': {'queued': len(fabric_ids)}}

    except Exception as ex:
        frappe.log_error(frappe.get_traceback(), 'fabric.generate_thumbnails()')
        return {'success': False, 'error': str(ex)}

def _generate_thumbnails_job(fabric_ids: list):
    '''
    Background worker for generate_thumbnails(): create a thumbnail for each of
    the given fabric ids, tolerating individual failures so one bad image does
    not abort the whole run.
    '''
    created, failed = 0, 0
    for fabric_id in (fabric_ids or []):
        result = create_fabric_thumbnail(fabric_id)
        if result.get('success'):
            created += 1
        else:
            failed += 1
    return {'total': len(fabric_ids or []), 'created': created, 'failed': failed}

@frappe.whitelist(allow_guest=True)
def create_fabric_thumbnail(fabric_id: str):
    '''
    If there is any image in s3 for this fabric, converts it to a low-res 250x250
    thumbnail eliminating the moire effect. It should not distort the knit design
    of the fabric.
    The converted image should be uploaded to the same s3 bucket folder with the
    '_thumb' suffix added to the filename.
    '''
    try:
        batch = frappe.db.get_value(DYED_FABRIC_MASTER_DOCTYPE, fabric_id, 'batch')
        if not batch:
            return {'success': False, 'error': f'Fabric "{fabric_id}" not found or has no batch.'}

        # source image: images/dyed-fabric/<batch>.jpg
        src_key = cloud.dyed_fabric_image_key(batch)
        src_bytes = cloud.download_object(src_key)
        if not src_bytes:
            return {'success': False, 'error': f'No image found in S3 for batch "{batch}".'}

        #thumb_bytes = util.fabric_image_thumbnail(src_bytes, IMG_THUMBNAIL_CUT_SIZE, IMG_THUMBNAIL_FINAL_SIZE)
        thumb_bytes = util.make_image_thumbnail(src_bytes, IMG_THUMBNAIL_CUT_SIZE)
        if not thumb_bytes:
            return {
                'success': False, 
                'error': f'Image for batch "{batch}" is smaller than {IMG_THUMBNAIL_CUT_SIZE}px!'
            }

        # thumbnail lives in the dedicated subfolder
        thumb_key = cloud.dyed_fabric_thumbnail_key(batch)
        cloud.upload_file(io.BytesIO(thumb_bytes), thumb_key, content_type='image/jpeg')

        return {
            'success': True,
            'data': {
                'batch': batch, 
                'thumbnail_key': thumb_key
            }
        }

    except Exception as ex:
        frappe.log_error(frappe.get_traceback(), 'fabric.create_fabric_thumbnail()')
        return {'success': False, 'error': str(ex)}

@frappe.whitelist(allow_guest=True)
def update_image_flag():
    '''
    1. gets the list of all fabric image files in the relevant S3 bucket.
    2. sets the 'has_image_file' flag in doctype DYED_FABRIC_MASTER_DOCTYPE for which
       the image exists in S3 (match with the 'batch' field).

    Returns a summary dict with the number of rows flagged / unflagged.
    '''
    try:
        #--- 1. batches that actually have an image file in S3 ---
        image_batches = cloud.get_all_dyed_fabric_batches()

        #--- 2. reconcile the flag against current db state ---
        DyedFabric = frappe.qb.DocType(DYED_FABRIC_MASTER_DOCTYPE)
        rows = (
            frappe.qb
            .from_(DyedFabric)
            .select(
                DyedFabric.name,
                DyedFabric.batch,
                DyedFabric.has_image_file,
            )
            .where(DyedFabric.batch.notnull())
            .run(as_dict=True)
        )

        to_set = []    # names that should be flagged (1)
        to_clear = []  # names that should be cleared (0)
        for row in rows:
            should_have = bool(row.get('batch')) and row['batch'] in image_batches
            currently_has = bool(row.get('has_image_file'))
            if should_have and not currently_has:
                to_set.append(row['name'])
            elif not should_have and currently_has:
                to_clear.append(row['name'])

        #--- 3. persist only the rows whose flag actually changed ---
        #    chunk the IN(...) list so a full reconcile (~3000 names) can't
        #    blow past max_allowed_packet or build a pathologically large query.
        CHUNK_SIZE = 500
        for value, names in ((1, to_set), (0, to_clear)):
            for i in range(0, len(names), CHUNK_SIZE):
                chunk = names[i:i + CHUNK_SIZE]
                (
                    frappe.qb
                    .update(DyedFabric)
                    .set(DyedFabric.has_image_file, value)
                    .where(DyedFabric.name.isin(chunk))
                    .run()
                )
        frappe.db.commit()

        # generate thumbnails
        th_res = generate_thumbnails()

        # delete orphan thumbnails
        del_res = cloud.delete_orphan_fabric_thumbnails()

        return {
            'update_image_flag': {
                'success': True,
                'stats': {
                    'total_db_rows': len(rows),
                    'total_images_in_s3': len(image_batches),
                    'flagged': len(to_set),
                    'unflagged': len(to_clear)
                }
            },
            'generate_thumbnails': th_res,
            'delete_orphan_thumbnails': del_res
        }

    except Exception as ex:
        frappe.log_error(frappe.get_traceback(), 'fabric._update_image_flag()')
        return {'success': False, 'error': str(ex)}

@frappe.whitelist(allow_guest=True)
def get_image_stat():
    '''
    Retuens the distinct fabric batches with and without images as the following json
    {
        "total_db": <count of distinct batches in the db>,
        "total_s3": <count of distinct batches in the s3 bucket>,
        "total_matches": <count of distinct db batches with matching s3 image files>,
        "total_missing_in_s3": <count of distinct db batches without matching s3 files>,
        "total_missing_in_db": <count of distinct s3 batches without matching db rows>,
        "missing_in_s3": <sorted list of distinct db batches without matching s3 image files>,
        "missing_in_db": <sorted list of distinct s3 batches without matching db rows>,
        "matches": <sorted list of distinct db batches with matching s3 image files>
    }

    A batch "exists in S3" when a "<batch>.jpg" image for it currently exists in
    the bucket (the same source update_image_flag() draws from). The two sets are
    compared both ways to surface DB rows missing their S3 images and orphan S3
    images with no matching DB row.
    '''
    try:
        #--- 1. distinct batches that actually have an image file in S3 ---
        s3_batches = cloud.get_all_dyed_fabric_batches()

        #--- 2. distinct batches across all dyed-fabric rows ---
        DyedFabric = frappe.qb.DocType(DYED_FABRIC_MASTER_DOCTYPE)

        rows = (
            frappe.qb
            .from_(DyedFabric)
            .select(DyedFabric.batch)
            .distinct()
            .where(DyedFabric.batch.notnull())
            .run(as_dict=True)
        )
        db_batches = {row['batch'] for row in rows}

        #--- 3. compare the DB and S3 batch sets both ways ---
        matches = db_batches & s3_batches
        missing_in_s3 = db_batches - s3_batches
        missing_in_db = s3_batches - db_batches

        return {
            'success': True,
            'data': {
                'total_db': len(db_batches),
                'total_s3': len(s3_batches),
                'total_matches': len(matches),
                'total_missing_in_s3': len(missing_in_s3),
                'total_missing_in_db': len(missing_in_db),
                'missing_in_s3': sorted(missing_in_s3),
                'missing_in_db': sorted(missing_in_db),
                'matches': sorted(matches),
            }
        }

    except Exception as ex:
        frappe.log_error(frappe.get_traceback(), 'fabric.get_image_stat()')
        return {'success': False, 'error': str(ex)}


@frappe.whitelist(allow_guest=True)
@auth_required
def thumbnail_signed_url(batch_nr: str):
    return cloud.fabric_thumbnail_signed_url(batch_nr)


#--- helpers ---
def _fiber_types_by_fabric(fabric_ids):
    '''
    Fetch the `fiber_types` child rows for the given Moodboard Dyed Fabric ids in
    a single query and return them grouped by category, ready to render the
    per-category chip tabs (Cotton, Lyocell, ...).

    Returns:
        {
            <fabric_id>: [
                {'category': 'Cotton',  'types': ['BCI Cotton', 'Organic Cotton']},
                {'category': 'Lyocell', 'types': ['Tencel']},
            ],
            ...
        }
        Fabrics with no fiber types simply don't appear in the map.
    '''
    fabric_ids = list({fid for fid in (fabric_ids or []) if fid})
    if not fabric_ids:
        return {}

    Fiber = frappe.qb.DocType(DYED_FABRIC_FIBER_DOCTYPE)
    rows = (
        frappe.qb.from_(Fiber)
        .select(
            Fiber.parent,
            Fiber.category,
            Fiber.fiber_type,
            Fiber.idx,
        )
        .where(Fiber.parent.isin(fabric_ids))
        .where(Fiber.parenttype == DYED_FABRIC_MASTER_DOCTYPE)
        .where(Fiber.parentfield == 'fiber_types')
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
