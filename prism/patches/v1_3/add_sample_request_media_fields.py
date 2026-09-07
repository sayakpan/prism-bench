'''
v1_3 patch: attach the Sample Request media child tables (garment images +
product videos) to the Sample Request master.

Sample Request is a Desk-managed master doctype (not defined in this app), so it
is extended via Custom Fields -- mirroring how the fiber-types table was added
(see api/garment.py). The child doctypes themselves, "Sample Request Garment
Image" and "Sample Request Product Video", ARE app-defined and are created by the
normal doctype sync before this (post_model_sync) patch runs.

Idempotent: create_custom_fields updates an existing field in place instead of
duplicating it, so re-migrating is safe.
'''

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

CUSTOM_FIELDS = {
    "Sample Request": [
        {
            "fieldname": "media_section",
            "label": "Media",
            "fieldtype": "Section Break",
        },
        {
            "fieldname": "garment_images",
            "label": "Garment Images",
            "fieldtype": "Table",
            "options": "Sample Request Garment Image",
            "insert_after": "media_section",
            "description": "Per-garment image gallery. One child row per image.",
        },
        {
            "fieldname": "product_video",
            "label": "Product Video",
            "fieldtype": "Table",
            "options": "Sample Request Product Video",
            "insert_after": "garment_images",
            "description": "Per-garment product video(s). One child row per video.",
        },
    ]
}


def execute():
    create_custom_fields(CUSTOM_FIELDS, ignore_validate=True)
