'''
Prism Notification — unified cross-system notification feed.

Frappe is the SOURCE OF TRUTH: every notification is a `Prism Notification`
row. Producers anywhere in the app call `notify(...)`, which persists one row
per recipient and then relays the batch to prism-bot (the realtime relay).
prism-bot pushes each item over the chat WebSocket to the recipient's live
browser sessions and proxies the web client's read calls back to `mark_read`.

Two channels, one truth:
  - storage + read-state  -> here (Frappe)
  - live delivery         -> prism-bot, best-effort (a relay failure never
                             loses a notification; it surfaces on the next
                             list_notifications fetch)

Auth model:
  - Frappe -> bot /notify : token <bot_api_key>:<bot_api_secret>  (site_config)
  - web -> bot -> Frappe  : the end-user JWT is forwarded as X-Auth-Token, so
                            the whitelisted read/list methods below run under
                            @auth_required exactly like the rest of prism.api.*
                            and a user only ever touches their own rows.

Conventions mirror prism.api.moodboard_comment.* and prism.api.techpack.*.
'''

import hashlib
import hmac
import time

import frappe
import requests

from prism.auth.authenticator import auth_required
import prism.api.util as util

DOCTYPE = 'Prism Notification'

# Final fallback prism-bot base (dev). _bot_base() resolves the real target
# per environment — see that function.
_DEFAULT_BOT_BASE = 'https://prismbotdev.pratibhasyntex.com'

# event_type -> default category. Callers may pass an explicit `category` to
# override; when omitted we resolve here, then fall back to _DEFAULT_CATEGORY.
# Open-ended by design: adding an event_type without an entry is fine — it
# just lands in the default bucket until you map it.
EVENT_CATEGORY = {
    'comment_new':         'Comments',
    'comment_reply':       'Comments',
    'comment_mention':     'Comments',
    'request_created':          'Requests',
    'request_updated':          'Requests',
    'request_quoted':           'Requests',
    'request_countered':        'Requests',
    'request_accepted':         'Requests',
    'request_counter_accepted': 'Requests',
    'request_approved':         'Requests',
    'request_rejected':         'Requests',
    'moodboard_published':      'Moodboards',
}
_DEFAULT_CATEGORY = 'General'

# Body preview length in the stored/relayed payload.
_PREVIEW_LEN = 140

# Fields read back when serializing to the wire contract.
_FETCH_FIELDS = [
    'name', 'recipient', 'from_user', 'category', 'event_type',
    'title', 'body', 'deeplink', 'ref_doctype', 'ref_name',
    'is_read', 'creation',
]


# =====================================================================
# Producer API — call notify(...) from anywhere an event happens
# =====================================================================

def notify(recipients, event_type, title, body='', *, deeplink='',
           category=None, from_user=None, ref_doctype=None, ref_name=None,
           deliver='enqueue'):
    '''
    Persist one Prism Notification per recipient (Frappe = source of truth),
    then relay the batch to prism-bot for live delivery.

    recipients : iterable of user emails. The actor (from_user) is dropped
                 automatically, as are blanks / non-existent users.
    category   : explicit grouping bucket; when None, derived from event_type.
    deliver    : 'enqueue' (default) relays after_commit on a background worker;
                 'sync' relays inline (no worker needed — handy for tests);
                 'none' persists only and leaves relay to the caller.

    Returns the list of created notification docnames. Safe to call from a
    doc-event hook: 'enqueue' fires only once the surrounding transaction
    commits and never blocks the request thread.
    '''
    recipients = _clean_recipients(recipients, exclude=from_user)
    if not recipients:
        return []

    cat = (category or category_for(event_type)).strip()
    body = _preview(body or '')

    created = []
    for recipient in recipients:
        doc = frappe.get_doc({
            'doctype': DOCTYPE,
            'recipient': recipient,
            'from_user': from_user or None,
            'category': cat,
            'event_type': event_type,
            'title': title,
            'body': body,
            'deeplink': deeplink or '',
            'ref_doctype': ref_doctype or '',
            'ref_name': ref_name or '',
            'is_read': 0,
        }).insert(ignore_permissions=True)
        created.append(doc)

    items = [_to_contract(d) for d in created]
    if deliver == 'sync':
        relay_to_bot(items)
    elif deliver == 'enqueue':
        frappe.enqueue(
            'prism.api.notifications.relay_to_bot',
            queue='short',
            enqueue_after_commit=True,
            items=items,
        )
    # deliver == 'none' -> caller is responsible for the relay
    return [d.name for d in created]


