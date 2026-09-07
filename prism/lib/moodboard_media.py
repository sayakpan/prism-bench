'''
Moodboard media offload to S3 + compressed thumbnails.

Every moodboard image is staged locally first (util.save_file writes a public
/files Frappe File — the AI images run 4-7 MB each), which made list pages heavy
and kept large blobs on the app server. This module moves them to S3 (same bucket
as the rest of prism, via prism.lib.cloud) and derives an aspect-preserved WebP
thumbnail for the board card.

It mirrors prism/prism/doctype/moodboard_style/moodboard_style.py, which already
does the same Desk-attachment -> S3 offload for a style's assets. The offload is
driven from the Moodboard / Moodboard Version / Moodboard Message controllers'
before_save hooks, so every write path is covered, and it is idempotent: a value
already on S3 (or empty) is left untouched, so re-saving / re-running is safe.

Scope (per the product decision): version `image` + `edited_image`, the board
`thumbnail`, and every image embedded (IMAGE_* convention) in `canvas_state`,
message `attachments`, the garment `cleaned_front/back_image` fields, the
uploaded Customer Inspiration / Print Direction images, and the recoloured
surplus swatches.

Out of scope: the `garment_inspiration` JSON field's `imageUrl` entries, which are
external URLs stored verbatim rather than an upload.
'''

import io

import frappe

import prism.lib.cloud as cloud
import prism.api.util as util

MAX_IMAGE_FILE_SIZE_MB = 30      # matches moodboard_v2.MAX_IMAGE_FILE_SIZE_MB
MAX_BRIEF_FILE_SIZE_MB = 20      # matches moodboard_v2.MAX_BRIEF_FILE_SIZE_MB
# Default card thumbnail produced automatically on every write and migration.
# 1024px longest edge (512 went soft when the card renders larger / on 2x displays)
# at WebP q88 — still a big drop from the 4-7 MB original.
THUMBNAIL_MAX_DIM = 1024
THUMBNAIL_QUALITY = 88
# High-quality thumbnail — larger + LANCZOS + unsharp (see util). Produced ONLY on
# demand by the "Upgrade Thumbnail Quality" action, not automatically.
THUMBNAIL_HQ_MAX_DIM = 1280
THUMBNAIL_HQ_QUALITY = 90


# =====================================================================
# Low-level helpers
# =====================================================================

def _is_local_file(value):
    ''' A staged, not-yet-offloaded local Frappe file path. '''
    return isinstance(value, str) and (
        value.startswith('/files/') or value.startswith('/private/files/')
    )


def _find_file(file_url):
    name = frappe.db.get_value('File', {'file_url': file_url}, 'name')
    return frappe.get_doc('File', name) if name else None


def _is_s3_thumbnail(value):
    ''' True when the thumbnail is already a migrated, compressed S3 .webp — i.e.
        an S3/CDN URL (not a local /files path) ending in .webp. Used to make
        migration re-runs skip thumbnail regeneration. '''
    if not value or _is_local_file(value):
        return False
    return value.split('?')[0].lower().endswith('.webp')


def _read_bytes(value):
    ''' Bytes for a stored image value, whether a local Frappe file or an S3 object. '''
    if not value:
        return None
    if _is_local_file(value):
        fd = _find_file(value)
        return fd.get_content() if fd else None
    key = cloud.asset_key(value)
    return cloud.download_object(key) if key else None


