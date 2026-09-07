import io

import frappe
from frappe.model.document import Document
from frappe.utils import cint

import prism.lib.cloud as cloud
import prism.lib.bom as bomlib
import prism.lib.esg as esglib
from prism.prism.doctype.moodboard_style_render.moodboard_style_render import build_render_key

MAX_MODEL_FILE_SIZE_MB = 50
MAX_GARMENT_FILE_SIZE_MB = 25
MAX_GARMENT_IMAGE_FILE_SIZE_MB = 5
MAX_BOM_FILE_SIZE_MB = 25
MAX_VIDEO_FILE_SIZE_MB = 200

# The main render (`image` and each `renders` row) is generator output — a
# full-size PNG, routinely several MB. Capped higher than a hand-uploaded gallery
# image, which stays at MAX_GARMENT_IMAGE_FILE_SIZE_MB.
MAX_STYLE_IMAGE_FILE_SIZE_MB = 15

# The Moodboard Style fields whose values are S3-offloaded objects (GLB, garment
# file, BOM, product video, and the per-garment crop `image`). Cleanup on delete is
# scoped to exactly these.
_S3_ASSET_FIELDS = ('model_3d', 'garment_file', 'bom', 'product_video', 'image')

# Style text fields scanned (in order) for a GSM to cross-check against the BOM.
_STYLE_GSM_FIELDS = ('fabric_quality', 'garment_name', 'description')


class MoodboardStyle(Document):
    def validate(self):
        # Order matters: the redesign check must run before the verdict is
        # reconciled, so `include` follows the reset rather than the stale verdict.
        _reset_decision_on_redesign(self)
        _reconcile_decision(self)
        # After the verdict is settled: `in_moodboard` hangs off it and would
        # otherwise be reconciled against the value the save came in with.
        _reconcile_moodboard_membership(self)
        _reconcile_renders(self)
        _reconcile_image_status(self)

    def before_save(self):
        # Parse the BOM before the offload — the freshly-attached local file is
        # still on disk here, and `bom` hasn't yet been rewritten to an S3 URL.
        _extract_bom_metrics(self)
        _offload_model_to_s3(self)
        # Renders first: the current render and `image` are usually the same staged
        # file, and this hands the S3 URL up to `image` so the bytes are uploaded
        # once and the row is never left pointing at a File the image offload deleted.
        _offload_renders_to_s3(self)
        _offload_document_to_s3(self, 'image', cloud.build_style_image_key, MAX_STYLE_IMAGE_FILE_SIZE_MB)
        _offload_document_to_s3(self, 'garment_file', cloud.build_garment_file_key, MAX_GARMENT_FILE_SIZE_MB)
        _offload_document_to_s3(self, 'bom', cloud.build_bom_file_key, MAX_BOM_FILE_SIZE_MB)
        _offload_document_to_s3(self, 'product_video', cloud.build_product_video_key, MAX_VIDEO_FILE_SIZE_MB)
        _offload_garment_images_to_s3(self)

    def on_update(self):
        _flag_board_unpublished(self.moodboard)
        _ensure_esg(self)

    def on_trash(self):
        _delete_s3_assets(self)
        _flag_board_unpublished(self.moodboard)


_DECISIONS = ('Pending', 'Approved', 'Rejected')

# What makes this row *a different garment*. A regenerate reuses the stream index
# 0…5 of the run before it, so an upsert rewrites the piece in place — and a
# verdict that survived that would transfer an approval to a garment nobody saw.
#
# Fabric and colour are deliberately NOT in here: switching a style's fabric is a
# deliberate act on a piece someone already approved, and un-approving it every
# time would make the fabric picker unusable.
_GARMENT_IDENTITY_FIELDS = (
    'garment_name', 'product_category', 'gender', 'description',
    'design_brief', 'signature_details',
)


def _reset_decision_on_redesign(doc):
    '''
    Send the verdict back to Pending when the garment underneath it changes.

    Skipped when the caller moved `decision` in this same save — ruling on the new
    version and rewriting it at once is their call to make, not ours to overrule.
    '''
    before = doc.get_doc_before_save()
    if not before:
        return
    if (doc.decision or 'Pending') == 'Pending':
        return
    if (doc.decision or '') != (before.decision or ''):
        return
    if _garment_fingerprint(doc) == _garment_fingerprint(before):
        return

    doc.decision = 'Pending'
    doc.decision_comment = None


def _garment_fingerprint(doc):
    return tuple((doc.get(f) or '').strip() for f in _GARMENT_IDENTITY_FIELDS)


