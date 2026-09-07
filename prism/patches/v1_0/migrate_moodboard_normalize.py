'''
One-time patch: normalize existing Moodboard draft_layout_json blobs into the new
columns / JSON fields / child tables / standalone doctypes (Version, Message,
Style, Fabric, Garment) + Brand Moodboard links.

Runs in [post_model_sync] (after the new doctypes/columns exist). Idempotent and
non-destructive — the old draft_layout_json is left intact as a rollback source,
and each board's child rows are deleted+recreated via raw SQL (no background-job
enqueue), so it's safe inside bench migrate. Per-board failures are logged, not
fatal, so a single bad board never aborts the migration.
'''

import frappe

import prism.lib.moodboard_migrate as mm


def execute():
    summary = mm.run()
    frappe.logger('prism').info(f'[moodboard normalize] {summary}')
    print(f'[moodboard normalize] {summary}')