def category_for(event_type):
    ''' Resolve the default category bucket for an event_type. '''
    return EVENT_CATEGORY.get(event_type, _DEFAULT_CATEGORY)


def relay_to_bot(items):
    '''
    POST a batch of notification items to prism-bot's /notify ingress for live
    fan-out. Best-effort — failures are logged, never raised (the rows are
    already persisted; the client backfills via list). Returns a small status
    dict (handy for the test endpoint; ignored on the background-job path).
    '''
    if not items:
        return {'relayed': False, 'reason': 'no items'}
    base = _bot_base()
    headers = {
        'Content-Type': 'application/json',
        'Accept': 'application/json',
        'X-Notify-Token': _notify_token(),
    }
    try:
        res = requests.post(
            f'{base}/notify', json={'items': items}, headers=headers, timeout=15,
        )
        res.raise_for_status()
        return {
            'relayed': True,
            'bot': base,
            'status_code': res.status_code,
            'response': res.json() if res.content else None,
        }
    except Exception as exc:
        frappe.log_error(frappe.get_traceback(), 'notifications.relay_to_bot()')
        return {'relayed': False, 'bot': base, 'error': str(exc)}


# =====================================================================
# Test / dev — fire a notification end-to-end with one HTTP call
# =====================================================================

@frappe.whitelist()
def send_test_notification(recipient=None, title=None, body=None,
                           category=None, event_type=None, deeplink=None):
    '''
    Dev/test: create a notification and relay it to prism-bot immediately (no
    background worker required), so you can watch it land in prism-web's Bell.

    Authenticate with Frappe token auth (Administrator's api_key:api_secret).
    The `recipient` should be a real prism-web user WITH AN OPEN SESSION (their
    browser must hold a live socket) to see the live push — otherwise it is
    still stored and shows up on their next page load.

        recipient   default: the caller (the token's user)
        category    default: derived from event_type, else 'General'
        event_type  default: 'test_event'

    Returns the created docname(s) and the bot's relay response (including how
    many live sockets it delivered to).
    '''
    recipient = recipient or frappe.session.user
    if not recipient or recipient == 'Guest':
        frappe.throw('A recipient is required.', frappe.ValidationError)
    if not frappe.db.exists('User', recipient):
        frappe.throw(f'User {recipient!r} does not exist.', frappe.ValidationError)

    ids = notify(
        [recipient],
        event_type=event_type or 'test_event',
        title=title or 'Test notification',
        body=body or 'Frappe → prism-bot → prism-web test ping.',
        deeplink=deeplink or '/communications/chats',
        category=category,
        deliver='none',          # persist now; we relay below and report it
    )
    frappe.db.commit()

    if not ids:
        return {'success': False,
                'error': f'No notification created for {recipient!r}.'}

    docs = [frappe.get_doc(DOCTYPE, n) for n in ids]
    report = relay_to_bot([_to_contract(d) for d in docs])
    return {
        'success': True,
        'recipient': recipient,
        'created': ids,
        'relay': report,
    }


# =====================================================================
# Whitelisted read API — called by prism-bot with the forwarded user JWT
# (X-Auth-Token), so @auth_required identifies the real end user.
# =====================================================================

@frappe.whitelist(allow_guest=True)
@auth_required
def list_notifications(unread_only=0, category=None, page=1, page_size=30):
    '''
    The caller's notification feed, newest first. Backlog source for the Bell
    on load (covers anything missed while the socket was disconnected).
    '''
    user = util.get_current_user_id()
    page = max(1, int(page or 1))
    page_size = max(1, min(100, int(page_size or 30)))

    filters = {'recipient': user}
    if _truthy(unread_only):
        filters['is_read'] = 0
    if category:
        filters['category'] = category

    rows = frappe.get_all(
        DOCTYPE,
        filters=filters,
        fields=_FETCH_FIELDS,
        order_by='creation desc',
        limit_start=(page - 1) * page_size,
        limit_page_length=page_size,
    )
    total = frappe.db.count(DOCTYPE, filters)
    unread_total = frappe.db.count(DOCTYPE, {'recipient': user, 'is_read': 0})

    return {
        'items': [_row_to_contract(r) for r in rows],
        'page': page,
        'page_size': page_size,
        'total': total,
        'unread_total': unread_total,
    }


@frappe.whitelist(allow_guest=True)
@auth_required
def unread_count():
    ''' Lightweight badge count for the Bell. '''
    user = util.get_current_user_id()
    return {'unread_total': frappe.db.count(DOCTYPE, {'recipient': user, 'is_read': 0})}


