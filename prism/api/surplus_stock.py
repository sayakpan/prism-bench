import io
import json
import re

import frappe
from frappe.query_builder.functions import Count

from prism.auth.authenticator import auth_required
import prism.lib.cloud as cloud
import prism.api.util as util

DOCTYPE_SURPLUS_STOCK_ITEMS = 'Surplus Stock'

IMG_THUMBNAIL_CUT_SIZE = 1500

# The fields a posted sheet row is allowed to write. `has_image_file` and
# `image_urls` are deliberately absent -- they are owned by update_image_urls(),
# which reconciles them against S3, so a sheet push must never clobber them.
UPSERT_FIELDS = (
    'material', 'material_type_desc', 'material_desc', 'batch', 'plant',
    'total_qty', 'total_value_rs_lakh', 'base_uom', 'stock_segment',
    'storage_location', 'customer_name', 'quality', 'quality_full_name',
    'blend', 'blend_full_name', 'gsm', 'shade_catagory', 'color',
    'yarn_deatails', 'fab_code', 'fabric_type', 'width', 'dia', 'gauge',
    'ageing', 'so', 'hoi_item', 'testing_report', 's_l',
)
INT_FIELDS = ('gsm', 'width', 'dia', 'gauge')
FLOAT_FIELDS = ('total_qty', 'total_value_rs_lakh', 's_l')

# Fieldtype Data is varchar(140) -- overlong cells get truncated rather than
# failing the whole row.
DATA_FIELD_LENGTH = 140

UPSERT_COMMIT_CHUNK = 500  # rows per commit
UPSERT_LOOKUP_CHUNK = 500  # keys per IN(...) lookup
MAX_REPORTED_ERRORS = 50

# Sheet values that mean "nothing here".
NULL_TOKENS = {'', '-', '--', 'n/a', 'na', 'nan', 'none', 'null', 'undefined'}

_NUMBER_RE = re.compile(r'-?\d+(?:\.\d+)?')


# --- write ---
@frappe.whitelist(allow_guest=True)
def upsert():
    '''
    Adds or updates multiple records in DOCTYPE_SURPLUS_STOCK_ITEMS.
    Consider the field 'key' as primary key.

    The rows are posted as a JSON array of full rows -- every object carries
    all the columns:

        [{"key": "r2", "material": "...", "gsm": 180, ...}, ...]

    Object keys are Surplus Stock fieldnames; unknown ones are ignored, as is
    `has_image_file` (owned by update_image_flag(), never by the sheet). A
    column missing from a row is written as empty, so the record always ends
    up mirroring the sheet.

    A row with no `key` is skipped, and rows fail individually: one bad
    row is reported and the rest still import.

    Returns:
        {
            'success': True,
            'stats': {'total', 'created', 'updated', 'skipped'},
            'errors': [{'row': <1-based index>, 'reason': <message>}, ...],
        }
        `errors` holds at most MAX_REPORTED_ERRORS entries; stats['skipped'] 
        is the true failure count.
    '''

    try:
        # The rows are posted as a flat JSON array, so Frappe leaves them 
        # out of the args -- read the request body instead.
        rows = json.loads(frappe.request.data or '[]')
        if not isinstance(rows, list):
            frappe.throw('Expected a JSON array of surplus stock rows.')

        stats = {'total': len(rows), 'created': 0, 'updated': 0, 'skipped': 0}
        errors = []

        # key -> existing document name, resolved up front so the loop
        # below costs no lookup query per row.
        existing = _names_by_key(rows)

        for index, row in enumerate(rows, start=1):
            try:
                key = _clean_text(row.get('key'))
                if not key:
                    stats['skipped'] += 1
                    _add_error(errors, index, 'Row has no key')
                    continue

                values = _to_stock_values(row)
                name = existing.get(key)

                if name:
                    frappe.db.set_value(
                        DOCTYPE_SURPLUS_STOCK_ITEMS, name, values, update_modified=True
                    )
                    stats['updated'] += 1
                else:
                    values['key'] = key
                    doc = frappe.get_doc(dict(doctype=DOCTYPE_SURPLUS_STOCK_ITEMS, **values))
                    doc.insert(ignore_permissions=True)
                    # so a key repeated later in the same payload updates
                    # this row instead of inserting a duplicate
                    existing[key] = doc.name
                    stats['created'] += 1

            except Exception as ex:
                stats['skipped'] += 1
                _add_error(errors, index, str(ex))

            if index % UPSERT_COMMIT_CHUNK == 0:
                frappe.db.commit()

        frappe.db.commit()

        return {'success': True, 'stats': stats, 'errors': errors}

    except Exception as ex:
        frappe.db.rollback()
        frappe.log_error(frappe.get_traceback(), 'surplus_stock.upsert()')
        return {'success': False, 'error': str(ex)}


