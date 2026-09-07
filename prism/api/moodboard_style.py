'''
Moodboard styles + embedded per-style cost — dedicated DocType backend.

Promotes each moodboard "extracted style" (and its computed cost) out of the
moodboard draft JSON (editorV2.foundation.extracted_styles / editorV2.cost_calculation)
into a standalone `Moodboard Style` doctype — one row per style, Link to the board.

Source of truth = the doctype. The moodboard `get_draft` API rebuilds the old
JSON shapes from these rows (see build_extracted_styles / build_cost_calculation),
so the ~8 read consumers keep working unchanged. Only the write paths
(StylesStep save, CostingStep save) are redirected to the methods here.

Conventions mirror prism.api.moodboard.* (which this companions): the cost is
still computed by prism.api.costing.get_garment_cost on the client — only where
the result is persisted changes. Images use the IMAGE_<key> S3 convention.
'''

import random
import time

import frappe

from prism.auth.authenticator import auth_required
import prism.api.util as util
import prism.lib.cloud as cloud
from prism.prism.doctype.moodboard_style_render.moodboard_style_render import build_render_key

DOCTYPE = 'Moodboard Style'
MAX_IMAGE_FILE_SIZE_MB = 5
MAX_STYLE_IMAGE_FILE_SIZE_MB = 15  # the generated render — routinely several MB
MAX_MODEL_FILE_SIZE_MB = 50
MAX_GARMENT_FILE_SIZE_MB = 25
MAX_BOM_FILE_SIZE_MB = 25
MAX_VIDEO_FILE_SIZE_MB = 200

# Pagination tuning for list_styles_multiple (mirrors moodboard_v2.list_moodboards).
_DEFAULT_LIMIT = 20
_MAX_LIMIT = 100

# Attribute columns promoted out of the old `attrs` map (re-nested by the
# compat serializer). Everything else the classifier emits rides in attrs_extra.
_ATTR_FIELDS = (
    'gender', 'product_category', 'garment_name', 'description',
    'fabric_quality', 'element_colour', 'element_colour_hex', 'element_colour_tcx',
)

# Generator output promoted to its own column. Served under `design` rather than
# folded into `attrs`, so none of it reaches the brand-facing published snapshot
# (moodboard_v2._brand_style picks its keys explicitly) — the rationale is an
# internal argument, not a product description.
#
# `fab_batch` is the one member that isn't generator output — it is the board's
# dye lot, filled in from the board's selection or by a fabric switch. It lives
# here so it travels with `fab_code` through every write path and serializer: the
# two are one identity, and a code that moved without its batch would name a lot
# of some other cloth.
_DESIGN_FIELDS = (
    'fab_code', 'fab_batch', 'reason', 'fabric_rationale', 'design_brief',
    'colour_treatment', 'print_label', 'print_application',
    'secondary_colour', 'secondary_colour_hex', 'secondary_colour_tcx',
)

# Every inbound spelling of a writable scalar field: the doctype fieldname, the
# frontend's camelCase, and the wire key POST /api/ai/generate-styles-set emits —
# so a generated style can be handed to the write APIs unmodified. First key
# present wins.
_INBOUND_KEYS = {
    'idx': ('idx', 'index'),
    'gender': ('gender',),
    'product_category': ('product_category', 'productCategory', 'styleCategory'),
    'garment_name': ('garment_name', 'garmentName'),
    'description': ('description',),
    'fabric_quality': ('fabric_quality', 'fabricQuality', 'fabricName'),
    'fab_code': ('fab_code', 'fabCode', 'fabricCode'),
    # Deliberately NOT `batch`: the generator's product objects are accepted
    # verbatim, and a bare `batch` there would be someone else's word.
    'fab_batch': ('fab_batch', 'fabBatch'),
    'element_colour': ('element_colour', 'elementColour'),
    'element_colour_hex': ('element_colour_hex', 'elementColourHex'),
    'element_colour_tcx': ('element_colour_tcx', 'elementColourTcx'),
    'secondary_colour': ('secondary_colour', 'secondaryColourName'),
    'secondary_colour_hex': ('secondary_colour_hex', 'secondaryColourHex'),
    'secondary_colour_tcx': ('secondary_colour_tcx', 'secondaryColourTcx'),
    'colour_treatment': ('colour_treatment', 'colourTreatment'),
    'print_label': ('print_label', 'printLabel'),
    'print_application': ('print_application', 'printApplication'),
    'reason': ('reason',),
    'fabric_rationale': ('fabric_rationale', 'fabricRationale'),
    'design_brief': ('design_brief', 'designBrief'),
    'image_status': ('image_status', 'imageStatus'),
    'image_error': ('image_error', 'imageError'),
    'decision': ('decision',),
    'decision_comment': ('decision_comment', 'decisionComment', 'comment'),
}

# The generator's colour objects — {name, hex, pantone} — unpacked into the flat
# trios. `pantone` is the TCX code. Applied after the scalar pass, so a structured
# colour wins over a flat one; a value that is a plain string (just the name) falls
# through to the scalar handling instead.
_COLOUR_OBJECTS = {
    'colour': ('element_colour', 'element_colour_hex', 'element_colour_tcx'),
    'secondaryColour': ('secondary_colour', 'secondary_colour_hex', 'secondary_colour_tcx'),
    'secondary_colour': ('secondary_colour', 'secondary_colour_hex', 'secondary_colour_tcx'),
}

_DECISIONS = ('Pending', 'Approved', 'Rejected')
# `stale` = the picture is from a previous fabric and a newer render is
# outstanding. Keeping the old picture beats an empty frame on an uncached switch.
_IMAGE_STATUSES = ('pending', 'ready', 'stale', 'failed')

_DEFAULT_MOQ = 1500


class InvalidRequest(frappe.ValidationError):
    '''
    A malformed or self-contradictory request — 400.

    A bare frappe.throw raises ValidationError, which Frappe answers with 417.
    That is fine for "this style does not exist"; it is the wrong shape for "you
    asked for something the request itself forbids", which a client has to be able
    to tell apart from a server fault without reading the message.
    '''
    http_status_code = 400


# =====================================================================
# Whitelisted endpoints (§3)
# =====================================================================

@frappe.whitelist(allow_guest=True, methods=['POST'])
def sync_styles(moodboard=None, styles=None):
    '''
    Bulk upsert + delete to match a StylesStep save. Only *included* styles are
    persisted; any existing row for the board not in the included set is deleted
    (along with its embedded cost). Returns the saved set (ordered by idx).

    Legacy write path — the generated-styles page must NOT use it. Two reasons:
    it sets include=1 on everything it keeps (which the controller reads as an
    approval, since this endpoint predates decisions), and it *deletes* the rows
    it does not receive — including a Rejected style, whose comment is a record
    someone made on purpose. The generated flow uses save_generated_styles, which
    upserts without approving and only deletes when explicitly asked to.
    '''
    _assert_board(moodboard)
    styles = util_as_list(styles)

    included = [s for s in (_as_dict(x) for x in styles) if _truthy(s.get('include', True))]

    keep = set()
    for i, s in enumerate(included):
        doc = _get_or_new(s.get('id'), moodboard)
        doc.moodboard = moodboard
        doc.idx = i
        doc.include = 1
        _apply_details(doc, s, is_new=(not doc.name or not frappe.db.exists(DOCTYPE, doc.name)))
        doc.save(ignore_permissions=True)
        keep.add(doc.name)

    # Reconcile: drop excluded / removed rows (cascades the embedded cost).
    for name in frappe.get_all(DOCTYPE, filters={'moodboard': moodboard}, pluck='name'):
        if name not in keep:
            frappe.delete_doc(DOCTYPE, name, ignore_permissions=True, force=True)

    frappe.db.commit()
    return _list(moodboard)


@frappe.whitelist(allow_guest=True)
def list_styles(moodboard=None, origin=None):
    '''
    All styles for a board (incl. cost summary), ordered by idx.

    `origin` filters by where the row came from, because a board can hold both
    kinds at once and the row shape is identical:

        generated : produced by generate-styles-set
        extracted : the legacy StylesStep rows
        all       : both (default — the historical behaviour)

    The test is a non-empty `design_brief`: only the generator writes one, and it
    is what the renderer was instructed with, so it cannot be absent on a style it
    produced. Filtering server-side means the rule lives in one place — if the
    discriminator ever changes, callers passing `origin` don't.
    '''
    _assert_board(moodboard)
    return _list(moodboard, origin=origin)


@frappe.whitelist(allow_guest=True)
@auth_required
def list_styles_multiple(moodboards=None, limit=20, offset=0):
    '''
    Styles across several boards in one paginated envelope — same shape as
    prism.api.moodboard_v2.list_moodboards: { total, limit, offset, items }. Each item
    matches a list_styles row (incl. cost summary).

    PSL / internal users only — brand users are forbidden. `moodboards` is a JSON array
    (or comma-separated list) of Moodboard ids; unknown ids simply contribute no rows.
    Results are flat-ordered by moodboard, then idx, then creation, so the page window is
    stable across calls.
    '''
    _require_internal()
    limit, offset = _page(limit, offset)

    ids = _board_ids(moodboards)
    if not ids:
        return _page_envelope([], 0, limit, offset)

    flt = {'moodboard': ['in', ids]}
    total = frappe.db.count(DOCTYPE, flt)
    names = frappe.get_all(
        DOCTYPE, filters=flt,
        order_by='moodboard asc, idx asc, creation asc', pluck='name',
        ignore_permissions=True, limit_start=offset, limit_page_length=limit,
    )

    # Board titles for this page — one lookup, attached per item as `moodboardTitle`.
    titles = dict(frappe.get_all(
        'Moodboard', filters={'name': ['in', ids]},
        fields=['name', 'moodboard_title'], as_list=True, ignore_permissions=True,
    ))

    items = []
    for n in names:
        item = _to_api(frappe.get_doc(DOCTYPE, n))
        item['moodboardTitle'] = titles.get(item['moodboard'])
        items.append(item)
    return _page_envelope(items, total, limit, offset)


@frappe.whitelist(allow_guest=True)
def get_style(id=None):
    ''' One style incl. full cost_inputs / cost_result. '''
    doc = _get_or_404(id)
    return _to_api(doc, full=True)


