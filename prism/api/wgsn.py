'''
WGSN — read endpoints over the WGSN trend-report doctype.

Pagination mirrors prism.api.moodboard_v2.list_moodboards: limit/offset inputs
and the { total, limit, offset, items } envelope, where total counts ALL
matching reports (ignoring the page window) so the client can page.
'''

import frappe
from frappe.query_builder.functions import Count

from prism.auth.authenticator import auth_required

DOCTYPE = 'WGSN'
TREND_ITEM_DOCTYPE = 'WGSN Trend Item'

# Pagination / search tuning (same values as moodboard_v2).
_DEFAULT_LIMIT = 20
_MAX_LIMIT = 100
_SEARCH_MIN_CHARS = 3

# Exact-match filter keys accepted in `filters` (client key == column).
_FILTER_FIELDS = ('gender', 'category', 'season', 'source', 'framework', 'author')


@frappe.whitelist(allow_guest=True)
@auth_required
def list_all(filters=None, limit=20, offset=0):
    '''
    Paginated list of WGSN trend reports — light rows only; the heavy rich-text
    fields (opportunity) and the trend_items detail rows stay out of the list.
    Each row carries trendItemsCount so the client can show the item count
    without fetching the report.

    Optional filters (dict or JSON string): gender, category, season, source,
    framework, author (exact match), search (report_title substring,
    >= 3 chars).

    Pagination: limit (default 20, capped 100) + offset (>= 0). Returns
    { total, limit, offset, items }.
    '''
    filters = _as_dict(filters)
    limit, offset = _page(limit, offset)

    q = {}
    for key in _FILTER_FIELDS:
        if filters.get(key):
            q[key] = filters[key]
    search = (filters.get('search') or '').strip()
    if len(search) >= _SEARCH_MIN_CHARS:
        q['report_title'] = ['like', f'%{search}%']

    total = frappe.db.count(DOCTYPE, q)
    rows = frappe.get_all(
        DOCTYPE, filters=q,
        fields=['name', 'report_title', 'gender', 'category', 'author',
                'published_date', 'season', 'source', 'framework', 'core_theme',
                'stepic_forecasts', 'extraction_date', 'creation', 'modified'],
        order_by='creation desc',
        ignore_permissions=True,
        limit_start=offset, limit_page_length=limit,
    )

    counts = _trend_item_counts([r['name'] for r in rows])

    items = [{
        'id': r['name'],
        'reportTitle': r.get('report_title'),
        'gender': r.get('gender') or None,
        'category': r.get('category') or None,
        'author': r.get('author') or None,
        'publishedDate': _date(r.get('published_date')),
        'season': r.get('season') or None,
        'source': r.get('source') or None,
        'framework': r.get('framework') or None,
        'coreTheme': r.get('core_theme') or None,
        'stepicForecasts': r.get('stepic_forecasts') or None,
        'extractionDate': _date(r.get('extraction_date')),
        'trendItemsCount': counts.get(r['name'], 0),
        'createdAt': _iso(r.get('creation')),
        'modifiedAt': _iso(r.get('modified')),
    } for r in rows]

    return _page_envelope(items, total, limit, offset)


def _trend_item_counts(report_names):
    ''' {report name: trend_items row count} for the page's reports, one query. '''
    if not report_names:
        return {}
    Item = frappe.qb.DocType(TREND_ITEM_DOCTYPE)
    rows = (
        frappe.qb.from_(Item)
        .select(Item.parent, Count(Item.name).as_('cnt'))
        .where(Item.parent.isin(report_names))
        .where(Item.parenttype == DOCTYPE)
        .groupby(Item.parent)
    ).run(as_dict=True)
    return {r['parent']: r['cnt'] for r in rows}


# =====================================================================
# Helpers (same shapes as moodboard_v2)
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


def _iso(value):
    if not value:
        return None
    return frappe.utils.get_datetime(value).isoformat()


def _date(value):
    return str(value) if value else None