# --- read ----
@frappe.whitelist(allow_guest=True)
@auth_required
def list_all(
    qualities: list = [],
    blends: list = [],
    min_gsm: int = None,
    max_gsm: int = None,

    order_by: str = 'creation',
    order_dir: str = 'desc',

    page: int = 1,
    page_size: int = 20,
):
    '''
    Returns a paginated list of surplus stock items,
    optionally filtered by quality, blend and gsm range.

    Args:
        qualities: Optional list of quality values to filter on (IN).
        blends:    Optional list of blend values to filter on (IN).
        min_gsm:   Optional lower bound (inclusive) on gsm.
        max_gsm:   Optional upper bound (inclusive) on gsm.
        order_by:  Column to sort by (whitelisted; defaults to 'creation').
        order_dir: Sort direction, 'asc' or 'desc' (defaults to 'desc').
        page:      1-based page number.
        page_size: Number of rows per page (clamped to [1, 100]).

    Returns:
        {
            'success': True,
            'data': [<surplus stock row>, ...],
            'pagination': {
                'page', 'page_size', 'total_count', 'total_pages',
                'has_next', 'has_previous',
            }
        }
    '''

    try:
        #--- normalize paging / sorting params (whitelisted to avoid injection) ---
        ALLOWED_ORDER_FIELDS = [
            'creation', 'quality', 'blend', 'gsm', 'total_qty', 'ageing',
            'material', 'fabric_type', 'shade_catagory',
        ]

        page = max(1, int(page or 1))
        page_size = min(100, max(1, int(page_size or 20)))
        offset = (page - 1) * page_size

        if order_by not in ALLOWED_ORDER_FIELDS:
            order_by = 'creation'
        direction = frappe.qb.asc if str(order_dir).lower() == 'asc' else frappe.qb.desc

        #--- db surplus stock matches ---
        SurplusStock = frappe.qb.DocType(DOCTYPE_SURPLUS_STOCK_ITEMS)

        def _get_filters(q):
            if qualities:
                q = q.where(SurplusStock.quality.isin(qualities))
            if blends:
                q = q.where(SurplusStock.blend.isin(blends))
            if min_gsm is not None:
                q = q.where(SurplusStock.gsm >= int(min_gsm))
            if max_gsm is not None:
                q = q.where(SurplusStock.gsm <= int(max_gsm))
            return q

        #--- total count for pagination metadata ---
        count_query = _get_filters(
            frappe.qb.from_(SurplusStock).select(Count('*'))
        )
        total_count = count_query.run()[0][0]
        total_pages = (total_count + page_size - 1) // page_size

        #--- page of rows ---
        query = _get_filters(
            frappe.qb.from_(SurplusStock).select(*_row_columns(SurplusStock))
        ).orderby(
            getattr(SurplusStock, order_by), order=direction
        ).limit(page_size).offset(offset)

        rows = query.run(as_dict=True)

        # fabric images
        for row in rows:
            row['image_url'] = cloud.fabric_image_signed_url(row.get('batch')) if row['has_image_file'] else None
            row['thumbnail'] = cloud.fabric_thumbnail_signed_url(row.get('batch')) if row['has_image_file'] else None

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
        frappe.log_error(frappe.get_traceback(), 'surplus_stock.list_all()')
        return {'success': False, 'error': str(ex)}

@frappe.whitelist(allow_guest=True)
@auth_required
def details(item_id: str):
    '''
    Returns the details of a surplus_stock_item.

    Args:
        item_id: The unique surplus stock item id (SurplusStock.name).

    Returns:
        {'success': True, 'data': <surplus stock row>}
        or {'success': False, 'error': <message>} when not found / on error.
    '''

    try:
        if not item_id:
            return {'success': False, 'error': 'item_id is required'}

        SurplusStock = frappe.qb.DocType(DOCTYPE_SURPLUS_STOCK_ITEMS)

        rows = (
            frappe.qb.from_(SurplusStock)
            .select(*_row_columns(SurplusStock))
            .where(SurplusStock.name == item_id)
            .limit(1)
            .run(as_dict=True)
        )

        if not rows:
            return {'success': False, 'error': 'Surplus stock item not found'}

        row = rows[0]

        row['image_url'] = cloud.fabric_image_signed_url(row.get('batch')) if row['has_image_file'] else None
        row['thumbnail'] = cloud.fabric_thumbnail_signed_url(row.get('batch')) if row['has_image_file'] else None

        return {'success': True, 'data': row}

    except Exception as ex:
        frappe.log_error(frappe.get_traceback(), 'surplus_stock.details()')
        return {'success': False, 'error': str(ex)}


