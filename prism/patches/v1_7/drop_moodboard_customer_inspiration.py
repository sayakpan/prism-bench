'''
v1_7 patch: drop the redundant "Moodboard Customer Inspiration" child doctype.

The board's Project Brief section already carries "Moodboard Inspiration Image"
with the identical shape (image + label), so Customer Inspiration was a duplicate
that never took data. Removing the doctype's files does NOT remove it from the
database — Frappe leaves the DocType row and its `tab...` table behind — hence
this patch.

Guarded: if any row somehow exists, the patch leaves everything alone and logs,
rather than silently destroying content. Idempotent, so re-running is a no-op.
'''

import frappe

DOCTYPE = 'Moodboard Customer Inspiration'


def execute():
    if not frappe.db.exists('DocType', DOCTYPE):
        return

    if frappe.db.table_exists(DOCTYPE):
        rows = frappe.db.sql(f'SELECT COUNT(*) FROM `tab{DOCTYPE}`')[0][0]
        if rows:
            frappe.log_error(
                f'{DOCTYPE} still holds {rows} row(s); leaving the doctype in place. '
                'Migrate the data, then re-run this patch.',
                'v1_7.drop_moodboard_customer_inspiration')
            return

    # delete_doc on a DocType drops its table too.
    frappe.delete_doc('DocType', DOCTYPE, ignore_permissions=True, force=True)
    frappe.db.commit()
