# Copyright (c) 2026, ws and contributors
# For license information, please see license.txt

from frappe.model.document import Document

# Pre-migration AOP/Digital values (title-case Yes/No) -> current select options.
# Healed on save so legacy rows re-validate cleanly against the new options.
_AOP_LEGACY = {'no': 'NO', 'yes': 'YES'}


class MoodboardESG(Document):
	def before_save(self):
		for row in self.elements:
			value = row.finishing_aop_digital
			if isinstance(value, str) and value.strip().lower() in _AOP_LEGACY:
				row.finishing_aop_digital = _AOP_LEGACY[value.strip().lower()]
