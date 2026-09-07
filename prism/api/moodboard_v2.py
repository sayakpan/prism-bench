'''
Moodboard v2 — structured endpoints over the normalized Moodboard model.

Replaces the monolithic prism.api.moodboard.{create,update,get}_draft blob flow.
The old endpoints stay intact for the transition; the frontend moves here per
subsystem. Reads are thin (heavy collections — versions/messages/styles — are
lazy). Writes are PATCH (only keys present in the payload change). Identity comes
from the JWT session (prism.auth.authenticator), exactly like the rest of prism.api.*.

Access model (for now):
  - editor = the board owner only. Writes are owner-only.
  - viewer = everyone else. Internal/PSL read live data; brand users read the
    frozen published_snapshot, scoped to their brand + Published status.

Board lifecycle:
  A board is created as soon as the user starts one (create_moodboard takes an empty
  payload) and is filled in over the following steps, rather than being materialised
  after the first image generation. Two fields carry that:
    - setup_stage    — the wizard step the client was last on; what "edit" resumes to.
                       Client-set, follows the user backwards too, so it is a resume
                       marker only.
    - primary_version — the completeness signal. Until it's set the board is
                       in-progress: hidden from other internal users' lists
                       (_list_scope) and not publishable (publish).
  In-progress boards are never swept — they stay visible to their owner, that owner's
  shared editors, and admins, so abandoned work can always be resumed.
'''

import mimetypes

import frappe

from prism.auth.authenticator import auth_required
import prism.api.util as util
import prism.api.moodboard_style as ms
import prism.api.surplus_recommender as surplus_recommender
import prism.lib.cloud as cloud
import prism.lib.moodboard_media as media

DOCTYPE = 'Moodboard'
MAX_IMAGE_FILE_SIZE_MB = 30

# Customer brief attachments are documents as well as images, with their own
# (smaller) cap. Extensions are checked rather than the client-declared mime type,
# which is trivially spoofed and often just 'application/octet-stream' anyway.
MAX_BRIEF_FILE_SIZE_MB = 20
BRIEF_FILE_EXTENSIONS = (
    '.pdf',
    '.ppt', '.pptx',
    '.xls', '.xlsx', '.csv',
    '.png', '.jpg', '.jpeg', '.webp', '.gif',
)

_STATUS_VALUES = ('Draft', 'Published', 'Unpublished Changes')

# Wizard steps, in order. A board is now created up-front (see create_moodboard) and
# filled in across these steps, so `setup_stage` records the step the client was last
# on and is what "edit" resumes to. It follows the user backwards as well as forwards
# — it is a resume marker, NOT a measure of how far the board got. Completeness is
# `primary_version` (an image has been chosen), which is what publish() and list
# visibility key off.
_SETUP_STAGES = (
    'Project Brief',
    'Creative Direction',
    'Fabric Selection',
    'Style Generation',
    'Costing',
    'ESG',
    'Generation',
    'Canvas Edit',
    'Final Review',
)
_DEFAULT_SETUP_STAGE = _SETUP_STAGES[0]

# Pagination / search tuning for list_moodboards.
_DEFAULT_LIMIT = 20
_MAX_LIMIT = 100
_SEARCH_MIN_CHARS = 3

# Core scalar/JSON fields patchable via update_moodboard (PATCH — only the keys
# present in the payload are written). camelCase (client) -> column.
_PATCH_FIELDS = {
    'title': 'moodboard_title',
    'setupStage': 'setup_stage',
    'season': 'season',
    'creativityBias': 'creativity_bias',
    'mood': 'mood',
    'userVision': 'user_vision',
    'brief': 'brief',
    'imageModel': 'image_model',
    'imageSize': 'image_size',
    'thumbnail': 'thumbnail',
    'fabricAutoSuggest': 'fabric_auto_suggest',
    'fabricAutoCount': 'fabric_auto_count',
    'styleAutoSuggest': 'style_auto_suggest',
    'styleAutoCount': 'style_auto_count',
    'variantCount': 'variant_count',
}
# JSON-array/object fields (stored as JSON columns) — also patched here.
_PATCH_JSON_FIELDS = {
    'genders': 'genders',
    'styleCategories': 'style_categories',
    'moodTags': 'mood_tags',
    'colours': 'colours',
    'alignmentReport': 'alignment_report',
    'addons': 'addons',
    'keywords': 'keywords',
    'keyInsights': 'key_insights',
    'fabricDirection': 'fabric_direction',
    'garmentInspiration': 'garment_inspiration',
    'garmentDirectionSignals': 'garment_direction_signals',
    'printDirectionSignals': 'print_direction_signals',
}

# Internal working material: the project brief's reference images and source
# documents, plus keywords, insights, the direction fields and the inspiration
# tables. _build_snapshot drops these keys so none of it reaches brand users, who
# only ever read the frozen snapshot. The image tables in particular hold
# third-party material (scraped product shots, customer-supplied decks), which is
# exactly what shouldn't be redistributed under a brand's own board. The written
# brief itself (`brief` / `userVision`) stays in the snapshot as it always has.
_SNAPSHOT_INTERNAL_KEYS = (
    'inspirationImages',
    'customerBrief',
    'keywords',
    'keyInsights',
    'fabricDirection',
    'garmentInspiration',
    'garmentDirectionSignals',
    'printDirection',
    'printDirectionSignals',
)


# =====================================================================
# Read
# =====================================================================

@frappe.whitelist(allow_guest=True)
@auth_required
def list_moodboards(filters=None, limit=20, offset=0):
    '''
    Relevance-scoped, small list objects (no heavy fields). Internal/PSL see all
    active boards; brand users see only Published boards linked to their brand.

    In-progress boards (created but with no primary version chosen yet) are omitted
    unless the caller can act on them — owner, shared editor, or admin. Items carry
    `setupStage` + `isComplete` so those callers can render a "resume" card.

    Optional filters: brand, season, gender, status, search (title), styleCategories.
      - gender / search are pushed into the DB query (search needs >= 3 chars).
      - search (title substring) is honoured for both brand and internal users.
      - styleCategories: list (or single styleCategory) — a board matches if its
        style_categories contains ANY of the requested values (see list_style_categories
        for the dropdown of valid values).

      - brand / brands: a brand user whose brand carries "All Moodboard Permission"
        may pass one brand id (`brand`) or an array (`brands`) to narrow the
        cross-brand result to boards linked to ANY of those brands; absent, they see
        every Published board. (Ordinary brand users always see only their own boards,
        so this filter is a no-op for them.)

    Pagination: limit (default 20, capped 100) + offset (>= 0). Returns an envelope
    { total, limit, offset, items, allMoodboardPermission } where total counts ALL
    matching boards (ignoring the page window) so the client can page, and
    allMoodboardPermission tells the client whether the caller's brand carries "All
    Moodboard Permission" (so it can show the cross-brand brands filter).
    '''
    user, brand = _identity()
    is_admin = _is_admin()
    editor_ids = set() if is_admin else _collab_editor_ids(user)
    filters = _as_dict(filters)
    limit, offset = _page(limit, offset)

    # Tells the client this caller's brand carries "All Moodboard Permission", so it
    # can offer the cross-brand `brands` filter (an array of brand ids) honoured below.
    sees_all = bool(brand and _brand_sees_all(brand['id']))

    q = _list_scope(user, brand, filters, is_admin=is_admin, editor_ids=editor_ids)
    if q is None:
        return _page_envelope([], 0, limit, offset, allMoodboardPermission=sees_all)

    # Style-category filter (JSON-array column, same shape as genders). Multi-select:
    # a board matches if its style_categories contains ANY of the requested values
    # (OR within the filter). Resolve to names and intersect with any existing name
    # scope so it composes with the brand restriction set in _list_scope.
    cats = _as_list(filters.get('styleCategories'))
    if not cats and filters.get('styleCategory'):
        cats = [filters['styleCategory']]
    if cats:
        if not _narrow_names(q, _names_with_style_categories(cats)):
            return _page_envelope([], 0, limit, offset, allMoodboardPermission=sees_all)

    # Brand users see the freshest published work first (fall back to creation for
    # any board missing a publish timestamp); internal/PSL users keep creation order.
    order_by = 'last_published_at desc, creation desc' if brand else 'creation desc'

    total = frappe.db.count(DOCTYPE, q)
    rows = frappe.get_all(
        DOCTYPE, filters=q,
        fields=['name', 'moodboard_title', 'thumbnail', 'season', 'status', 'owner',
                'genders', 'setup_stage', 'primary_version', 'creation', 'modified',
                'last_published_at'],
        order_by=order_by, ignore_permissions=True,
        limit_start=offset, limit_page_length=limit,
    )

    brands_map = _brands_for([r['name'] for r in rows])
    owners_map = _owners_for([r.get('owner') for r in rows])

    items = [{
        'id': r['name'],
        'title': r.get('moodboard_title'),
        'thumbnail': r.get('thumbnail') or None,
        'brands': brands_map.get(r['name'], []),
        'genders': _parse(r.get('genders'), []),
        'season': r.get('season') or None,
        'owner': owners_map.get(r.get('owner')),
        'access': 'editor' if (is_admin or r.get('owner') == user or r['name'] in editor_ids) else 'viewer',
        'status': r.get('status'),
        # In-progress boards reach the list only for people who can act on them, so
        # the client can render a "resume at <setupStage>" card for those.
        'setupStage': r.get('setup_stage') or _DEFAULT_SETUP_STAGE,
        'isComplete': bool(r.get('primary_version')),
        'createdAt': _iso(r.get('creation')),
        'lastEditedAt': _iso(r.get('modified')),
        'lastPublishedAt': _iso(r.get('last_published_at')),
    } for r in rows]

    return _page_envelope(items, total, limit, offset, allMoodboardPermission=sees_all)


@frappe.whitelist(allow_guest=True)
@auth_required
def list_style_categories(filters=None):
    '''
    Distinct style categories present across the boards the caller can see — the
    value set for the list_moodboards `styleCategories` filter dropdown.

    Uses the SAME visibility scope and filters as list_moodboards (brand scope,
    season, gender, search, owner) EXCEPT styleCategories itself, so the options
    cascade with the other active filters and every option is guaranteed to return
    at least one board. Returns { total, items } with items sorted case-insensitively.
    '''
    user, brand = _identity()
    filters = _as_dict(filters)

    q = _list_scope(user, brand, filters)
    if q is None:
        return {'total': 0, 'items': []}

    rows = frappe.get_all(
        DOCTYPE, filters=q, fields=['style_categories'],
        ignore_permissions=True, limit_page_length=0,
    )

    # Dedup case-insensitively while keeping the first-seen casing. style_categories
    # is normally a JSON array, but some legacy rows store a bare JSON scalar string
    # ("Tops" instead of ["Tops"]); coerce that to a single value rather than
    # iterating it character-by-character.
    seen = {}
    for r in rows:
        parsed = _parse(r.get('style_categories'), [])
        if isinstance(parsed, str):
            parsed = [parsed]
        elif not isinstance(parsed, list):
            parsed = []
        for c in parsed:
            c = c.strip() if isinstance(c, str) else c
            if c and str(c).lower() not in seen:
                seen[str(c).lower()] = c
    items = sorted(seen.values(), key=lambda c: str(c).lower())
    return {'total': len(items), 'items': items}


@frappe.whitelist(allow_guest=True)
@auth_required
def list_moodboard_owners(filters=None, search=None):
    '''
    Internal users who own at least one visible board, each with a board count —
    the value set for the list_moodboards `owner` filter dropdown.

    Uses the SAME visibility scope and filters as list_moodboards (brand scope,
    season, gender, search, styleCategories) EXCEPT the `owner` filter itself, so
    the owner options + counts cascade with the other active filters and every
    option is guaranteed to return at least one board. Counts every active board
    the user owns (Draft / Published / Unpublished Changes).

    Brand users and system accounts (Administrator/Guest) are excluded, so every
    entry is a real internal owner. Optional `search` matches owner name or email.
    Returns { total, items } with items [{id, name, image, count}] sorted by name.
    '''
    user, brand = _identity()
    filters = _as_dict(filters)

    # Same scope as list_moodboards, minus the owner filter (we're enumerating owners).
    scope_filters = {k: v for k, v in filters.items() if k != 'owner'}
    q = _list_scope(user, brand, scope_filters)
    if q is None:
        return {'total': 0, 'items': []}

    # Mirror list_moodboards' styleCategories narrowing so counts stay consistent.
    cats = _as_list(filters.get('styleCategories'))
    if not cats and filters.get('styleCategory'):
        cats = [filters['styleCategory']]
    if cats:
        if not _narrow_names(q, _names_with_style_categories(cats)):
            return {'total': 0, 'items': []}

    rows = frappe.get_all(
        DOCTYPE, filters=q, fields=['owner'],
        ignore_permissions=True, limit_page_length=0,
    )
    counts = {}
    for r in rows:
        o = r.get('owner')
        if o:
            counts[o] = counts.get(o, 0) + 1
    if not counts:
        return {'total': 0, 'items': []}

    # Keep only internal owners: drop brand users and system accounts.
    brand_users = set(frappe.get_all('Brand User', pluck='user'))
    owner_objs = _owners_for(list(counts))

    s = (search or '').strip().lower()
    items = []
    for email, cnt in counts.items():
        if email in brand_users or email in ('Administrator', 'Guest'):
            continue
        o = owner_objs.get(email) or {'id': email, 'name': email, 'image': None}
        if s and s not in (o['name'] or '').lower() and s not in email.lower():
            continue
        items.append({**o, 'count': cnt})

    items.sort(key=lambda o: (o['name'] or '').lower())
    return {'total': len(items), 'items': items}


@frappe.whitelist(allow_guest=True)
@auth_required
def list_moodboard_brands(filters=None, search=None):
    '''
    Brands linked to the boards the caller can see, each with a board count — for the
    list_moodboards `brands` filter dropdown (and any brand-facet UI). Callable by both
    internal and brand users.

    Uses the SAME visibility scope and filters as list_moodboards (season, gender,
    search-by-title, owner, status, styleCategories) EXCEPT the brand/brands filter
    itself, so the brand options + counts cascade with the other active filters and
    every option is guaranteed to return at least one board. The count for a brand is
    the number of DISTINCT accessible boards linked to it, so it never counts boards
    the caller can't see.

    A board may be associated with several brands at once; that is intentional shared
    access, so each such brand is counted for it. The brands returned are always those
    linked to the boards the caller can actually see, mirroring list_moodboards' scope:
      - Internal / PSL users: every active board they can see; a passed brand filter is
        ignored here (we're enumerating brands) so the full facet is returned.
      - "All Moodboard Permission" brand users: every brand with a Published /
        Unpublished-Changes board (cross-brand), same as their list_moodboards scope.
      - Ordinary brand users: the boards linked to their brand — plus any sibling brands
        those shared boards also carry, since both brands legitimately access the board.

    Optional `search` matches the brand name (case-insensitive). Returns { total, items }
    with items [{id, name, count}] sorted by count desc, then name.
    '''
    user, brand = _identity()
    filters = _as_dict(filters)

    # Same scope as list_moodboards, minus the brand/brands filter (we're enumerating
    # brands). Dropping both keys keeps the facet independent of the current selection.
    scope_filters = {k: v for k, v in filters.items() if k not in ('brand', 'brands')}
    q = _list_scope(user, brand, scope_filters)
    if q is None:
        return {'total': 0, 'items': []}

    # Mirror list_moodboards' styleCategories narrowing so counts stay consistent.
    cats = _as_list(filters.get('styleCategories'))
    if not cats and filters.get('styleCategory'):
        cats = [filters['styleCategory']]
    if cats:
        if not _narrow_names(q, _names_with_style_categories(cats)):
            return {'total': 0, 'items': []}

    # The exact set of boards the caller can see under the active filters.
    names = frappe.get_all(DOCTYPE, filters=q, pluck='name',
                           ignore_permissions=True, limit_page_length=0)
    if not names:
        return {'total': 0, 'items': []}

    # Count distinct accessible boards per brand via the Brand Moodboard link table.
    links = frappe.get_all('Brand Moodboard', filters={'moodboard': ['in', names]},
                           fields=['brand', 'moodboard'], ignore_permissions=True)
    counts = {}
    for l in links:
        counts.setdefault(l['brand'], set()).add(l['moodboard'])
    if not counts:
        return {'total': 0, 'items': []}

    s = (search or '').strip().lower()
    items = []
    for bid, mset in counts.items():
        label = frappe.db.get_value('Brand', bid, 'brand') or bid
        if s and s not in str(label).lower():
            continue
        items.append({'id': bid, 'name': label, 'count': len(mset)})

    items.sort(key=lambda b: (-b['count'], str(b['name']).lower()))
    return {'total': len(items), 'items': items}


@frappe.whitelist(allow_guest=True)
@auth_required
def get_moodboard(id=None):
    '''
    Editor core (internal, live) or the frozen published_snapshot (brand users).
    Heavy collections (versions/messages/styles) are fetched via their own lists.
    '''
    doc = _get_or_404(id)
    user, brand = _identity()

    # Live owner object ({id, name, image}) overlaid outside the frozen snapshot, so it
    # stays current (and present on older snapshots) alongside the plain `owner` email.
    owner_obj = _owner_obj(doc.owner)

    if brand:
        # Brand user: only the frozen snapshot — never live edits. Access is granted if
        # either (a) it's one of their brand's boards carrying a snapshot (Published or
        # Unpublished Changes), or (b) their brand has "All Moodboard Permission" and the
        # board carries a snapshot (Published or Unpublished Changes). Both cases serve
        # the last frozen snapshot, so a board flipped to "Unpublished Changes" (e.g. by a
        # post-publish media upload) stays reachable instead of vanishing.
        linked_ok = doc.status in ('Published', 'Unpublished Changes') and _is_brand_linked(doc.name, brand['id'])
        all_ok = doc.status in ('Published', 'Unpublished Changes') and _brand_sees_all(brand['id'])
        if not (linked_ok or all_ok):
            _forbidden('Not available')
        snap = _parse(doc.published_snapshot, None)
        result = _overlay_live_media(doc, snap)
        if isinstance(result, dict):
            result['ownerObject'] = owner_obj
        return result

    core = _core(doc, user)
    core['ownerObject'] = owner_obj
    return core