def _reconcile_decision(doc):
    '''
    Keep `include` and `decision` from ever disagreeing, whichever one the save
    touched, and enforce the two rules the verdict carries.

    `include` is driven by the verdict — Approved sets it, Rejected and Pending
    clear it — so an approval reaches every downstream consumer (costing, ESG,
    techpack, the published set) without any of them learning what a decision is.

    The reverse direction is what keeps the older write paths working. The legacy
    StylesStep save (api.moodboard_style.sync_styles) and the Desk "Include"
    checkbox both set `include` and know nothing about `decision`; when only the
    checkbox moved in this save, the checkbox IS the verdict for that save. Only
    an Approved row is demoted to Pending on an untick — a Rejected row stays
    rejected (its comment is still the truth, and unticking an already-clear box
    is a no-op anyway).

    Rejected without a comment is refused here rather than only in the dialog:
    the workbench, the Desk form and any script all land on this one check.
    '''
    before = doc.get_doc_before_save()
    prev_decision = (before.decision or '') if before else ''
    prev_include = cint(before.include) if before else None

    decision = (doc.decision or '').strip()
    if decision and decision not in _DECISIONS:
        frappe.throw(f'Unknown decision "{decision}". Expected one of: {", ".join(_DECISIONS)}.')

    supplied = bool(decision)
    toggled = (
        prev_include is not None
        and cint(doc.include) != prev_include
        and decision == prev_decision
    )

    if not decision:
        # Never decided (a legacy row, or a create that only set `include`).
        decision = 'Approved' if cint(doc.include) else 'Pending'
    elif toggled:
        # The checkbox moved on its own — treat it as the verdict for this save.
        decision = 'Approved' if cint(doc.include) else ('Pending' if decision == 'Approved' else decision)

    doc.decision = decision
    doc.include = 1 if decision == 'Approved' else 0

    if decision == 'Rejected' and not (doc.decision_comment or '').strip():
        frappe.throw('A comment is required to reject a style.', frappe.MandatoryError)
    if decision != 'Rejected':
        doc.decision_comment = None

    # Pending means nobody has ruled yet, so it carries no decider — which also
    # keeps the legacy include=1 -> Approved backfill above from inventing one.
    if decision == 'Pending':
        doc.decided_by = None
        doc.decided_on = None
    elif decision != prev_decision and (supplied or toggled):
        if not before or doc.decided_by == before.decided_by:
            doc.decided_by = _current_user()
        if not before or doc.decided_on == before.decided_on:
            doc.decided_on = frappe.utils.now_datetime()


def _reconcile_moodboard_membership(doc):
    '''
    Keep `in_moodboard` from outliving the approval it depends on.

    Only an Approved style can be in the moodboard, so a verdict leaving Approved
    clears the flag in the same save that moves it. That is the whole reason this
    lives here and not in the client: the un-approve and the un-include are one
    transaction, and a client-side clear leaves a rejected style still flagged for
    the board every time the second call never lands.

    Moving *into* Approved sets it, because approval is what puts a style in the
    board and narrowing is an explicit deselection. Note that is a transition, not
    a floor — a deselection made while the style stayed Approved survives every
    later save, which is what makes set_style_moodboard's 0 stick.

    Runs after _reconcile_decision, so `decision` is already the verdict this save
    settled on rather than whatever the payload carried.
    '''
    before = doc.get_doc_before_save()
    prev_decision = (before.decision or '') if before else ''

    if (doc.decision or '') != 'Approved':
        doc.in_moodboard = 0
    elif prev_decision != 'Approved':
        doc.in_moodboard = 1


def _current_user():
    ''' The acting user, or None for Guest / the scheduler (decided_by is a User link). '''
    user = frappe.session.user
    return user if user and user != 'Guest' and frappe.db.exists('User', user) else None


def _reconcile_renders(doc):
    '''
    Keep the `renders` cache table self-consistent: every row carries the key it
    is looked up by, no row claims to be current without an image, and at most one
    row is current (the last one flagged wins, so a caller can just set the new
    row and leave the old one alone).
    '''
    rows = doc.renders or []
    for row in rows:
        row.render_key = row.render_key or build_render_key(
            row.fab_code, row.element_colour_tcx, row.print_label, row.recolour,
            row.fab_batch)
        row.status = row.status or ('ready' if (row.image or '').strip() else 'failed')
        if row.status == 'ready':
            row.error = None
        else:
            row.is_current = 0  # a failed render is never what's on the card

    current = [r for r in rows if cint(r.is_current)]
    for row in current[:-1]:
        row.is_current = 0


_IMAGE_STATUSES = ('pending', 'ready', 'stale', 'failed')

