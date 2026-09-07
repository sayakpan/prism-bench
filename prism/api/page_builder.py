import base64
import io
import json
import mimetypes

import frappe

from prism.auth.authenticator import auth_required
import prism.api.util as util
import prism.lib.cloud as cloud
from prism.api import page_builder_diff
from prism.prism.doctype.page_builder_version.page_builder_version import record_version

MAX_IMAGE_FILE_SIZE_MB = 5


# --- write ---
@frappe.whitelist(allow_guest=True)
@auth_required
def create_draft(draft_json: dict, brand_id: str):
    return _upsert(draft_json, brand_id, is_new=True)

@frappe.whitelist(allow_guest=True)
@auth_required
def update_draft(brand_id: str, draft_json: dict):
    return _upsert(draft_json, brand_id, is_new=False)

@frappe.whitelist(allow_guest=True)
@auth_required
def publish(brand_id: str):
    ''' Publishes the Page Builder for a Brand. '''

    try:
        if not _user_has_valid_role():
            return {'success': False, 'error': 'Not authorized to publish Page Builder!'}

        if not brand_id:
            return {'success': False, 'error': 'Brand ID is required to publish Page Builder!'}

        page_builder_name = frappe.db.exists('Page Builder', {'brand_id': brand_id})
        if not page_builder_name:
            return {'success': False, 'error': 'Page Builder does not exist!'}

        # save in db
        doc = frappe.get_doc('Page Builder', page_builder_name)

        old_published = doc.published_layout_json
        new_published = doc.draft_layout_json

        doc.published_layout_json = doc.draft_layout_json
        doc.status = 'Published'
        doc.last_published_at = frappe.utils.now()

        doc.save(ignore_permissions=True)

        record_version(doc, old_published, new_published, action='Published', target='Published')

        frappe.db.commit()

        return {
            'success': True,
            'data': doc
        }

    except Exception as ex:
        frappe.db.rollback()
        frappe.log_error(frappe.get_traceback(), 'page_builder.publish()')
        return { 'success': False, 'error': str(ex) }

@frappe.whitelist(allow_guest=True)
@auth_required
def unpublish(brand_id: str):
    ''' Unpublishes the Page Builder for a Brand. '''

    try:
        if not _user_has_valid_role():
            return {'success': False, 'error': 'Not authorized to unpublish Page Builder!'}

        if not brand_id:
            return {'success': False, 'error': 'Brand ID is required to unpublish Page Builder!'}

        page_builder_name = frappe.db.exists('Page Builder', {'brand_id': brand_id})
        if not page_builder_name:
            return {'success': False, 'error': 'Page Builder does not exist!'}

        doc = frappe.get_doc('Page Builder', page_builder_name)
        doc.status = 'Draft'
        doc.save(ignore_permissions=True)

        record_version(doc, doc.published_layout_json, doc.published_layout_json,
                       action='Unpublished', target='Published')

        frappe.db.commit()

        return {'success': True}

    except Exception as ex:
        frappe.db.rollback()
        frappe.log_error(frappe.get_traceback(), 'page_builder.unpublish()')
        return {'success': False, 'error': str(ex)}

# --- read ---
@frappe.whitelist(allow_guest=True)
@auth_required
def get_draft(brand_id: str):
    ''' Returns the draft layout json of the brand.'''
    try:
        if not _user_has_valid_role():
            return {'success': False, 'error': 'Not authorized to get Page Builder!'}

        if not brand_id:
            return {'success': False, 'error': 'Brand ID is required!'}

        page_builder_name = frappe.db.exists('Page Builder', {'brand_id': brand_id})
        if not page_builder_name:
            return {'success': False, 'error': 'Page Builder does not exist!'}

        # fetch from db
        doc = frappe.get_doc('Page Builder', page_builder_name)
        if not doc.draft_layout_json:
            doc.draft_layout_json = doc.published_layout_json

        return {
            'success': True,
            'data': frappe.parse_json(doc.draft_layout_json)
        }

    except Exception as ex:
        frappe.log_error(frappe.get_traceback(), 'page_builder.get_draft()')
        return { 'success': False, 'error': str(ex) }