# =====================================================================
# Published styles catalog — brand & internal facing
# =====================================================================

@frappe.whitelist(allow_guest=True)
@auth_required
def list_moodboard_styles(filters=None, limit=20, offset=0):
    '''
    Paginated catalog of *published* moodboard styles for the styles listing page.

    Scope mirrors list_moodboards' visibility but is pinned to the published set
    (boards in Published / Unpublished Changes, styles with include=1):
      - Brand users see styles from boards linked to their brand (or every board,
        if their brand carries "All Moodboard Permission").
      - Internal / PSL users see styles across ALL published boards, each carrying
        a `brands` tag ([{id, name}]).

    Brand users get the brand-safe style shape (no internal price breakdown, no
    tech-pack / BOM, no cross-brand tags — reuses _brand_style); internal users get
    the full style plus brand tags.

    Optional filters: search (garment name / category / description / colour, >= 3
    chars), gender, productCategory, fabricQuality, colour (style columns); season,
    brand/brands, moodboard (board level). Pagination: limit (default 20, capped
    100) + offset. Returns { total, limit, offset, items }.
    '''
    user, brand = _identity()
    filters = _as_dict(filters)
    limit, offset = _page(limit, offset)

    names = _style_board_scope(user, brand, filters)
    if not names:
        return _page_envelope([], 0, limit, offset)

    sflt, or_f = _style_filters(names, filters)
    all_names = frappe.get_all(
        ms.DOCTYPE, filters=sflt, or_filters=or_f,
        order_by='creation desc, name asc', pluck='name',
        ignore_permissions=True, limit_page_length=0,
    )
    total = len(all_names)
    page = all_names[offset:offset + limit]
    if not page:
        return _page_envelope([], total, limit, offset)

    docs = [frappe.get_doc(ms.DOCTYPE, n) for n in page]
    board_ids = list({d.moodboard for d in docs})
    boards = {b['name']: b for b in frappe.get_all(
        DOCTYPE, filters={'name': ['in', board_ids]},
        fields=['name', 'moodboard_title', 'season', 'status'], ignore_permissions=True)}
    brands_map = {} if brand else _brands_for(board_ids)

    items = [_style_item(d, boards.get(d.moodboard), brands_map, is_brand=bool(brand)) for d in docs]
    return _page_envelope(items, total, limit, offset)


@frappe.whitelist(allow_guest=True)
@auth_required
def get_moodboard_style(id=None):
    '''
    One published style with the same brand access restrictions as the catalog:
      - Brand users may read it only if its board is Published / Unpublished Changes
        and linked to their brand (or their brand has "All Moodboard Permission");
        they get the brand-safe shape.
      - Internal / PSL users may read any published style; they get the full style
        (incl. costInputs / costResult) plus brand tags.
    A style whose board is not published (or is excluded) is reported as not found.
    '''
    doc = ms._get_or_404(id)
    board = _get_or_404(doc.moodboard)
    user, brand = _identity()

    accessible_style = board.is_active and doc.include
    if brand:
        # Brand users: published set only, and the board must be theirs.
        published = board.status in ('Published', 'Unpublished Changes')
        linked = _is_brand_linked(board.name, brand['id']) or _brand_sees_all(brand['id'])
        if not (accessible_style and published and linked):
            frappe.throw('Style not found', frappe.DoesNotExistError)
        is_brand, brands_map = True, {}
    else:
        # Internal / PSL: any active board — Draft included.
        if not accessible_style:
            frappe.throw('Style not found', frappe.DoesNotExistError)
        is_brand, brands_map = False, _brands_for([board.name])

    board_info = frappe._dict(moodboard_title=board.moodboard_title, season=board.season,
                              status=board.status)
    item = _style_item(doc, board_info, brands_map, is_brand=is_brand)

    # Small parent-board object for the detail header (title + thumbnail + when it
    # was last published), alongside the flat moodboard / moodboardTitle / season.
    item['moodboardObject'] = {
        'id': board.name,
        'title': board.moodboard_title,
        'thumbnail': board.thumbnail or None,
        'lastPublishedAt': _iso(board.last_published_at),
    }

    if not is_brand:
        # Internal detail: attach full cost inputs/result (as ms.get_style does).
        full = ms._to_api(doc, full=True)
        item['costInputs'] = full.get('costInputs')
        item['costResult'] = full.get('costResult')
    return item


# --- catalog filter-option lists (each cascades with the other active filters) ---

@frappe.whitelist(allow_guest=True)
@auth_required
def list_style_genders(filters=None):
    ''' Distinct genders across the caller's visible published styles. -> {total, items}. '''
    return _style_option(_identity(), filters, drop='gender', field='gender')


@frappe.whitelist(allow_guest=True)
@auth_required
def list_style_product_categories(filters=None):
    ''' Distinct product categories across visible published styles. -> {total, items}. '''
    return _style_option(_identity(), filters, drop='productCategory', field='product_category')


@frappe.whitelist(allow_guest=True)
@auth_required
def list_style_fabric_qualities(filters=None):
    ''' Distinct fabric qualities across visible published styles. -> {total, items}. '''
    return _style_option(_identity(), filters, drop='fabricQuality', field='fabric_quality')


@frappe.whitelist(allow_guest=True)
@auth_required
def list_style_fabrics(filters=None):
    '''
    Distinct matched fabrics across the caller's visible published styles, read from
    each style's attrs_extra.matched_fabric. Returns the WHOLE fabric object per
    distinct fabric (deduped by its id). Feeds the `fabric` filter dropdown; pass the
    chosen fabric's `id` back as filters.fabric.
    -> { total, items: [{ id, name, code, blend, composition, custom_fabric_name,
         description, finish, gsm }, ...] }.
    '''
    user, brand = _identity()
    filters = _as_dict(filters)
    names = _style_board_scope(user, brand, filters)
    if not names:
        return {'total': 0, 'items': []}
    sflt, or_f = _style_filters(names, filters, drop='fabric')
    rows = frappe.get_all(ms.DOCTYPE, filters=sflt, or_filters=or_f,
                          pluck='attrs_extra', ignore_permissions=True, limit_page_length=0)
    seen = {}
    for raw in rows:
        extra = _parse(raw, {})
        fabric = extra.get('matched_fabric') if isinstance(extra, dict) else None
        if not isinstance(fabric, dict):
            continue
        key = fabric.get('id') or fabric.get('code') or fabric.get('name')
        if key and key not in seen:
            seen[key] = fabric
    items = sorted(seen.values(), key=lambda f: str(f.get('name') or '').lower())
    return {'total': len(items), 'items': items}


@frappe.whitelist(allow_guest=True)
@auth_required
def list_style_colours(filters=None):
    '''
    Distinct element colours across visible published styles, each with its hex and
    Pantone (TCX) code when known. -> { total, items: [{name, hex, pantone}] }.
    '''
    user, brand = _identity()
    filters = _as_dict(filters)
    names = _style_board_scope(user, brand, filters)
    if not names:
        return {'total': 0, 'items': []}
    sflt, or_f = _style_filters(names, filters, drop='colour')
    rows = frappe.get_all(ms.DOCTYPE, filters=sflt, or_filters=or_f,
                          fields=['element_colour', 'element_colour_hex', 'element_colour_tcx'],
                          ignore_permissions=True, limit_page_length=0)
    seen = {}
    for r in rows:
        name = (r.get('element_colour') or '').strip()
        if name and name.lower() not in seen:
            seen[name.lower()] = {
                'name': name,
                'hex': (r.get('element_colour_hex') or '').strip() or None,
                'pantone': (r.get('element_colour_tcx') or '').strip() or None,
            }
    items = sorted(seen.values(), key=lambda c: c['name'].lower())
    return {'total': len(items), 'items': items}


@frappe.whitelist(allow_guest=True)
@auth_required
def list_style_seasons(filters=None):
    ''' Distinct seasons of boards that have matching published styles. -> {total, items}. '''
    user, brand = _identity()
    filters = _as_dict(filters)
    board_ids = _style_board_ids_with_styles(user, brand, filters, drop_board='season')
    if not board_ids:
        return {'total': 0, 'items': []}
    seasons = frappe.get_all(DOCTYPE, filters={'name': ['in', board_ids]},
                             pluck='season', ignore_permissions=True)
    items = _distinct_ci(seasons)
    return {'total': len(items), 'items': items}


@frappe.whitelist(allow_guest=True)
@auth_required
def list_style_brands(filters=None):
    '''
    Brands present across the visible published styles (internal / PSL only — a
    brand user's catalog is implicitly their own brand). -> { total, items: [{id, name}] }.
    '''
    user, brand = _identity()
    filters = _as_dict(filters)
    if brand:
        return {'total': 0, 'items': []}
    board_ids = _style_board_ids_with_styles(user, brand, filters, drop_board='brand')
    if not board_ids:
        return {'total': 0, 'items': []}
    seen = {}
    for lst in _brands_for(board_ids).values():
        for b in lst:
            seen[b['id']] = b
    items = sorted(seen.values(), key=lambda b: (b['name'] or '').lower())
    return {'total': len(items), 'items': items}


# --- catalog scope / serialization helpers ---

# Request filter key -> Moodboard Style column, for the style-level filters.
_STYLE_FILTER_COLS = {
    'gender': 'gender',
    'productCategory': 'product_category',
    'fabricQuality': 'fabric_quality',
    'colour': 'element_colour',
}


def _style_board_scope(user, brand, filters):
    '''
    Board names whose published styles the caller may see. Reuses list_moodboards'
    visibility scope (brand link / All Moodboard Permission) but forces the
    published set (Published / Unpublished Changes) for everyone — internal users
    included, who otherwise also see Drafts. Honours the board-level filters
    season / brand(s) / styleCategory / moodboard. Returns a list (possibly empty).
    '''
    board_filters = {}
    # season may arrive as a single value or a multi-select array; wrap arrays as an
    # `in` filter so frappe doesn't misread a bare list as an [operator, value] pair.
    season = filters.get('season')
    if isinstance(season, (list, tuple)):
        season = [s for s in season if s]
        if season:
            board_filters['season'] = ['in', season]
    elif season:
        board_filters['season'] = season
    # brand / brands passthrough — only for brand *callers*, where _list_scope routes
    # it through _brand_filter_ids (array-safe) to narrow All-Moodboard-Permission
    # users. For internal callers the brand filter is applied below instead, because
    # _list_scope's internal branch expects a SCALAR brand and would choke on an array.
    if brand:
        for k in ('brand', 'brands'):
            if filters.get(k) is not None:
                board_filters[k] = filters[k]

    q = _list_scope(user, brand, board_filters)
    if q is None:
        return []

    # Internal / PSL brand filter — narrow to boards published to ANY of the requested
    # brands (single value or multi-select array). Done here (not via _list_scope) so
    # an array brand can't trip the scalar-brand path.
    if not brand:
        brand_ids = _brand_filter_ids(filters)
        if brand_ids:
            scoped = frappe.get_all('Brand Moodboard',
                                    filters={'brand': ['in', brand_ids]}, pluck='moodboard')
            if not _narrow_names(q, scoped):
                return []

    # Brand users are pinned to the published set (Published / Unpublished Changes)
    # by _list_scope. Internal / PSL users see EVERY active board (Draft included),
    # optionally narrowed by the publishState filter:
    #   'published'   -> boards carrying a live snapshot (Published/Unpublished Changes)
    #   'unpublished' -> Draft boards only
    if not brand:
        state = filters.get('publishState')
        if isinstance(state, (list, tuple)):
            state = state[0] if state else ''
        state = (state or '').strip().lower()
        if state == 'published':
            q['status'] = ['in', ['Published', 'Unpublished Changes']]
        elif state == 'unpublished':
            q['status'] = 'Draft'

    # styleCategory narrowing (board-level JSON array), same shape as list_moodboards.
    cats = _as_list(filters.get('styleCategories'))
    if not cats and filters.get('styleCategory'):
        cats = [filters['styleCategory']]
    if cats:
        if not _narrow_names(q, _names_with_style_categories(cats)):
            return []

    names = frappe.get_all(DOCTYPE, filters=q, pluck='name', ignore_permissions=True)

    # Board narrowing (only within the visible scope) — single id or an array.
    target = filters.get('moodboard')
    if isinstance(target, (list, tuple)):
        wanted = {t.strip() if isinstance(t, str) else t for t in target if t}
        if wanted:
            names = [n for n in names if n in wanted]
    else:
        target = target.strip() if isinstance(target, str) else target
        if target:
            names = [n for n in names if n == target]
    return names


def _style_filters(board_names, filters, drop=None):
    '''
    (filters, or_filters) for the Moodboard Style query within board_names: the
    style-column filters (gender / productCategory / fabricQuality / colour) plus
    the free-text `search`. `drop` omits one filter key (used by the cascading
    option lists). Only included styles (include=1) are part of the published set.
    '''
    sflt = {'moodboard': ['in', board_names], 'include': 1}
    for key, col in _STYLE_FILTER_COLS.items():
        if key == drop:
            continue
        val = filters.get(key)
        if isinstance(val, str):
            val = val.strip()
        if isinstance(val, (list, tuple)):
            # Multi-select: match any of the given values. A bare list would
            # otherwise be read by frappe as an [operator, value] pair (a 1-item
            # list raises "expected 2, got 1"; a 2-item list matches wrongly).
            val = [v.strip() if isinstance(v, str) else v for v in val if v not in (None, '')]
            if val:
                sflt[col] = ['in', val]
        elif val:
            sflt[col] = val

    # Fabric lives in the attrs_extra JSON (attrs_extra.matched_fabric), not a column.
    # Match the fabric id as a quoted JSON token — ids are unique, so a LIKE on
    # `"<id>"` scopes to styles carrying that matched fabric. See list_style_fabrics.
    if drop != 'fabric':
        fabric = filters.get('fabric')
        if isinstance(fabric, (list, tuple)):
            fabric = next((f for f in fabric if f), None)  # single-select dropdown
        fabric = fabric.strip() if isinstance(fabric, str) else fabric
        if fabric:
            sflt['attrs_extra'] = ['like', f'%"{fabric}"%']

    or_f = None
    if drop != 'search':
        s = (filters.get('search') or '').strip()
        if len(s) >= _SEARCH_MIN_CHARS:
            or_f = {'garment_name': ['like', f'%{s}%'],
                    'product_category': ['like', f'%{s}%'],
                    'description': ['like', f'%{s}%'],
                    'element_colour': ['like', f'%{s}%']}
    return sflt, or_f


def _style_item(doc, board, brands_map, is_brand):
    '''
    Catalog list/detail item. Brand users get the brand-safe shape (no internal
    price breakdown / tech-pack / BOM / cross-brand tags); internal users get the
    full style plus brand tags. `board` is a mapping with moodboard_title / season.
    '''
    base = ms._to_api(doc)
    if is_brand:
        item = _brand_style(base)
        item['brands'] = []
    else:
        item = base
        item['brands'] = brands_map.get(doc.moodboard, [])
        # Internal catalog spans Draft + published boards — tag each style's state.
        item['publishState'] = _publish_state(board.get('status') if board else None)
    item['moodboard'] = doc.moodboard
    item['moodboardTitle'] = board.get('moodboard_title') if board else None
    item['season'] = (board.get('season') or None) if board else None
    return item


def _publish_state(status):
    '''
    Two-state publish tag for the internal catalog: a board carrying a live snapshot
    (Published / Unpublished Changes) is 'Published'; a Draft board is 'Unpublished'.
    '''
    return 'Published' if status in ('Published', 'Unpublished Changes') else 'Unpublished'


def _style_option(identity, filters, drop, field):
    ''' Distinct string values of a style column across visible published styles. '''
    user, brand = identity
    filters = _as_dict(filters)
    names = _style_board_scope(user, brand, filters)
    if not names:
        return {'total': 0, 'items': []}
    sflt, or_f = _style_filters(names, filters, drop=drop)
    values = frappe.get_all(ms.DOCTYPE, filters=sflt, or_filters=or_f,
                            pluck=field, ignore_permissions=True, limit_page_length=0)
    items = _distinct_ci(values)
    return {'total': len(items), 'items': items}


def _style_board_ids_with_styles(user, brand, filters, drop_board):
    '''
    Board ids that (a) fall in the caller's published scope with `drop_board`
    removed from the board filters and (b) actually carry a style matching the
    remaining style-level filters. Backs the board-level option lists so their
    values cascade with the other active filters.
    '''
    board_filters = {k: v for k, v in filters.items() if k != drop_board}
    if drop_board == 'brand':
        board_filters.pop('brands', None)
    names = _style_board_scope(user, brand, board_filters)
    if not names:
        return []
    sflt, or_f = _style_filters(names, filters)
    return list(set(frappe.get_all(
        ms.DOCTYPE, filters=sflt, or_filters=or_f,
        pluck='moodboard', ignore_permissions=True, limit_page_length=0)))


def _distinct_ci(values):
    ''' Case-insensitive dedupe keeping first-seen casing, sorted case-insensitively. '''
    seen = {}
    for v in values:
        v = v.strip() if isinstance(v, str) else v
        if v and str(v).lower() not in seen:
            seen[str(v).lower()] = v
    return sorted(seen.values(), key=lambda x: str(x).lower())


