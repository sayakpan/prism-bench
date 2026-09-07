# Copyright (c) 2026, ws and contributors
# For license information, please see license.txt

import re

from frappe.model.document import Document

# Statuses a successful engine run may advance to "Prototype Ready". Anything
# further along (Scraped) is left alone. "Failed" is included so a good run
# clears an earlier failure rather than stranding the brand in an error state
# whose `last_engine_error` has already been wiped.
RESUMABLE_STATUSES = ('New', 'Engine Running', 'Failed')


class ScraperBrand(Document):
	def before_save(self):
		self.brand_name = (self.brand_name or '').strip()
		self.website = normalize_website(self.website)
		self.slug = slugify(self.brand_name)


def slugify(brand_name):
	'''
	Lower-cased alphanumeric form of a brand name -- "Brook There" -> "brookthere".
	Used to build the scraped-file name (`<slug>_<datetime>.xlsx`), so it must stay
	free of spaces, separators and anything that needs URL-escaping.
	'''
	return re.sub(r'[^a-z0-9]', '', (brand_name or '').lower()) or 'brand'


def normalize_website(website):
	'''
	Canonical form of a site URL so the same brand can't be created twice from
	`https://Brookthere.com/` and `brookthere.com`. Lower-cases the whole URL,
	adds the https:// scheme when missing and drops any trailing slash.
	'''
	site = (website or '').strip().lower()
	if not site:
		return ''
	if not site.startswith(('http://', 'https://')):
		site = f'https://{site}'
	return site.rstrip('/')
