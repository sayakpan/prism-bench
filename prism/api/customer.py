import frappe


@frappe.whitelist(allow_guest=True)
#@auth_required
def get_all():
    ''' Returns the list of all customers sorted by brand. '''
    try:
        customers = frappe.get_all('Customer',
            fields=[
                'name as id',
                'brand',
                'company_name',
                'country',
                'status'
            ],
            order_by='brand asc'
        )

        return {
            'success': True,
            'customers': customers
        }

    except Exception as ex:
        frappe.log_error(frappe.get_traceback(), 'customer.get_all()')
        return {'success': False, 'error': str(ex)}

@frappe.whitelist(allow_guest=True)
#@auth_required
def search(search_str: str, num_max_items: int=10):
    ''' Returns the list of customers matching the search string, sorted by the closest match. '''
    try:
        term = (search_str or '').strip()
        if not term:
            return {'success': True, 'customers': []}

        like_term = f'%{term}%'
        lower_term = term.lower()

        customers = frappe.get_all('Customer',
            or_filters=[
                ['brand', 'like', like_term],
                ['company_name', 'like', like_term],
            ],
            fields=[
                'name as id',
                'brand',
                'company_name',
                'country',
                'status'
            ]
        )

        def rank(c):
            brand = (c.get('brand') or '').lower()
            company = (c.get('company_name') or '').lower()
            if brand == lower_term or company == lower_term:
                return (0, brand)
            if brand.startswith(lower_term) or company.startswith(lower_term):
                return (1, brand)
            return (2, brand)

        customers.sort(key=rank)

        return {
            'success': True,
            'customers': customers[:num_max_items]
        }

    except Exception as ex:
        frappe.log_error(frappe.get_traceback(), 'customer.search()')
        return {'success': False, 'error': str(ex)}