@frappe.whitelist(allow_guest=True)
@auth_required
def list_versions(id=None):
    ''' Version history (internal only). '''
    _require_internal()
    _get_or_404(id)
    rows = frappe.get_all(
        'Moodboard Version', filters={'moodboard': id},
        fields=['name', 'version_key', 'parent_version', 'prompt', 'llm_prompt', 'ai_reply',
                'image', 'edited_image', 'canvas_state', 'image_model', 'image_size',
                'generation_time', 'is_showable', 'response_id', 'response_created_at',
                'edited_at', 'creation'],
        order_by='creation asc', ignore_permissions=True,
    )

    # reference_images is a child table -- frappe.get_all's field list above can't
    # reach it, so it's bulk-fetched once (grouped by parent) rather than loading
    # every version as a full Document just for its gallery rows.
    galleries = {}
    if rows:
        for img in frappe.get_all(
            'Moodboard Version Image', filters={'parent': ['in', [r.name for r in rows]]},
            fields=['name', 'parent', 'image', 'display_order', 'idx'],
            order_by='idx asc', ignore_permissions=True,
        ):
            galleries.setdefault(img.parent, []).append(img)
    for r in rows:
        r['reference_images'] = galleries.get(r.name, [])

    return [_version_obj(r) for r in rows]


@frappe.whitelist(allow_guest=True)
@auth_required
def list_messages(id=None):
    ''' Chat transcript (internal only). '''
    _require_internal()
    _get_or_404(id)
    rows = frappe.get_all(
        'Moodboard Message', filters={'moodboard': id},
        fields=['name', 'message_key', 'role', 'text', 'version', 'attachments', 'creation'],
        order_by='creation asc', ignore_permissions=True,
    )
    return [{
        'id': r['name'], 'messageKey': r.get('message_key'), 'role': r.get('role'),
        'text': r.get('text'), 'version': r.get('version'),
        'attachments': _parse(r.get('attachments'), []), 'createdAt': _iso(r.get('creation')),
    } for r in rows]


# =====================================================================
# Write — core (PATCH)
# =====================================================================

@frappe.whitelist(allow_guest=True, methods=['POST'])
@auth_required
def create_moodboard(payload=None):
    '''
    Create a board. Owner = session user; status Draft; setup_stage at the first
    wizard step. Everything else is optional — the board is meant to be created as
    soon as the user starts one and filled in by later PATCHes (update_moodboard,
    sync_fabrics/sync_garments, add_version), so `payload` may be empty.

    Until a primary version is chosen the board is in-progress: it stays out of
    other internal users' lists (see _list_scope) and cannot be published.
    '''
    _require_internal()
    payload = _as_dict(payload)
    doc = frappe.new_doc(DOCTYPE)
    doc.moodboard_title = payload.get('title') or 'Untitled'
    doc.status = 'Draft'
    doc.is_active = 1
    doc.setup_stage = _DEFAULT_SETUP_STAGE
    # Insert BEFORE patching: _apply_patch's thumbnail branch stores to an S3 key
    # built from doc.name, which doesn't exist until the row does.
    doc.insert(ignore_permissions=True)
    ignored = []
    if payload:
        ignored = _apply_patch(doc, payload)
        doc.save(ignore_permissions=True)
    _log_edit(doc, 'Created')
    frappe.db.commit()
    result = _core(doc, _identity()[0])
    report = _ignored_keys_report(ignored)
    if report:
        result['ignoredKeys'] = report
    return result


@frappe.whitelist(allow_guest=True, methods=['POST'])
@auth_required
def update_moodboard(id=None, payload=None):
    '''
    PATCH: only the keys present in payload are written; absent keys are left
    untouched. Editing a Published board flips status to "Unpublished Changes".
    '''
    doc = _get_or_404(id)
    _assert_editor(doc)
    ignored = _apply_patch(doc, _as_dict(payload))
    _touch_if_published(doc)
    doc.save(ignore_permissions=True)
    _log_edit(doc, 'Updated')
    frappe.db.commit()
    result = _core(doc, _identity()[0])
    report = _ignored_keys_report(ignored)
    if report:
        result['ignoredKeys'] = report
    return result


@frappe.whitelist(allow_guest=True, methods=['POST'])
@auth_required
def set_brands(id=None, brand_ids=None):
    ''' Reconcile the board's Brand Moodboard links (assignment). '''
    doc = _get_or_404(id)
    _assert_editor(doc)
    result = _set_brands(id, brand_ids)
    _log_edit(doc, 'Brands Updated')
    frappe.db.commit()
    return result


def _set_brands(moodboard, brand_ids):
    wanted = {b for b in _as_list(brand_ids) if frappe.db.exists('Brand', b)}
    existing = set(frappe.get_all('Brand Moodboard', filters={'moodboard': moodboard}, pluck='brand'))
    for b in wanted - existing:
        bm = frappe.new_doc('Brand Moodboard'); bm.brand = b; bm.moodboard = moodboard
        bm.insert(ignore_permissions=True)
    for b in existing - wanted:
        link = frappe.db.get_value('Brand Moodboard', {'moodboard': moodboard, 'brand': b})
        if link:
            frappe.delete_doc('Brand Moodboard', link, ignore_permissions=True, force=True)
    return _brands_for([moodboard]).get(moodboard, [])


# =====================================================================
# Write — sharing (collaborators)
# =====================================================================

@frappe.whitelist(allow_guest=True, methods=['POST'])
@auth_required
def share_moodboard(id=None, user=None):
    '''
    Grant edit access on a board to an internal user. Owner or admin only.
    Target must be an enabled, non-brand (internal) user. Idempotent: re-sharing
    an existing collaborator is a no-op. Returns the current collaborator list.
    '''
    doc = _get_or_404(id)
    _assert_owner_or_admin(doc)
    target = _assert_internal_user(user)

    # The owner already has full edit rights — no collaborator row needed.
    if target != doc.owner and not frappe.db.exists(
            'Moodboard Collaborator', {'moodboard': doc.name, 'user': target}):
        c = frappe.new_doc('Moodboard Collaborator')
        c.moodboard = doc.name
        c.user = target
        c.access = 'editor'
        c.insert(ignore_permissions=True)
        _log_edit(doc, 'Shared', details=f'editor access granted to {target}')
        frappe.db.commit()

    return {'success': True, 'collaborators': _collaborators_for(doc.name)}


@frappe.whitelist(allow_guest=True, methods=['POST'])
@auth_required
def unshare_moodboard(id=None, user=None):
    ''' Revoke a user's edit access on a board. Owner or admin only. '''
    doc = _get_or_404(id)
    _assert_owner_or_admin(doc)

    link = frappe.db.exists('Moodboard Collaborator', {'moodboard': doc.name, 'user': user})
    if link:
        frappe.delete_doc('Moodboard Collaborator', link, ignore_permissions=True, force=True)
        _log_edit(doc, 'Unshared', details=f'access revoked from {user}')
        frappe.db.commit()

    return {'success': True, 'collaborators': _collaborators_for(doc.name)}


@frappe.whitelist(allow_guest=True, methods=['POST'])
@auth_required
def set_collaborators(id=None, users=None):
    '''
    Reconcile a board's editor collaborators to EXACTLY `users` (list of emails) —
    the "Save" action of the Manage Access dialog. Owner or admin only. Every email
    is validated as an enabled internal (non-brand) user first; one bad entry aborts
    the whole change (nothing is written). The owner is ignored if present (they
    always edit). Returns the resulting collaborator list.
    '''
    doc = _get_or_404(id)
    _assert_owner_or_admin(doc)

    # Validate everything BEFORE writing anything (two-phase, all-or-nothing).
    wanted = set()
    for e in _as_list(users):
        e = (e or '').strip()
        if not e or e == doc.owner:
            continue
        _assert_internal_user(e)  # raises on invalid / disabled / brand user
        wanted.add(e)

    existing = set(frappe.get_all('Moodboard Collaborator',
                                  filters={'moodboard': doc.name}, pluck='user'))
    added, removed = [], []
    for e in wanted - existing:
        c = frappe.new_doc('Moodboard Collaborator')
        c.moodboard = doc.name
        c.user = e
        c.access = 'editor'
        c.insert(ignore_permissions=True)
        added.append(e)
    for e in existing - wanted:
        link = frappe.db.get_value('Moodboard Collaborator', {'moodboard': doc.name, 'user': e})
        if link:
            frappe.delete_doc('Moodboard Collaborator', link, ignore_permissions=True, force=True)
            removed.append(e)

    if added or removed:
        _log_edit(doc, 'Access Changed', details={'added': added, 'removed': removed})
        frappe.db.commit()

    return {'success': True, 'collaborators': _collaborators_for(doc.name)}


@frappe.whitelist(allow_guest=True)
@auth_required
def list_internal_users(search=None, limit=50):
    '''
    Enabled internal (non-brand) users for the Manage Access picker. Optional
    `search` matches full name or email. Excludes brand users and system accounts,
    so every returned user is a valid share target. -> [{id, name, image}]
    '''
    f = {'enabled': 1, 'user_type': 'System User',
         'name': ['not in', ['Administrator', 'Guest']]}
    s = (search or '').strip()
    or_f = {'full_name': ['like', f'%{s}%'], 'name': ['like', f'%{s}%']} if s else None
    rows = frappe.get_all(
        'User', filters=f, or_filters=or_f,
        fields=['name', 'full_name', 'user_image'],
        order_by='full_name asc', ignore_permissions=True,
        limit_page_length=min(int(limit or 50), _MAX_LIMIT))
    brand_users = set(frappe.get_all('Brand User', pluck='user'))
    return [{'id': r['name'], 'name': r.get('full_name') or r['name'],
             'image': r.get('user_image') or None}
            for r in rows if r['name'] not in brand_users]


@frappe.whitelist(allow_guest=True)
@auth_required
def resolve_user(email=None):
    '''
    Validate a pasted email for the Manage Access chip input. Returns
    {valid, reason?, user?}. valid=True only for an enabled internal user, with the
    user object ({id, name, image}) the client renders as a chip.
    '''
    email = (email or '').strip()
    if not email:
        return {'valid': False, 'reason': 'Empty email'}
    if not frappe.db.exists('User', email):
        return {'valid': False, 'reason': 'No such user'}
    if not frappe.db.get_value('User', email, 'enabled'):
        return {'valid': False, 'reason': 'User is disabled'}
    if frappe.db.exists('Brand User', {'user': email}):
        return {'valid': False, 'reason': 'Not an internal user'}
    return {'valid': True, 'user': _owner_obj(email)}


@frappe.whitelist(allow_guest=True)
@auth_required
def list_collaborators(id=None):
    ''' Owner + shared editors of a board (for a "Shared with" panel). '''
    doc = _get_or_404(id)
    return {
        'owner': _owner_obj(doc.owner),
        'collaborators': _collaborators_for(doc.name),
    }


@frappe.whitelist(allow_guest=True)
@auth_required
def list_edit_log(id=None, limit=50, offset=0):
    ''' Audit trail for a board: who edited, in what capacity (Owner/Admin/
        Collaborator), what action, and when. Most recent first. '''
    doc = _get_or_404(id)
    limit, offset = _page(limit, offset)
    rows = frappe.get_all(
        'Moodboard Edit Log', filters={'moodboard': doc.name},
        fields=['name', 'actor', 'actor_type', 'action', 'details', 'creation'],
        order_by='creation desc', ignore_permissions=True,
        limit_start=offset, limit_page_length=limit)
    actors = _owners_for([r['actor'] for r in rows])
    items = [{
        'id': r['name'],
        'actor': actors.get(r['actor']),
        'actorType': r.get('actor_type'),
        'action': r.get('action'),
        'details': r.get('details'),
        'at': _iso(r.get('creation')),
    } for r in rows]
    total = frappe.db.count('Moodboard Edit Log', {'moodboard': doc.name})
    return {'total': total, 'limit': limit, 'offset': offset, 'items': items}


# =====================================================================
# Write — fabrics / garments (realign: full replace, store all candidates)
# =====================================================================

@frappe.whitelist(allow_guest=True, methods=['POST'])
@auth_required
def sync_fabrics(id=None, fabrics=None):
    '''
    Replace the dyed-fabric candidate set (selected ones flagged), mirroring
    sync_surplus_fabrics: the client owns the ordering and sends the whole list
    back, deselected rows included — this replaces the table, so filtering them
    out here would delete them rather than just deselect them.

    Each item is a recommendation object straight from
    moodboard_dyed_recommender.recommend(), a group object from its catalogue(),
    or the older get_matching_fabrics shape. All three are accepted and stored the
    same way: the queryable columns are written from whichever keys are present
    and the whole object is kept verbatim in `recommendation`, so a field the
    recommender grows needs no migration here.

    IDENTITY IS `id` — the Moodboard Dyed Fabric docname the recommender returns.
    A repeated id is collapsed to its first occurrence: the board cannot hold the
    same fabric twice, and a duplicate would become two tiles the user can never
    tell apart. `code` is deliberately NOT the key (14,334 distinct codes across
    24,838 rows, so it is not unique), and an item carrying no id at all is kept
    rather than dropped — there is nothing to collapse it against.

    A recommendation is a FOLD of the catalogue rows that score identically, and
    the board selects the fold. `variant_count` / `variant_codes` are stored for
    display; picking one variant out of a fold is not a thing the board does.

    Recolours have no dyed equivalent, so nothing else is touched by this call.
    '''
    doc = _get_or_404(id)
    _assert_editor(doc)
    doc.set('fabrics', [])
    seen = set()
    for x in _as_list(fabrics):
        x = _as_dict(x)
        declared = _clean_str(_pick(x, 'fabric', 'id'))
        if declared and declared in seen:
            continue
        if declared:
            seen.add(declared)
        doc.append('fabrics', _dyed_fabric_row(x, declared, len(doc.fabrics)))
    _touch_if_published(doc)
    doc.save(ignore_permissions=True)
    _log_edit(doc, 'Fabrics Updated')
    frappe.db.commit()
    return _core(doc, _identity()[0])['fabrics']


def _dyed_fabric_row(x, declared, rank):
    '''
    One sent fabric -> the stored row. Columns and the raw blob are written from
    the same dict in one place so the denormalised copies cannot drift.

    image_url / thumbnail are stripped from the raw blob: they are derived from
    `batch` and are rebuilt on read, so a re-photographed batch shows up without
    rewriting anything here. The legacy `image_url` COLUMN is still written from
    whatever was sent, because rows from get_matching_fabrics carry a URL and no
    batch, and that column is the only place it survives.
    '''
    raw = {k: v for k, v in x.items()
           if k not in ('image_url', 'thumbnail', 'imageUrl', 'selected')}
    sent_image = _pick(x, 'image_url', 'imageUrl')
    batch = _clean_str(x.get('batch'))
    custom_name = _pick(x, 'custom_fabric_name', 'customFabricName')
    composition = x.get('composition')
    return {
        'fabric': _resolve_dyed_fabric(declared, x),
        'selected': 1 if x.get('selected') else 0,
        'source': _dyed_source(x),
        'rank': rank,
        'source_id': _pick(x, 'source_id', 'sourceId'),

        'match_score': _int_or_none(_pick(x, 'match_score', 'matchScore')),
        'match_source': _pick(x, 'match_source', 'matchSource'),
        'matched_brief_index': _int_or_none(_pick(x, 'matched_brief_index', 'matchedBriefIndex')),
        'matched_brief_fabric': _pick(x, 'matched_brief_fabric', 'matchedBriefFabric'),
        'best_for': _pick(x, 'best_for', 'bestFor'),
        'reason': x.get('reason'),
        'caveats': _dump(x.get('caveats') or []),
        'fit_score': _pick(x, 'fit_score', 'fitScore'),
        'score_breakdown': _dump(_pick(x, 'score_breakdown', 'scoreBreakdown') or {}),

        # `name` is the get_matching_fabrics display label; the recommender has no
        # such key, so it falls back to the fabric's own name rather than leaving
        # a client that renders this column with nothing to show.
        'fabric_name': x.get('name') or custom_name,
        'code': x.get('code'),
        'batch': batch,
        'description': x.get('description'),
        'ai_description': _pick(x, 'ai_description', 'aiDescription'),
        'custom_fabric_name': custom_name,

        'quality': x.get('quality'),
        'quality_label': _pick(x, 'quality_label', 'qualityLabel'),
        'construction_family': _pick(x, 'construction_family', 'constructionFamily'),
        'texture_tags': _dump(_pick(x, 'texture_tags', 'textureTags') or []),
        'structure_ratio': _pick(x, 'structure_ratio', 'structureRatio'),

        # The recommender sends the blend as `composition`; `blend` is the legacy
        # column and is written from it so a client that mapped it keeps working.
        'blend': x.get('blend') or composition,
        'composition': composition,
        'composition_label': _pick(x, 'composition_label', 'compositionLabel'),
        'composition_pct': _dump(_pick(x, 'composition_pct', 'compositionPct') or {}),
        'gsm': _int_or_none(x.get('gsm')),

        'color': x.get('color'),
        'shade': x.get('shade'),
        'shade_category': _pick(x, 'shade_category', 'shadeCategory'),
        'finish': x.get('finish'),

        'matched_palette_id': _pick(x, 'matched_palette_id', 'matchedPaletteId'),
        'matched_palette_name': _pick(x, 'matched_palette_name', 'matchedPaletteName'),
        'needs_dyeing': 1 if _pick(x, 'needs_dyeing', 'needsDyeing') else 0,

        'price_from_fabric_masters': _pick(x, 'price_from_fabric_masters', 'priceFromFabricMasters'),
        'closest_fabric_master': _pick(x, 'closest_fabric_master', 'closestFabricMaster'),

        'variant_count': _int_or_none(_pick(x, 'variant_count', 'variantCount')),
        'variant_codes': _dump(_pick(x, 'variant_codes', 'variantCodes') or []),

        'has_image': _dyed_has_image(x, batch, sent_image),
        'image_url': sent_image,
        'relevancy_score': _pick(x, 'relevancy_score', 'relevancyScore'),
        'recommendation': _dump(raw),
    }


