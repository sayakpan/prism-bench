import frappe

from prism.prism.doctype.navbar.navbar import _load_default

DOCTYPE = 'Navbar'


@frappe.whitelist(allow_guest=True)
def get_nav():
    '''
    The single, centralized navbar shared by ALL brands (not brand-specific):
    a nested array of { label, href, image?, children? }. Falls back to the
    bundled default tree if nothing has been saved yet.
    '''
    try:
        raw = frappe.db.get_single_value(DOCTYPE, 'links')
        links = frappe.parse_json(raw) if raw else None
        if not links:
            links = _load_default()

        return {'success': True, 'data': {'links': links, 'total': _count(links)}}

    except Exception as ex:
        frappe.log_error(frappe.get_traceback(), 'navbar.get_nav()')
        return {'success': False, 'error': str(ex)}


def _count(nodes):
    ''' Total node count across the whole tree. '''
    total = 0
    for node in nodes or []:
        total += 1
        total += _count(node.get('children'))
    return total
