'''
Moodboard comments — dedicated DocType backend.

Replaces the old draft_json `moodboard_comments[]` / `brand_comments[]` blobs
with a single `Moodboard Comment` DocType that holds both streams (internal +
brand) and replies (via `parent_comment`).

Brand-vs-internal visibility is enforced **server-side** here — never trust the
client for brand identity, author, or stream. Identity is resolved from the
JWT session (set by prism.auth.authenticator.set_user_from_jwt_header /
@auth_required), exactly like the rest of prism.api.*.

Conventions mirror prism.api.moodboard.*.
'''

import frappe

from prism.auth.authenticator import auth_required
import prism.api.util as util

DOCTYPE = 'Moodboard Comment'

# Fields fetched for serialization (Frappe `name` -> client `id`).
_FETCH_FIELDS = [
    'name', 'moodboard', 'stream', 'block_id', 'brand_id', 'brand_name',
    'author', 'author_name', 'author_type', 'text', 'resolved',
    'parent_comment', 'creation', 'modified',
]

# Length of the `preview` snippet in notification/realtime payloads.
_PREVIEW_LEN = 140

# TODO: once a dedicated "PSL / moderator" role exists, list it here so internal
# team members other than the moodboard owner can resolve/moderate. Empty for
# now => moderation is limited to the moodboard owner (see _is_moderator).
_PSL_ROLES = []


# =====================================================================
# Whitelisted endpoints
# =====================================================================

@frappe.whitelist(allow_guest=True)
@auth_required
def list_comments(moodboard: str):
    '''
    Returns visible top-level comments for a moodboard with their replies
    nested and sorted ascending by created_at.

    Internal users get both streams; brand users get ONLY their own brand's
    brand-stream threads (filtered in the query, never after).
    '''
    try:
        if not moodboard:
            _bad_request('moodboard is required')

        user, brand = _identity()
        is_brand = bool(brand)
        my_brand_id = brand['id'] if brand else None

        # --- top-level comments (visibility enforced in the filter) ---
        internal_rows = []
        brand_rows = []

        if is_brand:
            brand_rows = frappe.get_all(
                DOCTYPE,
                filters={
                    'moodboard': moodboard,
                    'stream': 'brand',
                    'brand_id': my_brand_id,
                    'parent_comment': ['in', ('', None)],
                },
                fields=_FETCH_FIELDS,
                order_by='creation asc',
            )
        else:
            top = frappe.get_all(
                DOCTYPE,
                filters={
                    'moodboard': moodboard,
                    'parent_comment': ['in', ('', None)],
                },
                fields=_FETCH_FIELDS,
                order_by='creation asc',
            )
            internal_rows = [r for r in top if r.get('stream') == 'internal']
            brand_rows = [r for r in top if r.get('stream') == 'brand']

        top_rows = internal_rows + brand_rows
        replies_by_parent = _fetch_replies([r['name'] for r in top_rows])

        internal = [_with_replies(r, replies_by_parent) for r in internal_rows]
        brand_list = [_with_replies(r, replies_by_parent) for r in brand_rows]

        open_count = sum(
            1 for r in top_rows if not r.get('resolved')
        )

        return {
            'internal': internal,
            'brand': brand_list,
            'open_count': open_count,
        }

    except frappe.PermissionError:
        raise
    except Exception as ex:
        frappe.log_error(frappe.get_traceback(), 'moodboard_comment.list_comments()')
        raise


@frappe.whitelist(allow_guest=True)
@auth_required
def add_comment(moodboard: str, stream: str, text: str, block_id: str = None):
    '''
    Create a top-level comment. `stream` must be allowed for the caller (§2).
    Brand fields and author are stamped from the session — any client-supplied
    brand_id / author is ignored.
    '''
    user, brand = _identity()
    is_brand = bool(brand)

    text = (text or '').strip()
    if not text:
        _bad_request('Comment text is required')

    if not moodboard or not frappe.db.exists('Moodboard', moodboard):
        _bad_request('Moodboard does not exist')

    if stream == 'internal':
        if is_brand:
            _forbidden('Brand users cannot add internal comments')
        comment_brand_id = ''
        comment_brand_name = ''
        comment_block_id = block_id
    elif stream == 'brand':
        if not is_brand:
            _forbidden('Internal users cannot add brand comments')
        comment_brand_id = brand['id']
        comment_brand_name = brand.get('brand') or ''
        comment_block_id = None  # brand comments are never block-anchored
    else:
        _bad_request("stream must be 'internal' or 'brand'")

    doc = frappe.new_doc(DOCTYPE)
    doc.moodboard = moodboard
    doc.stream = stream
    doc.block_id = comment_block_id
    doc.brand_id = comment_brand_id
    doc.brand_name = comment_brand_name
    doc.author = user
    doc.author_name = _display_name(user)
    doc.author_type = 'brand' if is_brand else 'internal'
    doc.text = text
    doc.resolved = 0
    doc.insert(ignore_permissions=True)
    frappe.db.commit()

    return _serialize(doc)