def _resolve_dyed_fabric(declared, x):
    '''
    The Moodboard Dyed Fabric this row links to, or None.

    Checked for existence before it is written, unlike everywhere else this
    module sets a Link. The value is a snapshot of a recommendation that may be
    weeks old, and a fabric deleted from the catalogue since would otherwise make
    the WHOLE board unsaveable in _validate_links — one stale tile taking the
    board down with it. The declared id stays in the raw blob either way, so
    nothing is lost by leaving the column empty.
    '''
    if declared and frappe.db.exists('Moodboard Dyed Fabric', declared):
        return declared
    return _resolve_fabric(x)


_DYED_SOURCES = ('recommended', 'library')


def _dyed_source(x):
    '''
    Where the fabric came from. The client should send it, but the two shapes are
    self-identifying — a catalogue() item carries no match/brief attribution while
    a recommend() one always does — so an unset or unknown value is inferred rather
    than rejected. Keeps a library pick from reading as a 0-scored recommendation.
    '''
    declared = _clean_str(x.get('source'))
    if declared in _DYED_SOURCES:
        return declared
    scored = (_pick(x, 'match_score', 'matchScore') is not None
              or _pick(x, 'fit_score', 'fitScore') is not None
              or _pick(x, 'match_source', 'matchSource') is not None)
    return 'recommended' if scored else 'library'


def _dyed_has_image(x, batch, sent_image):
    '''
    Whether this fabric has a photographed swatch, which is what decides if the
    read rebuilds its URLs from `batch`.

    A dyed image URL can always be CONSTRUCTED from a batch, so the batch alone
    proves nothing — `has_image` is what an actual bucket listing set, and the
    recommender sends it. Inferred from a sent image_url only for the older
    get_matching_fabrics shape, which has no such flag.
    '''
    declared = _pick(x, 'has_image', 'hasImage')
    if declared is not None:
        return 1 if declared else 0
    return 1 if (batch and sent_image) else 0


@frappe.whitelist(allow_guest=True, methods=['POST'])
@auth_required
def set_fabric_selection(id=None, fabric_ids=None, selections=None):
    '''
    Set exactly which dyed fabrics are on the board, without the client having to
    round-trip the whole payload. Selection toggling is the common action in the
    step, and a full sync per click would rewrite every row from a copy that may
    be stale.

    `fabric_ids` is the complete list of selected Moodboard Dyed Fabric ids —
    anything absent is deselected:

        ["47p26jifaj", "47nvc3ijlj"]

    `selections` accepts the same ids, or objects, for clients that find it more
    natural: {"id": ...} / {"fabric": ...} / {"code": ...}. A `code` selects EVERY
    row carrying it, because a code is not unique — if that is not what you meant,
    send the id.

    Rows are matched against both the `fabric` column and the id recorded in the
    raw recommendation, so a fabric whose catalogue row has since been deleted
    (and therefore has an empty Link — see _resolve_dyed_fabric) is still
    selectable by the id the client is holding.

    Returns the updated rows.
    '''
    doc = _get_or_404(id)
    _assert_editor(doc)

    ids = {v for v in (_clean_str(v) for v in _as_list(fabric_ids)) if v}
    codes = set()
    for entry in _as_list(selections):
        if isinstance(entry, dict):
            entry = _as_dict(entry)
            fabric_id = _clean_str(_pick(entry, 'id', 'fabric'))
            code = _clean_str(entry.get('code'))
            if fabric_id:
                ids.add(fabric_id)
            elif code:
                codes.add(code)
        else:
            value = _clean_str(entry)
            if value:
                ids.add(value)

    for row in (doc.fabrics or []):
        blob = _parse(row.recommendation, {})
        stored = _clean_str(blob.get('id')) if isinstance(blob, dict) else None
        row.selected = 1 if ((row.fabric and row.fabric in ids)
                             or (stored and stored in ids)
                             or (row.code and row.code in codes)) else 0

    _touch_if_published(doc)
    doc.save(ignore_permissions=True)
    _log_edit(doc, 'Fabrics Updated',
              details={'selected': sorted(ids), 'selectedCodes': sorted(codes)})
    frappe.db.commit()
    return _core(doc, _identity()[0])['fabrics']


@frappe.whitelist(allow_guest=True, methods=['POST'])
@auth_required
def sync_garments(id=None, garments=None):
    ''' Replace the garment candidate set (selected ones flagged; cleaned images). '''
    doc = _get_or_404(id)
    _assert_editor(doc)
    doc.set('garments', [])
    for x in _as_list(garments):
        x = _as_dict(x)
        gsr = x.get('gsr_no') or x.get('gsrNo')
        doc.append('garments', {
            # get_matching_garments returns the master docname as `id`.
            'garment': x.get('garment') or x.get('id') or (frappe.db.get_value('Sample Request', {'gsr_no': gsr}, 'name') if gsr else None),
            'selected': 1 if x.get('selected') else 0,
            'source_id': x.get('sourceId') or x.get('source_id'),
            'gsr_no': gsr,
            'garment_name': x.get('garment_name') or x.get('garmentName'),
            'gender': x.get('gender'),
            'product_category': x.get('product_category') or x.get('category'),
            'fabric_quality': x.get('fabric_quality') or x.get('fabricQuality'),
            'fabric_blend': x.get('fabric_blend') or x.get('fabricBlend'),
            'element_colour': x.get('element_colour') or x.get('sampleColour'),
            'finished_gsm': x.get('finished_gsm') if x.get('finished_gsm') is not None else x.get('finishedGsm'),
            'image_urls': _dump(x.get('image_urls') or x.get('imageUrls')),
            'relevancy_score': x.get('relevancy_score') if x.get('relevancy_score') is not None else x.get('relevancyScore'),
            'cleaned_front_image': _save_image(x.get('cleanedFrontImage') or x.get('cleaned_front_image')),
            'cleaned_back_image': _save_image(x.get('cleanedBackImage') or x.get('cleaned_back_image')),
        })
    _touch_if_published(doc)
    doc.save(ignore_permissions=True)
    _log_edit(doc, 'Garments Updated')
    frappe.db.commit()
    return _core(doc, _identity()[0])['garments']


# =====================================================================
# Write — surplus stock recommendations & recolours
# =====================================================================

# The surplus recommendations and the recoloured swatches are deliberately two
# tables with two endpoints, because they have two lifecycles:
#
#   surplus_fabrics   — what surplus_recommender.recommend() returned. Disposable:
#                       replaced wholesale every time the user re-runs the step.
#   surplus_recolours — swatches the user generated. Expensive, S3-backed, and must
#                       NOT be destroyed by a recommender re-run, so they live in a
#                       sibling table keyed by fab_code (stable across replaces,
#                       unlike a child-row name hash).
#
# Neither is reachable through update_moodboard — see _SYNC_ONLY_KEYS.
#
# surplus_fabrics is BATCH-CENTRIC: one row per (fab_code, batch), not per fab
# code. A fab code stocked in five of the brief's palette colours is five rows,
# each selectable on its own, because the board shows a tile per dye lot — the
# thing a user actually points at is a colourway, and a fab code is only the
# quality behind it. Recolours stay at fab-code level: a render is of the
# quality, and `source_batch` already records which lot it was taken over.
#
# There is no "hero batch" here any more. It existed only while one fab code had
# to be represented by one lot; now the row IS a lot. The hero survives one level
# up, in surplus_recommender's group objects, where a fab code is still one card
# in the picker and needs a default swatch.

@frappe.whitelist(allow_guest=True, methods=['POST'])
@auth_required
def sync_surplus_fabrics(id=None, fabrics=None):
    '''
    Replace the surplus recommendation set (selected ones flagged), mirroring
    sync_fabrics: the client owns the ordering and sends the whole list back,
    deselected rows included — this replaces the table, so filtering them out
    here would delete them rather than just deselect them.

    Each item is a recommendation object straight from
    surplus_recommender.recommend(), optionally carrying `selected`. Recolours are
    NOT touched by this call, so re-running the recommender is safe.

    One item per (fab_code, batch). To put five of a fab code's colourways on the
    board, send five items sharing that fab_code with different `batch` values,
    each `selected`. Send a fabric the user passed over once, carrying whatever
    batch the recommender led with, so the recommendation survives unselected.

    A repeated (fab_code, batch) pair is collapsed to its first occurrence — the
    board cannot hold the same lot twice, and a duplicate would otherwise become
    two tiles the user can never tell apart.
    '''
    doc = _get_or_404(id)
    _assert_editor(doc)
    doc.set('surplus_fabrics', [])
    seen = set()
    for x in _as_list(fabrics):
        x = _as_dict(x)
        fab_code = _clean_str(x.get('fabCode') or x.get('fab_code'))
        batch = _clean_str(x.get('batch'))
        if not fab_code or not batch:
            # The pair is the identity, and fab_code alone is the recolour join
            # key. A row missing either is unusable, so skip it rather than write
            # an orphan nothing can address.
            continue
        if (fab_code, batch) in seen:
            continue
        seen.add((fab_code, batch))
        doc.append('surplus_fabrics', _surplus_fabric_row(x, fab_code, batch, len(seen) - 1))
    _touch_if_published(doc)
    doc.save(ignore_permissions=True)
    _log_edit(doc, 'Surplus Fabrics Updated')
    frappe.db.commit()
    return _surplus_fabrics_payload(doc)


def _surplus_fabric_row(x, fab_code, batch, rank):
    '''
    One (fab_code, batch) pick -> the stored row. Columns and the raw blob are
    written from the same dict in one place so the denormalised copies cannot
    drift.

    image_url / thumbnail are stripped from the raw blob: the row records which
    batch the swatch comes from and the URLs are rebuilt from it on read, so a
    re-photographed lot shows up without rewriting anything here.

    The batch's own colour is taken from the matching entry in `matched_colors`
    when the recommendation was colour-matched, else from the matching entry in
    `colors`. Denormalised deliberately: the board renders a tile per lot, and
    looking each one up in Surplus Stock on every read would be a query per tile.
    '''
    raw = {k: v for k, v in x.items()
           if k not in ('image_url', 'thumbnail', 'imageUrl', 'selected')}
    detail = _batch_detail(x, batch)
    return {
        'fab_code': fab_code,
        'batch_color': detail.get('color'),
        'batch_shade_category': detail.get('shade_category'),
        'palette_id': detail.get('palette_id'),
        'palette_name': detail.get('palette_name'),
        'has_image': 1 if detail.get('has_image') else 0,
        'selected': 1 if x.get('selected') else 0,
        'source': _surplus_source(x),
        'rank': rank,
        'match_score': _int_or_none(_pick(x, 'match_score', 'matchScore')),
        'match_source': _pick(x, 'match_source', 'matchSource'),
        'matched_brief_index': _int_or_none(_pick(x, 'matched_brief_index', 'matchedBriefIndex')),
        'matched_brief_fabric': _pick(x, 'matched_brief_fabric', 'matchedBriefFabric'),
        'best_for': _pick(x, 'best_for', 'bestFor'),
        'reason': x.get('reason'),
        'caveats': _dump(x.get('caveats') or []),
        'quality_label': _pick(x, 'quality_label', 'qualityLabel'),
        'composition': x.get('composition'),
        'composition_label': _pick(x, 'composition_label', 'compositionLabel'),
        'gsm': _int_or_none(x.get('gsm')),
        'available': x.get('available'),
        'available_uom': _pick(x, 'available_uom', 'availableUom'),
        'price_per_uom': _pick(x, 'price_per_uom', 'pricePerUom'),
        'color': x.get('color'),
        'batch': batch,
        'colors': _dump(x.get('colors') or []),
        'recommendation': _dump(raw),
    }


def _batch_detail(x, batch):
    '''
    What the recommendation already knows about one of its batches.

    `matched_colors` is preferred over `colors` because only it carries the brief
    palette colour this lot was chosen to serve — the label the tile should show,
    since the mill's own colour text is raw and often truncated. Both are already
    in the payload the client is echoing back, so this costs no lookup.

    An empty result is fine and expected: a fabric picked on construction alone,
    or a batch the client chose outside the recommender's colourways, simply has
    no colour to denormalise.
    '''
    for entry in _as_list(x.get('matched_colors') or x.get('matchedColors')):
        entry = _as_dict(entry)
        if _clean_str(entry.get('batch')) == batch:
            return {
                'color': entry.get('color'),
                'shade_category': _pick(entry, 'shade_category', 'shadeCategory'),
                'palette_id': _pick(entry, 'palette_id', 'paletteId'),
                'palette_name': _pick(entry, 'palette_name', 'paletteName'),
                'has_image': _pick(entry, 'has_image', 'hasImage'),
            }
    for entry in _as_list(x.get('colors')):
        entry = _as_dict(entry)
        if _clean_str(entry.get('batch')) == batch:
            return {
                'color': entry.get('color'),
                'shade_category': _pick(entry, 'shade_category', 'shadeCategory'),
                'has_image': _pick(entry, 'has_image', 'hasImage'),
            }
    return {}


_SURPLUS_SOURCES = ('recommended', 'library')


def _surplus_source(x):
    '''
    Where the fabric came from. The client should send it, but the two shapes are
    self-identifying — a catalogue() group carries no match/brief attribution while
    a recommend() one always does — so an unset or unknown value is inferred rather
    than rejected. Keeps a library pick from reading as a 0-scored recommendation.
    '''
    declared = _clean_str(x.get('source'))
    if declared in _SURPLUS_SOURCES:
        return declared
    scored = _pick(x, 'match_score', 'matchScore') is not None
    return 'recommended' if scored or x.get('match_source') else 'library'


@frappe.whitelist(allow_guest=True, methods=['POST'])
@auth_required
def set_surplus_fabric_selection(id=None, selections=None, fab_codes=None):
    '''
    Set exactly which batches are selected, without the client having to
    round-trip the whole payload. Selection toggling is the common action in the
    step, and a full sync per click would rewrite every row (and risk dropping a
    recoloured swatch's row if the client's copy were stale).

    `selections` is the complete list of selected batches — anything absent is
    deselected:

        [{"fab_code": "1600006733", "batch": "2000357273"}, ...]

    `fab_codes` is the older whole-fabric form, kept working: a bare fab code
    selects every batch of it on the board. Useful for "take this fabric" and for
    clients written before the board became batch-centric. The two can be sent
    together; a fab code listed there wins over its individual batches.

    Returns the updated rows.
    '''
    doc = _get_or_404(id)
    _assert_editor(doc)

    whole = {c for c in (_clean_str(v) for v in _as_list(fab_codes)) if c}
    pairs = set()
    for entry in _as_list(selections):
        entry = _as_dict(entry)
        code = _clean_str(_pick(entry, 'fab_code', 'fabCode'))
        batch = _clean_str(entry.get('batch'))
        if not code:
            continue
        if batch:
            pairs.add((code, batch))
        else:
            # A selection naming only a fab code means the whole fabric, the same
            # as sending it in `fab_codes`.
            whole.add(code)

    for row in (doc.surplus_fabrics or []):
        row.selected = 1 if (row.fab_code in whole
                             or (row.fab_code, row.batch) in pairs) else 0

    _touch_if_published(doc)
    doc.save(ignore_permissions=True)
    _log_edit(doc, 'Surplus Fabrics Updated',
              details={'selected': sorted(f'{c}/{b}' for c, b in pairs),
                       'selectedFabrics': sorted(whole)})
    frappe.db.commit()
    return _surplus_fabrics_payload(doc)


@frappe.whitelist(allow_guest=True, methods=['POST'])
@auth_required
def sync_surplus_recolours(id=None, fab_code=None, recolours=None):
    '''
    Replace the recoloured swatches for ONE fab code, leaving every other fab
    code's recolours — and the recommendations themselves — untouched. Scoped
    rather than global so the frontend's recolour panel owns exactly the set it
    is showing.

    Each item: {name, pantone, hex, image, sourceBatch?, selected?, meta?}, where
    `image` is a base64 data URL (staged here, offloaded to S3 by the board's
    before_save) or an already-stored URL, which passes straight through so
    re-syncing an unchanged list does not re-upload anything.

    Passing an empty list clears that fab code's recolours. Returns the updated
    recolours for this fab code only.
    '''
    doc = _get_or_404(id)
    _assert_editor(doc)
    fab_code = _clean_str(fab_code)
    if not fab_code:
        _bad_request('fab_code is required')

    kept = [r for r in (doc.surplus_recolours or []) if r.fab_code != fab_code]
    incoming = []
    for x in _as_list(recolours):
        x = _as_dict(x)
        image = _save_recolour_image(x.get('image'))
        if not image:
            # `image` is mandatory on the child doctype; skipping gives the client
            # a no-op instead of a validation traceback on a half-filled row.
            continue
        incoming.append({
            'fab_code': fab_code,
            'color_name': _pick(x, 'name', 'color_name', 'colorName'),
            'pantone': x.get('pantone'),
            'hex_code': _hex_code(_pick(x, 'hex', 'hex_code', 'hexCode')),
            'image': image,
            'selected': 1 if x.get('selected') else 0,
            'source_batch': _pick(x, 'sourceBatch', 'source_batch'),
            # Stored as `render_meta`, sent and received as `meta`: a column named
            # `meta` is a frappe reserved keyword and breaks every save of the
            # board (see the field's description on the child doctype).
            'render_meta': _dump(x.get('meta')),
        })

    # A child table is set as a unit, so the whole table is rebuilt: the other fab
    # codes' rows are re-set as their existing child documents (which keeps their
    # row ids, and cannot drop a column a later migration adds), then this fab
    # code's new set is appended.
    doc.set('surplus_recolours', kept)
    for r in incoming:
        doc.append('surplus_recolours', r)

    _touch_if_published(doc)
    doc.save(ignore_permissions=True)   # before_save -> offload_board pushes to S3
    _log_edit(doc, 'Surplus Recolours Updated', details={'fabCode': fab_code, 'count': len(incoming)})
    frappe.db.commit()
    selected = _selected_batches_by_code(doc).get(fab_code) or set()
    return [_surplus_recolour_obj(r, selected) for r in (doc.surplus_recolours or [])
            if r.fab_code == fab_code]