@frappe.whitelist(allow_guest=True)
@auth_required
def get_published_page(brand_id: str):
    ''' Returns the published layout json of a brand. '''
    try:
        if not _user_has_valid_role():
            return {'success': False, 'error': 'Not authorized to get Page Builder!'}

        if not brand_id:
            return {'success': False, 'error': 'Brand ID is required!'}

        page_builder_name = frappe.db.exists('Page Builder', {
            'brand_id': brand_id,
            'is_active': 1,
            'status': 'Published'
        })
        if not page_builder_name:
            return {'success': False, 'error': 'Published Page Builder does not exist!'}

        doc = frappe.get_doc('Page Builder', page_builder_name)

        return {
            'success': True,
            'data': frappe.parse_json(doc.published_layout_json)
        }

    except Exception as ex:
        frappe.log_error(frappe.get_traceback(), 'page_builder.get_published_page()')
        return { 'success': False, 'error': str(ex) }


# --- history / versioning ---
@frappe.whitelist(allow_guest=True)
@auth_required
def get_history(brand_id: str, target: str = None, limit: int = 20, offset: int = 0):
    ''' Returns the change-log timeline for a brand's page, newest first. '''
    try:
        if not _user_has_valid_role():
            return {'success': False, 'error': 'Not authorized to view history!'}

        page_builder_name = frappe.db.exists('Page Builder', {'brand_id': brand_id})
        if not page_builder_name:
            return {'success': False, 'error': 'Page Builder does not exist!'}

        filters = {'page_builder': page_builder_name}
        if target:
            filters['target'] = target

        versions = frappe.get_all(
            'Page Builder Version',
            filters=filters,
            fields=['name', 'actor', 'action', 'target', 'change_count',
                    'summary', 'creation', 'parent_version'],
            order_by='creation desc',
            limit_page_length=int(limit),
            limit_start=int(offset),
        )

        for v in versions:
            v['actor'] = _actor(v.get('actor'))

        return {'success': True, 'data': versions}

    except Exception as ex:
        frappe.log_error(frappe.get_traceback(), 'page_builder.get_history()')
        return {'success': False, 'error': str(ex)}

@frappe.whitelist(allow_guest=True)
@auth_required
def get_version(version_name: str, include_snapshot: int = 0):
    ''' Returns one version: header + block-level change rows. '''
    try:
        if not _user_has_valid_role():
            return {'success': False, 'error': 'Not authorized to view version!'}

        if not frappe.db.exists('Page Builder Version', version_name):
            return {'success': False, 'error': 'Version does not exist!'}

        doc = frappe.get_doc('Page Builder Version', version_name)

        data = {
            'name': doc.name,
            'page_builder': doc.page_builder,
            'brand_id': doc.brand_id,
            'target': doc.target,
            'action': doc.action,
            'actor': _actor(doc.actor),
            'change_count': doc.change_count,
            'summary': doc.summary,
            'creation': doc.creation,
            'parent_version': doc.parent_version,
            'changes': [
                {
                    'block_id': c.block_id,
                    'block_type': c.block_type,
                    'block_path': c.block_path,
                    'change_kind': c.change_kind,
                    'field': c.field,
                    'old_value': c.old_value,
                    'new_value': c.new_value,
                }
                for c in doc.changes
            ],
        }

        if int(include_snapshot):
            data['layout_json'] = frappe.parse_json(doc.layout_json)

        return {'success': True, 'data': data}

    except Exception as ex:
        frappe.log_error(frappe.get_traceback(), 'page_builder.get_version()')
        return {'success': False, 'error': str(ex)}

@frappe.whitelist(allow_guest=True)
@auth_required
def diff_versions(brand_id: str, from_version: str, to_version: str):
    ''' Computes the block-level diff between any two versions of a brand's page. '''
    try:
        if not _user_has_valid_role():
            return {'success': False, 'error': 'Not authorized to diff versions!'}

        page_builder_name = frappe.db.exists('Page Builder', {'brand_id': brand_id})
        if not page_builder_name:
            return {'success': False, 'error': 'Page Builder does not exist!'}

        from_doc = frappe.get_doc('Page Builder Version', from_version)
        to_doc = frappe.get_doc('Page Builder Version', to_version)

        if page_builder_name not in (from_doc.page_builder, to_doc.page_builder) \
                or from_doc.page_builder != to_doc.page_builder:
            return {'success': False, 'error': 'Versions do not belong to this page!'}

        diff = page_builder_diff.diff_layouts(
            frappe.parse_json(from_doc.layout_json),
            frappe.parse_json(to_doc.layout_json),
        )

        return {'success': True, 'data': diff}

    except Exception as ex:
        frappe.log_error(frappe.get_traceback(), 'page_builder.diff_versions()')
        return {'success': False, 'error': str(ex)}