@frappe.whitelist(allow_guest=True)
@auth_required
def get_style_bom_metrics(id=None):
    '''
    The BOM-extracted metrics for one style — consumption + marker efficiency —
    with a status/warning that distinguishes "no BOM uploaded" from "BOM uploaded
    but not calculated" (e.g. a non-parseable file or a fabric row missing Roll
    Width / Avg. Fabric Length / GSM).

    Auth: requires the X-Auth-Token JWT (@auth_required) and is restricted to
    internal / PSL users — brand users get 403.

    Returns:
        {
            id, bom, consumption, markerEfficiency,
            hasBom, calculated,
            warning   # human-readable string, or None when both values are present
        }
    '''
    _require_internal()
    doc = _get_or_404(id)

    consumption = doc.consumption
    marker_efficiency = doc.marker_efficiency
    has_bom = bool((doc.bom or '').strip())
    calculated = consumption is not None and marker_efficiency is not None

    warning = None
    if not has_bom:
        warning = 'No BOM uploaded for this style — values unavailable.'
    elif not calculated:
        warning = ('A BOM is uploaded but its metrics are not calculated. The file may not be a '
                   'parseable .xlsx, its first fabric row is missing Roll Width / Avg. Fabric '
                   'Length / GSM, or the length cells carry no unit (cm/inch) to convert with.')

    return {
        'id': doc.name,
        'bom': cloud.asset_url(doc.bom),
        'consumption': consumption,
        'markerEfficiency': marker_efficiency,
        'hasBom': has_bom,
        'calculated': calculated,
        'warning': warning,
    }


@frappe.whitelist(allow_guest=True, methods=['POST'])
def update_style_details(id=None, **fields):
    '''
    Edit a single style's attributes / moq / include / image. Only the keys
    present in the body are touched.
    '''
    doc = _get_or_404(id)
    _apply_details(doc, fields, is_new=False)
    doc.save(ignore_permissions=True)
    frappe.db.commit()
    return _to_api(doc, full=True)


# =====================================================================
# Style generation — POST /api/ai/generate-styles-set
# =====================================================================

@frappe.whitelist(allow_guest=True, methods=['POST'])
@auth_required
def save_generated_styles(moodboard=None, styles=None, delete_ids=None, deleteIds=None):
    '''
    Persist a generated styles set. Takes the generator's own objects unmodified —
    `index`, `styleCategory`, `fabricName`, `fabricCode`, `colour` /
    `secondaryColour`, `signatureDetails[]`, `designBrief`, `reason`,
    `fabricRationale` are all understood (see _flatten_payload). Note the plan
    event names the array `plan.products`; pass it as `styles`.

    Called on the `plan` event, 3-4 minutes before the first image: a style with
    no `image` is saved with image_status `pending`, so a row waiting on its render
    is never mistaken for one whose render failed. The images arrive later, one
    `set_style_image` call per product event.

    Nothing here is approved. Every new row lands Pending, which means include=0 —
    so a freshly generated set is invisible to costing, ESG, the techpack and the
    published board until a merchandiser rules on it.

        moodboard  : the board id
        styles     : the generator's products array
        delete_ids : DEPRECATED, ignored (alias `deleteIds`). See "Regenerating".

    ## Regenerating — this endpoint APPENDS

    A regenerate adds its styles to the board; it never rewrites or removes what
    is already there. The previous run's garments stay exactly as they are, with
    their verdicts, comments, costs, ESG rows and render caches intact.

    Two consequences of that, both handled here:

    * The generator's `index` is NOT the row's position on the board. Every run
      emits 0…N, so honouring it verbatim would stack every run on top of the
      first. New rows are numbered from the end of the board instead —
      `max(idx) + 1 + position` — which keeps _board_docs' `idx asc` ordering
      meaningful across runs. The board position is ours; the stream index is the
      generator's and is not stored.

    * Nothing is matched by index any more. A row is only ever updated when the
      caller names it in `id` (a hand edit, or a deliberate re-post of one style).
      Anything else creates a row. That is what makes the append an append.

    Because nothing is overwritten, `resetDecisions` is normally empty here — no
    verdict can be transferred to a garment nobody saw, so there is nothing to
    reset. The mechanism stays live for `id`-targeted rewrites and for hand edits
    through update_style_details.

    Deletion is no longer part of a save. `delete_ids` is accepted so an
    un-updated client does not 500, but it is ignored — clearing a superseded set
    is an explicit act, done through delete_style.

    ## Getting the ids back

    `savedStyles` is one row per incoming style, in payload order, so
    `savedStyles[i]` is `plan.products[i]`. That is the mapping the stream handler
    keys on: `set_style_image` identifies a style by `id`, never by index, because
    an index means nothing once a board holds more than one run. It is every row
    written, not just the new ones, so an id-targeted item in the payload does not
    shift the positions after it.

    `styles` is still the whole board, unchanged.

    ## The dye lot

    The generator picks a fab code; the board is batch-centric and holds one tile
    per (fab_code, batch). So a style arrives naming a quality, and the lot it is
    cut in has to be filled in from the board.

    It is filled in only when the board leaves no doubt: exactly one batch of that
    code is selected, so that lot IS the style's lot and recording it means the
    swatch is already ticked when the page loads. When the user put several lots
    of one code on the board, the generator did not say which of them it drew, and
    ranking them to pick a winner would tick a swatch on a guess — confidently
    wrong is worse than open. `fabBatch` stays null there and the UI degrades to
    code-level matching until someone picks, exactly as it does for every style
    written before batches existed.

    An explicit `fabBatch` in the payload always wins; nothing here overwrites a
    batch a style already has.

    Auth: X-Auth-Token JWT, internal / PSL users only.
    Returns { created, updated, deleted, resetDecisions[], savedStyles[], styles[] }.
    '''
    _require_internal()
    _assert_board(moodboard)

    incoming = [_as_dict(x) for x in util_as_list(styles)]
    if not incoming:
        frappe.throw('No styles to save (expected a `styles` array).')

    next_idx = _next_board_idx(moodboard)
    board_batches = _board_batches_by_code(moodboard)

    saved, created, reset = [], 0, []
    for position, src in enumerate(incoming):
        # Only an explicit id updates an existing row. The generator's `index` is
        # deliberately not consulted: every run emits 0…N, so matching on it would
        # rewrite the previous run's garments in place.
        doc = _get_or_new(src.get('id'), moodboard)
        is_new = not doc.name or not frappe.db.exists(DOCTYPE, doc.name)
        was_decided = (not is_new) and (doc.decision or 'Pending') != 'Pending'

        # A new row is appended to the end of the board; an id-targeted rewrite
        # keeps the position it already has.
        idx = (next_idx + position) if is_new else doc.idx

        doc.moodboard = moodboard
        _apply_details(doc, {**src, 'idx': idx}, is_new=is_new)

        # The lot, when the board names exactly one for this code (see above).
        # Guarded on emptiness, so a batch the payload carried or a merchandiser
        # already picked is never second-guessed here.
        if not _clean(doc.fab_batch):
            doc.fab_batch = _sole_batch(board_batches, doc.fab_code)

        doc.save(ignore_permissions=True)

        created += 1 if is_new else 0
        # The controller resets a verdict whose garment was replaced — report it,
        # so the page can tell the user their approval no longer stands.
        if was_decided and (doc.decision or 'Pending') == 'Pending':
            reset.append(doc.name)
        # One entry per incoming style, in payload order — created or updated
        # alike, so the caller's positional mapping never shifts.
        saved.append(doc)

    frappe.db.commit()
    return {
        'created': created,
        'updated': len(incoming) - created,
        'deleted': 0,
        'resetDecisions': reset,
        'savedStyles': [_to_api(d, full=True) for d in saved],
        'styles': _list(moodboard),
    }


def _next_board_idx(moodboard):
    '''
    The board position a newly generated style takes: one past the highest in use.

    The generator restarts its index at 0 on every run, so it cannot be the board
    position — appending run 2 at 0…5 would interleave it with run 1 under
    _board_docs' `idx asc, creation asc` and produce 0,0,1,1,… Numbering from the
    end keeps each run contiguous and in the order it was generated.
    '''
    rows = frappe.get_all(DOCTYPE, filters={'moodboard': moodboard}, fields=['idx'],
                          order_by='idx desc', limit=1, ignore_permissions=True)
    top = _int(rows[0].idx) if rows else None
    return (top + 1) if top is not None else 0


def _board_batches_by_code(moodboard):
    '''
    fab code -> the set of that code's dye lots the user actually put on the board.

    Read straight off the board's `surplus_fabrics` rows, which are batch-centric:
    one row per (fab_code, batch), `selected` marking the ones on the board (see
    moodboard_v2.sync_surplus_fabrics). Rows missing either half of the identity
    are unusable and are skipped there too.

    One query for the whole save, not one per style — a run is six styles and they
    routinely share a fab code.
    '''
    rows = frappe.get_all(
        'Moodboard Surplus Fabric',
        filters={'parent': moodboard, 'parenttype': 'Moodboard',
                 'parentfield': 'surplus_fabrics', 'selected': 1},
        fields=['fab_code', 'batch'], ignore_permissions=True)

    out = {}
    for r in rows:
        code, batch = _clean(r.get('fab_code')), _clean(r.get('batch'))
        if code and batch:
            out.setdefault(code, set()).add(batch)
    return out


def _sole_batch(by_code, fab_code):
    '''
    The board's lot for a fab code when there is exactly one, else None.

    Two selected lots of a code is not a tie to be broken: the board is saying the
    user wants both on it, and nothing in a generated style says which one it was
    drawn in. None is the honest answer, and the one the client already handles.
    '''
    batches = by_code.get(_clean(fab_code)) or set()
    return next(iter(batches)) if len(batches) == 1 else None


