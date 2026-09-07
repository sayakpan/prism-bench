# Copyright (c) 2026, ws and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document

from prism.api.page_builder_diff import diff_layouts


class PageBuilderVersion(Document):
	pass


def record_version(page_builder_doc, old_layout, new_layout, action, target, actor=None):
	"""Create one Page Builder Version capturing a change.

	Stores the block-level diff rows (for display) plus the full ``new_layout``
	snapshot (for restore and any-to-any diff). Never raises: version logging
	must not break the page save that triggered it — failures are logged.

	Returns the created version name, or None on failure.
	"""
	try:
		diff = diff_layouts(old_layout, new_layout)

		version = frappe.new_doc("Page Builder Version")
		version.page_builder = page_builder_doc.name
		version.brand_id = page_builder_doc.brand_id
		version.target = target
		version.action = action
		version.actor = actor or frappe.session.user
		version.change_count = diff["change_count"]
		version.summary = diff["summary"]
		version.parent_version = frappe.db.get_value(
			"Page Builder Version",
			{"page_builder": page_builder_doc.name, "target": target},
			"name",
			order_by="creation desc",
		)
		version.layout_json = _as_json(new_layout)

		for row in diff["rows"]:
			version.append("changes", row)

		version.insert(ignore_permissions=True)
		return version.name

	except Exception:
		frappe.log_error(frappe.get_traceback(), "page_builder_version.record_version")
		return None


def _as_json(layout):
	if layout is None:
		return "{}"
	if isinstance(layout, str):
		return layout
	return frappe.as_json(layout)
