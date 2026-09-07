'''
Requests backend — quote & sample requests, negotiation state machine.

Replaces the temporary Supabase shim (prism-web `lib/requests/store.js`). A
single `Request` DocType (request_type = Quote | Sample) with two child tables
(`Request Line`, `Request Negotiation Event`) backs the whole flow.

Identity comes from the JWT session (set by prism.auth.authenticator —
frappe.set_user + frappe.local.jwt_payload), exactly like the rest of
prism.api.*. The state machine (append history / clear counter / fold counter
into quote / legal transitions) lives server-side here + in the Request
controller `validate`, never in the browser.

Every method returns the FULL record in the camelCase shape `recordFromRow()`
produced in the old store, wrapped by Frappe's `{ message: ... }` envelope so
`frappeUnwrap()` works unchanged.
'''

import frappe

from prism.auth.authenticator import auth_required
import prism.api.util as util
import prism.lib.cloud as cloud

DOCTYPE = 'Request'

# Cap for base64 images decoded on save (AI-coloured line render), matching the
# other image endpoints (moodboard / page builder).
MAX_IMAGE_FILE_SIZE_MB = 5

# Currencies the UI offers (the validation is light — the client is the source
# of the picker). Kept here for reference / future server-side validation.
_CURRENCIES = ('USD', 'EUR', 'INR')

# request_type <-> record `type`
_TYPE_TO_DB = {'quote': 'Quote', 'sample': 'Sample'}
_TYPE_FROM_DB = {'Quote': 'quote', 'Sample': 'sample'}

# status <-> record `status`
_STATUS_TO_DB = {
    'pending': 'Pending', 'quoted': 'Quoted', 'countered': 'Countered',
    'approved': 'Approved', 'rejected': 'Rejected',
}

# Pagination bounds for list_requests (mirrors prism.api.moodboard_v2).
_DEFAULT_LIMIT = 20
_MAX_LIMIT = 100


# =====================================================================
# Whitelisted endpoints (one per old store operation, §3)
# =====================================================================

@frappe.whitelist(allow_guest=True, methods=['POST'])
@auth_required
def create_request(type=None, brand=None, source=None, lines=None, notes=None, **kwargs):
    '''
    Insert a Request + child lines. status = Pending. Brand & contact are
    FORCED from the session — any client-supplied brand/contact is ignored (§4).
    Brand users only.
    '''
    user, brand_ctx = _identity()
    if not brand_ctx:
        _forbidden('Only brand users can create requests')

    req_type = _TYPE_TO_DB.get(str(type or '').lower())
    if not req_type:
        _bad_request("type must be 'quote' or 'sample'")

    source = _as_dict(source)
    lines = _as_list(lines)

    doc = frappe.new_doc(DOCTYPE)
    doc.request_type = req_type
    doc.status = 'Pending'

    # Brand / contact forced from session — never from the client body.
    doc.brand = brand_ctx['id']
    doc.brand_contact_name = _display_name(user)
    doc.brand_email = user

    # Source (flattened) + resolved board id for the cost/quote engine.
    doc.source_kind = source.get('kind')
    doc.source_id = source.get('id')
    doc.source_title = source.get('title')
    doc.source_thumbnail = source.get('thumbnail')
    doc.source_season = source.get('season')
    doc.moodboard = _resolve_moodboard_id(source)

    doc.notes = (notes or '').strip()

    for line in lines:
        doc.append('lines', _line_to_row(line))

    doc.insert(ignore_permissions=True)
    frappe.db.commit()
    _notify_psl_new(doc)
    return _ok(doc)


