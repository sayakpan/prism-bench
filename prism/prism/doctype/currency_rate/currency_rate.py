# Copyright (c) 2026, ws and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document


class CurrencyRate(Document):
	def before_naming(self):
		# The docname is field:currency_code, so the code has to be normalised
		# before naming runs -- otherwise "inr" and "INR" become two rows that
		# the unique constraint can no longer catch.
		self.currency_code = (self.currency_code or "").strip().upper()

	def validate(self):
		self.currency_code = (self.currency_code or "").strip().upper()
		self.currency_symbol = (self.currency_symbol or "").strip() or None

		if not self.currency_code:
			frappe.throw("Currency Code is required.")

		if self.currency_code == "USD":
			# USD is the base -- pinning it stops a stray edit from silently
			# rescaling every figure derived from this table.
			self.rate_to_usd = 1.0
		elif not self.rate_to_usd or float(self.rate_to_usd) <= 0:
			frappe.throw("Rate to USD must be greater than 0.")