@frappe.whitelist(allow_guest=True)
@auth_required
def mark_read(notification_id):
    '''
    Mark a single notification read. Ownership is enforced: the row's recipient
    must be the caller, else 403 — even though prism-bot relays the call, the
    forwarded JWT pins identity to the real user.
    '''
    user = util.get_current_user_id()
    doc = _get_owned_or_403(notification_id, user)
    if not doc.is_read:
        doc.is_read = 1
        doc.read_at = frappe.utils.now_datetime()
        doc.save(ignore_permissions=True)
        frappe.db.commit()
    return {'success': True, 'id': notification_id}


@frappe.whitelist(allow_guest=True)
@auth_required
def mark_all_read(category=None):
    ''' Mark every unread notification read for the caller (optionally scoped). '''
    user = util.get_current_user_id()
    filters = {'recipient': user, 'is_read': 0}
    if category:
        filters['category'] = category

    names = frappe.get_all(DOCTYPE, filters=filters, pluck='name')
    now = frappe.utils.now_datetime()
    for name in names:
        frappe.db.set_value(DOCTYPE, name, {'is_read': 1, 'read_at': now},
                            update_modified=False)
    if names:
        frappe.db.commit()
    return {'success': True, 'marked_read': len(names)}


# =====================================================================
# Helpers
# =====================================================================

def _get_owned_or_403(name, user):
    if not name or not frappe.db.exists(DOCTYPE, name):
        frappe.throw('Notification not found', frappe.DoesNotExistError)
    doc = frappe.get_doc(DOCTYPE, name)
    if doc.recipient != user:
        frappe.throw('You cannot modify this notification', frappe.PermissionError)
    return doc


def _clean_recipients(recipients, exclude=None):
    ''' De-dup, drop the actor / blanks, keep only real existing users. '''
    out = set()
    for r in (recipients or []):
        if not r or r == exclude:
            continue
        out.add(r)
    return {r for r in out if frappe.db.exists('User', r)}


def _bot_base():
    ''' prism-bot base URL from site_config 'bot_api_basepath' (per environment). '''
    return (frappe.get_site_config().get('bot_api_basepath') or _DEFAULT_BOT_BASE).rstrip('/')


# ── Time-based ingress token (TOTP-style) ────────────────────────────────
# The bot recomputes this exact value to authenticate /notify. Security comes
# from the SHARED SECRET, not the clock: UTC time is public, so a hash WITHOUT a
# secret is reproducible by anyone. Keep the secret identical on both sides —
# here via site_config 'notify_hash_secret', on the bot via env
# NOTIFY_HASH_SECRET. The token rotates every NOTIFY_HASH_WINDOW seconds, so a
# captured value is useless within ~a minute.
NOTIFY_HASH_WINDOW = 30  # seconds per step; MUST match the bot


def _notify_secret():
    return (frappe.get_site_config().get('notify_hash_secret')
            or 'VmaLHuCsPxdxYEDAO6Myydd8EkJFRsPzM5LOdQaKji-sxJIXZmqbenzte5UzkAtF')


def _notify_token(step=None):
    '''HMAC-SHA256(secret, current-UTC-step) as hex. time.time() is UTC epoch.'''
    if step is None:
        step = int(time.time()) // NOTIFY_HASH_WINDOW
    return hmac.new(
        _notify_secret().encode(), str(step).encode(), hashlib.sha256,
    ).hexdigest()


def _to_contract(doc):
    return _row_to_contract({f: doc.get(f) for f in _FETCH_FIELDS})


def _row_to_contract(row):
    ref_dt = row.get('ref_doctype')
    ref_nm = row.get('ref_name')
    return {
        'id': row.get('name'),
        'recipient': row.get('recipient'),
        'from_user': row.get('from_user') or None,
        'category': row.get('category'),
        'event_type': row.get('event_type'),
        'title': row.get('title'),
        'body': row.get('body') or '',
        'deeplink': row.get('deeplink') or '',
        'entity': ({'doctype': ref_dt, 'name': ref_nm} if (ref_dt and ref_nm) else None),
        'created_at': _iso(row.get('creation')),
        'read': bool(row.get('is_read')),
    }


def _iso(value):
    if not value:
        return None
    return frappe.utils.get_datetime(value).isoformat()


def _preview(text):
    text = (text or '').strip()
    return (text[:_PREVIEW_LEN] + '…') if len(text) > _PREVIEW_LEN else text


def _truthy(value):
    return value in (True, 1, '1', 'true', 'True')