@frappe.whitelist(allow_guest=True)
@auth_required
def list_requests(type=None, status=None, brand_id=None, limit=None, offset=None, **kwargs):
    '''
    Role-scoped list (§4). Brand users see only their own brand's requests
    (brand_id from the client is ignored); internal/PSL users see all and may
    optionally filter by brand_id.

    Paginated (mirrors prism.api.moodboard_v2.list_moodboards): returns
    { total, limit, offset, items } where `items` is the page of full records,
    newest first. `total` is the unpaginated count for the current filters.
    '''
    user, brand_ctx = _identity()
    limit, offset = _page(limit, offset)

    filters = {}
    if brand_ctx:
        filters['brand'] = brand_ctx['id']        # forced — ignore client brand_id
    elif brand_id:
        filters['brand'] = brand_id

    db_type = _TYPE_TO_DB.get(str(type or '').lower())
    if db_type:
        filters['request_type'] = db_type

    db_status = _STATUS_TO_DB.get(str(status or '').lower())
    if db_status:
        filters['status'] = db_status

    total = frappe.db.count(DOCTYPE, filters)
    names = frappe.get_all(
        DOCTYPE, filters=filters, order_by='creation desc',
        pluck='name', ignore_permissions=True,
        limit_start=offset, limit_page_length=limit,
    )
    items = [record_from_doc(frappe.get_doc(DOCTYPE, n)) for n in names]
    return _page_envelope(items, total, limit, offset)


@frappe.whitelist(allow_guest=True)
@auth_required
def get_request(id=None, **kwargs):
    ''' Single full record. Brand users may only read their own brand's. '''
    doc = _get_request_or_404(id)
    _assert_can_read(doc)
    return _ok(doc)


@frappe.whitelist(allow_guest=True, methods=['POST'])
@auth_required
def update_status(id=None, status=None, comment=None, **kwargs):
    '''
    PSL approve / reject / re-open. Writes the review fields when a comment is
    supplied (mirrors the old store: a re-open or comment-less decision leaves
    the quote review untouched). Internal/PSL users only.
    '''
    doc = _get_request_or_404(id)
    _claim_or_assert_assignee(doc)

    db_status = _STATUS_TO_DB.get(str(status or '').lower())
    if not db_status:
        _bad_request('Unknown status')

    doc.status = db_status

    trimmed = (comment or '').strip()
    if db_status != 'Pending' and trimmed:
        doc.review_decision = db_status.lower()   # 'approved' | 'rejected'
        doc.review_comment = trimmed
        doc.review_at = frappe.utils.now_datetime()

    doc.save(ignore_permissions=True)
    frappe.db.commit()

    if db_status == 'Approved':
        _notify_brand(doc, 'request_approved', f'Your request {doc.name} was approved')
    elif db_status == 'Rejected':
        _notify_brand(doc, 'request_rejected',
                      f'Your request {doc.name} was rejected', body=trimmed)
    return _ok(doc)


@frappe.whitelist(allow_guest=True, methods=['POST'])
@auth_required
def submit_quote(id=None, quote=None, **kwargs):
    '''
    PSL "Give Quote". Draft => merge quote fields only, no status change.
    Published => status Quoted, append a `quote` history event, clear any
    standing counter. Internal/PSL users only.
    '''
    doc = _get_request_or_404(id)
    _claim_or_assert_assignee(doc)
    quote = _as_dict(quote)

    is_draft = str(quote.get('status') or '').lower() == 'draft'
    _apply_quote_fields(doc, quote)
    doc.quote_status = 'Draft' if is_draft else 'Published'

    if not is_draft:
        doc.status = 'Quoted'
        _append_event(doc, actor='psl', kind='quote',
                      price=quote.get('price'), currency=quote.get('currency'),
                      qty=quote.get('qty'), delivery_date=quote.get('quotedDeliveryDate'),
                      note=quote.get('comment'))
        _clear_counter(doc)

    doc.save(ignore_permissions=True)
    frappe.db.commit()

    # Only a PUBLISHED quote is brand-facing; a draft save is internal-only.
    if not is_draft:
        _notify_brand(doc, 'request_quoted',
                      f'Your request {doc.name} has been quoted',
                      body=_price_summary(doc.quote_currency, doc.quote_price))
    return _ok(doc)


@frappe.whitelist(allow_guest=True, methods=['POST'])
@auth_required
def accept_quote(id=None, **kwargs):
    ''' Brand accepts the standing quote → Approved (accepted_via = quote). '''
    doc = _get_request_or_404(id)
    _require_brand_owner(doc)

    doc.status = 'Approved'
    doc.accepted_via = 'quote'
    doc.accepted_at = frappe.utils.now_datetime()

    doc.save(ignore_permissions=True)
    frappe.db.commit()
    _notify_assignee(doc, 'request_accepted',
                     f'{_brand_name(doc)} accepted your quote on {doc.name}')
    return _ok(doc)


