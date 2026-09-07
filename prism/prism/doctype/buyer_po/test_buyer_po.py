import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import flt


def _po(**overrides):
    doc = {
        'doctype': 'Buyer PO',
        'brand': 'KIABI',
        'buyer_entity': 'International Trading Fashion and Apparel Supply Limited',
        'po_number': '145672 / 00',
        'po_date': '2025-10-11',
        'po_type': 'Purchase Order',
        'style_code': 'ADMS26TOSEC',
        'style_description': 'TS MC CONFORT FIT UNI SPORT',
        'product_category': 'T-Shirt Short Sleeves',
        'fabric_composition': '100% Cotton (including Organic Cotton)',
        'hs_code': '61091000',
        'currency': 'EUR',
        'base_unit_price': 2.96,
        'uom': 'PCS',
        'incoterm': 'FOB - Port Incoterm 2000 DEL',
        'delivery_date': '2026-01-06',
        'delivery_location': 'Kiabi Logistique - EBA, Tarragona, Spain',
        'supplier_name': 'Pratibha Syntex Limited, Pithampur',
        'source_file': '/files/dummy.xlsx',
        'source_format': 'KIABI-ITX',
        'extraction_status': 'Parsed',
        'lines': [],
    }
    doc.update(overrides)
    return frappe.get_doc(doc)


def _line(**overrides):
    row = {
        'article_code': 'ADMS26TOSEC',
        'item_description': 'TS MC CONFORT FIT UNI SPORT',
        'colour_name': 'BLEU TROOP',
        'size': 'XS',
        'quantity': 2,
        'uom': 'PCS',
        'unit_price': 2.96,
        'delivery_date': '2026-01-06',
    }
    row.update(overrides)
    return row


