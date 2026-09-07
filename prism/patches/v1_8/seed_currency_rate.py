'''
v1_8 patch: seed the Currency Rate master with the currencies Brand Master Data's
scraped prices actually turn up in, plus the obvious neighbours.

`rate_to_usd` is USD per 1 unit of the currency. INR and EUR are carried over from
the existing Costing Currency rows (86.0 INR/USD, 89.0 INR/EUR) so the two masters
agree on day one rather than quietly disagreeing; the rest are round market figures
as of the RATE_AS_OF date below. These are static seeds -- whoever maintains pricing
is expected to keep them current in desk, which is the whole reason this lives in a
doctype instead of a constant in code.

Idempotent: a currency that already has a row is left completely alone, so a rate
someone has since updated by hand survives a re-run.
'''

import frappe

RATE_AS_OF = '2026-08-13'

# (code, name, symbol, USD per 1 unit)
RATES = (
    ('USD', 'US Dollar', '$', 1.0),
    ('INR', 'Indian Rupee', '₹', 0.010484),      # 1 INR ≈ $0.010484
    ('EUR', 'Euro', '€', 1.152195),              # 1 EUR ≈ $1.152195
    ('GBP', 'Pound Sterling', '£', 1.349624),    # 1 GBP ≈ $1.349624
    ('CAD', 'Canadian Dollar', 'C$', 0.716825),  # 1 CAD ≈ $0.716825
    ('AUD', 'Australian Dollar', 'A$', 0.704791), # 1 AUD ≈ $0.704791
    ('JPY', 'Japanese Yen', '¥', 0.006274),      # 1 JPY ≈ $0.006274
)

def execute():
    created = 0
    for code, name, symbol, rate in RATES:
        if frappe.db.exists('Currency Rate', code):
            continue
        frappe.get_doc({
            'doctype': 'Currency Rate',
            'currency_code': code,
            'currency_name': name,
            'currency_symbol': symbol,
            'rate_to_usd': rate,
            'rate_as_of': RATE_AS_OF,
        }).insert(ignore_permissions=True)
        created += 1

    frappe.db.commit()
    summary = f'{created} of {len(RATES)} currency rates seeded'
    frappe.logger('prism').info(f'[seed currency rate] {summary}')
    print(f'[seed currency rate] {summary}')