@frappe.whitelist(allow_guest=True, methods=['POST'])
@auth_required
def set_style_image(id=None, moodboard=None, index=None, image=None, status=None,
                    error=None, render_key=None, fab_code=None, fabric_quality=None,
                    element_colour_tcx=None, print_label=None, recolour=None,
                    fab_batch=None):
    '''
    Record the outcome of one render — the `product_ready` / `product_failed` end
    of the stream, and the same call a retry reports through.

    On success the image becomes the style's current render: stored, made the
    current row in the `renders` cache, image_status `ready`. On failure the
    reason is stored in `image_error` (NOT `reason`, which is why the style is in
    the range) and the failed row is kept in the cache — that is what lets the page
    offer a retry instead of silently re-running the same render.

    A failure clears `image`: the render being reported has no picture, and the
    previous fabric's picture is still in its own cache row, one apply_style_fabric
    call away. Nothing is lost, and the card never shows one fabric while the
    status describes another.

        id      : which style. Required — take it from `savedStyles[i].id` in
                  the save_generated_styles response, where `i` is the product's
                  stream index. `moodboard` + `index` is no longer accepted: a
                  board holds every run's styles now, so an index identifies
                  nothing on its own (see _resolve_style).
        image   : the render, as a `data:image/png;base64,…` URL (decoded to a
                  File here, offloaded to S3 on save) or an existing URL
        status  : ready | failed. Inferred from image/error when omitted.
        error   : product_failed.reason
        render_key : the key apply_style_fabric handed out for this render. Pass it
                  back verbatim — it is opaque here, stored and compared as a
                  string and never re-derived from `fab_code` or anything else.
                  Omit it and it is rebuilt from the style's current fabric
                  identity instead.
        fab_code, fab_batch, fabric_quality, element_colour_tcx, print_label,
        recolour : override the identity the cache row is written under.

    Auth: X-Auth-Token JWT, internal / PSL users only. Returns the updated style.
    '''
    _require_internal()
    name = _resolve_style(id, moodboard, index).name

    image = image if isinstance(image, str) else None
    status = _image_status(status) if status else ('ready' if image else 'failed')
    if status == 'pending':
        frappe.throw('`pending` is not a render outcome. Send ready or failed.')
    if status == 'ready' and not image:
        frappe.throw('A ready render needs an `image`.')

    # Staged before the write, and once: this is where the render's megabytes stop
    # being a string, and a save that has to be retried under contention must not
    # decode and upload them a second time (see _save_retrying).
    staged = {'image': _save_image(image) if status == 'ready' else None}

    def write(doc):
        identity = _render_identity(doc, fab_code, fabric_quality, element_colour_tcx,
                                    print_label, recolour, fab_batch)
        key = (render_key or '').strip() or build_render_key(
            identity['fab_code'], identity['element_colour_tcx'],
            identity['print_label'], identity['recolour'], identity['fab_batch'])

        if status == 'ready':
            doc.image = staged['image']
            doc.image_status = 'ready'
            doc.image_error = None
            # No fab code means no cache identity (see build_render_key) — the render
            # still lands on the style, it just can't be recalled by switching back.
            if key:
                _upsert_render(doc, key, identity, doc.image, 'ready', None)
        elif _cached_render(doc, key) is not None:
            # This exact fabric already has a good picture — a re-render that failed
            # must not cost us the one we have. Keep it, and say nothing about a
            # failure the card can't show anyway.
            row = _cached_render(doc, key)
            _make_current(doc, row)
            doc.image = row.image
            doc.image_status = 'ready'
            doc.image_error = None
        else:
            # `image` is deliberately left alone. It is either empty (nothing to lose)
            # or the previous fabric's picture, which is better on the card than an
            # empty frame — `failed` + image_error already say the newer render didn't
            # arrive, and its render row records the failure for the retry.
            doc.image_status = 'failed'
            doc.image_error = (error or '').strip() or 'The render failed.'
            if key:
                _upsert_render(doc, key, identity, None, 'failed', doc.image_error)

    # The staged file is moved to S3 and deleted from disk inside the save. A
    # deadlock rolls the row back but not the disk, so the attempt after one writes
    # the URL its predecessor produced, never a /files path with no bytes behind it.
    doc = _save_retrying(name, write, on_deadlock=lambda d: staged.update(image=d.image))
    return _to_api(doc, full=True)


@frappe.whitelist(allow_guest=True, methods=['POST'])
@auth_required
def apply_style_fabric(id=None, **fields):
    '''
    Point a style at a different fabric, and say whether that needs a render.

    Computes the render key for the new fabric identity and looks it up in the
    style's cache:

      * a `ready` row exists -> its image is copied up to the style, the row is
        flagged current, image_status is `ready`. No renderer call, no 3-4 minute
        wait. `cached: true`.
      * otherwise -> `needsRender: true`, and the style KEEPS its last good
        picture with image_status `stale`. A stale picture beats an empty frame,
        and it means an uncached switch never strands the user with nothing while
        a single-style render is unavailable. Render when you can and report back
        through set_style_image with the `renderKey` returned here.

    A previous failure for this exact identity comes back as `lastFailure`, so the
    page can say what went wrong last time and offer a retry rather than quietly
    repeating it.

        id       : the style id
        fabCode / fab_code       : the surplus quality — the ONLY required field
        fabBatch / fab_batch     : the dye lot of that code, when the user picked
                  one. Optional, and the point of it is that the board is
                  batch-centric: it holds one tile per (fab_code, batch), so three
                  lots of one Single Jersey are three swatches. Send the batch and
                  the style names the lot it is actually cut in — which is also
                  what lets the swatch the user clicked become the current one
                  instead of all three lighting up together.

                  Omit it and the style stays at code level, which is what every
                  row written before batches existed is; the client falls back to
                  matching on the code. Send `""` to go back to code level
                  deliberately.

                  A batch belongs to its code: switching `fabCode` without naming
                  a batch CLEARS the old one rather than carrying it across, since
                  a lot number means nothing under a different quality.

    Everything else defaults to what the style already holds, because a fabric
    switch off a dropdown that lists a label and a code is not the moment to
    re-post colour and print state the server can only get wrong. Send them only
    to change them:

        fabricName / fabric_quality
        colour: {name, hex, pantone} (or elementColourTcx etc.)
        printLabel, printApplication, colourTreatment
        recolour : the Moodboard Surplus Recolour applied — the same lot in two
                   colourways is two different renders. Defaults to the one on the
                   current render, so it survives a plain fabric switch.

    Auth: X-Auth-Token JWT, internal / PSL users only.
    Returns { cached, needsRender, renderKey, lastFailure, style }. `renderKey` is
    the key to render and report back; `style.renderKey` is the key of the picture
    actually on the card, which is the older one while this is stale.
    '''
    _require_internal()
    name = _get_or_404(id).name

    flat = _flatten_payload(fields)
    supplied = _first(fields, 'recolour', 'recolour_id', 'recolourId')
    out = {}

    def write(doc):
        was_code = _clean(doc.fab_code)

        for f in ('fab_code', 'fabric_quality', 'element_colour', 'element_colour_hex',
                  'element_colour_tcx', 'print_label', 'print_application', 'colour_treatment'):
            if f in flat:
                doc.set(f, flat.get(f))

        # The batch is a sub-identity of the code, not a peer of it. An unstated batch
        # survives a switch that kept the code (the caller only meant to change the
        # colourway or the print) but is dropped when the code moved — inheriting it
        # would file the style under a lot of a cloth it is no longer cut in, and key
        # its render against that.
        if 'fab_batch' in flat:
            doc.fab_batch = _clean(flat.get('fab_batch')) or None
        elif _clean(doc.fab_code) != was_code:
            doc.fab_batch = None

        recolour = _clean(supplied) if supplied is not None else _current_recolour(doc)
        key = build_render_key(doc.fab_code, doc.element_colour_tcx, doc.print_label,
                               recolour, doc.fab_batch)
        if not key:
            frappe.throw('`fabCode` is required — it is the identity a render is cached against.')

        rows = doc.renders or []
        cached = _cached_render(doc, key)
        failed = next((r for r in rows if r.render_key == key and r.status == 'failed'), None)

        if cached:
            _make_current(doc, cached)
            doc.fabric_quality = cached.fabric_quality or doc.fabric_quality
            doc.image = cached.image
            doc.image_status = 'ready'
            doc.image_error = None
        else:
            # Keep the last good picture and the row that owns it flagged current — it
            # is genuinely what's on the card. `stale` is the honest description: the
            # picture is from the previous fabric and a newer render is outstanding.
            doc.image_status = 'stale' if (doc.image or '').strip() else 'pending'
            doc.image_error = None

        # Handed back out of the closure so the response describes the attempt that
        # actually committed: a retry re-reads the row, and can find a render that
        # landed while this call was losing the race for it.
        out.update(key=key, cached=cached, failed=failed)

    doc = _save_retrying(name, write)
    cached, failed = out['cached'], out['failed']
    return {
        'cached': bool(cached),
        'needsRender': not cached,
        'renderKey': out['key'],
        'lastFailure': _render_obj(failed) if (failed and not cached) else None,
        'style': _to_api(doc, full=True),
    }


@frappe.whitelist(allow_guest=True, methods=['POST'])
@auth_required
def finalize_style_renders(moodboard=None, ids=None, error=None):
    '''
    Close out a generation run: every style still waiting on a render is marked
    failed.

    Call this once on the `final` event, and on a Stop mid-run. `product_failed`
    can land late or not at all, so without a terminal call a style whose render
    silently dropped sits `pending` in the database forever — a spinner with
    nothing behind it and no retry offered, because nothing ever recorded a
    failure.

    Only outstanding rows are touched: `pending` (never had a picture) and `stale`
    (kept the previous fabric's picture while a newer render was outstanding).
    A style that is `ready` or already `failed` is left exactly as it is, and a
    stale style keeps its picture — it just stops claiming a render is still
    coming.

    **Pass `ids`.** Since save_generated_styles appends, the board carries every
    run it has ever had, and this call cannot tell which rows belong to the run
    that is ending. `savedStyles[].id` from the save response is exactly that
    list. Without it the sweep falls back to the whole board and deliberately
    narrows to `pending` only: a `pending` row from an earlier run is a dead
    spinner and closing it is right, but a `stale` one is a deliberate fabric
    switch waiting on its render — failing that from an unrelated run's `final`
    would take a live state away from a style nobody touched.

        moodboard : the board whose run is ending
        ids       : the styles in this run. Strongly preferred — see above.
                    Omitted, every `pending` row on the board is closed.
        error     : the reason recorded on the stragglers. Defaults to a message
                    saying the run ended without the render arriving.

    Auth: X-Auth-Token JWT, internal / PSL users only.
    Returns { finalized, styles[] }.
    '''
    _require_internal()
    _assert_board(moodboard)

    reason = (error or '').strip() or 'The generation run ended before this render arrived.'
    wanted = set(_board_ids(ids))
    outstanding = ('pending', 'stale') if wanted else ('pending',)

    def close_out(doc):
        doc.image_status = 'failed'
        doc.image_error = reason

    # One transaction per style rather than one for the whole sweep. `final` fires
    # while the last renders of the run are still landing, and a board-wide
    # transaction would hold every one of those rows — and the gap locks a
    # child-table rewrite takes — for the length of the sweep, which is the other
    # half of the deadlock _save_retrying absorbs. The styles are independent and
    # only outstanding rows are ever touched, so a sweep that stops half way through
    # is safe to simply repeat.
    out = []
    for name in frappe.get_all(DOCTYPE,
                               filters={'moodboard': moodboard,
                                        'image_status': ['in', outstanding]},
                               order_by='idx asc', pluck='name', ignore_permissions=True):
        if wanted and name not in wanted:
            continue
        out.append(_to_api(_save_retrying(name, close_out), full=True))

    return {'finalized': len(out), 'styles': out}