@frappe.whitelist(allow_guest=True)
@auth_required
def add_reply(parent_comment: str, text: str):
    '''
    Reply to a thread. stream / brand_id / moodboard are inherited from the
    parent — never trusted from the body. Brand users may only reply on their
    own brand's thread, and only while the parent is unresolved.
    '''
    user, brand = _identity()
    is_brand = bool(brand)

    text = (text or '').strip()
    if not text:
        _bad_request('Reply text is required')

    parent = _get_comment_or_404(parent_comment)
    if parent.get('parent_comment'):
        _bad_request('Cannot reply to a reply')

    # visibility / write rules
    if is_brand:
        my_brand_id = brand['id']
        if parent['stream'] != 'brand' or parent.get('brand_id') != my_brand_id:
            _forbidden('You cannot reply on this thread')
        if parent.get('resolved'):
            _forbidden('Cannot reply to a resolved comment')
    # internal users may reply on any thread they can see (all)

    doc = frappe.new_doc(DOCTYPE)
    doc.moodboard = parent['moodboard']
    doc.stream = parent['stream']
    doc.brand_id = parent.get('brand_id') or ''
    doc.brand_name = parent.get('brand_name') or ''
    doc.parent_comment = parent['name']
    doc.author = user
    doc.author_name = _display_name(user)
    doc.author_type = 'brand' if is_brand else 'internal'
    doc.text = text
    doc.resolved = 0
    doc.insert(ignore_permissions=True)
    frappe.db.commit()

    return _serialize(doc)


@frappe.whitelist(allow_guest=True)
@auth_required
def update_comment(id: str, text: str):
    ''' Edit a comment. Author, or a moodboard moderator (internal owner/PSL). '''
    user, brand = _identity()

    text = (text or '').strip()
    if not text:
        _bad_request('Comment text is required')

    comment = _get_comment_or_404(id)
    if not _can_modify(user, brand, comment):
        _forbidden('You are not allowed to edit this comment')

    frappe.db.set_value(DOCTYPE, id, 'text', text)
    frappe.db.commit()

    doc = frappe.get_doc(DOCTYPE, id)
    _publish_event('moodboard_comment_updated', doc, exclude=user)
    return _serialize(doc)


@frappe.whitelist(allow_guest=True)
@auth_required
def delete_comment(id: str):
    '''
    Delete a comment. Author, or a moodboard moderator (internal owner/PSL).
    Deleting a top-level comment cascades to its replies.
    '''
    user, brand = _identity()

    comment = _get_comment_or_404(id)
    if not _can_modify(user, brand, comment):
        _forbidden('You are not allowed to delete this comment')

    # Build the realtime payload + audience BEFORE the rows disappear.
    doc = frappe.get_doc(DOCTYPE, id)
    audience = _audience_for(doc, exclude=user)
    payload = _event_payload('moodboard_comment_deleted', doc)

    if not comment.get('parent_comment'):
        # cascade replies
        for reply in frappe.get_all(DOCTYPE, filters={'parent_comment': id}, pluck='name'):
            frappe.delete_doc(DOCTYPE, reply, ignore_permissions=True, force=True)

    frappe.delete_doc(DOCTYPE, id, ignore_permissions=True, force=True)
    frappe.db.commit()

    _publish(payload['event'], payload, audience)
    return {'success': True, 'id': id}


@frappe.whitelist(allow_guest=True)
@auth_required
def resolve_comment(id: str, resolved=True):
    ''' Resolve / unresolve a top-level comment. Moderators only; brands rejected. '''
    user, brand = _identity()
    if brand:
        _forbidden('Brand users cannot resolve comments')

    comment = _get_comment_or_404(id)
    if comment.get('parent_comment'):
        _bad_request('Only top-level comments can be resolved')
    if not _is_moderator(user, comment['moodboard']):
        _forbidden('Only the moodboard owner can resolve comments')

    frappe.db.set_value(DOCTYPE, id, 'resolved', 1 if _truthy(resolved) else 0)
    frappe.db.commit()

    doc = frappe.get_doc(DOCTYPE, id)
    _publish_event('moodboard_comment_resolved', doc, exclude=user)
    return _serialize(doc)


