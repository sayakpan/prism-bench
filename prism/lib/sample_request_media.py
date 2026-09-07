'''
S3 offload for the Sample Request media child tables (garment images + product
videos), attached to the Desk-managed "Sample Request" doctype via doc_events in
hooks.py. Mirrors prism/prism/doctype/moodboard_style/moodboard_style.py, which
does the same for a Moodboard Style's `garment_images` / `product_video` inside
its own before_save/on_trash — but Sample Request has no app-owned controller, so
the equivalent hooks live here instead.

A Desk "Attach" upload lands as a local Frappe File (/files/... or
/private/files/...). On save we stream each such row to S3, rewrite the row to the
S3 URL, and delete the now-redundant local File. Rows already pointing at S3 (a
re-save, or a value set through an API) are left untouched, so the offload is
idempotent. On delete we remove the S3 objects so they don't orphan in the bucket.
'''

import io

import frappe

import prism.lib.cloud as cloud

MAX_IMAGE_FILE_SIZE_MB = 5
MAX_VIDEO_FILE_SIZE_MB = 200


def before_save(doc, method=None):
    ''' doc_events["Sample Request"]["before_save"] — offload freshly-attached media to S3. '''
    _offload_rows(
        doc.get('garment_images'), 'image',
        cloud.build_sample_request_image_key, MAX_IMAGE_FILE_SIZE_MB, doc.name, 'garment image',
    )
    _offload_rows(
        doc.get('product_video'), 'video',
        cloud.build_sample_request_video_key, MAX_VIDEO_FILE_SIZE_MB, doc.name, 'product video',
    )


def on_trash(doc, method=None):
    ''' doc_events["Sample Request"]["on_trash"] — delete this request's S3 media objects. '''
    _delete_rows(doc.get('garment_images'), 'image', 'garment_images')
    _delete_rows(doc.get('product_video'), 'video', 'product_video')


def _offload_rows(rows, fieldname, build_key, max_mb, sample_request, label):
    '''
    Stream each row's local Frappe file to S3, rewrite the row to the S3 URL, and
    delete the local File. Rows already on S3 (or empty) are skipped. Matches
    MoodboardStyle._offload_garment_images_to_s3.
    '''
    for row in (rows or []):
        value = (row.get(fieldname) or '').strip()
        if not _is_local_file(value):
            continue

        file_doc = _find_file(value)
        if not file_doc:
            continue  # nothing on disk to move; leave the value as-is

        content = file_doc.get_content()  # bytes
        if len(content) > max_mb * 1024 * 1024:
            frappe.throw(f'{label.capitalize()} exceeds the maximum allowed limit of {max_mb} MB.')

        filename = file_doc.file_name or value
        key = build_key(sample_request, cloud.file_ext(filename))
        cloud.upload_file(io.BytesIO(content), key, cloud.content_type_for(filename))

        row.set(fieldname, cloud.asset_url(key))
        frappe.delete_doc('File', file_doc.name, ignore_permissions=True, force=True)


def _delete_rows(rows, fieldname, table_label):
    '''
    Best-effort S3 cleanup of each row's object on delete. S3 delete is idempotent,
    and a failure is logged rather than allowed to block the document deletion.
    Matches MoodboardStyle._delete_s3_assets.
    '''
    for row in (rows or []):
        key = cloud.asset_key(row.get(fieldname))
        if not key:
            continue
        try:
            cloud.delete_object(key)
        except Exception:
            frappe.log_error(frappe.get_traceback(), f'Sample Request on_trash S3 cleanup ({table_label})')


def _is_local_file(value):
    return bool(value) and (
        value.startswith('/files/') or value.startswith('/private/files/')
    )


def _find_file(file_url):
    name = frappe.db.get_value('File', {'file_url': file_url}, 'name')
    return frappe.get_doc('File', name) if name else None