@frappe.whitelist(allow_guest=True)
@auth_required
def list_style_renders(id=None):
    ''' Every fabric this style has been rendered against, current one first. '''
    _require_internal()
    doc = _get_or_404(id)
    return {
        'id': doc.name,
        'currentRenderKey': _current_render_key(doc),
        'renders': _renders_of(doc),
    }


@frappe.whitelist(allow_guest=True, methods=['POST'])
@auth_required
def decide_style(id=None, ids=None, decision=None, comment=None):
    '''
    Rule on one style or a set of them.

    The verdict drives `include` — Approved sets it, Rejected and Pending clear it
    — so an approval reaches costing, ESG, the techpack menu and the published set
    without any of them needing to know decisions exist.

    Rejecting requires a comment. That is enforced in the doctype controller, not
    just here, so the Desk form and any script hit the same wall as the dialog.

        id / ids : one style id, or a list of them
        decision : Pending | Approved | Rejected
        comment  : mandatory when Rejected; discarded otherwise

    `decided_by` / `decided_on` are stamped from the JWT session user.
    Auth: X-Auth-Token JWT, internal / PSL users only. Returns { updated, styles }.
    '''
    _require_internal()
    verdict = _decision(decision)

    names = [n for n in ([id] if id else []) + util_as_list(ids) if n]
    if not names:
        frappe.throw('`id` (or `ids`) of the style to decide on is required.')

    out = []
    for name in dict.fromkeys(names):  # de-duped, order preserved
        doc = _get_or_404(name)
        doc.decision = verdict
        doc.decision_comment = comment
        doc.save(ignore_permissions=True)
        out.append(_to_api(doc, full=True))

    frappe.db.commit()
    return {'updated': len(out), 'styles': out}


@frappe.whitelist(allow_guest=True, methods=['POST'])
@auth_required
def set_style_moodboard(id=None, ids=None, in_moodboard=None, inMoodboard=None):
    '''
    Add one style or a set of them to the moodboard the brand is shown, or take
    them back out.

    Separate from `include`, and deliberately not part of update_style_details:
    that is the legacy StylesStep path and `include` is the verdict's own derived
    flag, gating costing, ESG, the techpack and the published set. This narrows
    the moodboard alone, inside what an approval already allows.

    Approval is what puts a style in the board — every approved style is in it
    unless someone takes it out, so this endpoint is only ever used to trim, or to
    put back something trimmed. Setting it on a style that is not Approved is
    refused with a 400 rather than silently ignored: the caller asked for a state
    that cannot exist, and quietly writing 0 instead would look like it worked.

    The reverse rule needs no endpoint — a verdict leaving Approved clears the
    flag in the doctype controller, in the same save.

        id / ids     : one style id, or a list of them
        inMoodboard  : true to include in the moodboard, false to remove
                       (`in_moodboard` accepted too)

    Auth: X-Auth-Token JWT, internal / PSL users only. Returns { updated, styles }.
    '''
    _require_internal()

    raw = in_moodboard if in_moodboard is not None else inMoodboard
    if raw is None:
        frappe.throw('`inMoodboard` is required (true to add to the moodboard, false to remove).',
                     InvalidRequest)
    wanted = _flag(raw, 'inMoodboard')

    names = [n for n in ([id] if id else []) + util_as_list(ids) if n]
    if not names:
        frappe.throw('`id` (or `ids`) of the style to set is required.', InvalidRequest)

    # Resolved and checked in full before anything is written, so a batch with one
    # unapproved style in it fails as a whole instead of half-applying.
    docs = [_get_or_404(n) for n in dict.fromkeys(names)]
    if wanted:
        blocked = [(d.name, d.decision or 'Pending') for d in docs if (d.decision or 'Pending') != 'Approved']
        if blocked:
            listed = ', '.join(f'{n} ({v})' for n, v in blocked)
            frappe.throw(
                f'Only an approved style can be part of the moodboard. Not approved: {listed}.',
                InvalidRequest,
            )

    for doc in docs:
        doc.in_moodboard = wanted
        doc.save(ignore_permissions=True)

    frappe.db.commit()
    return {'updated': len(docs), 'styles': [_to_api(d, full=True) for d in docs]}


def _render_identity(doc, fab_code, fabric_quality, element_colour_tcx, print_label,
                     recolour, fab_batch=None):
    ''' The fabric a render belongs to: what the caller passed, else the style's own. '''
    code = _clean(fab_code) or (doc.fab_code or '')
    return {
        'fab_code': code,
        # Same rule as apply_style_fabric: the style's batch is only its own code's
        # batch. An override that names a different code without naming a batch
        # gets no batch, rather than the one belonging to the code it replaced.
        'fab_batch': (
            _clean(fab_batch) if fab_batch is not None
            else ((doc.fab_batch or '') if code == (doc.fab_code or '') else '')
        ),
        'fabric_quality': _clean(fabric_quality) or (doc.fabric_quality or ''),
        'element_colour_tcx': _clean(element_colour_tcx) or (doc.element_colour_tcx or ''),
        'print_label': _clean(print_label) if print_label is not None else (doc.print_label or ''),
        'recolour': _clean(recolour),
    }


def _current_recolour(doc):
    '''
    The recolour on the render currently shown. A fabric switch off a dropdown
    that only carries a label and a code doesn't re-state the recolour, but it is
    part of the key — so it has to persist across the switch rather than silently
    resetting the style to the un-recoloured lot.
    '''
    for row in (doc.renders or []):
        if frappe.utils.cint(row.is_current):
            return row.recolour or ''
    return ''


def _cached_render(doc, key):
    ''' The usable render for a key — ready, with a picture — or None. '''
    if not key:
        return None
    return next((r for r in (doc.renders or [])
                 if r.render_key == key and r.status == 'ready' and (r.image or '').strip()), None)


def _make_current(doc, row):
    ''' Flag exactly one render row as the one on the card (None = no current row). '''
    for r in (doc.renders or []):
        r.is_current = 1 if (row is not None and r is row) else 0


def _upsert_render(doc, key, identity, image, status, error):
    '''
    Write one render into the style's cache and make it the current row.

    One row per key, not per attempt: a retry overwrites its own failed row, which
    is exactly what "switching back to a fabric already rendered costs nothing"
    needs — the lookup is by identity, and two rows for one identity would make it
    ambiguous. A failed row is still kept until it succeeds.

    A ready row is never demoted to failed here — callers route a failure that
    already has a good picture away from this function (see set_style_image), so a
    re-render that fails can't cost us the image we already had.
    '''
    row = next((r for r in (doc.renders or []) if r.render_key == key), None)
    if row is None:
        row = doc.append('renders', {'render_key': key})

    row.update({
        'fab_code': identity['fab_code'],
        'fab_batch': identity['fab_batch'],
        'fabric_quality': identity['fabric_quality'],
        'element_colour_tcx': identity['element_colour_tcx'],
        'print_label': identity['print_label'],
        'recolour': identity['recolour'],
        'status': status,
        'error': error if status == 'failed' else None,
    })
    if status == 'ready':
        row.image = image
        _make_current(doc, row)
    else:
        # A failed row is never what's on the card — but the row that owns the
        # picture still showing (the previous fabric's) keeps its flag. Clearing
        # every flag here would orphan the stale image the card is displaying.
        row.is_current = 0
    return row


# InnoDB picks a victim and rolls it back whole, so a save that loses a deadlock is
# redone rather than resumed. Four attempts over at most ~1s of backoff covers a
# six-render burst; a collision that outlives that is not the transient kind.
_DEADLOCK_ATTEMPTS = 4
_DEADLOCK_BACKOFF = 0.1


def _save_retrying(name, write, on_deadlock=None):
    '''
    Save one style, retrying when MySQL names it the victim of a deadlock (1213).

    The six renders of a run report within seconds of each other, and `final` sweeps
    the board while the last of them are still arriving. Every one of those saves
    rewrites the style's `renders` child table, which Frappe does as a DELETE of the
    rows the doc no longer carries followed by an INSERT of the ones it does (see
    Document.update_child_table). The DELETE takes gap locks on
    `tabMoodboard Style Render`; a concurrent INSERT needs a lock inside one of
    those gaps, and two of them taken in opposite orders is error 1213.

    That is a lock-ordering collision, not a conflicting write — nothing is wrong
    with the data, one transaction simply has to go second. So it goes second:
    InnoDB has already rolled the loser back by the time the exception surfaces, and
    the row is read and written again from scratch. The doc is loaded inside each
    attempt because reusing the one from a dead transaction would save a `modified`
    stamp the row no longer carries, which Frappe reads as someone else's edit.

        write(doc)       : applies the change. Called once per attempt, on a doc
                           loaded within that attempt.
        on_deadlock(doc) : handed the doc whose save was rolled back, before the next
                           attempt discards it — for carrying forward whatever that
                           save did outside the database and the rollback could not
                           undo (an S3 upload, in practice; see set_style_image).

    Backs off with jitter so two victims of one collision don't line up and repeat
    it, and re-raises on the last attempt: a deadlock that survives four tries is a
    real contention problem and should be seen rather than absorbed.
    '''
    for attempt in range(_DEADLOCK_ATTEMPTS):
        doc = frappe.get_doc(DOCTYPE, name)
        try:
            write(doc)
            doc.save(ignore_permissions=True)
            frappe.db.commit()
            return doc
        except frappe.QueryDeadlockError:
            if on_deadlock:
                on_deadlock(doc)
            frappe.db.rollback()
            if attempt == _DEADLOCK_ATTEMPTS - 1:
                raise
            frappe.logger('prism.moodboard_style').warning(
                f'deadlock saving {DOCTYPE} {name}, retrying ({attempt + 1})')
            time.sleep(_DEADLOCK_BACKOFF * (2 ** attempt) * (0.5 + random.random()))


