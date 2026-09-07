import frappe
from frappe.model.document import Document
from frappe.utils import slug


class BrandCategory(Document):
    def autoname(self):
        self.name = generate_unique_slug(self.category_name, self.doctype)

def generate_unique_slug(text, doctype):
    text = text.replace('&', 'and')
    base_slug = slug(text)
    unique_slug = base_slug
    
    counter = 1
    while frappe.db.exists(doctype, unique_slug):
        unique_slug = f'{base_slug}-{counter}'
        counter += 1

    return unique_slug