@frappe.whitelist(allow_guest=True)
@auth_required
def restore_version(brand_id: str, version_name: str):
    ''' Restores a version's full layout into the draft (whole page). '''
    try:
        if not _user_has_valid_role():
            return {'success': False, 'error': 'Not authorized to restore!'}

        page_builder_name = frappe.db.exists('Page Builder', {'brand_id': brand_id})
        if not page_builder_name:
            return {'success': False, 'error': 'Page Builder does not exist!'}

        version = frappe.get_doc('Page Builder Version', version_name)
        if version.page_builder != page_builder_name:
            return {'success': False, 'error': 'Version does not belong to this page!'}

        doc = frappe.get_doc('Page Builder', page_builder_name)
        old_draft = doc.draft_layout_json

        doc.draft_layout_json = version.layout_json
        doc.status = 'Draft'
        doc.save(ignore_permissions=True)

        record_version(doc, old_draft, doc.draft_layout_json,
                       action='Restored', target='Draft')

        frappe.db.commit()

        return {
            'success': True,
            'data': {
                'brand_id': brand_id,
                'restored_from': version_name,
                'actor': _actor(frappe.session.user),
                'draft_json': frappe.parse_json(doc.draft_layout_json),
            },
        }

    except Exception as ex:
        frappe.db.rollback()
        frappe.log_error(frappe.get_traceback(), 'page_builder.restore_version()')
        return {'success': False, 'error': str(ex)}


# --- maintenance (Desk) ---
@frappe.whitelist(methods=['POST'])
def migrate_images_to_s3(name=None):
    '''
    Move a single Page Builder's images from local Frappe /files storage to S3 and
    reclaim the disk, driven by the Desk form button.

    Rewrites EVERY stored copy of the page — draft, published, and every Page
    Builder Version snapshot — so each local /files image is uploaded once (deduped
    across all copies) and every reference points at the servable S3 URL. Restore
    keeps working: a restored version now yields S3 URLs.

    Only after all rewrites are committed are the local File docs deleted, and only
    for images no longer referenced anywhere (a defensive cross-page check guards
    the near-impossible shared-file case). Ordering is deliberate — rewrite + commit
    first, delete second — so a crash can only ever orphan a local file on disk,
    never leave a stored reference pointing at a deleted one.

    Idempotent: images already on S3 (or any foreign URL) are skipped, and a
    per-image upload failure leaves that local ref intact (and its file undeleted)
    rather than breaking the page. Session-authed + System Manager only, mirroring
    moodboard_v2.regenerate_moodboard_thumbnail.
    '''
    frappe.only_for('System Manager')

    if not name or not frappe.db.exists('Page Builder', name):
        frappe.throw(f'Page Builder "{name}" not found!')

    try:
        return {'success': True, 'data': _migrate_one(name)}
    except Exception as ex:
        frappe.db.rollback()
        frappe.log_error(frappe.get_traceback(), 'page_builder.migrate_images_to_s3()')
        return {'success': False, 'error': str(ex)}