def _save_recolour_image(value):
    '''
    Stage a recoloured swatch. util.save_file raises ValueError on the size cap;
    surface that as a 400 rather than letting it become a 500, since a 30 MB
    recolour output is a client-fixable problem.
    '''
    if not value or not isinstance(value, str):
        return None
    if not value.startswith('data:'):
        return value    # already stored (local path or S3 url) — no re-upload
    try:
        return util.save_file(value, MAX_IMAGE_FILE_SIZE_MB)
    except ValueError as ex:
        _bad_request(str(ex))


def _hex_code(value):
    ''' Normalise a hex colour to '#RRGGBB' upper-case, or None. Anything that is
        not a 3/6-digit hex is kept verbatim — the recolour source is trusted to
        know its own colour space, and rejecting here would lose the swatch. '''
    s = _clean_str(value)
    if not s:
        return None
    body = s[1:] if s.startswith('#') else s
    if len(body) in (3, 6) and all(c in '0123456789abcdefABCDEF' for c in body):
        return '#' + body.upper()
    return s


# =====================================================================
# Write — project brief attachments
# =====================================================================

@frappe.whitelist(allow_guest=True, methods=['POST'])
@auth_required
def sync_inspiration_images(id=None, images=None):
    ''' Replace the project brief's inspiration images (uploads -> S3 on save). '''
    return _sync_image_table(id, 'inspiration_images', images, 'Inspiration Images Updated')


@frappe.whitelist(allow_guest=True, methods=['POST'])
@auth_required
def sync_customer_brief(id=None, files=None):
    '''
    Replace the customer brief attachments. Accepts PDF / PPT / Excel / images up
    to 20 MB each; anything else is rejected outright rather than silently dropped,
    since a missing brief document is worth an error the user can see.

    Each item: {file, fileName}. `file` is a base64 data URL (staged, then offloaded
    to S3) or an already-stored URL, which passes through untouched so re-syncing an
    unchanged list doesn't re-upload anything.
    '''
    doc = _get_or_404(id)
    _assert_editor(doc)
    doc.set('customer_brief', [])
    for x in _as_list(files):
        x = _as_dict(x)
        raw = x.get('file')
        if not raw:
            continue
        name = (x.get('fileName') or x.get('file_name') or '').strip()
        url, name, ext, size = _save_brief_file(raw, name, x)
        doc.append('customer_brief', {
            'file': url,
            'file_name': name,
            'file_type': ext.lstrip('.'),
            'file_size': size,
        })
    _touch_if_published(doc)
    doc.save(ignore_permissions=True)   # before_save -> offload_board pushes to S3
    _log_edit(doc, 'Customer Brief Updated')
    frappe.db.commit()
    return [_brief_file_obj(r) for r in (doc.customer_brief or [])]


def _save_brief_file(raw, name, payload):
    '''
    Resolve one brief attachment to (url, file_name, ext, size_bytes).

    The extension is taken from the client's fileName, falling back to the data
    URL's mime type — util.save_file defaults to '.png' for anything it can't map,
    which would mislabel a .pptx, so the name matters here.
    '''
    ext = _file_ext(name)
    if not ext and isinstance(raw, str) and raw.startswith('data:'):
        mime = raw.split(':', 1)[1].split(';', 1)[0]
        ext = (mimetypes.guess_extension(mime) or '').lower()
    if ext not in BRIEF_FILE_EXTENSIONS:
        _bad_request(
            f'Unsupported file type "{ext or name or "unknown"}". '
            f'Allowed: {", ".join(e.lstrip(".") for e in BRIEF_FILE_EXTENSIONS)}')

    if not isinstance(raw, str) or not raw.startswith('data:'):
        # Already stored (local path or S3 url) — keep it and trust the metadata
        # the client round-tripped from a previous read.
        size = payload.get('fileSize') or payload.get('file_size')
        return raw, name or None, ext, frappe.utils.cint(size) or None

    if not name:
        name = f'brief_{frappe.generate_hash()[:8]}{ext}'
    try:
        url = util.save_file(raw, MAX_BRIEF_FILE_SIZE_MB, file_name=name)
    except ValueError as ex:
        # save_file raises on the size cap; surface it as a 400, not a 500.
        _bad_request(str(ex))
    size = frappe.db.get_value('File', {'file_url': url}, 'file_size')
    return url, name, ext, frappe.utils.cint(size) or None


def _file_ext(name):
    ''' Lower-cased extension incl. the dot, or '' — mirrors cloud.file_ext. '''
    if not name or '.' not in name:
        return ''
    return '.' + name.rsplit('.', 1)[1].strip().lower()


# =====================================================================
# Write — direction & inspiration
# =====================================================================

# Each of these replaces the whole table, mirroring sync_fabrics / sync_garments:
# the client owns the ordering and sends the full list back. Keys are accepted in
# camelCase or snake_case, as elsewhere in this module.

@frappe.whitelist(allow_guest=True, methods=['POST'])
@auth_required
def sync_print_direction(id=None, images=None):
    '''
    Replace the print direction images (uploads -> S3 on save).

    Each row also carries `selected` (default True). The client sends every row
    on every sync, deselected ones included — this replaces the whole table, so
    filtering out deselected rows here would delete them instead of just
    deselecting them. Only an explicit `false` deselects a row; missing or null
    is "not set" and reads back as selected, same as a row written before this
    field existed.
    '''
    doc = _get_or_404(id)
    _assert_editor(doc)
    doc.set('print_direction', [])
    for x in _as_list(images):
        x = _as_dict(x)
        image = _save_image(x.get('image'))
        if not image:
            # `image` is mandatory; skipping here gives the client a no-op
            # instead of a validation traceback on a half-filled row.
            continue
        doc.append('print_direction', {
            'image': image,
            'label': x.get('label'),
            'selected': 0 if x.get('selected') is False else 1,
        })
    _touch_if_published(doc)
    doc.save(ignore_permissions=True)   # before_save -> offload_board pushes to S3
    _log_edit(doc, 'Print Direction Updated')
    frappe.db.commit()
    return [_print_direction_obj(r) for r in (doc.print_direction or [])]


def _sync_image_table(id, field, images, log_action):
    doc = _get_or_404(id)
    _assert_editor(doc)
    doc.set(field, [])
    for x in _as_list(images):
        x = _as_dict(x)
        image = _save_image(x.get('image'))
        if not image:
            # `image` is mandatory on both child doctypes; skipping here gives the
            # client a no-op instead of a validation traceback on a half-filled row.
            continue
        doc.append(field, {'image': image, 'label': x.get('label')})
    _touch_if_published(doc)
    doc.save(ignore_permissions=True)   # before_save -> offload_board pushes to S3
    _log_edit(doc, log_action)
    frappe.db.commit()
    return [_inspiration_image_obj(r) for r in (doc.get(field) or [])]


@frappe.whitelist(allow_guest=True, methods=['POST'])
@auth_required
def sync_garment_inspiration(id=None, items=None):
    '''
    Replace the garment inspiration list. `items` is stored verbatim as JSON — the
    shape prism.api.garment_matching.match_garments returns (id, brand, productName,
    category, ..., matchRationale) — rather than a fixed child-table row, since
    matched garments carry far more fields than a table schema would want to pin
    down. `id` must name a real Brand Master Data record; an item that fails this
    is dropped rather than failing the whole sync, so a stale reference can't
    block the save.
    '''
    doc = _get_or_404(id)
    _assert_editor(doc)

    kept = []
    for x in _as_list(items):
        x = _as_dict(x)
        gid = x.get('id')
        if gid and frappe.db.exists('Brand Master Data', gid):
            kept.append(x)

    doc.garment_inspiration = _dump(kept)
    _touch_if_published(doc)
    doc.save(ignore_permissions=True)
    _log_edit(doc, 'Garment Inspiration Updated')
    frappe.db.commit()
    return _parse(doc.garment_inspiration, [])


# =====================================================================
# Write — versions / messages
# =====================================================================

@frappe.whitelist(allow_guest=True, methods=['POST'])
@auth_required
def add_version(id=None, version=None):
    doc = _get_or_404(id)
    _assert_editor(doc)
    v = _as_dict(version)
    vd = frappe.new_doc('Moodboard Version')
    vd.moodboard = doc.name
    vd.version_key = v.get('versionKey') or v.get('id')
    vd.parent_version = v.get('parentVersion')
    vd.prompt = v.get('prompt')
    vd.llm_prompt = _dump(v.get('llmPrompt'))
    vd.ai_reply = v.get('aiReply')
    vd.image = _save_image(v.get('image'))
    vd.edited_image = _save_image(v.get('editedImage'))
    vd.canvas_state = _dump_with_images(v.get('canvasState'))
    vd.image_model = v.get('imageModel')
    vd.image_size = v.get('imageSize')
    vd.generation_time = v.get('generationTime')
    vd.is_showable = 1 if v.get('isShowable', True) else 0
    vd.response_id = v.get('responseId')
    vd.insert(ignore_permissions=True)
    _touch_if_published(doc, save=True)
    _log_edit(doc, 'Version Added')
    frappe.db.commit()
    return _version_obj(vd.as_dict())


@frappe.whitelist(allow_guest=True, methods=['POST'])
@auth_required
def update_version(version_id=None, payload=None):
    ''' PATCH a version (e.g. edited_image, canvas_state). '''
    vd = _get_doc_or_404('Moodboard Version', version_id)
    doc = _get_or_404(vd.moodboard)
    _assert_editor(doc)
    p = _as_dict(payload)
    if 'llmPrompt' in p:
        vd.llm_prompt = _dump(p.get('llmPrompt'))
    if 'generationTime' in p:
        vd.generation_time = p.get('generationTime')
    if 'editedImage' in p:
        vd.edited_image = _save_image(p.get('editedImage'))
    if 'editedAt' in p:
        vd.edited_at = _dt(p.get('editedAt'))
    if 'canvasState' in p:
        vd.canvas_state = _dump_with_images(p.get('canvasState'))
    if 'isShowable' in p:
        vd.is_showable = 1 if p.get('isShowable') else 0
    vd.save(ignore_permissions=True)
    _touch_if_published(doc, save=True)
    _log_edit(doc, 'Version Updated')
    frappe.db.commit()
    return _version_obj(vd.as_dict())


@frappe.whitelist(allow_guest=True, methods=['POST'])
@auth_required
def sync_version_images(version_id=None, images=None):
    '''
    Replace a moodboard version's reference-image gallery (the `reference_images`
    child table, one Moodboard Version Image row per image). Same shape as
    sync_inspiration_images, but scoped to a version instead of the board.

    Each item: {image}. `image` is a base64 data URL (staged here, offloaded to
    S3 by the version's before_save -- see moodboard_media.offload_version) or an
    already-stored URL, which passes straight through so re-syncing an unchanged
    list does not re-upload anything. Passing an empty list clears the gallery.
    Returns the version's updated referenceImages.
    '''
    vd = _get_doc_or_404('Moodboard Version', version_id)
    doc = _get_or_404(vd.moodboard)
    _assert_editor(doc)

    vd.set('reference_images', [])
    for idx, x in enumerate(_as_list(images), start=1):
        x = _as_dict(x)
        image = _save_image(x.get('image'))
        if not image:
            # `image` is mandatory on the child doctype; skip a half-filled row
            # instead of failing the whole sync on it.
            continue
        vd.append('reference_images', {'image': image, 'display_order': idx})

    vd.save(ignore_permissions=True)   # before_save -> offload_version pushes to S3
    _touch_if_published(doc, save=True)
    _log_edit(doc, 'Version Reference Images Updated', details={'versionId': version_id})
    frappe.db.commit()
    return _version_reference_images_of(vd)


@frappe.whitelist(allow_guest=True, methods=['POST'])
@auth_required
def delete_version_image(version_id=None, image_id=None, url=None):
    '''
    Remove a single image from a version's reference-image gallery, matched by
    its row id (`image_id`, from `referenceImages[].id`) or, failing that, its
    stored URL. Best-effort deletes the underlying S3 object. Returns the
    version's updated referenceImages.
    '''
    vd = _get_doc_or_404('Moodboard Version', version_id)
    doc = _get_or_404(vd.moodboard)
    _assert_editor(doc)

    image_id = (image_id or '').strip()
    target = (url or '').strip()
    if not image_id and not target:
        _bad_request('`image_id` or `url` of the image to remove is required.')

    def _matches(r):
        if image_id:
            return r.name == image_id
        return (r.image or '').strip() == target or cloud.asset_url(r.image) == target

    rows = vd.reference_images or []
    removed = [r for r in rows if _matches(r)]
    kept = [r for r in rows if not _matches(r)]
    if not removed:
        frappe.throw('Image not found in this version\'s gallery.', frappe.DoesNotExistError)

    vd.set('reference_images', kept)
    vd.save(ignore_permissions=True)
    _touch_if_published(doc, save=True)
    _log_edit(doc, 'Version Reference Image Deleted', details={'versionId': version_id})
    frappe.db.commit()

    # Best-effort S3 cleanup — never block the removal on a bucket failure.
    for r in removed:
        key = cloud.asset_key(r.image)
        if not key:
            continue
        try:
            cloud.delete_object(key)
        except Exception:
            frappe.log_error(frappe.get_traceback(), 'delete_version_image S3 cleanup')

    return _version_reference_images_of(vd)


@frappe.whitelist(allow_guest=True, methods=['POST'])
@auth_required
def set_primary_version(id=None, version_id=None):
    doc = _get_or_404(id)
    _assert_editor(doc)
    updates = {'primary_version': version_id}
    if version_id:
        ver = frappe.db.get_value(
            'Moodboard Version', {'name': version_id, 'moodboard': id},
            ['edited_image', 'image'], as_dict=True)
        if not ver:
            _bad_request('Version not on this board')
        # Board thumbnail follows the primary version (prefer the edited image), but
        # stored as a compressed, aspect-preserved WebP in S3 — not the 4-7 MB
        # original — so list pages stay light.
        updates['thumbnail'] = media.make_and_store_thumbnail(doc.name, ver.edited_image or ver.image)
    if doc.status == 'Published':
        updates['status'] = 'Unpublished Changes'
    frappe.db.set_value(DOCTYPE, doc.name, updates, update_modified=True)
    _log_edit(doc, 'Primary Version Set')
    frappe.db.commit()
    return {'primaryVersion': version_id, 'thumbnail': updates.get('thumbnail')}


@frappe.whitelist(allow_guest=True, methods=['POST'])
@auth_required
def delete_version(version_id=None):
    vd = _get_doc_or_404('Moodboard Version', version_id)
    doc = _get_or_404(vd.moodboard)
    _assert_editor(doc)
    # S3 images are left in place (duplicate_moodboard may share the same object);
    # orphans are reclaimed later by a reference-aware sweep, not here.
    frappe.db.delete('Moodboard Version', {'name': version_id})
    _log_edit(doc, 'Version Deleted')
    frappe.db.commit()
    return {'success': True, 'id': version_id}


@frappe.whitelist(allow_guest=True, methods=['POST'])
@auth_required
def add_message(id=None, message=None):
    doc = _get_or_404(id)
    _assert_editor(doc)
    m = _as_dict(message)
    md = frappe.new_doc('Moodboard Message')
    md.moodboard = doc.name
    md.message_key = m.get('messageKey') or m.get('id')
    md.role = m.get('role')
    md.text = m.get('text')
    md.version = m.get('version')
    md.attachments = _dump_with_images(m.get('attachments'))
    md.insert(ignore_permissions=True)
    _log_edit(doc, 'Message Added')
    frappe.db.commit()
    return {'id': md.name}


# =====================================================================
# Publish lifecycle
# =====================================================================

@frappe.whitelist(allow_guest=True, methods=['POST'])
@auth_required
def publish(id=None, brand_ids=None):
    '''
    Freeze a snapshot of the normalized board, set status Published, stamp
    last_published_at, and ensure Brand Moodboard links.

    Requires a primary version. Boards used to exist only once an image had been
    generated, which made that guarantee implicit; now that they're created up-front
    it has to be enforced, or publish() freezes a snapshot with no primaryVersionImage
    and brands get a board with nothing to look at. (republish_all does NOT come
    through here, so already-published legacy boards are unaffected.)
    '''
    doc = _get_or_404(id)
    _assert_editor(doc)

    if not doc.primary_version:
        _bad_request('Choose a primary version before publishing')

    if brand_ids is not None:
        _set_brands(id, brand_ids)

    # Offload any still-local garment images to S3 first, so the frozen snapshot
    # captures the S3 urls (not local paths the save is about to move/delete).
    media.offload_board(doc)
    doc.published_snapshot = _dump(_build_snapshot(doc))
    doc.status = 'Published'
    doc.last_published_at = frappe.utils.now_datetime()
    doc.save(ignore_permissions=True)
    _log_edit(doc, 'Published')
    frappe.db.commit()

    # Cross-system Bell feed: ping every user of the brands this board reached.
    _notify_published(doc)

    return {'status': doc.status, 'lastPublishedAt': _iso(doc.last_published_at)}


