# Copyright (c) 2026, ws and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document


class GarmentFabricRule(Document):
	def validate(self):
		self.section = (self.section or 'Any').strip() or 'Any'
		self.garment_style = (self.garment_style or '').strip()

		if self.min_gsm and self.max_gsm and self.min_gsm > self.max_gsm:
			frappe.throw(_('Min GSM ({0}) cannot be above Max GSM ({1}).')
			             .format(self.min_gsm, self.max_gsm))

		# Half a band is not a band. The optimiser needs both ends to build a
		# range, and would silently fall back to its relative default with one --
		# which reads, on this screen, as though the rule were being applied.
		if bool(self.min_gsm) != bool(self.max_gsm):
			frappe.throw(_('Set both Min GSM and Max GSM, or neither. '
			               'A single bound is ignored.'))

		# A rule nobody has checked must not be able to widen anything, and
		# ticking Reviewed on an empty row is the easiest way to do that by
		# accident.
		if self.is_reviewed and not (self.min_gsm or self.allowed_families
		                             or self.requires_stretch
		                             or not self.allow_family_change):
			frappe.throw(_('This rule says nothing yet. Set a weight band, a list of '
			               'allowed families, or a stretch / construction restriction '
			               'before marking it Reviewed.'))
