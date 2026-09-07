'''
v1_6 patch: backfill the new `sort_order` field on existing Fiber Type masters.

Ordering is now defined once on the Fiber Type master (see `sort_order` field) and
the fabric/garment APIs render fiber types by it. Fresh installs get the values from
seed_fiber_taxonomy; this patch assigns the same running order to Fiber Types that
were seeded before the field existed.

Idempotent: only writes rows whose `sort_order` is still 0/unset, so re-running (or
running after someone has hand-tuned the values) leaves existing order untouched.
Fiber Type is hash-named, so rows are matched by `type_name`, not docname.
'''

import frappe

from prism.patches.v1_2.seed_fiber_taxonomy import TAXONOMY


def execute():
    updated = 0

    # Running counter across the whole taxonomy — mirrors seed_fiber_taxonomy so
    # existing and freshly-seeded installs end up with identical ordering.
    sort_order = 0
    for types in TAXONOMY.values():
        for type_name in types:
            sort_order += 1
            names = frappe.get_all(
                "Fiber Type",
                filters={"type_name": type_name, "sort_order": 0},
                pluck="name",
            )
            for name in names:
                frappe.db.set_value(
                    "Fiber Type", name, "sort_order", sort_order,
                    update_modified=False,
                )
                updated += 1

    frappe.db.commit()
    summary = f"{updated} fiber types backfilled with sort_order"
    frappe.logger("prism").info(f"[backfill fiber type sort_order] {summary}")
    print(f"[backfill fiber type sort_order] {summary}")