@frappe.whitelist(allow_guest=True, methods=['POST'])
@auth_required
def submit_counter(id=None, counter=None, **kwargs):
    ''' Brand counter-offer → Countered, fill counter fields, log the event. '''
    doc = _get_request_or_404(id)
    _require_brand_owner(doc)
    counter = _as_dict(counter)

    doc.status = 'Countered'
    doc.counter_price = _num(counter.get('price'))
    doc.counter_currency = counter.get('currency')
    doc.counter_qty = _int(counter.get('qty'))
    doc.counter_delivery_date = counter.get('deliveryDate') or None
    doc.counter_note = counter.get('note')
    doc.counter_at = frappe.utils.now_datetime()

    _append_event(doc, actor='brand', kind='counter',
                  price=counter.get('price'), currency=counter.get('currency'),
                  qty=counter.get('qty'), delivery_date=counter.get('deliveryDate'),
                  note=counter.get('note'))

    doc.save(ignore_permissions=True)
    frappe.db.commit()
    _notify_assignee(doc, 'request_countered',
                     f'{_brand_name(doc)} sent a counter-offer on {doc.name}',
                     body=_price_summary(doc.counter_currency, doc.counter_price))
    return _ok(doc)


@frappe.whitelist(allow_guest=True, methods=['POST'])
@auth_required
def accept_counter(id=None, **kwargs):
    '''
    PSL accepts the brand's counter → Approved at the counter terms. Folds the
    agreed price / qty / delivery into the quote and clears the counter.
    Internal/PSL users only.
    '''
    doc = _get_request_or_404(id)
    _claim_or_assert_assignee(doc)

    if not doc.counter_at:
        _bad_request('No standing counter to accept')

    if doc.counter_price:
        doc.quote_price = doc.counter_price
    if doc.counter_currency:
        doc.quote_currency = doc.counter_currency
    if doc.counter_qty:
        doc.quote_qty = doc.counter_qty
    if doc.counter_delivery_date:
        doc.quoted_delivery_date = doc.counter_delivery_date

    doc.status = 'Approved'
    doc.accepted_via = 'counter'
    doc.accepted_at = frappe.utils.now_datetime()
    _clear_counter(doc)

    doc.save(ignore_permissions=True)
    frappe.db.commit()
    _notify_brand(doc, 'request_counter_accepted',
                  f'Your counter-offer on {doc.name} was accepted')
    return _ok(doc)


@frappe.whitelist(allow_guest=True, methods=['POST'])
@auth_required
def delete_request(id=None, **kwargs):
    ''' Parity with the old store; currently unused. Assignee or admin only. '''
    doc = _get_request_or_404(id)
    _claim_or_assert_assignee(doc)
    frappe.delete_doc(DOCTYPE, doc.name, ignore_permissions=True, force=True)
    frappe.db.commit()
    return {'success': True, 'id': id}


# =====================================================================
# permission_query_conditions hook (desk / report) — §4
# =====================================================================

def get_permission_query_conditions(user=None):
    '''
    Row-scoping for brand users in desk / report-builder contexts. The
    whitelisted API enforces the same rule directly in its filters; this is the
    defense-in-depth layer for any non-API access. Internal/System users are
    unrestricted.
    '''
    user = user or frappe.session.user
    if user in ('Administrator', 'Guest'):
        return ''
    brand_id = frappe.db.get_value('Brand User', {'user': user}, 'brand')
    if not brand_id:
        return ''   # internal/PSL — see all
    return f"`tabRequest`.`brand` = {frappe.db.escape(brand_id)}"


# =====================================================================
# Serializer — emits the camelCase shape the old recordFromRow() produced
# =====================================================================

def record_from_doc(doc):
    return {
        'id': doc.name,
        'type': _TYPE_FROM_DB.get(doc.request_type, 'quote'),
        'status': (doc.status or 'Pending').lower(),
        'brand': _brand_obj(doc),
        'source': _source_obj(doc),
        'moodboard': _moodboard_obj(doc),
        'lines': [_row_to_line(l, doc.source_kind) for l in (doc.lines or [])],
        'notes': doc.notes or '',
        'quote': _quote_obj(doc),
        'counter': _counter_obj(doc),
        'assignment': _assignment_obj(doc),
        'createdAt': _iso(doc.creation),
        'updatedAt': _iso(doc.modified),
    }


