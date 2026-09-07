# Copyright (c) 2026, ws and contributors
# For license information, please see license.txt

import hashlib
import json

import frappe
from frappe.model.document import Document

from prism.prism.doctype.scraper_brand.scraper_brand import RESUMABLE_STATUSES


class ScraperBrandVersion(Document):
	pass


def record_version(brand_doc, prototype_json, html_files, scraper_code, source='AI Engine', actor=None):
	"""Create one Scraper Brand Version from an engine run.

	The engine posts its full output on every run, whether or not it changed
	anything, so identical content is de-duplicated: when the incoming triple
	hashes to the same value as the brand's latest version, nothing is inserted
	and that existing version is returned instead. Version numbers therefore
	count real changes, not runs.

	Returns ``(version_doc, created)`` where ``created`` is False for a de-duped
	post. Raises on failure -- unlike page-builder version logging this *is* the
	operation being performed, not a side effect of one.
	"""
	prototype = normalize_prototype(prototype_json)
	files = normalize_html_files(html_files)
	code = normalize_code(scraper_code)

	content_hash = compute_content_hash(prototype, files, code)

	latest = get_latest_version(brand_doc.name)
	if latest and latest.content_hash == content_hash:
		return frappe.get_doc('Scraper Brand Version', latest.name), False

	# Lock the brand row so two concurrent engine runs can't claim one number.
	frappe.db.get_value('Scraper Brand', brand_doc.name, 'version_count', for_update=True)
	next_no = (latest.version_no or 0) + 1 if latest else 1

	version = frappe.new_doc('Scraper Brand Version')
	version.scraper_brand = brand_doc.name
	version.version_no = next_no
	version.parent_version = latest.name if latest else None
	version.source = source
	version.actor = actor or frappe.session.user
	version.content_hash = content_hash
	version.change_summary = summarize_changes(latest, prototype, files, code)
	version.prototype_json = prototype
	version.html_files = frappe.as_json(files)
	version.scraper_code = code
	version.insert(ignore_permissions=True)

	brand_doc.current_version = version.name
	brand_doc.version_count = next_no
	brand_doc.last_engine_finished_at = frappe.utils.now()
	if brand_doc.status in RESUMABLE_STATUSES:
		brand_doc.status = 'Prototype Ready'
		brand_doc.last_engine_error = None
	brand_doc.save(ignore_permissions=True)

	return version, True


def get_latest_version(brand_name):
	"""Newest version of a brand as a dict (name, version_no, content_hash), or None."""
	rows = frappe.get_all(
		'Scraper Brand Version',
		filters={'scraper_brand': brand_name},
		fields=['name', 'version_no', 'content_hash'],
		order_by='version_no desc, creation desc',
		limit=1,
	)
	return rows[0] if rows else None


def normalize_prototype(prototype_json):
	"""Store the prototype as a JSON string, whatever shape the caller sent."""
	if prototype_json in (None, ''):
		return None
	if isinstance(prototype_json, str):
		parsed = _try_parse(prototype_json)
		return frappe.as_json(parsed) if parsed is not None else prototype_json
	return frappe.as_json(prototype_json)


def normalize_html_files(html_files):
	"""Coerce the engine's HTML payload into a list of {file_name, content}.

	Accepts the three shapes the frontend might reasonably send -- a list of
	objects, a ``{filename: content}`` mapping, or a JSON string of either --
	and always returns a list, so downstream code (hashing, diffing, the GET
	response) only ever deals with one shape. Caller order is preserved; only
	the hash sorts, so re-ordering the same files is not treated as a change.
	"""
	if html_files in (None, ''):
		return []

	if isinstance(html_files, str):
		html_files = _try_parse(html_files)
		if html_files is None:
			return []

	if isinstance(html_files, dict):
		return [
			{'file_name': str(name), 'content': content if content is not None else ''}
			for name, content in html_files.items()
		]

	if not isinstance(html_files, (list, tuple)):
		return []

	files = []
	for idx, row in enumerate(html_files):
		if not isinstance(row, dict):
			# A bare string in the list is a file body with no name of its own.
			files.append({'file_name': f'file_{idx + 1}.html', 'content': str(row or '')})
			continue
		name = row.get('file_name') or row.get('filename') or row.get('name') or row.get('path')
		content = row.get('content')
		if content is None:
			content = row.get('html') or row.get('code') or ''
		files.append({'file_name': str(name or f'file_{idx + 1}.html'), 'content': content})

	return files


def normalize_code(scraper_code):
	"""The scraping code as plain text (empty string when absent)."""
	if scraper_code in (None, ''):
		return ''
	return scraper_code if isinstance(scraper_code, str) else str(scraper_code)


def compute_content_hash(prototype, files, code):
	"""sha256 over the whole triple, order-independent across the HTML files."""
	payload = {
		'prototype': prototype or '',
		'html': sorted(
			((f.get('file_name') or '', f.get('content') or '') for f in files),
			key=lambda pair: pair[0],
		),
		'code': code or '',
	}
	blob = json.dumps(payload, sort_keys=True, separators=(',', ':'), default=str)
	return hashlib.sha256(blob.encode('utf-8')).hexdigest()


def summarize_changes(latest, prototype, files, code):
	"""Human summary of what moved since the previous version."""
	if not latest:
		return f'Initial version ({len(files)} html file(s))'

	previous = frappe.get_doc('Scraper Brand Version', latest.name)
	parts = []

	if (previous.prototype_json or '') != (prototype or ''):
		parts.append('prototype')

	if (previous.scraper_code or '') != (code or ''):
		parts.append('code')

	old_files = {f.get('file_name'): f.get('content') for f in normalize_html_files(previous.html_files)}
	new_files = {f.get('file_name'): f.get('content') for f in files}

	added = len(set(new_files) - set(old_files))
	removed = len(set(old_files) - set(new_files))
	edited = sum(1 for k in set(old_files) & set(new_files) if old_files[k] != new_files[k])

	for count, label in ((added, 'added'), (removed, 'removed'), (edited, 'changed')):
		if count:
			parts.append(f'{count} html {label}')

	return ', '.join(parts) if parts else 'No content change'


def _try_parse(raw):
	try:
		return json.loads(raw)
	except (ValueError, TypeError):
		return None