# A status that claims a picture. Asserting either without one is a contradiction,
# so it falls back to pending.
_STATUSES_NEEDING_IMAGE = ('ready', 'stale')


def _reconcile_image_status(doc):
    '''
    Keep `image_status` honest about `image`, so a row saved at `plan` time —
    3-4 minutes before the first render lands — never reads as a finished style.

    Having a picture is NOT the same as that picture being current. A fabric
    switch with no cached render keeps the last good image and says `stale`,
    because a stale picture beats an empty frame; a render that then fails says
    `failed` and still keeps it. So an explicit status is believed, and only two
    things are corrected: a status that claims a picture there isn't one, and a
    picture that moved without anybody saying what that means (a Desk attach, a
    plain image write) — which is a new current render, i.e. `ready`.

    `image_error` only survives on `failed`; every other state clears it.
    '''
    before = doc.get_doc_before_save()
    prev_image = (before.image or '').strip() if before else ''
    prev_status = (before.image_status or '').strip() if before else ''

    image = (doc.image or '').strip()
    status = (doc.image_status or '').strip()
    if status not in _IMAGE_STATUSES:
        status = ''

    if image != prev_image and status == prev_status:
        # The picture moved on its own — that IS the statement.
        status = 'ready' if image else 'pending'
    if not status:
        status = 'ready' if image else 'pending'
    if not image and status in _STATUSES_NEEDING_IMAGE:
        status = 'pending'

    doc.image_status = status
    if status != 'failed':
        doc.image_error = None


def _ensure_esg(doc):
    '''
    Auto-create the Moodboard ESG once a style is costed (cost_status == Computed).
    Fires on every save path (the update_style_cost API and direct Desk cost edits);
    create-once, so it's a cheap no-op after the first time. Best-effort — an ESG
    build failure is logged and never blocks the style save.
    '''
    if doc.cost_status != 'Computed':
        return
    try:
        esglib.ensure_esg_for_style(doc)
    except Exception:
        frappe.log_error(frappe.get_traceback(), 'MoodboardStyle ESG auto-create')


def _extract_bom_metrics(doc):
    '''
    When a *new* BOM spreadsheet is set on the style, parse it and auto-fill
    `consumption` and `marker_efficiency` (see prism.lib.bom). Covers every upload
    path — Desk attach, the workbench (frappe.client.set_value), and the upload
    APIs — because all of them land here via doc.save().

    Runs before the S3 offload, so a Desk/workbench upload is read from the local
    File; the API path (where `bom` is already an S3 URL) is read back from S3.
    Best-effort: failures are logged, never block the save, and fields are only
    overwritten when a value is successfully derived (so a malformed BOM or a
    manual edit is preserved).
    '''
    try:
        value = (doc.bom or '').strip()
        if not value or value == _prev_value(doc, 'bom'):
            return  # no new BOM this save
        if cloud.file_ext(value) not in ('.xlsx', '.xlsm'):
            return  # only spreadsheet BOMs are parseable

        content = _read_bom_bytes(value)
        if not content:
            return
        metrics = bomlib.parse_bom_metrics(content)
        if not metrics:
            return

        if metrics.get('consumption') is not None:
            doc.consumption = metrics['consumption']
        if metrics.get('marker_efficiency') is not None:
            doc.marker_efficiency = metrics['marker_efficiency']

        _warn_gsm_mismatch(doc, metrics.get('gsm'))
    except Exception:
        frappe.log_error(frappe.get_traceback(), 'MoodboardStyle BOM extraction')


def _read_bom_bytes(value):
    ''' BOM bytes from a local Frappe File (/files/...) or, once offloaded, its S3 object. '''
    if _is_local_file(value):
        file_doc = _find_file(value)
        return file_doc.get_content() if file_doc else None
    key = cloud.asset_key(value)
    return cloud.download_object(key) if key else None


def _warn_gsm_mismatch(doc, excel_gsm):
    ''' Non-blocking warning when the style's own GSM disagrees with the BOM's. '''
    if not excel_gsm:
        return
    style_gsm = None
    for field in _STYLE_GSM_FIELDS:
        style_gsm = bomlib.gsm_from_text(doc.get(field))
        if style_gsm:
            break
    if style_gsm and abs(style_gsm - excel_gsm) > 0.01:
        frappe.msgprint(
            f'BOM GSM ({excel_gsm:g}) does not match the style GSM ({style_gsm:g}). '
            f'Consumption was computed from the BOM value.',
            title='GSM mismatch', indicator='orange',
        )


def _prev_value(doc, fieldname):
    ''' The pre-save value of a field ('' for a brand-new doc). '''
    before = doc.get_doc_before_save()
    return (before.get(fieldname) or '').strip() if before else ''