def _assignment_obj(doc):
    '''
    Single-owner assignment state. `canAct` is evaluated for the CURRENT caller
    so the UI can hide PSL action buttons from everyone but the assignee (an
    unclaimed request is actionable by any internal user; a claimed one only by
    the assignee or a System Manager). Brand callers always get canAct=False —
    their Accept/Counter buttons are gated separately by brand ownership.
    '''
    return {
        'assignedTo': doc.assigned_psl or None,
        'assignedToName': _display_name(doc.assigned_psl) if doc.assigned_psl else None,
        'assignedAt': _iso(doc.assigned_at),
        'canAct': _can_act(doc),
    }


def _brand_obj(doc):
    if not doc.brand:
        return None
    name = frappe.db.get_value('Brand', doc.brand, 'brand') or doc.brand
    return {
        'id': doc.brand,
        'name': name,
        'email': doc.brand_email or '',
        'contactName': doc.brand_contact_name or '',
    }


def _source_obj(doc):
    if not (doc.source_kind or doc.source_id):
        return None
    return {
        'kind': doc.source_kind or None,
        'id': doc.source_id or None,
        'title': doc.source_title or None,
        'thumbnail': doc.source_thumbnail or None,
        'season': doc.source_season or '',
        'moodboard': _moodboard_obj(doc),
    }


def _moodboard_obj(doc):
    '''
    The board reference for the cost/quote engine. Only `id` is consumed by the
    UI; title/thumbnail/season are best-effort from the source fields.
    '''
    mb_id = doc.moodboard or None
    if not mb_id and doc.source_kind == 'moodboard' and doc.source_id:
        mb_id = doc.source_id
    if not mb_id:
        return None
    return {
        'id': mb_id,
        'title': doc.source_title or 'Moodboard',
        'thumbnail': doc.source_thumbnail or None,
        'season': doc.source_season or '',
    }


def _quote_obj(doc):
    has_quote = bool(
        doc.quote_status or doc.quote_submitted_at or doc.review_decision
        or doc.accepted_via or (doc.negotiation_history or [])
    )
    if not has_quote:
        return None

    review = None
    if doc.review_decision:
        review = {
            'decision': doc.review_decision,
            'comment': doc.review_comment or '',
            'at': _iso(doc.review_at),
        }

    return {
        'status': (doc.quote_status or '').lower() or None,
        'price': doc.quote_price,
        'currency': doc.quote_currency or None,
        'qty': doc.quote_qty,
        'comment': doc.quote_comment or '',
        'incoterms': doc.incoterms or '',
        'paymentTerms': doc.payment_terms or '',
        'validForDays': doc.valid_for_days or None,
        'quotedDeliveryDate': _date(doc.quoted_delivery_date),
        'submittedAt': _iso(doc.quote_submitted_at),
        'acceptedVia': doc.accepted_via or None,
        'acceptedAt': _iso(doc.accepted_at),
        'review': review,
        'history': [_row_to_event(e) for e in (doc.negotiation_history or [])],
    }


def _counter_obj(doc):
    if not (doc.counter_at or doc.counter_price or doc.counter_qty or doc.counter_note):
        return None
    return {
        'price': doc.counter_price,
        'currency': doc.counter_currency or None,
        'qty': doc.counter_qty,
        'deliveryDate': _date(doc.counter_delivery_date),
        'note': doc.counter_note or '',
        'at': _iso(doc.counter_at),
    }


def _row_to_event(e):
    return {
        'by': e.actor,
        'kind': e.kind,
        'price': e.price,
        'currency': e.currency or None,
        'qty': e.qty,
        'deliveryDate': e.delivery_date or None,
        'note': e.note or '',
        'at': _iso(e.event_at),
    }