# =====================================================================
# PSL dashboard — Communication ▸ Comments tab
# =====================================================================
#
# A chat-style inbox for internal/PSL users: the left rail lists every
# moodboard the caller created that carries comments, each with an unread
# badge (§ _unread_counts_by_moodboard); selecting one opens its full threads
# in the right pane (get_moodboard_thread) and clears that board's badge.
#
# Reply / resolve / edit / delete on a thread reuse the existing endpoints
# (add_reply, resolve_comment, update_comment, delete_comment) — nothing new is
# needed there. Brand users are rejected throughout: this is the PSL surface.

@frappe.whitelist(allow_guest=True)
@auth_required
def list_comment_moodboards():
    '''
    Left rail: every moodboard the caller owns that has at least one comment,
    with its unread count, open-thread count and last activity. Sorted unread
    first, then most-recent activity. Brand users are rejected.
    '''
    user, brand = _identity()
    if brand:
        _forbidden('The Comments dashboard is for internal users only')

    owned = _owned_moodboards(user)
    if not owned:
        return {'moodboards': [], 'total_unread': 0}

    rows = frappe.get_all(
        DOCTYPE,
        filters={'moodboard': ['in', owned]},
        fields=['moodboard', 'text', 'creation', 'resolved', 'parent_comment'],
        order_by='creation asc',
    )
    if not rows:
        return {'moodboards': [], 'total_unread': 0}

    unread_by_board = _unread_counts_by_moodboard(user)

    agg = {}
    for r in rows:
        b = agg.setdefault(r['moodboard'], {'total': 0, 'open_threads': 0,
                                            'last_at': None, 'last_preview': ''})
        b['total'] += 1
        if not r.get('parent_comment') and not r.get('resolved'):
            b['open_threads'] += 1
        # rows are creation-ascending, so the last seen is the most recent
        b['last_at'] = r['creation']
        b['last_preview'] = _preview(r.get('text') or '')

    meta = _moodboard_meta(list(agg.keys()))

    out = []
    for mb_id, a in agg.items():
        m = meta.get(mb_id, {})
        unread = unread_by_board.get(mb_id) or {}
        out.append({
            'id': mb_id,
            'title': m.get('title') or mb_id,
            'thumbnail': m.get('thumbnail') or None,
            'unread_count': unread.get('total', 0),
            'brand_unread_count': unread.get('brand', 0),
            'internal_unread_count': unread.get('internal', 0),
            'open_threads': a['open_threads'],
            'total_comments': a['total'],
            'last_comment_preview': a['last_preview'],
            'last_activity_at': _iso(a['last_at']),
        })

    # unread boards first, then most recent activity first
    out.sort(key=lambda x: (x['unread_count'] > 0, x['last_activity_at'] or ''),
             reverse=True)

    return {
        'moodboards': out,
        'total_unread': sum(o['unread_count'] for o in out),
    }


@frappe.whitelist(allow_guest=True)
@auth_required
def get_moodboard_thread(moodboard: str, mark_read=True):
    '''
    Right pane: all top-level comments for one owned board (both streams) with
    their replies nested, ascending by created_at. Opening the thread marks the
    caller's unread notifications for this board read — pass mark_read=0 to peek
    without clearing the badge. Owner-only (this is the PSL dashboard).
    '''
    user, brand = _identity()
    if brand:
        _forbidden('The Comments dashboard is for internal users only')
    if not moodboard:
        _bad_request('moodboard is required')
    if not _is_moderator(user, moodboard):
        _forbidden('You do not own this moodboard')

    top = frappe.get_all(
        DOCTYPE,
        filters={'moodboard': moodboard, 'parent_comment': ['in', ('', None)]},
        fields=_FETCH_FIELDS,
        order_by='creation asc',
    )
    replies_by_parent = _fetch_replies([r['name'] for r in top])
    threads = [_with_replies(r, replies_by_parent) for r in top]

    internal = [t for t in threads if t['stream'] == 'internal']
    brand_list = [t for t in threads if t['stream'] == 'brand']
    open_count = sum(1 for r in top if not r.get('resolved'))

    if _truthy(mark_read):
        _mark_read_for_moodboard(user, moodboard)

    return {
        'moodboard': moodboard,
        'internal': internal,
        'brand': brand_list,
        'open_count': open_count,
    }