def _delete_s3_assets(doc):
    '''
    Delete this style's S3-offloaded assets (GLB, garment file, BOM, video) when
    the style is removed, so they don't orphan in the bucket. Scoped to the four
    _S3_ASSET_FIELDS only. Best-effort: S3 delete is idempotent, and a failure is
    logged rather than allowed to block the document deletion.
    '''
    for field in _S3_ASSET_FIELDS:
        key = cloud.asset_key(doc.get(field))
        if not key:
            continue
        try:
            cloud.delete_object(key)
        except Exception:
            frappe.log_error(frappe.get_traceback(), f'MoodboardStyle.on_trash S3 cleanup ({field})')

    # `garment_images` and `renders` are child tables, not single-value fields —
    # clean up the S3 object each row points at. The current render shares its
    # object with `image` above; S3 delete is idempotent, so deleting twice is fine.
    for field, rows in (('garment_images', doc.garment_images), ('renders', doc.renders)):
        for row in (rows or []):
            key = cloud.asset_key(row.image)
            if not key:
                continue
            try:
                cloud.delete_object(key)
            except Exception:
                frappe.log_error(frappe.get_traceback(), f'MoodboardStyle.on_trash S3 cleanup ({field})')


def _flag_board_unpublished(moodboard):
    '''
    Option C — a published board whose styles change has drifted from the brand
    snapshot, so flip it to "Unpublished Changes" to surface a "Publish changes"
    nudge in the editor. Covers every real edit path (the style APIs and Desk all
    go through doc.save / delete_doc, i.e. these hooks); the board hard-delete uses
    a raw frappe.db.delete that skips hooks, so this never fires on a dying board.

    Note this is complementary to the read-time media overlay in
    prism.api.moodboard_v2.get_moodboard: media (image/GLB) reaches brands live,
    but any other style change still needs a republish — which this flags.
    '''
    if not moodboard:
        return
    if frappe.db.get_value('Moodboard', moodboard, 'status') == 'Published':
        frappe.db.set_value('Moodboard', moodboard, 'status', 'Unpublished Changes')


def _offload_model_to_s3(doc):
    '''
    If `model_3d` holds a local Frappe file path (what a Desk "Attach" upload
    produces — /files/... or /private/files/...), stream that file to S3, store
    the S3 key on the field, and delete the now-redundant local File.

    This makes Desk-form attachments land in S3 just like the multipart
    upload_style_model API. Values already pointing at S3 (a bare key or the CDN
    URL) are left untouched.
    '''
    value = (doc.model_3d or '').strip()
    if not _is_local_file(value):
        return

    file_doc = _find_file(value)
    if not file_doc:
        return  # nothing on disk to move; leave the value as-is

    content = file_doc.get_content()  # bytes
    if len(content) > MAX_MODEL_FILE_SIZE_MB * 1024 * 1024:
        frappe.throw(f'Model file exceeds the maximum allowed limit of {MAX_MODEL_FILE_SIZE_MB} MB.')

    key = cloud.build_model_key(doc.moodboard, doc.name)
    cloud.upload_glb(io.BytesIO(content), key)

    doc.model_3d = cloud.asset_url(key)
    frappe.delete_doc('File', file_doc.name, ignore_permissions=True, force=True)


def _offload_document_to_s3(doc, fieldname, build_key, max_mb):
    '''
    Same Desk-attachment → S3 offload as _offload_model_to_s3, but for an
    arbitrary document field (garment file, BOM). Preserves the original file
    extension in the S3 key and infers the content type from it. Values already
    pointing at S3 (a bare key or the CDN URL) are left untouched.
    '''
    value = (doc.get(fieldname) or '').strip()
    if not _is_local_file(value):
        return

    file_doc = _find_file(value)
    if not file_doc:
        return  # nothing on disk to move; leave the value as-is

    content = file_doc.get_content()  # bytes
    if len(content) > max_mb * 1024 * 1024:
        label = doc.meta.get_label(fieldname)
        frappe.throw(f'{label} exceeds the maximum allowed limit of {max_mb} MB.')

    filename = file_doc.file_name or value
    key = build_key(doc.moodboard, doc.name, cloud.file_ext(filename))
    cloud.upload_file(io.BytesIO(content), key, cloud.content_type_for(filename))

    doc.set(fieldname, cloud.asset_url(key))
    frappe.delete_doc('File', file_doc.name, ignore_permissions=True, force=True)