def _notify_published(doc):
    '''
    Raise a Prism Notification for every user of the brands this board is linked
    to (Frappe -> prism-bot -> prism-web Bell). Recipients come from the board's
    current Brand Moodboard links, so it works whether or not publish() was
    called with an explicit brand_ids. Best-effort: a failure here must never
    break publish.
    '''
    try:
        import prism.api.notifications as notifications

        brands = frappe.get_all(
            'Brand Moodboard', filters={'moodboard': doc.name}, pluck='brand')
        recipients = set()
        for brand_id in brands:
            recipients |= set(
                frappe.get_all('Brand User', filters={'brand': brand_id}, pluck='user')
            )
        if not recipients:
            frappe.logger().info(
                f'moodboard_v2.publish: no brand users to notify for {doc.name}')
            return

        try:
            actor = util.get_current_user_id()
        except Exception:
            actor = None

        title = doc.moodboard_title or doc.name
        notifications.notify(
            recipients,
            event_type='moodboard_published',
            title=f'New moodboard published: {title}',
            body=title,
            deeplink=f'/moodboards-showcase/view?id={doc.name}',
            category='Moodboards',
            from_user=actor,
            ref_doctype='Moodboard',
            ref_name=doc.name,
        )
    except Exception:
        frappe.log_error(frappe.get_traceback(),
                         'moodboard_v2._notify_published()')


@frappe.whitelist(allow_guest=True, methods=['POST'])
@auth_required
def unpublish(id=None):
    ''' Take the board out of brand view (status -> Draft). Snapshot retained. '''
    doc = _get_or_404(id)
    _assert_editor(doc)
    doc.status = 'Draft'
    doc.save(ignore_permissions=True)
    _log_edit(doc, 'Unpublished')
    frappe.db.commit()
    return {'status': doc.status}


@frappe.whitelist(methods=['POST'])
def republish_all(ids=None, statuses=None, promote=0, limit=None):
    '''
    Admin/maintenance bulk refresh: re-freeze the CURRENT normalized state of boards
    into `published_snapshot` in the modern shape — the exact payload get_moodboard
    returns to brands (core + fabrics + garments + brand-safe styles + owner). Use
    after backfilling owners (or any bulk data change) so the frozen, brand-facing
    snapshots pick up current edits and the right owner.

    System Manager only, and deliberately NOT owner-scoped: one admin refreshes every
    board's snapshot regardless of who owns it (unlike publish(), which is owner-only
    via _assert_editor). No prism JWT required, so it also runs from bench and from the
    desk System Console:  frappe.call('prism.api.moodboard_v2.republish_all').

      ids:      explicit board names to refresh; default = all boards matching statuses.
      statuses: statuses to include; default ['Published', 'Unpublished Changes'].
                Draft boards carry no brand-facing snapshot, so they're always skipped.
      promote:  truthy -> also flip 'Unpublished Changes' to 'Published' and stamp
                last_published_at=now (a real republish that pushes live edits live).
                Falsy (default) -> snapshot-only refresh; status + last_published_at
                are left untouched.
      limit:    optional cap for batched runs.

    NOTE: the snapshot's owner comes straight from doc.owner — this does NOT invent
    owners. Set the owner column first, then run this to bake it into the snapshot.

    Per-board fault isolation + commit, so one bad board never aborts the sweep.
    Returns {total, ok, skipped, failed, errors}.
    '''
    frappe.only_for('System Manager')

    wanted = set(_as_list(statuses)) or {'Published', 'Unpublished Changes'}
    if ids:
        names = _as_list(ids)
    else:
        names = frappe.get_all(
            DOCTYPE, filters={'is_active': 1, 'status': ['in', list(wanted)]},
            pluck='name', order_by='creation asc', limit=limit)

    summary = {'total': len(names), 'ok': 0, 'skipped': 0, 'failed': 0, 'errors': []}
    for n in names:
        try:
            doc = _get_or_404(n)
            # Only snapshot-bearing boards; never resurrect a Draft into brand view.
            if doc.status not in ('Published', 'Unpublished Changes'):
                summary['skipped'] += 1
                continue
            media.offload_board(doc)  # local garment images -> S3 before freezing
            doc.published_snapshot = _dump(_build_snapshot(doc))
            if promote and doc.status == 'Unpublished Changes':
                doc.status = 'Published'
                doc.last_published_at = frappe.utils.now_datetime()
            doc.save(ignore_permissions=True)
            frappe.db.commit()
            summary['ok'] += 1
        except Exception as ex:
            frappe.db.rollback()
            summary['failed'] += 1
            summary['errors'].append({'moodboard': n, 'error': str(ex)})
            frappe.log_error(frappe.get_traceback(), f'republish_all {n}')
    return summary


# =====================================================================
# Media migration — backfill local /files images to S3
# =====================================================================

@frappe.whitelist(methods=['POST'])
def migrate_moodboard_media(id=None):
    '''
    Migrate ONE board's locally-stored images to S3 (versions, messages, garment
    cleaned images, the frozen snapshot, and a regenerated WebP thumbnail). For the
    gradual, one-by-one rollout — run it on a board, verify, then move on.

    System Manager only; no prism JWT required, so it also runs from bench / the
    System Console:  frappe.call('prism.api.moodboard_v2.migrate_moodboard_media', id='MB-...').
    Idempotent — values already on S3 are skipped, so re-running is safe.
    '''
    frappe.only_for('System Manager')
    if not id:
        _bad_request('id is required')
    _get_or_404(id)
    return media.migrate_moodboard(id)


@frappe.whitelist(methods=['POST'])
def regenerate_moodboard_thumbnail(id=None):
    '''
    "Upgrade Thumbnail Quality" for ONE board: rebuild a high-quality (LANCZOS +
    unsharp, larger) WebP thumbnail from its primary (or newest) version image,
    without re-running the full image migration. System Manager only. Returns the
    new thumbnail URL.
    '''
    frappe.only_for('System Manager')
    if not id:
        _bad_request('id is required')
    _get_or_404(id)
    url = media.regenerate_thumbnail(id)
    frappe.db.commit()
    if not url:
        return {'success': False, 'error': 'No version image to build a thumbnail from.'}
    return {'success': True, 'thumbnail': url}


@frappe.whitelist(methods=['POST'])
def regenerate_all_thumbnails(limit=None, ids=None, enqueue=0):
    '''
    "Upgrade ALL Thumbnails": rebuild EVERY active board's thumbnail (Draft +
    Published) at high quality, without re-uploading version images. Per-board
    fault isolation + commit.

      ids:     explicit board names; default = all active boards (oldest first).
      limit:   optional cap.
      enqueue: truthy -> run in a background worker and return {enqueued: True}.

    System Manager only. Returns {total, ok, skipped, failed, errors}.
    '''
    frappe.only_for('System Manager')

    if frappe.utils.cint(enqueue):
        frappe.enqueue(
            'prism.api.moodboard_v2.regenerate_all_thumbnails',
            queue='long', timeout=10800, limit=limit, ids=ids, enqueue=0,
        )
        return {'enqueued': True}

    names = _as_list(ids) if ids else frappe.get_all(
        DOCTYPE, filters={'is_active': 1}, pluck='name', order_by='creation asc', limit=limit)

    summary = {'total': len(names), 'ok': 0, 'skipped': 0, 'failed': 0, 'errors': []}
    for n in names:
        try:
            url = media.regenerate_thumbnail(n)
            summary['ok' if url else 'skipped'] += 1
            frappe.db.commit()
        except Exception as ex:
            frappe.db.rollback()
            summary['failed'] += 1
            summary['errors'].append({'moodboard': n, 'error': str(ex)})
            frappe.log_error(frappe.get_traceback(), f'regenerate_all_thumbnails {n}')
    return summary


@frappe.whitelist(allow_guest=True, methods=['POST'])
@auth_required
def migrate_moodboard_to_s3(id=None):
    '''
    Migrate ONE board's images to S3 and return every resulting URL. Offloads each
    version's image / edited_image (plus messages, garment images, and the frozen
    snapshot) from local /files to S3, regenerates the compressed WebP thumbnail,
    then returns the board thumbnail and per-version image URLs.

    Editor/owner (or admin) only, callable with the normal prism JWT — the
    self-serve, per-board path for the gradual rollout (no Desk/console needed).
    Idempotent: re-running just returns the current S3 URLs.
    '''
    doc = _get_or_404(id)
    _assert_editor(doc)

    summary = media.migrate_moodboard(id)

    versions = frappe.get_all(
        'Moodboard Version', filters={'moodboard': id},
        fields=['name', 'image', 'edited_image'],
        order_by='creation asc', ignore_permissions=True,
    )
    return {
        'moodboard': id,
        'thumbnail': frappe.db.get_value(DOCTYPE, id, 'thumbnail') or None,
        'versions': [
            {
                'id': v['name'],
                'image': v.get('image') or None,
                'editedImage': v.get('edited_image') or None,
            }
            for v in versions
        ],
        'summary': summary,
    }


@frappe.whitelist(methods=['POST'])
def migrate_all_moodboard_media(limit=None, ids=None, enqueue=0):
    '''
    Bulk sibling of migrate_moodboard_media — run the migration across many boards
    once the one-by-one runs check out. Per-board fault isolation: one bad board is
    logged and skipped, never aborting the sweep.

      ids:     explicit board names; default = all active boards (oldest first).
      limit:   optional cap for batched runs.
      enqueue: truthy -> run in a background worker (no web-request timeout) and
               return immediately with {enqueued: True}. Use this for the full run.

    System Manager only. Returns {total, ok, failed, errors, results} (or
    {enqueued: True} when enqueue is set).
    '''
    frappe.only_for('System Manager')

    if frappe.utils.cint(enqueue):
        # Hand the (potentially long) sweep to the long queue so a Desk button /
        # console call returns at once instead of hitting the request timeout.
        frappe.enqueue(
            'prism.api.moodboard_v2.migrate_all_moodboard_media',
            queue='long', timeout=10800, limit=limit, ids=ids, enqueue=0,
        )
        return {'enqueued': True}

    if ids:
        names = _as_list(ids)
    else:
        names = frappe.get_all(
            DOCTYPE, filters={'is_active': 1}, pluck='name',
            order_by='creation asc', limit=limit)

    summary = {'total': len(names), 'ok': 0, 'failed': 0, 'errors': [], 'results': []}
    for n in names:
        try:
            summary['results'].append(media.migrate_moodboard(n))
            summary['ok'] += 1
        except Exception as ex:
            frappe.db.rollback()
            summary['failed'] += 1
            summary['errors'].append({'moodboard': n, 'error': str(ex)})
            frappe.log_error(frappe.get_traceback(), f'migrate_all_moodboard_media {n}')
    return summary


# =====================================================================
# Manage — delete / duplicate
# =====================================================================

@frappe.whitelist(allow_guest=True, methods=['POST'])
@auth_required
def delete_moodboard(id=None):
    '''
    Hard-delete the board and all its related records (versions, messages, styles,
    brand links, comments). Owner only. Raw deletes on the dependents avoid the
    background-job queue (delete_dynamic_links) and the parent's linked-doc check.
    '''
    doc = _get_or_404(id)
    _assert_editor(doc)
    # S3 media is left in place (a duplicate board can share the same objects by
    # url); reclaim orphans later with a reference-aware sweep, not on delete.
    for dt in ('Moodboard Version', 'Moodboard Message', 'Moodboard Style',
               'Brand Moodboard', 'Moodboard Comment', 'Moodboard Collaborator',
               'Moodboard Edit Log'):
        frappe.db.delete(dt, {'moodboard': id})
    frappe.delete_doc('Moodboard', id, ignore_permissions=True, force=True)
    frappe.db.commit()
    return {'success': True, 'id': id}


@frappe.whitelist(allow_guest=True, methods=['POST'])
@auth_required
def duplicate_moodboard(id=None):
    '''
    Deep-copy a board into a fresh Draft owned by the caller: core + child tables
    (fabrics/garments) + versions + messages + styles (+cost) + brand assignments.
    Version/message links are remapped to the new versions. Returns the new board
    in the **list-item shape** (it's appended to the list; the row's onClick then
    calls get_moodboard).
    '''
    _require_internal()
    src = _get_or_404(id)
    user = _identity()[0]

    # Parent + its child tables (fabrics/garments) via copy_doc.
    new = frappe.copy_doc(src)
    new.moodboard_title = f"{src.moodboard_title} (Copy)"
    new.status = 'Draft'
    new.last_published_at = None
    new.published_snapshot = None
    new.primary_version = None
    new.insert(ignore_permissions=True)
    new_id = new.name

    # Versions (build old->new map, then remap parent_version + primary_version).
    vmap = {}
    for v in frappe.get_all('Moodboard Version', filters={'moodboard': id},
                            pluck='name', order_by='creation asc'):
        vd = frappe.copy_doc(frappe.get_doc('Moodboard Version', v))
        vd.moodboard = new_id
        vd.parent_version = None
        vd.insert(ignore_permissions=True)
        vmap[v] = vd.name
    for old_v, new_v in vmap.items():
        pv = frappe.db.get_value('Moodboard Version', old_v, 'parent_version')
        if pv and vmap.get(pv):
            frappe.db.set_value('Moodboard Version', new_v, 'parent_version', vmap[pv])
    if src.primary_version and vmap.get(src.primary_version):
        frappe.db.set_value('Moodboard', new_id, 'primary_version', vmap[src.primary_version])

    # Messages (remap version link).
    for m in frappe.get_all('Moodboard Message', filters={'moodboard': id},
                            pluck='name', order_by='creation asc'):
        md = frappe.copy_doc(frappe.get_doc('Moodboard Message', m))
        md.moodboard = new_id
        md.version = vmap.get(md.version)
        md.insert(ignore_permissions=True)

    # Styles (+ embedded cost).
    for s in frappe.get_all('Moodboard Style', filters={'moodboard': id},
                            pluck='name', order_by='idx asc'):
        sd = frappe.copy_doc(frappe.get_doc('Moodboard Style', s))
        sd.moodboard = new_id
        sd.insert(ignore_permissions=True)

    # Brand assignments.
    for b in frappe.get_all('Brand Moodboard', filters={'moodboard': id}, pluck='brand'):
        bm = frappe.new_doc('Brand Moodboard')
        bm.brand = b
        bm.moodboard = new_id
        bm.insert(ignore_permissions=True)

    new_doc = frappe.get_doc(DOCTYPE, new_id)
    _log_edit(new_doc, 'Created', details=f'duplicated from {id}')
    frappe.db.commit()
    return _list_obj(new_doc, user)


# =====================================================================
# Serializers
# =====================================================================

def _list_obj(doc, user):
    ''' Small list-item shape (matches list_moodboards items), built from a doc. '''
    return {
        'id': doc.name,
        'title': doc.moodboard_title,
        'thumbnail': doc.thumbnail or None,
        'brands': _brands_for([doc.name]).get(doc.name, []),
        'genders': _parse(doc.genders, []),
        'season': doc.season or None,
        'owner': _owner_obj(doc.owner),
        'access': 'editor' if _is_editor(doc) else 'viewer',
        'status': doc.status,
        'setupStage': doc.setup_stage or _DEFAULT_SETUP_STAGE,
        'isComplete': bool(doc.primary_version),
        'lastEditedAt': _iso(doc.modified),
        'lastPublishedAt': _iso(doc.last_published_at),
    }


def _core(doc, user):
    return {
        'id': doc.name,
        'title': doc.moodboard_title,
        'status': doc.status,
        # Where the wizard should resume, and whether the board is past the
        # in-progress stage (an image has been chosen).
        'setupStage': doc.setup_stage or _DEFAULT_SETUP_STAGE,
        'isComplete': bool(doc.primary_version),
        'isActive': bool(doc.is_active),
        'access': 'editor' if _is_editor(doc) else 'viewer',
        'owner': doc.owner,
        'season': doc.season or None,
        'creativityBias': doc.creativity_bias or None,
        'mood': doc.mood or None,
        'userVision': doc.user_vision or None,
        'brief': doc.brief or None,
        'inspirationImages': [_inspiration_image_obj(r) for r in (doc.inspiration_images or [])],
        'customerBrief': [_brief_file_obj(r) for r in (doc.customer_brief or [])],
        'imageModel': doc.image_model or None,
        'imageSize': doc.image_size or None,
        'thumbnail': doc.thumbnail or None,
        'primaryVersion': doc.primary_version or None,
        'genders': _parse(doc.genders, []),
        'styleCategories': _parse(doc.style_categories, []),
        'moodTags': _parse(doc.mood_tags, []),
        'colours': _parse(doc.colours, []),
        'alignmentReport': _parse(doc.alignment_report, None),
        'addons': _parse(doc.addons, None),
        'settings': {
            'fabricAutoSuggest': bool(doc.fabric_auto_suggest),
            'fabricAutoCount': doc.fabric_auto_count,
            'styleAutoSuggest': bool(doc.style_auto_suggest),
            'styleAutoCount': doc.style_auto_count,
            'variantCount': doc.variant_count,
        },
        'fabrics': [_fabric_obj(r) for r in (doc.fabrics or [])],
        'garments': [_garment_obj(r) for r in (doc.garments or [])],
        # Surplus recommendations, each carrying its recolours (joined by fab_code).
        'surplusFabrics': _surplus_fabrics_payload(doc),
        # Direction & inspiration — internal only; _build_snapshot strips these.
        'keywords': _parse(doc.keywords, []),
        'keyInsights': _parse(doc.key_insights, {}),
        'fabricDirection': _parse(doc.fabric_direction, None),
        'garmentInspiration': _parse(doc.garment_inspiration, []),
        'garmentDirectionSignals': _parse(doc.garment_direction_signals, None),
        'printDirection': [_print_direction_obj(r) for r in (doc.print_direction or [])],
        'printDirectionSignals': _parse(doc.print_direction_signals, None),
        'brands': _brands_for([doc.name]).get(doc.name, []),
        'lastEditedAt': _iso(doc.modified),
        'lastPublishedAt': _iso(doc.last_published_at),
        'createdAt': _iso(doc.creation),
    }


