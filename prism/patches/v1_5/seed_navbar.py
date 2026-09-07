'''
v1_5 patch: seed the single, centralized Navbar with the default menu tree.

The navbar is now one global record (Single DocType `Navbar`) shared by all
brands — not brand-specific. This populates it from the bundled
default_navbar.json on first migrate.

Runs post_model_sync (after the Navbar doctype is created). Idempotent:
seed_default() leaves an already-populated navbar untouched.
'''

import frappe

from prism.prism.doctype.navbar.navbar import seed_default


def execute():
    frappe.reload_doc('prism', 'doctype', 'navbar')
    seed_default(overwrite=False)