# utility
@frappe.whitelist(allow_guest=True)
def update_image_flag():
    '''
    1. gets the list of all fabric image files in the relevant S3 bucket.
    2. sets the 'has_image_file' flag in doctype DOCTYPE_SURPLUS_STOCK_ITEMS for which
       the image exists in S3 (match with the 'batch' field).

    Returns a summary dict with the number of rows flagged / unflagged.
    '''
    try:
        #--- 1. batches that actually have an image file in S3 ---
        image_batches = cloud.get_all_dyed_fabric_batches()

        #--- 2. reconcile the flag against current db state ---
        StockItems = frappe.qb.DocType(DOCTYPE_SURPLUS_STOCK_ITEMS)
        rows = (
            frappe.qb
            .from_(StockItems)
            .select(
                StockItems.name,
                StockItems.batch,
                StockItems.has_image_file,
            )
            .where(StockItems.batch.notnull())
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
                    .update(StockItems)
                    .set(StockItems.has_image_file, value)
                    .where(StockItems.name.isin(chunk))
                    .run()
                )
        frappe.db.commit()

        # generate thumbnails
        #th_res = generate_thumbnails()

        # delete orphan thumbnails
        #del_res = cloud.delete_orphan_fabric_thumbnails()

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
            #'generate_thumbnails': th_res,
            #'delete_orphan_thumbnails': del_res
        }

    except Exception as ex:
        frappe.log_error(frappe.get_traceback(), 'surplus_stock.update_image_flag()')
        return {'success': False, 'error': str(ex)}

@frappe.whitelist(allow_guest=True)
def update_image_urls():
    '''
    Refreshes the `image_urls` JSON field -- and the `has_image_file` flag that
    goes with it -- on every Surplus Stock row from the images currently in S3.

    This is the surplus equivalent of garment.update_image_urls(), and it
    supersedes update_image_flag(): the flag says only *whether* an image
    exists, while `image_urls` records *which* object keys do, so a reader never
    has to guess a URL that may 403. Both are written here so they cannot drift
    apart.

    Only batches whose image is actually in the bucket get an entry. A row whose
    image has gone away is reset to NULL / 0 in the same pass, so this is a
    reconcile and safe to re-run.

    Stored per row (object KEYS, not URLs -- cloud.public_asset_url() turns them
    into URLs at read time, keeping the basepath out of the database):

        {"image": "images/dyed-fabric/<batch>.jpg",
         "thumbnail": "images/dyed-fabric/thumb/<batch>.jpg"}

    `thumbnail` is present only when a thumbnail exists for that batch. Nothing
    in S3 produces thumbnails on its own -- they are cut from the original by
    create_thumbnail(). So this ends by calling generate_thumbnails(), the same
    way fabric.update_image_flag() does, and the job records its own key as it
    uploads. On a batch whose image was only just added, the first pass stores
    `image` alone and the thumbnail lands moments later.

    Built for scale the same way the garment one is: the image folder is listed
    ONCE and turned into an in-memory batch index, rather than one S3 request
    per row, and only rows whose stored value actually differs are written.

    Returns a summary dict with the number of rows scanned / matched / updated,
    plus the thumbnail job's queue result.
    '''
    try:
        #--- 1. batch -> {image, thumbnail} from a single S3 listing ---
        image_index = _build_surplus_image_index(
            cloud.list_folder_objects(cloud.DYED_FABRIC_IMG_FOLDER_NAME)
        )

        #--- 2. read every row that could carry an image ---
        StockItems = frappe.qb.DocType(DOCTYPE_SURPLUS_STOCK_ITEMS)
        rows = (
            frappe.qb
            .from_(StockItems)
            .select(
                StockItems.name,
                StockItems.batch,
                StockItems.image_urls,
                StockItems.has_image_file,
            )
            .where(StockItems.batch.notnull())
            .run(as_dict=True)
        )

        #--- 3. collect only the rows whose keys or flag changed ---
        doc_updates = {}
        matched = 0
        for row in rows:
            desired = image_index.get(row['batch'], {})
            if desired:
                matched += 1

            update = {}
            if desired != _parse_image_urls(row.get('image_urls')):
                update['image_urls'] = json.dumps(desired) if desired else None
            if bool(desired) != bool(row.get('has_image_file')):
                update['has_image_file'] = 1 if desired else 0

            if update:
                doc_updates[row['name']] = update

        #--- 4. persist in chunked, multi-row UPDATEs ---
        if doc_updates:
            frappe.db.bulk_update(
                DOCTYPE_SURPLUS_STOCK_ITEMS,
                doc_updates,
                chunk_size=500,
                update_modified=False,
            )
            frappe.db.commit()

        #--- 5. queue thumbnails for the originals that still lack one ---
        #    Nothing in S3 creates thumbnails on its own; they are cut from the
        #    original by create_thumbnail() below. The job writes its own key
        #    back into `image_urls`, so a thumbnail generated after this pass
        #    still lands in the record without waiting for the next reconcile.
        th_res = generate_thumbnails()

        return {
            'success': True,
            'stats': {
                'total_db_rows': len(rows),
                'batches_with_images_in_s3': len(image_index),
                'batches_with_thumbnails': sum(
                    1 for keys in image_index.values() if keys.get('thumbnail')
                ),
                'rows_with_images': matched,
                'rows_updated': len(doc_updates),
            },
            'generate_thumbnails': th_res,
        }

    except Exception as ex:
        frappe.log_error(frappe.get_traceback(), 'surplus_stock.update_image_urls()')
        return {'success': False, 'error': str(ex)}