def _offload_garment_images_to_s3(doc):
    '''
    Same Desk-attachment → S3 offload as _offload_model_to_s3, but for each row of
    the `garment_images` child table (a gallery). A row whose `image` is a local
    Frappe file (/files/... — what a Desk "Attach" upload produces) is streamed to
    S3, the row is rewritten to the S3 URL, and the local File is deleted. Rows
    already pointing at S3 (the API upload path, or a re-save) are left untouched.
    '''
    for row in (doc.garment_images or []):
        value = (row.image or '').strip()
        if not _is_local_file(value):
            continue

        file_doc = _find_file(value)
        if not file_doc:
            continue  # nothing on disk to move; leave the value as-is

        content = file_doc.get_content()  # bytes
        if len(content) > MAX_GARMENT_IMAGE_FILE_SIZE_MB * 1024 * 1024:
            frappe.throw(f'Garment image exceeds the maximum allowed limit of {MAX_GARMENT_IMAGE_FILE_SIZE_MB} MB.')

        filename = file_doc.file_name or value
        key = cloud.build_garment_image_key(doc.moodboard, doc.name, cloud.file_ext(filename))
        cloud.upload_file(io.BytesIO(content), key, cloud.content_type_for(filename))

        row.image = cloud.asset_url(key)
        frappe.delete_doc('File', file_doc.name, ignore_permissions=True, force=True)


def _offload_renders_to_s3(doc):
    '''
    Same Desk-attachment -> S3 offload as _offload_garment_images_to_s3, but for
    each row of the `renders` cache table. Renders arrive as base64 data URLs
    (staged to /files by the API) or as Desk attachments; either way the row is
    rewritten to its S3 URL and the local File deleted. Rows already on S3 — which
    is every row after its first save, and every row a re-render didn't touch —
    are skipped.
    '''
    for row in (doc.renders or []):
        value = (row.image or '').strip()
        if not _is_local_file(value):
            continue

        file_doc = _find_file(value)
        if not file_doc:
            continue  # nothing on disk to move; leave the value as-is

        content = file_doc.get_content()  # bytes
        if len(content) > MAX_STYLE_IMAGE_FILE_SIZE_MB * 1024 * 1024:
            frappe.throw(f'Render image exceeds the maximum allowed limit of {MAX_STYLE_IMAGE_FILE_SIZE_MB} MB.')

        filename = file_doc.file_name or value
        key = cloud.build_style_image_key(doc.moodboard, doc.name, cloud.file_ext(filename))
        cloud.upload_file(io.BytesIO(content), key, cloud.content_type_for(filename))

        url = cloud.asset_url(key)
        # The current render and `image` are the same picture — keep them the same
        # object rather than uploading the bytes twice.
        if (doc.image or '').strip() == value:
            doc.image = url
        row.image = url
        frappe.delete_doc('File', file_doc.name, ignore_permissions=True, force=True)


def _is_local_file(value):
    return bool(value) and (
        value.startswith('/files/') or value.startswith('/private/files/')
    )


def _find_file(file_url):
    name = frappe.db.get_value('File', {'file_url': file_url}, 'name')
    return frappe.get_doc('File', name) if name else None


def migrate_style_image(name):
    '''
    Move ONE style's per-garment crop `image` from a local /files path to S3,
    WITHOUT running the doc's save hooks — so it does not flip a published board to
    "Unpublished Changes", re-parse the BOM, or rebuild ESG. Brands still see the new
    image because get_moodboard overlays each style's live media onto the snapshot
    (_overlay_live_media), so only the live row needs updating.

    Idempotent: an image already on S3 (or absent) is a no-op. Returns the new S3
    URL, or None when nothing was migrated.
    '''
    value = (frappe.db.get_value('Moodboard Style', name, 'image') or '').strip()
    if not _is_local_file(value):
        return None
    file_doc = _find_file(value)
    if not file_doc:
        return None

    content = file_doc.get_content()
    if len(content) > MAX_STYLE_IMAGE_FILE_SIZE_MB * 1024 * 1024:
        frappe.throw(f'Style image exceeds the maximum allowed limit of {MAX_STYLE_IMAGE_FILE_SIZE_MB} MB.')

    moodboard = frappe.db.get_value('Moodboard Style', name, 'moodboard')
    filename = file_doc.file_name or value
    key = cloud.build_style_image_key(moodboard, name, cloud.file_ext(filename))
    cloud.upload_file(io.BytesIO(content), key, cloud.content_type_for(filename))

    url = cloud.asset_url(key)
    frappe.db.set_value('Moodboard Style', name, 'image', url, update_modified=False)
    frappe.delete_doc('File', file_doc.name, ignore_permissions=True, force=True)
    return url
