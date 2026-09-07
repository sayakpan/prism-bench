# Copyright (c) 2026, ws and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document


class MoodboardDyedFabric(Document):
	def validate(self):
		self._stamp_manual_fabric_match()

	def _stamp_manual_fabric_match(self):
		'''
		A hand-picked Closest Fabric Master is recorded as Manual, so a later
		bulk run leaves it alone.

		This fires on document saves only, which is exactly the distinction
		wanted: the matcher writes through `frappe.db.set_value` and never
		reaches here, so the only way to land in this branch is a person having
		changed the link themselves.
		'''
		if self.is_new() or not self.has_value_changed('closest_fabric_master'):
			return

		# The stored per-kg cost belongs to the fabric it was computed from, so it
		# is dropped either way. Recosting here would mean LLM calls inside a
		# document save; "Refresh Fabric Master Cost" puts it back.
		self.closest_fabric_cost_per_kg = 0
		self.closest_fabric_costed_on = None

		if self.closest_fabric_master:
			self.closest_match_method = 'Manual'
			self.closest_match_reason = f'Set manually by {frappe.session.user}.'
			self.closest_match_score = 0
			self.closest_match_alternates = None
			# Cleared rather than recomputed: the signature says "this is the
			# match the scorer would make for this quality/blend/GSM", and after
			# a hand edit that is no longer a claim this row can make.
			self.closest_match_signature = None
			self.closest_matched_on = frappe.utils.now_datetime()
		else:
			# Link cleared -- drop the whole match rather than leave orphaned
			# codes, scores and reasons describing a fabric no longer pointed at.
			self.closest_fabric_code = None
			self.closest_fabric_desc = None
			self.closest_match_score = 0
			self.closest_match_method = None
			self.closest_match_reason = None
			self.closest_match_alternates = None
			self.closest_match_signature = None
			self.closest_matched_on = None
