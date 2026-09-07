import frappe
from frappe.model.document import Document


class BrandPageAIChat(Document):
	pass


def on_doctype_update():
	# Fast lookup of a brand's chats, newest first.
	frappe.db.add_index("Brand Page AI Chat", ["brand", "last_active"])
