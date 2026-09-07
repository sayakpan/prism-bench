import frappe
from frappe.model.document import Document

import prism.lib.moodboard_media as media


class MoodboardVersion(Document):
    def before_save(self):
        # Move any freshly-staged local image (image / edited_image / canvas_state)
        # to S3. Idempotent — values already on S3 are left untouched.
        media.offload_version(self)

    # NOTE: no on_trash S3 cleanup on purpose. duplicate_moodboard() copies version
    # image URLs by value, so a board and its duplicate can share the same S3
    # object; deleting one must not remove an object the other still references.
    # Deletes stay orphan-tolerant (as they were for local files) — reclaim unused
    # objects later with a reference-aware sweep.