def _fabric_obj(r):
    '''
    One stored fabric row -> the shape moodboard_dyed_recommender.recommend()
    returned for it, plus board state.

    The raw blob is laid down first and the columns overlaid on top: the columns
    are the queryable index, the blob is the record, and a field the recommender
    grows lands in the response with no migration here. Rows written before the
    blob existed (get_matching_fabrics) simply have an empty one, and the overlay
    alone reproduces exactly the object they always returned — every key that
    shape carried is still emitted below.

    `id` comes from the blob when the Link column is empty, which happens when
    the catalogue row has been deleted since the recommendation was made (see
    _resolve_dyed_fabric). The client keeps addressing the fabric by the id it
    was given either way.

    image_url / thumbnail are plain public asset URLs rebuilt from `batch`, NOT
    presigned: a swatch URL that expires cannot be cached or persisted by the
    client, and the bucket serves these publicly anyway. They are rebuilt only
    where a swatch was actually photographed — a dyed URL can be constructed for
    any batch, so constructing one unconditionally would hand the client a link
    to nothing. The stored column is the fallback for legacy rows, which carry a
    URL and often no batch.
    '''
    o = _parse(r.recommendation, {})
    if not isinstance(o, dict):
        o = {}
    photographed = bool(r.has_image and r.batch)
    o.update({
        'id': r.fabric or o.get('id') or None,
        'selected': bool(r.selected),
        'source': r.source or 'recommended',
        'rank': r.rank,
        'source_id': r.source_id,

        'match_score': r.match_score,
        'match_source': r.match_source,
        'matched_brief_index': r.matched_brief_index,
        'matched_brief_fabric': r.matched_brief_fabric,
        'best_for': r.best_for,
        'reason': r.reason,
        'caveats': _parse(r.caveats, []),
        'fit_score': r.fit_score,
        'score_breakdown': _parse(r.score_breakdown, {}),

        'name': r.fabric_name,
        'code': r.code,
        'batch': r.batch,
        'description': r.description,
        'ai_description': r.ai_description,
        'custom_fabric_name': r.custom_fabric_name,

        'quality': r.quality,
        'quality_label': r.quality_label,
        'construction_family': r.construction_family,
        'texture_tags': _parse(r.texture_tags, []),
        'structure_ratio': r.structure_ratio,

        'blend': r.blend,
        'composition': r.composition,
        'composition_label': r.composition_label,
        'composition_pct': _parse(r.composition_pct, {}),
        'gsm': r.gsm,

        'color': r.color,
        'shade': r.shade,
        'shade_category': r.shade_category,
        'finish': r.finish,

        'matched_palette_id': r.matched_palette_id or None,
        'matched_palette_name': r.matched_palette_name or None,
        'needs_dyeing': bool(r.needs_dyeing),

        'price_from_fabric_masters': r.price_from_fabric_masters,
        'closest_fabric_master': r.closest_fabric_master,

        'variant_count': r.variant_count,
        'variant_codes': _parse(r.variant_codes, []),

        'has_image': photographed,
        'image_url': (cloud.fabric_image_url(r.batch) if photographed
                      else (r.image_url or None)),
        'thumbnail': cloud.fabric_thumbnail_url(r.batch) if photographed else None,
        'relevancy_score': r.relevancy_score,
    })
    return o


def _garment_obj(r):
    '''
    Garment child object — emitted entirely from the stored row (the full
    get_matching_garments payload is persisted). Keys mirror get_matching_garments
    + selected + source_id + cleaned images.
    '''
    return {
        'id': r.garment or None,
        'selected': bool(r.selected),
        'source_id': r.source_id,
        'gsr_no': r.gsr_no,
        'garment_name': r.garment_name,
        'gender': r.gender,
        'product_category': r.product_category,
        'fabric_quality': r.fabric_quality,
        'fabric_blend': r.fabric_blend,
        'element_colour': r.element_colour,
        'finished_gsm': r.finished_gsm,
        'image_urls': _parse(r.image_urls, {}),
        'relevancy_score': r.relevancy_score,
        'cleaned_front_image': r.cleaned_front_image or None,
        'cleaned_back_image': r.cleaned_back_image or None,
    }


def _surplus_fabrics_payload(doc):
    '''
    The surplus block as the client reads it: one entry per (fab_code, batch),
    each carrying the recolours for its fab code.

    Recolours are shared across a fab code's batches by design, so two selected
    batches of one fabric both report the same recolour list. That is the truth —
    a recolour is a render of the quality, not of a lot — and `stale` on each
    tells the client which lot it was actually taken over.
    '''
    selected = _selected_batches_by_code(doc)
    by_code = {}
    for r in (doc.surplus_recolours or []):
        by_code.setdefault(r.fab_code, []).append(
            _surplus_recolour_obj(r, selected.get(r.fab_code) or set()))
    return [_surplus_fabric_obj(r, by_code.get(r.fab_code, []))
            for r in (doc.surplus_fabrics or [])]


def _selected_batches_by_code(doc):
    ''' fab code -> the set of its batches the user actually put on the board.
        What a recolour's `source_batch` is judged against. '''
    selected = {}
    for r in (doc.surplus_fabrics or []):
        if r.selected and r.batch:
            selected.setdefault(r.fab_code, set()).add(r.batch)
    return selected


def _surplus_fabric_obj(r, recolours=None):
    '''
    One stored (fab_code, batch) row -> the shape surplus_recommender.recommend()
    returned for its fab code, narrowed to this batch, plus board state.

    The raw blob is laid down first and the columns overlaid on top: the columns
    are the queryable index, the blob is the record, and a field the recommender
    grows lands in the response with no migration here.

    The overlay is what makes the row batch-centric. The blob was written for the
    whole fab code and still carries ITS hero and ITS colour; every field that
    describes a lot is replaced here by this row's own, so a tile can never show
    one lot's swatch over another lot's colour name. `hero_color` /
    `hero_shade_category` keep their names — they now mean "this row's batch" —
    so a client that mapped them before the board became batch-centric keeps
    working.

    image_url / thumbnail are plain public asset URLs, NOT presigned: a swatch
    URL that expires cannot be cached or persisted by the client, and the bucket
    serves these publicly anyway.
    '''
    o = _parse(r.recommendation, {})
    if not isinstance(o, dict):
        o = {}
    o.update({
        'fab_code': r.fab_code,
        'selected': bool(r.selected),
        'source': r.source or 'recommended',
        'rank': r.rank,
        'match_score': r.match_score,
        'match_source': r.match_source,
        'matched_brief_index': r.matched_brief_index,
        'matched_brief_fabric': r.matched_brief_fabric,
        'best_for': r.best_for,
        'reason': r.reason,
        'caveats': _parse(r.caveats, []),
        'quality_label': r.quality_label,
        'composition': r.composition,
        'composition_label': r.composition_label,
        'gsm': r.gsm,
        'available': r.available,
        'available_uom': r.available_uom,
        'price_per_uom': r.price_per_uom,
        'color': r.color,
        'batch': r.batch,
        'hero_color': r.batch_color,
        'hero_shade_category': r.batch_shade_category,
        'palette_id': r.palette_id or None,
        'palette_name': r.palette_name or None,
        'has_image': bool(r.has_image),
        'image_url': cloud.fabric_image_url(r.batch),
        'thumbnail': cloud.fabric_thumbnail_url(r.batch),
        'colors': _parse(r.colors, []),
        'recolours': recolours or [],
    })
    # The recommender's own group-level hero has no meaning once a row IS a lot,
    # and leaving it in the blob would hand the client a second, contradictory
    # batch to render from.
    for key in ('auto_batch', 'hero_batch', 'hero_source', 'hero_palette_id'):
        o.pop(key, None)
    return o


def _surplus_recolour_obj(r, selected_batches=None):
    '''
    Recoloured swatch row. `image` is the stored S3 url (or, briefly between the
    sync and the board's before_save, the staged /files path).

    `stale` says the swatch was rendered over a lot that is no longer on the
    board — the user has since dropped that batch and kept others. The recolour
    survives, because the render is real and expensive, but it was taken over a
    different lot's texture and weave, so the UI has to be able to say so and
    offer a re-render.

    Judged against the fab code's SELECTED batches, not against one hero: the
    board can now carry several lots of a fabric, and a recolour made over any of
    them is still current. Never true when the source batch is unknown (an older
    recolour) or when nothing of that fabric is selected — unknowable is not
    stale, and neither is "not on the board at all".
    '''
    selected_batches = selected_batches or set()
    return {
        'id': r.name,
        'fabCode': r.fab_code,
        'name': r.color_name or None,
        'pantone': r.pantone or None,
        'hex': r.hex_code or None,
        'image': r.image or None,
        'selected': bool(r.selected),
        'sourceBatch': r.source_batch or None,
        'selectedBatches': sorted(selected_batches),
        'stale': bool(r.source_batch and selected_batches
                      and r.source_batch not in selected_batches),
        'originalImage': _fabric_signed_url(r.source_batch, cloud.fabric_image_signed_url),
        'meta': _parse(r.render_meta, None),
    }


def _fabric_signed_url(batch, signer):
    ''' Presign a stock swatch, best-effort: a missing batch or an S3 hiccup must
        degrade to no image rather than fail the whole board read. '''
    if not batch:
        return None
    try:
        return signer(batch)
    except Exception:
        frappe.log_error(frappe.get_traceback(), 'moodboard_v2._fabric_signed_url')
        return None


def _inspiration_image_obj(r):
    ''' Uploaded-image row (Inspiration Images). '''
    return {
        'id': r.name,
        'image': r.image or None,
        'label': r.label or None,
    }


def _reference_image_rows(rows):
    '''
    Shared serializer for a version's reference-image gallery: sorted ascending
    by displayOrder, each row mapped to {id, url, displayOrder} (url via
    asset_url — idempotent, full URLs pass through, bare keys resolve). Accepts
    either child Document rows (from a loaded Moodboard Version doc) or plain
    dicts (from a bulk frappe.get_all query, see list_versions), whichever the
    caller had on hand.
    '''
    def g(row, key):
        return row.get(key) if isinstance(row, dict) else getattr(row, key, None)

    items = [r for r in (rows or []) if g(r, 'image')]
    items.sort(key=lambda r: ((g(r, 'display_order') or 0), g(r, 'idx') or 0))
    return [
        {'id': g(r, 'name'), 'url': cloud.asset_url(g(r, 'image')), 'displayOrder': g(r, 'display_order') or 0}
        for r in items
    ]


def _version_reference_images_of(vd):
    ''' A loaded Moodboard Version doc's reference-image gallery. See _reference_image_rows. '''
    return _reference_image_rows(vd.get('reference_images'))


def _print_direction_obj(r):
    '''
    Print reference row. `selected` defaults True — a row synced without the key,
    or a pre-existing row from before this field existed (where the column reads
    back None instead of 0/1), is treated as selected rather than deselected.
    '''
    return {
        'id': r.name,
        'image': r.image or None,
        'label': r.label or None,
        'selected': r.get('selected') != 0,
    }


def _brief_file_obj(r):
    ''' Customer brief attachment. `fileName` is the name as uploaded — the stored
        object is hash-named, so the UI needs this to label the download. '''
    return {
        'id': r.name,
        'file': r.file or None,
        'fileName': r.file_name or None,
        'fileType': r.file_type or None,
        'fileSize': r.file_size or None,
    }


def _build_snapshot(doc):
    ''' Frozen published view: core + brand-safe styles + primary version image,
        minus the internal-only direction & inspiration block. '''
    snap = _core(doc, doc.owner)
    for key in _SNAPSHOT_INTERNAL_KEYS:
        snap.pop(key, None)
    # Rejected surplus candidates are internal working material — a brand sees the
    # fabrics that were chosen for their board, not the ones that were passed over
    # (and not the stock depth / pricing of the rest of the surplus catalogue).
    snap['surplusFabrics'] = [f for f in snap.get('surplusFabrics') or [] if f.get('selected')]
    snap['styles'] = [_brand_style(s) for s in ms.list_styles(moodboard=doc.name)]
    if doc.primary_version:
        snap['primaryVersionImage'] = frappe.db.get_value('Moodboard Version', doc.primary_version, 'image')
    return snap


def _overlay_live_media(doc, snap):
    '''
    Option A — read-time media overlay. The published snapshot stays frozen for
    the *set* of styles and their costs (the brand sign-off), but each style's
    visual assets (image + model3d/GLB) are refreshed from the live Moodboard
    Style rows. This lets an image or GLB added/replaced after publish show to
    brands without a republish.

    Only styles still present live are refreshed (matched by id); a style that
    was deleted/re-extracted keeps its frozen asset. New styles added after
    publish are intentionally NOT introduced here — that remains a publish action.
    '''
    if not isinstance(snap, dict):
        return snap
    styles = snap.get('styles')
    if not isinstance(styles, list) or not styles:
        return snap

    live = {s.get('id'): s for s in ms.list_styles(moodboard=doc.name)}
    for st in styles:
        cur = live.get(st.get('id'))
        if cur:
            st['image'] = cur.get('image')
            st['model3d'] = cur.get('model3d')
            st['garmentImages'] = cur.get('garmentImages')
            st['productVideo'] = cur.get('productVideo')
            st['esg'] = cur.get('esg')
    return snap


def _brand_style(s):
    '''
    Brand-safe extracted style for the published snapshot: the basic style fields +
    attrs, with cost reduced to currency + fxRate only (internal price breakdown is
    never exposed to brands). Feeds the brand moodboard styles section + sample drawer.
    '''
    cost = s.get('cost') or None
    return {
        'id': s.get('id'),
        'createdAt': s.get('createdAt'),
        'image': s.get('image'),
        'model3d': s.get('model3d'),
        'garmentImages': s.get('garmentImages'),
        'productVideo': s.get('productVideo'),
        'include': s.get('include'),
        'moq': s.get('moq'),
        'attrs': s.get('attrs') or {},
        'cost': {'currency': cost.get('currency'), 'fxRate': cost.get('fxRate')} if cost else None,
        'esg': s.get('esg'),
    }


def _version_obj(r):
    g = r.get if isinstance(r, dict) else (lambda k: getattr(r, k, None))
    return {
        'id': g('name'), 'versionKey': g('version_key'), 'parentVersion': g('parent_version'),
        'prompt': g('prompt'), 'llmPrompt': _parse(g('llm_prompt'), None), 'aiReply': g('ai_reply'),
        'image': g('image'), 'editedImage': g('edited_image'),
        'canvasState': _parse(g('canvas_state'), None),
        'referenceImages': _reference_image_rows(g('reference_images')),
        'imageModel': g('image_model'), 'imageSize': g('image_size'),
        'generationTime': g('generation_time'),
        'isShowable': bool(g('is_showable')), 'responseId': g('response_id'),
        'responseCreatedAt': _iso(g('response_created_at')), 'editedAt': _iso(g('edited_at')),
        'createdAt': _iso(g('creation')),
    }


# =====================================================================
# Patch / write helpers
# =====================================================================

def _apply_patch(doc, payload):
    '''
    Write only the keys present in payload (PATCH).

    Returns the payload keys that were NOT written. A PATCH stays permissive —
    an unrecognised key is skipped rather than failing the call — but callers
    surface the list as `ignoredKeys` so a misrouted key (a child table sent here
    instead of to its sync endpoint, or a plain typo) doesn't read as a success.
    '''
    for key, col in _PATCH_FIELDS.items():
        if key in payload:
            val = payload.get(key)
            if key == 'setupStage':
                # Reject unknown steps rather than writing a value the Select can't
                # hold (Frappe would silently keep it until the next Desk save).
                if val not in _SETUP_STAGES:
                    _bad_request(f'Unknown setup stage: {val}')
            if key == 'thumbnail':
                # Stage the upload, then store a compressed WebP thumbnail in S3
                # (aspect preserved) rather than the full-size image.
                val = media.make_and_store_thumbnail(doc.name, _save_image(val))
            doc.set(col, val)
    for key, col in _PATCH_JSON_FIELDS.items():
        if key in payload:
            doc.set(col, _dump(payload.get(key)))

    known = set(_PATCH_FIELDS) | set(_PATCH_JSON_FIELDS)
    return [k for k in payload if k not in known]


# Board data that is real but not writable through update_moodboard — each has its
# own endpoint. Named in the ignoredKeys warning so a caller sending one is told
# where it should have gone instead of getting a silent no-op.
_SYNC_ONLY_KEYS = {
    'fabrics': 'sync_fabrics',
    'garments': 'sync_garments',
    'surplusFabrics': 'sync_surplus_fabrics',
    'surplusRecolours': 'sync_surplus_recolours',
    'inspirationImages': 'sync_inspiration_images',
    'customerBrief': 'sync_customer_brief',
    'printDirection': 'sync_print_direction',
    'versions': 'add_version',
    'messages': 'add_message',
    'brands': 'set_brands',
    'collaborators': 'set_collaborators',
}