@frappe.whitelist(allow_guest=True)
@auth_required
def mark_moodboard_read(moodboard: str):
    ''' Clear the caller's unread comment notifications for one owned board. '''
    user, brand = _identity()
    if brand:
        _forbidden('The Comments dashboard is for internal users only')
    if not moodboard:
        _bad_request('moodboard is required')
    if not _is_moderator(user, moodboard):
        _forbidden('You do not own this moodboard')

    marked = _mark_read_for_moodboard(user, moodboard)
    return {'success': True, 'marked_read': marked}


# =====================================================================
# Identity / permission helpers
# =====================================================================

def _identity():
    '''
    Returns (user_email, brand) for the JWT session.
    brand is the dict {id, brand, category} for brand users, else None.
    Internal/PSL users => brand is None.
    '''
    user = util.get_current_user_id()
    brand = util.get_current_brand()
    return user, brand


def _get_moodboard_owner(moodboard_id: str):
    '''
    The moodboard owner used for moderation rights and internal notifications.

    TODO: a dedicated collaborator-owner field is being added to the Moodboard
    DocType. When it lands, repoint this single line at that field
    (e.g. 'collab_owner') — everything else keys off this helper.
    '''
    return frappe.db.get_value('Moodboard', moodboard_id, 'owner')


def _owned_moodboards(user: str):
    '''
    Moodboard ids the user created — the scope of the PSL Comments dashboard.
    Single keying point for ownership; repoint here (and _get_moodboard_owner)
    when the collab_owner field lands.
    '''
    if not user:
        return []
    return frappe.get_all('Moodboard', filters={'owner': user}, pluck='name')


def _moodboard_meta(names):
    ''' {moodboard_id: {title, thumbnail}} for the left-rail rows. '''
    if not names:
        return {}
    rows = frappe.get_all(
        'Moodboard',
        filters={'name': ['in', names]},
        fields=['name', 'moodboard_title', 'thumbnail'],
    )
    return {
        r['name']: {'title': r.get('moodboard_title'), 'thumbnail': r.get('thumbnail')}
        for r in rows
    }


def _is_moderator(user: str, moodboard_id: str):
    ''' Internal (non-brand) moodboard owner, or a configured PSL role holder. '''
    if user and user == _get_moodboard_owner(moodboard_id):
        return True
    if _PSL_ROLES and util.user_has_roles(_PSL_ROLES):
        return True
    return False


def _can_modify(user: str, brand, comment: dict):
    ''' Edit/delete rule: own comment, or (internal) moderator of the moodboard. '''
    if user and user == comment.get('author'):
        return True
    if brand:
        return False  # brand users: own comment only
    return _is_moderator(user, comment['moodboard'])


# =====================================================================
# Fetch / serialization helpers
# =====================================================================

def _get_comment_or_404(name: str):
    if not name or not frappe.db.exists(DOCTYPE, name):
        frappe.throw('Comment not found', frappe.DoesNotExistError)
    return frappe.db.get_value(DOCTYPE, name, _FETCH_FIELDS, as_dict=True)


def _fetch_replies(parent_names):
    ''' Returns {parent_name: [reply_row, ...]} sorted ascending by creation. '''
    if not parent_names:
        return {}
    rows = frappe.get_all(
        DOCTYPE,
        filters={'parent_comment': ['in', parent_names]},
        fields=_FETCH_FIELDS,
        order_by='creation asc',
    )
    grouped = {}
    for r in rows:
        grouped.setdefault(r['parent_comment'], []).append(_row_to_obj(r))
    return grouped


def _with_replies(row, replies_by_parent):
    obj = _row_to_obj(row)
    obj['replies'] = replies_by_parent.get(row['name'], [])
    return obj


def _serialize(doc):
    ''' Serialize a freshly mutated Document (mutation responses return one row). '''
    return _row_to_obj({f: doc.get(f) for f in _FETCH_FIELDS})


def _row_to_obj(row):
    return {
        'id': row.get('name'),
        'moodboard': row.get('moodboard'),
        'stream': row.get('stream'),
        'block_id': row.get('block_id') or None,
        'brand_id': row.get('brand_id') or None,
        'brand_name': row.get('brand_name') or None,
        'author': row.get('author'),
        'author_name': row.get('author_name'),
        'author_type': row.get('author_type'),
        'text': row.get('text'),
        'resolved': bool(row.get('resolved')),
        'parent_comment': row.get('parent_comment') or None,
        'created_at': _iso(row.get('creation')),
        'updated_at': _iso(row.get('modified')),
    }


