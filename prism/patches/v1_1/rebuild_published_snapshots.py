'''
v1_1 patch: for Published moodboards, rebuild `published_snapshot` in the NEW model
shape (from the published_layout_json blob, so it's faithful to what was actually
published and never leaks unpublished draft edits) and backfill `last_published_at`
from the draft blob's createdAt.

Runs after v1_0 (migrate_moodboard_normalize). Idempotent, non-destructive, per-board
fault-isolated. v1_0 stays active — fresh environments need it to normalize first.
'''

import frappe

import prism.lib.moodboard_migrate as mm


def execute():
    summary = mm.run_published_snapshots()
    frappe.logger('prism').info(f'[published snapshots] {summary}')
    print(f'[published snapshots] {summary}')
