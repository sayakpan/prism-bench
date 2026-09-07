# Copyright (c) 2026, ws and contributors
# For license information, please see license.txt

from frappe.model.document import Document


class FabricConstructionFamily(Document):
	def before_save(self):
		# The family key is matched against surplus_recommender's classifier
		# output, which is always lowercase. A stray capital would silently
		# match nothing.
		if self.family_key:
			self.family_key = self.family_key.strip().lower()
		if self.construction:
			self.construction = self.construction.strip()