@frappe.whitelist(methods=['POST'])
def bulk_migrate_images_to_s3(names=None):
    '''
    Bulk variant of migrate_images_to_s3, driven by the list-view "Migrate Images
    to S3" action over the selected pages. Runs the exact same per-page migration
    (draft + published + snapshots -> S3, then delete now-unreferenced local files),
    one page at a time, and returns aggregate + per-page results.

    Per-page isolation: each page commits on its own and a failure on one page is
    logged and skipped without aborting the rest. Shared local files are handled
    correctly across the batch — the cross-reference guard keeps a file until the
    last page referencing it has been migrated. Session-authed + System Manager only.
    '''
    frappe.only_for('System Manager')

    names = frappe.parse_json(names) if isinstance(names, str) else names
    if not names:
        return {'success': False, 'error': 'No pages selected!'}

    totals = {'migrated': 0, 'failed': 0, 'missing': 0, 'snapshots': 0, 'deleted': 0}
    results = []
    pages_ok = 0
    pages_failed = 0

    for name in names:
        if not frappe.db.exists('Page Builder', name):
            results.append({'name': name, 'ok': False, 'error': 'Page Builder not found'})
            pages_failed += 1
            continue
        try:
            stats = _migrate_one(name)
            for k in totals:
                totals[k] += stats.get(k, 0)
            results.append({'name': name, 'ok': True, 'stats': stats})
            pages_ok += 1
        except Exception as ex:
            frappe.db.rollback()
            frappe.log_error(frappe.get_traceback(),
                             f'page_builder.bulk_migrate_images_to_s3({name})')
            results.append({'name': name, 'ok': False, 'error': str(ex)})
            pages_failed += 1

    return {
        'success': True,
        'data': {'pages_ok': pages_ok, 'pages_failed': pages_failed,
                 'totals': totals, 'results': results},
    }

def _migrate_one(name):
    '''
    Migrate one Page Builder's images to S3 and reclaim disk — the shared core of
    the single-page and bulk endpoints. Rewrites draft + published + every version
    snapshot, commits, then deletes the now-unreferenced local files. Raises on a
    hard error; the calling endpoint owns rollback/logging. Returns the stats dict.
    '''
    doc = frappe.get_doc('Page Builder', name)
    brand_id = doc.brand_id
    stats = {'migrated': 0, 'failed': 0, 'missing': 0, 'snapshots': 0, 'deleted': 0}
    url_map = {}

    # 1. draft + published on the Page Builder doc
    changed = False
    for field in ('draft_layout_json', 'published_layout_json'):
        new_raw = _rewrite_blob(doc.get(field), brand_id, stats, url_map)
        if new_raw is not None:
            doc.set(field, new_raw)
            changed = True
    if changed:
        doc.save(ignore_permissions=True)

    # 2. every version snapshot for this page
    version_names = frappe.get_all(
        'Page Builder Version', filters={'page_builder': name}, pluck='name'
    )
    for vname in version_names:
        new_raw = _rewrite_blob(
            frappe.db.get_value('Page Builder Version', vname, 'layout_json'),
            brand_id, stats, url_map,
        )
        if new_raw is not None:
            frappe.db.set_value('Page Builder Version', vname, 'layout_json',
                                new_raw, update_modified=False)
            stats['snapshots'] += 1

    # Persist ALL rewrites before deleting anything, so no stored reference can
    # ever point at a file we're about to remove.
    frappe.db.commit()

    # 3. now that nothing on this page references them, delete the local files
    stats['deleted'] = _delete_migrated_locals(url_map)
    frappe.db.commit()

    return stats


# --- helpers ---
def _actor(email):
    ''' Resolves a user email into an {name, email} object for API responses. '''
    if not email:
        return {'name': '', 'email': ''}
    full_name = frappe.db.get_value('User', email, 'full_name')
    return {'name': full_name or email, 'email': email}

def _user_has_valid_role():
    #return util.user_has_roles(['Template Manager', 'Visualizer'])
    #return not util.user_has_roles(['Buyer'])
    return True

def _upsert(draft_json: dict, brand_id: str, is_new: bool):
    try:
        if not _user_has_valid_role():
            return {'success': False, 'error': 'Not authorized to add/update Page Builder!'}

        if not brand_id:
            return {'success': False, 'error': 'Brand ID is required!'}

        existing_name = frappe.db.exists('Page Builder', {'brand_id': brand_id})

        # initiate doc object
        if is_new:
            if existing_name:
                return {'success': False, 'error': 'Page Builder already exists for this Brand!'}
            doc = frappe.new_doc('Page Builder')
            doc.brand_id = brand_id
            old_draft = None
        else:
            if not existing_name:
                return {'success': False, 'error': 'Page Builder does not exist!'}
            doc = frappe.get_doc('Page Builder', existing_name)
            old_draft = doc.draft_layout_json

        # save images (replace base64 "IMAGE_*" attributes with S3 URLs)
        _process_images(draft_json, brand_id)

        # save in db
        if draft_json:
            doc.draft_layout_json = json.dumps(draft_json)

        if is_new:
            doc.insert(ignore_permissions=True)
        else:
            doc.save(ignore_permissions=True)

        record_version(
            doc, old_draft, doc.draft_layout_json,
            action='Draft Created' if is_new else 'Draft Updated',
            target='Draft',
        )

        frappe.db.commit()

        return {
            'success': True,
            'data': {
                'id': doc.name,
                'brand_id': doc.brand_id,
                'draft_json': draft_json
            }
        }

    except Exception as ex:
        frappe.db.rollback()
        frappe.log_error(frappe.get_traceback(), 'page_builder._upsert()')
        return { 'success': False, 'error': str(ex) }

