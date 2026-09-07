import frappe
from frappe.model.document import Document


class BrandPageAIMessage(Document):
	pass


def on_doctype_update():
	# (chat, seq) — ordered reads of a chat's messages.
	frappe.db.add_index("Brand Page AI Message", ["chat", "seq"])
	# (chat, is_deleted) — filtering out soft-deleted rows per chat.
	frappe.db.add_index("Brand Page AI Message", ["chat", "is_deleted"])