def _iso(value):
    if not value:
        return None
    return frappe.utils.get_datetime(value).isoformat()


def _display_name(user_email: str):
    return frappe.db.get_value('User', user_email, 'full_name') or user_email


def _truthy(value):
    return value in (True, 1, '1', 'true', 'True')


# =====================================================================
# Notifications + realtime (§5, §6)
# =====================================================================

def dispatch_new_comment(doc):
    '''
    Called from MoodboardComment.after_insert for both top-level comments and
    replies. Builds the recipient set (§5.1), drops the actor, then fans out to
    the Notification Log (the always-on v1 channel) and realtime (§6).
    '''
    actor = doc.author
    recipients = _recipients_for_new(doc, actor)
    if not recipients:
        return

    payload = _event_payload('moodboard_comment_new', doc)
    subject = _subject(doc)

    for recipient in recipients:
        _create_notification_log(recipient, actor, doc, subject)

    _publish('moodboard_comment_new', payload, recipients)

    # Cross-system Bell feed (Frappe -> prism-bot -> prism-web). Additive and
    # best-effort: separate from the Notification Log above, which still powers
    # the PSL Comments-dashboard unread badges.
    _push_prism_notification(doc, actor, recipients, subject)


def _recipients_for_new(doc, actor):
    recipients = set()

    owner = _get_moodboard_owner(doc.moodboard)
    if owner:
        recipients.add(owner)

    is_reply = bool(doc.parent_comment)
    if is_reply:
        # all prior participants of the thread (parent author + earlier repliers)
        recipients |= _thread_participants(doc.parent_comment)

    if doc.stream == 'internal':
        # other internal users who have commented on this board
        recipients |= _internal_commenters(doc.moodboard)
    elif doc.stream == 'brand':
        if doc.author_type == 'internal':
            # PSL replied on a brand thread -> notify that brand's users
            recipients |= _brand_users(doc.brand_id)
        # brand authored -> the internal owner/PSL (owner already added above)

    recipients.discard(actor)
    recipients.discard(None)
    recipients.discard('')
    # only real, existing users
    return {r for r in recipients if frappe.db.exists('User', r)}


def _audience_for(doc, exclude=None):
    ''' Audience for update/delete/resolve events: thread participants + owner. '''
    recipients = set()
    owner = _get_moodboard_owner(doc.moodboard)
    if owner:
        recipients.add(owner)
    root = doc.parent_comment or doc.name
    recipients |= _thread_participants(root)
    recipients.discard(exclude)
    recipients.discard(None)
    recipients.discard('')
    return {r for r in recipients if frappe.db.exists('User', r)}


def _thread_participants(root_name):
    ''' Author of the root comment + authors of all its replies. '''
    authors = set(frappe.get_all(DOCTYPE, filters={'name': root_name}, pluck='author'))
    authors |= set(frappe.get_all(DOCTYPE, filters={'parent_comment': root_name}, pluck='author'))
    return authors


def _internal_commenters(moodboard):
    return set(frappe.get_all(
        DOCTYPE,
        filters={'moodboard': moodboard, 'stream': 'internal'},
        pluck='author',
    ))


def _brand_users(brand_id):
    if not brand_id:
        return set()
    return set(frappe.get_all('Brand User', filters={'brand': brand_id}, pluck='user'))


def _unread_notifications(user):
    '''
    The user's unread comment notifications. The owner is always a recipient of
    new comments/replies on their board (see _recipients_for_new), so these rows
    are the source of truth for the dashboard's unread badges.
    '''
    return frappe.get_all(
        'Notification Log',
        filters={'for_user': user, 'document_type': DOCTYPE, 'read': 0},
        fields=['name', 'document_name'],
    )


def _unread_counts_by_moodboard(user):
    '''
    {moodboard_id: {'total': n, 'brand': n, 'internal': n}}, resolved from the
    user's unread logs. `total` is the overall unread count; `brand`/`internal`
    split it by the originating comment's stream.
    '''
    logs = _unread_notifications(user)
    comment_ids = [l['document_name'] for l in logs if l.get('document_name')]
    if not comment_ids:
        return {}
    # comment -> (moodboard, stream); logs for deleted comments resolve to
    # nothing and are skipped (delete_comment removes the row but leaves the
    # Notification Log).
    comment_meta = {
        r['name']: r for r in frappe.get_all(
            DOCTYPE, filters={'name': ['in', comment_ids]},
            fields=['name', 'moodboard', 'stream'],
        )
    }
    counts = {}
    for cid in comment_ids:
        meta = comment_meta.get(cid)
        if not meta:
            continue
        mb = meta['moodboard']
        b = counts.setdefault(mb, {'total': 0, 'brand': 0, 'internal': 0})
        b['total'] += 1
        if meta.get('stream') in ('brand', 'internal'):
            b[meta['stream']] += 1
    return counts


