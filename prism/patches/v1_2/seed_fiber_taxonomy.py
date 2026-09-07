'''
v1_2 patch: seed the Fiber Category / Fiber Type taxonomy used by the
Moodboard Dyed Fabric "Fiber Types" table.

Idempotent: re-running only inserts what is missing (uses the unique
category_name / type_name as the docname). Editing the TAXONOMY dict and
re-migrating adds new rows without touching or duplicating existing ones.
'''

import frappe

# category -> ordered list of fiber types (spelling kept as per source sheet)
TAXONOMY = {
    "Cotton": [
        "BCI Cotton",
        "Transitional Cotton",
        "Fair Trade Org Cotton",
        "Organic Cotton",
        "Recycle Cotton",
        "Regenagri Cotton",
        "Vasudha Primo Cotton",
        "Supima Cotton",
        "ROC Cotton",
    ],
    "Lyocell": [
        "Excel",
        "Tencel",
    ],
    "Modal": [
        "Micro Modal",
        "Modal",
    ],
    "Polyester": [
        "Celliant",
        "Coolmax",
        "Coolplus",
        "Recycle Polyster",
    ],
    "Spandex": [
        "Black Elastane",
        "Lycra",
        "Recycle Spandex",
        "Roica",
    ],
    "Viscose": [
        "Ecovero",
        "Liva Eco",
        "Viloft",
    ],
}


def execute():
    created_cat = created_type = 0

    # Running counter across the whole taxonomy: gives both the within-category
    # order and the category order (via first-seen) the APIs render by.
    sort_order = 0

    for category, types in TAXONOMY.items():
        if not frappe.db.exists("Fiber Category", category):
            frappe.get_doc(
                {"doctype": "Fiber Category", "category_name": category}
            ).insert(ignore_permissions=True)
            created_cat += 1

        for type_name in types:
            sort_order += 1
            if not frappe.db.exists("Fiber Type", type_name):
                frappe.get_doc(
                    {
                        "doctype": "Fiber Type",
                        "type_name": type_name,
                        "category": category,
                        "sort_order": sort_order,
                    }
                ).insert(ignore_permissions=True)
                created_type += 1

    frappe.db.commit()
    summary = f"{created_cat} categories, {created_type} fiber types inserted"
    frappe.logger("prism").info(f"[seed fiber taxonomy] {summary}")
    print(f"[seed fiber taxonomy] {summary}")
