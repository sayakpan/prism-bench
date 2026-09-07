'''
v2_1 patch: make Moodboard Surplus Fabric batch-centric.

Identity moves from `fab_code` to (`fab_code`, `batch`): a fab code stocked in
five of a brief's palette colours is now five selectable rows, one per dye lot,
because the board shows a tile per lot. The old model could carry exactly one
batch per fab code -- whichever was the hero -- so everything else the user might
have wanted was unreachable.

That also retires the hero: `hero_batch` and set_surplus_fabric_hero_batch only
existed to decide which single lot represented a fab code, and a row that IS a
lot has nothing left to decide. The pin, where one was set, becomes the row's
batch -- it was the user's explicit choice of lot, which is exactly what the new
column means.

Rows with no batch at all are removed. Under the new identity they cannot be
addressed, selected or rendered, and they predate any batch ever being recorded.

Runs pre_model_sync, while `hero_batch` is still in the schema to be read from.
Idempotent: a site already migrated has no `hero_batch` column and returns.
'''

import frappe

DOCTYPE = 'Moodboard Surplus Fabric'


def execute():
    if not frappe.db.table_exists(DOCTYPE):
        return

    columns = frappe.db.get_table_columns(DOCTYPE)
    if 'hero_batch' not in columns:
        return

    # A pin was a human choosing this lot over the recommender's. Under the new
    # identity that is simply the row's batch, so promote it before dropping.
    frappe.db.sql(f'''
        UPDATE `tab{DOCTYPE}`
           SET `batch` = `hero_batch`
         WHERE `hero_batch` IS NOT NULL AND `hero_batch` != ''
    ''')
    frappe.db.sql_ddl(f'ALTER TABLE `tab{DOCTYPE}` DROP COLUMN `hero_batch`')

    orphans = frappe.db.sql(f'''
        SELECT COUNT(*) FROM `tab{DOCTYPE}`
         WHERE `batch` IS NULL OR `batch` = ''
    ''')[0][0]
    if orphans:
        frappe.db.sql(f'''
            DELETE FROM `tab{DOCTYPE}` WHERE `batch` IS NULL OR `batch` = ''
        ''')
        frappe.log_error(
            f'Dropped {orphans} {DOCTYPE} row(s) with no batch: unaddressable '
            'once identity is (fab_code, batch).',
            'v2_1.surplus_fabric_batch_centric')

    frappe.db.commit()
