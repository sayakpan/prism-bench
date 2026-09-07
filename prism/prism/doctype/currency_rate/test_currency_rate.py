# Copyright (c) 2026, ws and Contributors
# See license.txt

import frappe
from frappe.tests.utils import FrappeTestCase


class TestCurrencyRate(FrappeTestCase):
	def _make(self, code, rate=1.0, symbol=None):
		''' Insert a throwaway rate. Only ever called with codes the seed patch
		does not ship, so a run can never clobber a real rate. '''
		name = code.strip().upper()
		if frappe.db.exists('Currency Rate', name):
			frappe.delete_doc('Currency Rate', name, force=True)
		return frappe.get_doc({
			'doctype': 'Currency Rate',
			'currency_code': code,
			'currency_symbol': symbol,
			'rate_to_usd': rate,
		}).insert(ignore_permissions=True)

	def test_code_is_uppercased_before_naming(self):
		doc = self._make('  chf ', rate=1.12)
		self.assertEqual(doc.currency_code, 'CHF')
		self.assertEqual(doc.name, 'CHF')

	def test_usd_rate_is_pinned_to_one(self):
		# Edit the seeded USD row in place rather than replacing it — a stray
		# rate on the base currency would silently rescale every derived figure.
		doc = frappe.get_doc('Currency Rate', 'USD')
		doc.rate_to_usd = 42.0
		doc.save(ignore_permissions=True)
		self.assertEqual(doc.rate_to_usd, 1.0)

	def test_non_positive_rate_is_rejected(self):
		with self.assertRaises(frappe.ValidationError):
			self._make('zar', rate=0)

	def test_symbol_is_normalised_to_none_when_blank(self):
		doc = self._make('sek', rate=0.09, symbol='   ')
		self.assertIsNone(doc.currency_symbol)