def _row_to_line(l, source_kind=None):
    color = _compact({'tcx': l.color_tcx, 'hex': l.color_hex, 'name': l.color_name})
    # Multi-colour: prefer the stored `colors[]` array; fall back to the single
    # colour so rows written before this field existed still expose `colors`.
    colors = _parse_json(l.colors, None)
    if not isinstance(colors, list) or not colors:
        colors = [color] if color else []
    fabric = _compact({
        'name': l.fabric_name, 'description': l.fabric_description, 'gsm': l.gsm,
    })
    esg = _compact({
        'rating': l.esg_rating, 'category': l.esg_category, 'score': l.esg_score,
    })
    return {
        'styleId': l.style_id or '',
        'styleSku': l.style_sku or '',
        'styleName': l.style_name or '',
        'garmentName': l.garment_name or '',
        'brand': l.style_brand or '',
        'productType': l.product_type or '',
        'thumbnail': _line_thumbnail(l, source_kind),
        'sizes': _parse_json(l.sizes, []),
        'sizeQuantities': _parse_json(l.size_quantities, {}),
        'totalQty': l.total_qty or 0,
        'color': color or None,
        'colors': colors,
        'aiColoredImage': l.ai_colored_image or None,
        'fabric': fabric or None,
        'fabricQuality': l.fabric_quality or '',
        'fabricBlend': l.fabric_blend or '',
        'isBlendChangeRequested': bool(l.is_blend_change_requested),
        'changedBlend': l.changed_blend or '',
        'calculatedEsg': esg or None,
        'gsm': l.gsm or None,
        'customization': _parse_json(l.customization, None),
        'sampleRequested': bool(l.sample_requested),
        'sampleSizeQuantities': _parse_json(l.sample_size_quantities, {}),
        'sampleTotalQty': l.sample_total_qty or 0,
        'sampleType': l.sample_type or None,
        'targetPriceUsd': l.target_price if l.target_price else None,
        'targetPriceCurrency': l.target_price_currency or 'USD',
        'tentativeDeliveryDate': l.tentative_delivery_date or '',
        'brandStyleNumber': l.brand_style_number or '',
        'notes': l.line_notes or '',
    }


# =====================================================================
# Thumbnails — resolved fresh from the source master on read
# =====================================================================
#
# The line's persisted `thumbnail` is a snapshot taken at request-creation time,
# so it may now be dead: an expired S3 presigned URL, or a local /files path whose
# File was deleted by the media-to-S3 migration. Instead of trusting it, we
# re-derive the thumbnail from the durable source doc via `style_id` (keyed on the
# request's `source_kind`), and only fall back to the stored value when the source
# is gone. This self-heals existing broken rows — no backfill needed.

def _line_thumbnail(line, source_kind):
    ''' Fresh thumbnail for a line, resolved from its source master; stored value is last resort. '''
    sid = (line.style_id or '').strip()
    if sid:
        if source_kind == 'moodboard':
            url = _moodboard_style_thumbnail(sid)
            if url:
                return url
        elif source_kind == 'style':
            url = _garment_thumbnail(sid)
            if url:
                return url
    return line.thumbnail or ''


def _moodboard_style_thumbnail(style_id):
    '''
    A Moodboard Style's thumbnail: its per-garment crop `image`, else the first
    row of its garment gallery. Both are stored as public (non-expiring) asset
    URLs, so no signing is needed. None when the style / any image is gone.
    '''
    if not frappe.db.exists('Moodboard Style', style_id):
        return None
    img = (frappe.db.get_value('Moodboard Style', style_id, 'image') or '').strip()
    if not img:
        img = frappe.db.get_value(
            'Moodboard Style Garment Image',
            {'parent': style_id, 'parentfield': 'garment_images'},
            'image', order_by='display_order asc, idx asc',
        ) or ''
    return cloud.asset_url(img) if img else None


def _garment_thumbnail(sample_request):
    '''
    An inventory garment ("Sample Request") thumbnail. Prefer the signed `front`
    of the gsr image blobs in priority 3D -> clean -> base (matching
    garment.details); fall back to the first uploaded garment-gallery row (a public
    asset URL). None when the garment / any image is gone.
    '''
    row = frappe.db.get_value(
        'Sample Request', sample_request,
        ['image_urls_3d', 'image_urls_clean', 'image_urls'], as_dict=True,
    )
    if not row:
        return None
    for raw in (row.image_urls_3d, row.image_urls_clean, row.image_urls):
        url = cloud.garment_front_signed_url(raw)
        if url:
            return url
    gallery = frappe.db.get_value(
        'Sample Request Garment Image',
        {'parent': sample_request, 'parentfield': 'garment_images'},
        'image', order_by='display_order asc, idx asc',
    )
    return cloud.asset_url(gallery) if gallery else None


