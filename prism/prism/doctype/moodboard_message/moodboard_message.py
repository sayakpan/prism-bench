import frappe
from frappe.model.document import Document

import prism.lib.moodboard_media as media


class MoodboardMessage(Document):
    def before_save(self):
        # Offload images embedded in the attachments JSON to S3 (idempotent).
        media.offload_message(self)
