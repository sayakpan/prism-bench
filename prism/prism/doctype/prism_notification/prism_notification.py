import frappe
from frappe.model.document import Document


class PrismNotification(Document):
    '''
    A single cross-system notification row. Frappe is the source of truth;
    prism-bot only relays the live push and proxies read-state changes back
    here. See prism.api.notifications for the dispatch/read API.
    '''

    def before_insert(self):
        # Stamp a default category if a caller inserted a row directly without
        # going through notify() (which already resolves this).
        if not self.category:
            import prism.api.notifications as notifications
            self.category = notifications.category_for(self.event_type)