def _mark_read_for_moodboard(user, moodboard):
    ''' Mark read every unread comment notification for the user on this board. '''
    logs = _unread_notifications(user)
    comment_ids = [l['document_name'] for l in logs if l.get('document_name')]
    if not comment_ids:
        return 0
    board_comments = set(frappe.get_all(
        DOCTYPE,
        filters={'name': ['in', comment_ids], 'moodboard': moodboard},
        pluck='name',
    ))
    to_mark = [l['name'] for l in logs if l.get('document_name') in board_comments]
    for log_name in to_mark:
        frappe.db.set_value('Notification Log', log_name, 'read', 1)
    if to_mark:
        frappe.db.commit()
    return len(to_mark)


def _create_notification_log(recipient, actor, doc, subject):
    frappe.get_doc({
        'doctype': 'Notification Log',
        'for_user': recipient,
        'from_user': actor,
        'subject': subject,
        'type': 'Alert',
        'document_type': DOCTYPE,
        'document_name': doc.name,
    }).insert(ignore_permissions=True)


def _publish_event(event, doc, exclude=None):
    audience = _audience_for(doc, exclude=exclude)
    if audience:
        _publish(event, _event_payload(event, doc), audience)


def _publish(event, payload, users):
    for user in users:
        frappe.publish_realtime(event, message=payload, user=user, after_commit=True)


def _event_payload(event, doc):
    text = doc.text or ''
    return {
        'event': event,
        'moodboard': doc.moodboard,
        'comment_id': doc.name,
        'parent_comment': doc.parent_comment or None,
        'stream': doc.stream,
        'brand_id': doc.brand_id or None,
        'author_name': doc.author_name,
        'preview': _preview(text),
        'created_at': _iso(doc.creation),
    }


def _subject(doc):
    return f'{doc.author_name or doc.author} commented: {_preview(doc.text or "")}'


def _push_prism_notification(doc, actor, recipients, subject):
    '''
    Mirror a new comment/reply into the cross-system Prism Notification feed
    (the prism-web Bell, delivered live via prism-bot). Best-effort: a failure
    here must never affect the comment insert or the Notification Log fan-out.
    '''
    try:
        import prism.api.notifications as notifications

        is_reply = bool(doc.parent_comment)
        who = doc.author_name or doc.author
        title = f'{who} replied to a comment' if is_reply else f'{who} added a comment'
        event_type = 'comment_reply' if is_reply else 'comment_new'

        # Recipient-aware deep link. Brand users have no PSL Comments dashboard:
        # they open the moodboard in the showcase with the comments drawer
        # auto-opened (?comments=1). PSL/internal users land on the
        # Communications ▸ Comments dashboard as before.
        brand_link = (f'/moodboards-showcase/view?id={doc.moodboard}'
                      f'&comments=1&comment={doc.name}')
        psl_link = f'/communications/comments?board={doc.moodboard}'

        brand_users, psl_users = [], []
        for r in recipients:
            target = brand_users if frappe.db.exists('Brand User', {'user': r}) else psl_users
            target.append(r)

        common = dict(
            event_type=event_type, title=title, body=doc.text or '',
            category='Comments', from_user=actor,
            ref_doctype=DOCTYPE, ref_name=doc.name,
        )
        if psl_users:
            notifications.notify(psl_users, deeplink=psl_link, **common)
        if brand_users:
            notifications.notify(brand_users, deeplink=brand_link, **common)
    except Exception:
        frappe.log_error(frappe.get_traceback(),
                         'moodboard_comment._push_prism_notification()')


def _preview(text):
    text = text.strip()
    return (text[:_PREVIEW_LEN] + '…') if len(text) > _PREVIEW_LEN else text


# =====================================================================
# Error helpers (map to the §3.8 contract)
# =====================================================================

def _forbidden(message):
    frappe.throw(message, frappe.PermissionError)  # -> HTTP 403


def _bad_request(message):
    frappe.throw(message, frappe.ValidationError)  # -> HTTP 417