class TestBuyerPO(FrappeTestCase):
    def tearDown(self):
        frappe.db.rollback()

    def test_line_amount_is_recomputed_from_qty_and_price(self):
        doc = _po(lines=[_line(quantity=9), _line(quantity=17, size='M')]).insert()
        self.assertEqual(flt(doc.lines[0].line_amount, 2), 26.64)
        self.assertEqual(flt(doc.lines[1].line_amount, 2), 50.32)

    def test_line_amount_includes_per_unit_tax_for_domestic_orders(self):
        # DMart prints a landed line value: 3240 x (145.00 net + 7.25 IGST).
        doc = _po(
            source_format='DMart',
            currency='INR',
            base_unit_price=145.0,
            uom='EA',
            lines=[_line(quantity=3240, uom='EA', unit_price=145.0,
                         tax_rate=5, tax_amount=7.25)],
        ).insert()
        self.assertEqual(flt(doc.lines[0].line_amount, 2), 493290.0)

    def test_blank_line_price_falls_back_to_header_base_price(self):
        doc = _po(lines=[_line(unit_price=None, quantity=10)]).insert()
        self.assertEqual(flt(doc.lines[0].unit_price, 2), 2.96)
        self.assertEqual(flt(doc.lines[0].line_amount, 2), 29.6)

    def test_totals_are_derived_when_the_po_prints_none(self):
        # Tchibo prints no order value — it must come from the lines.
        doc = _po(lines=[_line(quantity=700, unit_price=5.09),
                         _line(quantity=1136, unit_price=5.09)]).insert()
        self.assertEqual(flt(doc.total_order_qty, 2), 1836.0)
        self.assertEqual(flt(doc.total_order_value, 2), 9345.24)
        self.assertEqual(flt(doc.qty_variance, 2), 0.0)
        self.assertIn('not printed', doc.extraction_notes)

    def test_printed_totals_that_disagree_with_lines_raise_a_variance(self):
        doc = _po(total_order_qty=999, total_order_value=100.0,
                  lines=[_line(quantity=2)]).insert()
        self.assertEqual(flt(doc.computed_total_qty, 2), 2.0)
        self.assertEqual(flt(doc.qty_variance, 2), -997.0)
        self.assertIn('Quantity mismatch', doc.extraction_notes)

    def test_partial_extraction_saves_as_draft_with_advisory_notes(self):
        # None of the spec's Reqd=Yes fields may block a parser from landing.
        doc = _po(hs_code=None, fabric_composition=None, incoterm=None,
                  extraction_status='Draft',
                  lines=[_line(colour_name=None)]).insert()
        self.assertEqual(doc.extraction_status, 'Draft')
        self.assertIn('Missing for verification', doc.extraction_notes)

    def test_verify_is_refused_while_required_values_are_missing(self):
        doc = _po(hs_code=None, lines=[_line()]).insert()
        doc.extraction_status = 'Verified'
        self.assertRaises(frappe.ValidationError, doc.save)

    def test_verify_is_refused_while_totals_do_not_reconcile(self):
        doc = _po(total_order_qty=999, lines=[_line(quantity=2)]).insert()
        doc.extraction_status = 'Verified'
        self.assertRaises(frappe.ValidationError, doc.save)

    def test_verify_succeeds_on_a_complete_reconciled_po(self):
        doc = _po(lines=[_line()]).insert()
        doc.extraction_status = 'Verified'
        doc.save()
        self.assertEqual(doc.extraction_status, 'Verified')

    def test_size_range_satisfies_size_at_the_gate(self):
        # DMart gives only a span (S-2XL) and no per-size split.
        doc = _po(lines=[_line(size=None, size_range='S-2XL')]).insert()
        doc.extraction_status = 'Verified'
        doc.save()
        self.assertEqual(doc.extraction_status, 'Verified')

    def test_reuploading_the_same_po_is_refused(self):
        _po(lines=[_line()]).insert()
        self.assertRaises(frappe.DuplicateEntryError, _po(lines=[_line()]).insert)

    def test_an_amendment_lands_alongside_the_original(self):
        first = _po(lines=[_line()]).insert()
        second = _po(revision_no='01', lines=[_line()]).insert()
        self.assertNotEqual(first.name, second.name)
        self.assertEqual(first.po_number, second.po_number)

    def test_the_same_po_number_from_a_different_buyer_is_allowed(self):
        _po(lines=[_line()]).insert()
        other = _po(buyer_entity='Avenue Supermarts Ltd.', lines=[_line()]).insert()
        self.assertTrue(other.name)

    def test_human_notes_survive_the_auto_check_rewrite(self):
        # A standing variance keeps a warning on every save.
        doc = _po(total_order_qty=999, lines=[_line(quantity=2)]).insert()
        doc.extraction_notes = 'Checked against the scan by hand.'
        doc.save()
        doc.save()
        self.assertIn('Checked against the scan by hand.', doc.extraction_notes)
        # The machine block is rewritten, not appended, on every save.
        self.assertEqual(doc.extraction_notes.count('--- auto-checks ---'), 1)
        self.assertIn('Quantity mismatch', doc.extraction_notes)

    def test_the_auto_block_clears_once_the_warnings_are_resolved(self):
        doc = _po(total_order_qty=999, lines=[_line(quantity=2)]).insert()
        self.assertIn('--- auto-checks ---', doc.extraction_notes)
        doc.total_order_qty = 2
        doc.save()
        self.assertNotIn('--- auto-checks ---', doc.extraction_notes or '')

    def test_line_currency_mirrors_the_order_currency(self):
        doc = _po(currency='USD', lines=[_line()]).insert()
        self.assertEqual(doc.lines[0].currency, 'USD')

    def test_unseen_values_land_without_being_rejected(self):
        # None of these are in any option list — a new buyer format, a new
        # document type, a new selling unit must never block the extraction.
        doc = _po(
            source_format='Primark-EU',
            po_type='Blanket Release',
            uom='DOZ',
            mode_of_transport='Multimodal',
            lines=[_line(uom='DOZ')],
        ).insert()
        self.assertEqual(doc.source_format, 'Primark-EU')
        self.assertEqual(doc.po_type, 'Blanket Release')
        self.assertEqual(doc.lines[0].uom, 'DOZ')

    def test_an_unknown_currency_lands_but_is_flagged(self):
        doc = _po(currency='XYZ', lines=[_line()]).insert()
        self.assertEqual(doc.currency, 'XYZ')
        self.assertIn('not in the Currency master', doc.extraction_notes)

    def test_a_known_currency_is_not_flagged(self):
        doc = _po(currency='EUR', lines=[_line()]).insert()
        self.assertNotIn('Currency master', doc.extraction_notes or '')
