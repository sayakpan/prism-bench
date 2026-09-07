import io

import frappe
from frappe.model.document import Document
from frappe.utils import flt, getdate

import prism.lib.cloud as cloud

MAX_IMAGE_FILE_SIZE_MB = 10

# Attach Image fields whose value is offloaded to S3 on save and dropped from the
# bucket on delete. Add a fieldname here and both paths pick it up.
IMAGE_FIELDS = ('actual_image', 'artwork_image')


class DevelopmentStyle(Document):
    def before_save(self):
        self._derive_month()
        self._derive_tentative_fob()

        for fieldname in IMAGE_FIELDS:
            _offload_image_to_s3(self, fieldname)

    def on_trash(self):
        for fieldname in IMAGE_FIELDS:
            _delete_image_from_s3(self, fieldname)

    def _derive_month(self):
        '''
        Fill Month from Development Date when it wasn't given. The legacy
        development sheets carry only a month name and no year, so Month stays
        writable on its own; this only covers the rows that do have a date.
        '''
        if self.month or not self.development_date:
            return

        self.month = getdate(self.development_date).strftime('%B').upper()

    def _derive_tentative_fob(self):
        '''
        Tentative FOB is MRP divided by a fixed factor (2.2 in the source sheets).
        Only filled when left blank, so a hand-entered or buyer-supplied FOB is
        never overwritten.
        '''
        if self.tentative_fob or not self.mrp:
            return

        factor = flt(self.fob_factor)
        if not factor:
            return

        self.tentative_fob = flt(self.mrp) / factor


def _offload_image_to_s3(doc, fieldname):
    '''
    Desk-attachment -> S3 offload for a Development Style image, mirroring
    brand._offload_logo_to_s3: the Attach Image control stages a local /files
    File, which we stream to S3, delete locally, and replace with the servable S3
    URL — so the image only ever lives in the bucket, never in Frappe assets.
    Idempotent: a value already on S3 (or empty) is left untouched, which also
    means a Data Import that supplies a ready-made S3 URL passes straight through.
    '''
    value = (doc.get(fieldname) or '').strip()
    if not (value.startswith('/files/') or value.startswith('/private/files/')):
        return

    file_name = frappe.db.get_value('File', {'file_url': value}, 'name')
    if not file_name:
        return  # nothing on disk to move; leave the value as-is

    file_doc = frappe.get_doc('File', file_name)
    content = file_doc.get_content()  # bytes
    if len(content) > MAX_IMAGE_FILE_SIZE_MB * 1024 * 1024:
        label = doc.meta.get_label(fieldname)
        frappe.throw(f'{label} exceeds the maximum allowed limit of {MAX_IMAGE_FILE_SIZE_MB} MB.')

    filename = file_doc.file_name or value
    key = cloud.build_development_style_image_key(doc.name, cloud.file_ext(filename))
    cloud.upload_file(io.BytesIO(content), key, cloud.content_type_for(filename))
    frappe.delete_doc('File', file_doc.name, ignore_permissions=True, force=True)

    doc.set(fieldname, cloud.asset_url(key))


def _delete_image_from_s3(doc, fieldname):
    '''
    Drop an offloaded image from the bucket when the Development Style is removed,
    so it doesn't orphan. Best-effort — a bucket failure is logged, never allowed
    to block the deletion.
    '''
    key = cloud.asset_key(doc.get(fieldname))
    if not key:
        return

    try:
        cloud.delete_object(key)
    except Exception:
        frappe.log_error(
            frappe.get_traceback(),
            f'Development Style.on_trash S3 cleanup ({fieldname})',
        )
