'''
v2_4 patch: settle `in_moodboard` on every Moodboard Style that predates it.

The new column is `NOT NULL DEFAULT 1`, so MariaDB fills every existing row with
1 when it is added — which is exactly what we want for the approved ones. Default
on is the whole point: an approval puts a style in the moodboard, and trimming
the set is an explicit deselection, so every board written before this field
existed keeps behaving precisely as it does today.

What the column default gets wrong is the rest of the table. A Pending or
Rejected style would also land on 1, and `in_moodboard = 1` on a style nobody
approved is a state the rules do not allow — the controller clears it, but only
on that row's next save, which for a rejected style may never come. Until then a
client reading the row sees a rejected garment flagged for the board.

So the only rows touched here are the impossible ones:

    include = 1  ->  left at 1   (approved, in the board, as it is now)
    include = 0  ->  set to 0    (never approved, so never in the board)

Keyed on `include` rather than `decision` because `include` is what the published
set actually filters on today (moodboard_v2._brand_styles), so "keeps behaving
exactly as it does now" is literally true of the rows this leaves alone. The two
are normally reconciled, but not on every legacy row — some carry include = 1
under a Pending verdict, from bulk paths that wrote the flag without going
through the controller. Keying on `decision` would take every one of those out of
the board on the day this ships, which is the regression the default is there to
prevent. They are reconciled the next time each row is saved, and `in_moodboard`
follows the same reconciliation.

Written as a bulk UPDATE rather than doc.save(): the save hooks would flip every
published board to "Unpublished Changes", re-parse BOMs and rebuild ESG across
the whole table. Runs post_model_sync, after the new column exists.

Safe to re-run. It cannot undo a deselection: a style someone took out of the
board is include = 1, in_moodboard = 0, and the WHERE clause only ever looks at
rows sitting at 1 with no approval behind them.
'''

import frappe

DOCTYPE = 'Moodboard Style'


def execute():
    if not frappe.db.table_exists(DOCTYPE):
        return
    if not frappe.db.has_column(DOCTYPE, 'in_moodboard'):
        return

    frappe.db.sql(
        '''
        UPDATE `tabMoodboard Style`
        SET in_moodboard = 0
        WHERE in_moodboard = 1 AND (include = 0 OR include IS NULL)
        '''
    )
    frappe.db.commit()

    summary = frappe.db.sql(
        '''
        SELECT include, in_moodboard, COUNT(*)
        FROM `tabMoodboard Style`
        GROUP BY include, in_moodboard
        '''
    )
    frappe.logger('prism').info(f'[backfill style in_moodboard] {summary}')
    print(f'[backfill style in_moodboard] {summary}')