# =====================================================================
# Mutation helpers
# =====================================================================

def _line_to_row(line):
    line = _as_dict(line)
    colors = _colors_list(line)
    # Keep the flat single-colour columns populated (from the explicit `color`
    # or, failing that, the first multi-colour entry) so existing readers of
    # color_hex/name/tcx keep working.
    color = _as_dict(line.get('color')) or (colors[0] if colors else {})
    fabric = _as_dict(line.get('fabric'))
    # ESG comes nested as `calculatedEsg` {rating, category, score}; flat
    # esgRating/esgCategory/esgScore keys are accepted as a fallback.
    esg = _as_dict(line.get('calculatedEsg'))
    return {
        'style_id': line.get('styleId'),
        'style_sku': line.get('styleSku'),
        'style_name': line.get('styleName'),
        'garment_name': line.get('garmentName'),
        'style_brand': line.get('brand'),
        'product_type': line.get('productType'),
        'thumbnail': line.get('thumbnail'),
        'brand_style_number': line.get('brandStyleNumber'),
        'total_qty': _int(line.get('totalQty')),
        'sample_requested': 1 if line.get('sampleRequested') else 0,
        'sample_total_qty': _int(line.get('sampleTotalQty')),
        'sample_type': line.get('sampleType'),
        'color_hex': color.get('hex'),
        'color_name': color.get('name'),
        'color_tcx': color.get('tcx'),
        'colors': _dump_json(colors) if colors else None,
        'ai_colored_image': _save_image(line.get('aiColoredImage')),
        'fabric_name': fabric.get('name'),
        'fabric_description': fabric.get('description'),
        'fabric_blend': line.get('fabricBlend'),
        'is_blend_change_requested': 1 if line.get('isBlendChangeRequested') else 0,
        'changed_blend': line.get('changedBlend'),
        'fabric_quality': line.get('fabricQuality'),
        'gsm': _int(line.get('gsm')),
        'esg_rating': esg.get('rating') or line.get('esgRating'),
        'esg_category': esg.get('category') or line.get('esgCategory'),
        'esg_score': _num(esg.get('score') or line.get('esgScore')),
        'target_price': _num(line.get('targetPriceUsd')),
        'target_price_currency': line.get('targetPriceCurrency') or 'USD',
        'tentative_delivery_date': line.get('tentativeDeliveryDate') or '',
        'line_notes': line.get('notes') or '',
        'sizes': _dump_json(line.get('sizes')),
        'size_quantities': _dump_json(line.get('sizeQuantities')),
        'sample_size_quantities': _dump_json(line.get('sampleSizeQuantities')),
        'customization': _dump_json(line.get('customization')),
    }


def _colors_list(line):
    '''
    Normalise the multi-colour payload to a list of {hex, name, tcx} dicts.
    Accepts the new `colors[]` array (list or JSON string); falls back to the
    legacy single `color` object so old-shape payloads still populate colours.
    '''
    raw = line.get('colors')
    if isinstance(raw, str):
        raw = _as_list(raw)
    out = []
    if isinstance(raw, list):
        out = [c for c in (_compact(_as_dict(c)) for c in raw) if c]
    if out:
        return out
    single = _compact(_as_dict(line.get('color')))
    return [single] if single else []


def _save_image(value):
    ''' base64 data URL -> uploaded /files path; an existing path/URL is kept. '''
    if not value or not isinstance(value, str):
        return None
    if value.startswith('data:'):
        return util.save_file(value, MAX_IMAGE_FILE_SIZE_MB)
    return value


