import frappe
from frappe.model.document import Document
from frappe.utils import flt

# Everything the field spec marks Reqd=Yes. These are NOT reqd on the DocType:
# five of them are genuinely absent from at least one of the three source
# formats (no HS code on either KIABI order, no composition on Tchibo or DMart,
# no incoterm on DMart, no colour on Tchibo or DMart, no size split on DMart),
# and a reqd child field would block the parent from saving at all. So the
# parser can always land a partial Draft, and the requirement is enforced at
# the verification gate instead.
_REQUIRED_ON_VERIFY = (
    'brand',
    'buyer_entity',
    'po_number',
    'po_date',
    'po_type',
    'style_code',
    'style_description',
    'product_category',
    'fabric_composition',
    'hs_code',
    'currency',
    'base_unit_price',
    'total_order_qty',
    'uom',
    'total_order_value',
    'incoterm',
    'delivery_date',
    'delivery_location',
    'supplier_name',
    'source_file',
    'source_format',
)

_REQUIRED_LINE_ON_VERIFY = (
    'article_code',
    'item_description',
    'colour_name',
    'quantity',
    'uom',
    'unit_price',
    'line_amount',
    'delivery_date',
)

# Everything from this marker to the end of extraction_notes is machine-owned
# and rewritten on every save. Anything a human types above it survives.
_AUTO_MARKER = '--- auto-checks ---'

_QTY_TOLERANCE = 0.01
_VALUE_TOLERANCE = 0.05