def _process_images(obj, brand_id):
    '''
    Recursively find "IMAGE_*" attributes carrying inline base64 and offload them
    to S3, replacing the value with the servable S3 URL.

    Only fresh base64 payloads (length > 100) are touched. Values already stored
    as URLs — legacy local `/files/...` paths and prior S3 URLs alike — are short
    and left exactly as-is, so existing pages keep serving their images unchanged.
    Nothing is ever deleted here: a reverted/removed block's old image stays in the
    bucket so it can be restored later.
    '''
    if isinstance(obj, dict):
        for key, value in obj.items():
            if key.startswith('IMAGE_') and isinstance(value, str) and value:
                if len(value) > 100:
                    obj[key] = _save_image(value, brand_id)
            else:
                _process_images(value, brand_id)
    elif isinstance(obj, list):
        for item in obj:
            _process_images(item, brand_id)
    else:
        pass

    return

def _rewrite_blob(raw, brand_id, stats, url_map):
    '''
    Migrate every local image inside one stored layout JSON string. Returns the new
    JSON string when it changed, or None (nothing to write) when the blob is empty,
    unparseable, or already fully on S3 — so callers only write on a real change.
    '''
    if not raw:
        return None
    parsed = frappe.parse_json(raw)
    if parsed is None:
        return None
    _migrate_local_images(parsed, brand_id, stats, url_map)
    new_raw = json.dumps(parsed)
    return new_raw if new_raw != raw else None

def _delete_migrated_locals(url_map):
    '''
    Delete the local File docs for images that were successfully migrated this run
    (url_map maps local_url -> new S3 url; failed/missing map to themselves). A
    file is removed only if no OTHER page or version snapshot still references it —
    the guard against the near-impossible case of a /files URL shared across pages.
    Returns the number of local files actually deleted.
    '''
    deleted = 0
    migrated_locals = [local for local, new in url_map.items() if new != local]
    for local_url in migrated_locals:
        if _still_referenced_elsewhere(local_url):
            continue
        file_name = frappe.db.get_value('File', {'file_url': local_url}, 'name')
        if not file_name:
            continue
        try:
            frappe.delete_doc('File', file_name, ignore_permissions=True, force=True)
            deleted += 1
        except Exception:
            frappe.log_error(frappe.get_traceback(), 'page_builder._delete_migrated_locals()')
    return deleted

def _still_referenced_elsewhere(local_url):
    '''
    True if any Page Builder draft/published or any version snapshot still contains
    this local URL. This page's own copies are already rewritten + committed by the
    time we get here, so a hit means a genuine external reference — keep the file.
    '''
    like = '%' + local_url + '%'
    if frappe.db.sql(
        "SELECT 1 FROM `tabPage Builder` "
        "WHERE draft_layout_json LIKE %s OR published_layout_json LIKE %s LIMIT 1",
        (like, like),
    ):
        return True
    if frappe.db.sql(
        "SELECT 1 FROM `tabPage Builder Version` WHERE layout_json LIKE %s LIMIT 1",
        (like,),
    ):
        return True
    return False

