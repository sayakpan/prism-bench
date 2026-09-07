import frappe
from frappe.model.document import Document


class TechpackCosting(Document):
	def before_insert(self):
		# fall back to the attached pdf's file name (sans extension) when title is blank
		if not self.title and self.tech_pack:
			file_name = frappe.db.get_value('File', {'file_url': self.tech_pack}, 'file_name')
			if file_name:
				self.title = file_name.rsplit('.', 1)[0].replace('_', ' ')

	def after_insert(self):
		if not self.tech_pack:
			return

		frappe.enqueue(
			'prism.api.techpack.calculate_cost',
			queue='long',
			techpack_costing_name=self.name,
		)