@frappe.whitelist(allow_guest=True)
def generate_thumbnails():
    '''
    Starts a long running background job to generate the thumbnail image of the
    surplus stock batches for which 'has_image_file' is 1 and the thumbnail image
    is either missing or stale (its creation date-time is prior to the upload
    date-time of the original image).

    The surplus twin of fabric.generate_thumbnails(). Both doctypes key their
    images off the batch number in the same S3 folder, so the work is deduped by
    BATCH here rather than by row -- a fab code carries many lots per batch, and
    cutting the same thumbnail once per lot would be pure waste.
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

        #--- 3. batches whose thumbnail is missing or stale (older than original) ---
        StockItems = frappe.qb.DocType(DOCTYPE_SURPLUS_STOCK_ITEMS)
        rows = (
            frappe.qb
            .from_(StockItems)
            .select(StockItems.batch)
            .distinct()
            .where(StockItems.has_image_file == 1)
            .where(StockItems.batch.notnull())
            .run(as_dict=True)
        )

        todo = []
        for row in rows:
            batch = row['batch']
            src_dtm = original_dtm.get(batch)
            if not src_dtm:
                continue  # no source image in S3 -> nothing to generate from

            existing_dtm = thumb_dtm.get(batch)
            # (re)generate when the thumbnail is absent or predates the original
            if existing_dtm is None or existing_dtm < src_dtm:
                todo.append(batch)

        if not todo:
            return {'success': True, 'data': {'queued': 0, 'message': 'All thumbnails are up to date.'}}

        #--- 4. hand the generation work off to a long-running background job ---
        frappe.enqueue(
            'prism.api.surplus_stock._generate_thumbnails_job',
            queue='long',
            timeout=10800,
            batches=todo,
        )

        return {'success': True, 'data': {'queued': len(todo)}}

    except Exception as ex:
        frappe.log_error(frappe.get_traceback(), 'surplus_stock.generate_thumbnails()')
        return {'success': False, 'error': str(ex)}

def _generate_thumbnails_job(batches: list):
    '''
    Background worker for generate_thumbnails(): create a thumbnail for each of
    the given batches, tolerating individual failures so one bad image does not
    abort the whole run.
    '''
    created, failed = 0, 0
    for batch in (batches or []):
        result = create_thumbnail(batch)
        if result.get('success'):
            created += 1
        else:
            failed += 1
    return {'total': len(batches or []), 'created': created, 'failed': failed}

@frappe.whitelist(allow_guest=True)
def create_thumbnail(batch: str):
    '''
    Cuts the low-res thumbnail for one batch from its original image in S3 and
    uploads it alongside, under the `thumb/` subfolder.

    Keyed by batch rather than by row (fabric.create_fabric_thumbnail() takes a
    fabric id) because the image belongs to the batch, and a surplus batch is
    shared by many stock rows.

    On success the new thumbnail key is written into `image_urls` on every row
    carrying that batch. Without that write the key would only appear at the
    next update_image_urls() run, and the field's promise -- that every key in
    it is a key that exists -- is kept by recording it the moment it does.
    '''
    try:
        if not batch:
            return {'success': False, 'error': 'batch is required'}

        # source image: images/dyed-fabric/<batch>.jpg
        src_key = cloud.dyed_fabric_image_key(batch)
        src_bytes = cloud.download_object(src_key)
        if not src_bytes:
            return {'success': False, 'error': f'No image found in S3 for batch "{batch}".'}

        thumb_bytes = util.make_image_thumbnail(src_bytes, IMG_THUMBNAIL_CUT_SIZE)
        if not thumb_bytes:
            return {
                'success': False,
                'error': f'Image for batch "{batch}" is smaller than {IMG_THUMBNAIL_CUT_SIZE}px!'
            }

        # thumbnail lives in the dedicated subfolder
        thumb_key = cloud.dyed_fabric_thumbnail_key(batch)
        cloud.upload_file(io.BytesIO(thumb_bytes), thumb_key, content_type='image/jpeg')

        rows_updated = _record_thumbnail_key(batch, src_key, thumb_key)

        return {
            'success': True,
            'data': {
                'batch': batch,
                'thumbnail_key': thumb_key,
                'rows_updated': rows_updated,
            }
        }

    except Exception as ex:
        frappe.log_error(frappe.get_traceback(), 'surplus_stock.create_thumbnail()')
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
        DyedFabric = frappe.qb.DocType(DOCTYPE_SURPLUS_STOCK_ITEMS)

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
        frappe.log_error(frappe.get_traceback(), 'surplus_stock.get_image_stat()')
        return {'success': False, 'error': str(ex)}


#--- helpers ---
def _build_surplus_image_index(objects: list):
    '''
    Turns one flat listing of the dyed-fabric image folder into a
    batch -> {'image': <key>[, 'thumbnail': <key>]} map.

    That single folder holds both the originals (`<batch>.jpg`) and their
    thumbnails (`thumb/<batch>.jpg`), so each key is placed by whether it sits
    directly under the folder or inside the thumbnail subfolder -- the same
    depth test cloud.delete_orphan_fabric_thumbnails() makes.

    A batch is indexed only when its ORIGINAL exists. A thumbnail with no
    original behind it is an orphan, and indexing it would put a record back
    into the "has an image" state on the strength of a leftover file.
    '''
    prefix = f'{cloud.DYED_FABRIC_IMG_FOLDER_NAME}/'
    thumb_prefix = f'{prefix}{cloud.DYED_FABRIC_THUMBNAIL_FOLDER_NAME}/'
    extension = cloud.DYED_FABRIC_IMG_EXTENSION

    images, thumbnails = {}, {}
    for obj in (objects or []):
        key = obj.get('key') if isinstance(obj, dict) else obj
        if not key or not key.lower().endswith(extension.lower()):
            continue

        if key.startswith(thumb_prefix):
            batch, bucket = key[len(thumb_prefix):-len(extension)], thumbnails
        else:
            batch, bucket = key[len(prefix):-len(extension)], images
            if '/' in batch:
                continue  # some deeper subfolder, not a fabric image

        if batch:
            bucket[batch] = key

    index = {}
    for batch, key in images.items():
        index[batch] = {'image': key}
        if batch in thumbnails:
            index[batch]['thumbnail'] = thumbnails[batch]

    return index

def _record_thumbnail_key(batch, image_key, thumb_key):
    '''
    Writes a freshly generated thumbnail key into `image_urls` on every row with
    this batch, and returns how many rows were touched.

    `image` is set alongside it because a row can only reach here when its
    original exists -- that is what create_thumbnail() just downloaded -- so a
    row whose reconcile has not run yet gets a complete entry rather than a
    thumbnail hanging off nothing.
    '''
    rows = frappe.get_all(
        DOCTYPE_SURPLUS_STOCK_ITEMS,
        filters={'batch': batch},
        fields=['name', 'image_urls', 'has_image_file'],
        limit_page_length=0,
    )

    desired = {'image': image_key, 'thumbnail': thumb_key}
    doc_updates = {}
    for row in rows:
        update = {}
        if _parse_image_urls(row.get('image_urls')) != desired:
            update['image_urls'] = json.dumps(desired)
        if not row.get('has_image_file'):
            update['has_image_file'] = 1
        if update:
            doc_updates[row['name']] = update

    if doc_updates:
        frappe.db.bulk_update(
            DOCTYPE_SURPLUS_STOCK_ITEMS,
            doc_updates,
            chunk_size=500,
            update_modified=False,
        )
        frappe.db.commit()

    return len(doc_updates)

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

def _row_columns(SurplusStock):
    '''The columns making up a surplus stock row, shared by list_all() and details().'''
    return [
        SurplusStock.name.as_('id'),
        SurplusStock.key,
        SurplusStock.material,
        SurplusStock.material_type_desc,
        SurplusStock.material_desc,
        SurplusStock.batch,
        SurplusStock.plant,
        SurplusStock.total_qty,
        SurplusStock.total_value_rs_lakh,
        SurplusStock.base_uom,
        SurplusStock.stock_segment,
        SurplusStock.storage_location,
        SurplusStock.customer_name,
        SurplusStock.quality,
        SurplusStock.quality_full_name,
        SurplusStock.blend,
        SurplusStock.blend_full_name,
        SurplusStock.gsm,
        SurplusStock.shade_catagory,
        SurplusStock.color,
        SurplusStock.yarn_deatails,
        SurplusStock.fab_code,
        SurplusStock.fabric_type,
        SurplusStock.width,
        SurplusStock.dia,
        SurplusStock.gauge,
        SurplusStock.ageing,
        SurplusStock.so,
        SurplusStock.hoi_item,
        SurplusStock.testing_report,
        SurplusStock.s_l,
        SurplusStock.has_image_file,
    ]

def _names_by_key(rows):
    ''' key -> existing document name, in a few chunked queries rather than one per row. '''
    ids = {_clean_text(row.get('key')) for row in rows}
    ids.discard(None)
    ids = sorted(ids)

    existing = {}
    for i in range(0, len(ids), UPSERT_LOOKUP_CHUNK):
        for row in frappe.get_all(
            DOCTYPE_SURPLUS_STOCK_ITEMS,
            filters={'key': ['in', ids[i:i + UPSERT_LOOKUP_CHUNK]]},
            fields=['name', 'key'],
            limit_page_length=0,
        ):
            existing[row['key']] = row['name']

    return existing

def _to_stock_values(row):
    ''' One posted row -> the full set of Surplus Stock field values. '''
    values = {}
    for field in UPSERT_FIELDS:
        value = row.get(field)
        if field in INT_FIELDS:
            values[field] = _to_number(value, int)
        elif field in FLOAT_FIELDS:
            values[field] = _to_number(value, float)
        else:
            text = _clean_text(value)
            values[field] = text[:DATA_FIELD_LENGTH] if text else None
    return values

def _clean_text(value):
    '''
    Cell value -> trimmed text, or None. Blank cells and the placeholders a sheet
    writes for a missing value ("None", "N/A", "-") both collapse to None so they
    are never stored as content.
    '''
    if value is None:
        return None
    if isinstance(value, float) and value.is_integer():
        value = int(value)  # drop the ".0" a sheet adds to whole numbers
    text = str(value).strip()
    return None if text.lower() in NULL_TOKENS else text

def _to_number(value, cast):
    '''
    Cell value -> number. Blank and unparsable cells become 0: Frappe's numeric
    columns are NOT NULL DEFAULT 0, and the db.set_value() used on the update
    path writes raw SQL that would reject None.
    '''
    if isinstance(value, bool):
        return cast(value)
    if isinstance(value, (int, float)):
        return cast(value)

    text = _clean_text(value)
    if not text:
        return 0
    match = _NUMBER_RE.search(text.replace(',', ''))
    return cast(float(match.group())) if match else 0

def _add_error(errors, row_number, reason):
    '''
    Record a row failure. The list is capped so a wholly broken payload returns a
    readable sample rather than megabytes of error text -- stats['skipped'] still
    carries the true total.
    '''
    if len(errors) < MAX_REPORTED_ERRORS:
        errors.append({'row': row_number, 'reason': reason})
