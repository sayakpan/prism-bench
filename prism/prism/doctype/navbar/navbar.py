import json
import os

import frappe
from frappe.model.document import Document


class Navbar(Document):
    def validate(self):
        # `links` must always be a JSON array (empty allowed). Normalize to a string.
        if self.links in (None, ''):
            self.links = '[]'
            return

        data = frappe.parse_json(self.links) if isinstance(self.links, str) else self.links
        if not isinstance(data, list):
            frappe.throw('Links must be a JSON array of menu nodes.')

        self.links = json.dumps(data, ensure_ascii=False, indent=2)


# --- seeding --------------------------------------------------------------

def _load_default():
    ''' The bundled default navigation tree (links only). '''
    path = os.path.join(os.path.dirname(__file__), 'default_navbar.json')
    with open(path, 'r', encoding='utf-8') as f:
        return json.load(f)


def seed_default(overwrite=False):
    '''
    Populate the single Navbar with the bundled default tree.
    Idempotent: leaves an already-populated navbar untouched unless
    overwrite=True. Returns True if it wrote, False if it skipped.
    '''
    doc = frappe.get_single('Navbar')
    existing = frappe.parse_json(doc.links) if doc.links else []
    if existing and not overwrite:
        return False

    doc.links = json.dumps(_load_default(), ensure_ascii=False, indent=2)
    doc.save(ignore_permissions=True)
    frappe.db.commit()
    return True
