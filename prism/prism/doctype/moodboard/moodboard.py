# Copyright (c) 2026, ws and contributors
# For license information, please see license.txt

# import frappe
from frappe.model.document import Document

import prism.lib.moodboard_media as media


class Moodboard(Document):
	def before_save(self):
		# Offload the garment cleaned front/back images to S3 (idempotent). The
		# board thumbnail is a compressed WebP generated where it is assigned
		# (set_primary_version / thumbnail PATCH), not here.
		media.offload_board(self)