def _resolve_style(id=None, moodboard=None, index=None):
    '''
    A style by id, and only by id.

    (board, stream index) used to be accepted, on the assumption that a caller
    handling a product event might not have ids to hand. It is refused now, and
    loudly rather than quietly: save_generated_styles appends, so a board holds
    every run it has ever had, and every run numbers its products from 0. Index 0
    of the third run would resolve to the first run's opening style and overwrite
    a picture and a render cache that belong to a different garment — silently,
    since the row exists and the write succeeds.

    The ids are always available: the plan is saved 3-4 minutes before the first
    render lands, and that response carries `savedStyles[]` in plan order.
    '''
    if id:
        return _get_or_404(id)
    if moodboard or index is not None:
        frappe.throw(
            '`index` no longer identifies a style — a board holds every generated '
            'run, and each run numbers its products from 0. Pass `id`, from '
            "`savedStyles[<index>].id` in this run's save_generated_styles response."
        )
    frappe.throw('`id` is required to identify the style.')


def _first(src, *keys):
    ''' The first of `keys` present in the mapping, else None. '''
    for k in keys:
        if k in src:
            return src.get(k)
    return None


def _clean(value):
    return value.strip() if isinstance(value, str) else ('' if value is None else str(value))


@frappe.whitelist(allow_guest=True, methods=['POST'])
def upload_style_model(id=None):
    '''
    Upload a style's 3D model (GLB) as a multipart file (NOT base64). The file is
    streamed straight to S3 under models/moodboard/<board>/<hash>.glb and the
    object key is stored on the style; the read APIs serve it as a public URL.

    Send as multipart/form-data with:
        id         : the style id (form field or query param)
        model_3d   : the .glb file part  (alias: `file`)
    '''
    doc = _get_or_404(id)

    files = getattr(frappe.request, 'files', None) or {}
    fileobj = files.get('model_3d') or files.get('file')
    if not fileobj:
        frappe.throw('No model file uploaded (expected multipart field "model_3d").')

    _assert_model_size(fileobj)

    key = cloud.build_model_key(doc.moodboard, doc.name)
    cloud.upload_glb(fileobj.stream, key)

    doc.model_3d = cloud.asset_url(key)
    doc.save(ignore_permissions=True)
    frappe.db.commit()
    return _to_api(doc, full=True)


@frappe.whitelist(allow_guest=True, methods=['POST'])
def upload_style_garment_file(id=None):
    '''
    Upload a style's garment file as a multipart file (NOT base64). The file is
    streamed straight to S3 under files/moodboard/garment/<board>/<hash><ext>
    (original extension preserved) and the object key is stored on the style; the
    read APIs serve it as a public URL.

    Send as multipart/form-data with:
        id            : the style id (form field or query param)
        garment_file  : the file part  (alias: `file`)
    '''
    return _upload_style_document(
        id, ('garment_file',), 'garment_file',
        cloud.build_garment_file_key, MAX_GARMENT_FILE_SIZE_MB,
    )


@frappe.whitelist(allow_guest=True, methods=['POST'])
def upload_style_bom(id=None):
    '''
    Upload a style's BOM (Bill of Materials) file as a multipart file (NOT
    base64). Streamed straight to S3 under files/moodboard/bom/<board>/<hash><ext>
    and served as a public URL. See upload_style_garment_file.

    Send as multipart/form-data with:
        id   : the style id (form field or query param)
        bom  : the file part  (aliases: `bom_file`, `file`)
    '''
    return _upload_style_document(
        id, ('bom', 'bom_file'), 'bom',
        cloud.build_bom_file_key, MAX_BOM_FILE_SIZE_MB,
    )


@frappe.whitelist(allow_guest=True, methods=['POST'])
def upload_style_product_video(id=None):
    '''
    Upload a style's product video as a multipart file (NOT base64). Streamed
    straight to S3 under files/moodboard/video/<board>/<hash><ext> (original
    extension preserved) and served as a public URL. See upload_style_garment_file.

    Send as multipart/form-data with:
        id             : the style id (form field or query param)
        product_video  : the video file part  (aliases: `video`, `file`)
    '''
    return _upload_style_document(
        id, ('product_video', 'video'), 'product_video',
        cloud.build_product_video_key, MAX_VIDEO_FILE_SIZE_MB,
    )


@frappe.whitelist(allow_guest=True, methods=['POST'])
def upload_style_garment_images(id=None):
    '''
    Append one or more images (PNG/JPG/etc.) to a style's garment gallery. Each
    file is streamed straight to S3 under files/moodboard/garment_image/<board>/<hash><ext>
    (original extension preserved) and its public URL is appended as a new row in
    the style's `garment_images` child table; the read APIs serve it as `garmentImages`.

    Send as multipart/form-data with:
        id              : the style id (form field or query param)
        garment_images  : one or more image file parts (repeat the field for
                          multiple; aliases: `garment_image`, `images`, `image`, `file`)
    '''
    doc = _get_or_404(id)

    fileobjs = _collect_files(('garment_images', 'garment_image', 'images', 'image', 'file'))
    if not fileobjs:
        frappe.throw('No image uploaded (expected multipart field "garment_images").')

    # New images append after the current max display order, preserving the order
    # the files arrive in.
    order = max([(r.display_order or 0) for r in (doc.garment_images or [])], default=0)
    for fileobj in fileobjs:
        _assert_image_file(fileobj)
        _assert_file_size(fileobj, MAX_IMAGE_FILE_SIZE_MB, 'Image')
        filename = getattr(fileobj, 'filename', '') or ''
        key = cloud.build_garment_image_key(doc.moodboard, doc.name, cloud.file_ext(filename))
        cloud.upload_file(fileobj.stream, key, cloud.content_type_for(filename))
        order += 1
        doc.append('garment_images', {'image': cloud.asset_url(key), 'display_order': order})

    doc.save(ignore_permissions=True)
    frappe.db.commit()
    return _to_api(doc, full=True)


@frappe.whitelist(methods=['POST'])
def migrate_style_images(limit=None, ids=None, enqueue=0):
    '''
    Bulk-migrate every Moodboard Style whose per-garment crop `image` is still a
    local /files path over to S3. Uses a hook-free field update (see
    moodboard_style doctype's migrate_style_image), so it never flips a published
    board to "Unpublished Changes", re-parses BOMs, or rebuilds ESG. Idempotent, with
    per-style fault isolation + commit.

      ids:     explicit style names; default = all styles with a local image.
      limit:   optional cap for batched runs.
      enqueue: truthy -> run in a background worker and return {enqueued: True}.

    System Manager only. Returns {total, migrated, skipped, failed, errors}.
    '''
    frappe.only_for('System Manager')

    if frappe.utils.cint(enqueue):
        frappe.enqueue(
            'prism.api.moodboard_style.migrate_style_images',
            queue='long', timeout=14400, limit=limit, ids=ids, enqueue=0,
        )
        return {'enqueued': True}

    from prism.prism.doctype.moodboard_style.moodboard_style import migrate_style_image

    if ids:
        names = ids if isinstance(ids, list) else frappe.parse_json(ids)
        if not isinstance(names, list):
            names = [ids]
    else:
        names = frappe.get_all(
            DOCTYPE,
            or_filters=[['image', 'like', '/files/%'], ['image', 'like', '/private/files/%']],
            pluck='name', order_by='creation asc', limit=limit)

    summary = {'total': len(names), 'migrated': 0, 'skipped': 0, 'failed': 0, 'errors': []}
    for n in names:
        try:
            url = migrate_style_image(n)
            summary['migrated' if url else 'skipped'] += 1
            frappe.db.commit()
        except Exception as ex:
            frappe.db.rollback()
            summary['failed'] += 1
            summary['errors'].append({'style': n, 'error': str(ex)})
            frappe.log_error(frappe.get_traceback(), f'migrate_style_images {n}')
    return summary


@frappe.whitelist(allow_guest=True, methods=['POST'])
def delete_style_garment_image(id=None, image_id=None, url=None):
    '''
    Remove a single image from a style's garment gallery, matched by its row id
    (`image_id`, from the `garmentImages[].id` served to the client) or, failing
    that, its stored URL. Best-effort deletes the underlying S3 object. Returns the
    updated style.
    '''
    doc = _get_or_404(id)
    image_id = (image_id or '').strip()
    target = (url or '').strip()
    if not image_id and not target:
        frappe.throw('`image_id` or `url` of the image to remove is required.')

    def _matches(r):
        if image_id:
            return r.name == image_id
        return (r.image or '').strip() == target or cloud.asset_url(r.image) == target

    rows = doc.garment_images or []
    removed = [r for r in rows if _matches(r)]
    kept = [r for r in rows if not _matches(r)]
    if not removed:
        frappe.throw('Image not found in this style\'s gallery.', frappe.DoesNotExistError)

    doc.set('garment_images', kept)
    doc.save(ignore_permissions=True)
    frappe.db.commit()

    # Best-effort S3 cleanup — never block the removal on a bucket failure.
    for r in removed:
        key = cloud.asset_key(r.image)
        if not key:
            continue
        try:
            cloud.delete_object(key)
        except Exception:
            frappe.log_error(frappe.get_traceback(), 'delete_style_garment_image S3 cleanup')

    return _to_api(doc, full=True)


# One entry per S3-offloaded style asset. `is_glb` picks the fast-path (fixed
# model/gltf-binary content type, no extension in the key) vs the arbitrary-file
# path (extension preserved, content type inferred from the filename). `aliases`
# let the client name the type loosely (e.g. "glb", "video").
_STYLE_ASSET_TYPES = {
    'model_3d': {
        'aliases': ('glb', 'model', '3d', 'model3d'),
        'build_key': cloud.build_model_key,
        'max_mb': MAX_MODEL_FILE_SIZE_MB,
        'is_glb': True,
    },
    'garment_file': {
        'aliases': ('garment', 'garmentfile'),
        'build_key': cloud.build_garment_file_key,
        'max_mb': MAX_GARMENT_FILE_SIZE_MB,
        'is_glb': False,
    },
    'bom': {
        'aliases': ('bom_file', 'billofmaterials'),
        'build_key': cloud.build_bom_file_key,
        'max_mb': MAX_BOM_FILE_SIZE_MB,
        'is_glb': False,
    },
    'product_video': {
        'aliases': ('video', 'productvideo'),
        'build_key': cloud.build_product_video_key,
        'max_mb': MAX_VIDEO_FILE_SIZE_MB,
        'is_glb': False,
    },
}


