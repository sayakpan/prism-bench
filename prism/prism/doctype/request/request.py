import frappe
from frappe.model.document import Document
from frappe.model.naming import make_autoname

# Naming series, chosen from request_type (see §2.1).
_SERIES = {
    'Quote': 'REQ-QUO-.#####',
    'Sample': 'REQ-SMP-.#####',
}

# Legal status transitions enforced server-side (§3). The state machine lives
# here so the rules hold regardless of which client (or which racing PSL user)
# calls the whitelisted methods. A no-op (status unchanged) is always allowed.
_TRANSITIONS = {
    'Pending':   {'Quoted', 'Approved', 'Rejected'},
    'Quoted':    {'Quoted', 'Countered', 'Approved', 'Rejected', 'Pending'},
    'Countered': {'Quoted', 'Countered', 'Approved', 'Rejected', 'Pending'},
    'Approved':  {'Pending'},
    'Rejected':  {'Pending'},
}


class Request(Document):
    def autoname(self):
        series = _SERIES.get(self.request_type, 'REQ-.#####')
        self.name = make_autoname(series)

    def validate(self):
        self._validate_status_transition()

    def _validate_status_transition(self):
        before = self.get_doc_before_save()
        # New document: must start in Pending.
        if not before:
            if self.status and self.status != 'Pending':
                frappe.throw(
                    f'A new request must start as Pending, not {self.status}.',
                    frappe.ValidationError,
                )
            return

        old = before.status
        new = self.status
        if old == new:
            return

        allowed = _TRANSITIONS.get(old, set())
        if new not in allowed:
            frappe.throw(
                f'Illegal status transition: {old} → {new}.',
                frappe.ValidationError,
            )