class BuyerPO(Document):
    def validate(self):
        self._notes = []
        self._check_duplicate()
        self._check_currency()
        self._apply_line_defaults()
        self._reconcile_totals()
        self._check_required_on_verify()
        self._write_auto_notes()

    # A PO number alone is not unique — the same number is reissued as a
    # revision, and two buyers can collide. The key is the triple.
    def _check_duplicate(self):
        if not self.po_number:
            return

        rows = frappe.db.get_all(
            'Buyer PO',
            filters={'po_number': self.po_number},
            fields=['name', 'buyer_entity', 'revision_no'],
        )
        for row in rows:
            if row.name == self.name:
                continue
            if (row.buyer_entity or '') != (self.buyer_entity or ''):
                continue
            if (row.revision_no or '') != (self.revision_no or ''):
                continue
            frappe.throw(
                f'{row.name} already holds PO {self.po_number}'
                f'{" rev " + self.revision_no if self.revision_no else ""}'
                f' for this buyer. Set a Revision / Version to land an amendment.',
                frappe.DuplicateEntryError,
            )

    # Currency is free text so an unrecognised code can still land, but money
    # amounts will not format against a currency the system does not know —
    # so say so rather than letting it pass silently.
    def _check_currency(self):
        if not self.currency:
            return
        if not frappe.db.exists('Currency', self.currency):
            self._notes.append(
                f'Currency "{self.currency}" is not in the Currency master — '
                f'amounts will not format with a symbol until it is added.'
            )

    def _apply_line_defaults(self):
        for line in self.lines:
            # Currency fields resolve their symbol through a sibling field, and
            # a child row cannot reach parent.currency — so mirror it down.
            line.currency = self.currency

            if not line.unit_price:
                line.unit_price = self.base_unit_price
            if not line.uom:
                line.uom = self.uom
            if not line.delivery_date:
                line.delivery_date = self.delivery_date

            # tax_amount is per unit, so this lands on the printed landed value
            # for domestic orders and on qty x price for everything else.
            expected = flt(line.quantity) * (flt(line.unit_price) + flt(line.tax_amount))
            printed = flt(line.line_amount)
            if printed and abs(printed - expected) > _VALUE_TOLERANCE:
                self._notes.append(
                    f'Line {line.idx}: printed amount {printed:,.2f} does not match '
                    f'{flt(line.quantity):,.2f} x {flt(line.unit_price) + flt(line.tax_amount):,.4f} '
                    f'= {expected:,.2f}. Recomputed value kept.'
                )
            line.line_amount = expected

    def _reconcile_totals(self):
        self.computed_total_qty = sum(flt(line.quantity) for line in self.lines)
        self.computed_total_value = sum(flt(line.line_amount) for line in self.lines)

        if not self.lines:
            self.qty_variance = 0
            self.value_variance = 0
            return

        # Tchibo prints no order value at all — derive it rather than leaving a
        # required field empty and a variance that means nothing.
        if not flt(self.total_order_qty):
            self.total_order_qty = self.computed_total_qty
            self._notes.append('Total order quantity not printed — derived from the lines.')
        if not flt(self.total_order_value):
            self.total_order_value = self.computed_total_value
            self._notes.append('Total order value not printed — derived from the lines.')

        self.qty_variance = flt(self.computed_total_qty) - flt(self.total_order_qty)
        self.value_variance = flt(self.computed_total_value) - flt(self.total_order_value)

        if abs(flt(self.qty_variance)) > _QTY_TOLERANCE:
            self._notes.append(
                f'Quantity mismatch: lines total {flt(self.computed_total_qty):,.2f} '
                f'against printed {flt(self.total_order_qty):,.2f}.'
            )
        if abs(flt(self.value_variance)) > _VALUE_TOLERANCE:
            self._notes.append(
                f'Value mismatch: lines total {flt(self.computed_total_value):,.2f} '
                f'against printed {flt(self.total_order_value):,.2f}.'
            )

    def _check_required_on_verify(self):
        missing = [
            self.meta.get_label(fieldname)
            for fieldname in _REQUIRED_ON_VERIFY
            if not self.get(fieldname)
        ]

        # A whole-column gap (no colour anywhere on a Tchibo contract) is one
        # fact, not one fact per row — so group the rows by which fields they
        # are short of rather than listing 64 near-identical lines.
        line_meta = frappe.get_meta('Buyer PO Line Item')
        by_gap = {}
        for line in self.lines:
            gaps = tuple(
                line_meta.get_label(fieldname)
                for fieldname in _REQUIRED_LINE_ON_VERIFY
                # A PO that gives only a size span (DMart) satisfies Size
                # through Size Range — the spec carries both for this reason.
                if not line.get(fieldname)
                and not (fieldname == 'size' and line.get('size_range'))
            )
            if gaps:
                by_gap.setdefault(gaps, []).append(line.idx)

        line_missing = []
        for gaps, idxs in by_gap.items():
            where = f'line {idxs[0]}' if len(idxs) == 1 else f'{len(idxs)} lines'
            line_missing.append(f'{where}: {", ".join(gaps)}')

        if self.extraction_status != 'Verified':
            # Below the gate these are advisory — the whole point of Draft.
            if missing:
                self._notes.append(f'Missing for verification: {", ".join(missing)}.')
            for entry in line_missing:
                self._notes.append(f'Missing for verification on {entry}.')
            return

        problems = []
        if missing:
            problems.append(f'Header: {", ".join(missing)}')
        problems.extend(line_missing)

        if problems:
            frappe.throw(
                'Cannot mark this PO Verified — required values are still missing:<br>'
                + '<br>'.join(problems),
                frappe.ValidationError,
            )

        if abs(flt(self.qty_variance)) > _QTY_TOLERANCE:
            frappe.throw(
                f'Cannot mark this PO Verified — the lines total '
                f'{flt(self.computed_total_qty):,.2f} against a printed order quantity of '
                f'{flt(self.total_order_qty):,.2f}.',
                frappe.ValidationError,
            )

    def _write_auto_notes(self):
        human = (self.extraction_notes or '').split(_AUTO_MARKER)[0].rstrip()
        if not self._notes:
            self.extraction_notes = human or None
            return

        block = '\n'.join(f'- {note}' for note in self._notes)
        self.extraction_notes = f'{human}\n\n{_AUTO_MARKER}\n{block}'.strip()