@frappe.whitelist(allow_guest=True, methods=['POST'])
@auth_required
def upload_style_asset(id=None, type=None, asset_type=None, kind=None):
    '''
    Single multipart endpoint for all four S3-offloaded style assets — upload one
    asset per call, with `type` selecting which style field is set. Mirrors the
    dedicated upload_style_model / _garment_file / _bom / _product_video endpoints
    (kept for back-compat) behind one call.

    Auth: requires the X-Auth-Token JWT (@auth_required) and is restricted to
    internal / PSL users — brand users get 403.

    Send as multipart/form-data with:
        id    : the style id (form field or query param)
        type  : one of  model_3d | garment_file | bom | product_video
                (aliases accepted: glb/model, video, garment, bom_file)
        file  : the file part (the type-named part, e.g. `product_video`, also works)

    The file is streamed straight to S3 (no base64, no full-file buffering), so a
    40-45 MB GLB or video never sits fully in memory. Per-type size caps apply
    (model 50 MB, garment/BOM 25 MB, video 200 MB). The object key is stored on
    the style and the read APIs serve it as a public URL. Returns the updated
    style (same shape as get_style).
    '''
    _require_internal()
    canonical, cfg = _resolve_asset_type(type or asset_type or kind)
    doc = _get_or_404(id)

    files = getattr(frappe.request, 'files', None) or {}
    # Accept the canonical field name, any alias, or a generic `file` part.
    candidates = (canonical, *cfg['aliases'], 'file')
    fileobj = next((files[name] for name in candidates if files.get(name)), None)
    if not fileobj:
        frappe.throw(f'No file uploaded (expected multipart field "{canonical}" or "file").')

    _assert_file_size(fileobj, cfg['max_mb'], canonical)

    previous = (doc.get(canonical) or '').strip()

    if cfg['is_glb']:
        key = cfg['build_key'](doc.moodboard, doc.name)
        cloud.upload_glb(fileobj.stream, key)
    else:
        filename = getattr(fileobj, 'filename', '') or ''
        key = cfg['build_key'](doc.moodboard, doc.name, cloud.file_ext(filename))
        cloud.upload_file(fileobj.stream, key, cloud.content_type_for(filename))

    new_url = cloud.asset_url(key)
    doc.set(canonical, new_url)
    doc.save(ignore_permissions=True)
    frappe.db.commit()

    # Re-upload replaces the field with a fresh-hash key, so any prior object is
    # now orphaned — delete it. Done after the save (so a cleanup failure can't
    # leave the style pointing at a deleted file) and only for this one field.
    _delete_replaced_asset(previous, new_url)

    return _to_api(doc, full=True)


def _delete_replaced_asset(previous, current):
    ''' Best-effort delete of the S3 object a re-upload replaced (no-op if unchanged). '''
    if not previous or previous == current:
        return
    old_key = cloud.asset_key(previous)
    if not old_key:
        return
    try:
        cloud.delete_object(old_key)  # S3 delete is idempotent
    except Exception:
        frappe.log_error(frappe.get_traceback(), 'upload_style_asset replaced-object cleanup')


@frappe.whitelist(allow_guest=True, methods=['POST'])
@auth_required
def remove_style_asset(id=None, type=None, asset_type=None, kind=None):
    '''
    Remove one uploaded style asset: delete its S3 object and clear the field.
    `type` selects which asset (same values/aliases as upload_style_asset).
    Idempotent — clearing an already-empty field is a no-op. Returns the updated
    style (same shape as get_style).

    Auth: requires the X-Auth-Token JWT (@auth_required) and is restricted to
    internal / PSL users — brand users get 403.

    Send as form-data / query params:
        id    : the style id
        type  : one of  model_3d | garment_file | bom | product_video
                (aliases: glb/model, video, garment, bom_file)
    '''
    _require_internal()
    canonical, cfg = _resolve_asset_type(type or asset_type or kind)
    doc = _get_or_404(id)

    current = (doc.get(canonical) or '').strip()
    if current:
        key = cloud.asset_key(current)
        if key:
            cloud.delete_object(key)  # S3 delete is idempotent
        doc.set(canonical, '')
        doc.save(ignore_permissions=True)
        frappe.db.commit()
    return _to_api(doc, full=True)


def _resolve_asset_type(requested):
    ''' Map a caller-supplied asset type (or alias) to (canonical, config); 400s otherwise. '''
    key = (requested or '').strip().lower().replace('-', '_').replace(' ', '_')
    if not key:
        allowed = ', '.join(_STYLE_ASSET_TYPES)
        frappe.throw(f'Missing upload type. Pass `type` as one of: {allowed}.')
    if key in _STYLE_ASSET_TYPES:
        return key, _STYLE_ASSET_TYPES[key]
    for canonical, cfg in _STYLE_ASSET_TYPES.items():
        if key in cfg['aliases']:
            return canonical, cfg
    allowed = ', '.join(_STYLE_ASSET_TYPES)
    frappe.throw(f'Unknown upload type "{requested}". Expected one of: {allowed}.')


@frappe.whitelist(allow_guest=True, methods=['POST'])
def update_style_cost(id=None, inputs=None, result=None, totals=None,
                      currency=None, fx_rate=None, order_quantity=None):
    '''
    Persist a computed cost: flatten `totals` into the summary columns, store the
    full inputs/result JSON (re-editable + audit snapshot), stamp computed_at.
    '''
    doc = _get_or_404(id)
    totals = _as_dict(totals)
    rollup = _as_dict(totals.get('rollup'))

    doc.cost_inputs = _dump(inputs)
    doc.cost_result = _dump(result)

    doc.cost_fabric = _num(totals.get('fabric'))
    doc.cost_trims = _num(totals.get('trims'))
    doc.cost_print = _num(totals.get('print'))
    doc.cost_embroidery = _num(totals.get('embroidery'))
    doc.cost_sam = _num(totals.get('sam'))
    doc.cost_base = _num(totals.get('base'))
    doc.cost_final = _num(totals.get('final'))
    doc.cost_final_in_currency = _num(
        rollup.get('finalCostInCurrency') if rollup.get('finalCostInCurrency') is not None
        else totals.get('finalCostInCurrency')
    )

    doc.cost_currency = currency or rollup.get('currencyCode') or 'INR'
    doc.cost_fx_rate = _num(fx_rate)
    doc.cost_order_quantity = _int(
        order_quantity if order_quantity is not None
        else _dig(inputs, 'print_sections', 'order_quantity')
    )

    doc.cost_status = 'Computed'
    doc.cost_computed_at = frappe.utils.now_datetime()

    doc.save(ignore_permissions=True)
    frappe.db.commit()
    return _to_api(doc, full=True)


@frappe.whitelist(allow_guest=True, methods=['POST'])
def delete_style(id=None, ids=None):
    '''
    Remove a style — or a set of them — along with the embedded cost, the ESG
    row, the render cache and the S3 assets (see MoodboardStyle.on_trash).

    This is the only way a generated style leaves a board. save_generated_styles
    appends, so a regenerate no longer clears what came before it; when the user
    wants an older set gone they say so, and this is the call that says it. Pass
    the whole set in `ids` and it is one round trip.

        id  : a single style
        ids : several, as a JSON array or a comma list. Combined with `id`.

    An id that does not exist is skipped rather than fatal — a double-click on
    "remove" must not fail the half of the batch that already went. All of them
    missing is still a 404, so a caller with a wholly stale list hears about it.

    Note this endpoint predates the JWT decorators and deliberately still lacks
    them: _require_internal reads frappe.local.jwt_payload, which only exists
    under @auth_required, so adding the guard would also force the X-Auth-Token
    header on the legacy StylesStep caller (whose sibling write path,
    sync_styles, is likewise undecorated).

    Returns { success, deleted, id, ids[], missing[], moodboard }.
    '''
    wanted = _board_ids(([id] if id else []) + _board_ids(ids))
    if not wanted:
        frappe.throw('`id` or `ids` is required.')

    gone, missing, moodboard = [], [], None
    for name in wanted:
        board = frappe.db.get_value(DOCTYPE, name, 'moodboard')
        if board is None:
            missing.append(name)
            continue
        moodboard = moodboard or board
        frappe.delete_doc(DOCTYPE, name, ignore_permissions=True, force=True)
        gone.append(name)

    if not gone:
        frappe.throw('Style not found', frappe.DoesNotExistError)

    frappe.db.commit()
    # `id` is kept for the single-delete callers that predate `ids`.
    return {'success': True, 'deleted': len(gone), 'id': id, 'ids': gone,
            'missing': missing, 'moodboard': moodboard}


# =====================================================================
# Compat rebuild — consumed by prism.api.moodboard.get_draft (§4)
# =====================================================================

def build_extracted_styles(moodboard):
    '''
    Rebuild `editorV2.foundation.extracted_styles[]` from the rows, ordered by
    idx, in the exact shape the read consumers expect:
        { id, IMAGE_image, include, moq, attrs: { ... } }
    '''
    out = []
    for doc in _board_docs(moodboard):
        out.append({
            'id': doc.name,
            'IMAGE_image': doc.image or '',
            'include': bool(doc.include),
            'moq': doc.moq or _DEFAULT_MOQ,
            'attrs': _attrs_of(doc),
        })
    return out


def build_cost_calculation(moodboard):
    '''
    Rebuild the `editorV2.cost_calculation` map from the rows:
        { _meta: {...}, [styleId]: { inputs, result, totals } }
    Only styles with a computed cost contribute entries; `_meta` (board-level
    display currency/fx) is assembled from the costed rows.
    '''
    docs = _board_docs(moodboard)
    costed = [d for d in docs if d.cost_status == 'Computed']
    if not costed:
        return None

    # Board-level display settings — shared across styles in the UI; derive from
    # the costed rows (first non-INR wins for the rate table, else identity).
    currency = 'INR'
    fx = 1.0
    for d in costed:
        if d.cost_currency and d.cost_currency != 'INR':
            currency = d.cost_currency
            fx = d.cost_fx_rate or fx
            break
    cost_calc = {'_meta': {'currency': currency, 'fx': fx, 'fxRates': {currency: fx}}}

    for d in costed:
        cost_calc[d.name] = {
            'inputs': _parse(d.cost_inputs, {}),
            'result': _parse(d.cost_result, None),
            'totals': _totals_of(d),
        }
    return cost_calc


