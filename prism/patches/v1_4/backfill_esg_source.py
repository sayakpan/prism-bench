'''
v1_4 patch: stamp the polymorphic source fields on existing Moodboard ESG rows.

Moodboard ESG gained `source_doctype` / `source_name` so it can belong to a
Sample Request as well as a Moodboard Style (see lib/esg.py). Every pre-existing
ESG was sourced from a Moodboard Style, so backfill those two fields from the old
`moodboard_style` link — otherwise the create-once guard (which now looks up by
source) wouldn't see them and could build duplicates.

Runs post_model_sync (after the new columns are created). Idempotent: only fills
rows whose source_name is still blank.
'''

import frappe

STYLE_DOCTYPE = 'Moodboard Style'


def execute():
    frappe.db.sql(
        '''
        UPDATE `tabMoodboard ESG`
        SET source_doctype = %s, source_name = moodboard_style
        WHERE (source_name IS NULL OR source_name = '')
          AND moodboard_style IS NOT NULL AND moodboard_style != ''
        ''',
        (STYLE_DOCTYPE,),
    )