def _apply_quote_fields(doc, quote):
    ''' Merge the PSL quote payload onto the parent (camelCase -> columns). '''
    if 'price' in quote:
        doc.quote_price = _num(quote.get('price'))
    if 'currency' in quote:
        doc.quote_currency = quote.get('currency')
    if 'qty' in quote:
        doc.quote_qty = _int(quote.get('qty'))
    if 'comment' in quote:
        doc.quote_comment = quote.get('comment')
    if 'incoterms' in quote:
        doc.incoterms = quote.get('incoterms')
    if 'paymentTerms' in quote:
        doc.payment_terms = quote.get('paymentTerms')
    if 'validForDays' in quote:
        doc.valid_for_days = _int(quote.get('validForDays'))
    if 'quotedDeliveryDate' in quote:
        doc.quoted_delivery_date = quote.get('quotedDeliveryDate') or None
    doc.quote_submitted_at = _dt(quote.get('submittedAt')) or frappe.utils.now_datetime()


def _append_event(doc, actor, kind, price=None, currency=None, qty=None,
                  delivery_date=None, note=None):
    doc.append('negotiation_history', {
        'actor': actor,
        'kind': kind,
        'price': _num(price),
        'currency': currency,
        'qty': _int(qty),
        'delivery_date': delivery_date or None,
        'note': note,
        'event_at': frappe.utils.now_datetime(),
    })


def _clear_counter(doc):
    doc.counter_price = None
    doc.counter_currency = None
    doc.counter_qty = None
    doc.counter_delivery_date = None
    doc.counter_note = None
    doc.counter_at = None


# =====================================================================
# Identity / permission helpers
# =====================================================================

def _identity():
    ''' (user_email, brand_dict|None). brand_dict set for brand users only. '''
    return util.get_current_user_id(), util.get_current_brand()


def _require_internal():
    _, brand = _identity()
    if brand:
        _forbidden('Only internal users can perform this action')


def _is_admin():
    ''' System Managers may override assignment (act on / take over any request). '''
    return 'System Manager' in frappe.get_roles(frappe.session.user)


def _psl_members():
    '''
    Every PSL/internal user = enabled Users with NO Brand User mapping
    (Administrator / Guest excluded). The audience notified when a brand creates
    a request, and the pool allowed to claim it.
    '''
    brand_users = set(frappe.get_all('Brand User', pluck='user'))
    skip = {'Administrator', 'Guest'}
    return [
        u for u in frappe.get_all('User', filters={'enabled': 1}, pluck='name')
        if u not in brand_users and u not in skip
    ]


def _claim_or_assert_assignee(doc):
    '''
    Gate for PSL actions + single-owner assignment. The FIRST PSL action claims
    the request (stamps assigned_psl + assigned_at); afterwards only the
    assignee may act — except a System Manager, who can always step in (and in
    doing so takes it over). Brand users are rejected. Caller must doc.save()
    to persist a fresh claim.
    '''
    user, brand = _identity()
    if brand:
        _forbidden('Only internal (PSL) users can perform this action')
    if not doc.assigned_psl:
        doc.assigned_psl = user
        doc.assigned_at = frappe.utils.now_datetime()
        return user
    if doc.assigned_psl != user and not _is_admin():
        _forbidden(f'This request is assigned to {_display_name(doc.assigned_psl)}.')
    return doc.assigned_psl


def _can_act(doc):
    ''' Whether the CURRENT caller may take a PSL action — drives button visibility. '''
    user, brand = _identity()
    if brand:
        return False
    if not doc.assigned_psl:
        return True
    return doc.assigned_psl == user or _is_admin()


def _require_brand_owner(doc):
    ''' Caller must be a brand user owning this request. '''
    _, brand = _identity()
    if not brand:
        _forbidden('Only brand users can perform this action')
    if doc.brand != brand['id']:
        _forbidden('You cannot act on another brand\'s request')


def _assert_can_read(doc):
    _, brand = _identity()
    if brand and doc.brand != brand['id']:
        _forbidden('You cannot read another brand\'s request')


def _get_request_or_404(name):
    if not name or not frappe.db.exists(DOCTYPE, name):
        frappe.throw('Request not found', frappe.DoesNotExistError)
    return frappe.get_doc(DOCTYPE, name)


def _resolve_moodboard_id(source):
    mb = _as_dict(source.get('moodboard'))
    if mb.get('id'):
        return mb['id']
    if source.get('kind') == 'moodboard' and source.get('id'):
        return source['id']
    return None


def _display_name(user_email):
    return frappe.db.get_value('User', user_email, 'full_name') or user_email