# =====================================================================
# Serializers
# =====================================================================

def _list(moodboard, origin=None):
    return [_to_api(d) for d in _board_docs(moodboard, origin=origin)]


def _to_api(doc, full=False):
    '''
    Frontend-facing record. `image` is the stored /files path (the service
    layer resolves it for display). Cost summary present only when computed;
    full inputs/result attached only for get_style / mutation responses.
    '''
    out = {
        'id': doc.name,
        'moodboard': doc.moodboard,
        'createdAt': _iso(doc.creation),
        'idx': doc.idx or 0,
        'include': bool(doc.include),
        # Absent must read as true on the client (same rule as isStyleIncluded):
        # rows written before the field existed are in the board, not stranded
        # outside it. Always sent from here, so that fallback is a safety net
        # rather than the normal path.
        'inMoodboard': bool(doc.in_moodboard),
        'image': doc.image or '',
        'imageStatus': doc.image_status or 'pending',
        'imageError': doc.image_error or None,
        'model3d': cloud.asset_url(doc.model_3d),
        'garmentFile': cloud.asset_url(doc.garment_file),
        'garmentImages': _garment_images_of(doc),
        'bom': cloud.asset_url(doc.bom),
        'productVideo': cloud.asset_url(doc.product_video),
        'consumption': doc.consumption,
        'markerEfficiency': doc.marker_efficiency,
        'moq': doc.moq or _DEFAULT_MOQ,
        'fabCode': doc.fab_code or '',
        # '' = this style names a quality but no particular lot. Match on fabCode
        # alone there; anything else would claim a lot nobody chose.
        'fabBatch': doc.fab_batch or '',
        'renderKey': _current_render_key(doc),
        'attrs': _attrs_of(doc),
        'design': _design_of(doc),
        'decision': _decision_of(doc),
        'cost': _cost_summary(doc),
        'esg': _esg_snapshot(doc.name),
    }
    if full:
        out['costInputs'] = _parse(doc.cost_inputs, None)
        out['costResult'] = _parse(doc.cost_result, None)
        out['renders'] = _renders_of(doc)
    return out


def _design_of(doc):
    '''
    The generator's argument for the style — what the drawer renders. Internal
    only: it is deliberately outside `attrs`, which is what the brand-facing
    published snapshot is built from.
    '''
    return {
        'fabCode': doc.fab_code or '',
        'fabBatch': doc.fab_batch or '',
        'reason': doc.reason or '',
        'fabricRationale': doc.fabric_rationale or '',
        'designBrief': doc.design_brief or '',
        'signatureDetails': _parse(doc.signature_details, []) or [],
        'colourTreatment': doc.colour_treatment or '',
        'printLabel': doc.print_label or '',
        'printApplication': doc.print_application or '',
        'secondaryColour': (
            {
                'name': doc.secondary_colour or '',
                'hex': doc.secondary_colour_hex or '',
                'pantone': doc.secondary_colour_tcx or '',
            }
            if (doc.secondary_colour or doc.secondary_colour_hex or doc.secondary_colour_tcx)
            else None
        ),
    }


def _decision_of(doc):
    ''' The verdict, and what `include` was derived from. '''
    return {
        'decision': doc.decision or 'Pending',
        'comment': doc.decision_comment or '',
        'decidedBy': doc.decided_by or None,
        'decidedOn': _iso(doc.decided_on),
    }


def _renders_of(doc):
    ''' Every fabric this style has been rendered against, current one first. '''
    rows = sorted(
        (doc.renders or []),
        key=lambda r: (0 if frappe.utils.cint(r.is_current) else 1, r.idx or 0),
    )
    return [_render_obj(r) for r in rows]


def _render_obj(row):
    return {
        'id': row.name,
        'renderKey': row.render_key,
        'fabCode': row.fab_code or '',
        'fabBatch': row.fab_batch or '',
        'fabricQuality': row.fabric_quality or '',
        'elementColourTcx': row.element_colour_tcx or '',
        'printLabel': row.print_label or '',
        'recolour': row.recolour or '',
        'image': cloud.asset_url(row.image) if row.image else '',
        'status': row.status or 'ready',
        'error': row.error or None,
        'isCurrent': bool(frappe.utils.cint(row.is_current)),
    }


def _current_render_key(doc):
    ''' The key of the render currently on the card, or None before the first one. '''
    for row in (doc.renders or []):
        if frappe.utils.cint(row.is_current):
            return row.render_key
    return None


def _esg_snapshot(style_name):
    '''
    Live ESG rating/category/score for a style, read fresh from the Moodboard ESG
    record (not any frozen board snapshot). None until an ESG exists.
    '''
    row = frappe.db.get_value(
        'Moodboard ESG', {'moodboard_style': style_name},
        ['overall_rating', 'category', 'score_percent'], as_dict=True,
    )
    if not row:
        return None
    return {
        'rating': row.get('overall_rating'),
        'category': row.get('category'),
        'score': row.get('score_percent'),
    }


def _cost_summary(doc):
    if doc.cost_status != 'Computed':
        return None
    return {
        'status': doc.cost_status,
        'orderQuantity': doc.cost_order_quantity,
        'currency': doc.cost_currency or 'INR',
        'fxRate': doc.cost_fx_rate,
        'fabric': doc.cost_fabric,
        'trims': doc.cost_trims,
        'print': doc.cost_print,
        'embroidery': doc.cost_embroidery,
        'sam': doc.cost_sam,
        'base': doc.cost_base,
        'final': doc.cost_final,
        'finalInCurrency': doc.cost_final_in_currency,
        'computedAt': _iso(doc.cost_computed_at),
    }


def _garment_images_of(doc):
    '''
    The style's garment gallery as a list of image objects, sorted ascending by
    displayOrder. Backed by the `garment_images` child table (one Moodboard Style
    Garment Image row per image); each row's stored URL is mapped through asset_url
    (idempotent — full URLs pass through, bare keys are resolved).

    Each item: { id, url, displayOrder }.
    '''
    rows = [r for r in (doc.garment_images or []) if r.image]
    rows.sort(key=lambda r: ((r.display_order if r.display_order is not None else 0), r.idx or 0))
    return [
        {'id': r.name, 'url': cloud.asset_url(r.image), 'displayOrder': r.display_order or 0}
        for r in rows
    ]


def _attrs_of(doc):
    attrs = {f: (doc.get(f) or '') for f in _ATTR_FIELDS}
    extra = _parse(doc.attrs_extra, {})
    if isinstance(extra, dict):
        # Promoted columns win over any stale copy in attrs_extra.
        for k, v in extra.items():
            attrs.setdefault(k, v)
    return attrs


def _totals_of(doc):
    ''' Reconstruct the `totals` block the quote engine / costing tabs read. '''
    base = doc.cost_base or 0
    return {
        'fabric': doc.cost_fabric or 0,
        'trims': doc.cost_trims or 0,
        'print': doc.cost_print or 0,
        'embroidery': doc.cost_embroidery or 0,
        'sam': doc.cost_sam or 0,
        'base': base,
        'total': base,
        'final': doc.cost_final or 0,
        'rollup': (_parse(doc.cost_result, {}) or {}).get('rollup') or {
            'final': doc.cost_final or 0,
            'currencyCode': doc.cost_currency or 'INR',
            'finalCostInCurrency': doc.cost_final_in_currency or 0,
        },
    }


# =====================================================================
# Mutation helpers
# =====================================================================

def _flatten_payload(src):
    '''
    The writable scalar fields present in a payload, keyed by doctype fieldname.

    One normalizer for three dialects: the doctype's own snake_case, the
    frontend's camelCase, and the generator's wire shape (`styleCategory`,
    `fabricName`, `fabricCode`, the `colour` / `secondaryColour` objects,
    `signatureDetails[]`, `index`). A generated style can therefore be passed to
    the write APIs verbatim.

    Presence-preserving: a key absent from the payload is absent from the result,
    which is what keeps every write path a PATCH. Flat keys beat the same field
    nested under `attrs`.
    '''
    src = _as_dict(src)
    out = {}

    for source in (_as_dict(src.get('attrs')), src):
        for field, keys in _INBOUND_KEYS.items():
            for k in keys:
                if k in source:
                    out[field] = source[k]
                    break

    # Colour objects override the flat spellings — a caller sending both meant the
    # structured one.
    for key, (name_f, hex_f, tcx_f) in _COLOUR_OBJECTS.items():
        if key not in src:
            continue
        colour = src.get(key)
        if colour is None:
            out[name_f] = out[hex_f] = out[tcx_f] = None
            continue
        colour = _as_dict(colour)
        if colour:
            out[name_f] = colour.get('name')
            out[hex_f] = colour.get('hex')
            out[tcx_f] = colour.get('pantone') if 'pantone' in colour else colour.get('tcx')

    for key in ('signature_details', 'signatureDetails'):
        if key in src:
            out['signature_details'] = _dump(src.get(key))
            break

    return out


