import io

import frappe
from frappe.model.document import Document
from frappe.utils import slug

import prism.lib.cloud as cloud

MAX_LOGO_FILE_SIZE_MB = 5


class Brand(Document):
    def autoname(self):
        self.name = generate_unique_slug(self.brand, self.doctype)

    def before_save(self):
        _offload_logo_to_s3(self)

    def after_insert(self):
        # generate the brand's style profile (LLM-backed) in the background
        frappe.enqueue(
            'prism.api.brand._update_style_profile',
            queue='long',
            brand_id=self.name,
        )

    def on_update(self):
        # clear cache when modified
        cache_key = f'brand_details:{self.name}'
        frappe.cache.delete_value(cache_key)

        # extract the brand's visual theme from its website (LLM-backed), once,
        # whenever a website is set but the theme has not been generated yet.
        # on_update fires on both insert and every subsequent save, so this
        # covers "user hits save from anywhere".
        self._maybe_enqueue_brand_theme()

    def _maybe_enqueue_brand_theme(self):
        if not self.website or self.brand_theme:
            return

        # avoid enqueuing duplicate jobs while one is already in flight
        # (e.g. rapid re-saves before the first job writes the theme back)
        lock_key = f'brand_theme_lock:{self.name}'
        if frappe.cache.get_value(lock_key):
            return
        frappe.cache.set_value(lock_key, 1, expires_in_sec=600)

        frappe.enqueue(
            'prism.api.brand._update_brand_theme',
            queue='long',
            brand_id=self.name,
        )

    def on_trash(self):
        # clear cache when deleted
        cache_key = f'brand_details:{self.name}'
        frappe.cache.delete_value(cache_key)

        _delete_logo_from_s3(self)


def _offload_logo_to_s3(doc):
    '''
    Desk-attachment -> S3 offload for the brand logo, mirroring
    moodboard_style._offload_document_to_s3: the Attach Image control stages a
    local /files File, which we stream to S3, delete locally, and replace with the
    servable S3 URL — so the logo only ever lives in the bucket, never in Frappe
    assets. Idempotent: a value already on S3 (or empty) is left untouched.
    '''
    value = (doc.logo or '').strip()
    if not (value.startswith('/files/') or value.startswith('/private/files/')):
        return

    file_name = frappe.db.get_value('File', {'file_url': value}, 'name')
    if not file_name:
        return  # nothing on disk to move; leave the value as-is

    file_doc = frappe.get_doc('File', file_name)
    content = file_doc.get_content()  # bytes
    if len(content) > MAX_LOGO_FILE_SIZE_MB * 1024 * 1024:
        frappe.throw(f'Logo exceeds the maximum allowed limit of {MAX_LOGO_FILE_SIZE_MB} MB.')

    filename = file_doc.file_name or value
    key = cloud.build_brand_logo_key(doc.name, cloud.file_ext(filename))
    cloud.upload_file(io.BytesIO(content), key, cloud.content_type_for(filename))
    frappe.delete_doc('File', file_doc.name, ignore_permissions=True, force=True)

    doc.logo = cloud.asset_url(key)


def _delete_logo_from_s3(doc):
    '''
    Drop the brand's offloaded logo from the bucket when the Brand is removed, so
    it doesn't orphan. Best-effort — a bucket failure is logged, never allowed to
    block the deletion.
    '''
    key = cloud.asset_key(doc.get('logo'))
    if not key:
        return
    try:
        cloud.delete_object(key)
    except Exception:
        frappe.log_error(frappe.get_traceback(), 'Brand.on_trash S3 cleanup (logo)')


def generate_unique_slug(text, doctype):
    text = text.replace('&', 'and')
    base_slug = slug(text)
    unique_slug = base_slug
    
    counter = 1
    while frappe.db.exists(doctype, unique_slug):
        unique_slug = f'{base_slug}-{counter}'
        counter += 1

    return unique_slug