def offload_value(value, build_key, max_mb=MAX_IMAGE_FILE_SIZE_MB, url_map=None):
    '''
    If `value` is a staged local Frappe file, stream it to S3, delete the local
    File, and return the servable S3 URL. Values already on S3 (or empty) are
    returned unchanged, so this is idempotent.

    build_key(ext) -> S3 key (a closure over the board name). When `url_map` is
    given, the old local url -> new S3 url mapping is recorded there (used by the
    migration to rewrite frozen snapshot paths after the local file is gone).
    '''
    if not _is_local_file(value):
        return value
    fd = _find_file(value)
    if not fd:
        return value  # nothing on disk to move; leave the value as-is

    content = fd.get_content()  # bytes
    if len(content) > max_mb * 1024 * 1024:
        frappe.throw(f'Image exceeds the maximum allowed limit of {max_mb} MB.')

    filename = fd.file_name or value
    key = build_key(cloud.file_ext(filename))
    cloud.upload_file(io.BytesIO(content), key, cloud.content_type_for(filename))
    frappe.delete_doc('File', fd.name, ignore_permissions=True, force=True)

    new_url = cloud.asset_url(key)
    if url_map is not None:
        url_map[value] = new_url
    return new_url


def offload_embedded(obj, build_key, url_map=None):
    '''
    Recursively rewrite any IMAGE_* string that is a staged local file to its S3
    URL — the convention used inside canvas_state overlays and message
    attachments. base64 `data:` values are NOT touched here; those are handled at
    write time by moodboard_v2._process_images before this runs.
    '''
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(v, str) and k.startswith('IMAGE_') and _is_local_file(v):
                obj[k] = offload_value(v, build_key, url_map=url_map)
            else:
                offload_embedded(v, build_key, url_map=url_map)
    elif isinstance(obj, list):
        for item in obj:
            offload_embedded(item, build_key, url_map=url_map)
    return obj


def offload_json_field(doc, fieldname, build_key, url_map=None):
    '''
    Offload embedded local images inside a JSON field (canvas_state / attachments),
    re-serializing only when the field actually references a local /files path so
    unrelated saves stay cheap.
    '''
    raw = doc.get(fieldname)
    if not raw:
        return
    probe = raw if isinstance(raw, str) else frappe.as_json(raw)
    if '/files/' not in probe:
        return  # nothing staged locally; skip parse + reserialize
    parsed = frappe.parse_json(raw)
    if parsed is None:
        return
    offload_embedded(parsed, build_key, url_map=url_map)
    doc.set(fieldname, frappe.as_json(parsed))


# =====================================================================
# Thumbnails
# =====================================================================

def make_and_store_thumbnail(moodboard, source_value, high_quality=False):
    '''
    Build an aspect-preserved, compressed WebP thumbnail from a source image
    (local file or S3 URL) and store it in S3, returning its servable URL.

    high_quality=False (default) is the light thumbnail written automatically on
    every write/migration; high_quality=True is the larger, sharpened thumbnail
    produced on demand by the "Upgrade Thumbnail Quality" action.

    Best-effort: on any failure it returns the source value unchanged (so the
    board still has a usable — if large — image rather than none), and logs.
    '''
    if not source_value:
        return source_value
    try:
        raw = _read_bytes(source_value)
        if not raw:
            return source_value
        max_dim = THUMBNAIL_HQ_MAX_DIM if high_quality else THUMBNAIL_MAX_DIM
        quality = THUMBNAIL_HQ_QUALITY if high_quality else THUMBNAIL_QUALITY
        thumb = util.make_image_thumbnail_fit(raw, max_dim, quality, sharpen=high_quality)
        if not thumb:
            return source_value
        key = cloud.build_moodboard_thumbnail_key(moodboard, '.webp')
        cloud.upload_file(io.BytesIO(thumb), key, content_type='image/webp')
        return cloud.asset_url(key)
    except Exception:
        frappe.log_error(frappe.get_traceback(), 'moodboard_media.make_and_store_thumbnail')
        return source_value


