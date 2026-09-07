'''
v2_0 patch: rename Moodboard Surplus Recolour.`meta` to `render_meta`.

`meta` is one of frappe's RESERVED_KEYWORDS — BaseDocument.meta is a
cached_property, and every cached_property is reserved. A doctype field of that
name is broken in two ways at once:

  - set() and update() both skip reserved keys, so the value was silently
    discarded and never reached the column; and
  - init_valid_columns() writes missing valid columns straight into __dict__,
    bypassing that guard, so it overwrote the cached Meta object with None.

The second one is what made this urgent: with `meta` resolving to None instead of
a Meta, _validate_links crashed on `self.meta.is_submittable`, so EVERY save of a
Moodboard carrying a recolour row died — sync_surplus_recolours could never
persist anything. (Which is also why no data can be at risk here: the column was
unwritable by construction. The copy below is defensive only.)

Runs pre_model_sync so the column is renamed before the schema sync reads the
updated doctype: the sync then finds `render_meta` already present and adds
nothing. Idempotent — a site that has already been migrated returns immediately.
'''

import frappe

DOCTYPE = 'Moodboard Surplus Recolour'
OLD, NEW = 'meta', 'render_meta'


def execute():
    if not frappe.db.table_exists(DOCTYPE):
        return

    columns = frappe.db.get_table_columns(DOCTYPE)
    if OLD not in columns:
        return                      # already renamed, or never existed

    if NEW not in columns:
        frappe.db.rename_column(DOCTYPE, OLD, NEW)
        frappe.db.commit()
        return

    # Both columns present (a post_model_sync run, or a half-applied migration):
    # carry anything the old column somehow holds into the new one, then drop it.
    frappe.db.sql(f'''
        UPDATE `tab{DOCTYPE}`
           SET `{NEW}` = `{OLD}`
         WHERE (`{NEW}` IS NULL OR `{NEW}` = '')
           AND `{OLD}` IS NOT NULL AND `{OLD}` != ''
    ''')
    frappe.db.sql_ddl(f'ALTER TABLE `tab{DOCTYPE}` DROP COLUMN `{OLD}`')
    frappe.db.commit()