def _apply_details(doc, src, is_new=False):
    ''' Apply attribute / design / moq / include / image fields present in `src`. '''
    src = _as_dict(src)
    flat = _flatten_payload(src)

    for f in (*_ATTR_FIELDS, *_DESIGN_FIELDS, 'signature_details'):
        if f in flat:
            doc.set(f, flat.get(f))

    if 'attrs_extra' in src:
        doc.attrs_extra = _dump(src.get('attrs_extra'))

    if 'moq' in src:
        doc.moq = _int(src.get('moq')) or _DEFAULT_MOQ
    elif is_new and not doc.moq:
        doc.moq = _DEFAULT_MOQ

    if 'include' in src:
        doc.include = 1 if _truthy(src.get('include')) else 0

    if 'idx' in flat:
        doc.idx = _int(flat.get('idx')) or 0

    # The verdict. `include` is derived from it in the doctype controller, so the
    # two can never be written into disagreement from here.
    if 'decision' in flat:
        doc.decision = _decision(flat.get('decision'))
    if 'decision_comment' in flat:
        doc.decision_comment = flat.get('decision_comment')

    # Render state. Only honoured when explicitly sent — the controller derives it
    # from `image` otherwise, so a plain image save can't leave a stale status.
    if 'image_status' in flat:
        doc.image_status = _image_status(flat.get('image_status'))
    if 'image_error' in flat:
        doc.image_error = flat.get('image_error')

    # Image: accept IMAGE_image (base64 or /files path) or a plain `image`.
    img = src.get('IMAGE_image') if 'IMAGE_image' in src else src.get('image')
    if img is not None:
        doc.image = _save_image(img)

    # 3D model (GLB): passthrough / clear only. The binary is never sent here —
    # it is uploaded to S3 via the dedicated multipart endpoint upload_style_model
    # (or, for Desk attachments, offloaded to S3 in the doctype controller's
    # before_save). Accept an existing URL round-tripped by the client, or '' to
    # clear. A local /files path is left as-is and offloaded on save.
    if 'model_3d' in src or 'model3d' in src:
        model = src.get('model_3d') if 'model_3d' in src else src.get('model3d')
        doc.model_3d = (model.strip() if isinstance(model, str) else model) or None

    # Garment file / BOM: same passthrough / clear contract as model_3d. The
    # binary is uploaded via the dedicated multipart endpoints (or offloaded from
    # a Desk attachment in before_save); here we only round-trip an existing URL,
    # accept a local /files path (offloaded on save), or '' to clear.
    for target, keys in (
        ('garment_file', ('garment_file', 'garmentFile')),
        ('bom', ('bom', 'bom_file')),
        ('product_video', ('product_video', 'productVideo')),
    ):
        present = next((k for k in keys if k in src), None)
        if present is not None:
            val = src.get(present)
            doc.set(target, (val.strip() if isinstance(val, str) else val) or None)

    # Garment image gallery: passthrough / replace of the whole child table (reorder
    # or removal from a client save). The binaries are added via the dedicated
    # multipart endpoint upload_style_garment_images; here we only round-trip an
    # existing list of images — strings, or {url|imageUrl|image_url} + optional
    # {displayOrder|display_order} dicts — rebuilding the rows. An explicit
    # displayOrder wins; otherwise the array position sets the order. [] / None clears.
    if 'garment_images' in src or 'garmentImages' in src:
        images = src.get('garment_images') if 'garment_images' in src else src.get('garmentImages')
        rows = []
        for i, item in enumerate(util_as_list(images)):
            if isinstance(item, str):
                url, order = item, i + 1
            else:
                url = _dig(item, 'url') or _dig(item, 'imageUrl') or _dig(item, 'image_url') or _dig(item, 'image')
                order = _int(_dig(item, 'displayOrder')) or _int(_dig(item, 'display_order')) or (i + 1)
            url = (url or '').strip() if isinstance(url, str) else url
            if url:
                rows.append({'image': url, 'display_order': order})
        doc.set('garment_images', rows)


def _save_image(value, max_mb=MAX_STYLE_IMAGE_FILE_SIZE_MB):
    '''
    base64 data URL -> uploaded /files path; an existing path/URL is kept.

    The generator hands back `data:image/png;base64,…` several MB at a time, so
    this is where those megabytes stop being a string: decoded to a File on the
    way in, then offloaded to S3 by the doctype's before_save.
    '''
    if not value or not isinstance(value, str):
        return None
    if value.startswith('data:'):
        return util.save_file(value, max_mb)
    return value


def _decision(value):
    ''' Validate a verdict against the Select options (case-insensitively). '''
    wanted = (value or '').strip().lower()
    for d in _DECISIONS:
        if d.lower() == wanted:
            return d
    frappe.throw(f'Unknown decision "{value}". Expected one of: {", ".join(_DECISIONS)}.')


def _flag(value, label):
    '''
    A boolean from the wire, strictly. Unlike _truthy — which reads anything it
    does not recognise as false — an unparseable value is refused here, because
    this drives a setter: a typo silently meaning "remove it from the board" is
    the one outcome the caller cannot see.
    '''
    if value in (True, 1, '1', 'true', 'True'):
        return 1
    if value in (False, 0, '0', 'false', 'False'):
        return 0
    frappe.throw(f'`{label}` must be true or false, got "{value}".', InvalidRequest)


def _image_status(value):
    ''' Validate a render status against the Select options (case-insensitively). '''
    wanted = (value or '').strip().lower()
    if wanted in _IMAGE_STATUSES:
        return wanted
    frappe.throw(f'Unknown image status "{value}". Expected one of: {", ".join(_IMAGE_STATUSES)}.')


def _assert_model_size(fileobj):
    ''' Reject GLB uploads over MAX_MODEL_FILE_SIZE_MB by measuring the stream. '''
    _assert_file_size(fileobj, MAX_MODEL_FILE_SIZE_MB, 'Model file')


def _collect_files(field_aliases):
    '''
    All uploaded file parts across the given multipart field names, preserving
    order. Uses werkzeug's getlist so several files sent under the same field
    (e.g. a multi-select gallery upload) are all returned, not just the first.
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


def _assert_image_file(fileobj):
    ''' Reject a non-image upload (by filename MIME / content type), so the gallery holds images only. '''
    filename = getattr(fileobj, 'filename', '') or ''
    content_type = (getattr(fileobj, 'content_type', '') or cloud.content_type_for(filename) or '')
    if not content_type.startswith('image/'):
        frappe.throw(f'"{filename or "file"}" is not an image. Only image files (PNG, JPG, etc.) are allowed.')


def _assert_file_size(fileobj, max_mb, label='File'):
    ''' Reject uploads over `max_mb` by measuring the stream without buffering it. '''
    stream = fileobj.stream
    stream.seek(0, 2)  # SEEK_END
    size = stream.tell()
    stream.seek(0)
    if size > max_mb * 1024 * 1024:
        frappe.throw(f'{label} exceeds the maximum allowed limit of {max_mb} MB.')


def _upload_style_document(id, form_fields, target_field, build_key, max_mb):
    '''
    Shared multipart-upload flow for a style document field (garment file, BOM):
    mirror upload_style_model but for arbitrary file types — preserve the original
    extension in the S3 key and infer the content type from the filename.
    '''
    doc = _get_or_404(id)

    files = getattr(frappe.request, 'files', None) or {}
    fileobj = None
    for fld in form_fields:
        fileobj = files.get(fld)
        if fileobj:
            break
    fileobj = fileobj or files.get('file')
    if not fileobj:
        expected = '" / "'.join(form_fields)
        frappe.throw(f'No file uploaded (expected multipart field "{expected}").')

    _assert_file_size(fileobj, max_mb)

    filename = getattr(fileobj, 'filename', '') or ''
    key = build_key(doc.moodboard, doc.name, cloud.file_ext(filename))
    cloud.upload_file(fileobj.stream, key, cloud.content_type_for(filename))

    doc.set(target_field, cloud.asset_url(key))
    doc.save(ignore_permissions=True)
    frappe.db.commit()
    return _to_api(doc, full=True)


def _require_internal():
    ''' Restrict to internal / PSL users (no Brand User mapping); brand users get 403. '''
    if util.get_current_brand():
        frappe.throw('Only internal users can access this resource.', frappe.PermissionError)


def _get_or_new(name, moodboard):
    if name and frappe.db.exists(DOCTYPE, name):
        return frappe.get_doc(DOCTYPE, name)
    return frappe.new_doc(DOCTYPE)


def _get_or_404(name):
    if not name or not frappe.db.exists(DOCTYPE, name):
        frappe.throw('Style not found', frappe.DoesNotExistError)
    return frappe.get_doc(DOCTYPE, name)


# Where a style came from, as a filter on the one column that tells them apart:
# only the generator writes a design_brief (it is the instruction the renderer was
# given, so a generated style always has one).
_ORIGIN_FILTERS = {
    'generated': {'design_brief': ['is', 'set']},
    'extracted': {'design_brief': ['is', 'not set']},
    'all': {},
}


def _board_docs(moodboard, origin=None):
    filters = {'moodboard': moodboard}
    origin = (origin or 'all').strip().lower()
    if origin not in _ORIGIN_FILTERS:
        frappe.throw(f'Unknown origin "{origin}". Expected one of: {", ".join(_ORIGIN_FILTERS)}.')
    filters.update(_ORIGIN_FILTERS[origin])

    names = frappe.get_all(
        DOCTYPE, filters=filters,
        order_by='idx asc, creation asc', pluck='name', ignore_permissions=True,
    )
    return [frappe.get_doc(DOCTYPE, n) for n in names]


def _assert_board(moodboard):
    if not moodboard or not frappe.db.exists('Moodboard', moodboard):
        frappe.throw('Moodboard does not exist', frappe.DoesNotExistError)


def _require_internal():
    ''' PSL / internal users only — a brand on the JWT means a brand user: forbidden. '''
    if util.get_current_brand():
        frappe.throw('Internal users only', frappe.PermissionError)


def _board_ids(value):
    ''' Normalize the `moodboards` input (JSON array / comma list) to a deduped id list. '''
    if isinstance(value, str) and value.strip() and not value.strip().startswith('['):
        raw = value.split(',')
    else:
        raw = util_as_list(value)
    seen, out = set(), []
    for v in raw:
        v = v.strip() if isinstance(v, str) else v
        if v and v not in seen:
            seen.add(v)
            out.append(v)
    return out


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


def _page_envelope(items, total, limit, offset):
    return {'total': total, 'limit': limit, 'offset': offset, 'items': items}


def util_as_list(value):
    if isinstance(value, list):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = frappe.parse_json(value)
            return parsed if isinstance(parsed, list) else []
        except Exception:
            return []
    return []


def _as_dict(value):
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = frappe.parse_json(value)
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            return {}
    return {}


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


def _dig(value, *keys):
    cur = _as_dict(value)
    for k in keys[:-1]:
        cur = _as_dict(cur.get(k))
    return cur.get(keys[-1]) if cur else None


def _num(value):
    try:
        return float(value) if value not in (None, '') else None
    except (TypeError, ValueError):
        return None


def _int(value):
    try:
        return int(value) if value not in (None, '') else None
    except (TypeError, ValueError):
        return None


def _truthy(value):
    return value in (True, 1, '1', 'true', 'True')


def _iso(value):
    if not value:
        return None
    return frappe.utils.get_datetime(value).isoformat()