def regenerate_thumbnail(moodboard, high_quality=True):
    '''
    Rebuild a board's WebP thumbnail from its primary version image (or the newest
    version's image if none is primary) and store it. Defaults to the high-quality
    (sharpened) thumbnail — this backs the "Upgrade Thumbnail Quality" action. Uses
    db.set_value so it only touches the thumbnail (no full board before_save).
    Returns the new thumbnail URL, or None if there's no version image to build from.
    '''
    src = None
    pv = frappe.db.get_value('Moodboard', moodboard, 'primary_version')
    if pv:
        src = (frappe.db.get_value('Moodboard Version', pv, 'edited_image')
               or frappe.db.get_value('Moodboard Version', pv, 'image'))
    if not src:
        rows = frappe.get_all(
            'Moodboard Version', filters={'moodboard': moodboard},
            fields=['edited_image', 'image'], order_by='creation desc', limit=1)
        if rows:
            src = rows[0].get('edited_image') or rows[0].get('image')
    if not src:
        return None
    url = make_and_store_thumbnail(moodboard, src, high_quality=high_quality)
    frappe.db.set_value('Moodboard', moodboard, 'thumbnail', url)
    return url


# =====================================================================
# Doc-level offload (called from controller before_save hooks)
# =====================================================================

def offload_version(doc, url_map=None):
    '''
    Offload a Moodboard Version's image, edited_image, canvas_state images, and
    the `reference_images` gallery child table.
    '''
    mb = doc.moodboard
    img_key = lambda ext: cloud.build_moodboard_version_image_key(mb, ext)
    emb_key = lambda ext: cloud.build_moodboard_embedded_image_key(mb, ext)
    if _is_local_file(doc.get('image')):
        doc.image = offload_value(doc.image, img_key, url_map=url_map)
    if _is_local_file(doc.get('edited_image')):
        doc.edited_image = offload_value(doc.edited_image, img_key, url_map=url_map)
    offload_json_field(doc, 'canvas_state', emb_key, url_map=url_map)
    _offload_version_gallery(doc, url_map=url_map)


def _offload_version_gallery(doc, url_map=None):
    '''
    Same Desk-attachment -> S3 offload as offload_value, but for each row of the
    `reference_images` child table (a gallery). A row whose `image` is a local
    Frappe file (/files/... -- what a Desk "Attach" upload produces) is streamed
    to S3, the row is rewritten to the S3 URL, and the local File is deleted. Rows
    already on S3 (the API upload path, or a re-save) are left untouched.
    '''
    gallery_key = lambda ext: cloud.build_moodboard_version_gallery_image_key(doc.moodboard, ext)
    for row in (doc.get('reference_images') or []):
        if _is_local_file(row.get('image')):
            row.image = offload_value(row.image, gallery_key, url_map=url_map)


def offload_message(doc, url_map=None):
    ''' Offload images embedded in a Moodboard Message's attachments JSON. '''
    emb_key = lambda ext: cloud.build_moodboard_embedded_image_key(doc.moodboard, ext)
    offload_json_field(doc, 'attachments', emb_key, url_map=url_map)


def offload_board(doc, url_map=None):
    '''
    Offload the board's child-row files: garment cleaned front/back shots, the
    uploaded Customer Inspiration / Print Direction / Inspiration Images, the
    recoloured surplus swatches, and the Customer Brief attachments (documents,
    own folder and cap).

    garment_inspiration is deliberately absent — it's a JSON field (not a child
    table) holding external imageUrls verbatim, and _is_local_file() would skip
    it anyway.
    '''
    emb_key = lambda ext: cloud.build_moodboard_embedded_image_key(doc.name, ext)
    for row in (doc.get('garments') or []):
        if _is_local_file(row.get('cleaned_front_image')):
            row.cleaned_front_image = offload_value(row.cleaned_front_image, emb_key, url_map=url_map)
        if _is_local_file(row.get('cleaned_back_image')):
            row.cleaned_back_image = offload_value(row.cleaned_back_image, emb_key, url_map=url_map)
    for field in ('print_direction', 'inspiration_images', 'surplus_recolours'):
        for row in (doc.get(field) or []):
            if _is_local_file(row.get('image')):
                row.image = offload_value(row.image, emb_key, url_map=url_map)

    # Customer brief attachments are documents as well as images, so they get their
    # own S3 folder and the smaller 20 MB cap enforced at upload.
    brief_key = lambda ext: cloud.build_moodboard_brief_file_key(doc.name, ext)
    for row in (doc.get('customer_brief') or []):
        if _is_local_file(row.get('file')):
            row.file = offload_value(row.file, brief_key,
                                     max_mb=MAX_BRIEF_FILE_SIZE_MB, url_map=url_map)