def _ignored_keys_report(ignored):
    ''' [{key, hint}] for the keys a patch skipped, or None when nothing was skipped. '''
    if not ignored:
        return None
    return [{
        'key': k,
        'hint': (f'not writable here — use {_SYNC_ONLY_KEYS[k]}()' if k in _SYNC_ONLY_KEYS
                 else 'unknown field — not written'),
    } for k in ignored]


def _touch_if_published(doc, save=False):
    if doc.status == 'Published':
        doc.status = 'Unpublished Changes'
        if save:
            frappe.db.set_value(DOCTYPE, doc.name, 'status', 'Unpublished Changes', update_modified=True)


def _resolve_fabric(x):
    return (frappe.db.get_value('Moodboard Dyed Fabric', {'code': x.get('code')}, 'name')
            or (frappe.db.get_value('Moodboard Dyed Fabric', {'batch': x.get('batch')}, 'name')
                if x.get('batch') else None))


# =====================================================================
# Identity / access
# =====================================================================

def _identity():
    return util.get_current_user_id(), util.get_current_brand()


# Admins are editors of EVERY board, regardless of owner. Resolved against the
# JWT session user (auth_required sets frappe.set_user). frappe.get_roles is
# request-cached, so calling this per row in a list is cheap.
_ADMIN_ROLES = ['Moodboard Admin', 'System Manager']

def _is_admin():
    return util.user_has_roles(_ADMIN_ROLES)


# --- collaborators / sharing -------------------------------------------------

def _collab_editor_ids(user):
    ''' Set of moodboard names the user may edit via an editor share (one query). '''
    if not user:
        return set()
    return set(frappe.get_all(
        'Moodboard Collaborator',
        filters={'user': user, 'access': 'editor'},
        pluck='moodboard', ignore_permissions=True))


def _is_collaborator_editor(moodboard, user):
    if not user or not moodboard:
        return False
    return bool(frappe.db.exists(
        'Moodboard Collaborator',
        {'moodboard': moodboard, 'user': user, 'access': 'editor'}))


def _collaborators_for(moodboard):
    ''' [{id, name, image, access}] for a board's shared editors. '''
    rows = frappe.get_all('Moodboard Collaborator', filters={'moodboard': moodboard},
                          fields=['user', 'access'], ignore_permissions=True)
    omap = _owners_for([r['user'] for r in rows])
    return [{**omap[r['user']], 'access': r['access']} for r in rows]


def _is_editor(doc):
    ''' May the current user edit this board: admin, owner, or shared editor.
        Brand users are never editors. '''
    if _is_admin():
        return True
    user, brand = _identity()
    if brand:
        return False
    return doc.owner == user or _is_collaborator_editor(doc.name, user)


# --- audit log ---------------------------------------------------------------

def _actor_type(doc):
    ''' The capacity the current user is acting in on this board (for the log). '''
    user, _ = _identity()
    if doc.owner == user:
        return 'Owner'
    if _is_admin():
        return 'Admin'
    if _is_collaborator_editor(doc.name, user):
        return 'Collaborator'
    return 'System'


def _log_edit(doc, action, details=None):
    ''' Best-effort audit row in Moodboard Edit Log. Must never break the write.
        Inserts within the caller's transaction (the caller commits afterwards). '''
    try:
        try:
            actor, _ = _identity()
        except Exception:
            actor = frappe.session.user
        log = frappe.new_doc('Moodboard Edit Log')
        log.moodboard = getattr(doc, 'name', doc)
        log.actor = actor
        log.actor_type = _actor_type(doc) if hasattr(doc, 'owner') else 'System'
        log.action = action
        if details is not None:
            log.details = details if isinstance(details, str) else frappe.as_json(details)
        log.insert(ignore_permissions=True)
    except Exception:
        frappe.log_error(frappe.get_traceback(), 'moodboard_v2._log_edit()')


def _assert_internal_user(email):
    ''' Validate a share target is an enabled, internal (non-brand) user. '''
    if not email or not frappe.db.exists('User', email):
        _bad_request('User does not exist')
    if not frappe.db.get_value('User', email, 'enabled'):
        _bad_request('User is disabled')
    if frappe.db.exists('Brand User', {'user': email}):
        _bad_request('Boards can only be shared with internal users')
    return email


def _assert_owner_or_admin(doc):
    ''' Sharing management (share/unshare) is restricted to the owner or an admin. '''
    if _is_admin():
        return
    user, brand = _identity()
    if brand or doc.owner != user:
        _forbidden('Only the board owner can manage sharing')


def _list_scope(user, brand, filters, is_admin=None, editor_ids=None):
    '''
    Build the frappe.get_all filter dict that defines which boards the caller can
    see, plus the request's non-pagination filters (season, gender, search, owner,
    brand/status). Shared by list_moodboards and list_style_categories so both apply
    identical scope. Returns None when the caller provably sees no boards (brand user
    with no linked boards), letting callers short-circuit to an empty result.

    is_admin / editor_ids are accepted so a caller that already resolved them doesn't
    pay for them twice; both are computed here when omitted.
    '''
    q = {'is_active': 1}
    if filters.get('season'):
        q['season'] = filters['season']

    # Gender lives in a JSON-array column (e.g. ["Women","Men"]); match a whole
    # quoted token so "Men" can't match "Women". MySQL's default collation makes
    # this case-insensitive, mirroring the old in-Python .lower() compare.
    gender = (filters.get('gender') or '').strip()
    if gender:
        q['genders'] = ['like', f'%"{gender}"%']

    # Title search — shared by brand and internal users; ignored under 3 chars.
    search = (filters.get('search') or '').strip()
    if len(search) >= _SEARCH_MIN_CHARS:
        q['moodboard_title'] = ['like', f'%{search}%']

    # Ownership scope: 'me' restricts to boards owned by the caller; 'all' (default)
    # leaves it open; any other value is treated as a specific owner's email (the
    # user-wise filter — see list_moodboard_owners for the dropdown). Only internal
    # users own boards — brand users own none, so 'me' (or any owner email) correctly
    # yields an empty page for them.
    owner = (filters.get('owner') or 'all').strip()
    if owner.lower() == 'me':
        q['owner'] = user
    elif owner.lower() != 'all' and owner:
        q['owner'] = owner

    if brand:
        if _brand_sees_all(brand['id']):
            # "All Moodboard Permission" — every board carrying a frozen snapshot across
            # all brands (not scoped to Brand Moodboard links). Both Published and
            # Unpublished Changes carry a snapshot the brand sees (frozen), so a board
            # flipped by a post-publish media upload stays listed; only Draft is hidden.
            q['status'] = ['in', ['Published', 'Unpublished Changes']]
            # Optional brand narrowing: such users may pass one brand id or an array
            # (`brand`/`brands`) to scope to the boards linked to ANY of those brands.
            brand_ids = _brand_filter_ids(filters)
            if brand_ids:
                scoped = frappe.get_all('Brand Moodboard',
                                        filters={'brand': ['in', brand_ids]}, pluck='moodboard')
                q['name'] = ['in', scoped or ['__none__']]
        else:
            # Brand user — their brand's boards that have a published snapshot. Both
            # Published and Unpublished Changes carry a frozen snapshot the brand sees;
            # only Draft (never published) is hidden.
            names = frappe.get_all('Brand Moodboard', filters={'brand': brand['id']}, pluck='moodboard')
            if not names:
                return None
            q['name'] = ['in', names]
            q['status'] = ['in', ['Published', 'Unpublished Changes']]
    else:
        if filters.get('status') in _STATUS_VALUES:
            q['status'] = filters['status']
        if filters.get('brand') and not _brand_sees_all(filters['brand']):
            # Normal brand filter — scope to that brand's linked boards. But if the
            # filtered brand carries "All Moodboard Permission", skip the restriction
            # so the PSL user sees every board (mirroring what that brand's users see).
            scoped = frappe.get_all('Brand Moodboard', filters={'brand': filters['brand']}, pluck='moodboard')
            q['name'] = ['in', scoped or ['__none__']]

        # Boards are now created the moment a user starts one, so the table carries
        # half-filled and abandoned work. Someone else's in-progress board (no primary
        # version chosen) is hidden from this list — an image being picked is the point
        # boards used to come into existence at, so this preserves what the team used
        # to see. Nothing is deleted: the owner, their shared editors and admins keep
        # seeing it so it can be resumed.
        if is_admin is None:
            is_admin = _is_admin()
        if not is_admin:
            hidden = _incomplete_board_names(
                user, editor_ids if editor_ids is not None else _collab_editor_ids(user))
            if hidden:
                if 'name' in q:
                    q['name'] = ['in', sorted(set(q['name'][1]) - hidden) or ['__none__']]
                else:
                    q['name'] = ['not in', sorted(hidden)]

    return q


def _incomplete_board_names(user, editor_ids):
    '''
    Active boards with no primary version that belong to someone else — the caller's
    own boards and any shared with them as editor are excluded, so a user never loses
    sight of work they can act on.
    '''
    if not user:
        return set()
    rows = frappe.get_all(
        DOCTYPE,
        filters={'is_active': 1, 'primary_version': ['is', 'not set'], 'owner': ['!=', user]},
        pluck='name', ignore_permissions=True, limit_page_length=0)
    return set(rows) - set(editor_ids or ())


def _narrow_names(q, names):
    '''
    Narrow a scope dict to `names`, composed with whatever name filter _list_scope
    already left on it. That is either ['in', allowed] (brand narrowing) or
    ['not in', hidden] (someone else's in-progress boards) — callers layering a
    positive filter on top must not have to know which, and reading a 'not in' list
    as if it were an allow-list inverts the whole query.

    Mutates q and returns False when the result is provably empty, so the caller can
    short-circuit to its own empty shape.
    '''
    names = set(names)
    cur = q.get('name')
    if isinstance(cur, (list, tuple)) and len(cur) == 2:
        op, val = cur
        if op == 'not in':
            names -= set(val)
        else:                      # 'in' (the only other form emitted here)
            names &= set(val)
    elif cur:
        names &= {cur}
    if not names:
        return False
    q['name'] = ['in', sorted(names)]
    return True


def _names_with_style_categories(categories):
    '''
    Names of boards whose style_categories JSON array contains ANY of the given
    values (quoted-token LIKE, same trick as the gender filter). Empty set if none.
    '''
    or_filters = [['style_categories', 'like', f'%"{c}"%'] for c in categories if c]
    if not or_filters:
        return set()
    return set(frappe.get_all(DOCTYPE, or_filters=or_filters, pluck='name', ignore_permissions=True))


def _require_internal():
    if _identity()[1]:
        _forbidden('Internal users only')


def _assert_editor(doc):
    # Admins edit any board; the owner and shared collaborators edit their own.
    if not _is_editor(doc):
        _forbidden('Only the board owner, a collaborator, or an admin can edit')


def _is_brand_linked(moodboard, brand_id):
    return bool(frappe.db.exists('Brand Moodboard', {'moodboard': moodboard, 'brand': brand_id}))


def _brand_filter_ids(filters):
    '''
    Normalize the request's brand filter to a list of brand ids. Accepts an array
    under `brands` (or legacy `brand`), a JSON-encoded array string, or a single
    bare id. Empty list when nothing usable was passed.
    '''
    raw = filters.get('brands')
    if raw is None:
        raw = filters.get('brand')
    ids = _as_list(raw)
    if not ids and isinstance(raw, str) and raw.strip():
        ids = [raw.strip()]
    return [b for b in ids if b]


def _brand_sees_all(brand_id):
    '''
    True if this brand carries "All Moodboard Permission" — a per-brand flag on the
    Brand record, so it applies to every user of the brand. Also used to expand a PSL
    user's `brand` filter to all boards. Read live from the DB (not the JWT) so it takes
    effect immediately, without waiting for brand users to re-login for a fresh token.
    '''
    if not brand_id:
        return False
    return bool(frappe.db.get_value('Brand', brand_id, 'all_moodboard_permission'))


def _brands_for(names):
    ''' {moodboard: [{id,name}]} for the given boards. '''
    if not names:
        return {}
    rows = frappe.get_all('Brand Moodboard', filters={'moodboard': ['in', names]},
                          fields=['moodboard', 'brand'], ignore_permissions=True)
    name_of = {}
    out = {}
    for r in rows:
        bid = r['brand']
        if bid not in name_of:
            name_of[bid] = frappe.db.get_value('Brand', bid, 'brand') or bid
        out.setdefault(r['moodboard'], []).append({'id': bid, 'name': name_of[bid]})
    return out


def _owners_for(emails):
    ''' {email: {id, name, image}} for the given owner emails (batch User lookup). '''
    emails = [e for e in set(emails) if e]
    if not emails:
        return {}
    rows = frappe.get_all('User', filters={'name': ['in', emails]},
                          fields=['name', 'full_name', 'user_image'], ignore_permissions=True)
    out = {r['name']: {'id': r['name'], 'name': r.get('full_name') or r['name'],
                       'image': r.get('user_image') or None} for r in rows}
    # Owners with no User row (e.g. Administrator/Guest) still get a usable object.
    for e in emails:
        out.setdefault(e, {'id': e, 'name': e, 'image': None})
    return out


def _owner_obj(email):
    ''' Single-owner convenience wrapper around _owners_for. '''
    return _owners_for([email]).get(email) if email else None


# =====================================================================
# Small utils
# =====================================================================

def _page(limit, offset):
    ''' Coerce request limit/offset to safe ints: limit in [1, _MAX_LIMIT], offset >= 0. '''
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        limit = _DEFAULT_LIMIT
    try:
        offset = int(offset)
    except (TypeError, ValueError):
        offset = 0
    limit = max(1, min(limit, _MAX_LIMIT))
    offset = max(0, offset)
    return limit, offset


def _page_envelope(items, total, limit, offset, **extra):
    return {'total': total, 'limit': limit, 'offset': offset, 'items': items, **extra}


def _get_or_404(name):
    if not name or not frappe.db.exists(DOCTYPE, name):
        frappe.throw('Moodboard not found', frappe.DoesNotExistError)
    return frappe.get_doc(DOCTYPE, name)


def _get_doc_or_404(doctype, name):
    if not name or not frappe.db.exists(doctype, name):
        frappe.throw(f'{doctype} not found', frappe.DoesNotExistError)
    return frappe.get_doc(doctype, name)


def _as_dict(value):
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        try:
            p = frappe.parse_json(value)
            return p if isinstance(p, dict) else {}
        except Exception:
            return {}
    return {}


def _as_list(value):
    if isinstance(value, list):
        return value
    if isinstance(value, str) and value.strip():
        try:
            p = frappe.parse_json(value)
            return p if isinstance(p, list) else []
        except Exception:
            return []
    return []


def _pick(payload, *keys):
    ''' First key present with a non-None value — the snake_case/camelCase pair
        this module accepts everywhere. Tests for None rather than truthiness so a
        legitimate 0 (a match_score of zero, brief index 0) is not skipped. '''
    for k in keys:
        v = payload.get(k)
        if v is not None:
            return v
    return None


def _clean_str(value):
    ''' Trimmed non-empty string, or None. Non-strings are coerced, so a fab code
        that arrives as a number still joins against the stored one. '''
    if value in (None, ''):
        return None
    return str(value).strip() or None


def _int_or_none(value):
    ''' int(value), or None when it is absent / not a number — so an optional
        numeric field never writes a 0 the client did not send. '''
    if value in (None, ''):
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _as_bool(value, default=False):
    ''' A request flag as the caller meant it. A JSON body delivers a real bool,
        but the same flag on a query string arrives as text — and `bool('false')`
        is True — so read the words rather than the truthiness of the string. '''
    if value is None or value == '':
        return default
    if isinstance(value, str):
        return value.strip().lower() not in ('0', 'false', 'no', 'none', 'null')
    return bool(value)


def _parse(value, default):
    if value in (None, ''):
        return default
    try:
        return frappe.parse_json(value)
    except Exception:
        return default


def _dump(value):
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return frappe.as_json(value)


def _save_image(value):
    ''' base64 data URL -> uploaded /files path; an existing path/URL passes through. '''
    if not value or not isinstance(value, str):
        return value
    if value.startswith('data:'):
        return util.save_file(value, MAX_IMAGE_FILE_SIZE_MB)
    return value


def _process_images(obj):
    '''
    Recursively upload any base64 carried under an IMAGE_* key (the convention
    used inside canvas_state overlays / message attachments), replacing it with
    the saved /files path. Mirrors prism.api.moodboard._process_images.
    '''
    if isinstance(obj, dict):
        for key, value in obj.items():
            if key.startswith('IMAGE_') and isinstance(value, str) and value.startswith('data:'):
                obj[key] = util.save_file(value, MAX_IMAGE_FILE_SIZE_MB)
            else:
                _process_images(value)
    elif isinstance(obj, list):
        for item in obj:
            _process_images(item)
    return obj


def _dump_with_images(value):
    ''' Parse a JSON blob, upload any embedded IMAGE_* base64, re-serialize. '''
    if value is None:
        return None
    parsed = value if isinstance(value, (dict, list)) else _parse(value, None)
    if isinstance(parsed, (dict, list)):
        return frappe.as_json(_process_images(parsed))
    return _dump(value)


def _iso(value):
    if not value:
        return None
    return frappe.utils.get_datetime(value).isoformat()


def _dt(value):
    if not value or not isinstance(value, str):
        return value
    s = value.strip().replace('T', ' ')
    if s.endswith('Z'):
        s = s[:-1]
    if ' ' in s:
        d, t = s.split(' ', 1)
        for sign in ('+', '-'):
            if sign in t:
                t = t.split(sign, 1)[0]
        s = f'{d} {t}'.strip()
    return s


def _forbidden(message):
    frappe.throw(message, frappe.PermissionError)


def _bad_request(message):
    frappe.throw(message, frappe.ValidationError)