# =====================================================================
# Notifications (Frappe -> prism-bot -> prism-web Bell)
# =====================================================================
#
# Three flows:
#   create_request          -> every PSL member ('request_created')
#   PSL acts (quote/approve/ -> the brand contact who owns the request
#   reject/accept-counter)
#   brand acts (counter/     -> the PSL member assigned to the request
#   accept)
#
# All best-effort: a notification failure must never break the request flow.

def _brand_name(doc):
    if not doc.brand:
        return 'A brand'
    return frappe.db.get_value('Brand', doc.brand, 'brand') or doc.brand


def _request_deeplink(doc, for_brand):
    # PSL land on the internal Requests view; brands on My Requests.
    base = '/my-requests' if for_brand else '/requests'
    return f'{base}?id={doc.name}'


def _notify(recipients, event_type, title, doc, for_brand, body=''):
    if not recipients:
        return
    try:
        import prism.api.notifications as notifications
        notifications.notify(
            recipients,
            event_type=event_type,
            title=title,
            body=body,
            deeplink=_request_deeplink(doc, for_brand),
            category='Requests',
            from_user=util.get_current_user_id(),
            ref_doctype=DOCTYPE,
            ref_name=doc.name,
        )
    except Exception:
        frappe.log_error(frappe.get_traceback(), 'request._notify()')


def _notify_psl_new(doc):
    ''' Brand created a request -> ping every PSL member. '''
    kind = _TYPE_FROM_DB.get(doc.request_type, 'quote')
    _notify(
        _psl_members(), 'request_created',
        f'New {kind} request from {_brand_name(doc)}',
        doc, for_brand=False, body=(doc.notes or doc.source_title or ''),
    )


def _notify_brand(doc, event_type, title, body=''):
    ''' PSL acted -> notify the brand contact who owns the request. '''
    if doc.brand_email:
        _notify([doc.brand_email], event_type, title, doc, for_brand=True, body=body)


def _notify_assignee(doc, event_type, title, body=''):
    ''' Brand acted -> notify the PSL member handling the request. '''
    if doc.assigned_psl:
        _notify([doc.assigned_psl], event_type, title, doc, for_brand=False, body=body)


def _price_summary(currency, price):
    if price in (None, '', 0):
        return ''
    return f'{currency or ""} {price}'.strip()


# =====================================================================
# Small utils
# =====================================================================

def _ok(doc):
    return record_from_doc(doc)


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


def _as_list(value):
    if isinstance(value, list):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = frappe.parse_json(value)
            return parsed if isinstance(parsed, list) else []
        except Exception:
            return []
    return []


def _parse_json(value, default):
    if value in (None, ''):
        return default
    try:
        return frappe.parse_json(value)
    except Exception:
        return default


def _dump_json(value):
    if value is None:
        return None
    return frappe.as_json(value)


def _compact(d):
    ''' Drop empty values; return {} if nothing meaningful remains. '''
    return {k: v for k, v in d.items() if v not in (None, '', 0)}


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


def _iso(value):
    if not value:
        return None
    return frappe.utils.get_datetime(value).isoformat()


def _date(value):
    if not value:
        return None
    return str(value)


def _dt(value):
    '''
    Normalise a client ISO datetime (e.g. "2026-06-15T09:28:59.817Z") to the
    naive "YYYY-MM-DD HH:MM:SS[.ffffff]" form MariaDB datetime columns accept.
    The browser sends UTC (`new Date().toISOString()`); we keep the wall-clock
    value and drop the tz marker — frappe stores naive datetimes anyway.
    '''
    if not value:
        return None
    if not isinstance(value, str):
        return value
    s = value.strip().replace('T', ' ')
    if s.endswith('Z'):
        s = s[:-1]
    # Strip a trailing "+HH:MM" / "-HH:MM" timezone offset, if present (guard
    # against the date part's own '-' separators by only scanning the time).
    if ' ' in s:
        date_part, time_part = s.split(' ', 1)
        for sign in ('+', '-'):
            if sign in time_part:
                time_part = time_part.split(sign, 1)[0]
        s = f'{date_part} {time_part}'.strip()
    return s


def _forbidden(message):
    frappe.throw(message, frappe.PermissionError)


def _bad_request(message):
    frappe.throw(message, frappe.ValidationError)