def _migrate_local_images(obj, brand_id, stats, url_map):
    '''
    Recursively rewrite every local /files image reference anywhere in a stored
    layout to an S3 URL. Unlike _process_images (which handles fresh base64 under
    IMAGE_* keys on write), this handles already-stored local URLs under ANY key —
    it's the backfill for pages saved before S3 routing existed.
    '''
    if isinstance(obj, dict):
        for key, value in obj.items():
            if isinstance(value, str):
                obj[key] = _migrate_local_ref(value, brand_id, stats, url_map)
            else:
                _migrate_local_images(value, brand_id, stats, url_map)
    elif isinstance(obj, list):
        for i, item in enumerate(obj):
            if isinstance(item, str):
                obj[i] = _migrate_local_ref(item, brand_id, stats, url_map)
            else:
                _migrate_local_images(item, brand_id, stats, url_map)

    return obj

def _migrate_local_ref(value, brand_id, stats, url_map):
    '''
    Offload one local /files image to S3 and return its S3 URL. Non-local values
    (S3, foreign URLs, plain text) pass straight through. The local File is NOT
    deleted, so version snapshots pointing at it stay restorable.
    '''
    if not (value.startswith('/files/') or value.startswith('/private/files/')):
        return value  # already S3 / foreign URL / not a file path

    # Resolve each distinct local URL exactly once per run: the cache holds the
    # outcome (new S3 URL, or the original on miss/failure), so a repeated image
    # is neither re-uploaded nor re-counted, and draft + published stay consistent.
    if value in url_map:
        return url_map[value]

    file_name = frappe.db.get_value('File', {'file_url': value}, 'name')
    if not file_name:
        stats['missing'] += 1
        url_map[value] = value  # dangling ref, no File doc — leave as-is
        return value

    try:
        file_doc = frappe.get_doc('File', file_name)
        content = file_doc.get_content()
        if isinstance(content, str):
            content = content.encode('utf-8', 'surrogateescape')
        filename = file_doc.file_name or value
        key = cloud.build_page_builder_image_key(brand_id, cloud.file_ext(filename))
        cloud.upload_file(io.BytesIO(content), key, cloud.content_type_for(filename))
        new_url = cloud.asset_url(key)
        url_map[value] = new_url
        stats['migrated'] += 1
        return new_url
    except Exception:
        frappe.log_error(frappe.get_traceback(), 'page_builder._migrate_local_ref()')
        stats['failed'] += 1
        url_map[value] = value  # keep the working local ref on failure — nothing breaks
        return value

def _save_image(base64_content, brand_id):
    '''
    Decode an inline base64 image and store it on S3, returning the servable URL.

    If S3 is unreachable/misconfigured, fall back to the previous local-file
    behaviour (util.save_file) rather than break the draft save — "new images go
    to S3" must never regress into "publishing is broken". The fallback is logged
    so a persistent S3 problem is visible. Oversize images fail outright (before
    either store) — that's a real error, not an infra hiccup.
    '''
    content, extension = _decode_image(base64_content)

    try:
        key = cloud.build_page_builder_image_key(brand_id, f'.{extension}')
        cloud.upload_file(io.BytesIO(content), key, cloud.content_type_for(f'img.{extension}'))
        return cloud.asset_url(key)
    except Exception:
        frappe.log_error(frappe.get_traceback(),
                         'page_builder._save_image S3 upload failed; using local fallback')
        file_doc = frappe.get_doc({
            'doctype': 'File',
            'file_name': f'pb_{frappe.generate_hash()[:8]}.{extension}',
            'content': content,
            'is_private': 0,
        })
        file_doc.save(ignore_permissions=True)
        return file_doc.file_url

def _decode_image(base64_content):
    '''
    Decode a (data-URI or bare) base64 image to (bytes, extension), enforcing the
    size cap. Mirrors util.save_file's decoding so behaviour is identical bar the
    storage target.
    '''
    extension = 'png'  # default fallback extension
    content = base64_content

    if ',' in content:
        header, content = content.split(',', 1)
        # header format: data:image/png;base64
        if ':' in header and ';' in header:
            mime_type = header.split(':')[1].split(';')[0]
            ext = mimetypes.guess_extension(mime_type)
            if ext:
                extension = ext.lstrip('.')

    file_bytes = base64.b64decode(content)
    if len(file_bytes) > MAX_IMAGE_FILE_SIZE_MB * 1024 * 1024:
        raise ValueError(f'Image exceeds the maximum allowed limit of {MAX_IMAGE_FILE_SIZE_MB} MB.')

    return file_bytes, extension
