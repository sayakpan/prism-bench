'''
v1_9 patch: give every existing Moodboard Style a decision and a render status.

`include` is now derived from `decision` (Approved sets it, Rejected and Pending
clear it). Existing rows have no decision, so without this they would all read as
Pending on their next save — and Pending clears `include`, which would quietly
drop every already-included style out of costing, ESG, the techpack and the
published set.

The backfill is therefore the identity mapping in the other direction:

    include = 1  ->  Approved   (stays included)
    include = 0  ->  Pending    (stays excluded, and is honestly "not ruled on"
                                rather than "rejected" — nobody wrote a reason,
                                and Rejected demands one)

`image_status` gets the same treatment: a row with an image is `ready`, a row
without one is `pending`. Nothing is marked `failed` — no failure was recorded
before this feature existed, and inventing one would put a retry button on rows
that never had a render attempted.

Written as a bulk UPDATE rather than doc.save(): the save hooks would flip every
published board to "Unpublished Changes", re-parse BOMs and rebuild ESG across
the whole table. Runs post_model_sync, after the new columns exist. Idempotent —
only rows still missing a value are touched.
'''

import frappe

DOCTYPE = 'Moodboard Style'


def execute():
    if not frappe.db.table_exists(DOCTYPE):
        return

    frappe.db.sql(
        '''
        UPDATE `tabMoodboard Style`
        SET decision = CASE WHEN include = 1 THEN 'Approved' ELSE 'Pending' END
        WHERE decision IS NULL OR decision = ''
        '''
    )

    frappe.db.sql(
        '''
        UPDATE `tabMoodboard Style`
        SET image_status = CASE
                WHEN image IS NOT NULL AND image != '' THEN 'ready'
                ELSE 'pending'
            END
        WHERE image_status IS NULL OR image_status = ''
        '''
    )

    frappe.db.commit()

    summary = frappe.db.sql(
        '''
        SELECT decision, COUNT(*) FROM `tabMoodboard Style` GROUP BY decision
        '''
    )
    frappe.logger('prism').info(f'[backfill style decision] {summary}')
    print(f'[backfill style decision] {summary}')
