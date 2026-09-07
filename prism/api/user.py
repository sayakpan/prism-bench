import frappe


@frappe.whitelist(allow_guest=True)
def get_all(search: str = '', start: int = 0, page_length: int = 20, type: str = ''):
    ''' Returns a paginated list of users with name, roles, image, and brand info, optionally filtered by a search term.
        Intended for dropdown usage: search matches user `name` or `full_name`.
        `type` filter: '' (all), 'brand' (only Brand Users), 'internal' (users not linked to any Brand).
    '''
    try:
        start = int(start or 0)
        page_length = int(page_length or 20)
        term = (search or '').strip()
        user_type = (type or '').strip().lower()

        brand_user_ids = [d.user for d in frappe.get_all('Brand User', fields=['user']) if d.user]
        brand_user_set = set(brand_user_ids)

        filters = {}
        or_filters = None
        if term:
            like_term = f'%{term}%'
            or_filters = [
                ['name', 'like', like_term],
                ['full_name', 'like', like_term],
            ]

        if user_type == 'brand':
            filters['name'] = ['in', brand_user_ids or ['']]
        elif user_type == 'internal':
            if brand_user_ids:
                filters['name'] = ['not in', brand_user_ids]

        users = frappe.get_all('User',
            filters=filters,
            or_filters=or_filters,
            fields=['name', 'full_name', 'user_image'],
            order_by='full_name asc',
            start=start,
            page_length=page_length
        )

        if or_filters:
            total = len(frappe.get_all('User', filters=filters, or_filters=or_filters, fields=['name']))
        else:
            total = frappe.db.count('User', filters=filters)

        user_names = [u.name for u in users]

        roles_map = {}
        brand_map = {}
        if user_names:
            role_rows = frappe.get_all('Has Role',
                filters={'parent': ['in', user_names], 'parenttype': 'User'},
                fields=['parent', 'role']
            )
            for r in role_rows:
                roles_map.setdefault(r.parent, []).append(r.role)

            brand_user_rows = frappe.get_all('Brand User',
                filters={'user': ['in', user_names]},
                fields=['user', 'brand']
            )
            brand_ids = list({bu.brand for bu in brand_user_rows if bu.brand})
            brand_name_map = {}
            if brand_ids:
                brand_rows = frappe.get_all('Brand',
                    filters={'name': ['in', brand_ids]},
                    fields=['name', 'brand']
                )
                brand_name_map = {b.name: b.brand for b in brand_rows}

            for bu in brand_user_rows:
                brand_map[bu.user] = {
                    'brand': bu.brand,
                    'brand_name': brand_name_map.get(bu.brand)
                }

        result = []
        for u in users:
            is_brand_user = u.name in brand_user_set
            entry = {
                'name': u.name,
                'full_name': u.full_name,
                'image': u.user_image,
                'type': 'brand' if is_brand_user else 'internal',
                'roles': roles_map.get(u.name, [])
            }
            if is_brand_user:
                bm = brand_map.get(u.name, {})
                entry['brand'] = bm.get('brand')
                entry['brand_name'] = bm.get('brand_name')
            result.append(entry)

        return {
            'success': True,
            'users': result,
            'start': start,
            'page_length': page_length,
            'total': total,
            'has_more': (start + len(result)) < total
        }

    except Exception as ex:
        frappe.log_error(frappe.get_traceback(), 'user.get_all()')
        return {'success': False, 'error': str(ex)}