# =====================================================================
# Migration (backfill existing local images to S3)
# =====================================================================

def migrate_moodboard(name):
    '''
    Backfill one board's locally-stored images to S3, in place and idempotently:
      - every Moodboard Version's image / edited_image / canvas_state images,
      - every Moodboard Message's attachment images,
      - the board's garment cleaned front/back images,
      - the frozen published_snapshot (its primaryVersionImage etc. are rewritten
        from the old->new url map, since the local files are gone by then),
      - the board thumbnail, built as a compressed WebP from the primary version
        image ONLY when it's still missing / a local file (an existing S3 .webp
        thumbnail is left as-is).

    Values already on S3 are skipped and the board is saved only when something
    actually changed, so a re-run over an already-migrated board is a near no-op.
    Returns a small per-board summary. Commits at the end.
    '''
    summary = {'moodboard': name, 'versions': 0, 'messages': 0, 'thumbnail': False}
    url_map = {}

    # 1. Versions
    for vname in frappe.get_all('Moodboard Version', filters={'moodboard': name}, pluck='name'):
        vd = frappe.get_doc('Moodboard Version', vname)
        before = (vd.get('image'), vd.get('edited_image'), vd.get('canvas_state'))
        offload_version(vd, url_map=url_map)
        if (vd.get('image'), vd.get('edited_image'), vd.get('canvas_state')) != before:
            vd.save(ignore_permissions=True)
            summary['versions'] += 1

    # 2. Messages
    for mname in frappe.get_all('Moodboard Message', filters={'moodboard': name}, pluck='name'):
        md = frappe.get_doc('Moodboard Message', mname)
        before = md.get('attachments')
        offload_message(md, url_map=url_map)
        if md.get('attachments') != before:
            md.save(ignore_permissions=True)
            summary['messages'] += 1

    # 3. Board: garment cleaned + inspiration images, snapshot rewrite, thumbnail
    doc = frappe.get_doc('Moodboard', name)
    dirty = False

    map_before = len(url_map)
    offload_board(doc, url_map=url_map)
    if len(url_map) > map_before:
        dirty = True

    # Rewrite the frozen snapshot by string-substituting the moved urls. The local
    # files are already deleted, so we cannot re-read them — the map is the source
    # of truth. Only touches urls this migration actually moved.
    snap = doc.get('published_snapshot')
    if snap and url_map:
        s = snap if isinstance(snap, str) else frappe.as_json(snap)
        new_s = s
        for old, new in url_map.items():
            if old in new_s:
                new_s = new_s.replace(old, new)
        if new_s != s:
            doc.published_snapshot = new_s
            dirty = True

    # Thumbnail: only (re)generate when it's missing or still a local file. Skip when
    # it's already a compressed S3 .webp, so re-running migration is a no-op here.
    thumb = doc.get('thumbnail')
    if not _is_s3_thumbnail(thumb):
        if doc.get('primary_version'):
            src = (frappe.db.get_value('Moodboard Version', doc.primary_version, 'edited_image')
                   or frappe.db.get_value('Moodboard Version', doc.primary_version, 'image'))
            if src:
                doc.thumbnail = make_and_store_thumbnail(name, src)
                summary['thumbnail'] = True
                dirty = True
        elif thumb in url_map:
            # No primary version, but the thumbnail pointed at a moved file — keep it valid.
            doc.thumbnail = url_map[thumb]
            dirty = True

    if dirty:
        doc.save(ignore_permissions=True)
    frappe.db.commit()
    return summary
