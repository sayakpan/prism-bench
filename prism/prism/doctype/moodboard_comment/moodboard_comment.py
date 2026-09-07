import frappe
from frappe.model.document import Document


class MoodboardComment(Document):
    def after_insert(self):
        '''
        Single fan-out point for new comments AND replies (both arrive here as
        inserts). Notification Log + realtime are best-effort: a failure here
        must never roll back the comment itself.
        '''
        import prism.api.moodboard_comment as mbc

        try:
            mbc.dispatch_new_comment(self)
        except Exception:
            frappe.log_error(frappe.get_traceback(), 'MoodboardComment.after_insert')
