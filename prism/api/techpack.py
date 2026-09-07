import base64
import io
import json
import math

import frappe
import requests
from frappe.utils import cint, formatdate, nowdate

import openpyxl
from openpyxl.drawing.image import Image as XLImage
from openpyxl.utils.cell import coordinate_to_tuple
from PIL import Image as PILImage

from prism.auth.authenticator import auth_required
import prism.api.llm as llm
import prism.api.costing as costing
import prism.api.util as util

MAX_PDF_FILE_SIZE_MB = 16
DEFAULT_ORDER_UNITS = 1500

# External market-pricing service: returns benchmark FOB / retail prices for a
# given brand + garment, used to populate the Techpack Costing doc.
BOT_API_BASEPATH = frappe.get_site_config().get('bot_api_basepath', 'https://prismbotdev.pratibhasyntex.com')

# --- Tech-pack Excel generation -------------------------------------------
# blank template shipped with the app (apps/prism/prism/templates/excel/...).
TECHPACK_TEMPLATE_PATH = ('prism', 'templates', 'excel', 'tech_pack_template.xlsx')

# sheet that carries the full first-page header + the product image.
INDEXING_SHEET = 'Indexing-1'
PRODUCT_IMAGE_ANCHOR = 'C12'                # top-left anchor of the merged image box C12:G19
PRODUCT_IMAGE_BOX = (360, 300)              # max (width, height) in px; aspect ratio preserved
PRODUCT_IMAGE_MAX_BYTES = 10 * 1024 * 1024  # cap the product image at 10 MB

XLSX_CONTENT_TYPE = 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'


def _recurring_header(left_col: str, right_col: str) -> dict:
    '''
    The compact header repeated on pages 2-9. Each sheet uses the same four
    left-hand and four right-hand fields, but the value columns differ per sheet
    (the labels are merged across a varying number of columns), so both the left
    and right value columns are passed in explicitly.
    '''
    return {
        'buyer': f'{left_col}1',
        'season_year': f'{left_col}2',
        'main_fabric': f'{left_col}3',
        'prb_designer': f'{left_col}4',
        'development_style_no': f'{right_col}1',
        'bulk_style_no': f'{right_col}2',
        'style_description': f'{right_col}3',
        'tech_pack_date': f'{right_col}4',
    }

# Fixed cell map per (visible) page. Field keys are the canonical payload keys
# resolved in _normalize_payload(). Only cells that actually exist on a given
# sheet's header are listed — e.g. the recurring pages have no Season /
# Collection / Gender / Category / Main Fabric Code / Product Image cells.
SHEET_CELL_MAP = {
    # Page 1 — full header.
    'Indexing-1': {
        'buyer': 'B1',
        'season_year': 'B2',
        'season': 'B3',
        'collection': 'B4',
        'gender': 'B5',
        'category': 'B6',
        'development_style_no': 'E1',
        'bulk_style_no': 'E2',
        'style_description': 'E3',
        'tech_pack_date': 'E4',
        'main_fabric_code': 'E7',
        'main_fabric': 'E8',
        'prb_designer': 'E9',
    },
    # Pages 2-9 — recurring compact header (left + right value columns differ per sheet).
    'Construction Page-2': _recurring_header('B', 'E'),
    'Measurement Chart-3': _recurring_header('C', 'I'),
    'How To Measure-4': _recurring_header('C', 'I'),
    'OB-5': _recurring_header('D', 'J'),
    'Colorways-6': _recurring_header('B', 'H'),
    'BOM & Placement-7': _recurring_header('B', 'G'),
    'Graphic-AOP-YD-8': _recurring_header('C', 'I'),
    'Comments-9.1': _recurring_header('C', 'I'),
    'Comments-9.2': _recurring_header('C', 'I'),
}


@frappe.whitelist(allow_guest=True)
def calculate_cost(techpack_costing_name: str):
    '''
    Background worker (enqueued from TechpackCosting.after_insert): runs
    _extract_from_techpack() against the doc's tech_pack PDF and stores the
    resulting fabric list in the doc's `fabrics` JSON field.
    '''
    try:
        file_url = frappe.db.get_value('Techpack Costing', techpack_costing_name, 'tech_pack')
        if not file_url:
            return

        #--- extract fabrics ---
        result = _extract_from_techpack(file_url=file_url)
        if not result.get('success'):
            frappe.log_error(
                f'_extract_from_techpack() failed for {techpack_costing_name}: {result.get("error")}',
                'techpack.calculate_cost()',
            )
            return

        garment_style = result.get('garment_style', '<unknown>')
        garment_gender = result.get('garment_gender', '<unknown>')
        garment_brand = result.get('garment_brand', '<unknown>')
        fabrics = result.get('fabrics', [])
        frappe.db.set_value(
            'Techpack Costing',
            techpack_costing_name,
            {
                'garment_style': garment_style,
                'garment_gender': garment_gender,
                'garment_brand': garment_brand,
                'fabrics': json.dumps(fabrics),
            }
        )
        frappe.db.commit()

        #--- calculate fabric cost ---
        costing_result = []
        for fabric in fabrics:
            cost = costing.calculate(
                constructions=fabric.get('construction', []),
                blends=fabric.get('blend', []),
                gsm=fabric.get('gsm'),
            )
            costing_result.append({
                'section': fabric.get('section'),
                'costing': cost.get('data')
            })

        frappe.db.set_value(
            'Techpack Costing',
            techpack_costing_name,
            'costing',
            json.dumps(costing_result),
        )
        frappe.db.commit()


        #--- calculate print cost ---
        print_parts  = _extract_print_info(techpack_costing_name)
        if print_parts:
            order_quantity = frappe.db.get_value('Techpack Costing', techpack_costing_name, 'order_quantity')
            print_cost = _get_print_cost(print_parts, order_quantity)
            if print_cost:
                frappe.db.set_value(
                    'Techpack Costing',
                    techpack_costing_name,
                    'print_costing',
                    json.dumps(print_cost),
                )
                frappe.db.commit()


        #--- get and store the Market FOB and Retail prices ---
        market_fob_and_retail = _get_market_fob_and_retail(
            brand=garment_brand,
            gender=garment_gender,
            category=garment_style,
            costing_result=costing_result,
        )
        if market_fob_and_retail is not None:
            frappe.db.set_value(
                'Techpack Costing',
                techpack_costing_name,
                'market_fob_and_retail',
                json.dumps(market_fob_and_retail),
            )
            frappe.db.commit()

        #--- capture trim costing ---
        trim_costing = costing.get_trim_cost(garment_style)
        frappe.db.set_value(
            'Techpack Costing',
            techpack_costing_name,
            'trim_costing',
            json.dumps(trim_costing),
        )
        frappe.db.commit()

    except Exception:
        frappe.db.rollback()
        frappe.log_error(frappe.get_traceback(), 'techpack.calculate_cost()')

def _get_market_fob_and_retail(brand: str, gender: str, category: str, costing_result: list):
    '''
    Query the external market-pricing service for benchmark FOB / retail prices
    for the given garment, and return them as a dict:
        {'market_fob': <float>, 'retail': <float>}

    `composition` is taken from the matched fabric's blend in the costing of the
    first (main) section. Returns None on any failure so the caller can skip
    storing the field.
    '''
    try:
        # composition = the blend of the matched fabric in the first/main section's costing.
        composition = ''
        if costing_result:
            composition = (costing_result[0].get('costing') or {}).get('blend', '')

        payload = {
            'brand': brand,
            'gender': gender,
            'category': category,
            'construction': 'Knitted',  # hard-coded per spec
            'composition': composition,
            'exclude_sets_packs': True,
        }
        headers = {
            'Accept': 'application/json',
            'Content-Type': 'application/json',
        }

        api_url = f'{BOT_API_BASEPATH}/external-cost/generate-quote'
        res = requests.post(api_url, json=payload, headers=headers, timeout=30)
        res.raise_for_status()
        stats = res.json().get('stats', {})

        return {
            'market_fob': stats.get('nitva_unit_rate_usd', {}).get('avg'),
            'retail': stats.get('brand_price', {}).get('avg'),
        }
    except Exception:
        frappe.log_error(frappe.get_traceback(), 'techpack._get_market_fob_and_retail()')
        return None

@frappe.whitelist(allow_guest=True, methods=['POST'])
@auth_required
def download_moodboard_excel(payload=None):
    '''
    Populates a fresh copy of the fixed tech-pack Excel template with the given
    payload and streams it back as an .xlsx download.

    The template is never mutated on disk: a new in-memory workbook is loaded on
    every call, so the response is regenerated each time and is concurrency-safe.

    Expected payload (JSON object):
    {
        "buyer": {
            "id": "wearpact",
            "name": "Wearpact"
        },
        "seasonYear": "2027",
        "season": "SS",
        "collectionTheme": "Strong Summer Vibes",
        "gender": [
            "Women"
        ],
        "category": "Bottoms",
        "prbDesigner": {
            "id": "sayak.pan@webspiders.com",
            "email": "sayak.pan@webspiders.com",
            "name": "Sayak Pan"
        },
        "mainFabric": [
            {
                "section": "Main Body",
                "fabricId": "3fqhji24d9",
                "code": "1800007361-A0000-1600006983-O-183-NO30",
                "name": "SPD SJY 100 BCI CC COMPACT 160 BRIDAL RO",
                "construction": "SJY COMPACT",
                "blend": "100% BCI COTTON",
                "gsm": 160,
                "finish": "ENZYME + SILICON SOFT",
                "consumption": 0.22,
                "print_type": "",
                "adjustment_percent": 0,
                "adjusted_heads": {},
                "head_remarks": {}
            }
        ],
        "productImage": "/files/doc_1808cdb4.png"
    }
    Note: the recurring pages (2-9) carry a compact header, so season /
    collection / gender / category / main_fabric_code have no cell there and are
    only written on the first (Indexing-1) page.
    '''
    try:
        # The fields are posted as a flat JSON body, so Frappe leaves the
        # `payload` arg as None — read the request body directly instead.
        if payload is None:
            payload = json.loads(frappe.request.data or '{}')
        elif isinstance(payload, str):
            payload = json.loads(payload)
        if not isinstance(payload, dict):
            frappe.throw('payload must be a JSON object')

        raw_data = {
            'buyer': payload.get('buyer', {}).get('name'),
            'season_year': payload.get('seasonYear'),
            'season': payload.get('season'),
            'collection': payload.get('collectionTheme'),
            'tech_pack_date': None,  # always stamped with today's date in _coerce()
            'gender': ', '.join(payload.get('gender', '')),
            'category': payload.get('category'),
            'prb_designer': payload.get('prbDesigner', {}).get('name'),
            'main_fabric': payload['mainFabric'][0].get('name') if payload['mainFabric'] else None,
            'main_fabric_code': payload['mainFabric'][0].get('code') if payload['mainFabric'] else None,
            'development_style_no': payload.get('development_style_no'),
            'bulk_style_no': payload.get('bulk_style_no'),
            'style_description': payload.get('style_description'),
            'product_image': payload.get('productImage'),
        }
        data = _normalize_payload(raw_data)
        image_path = data.pop('product_image', None)

        template_path = frappe.get_app_path(*TECHPACK_TEMPLATE_PATH)
        wb = openpyxl.load_workbook(template_path)

        for sheet_name, cell_map in SHEET_CELL_MAP.items():
            if sheet_name not in wb.sheetnames:
                continue
            ws = wb[sheet_name]
            for field, cell in cell_map.items():
                _set_cell(ws, cell, _coerce(field, data.get(field)))

        # Product image lives only on the first page; pages 2-9 stay blank.
        if image_path and INDEXING_SHEET in wb.sheetnames:
            _embed_image(wb[INDEXING_SHEET], image_path)

        buf = io.BytesIO()
        wb.save(buf)

        base_name = data.get('development_style_no') or data.get('style_description') or 'techpack'
        frappe.local.response.filename = f'{_safe_filename(base_name)}.xlsx'
        frappe.local.response.filecontent = buf.getvalue()
        frappe.local.response.type = 'download'
        frappe.local.response.content_type = XLSX_CONTENT_TYPE
    except Exception:
        frappe.log_error(frappe.get_traceback(), 'techpack.download_moodboard_excel()')
        raise


# --- helpers ---
def _extract_from_techpack(file_url: str):
    '''
    Takes a tech-pack pdf doc uploaded by the user and extracts (using claude 
    llm) all the fabrics required along with their construction, blend and gsm.
    Returns the result in a specified json format.
    '''

    try:
        # locate and load the uploaded pdf
        if not frappe.db.exists('File', {'file_url': file_url}):
            frappe.log_error(
                f'No file found for url: {file_url}', 
                'techpack._extract_from_techpack()'
            )
            return

        file_doc = frappe.get_doc('File', {'file_url': file_url})
        pdf_bytes = file_doc.get_content()

        max_size_bytes = MAX_PDF_FILE_SIZE_MB * 1024 * 1024
        if len(pdf_bytes) > max_size_bytes:
            frappe.log_error(
                f'PDF size exceeds the maximum allowed limit of {MAX_PDF_FILE_SIZE_MB} MB.', 
                'techpack._extract_from_techpack()'
            )
            return

        pdf_b64 = base64.standard_b64encode(pdf_bytes).decode('utf-8')

        system_prompt = f'''You are a fabric-procurement assistant. Given a garment tech-pack PDF, your job is to identify four things:

1. The overall garment style (one value, chosen from the controlled vocabulary below).
2. The garment gender (one value: Men, Women, Kids, Children or Unisex — the intended wearer of the garment).
3. The garment brand (the buyer / brand / label the tech-pack is produced for).
4. The fabric required for each distinct section of the garment (e.g., Main Body, Neck, Sleeve, Cuff, Pocket, Lining, Placket, Trim, etc.). For every section, return 2-3 closest-matching values for construction (quality) and blend (most relevant first), plus a single GSM value.

# OUTPUT FORMAT
Return a single raw JSON object in the exact shape below. The "garment_style" key holds the chosen garment style. The "garment_gender" key holds the intended wearer. The "garment_brand" key holds the brand. The "fabrics" key holds an array, one entry per distinct fabric section:
{{
    "garment_style": "Dress",
    "garment_gender": "Women",
    "garment_brand": "Wearpact",
    "fabrics": [
        {{
            "section": "Main Body",
            "construction": ["SJY_COMPACT", "SJY_REGULAR"],
            "blend": ["60.00 :40.00 FTO:RP", "70.00 :30.00 FTO:RP"],
            "gsm": 180
        }}
    ]
}}

# CONTROLLED VOCABULARY

Allowed garment_style values:
---
{_get_garment_styles()}

Allowed Construction (quality) values:
---
{_get_fabric_attributes('fabric')}

Allowed Blend values:
---
{_get_fabric_attributes('blend')}

# INSTRUCTIONS
- garment_style: pick exactly one value from the allowed garment_style list — the single closest match to the tech-pack's overall garment.
- garment_gender: pick exactly one value from Men, Women, Kids, Children or Unisex — the intended wearer of the garment. Infer from the tech-pack's fit, sizing, styling, and any explicit gender/age cues. If it cannot be reasonably determined, return "Unisex".
- garment_brand: the buyer / brand / label the tech-pack is produced for, taken from explicit cues in the document (buyer field, brand name, logo, header/footer). Return the brand name verbatim as written. If it cannot be reasonably determined, return an empty string ("").
- fabrics: one entry per distinct garment section called out in the tech-pack. Do not merge sections, do not invent sections that aren't referenced.
- construction and blend values MUST come strictly from the allowed lists above. Never output a value that is not present in those lists verbatim.
- For each of construction and blend, return a list of 2-3 closest matches, ordered from most to least relevant. If a value cannot be reasonably inferred, return an empty list ([]) for that field.
- gsm: a single integer. If not explicitly stated in the tech-pack, infer the most likely value from context (garment type, fabric description, season, weight cues, etc.).
- If construction, blend, or GSM are not explicitly specified in the tech-pack, infer the most likely values from the inventory based on the garment description, specifications, and any other contextual cues in the document — do not leave them blank just because they are absent.
- Output: return ONLY the raw JSON object. No markdown fences, no prose, no commentary, no trailing text.
'''

        user_prompt = (
            'Extract the garment style, garment gender, garment brand, and per-section '
            'fabric requirements from the attached tech-pack PDF. Return a single raw '
            'JSON object in the exact format specified, using only values from the '
            'allowed vocabulary lists.'
        )

        # llm call
        res = llm.extract_from_doc(system_prompt, user_prompt, pdf_b64, 'dict')

        return {
            'success': True,
            'garment_style': res.get('garment_style', '<unknown>'),
            'garment_gender': res.get('garment_gender', '<unknown>'),
            'garment_brand': res.get('garment_brand', '<unknown>'),
            'fabrics': res.get('fabrics', [])
        }

    except Exception as ex:
        frappe.log_error(frappe.get_traceback(), 'techpack._extract_from_techpack()')
        return {'success': False, 'error': str(ex)}

def _extract_print_info(techpack_costing_id: str):
    '''
    Takes the artwork pdf attached to a "Techpack Costing" record and extracts
    (using claude llm) the details of every distinct print part on the garment.
    Returns a JSON array, one object per print part, in the shape:
        [{
            "print_position": "Chest",
            "print_type": ["Non-PVC", "Puff"],
            "number_of_prints": 3,
            "length_inches": 8.3,
            "width_inches": 6.5,
            "coverage_percent": 70
        }]
    The "print_type" of each part is mapped to the closest matching value from
    the controlled vocabulary returned by _get_print_options(). Returns an empty
    list on any failure.
    '''
    try:
        artwork_file_url = frappe.db.get_value('Techpack Costing', techpack_costing_id, 'artwork')
        if not artwork_file_url:
            frappe.log_error(
                f'No artwork attached for Techpack Costing: {techpack_costing_id}',
                'techpack._extract_print_info()',
            )
            return []

        # locate and load the uploaded artwork pdf
        if not frappe.db.exists('File', {'file_url': artwork_file_url}):
            frappe.log_error(
                f'No file found for url: {artwork_file_url}',
                'techpack._extract_print_info()',
            )
            return []

        file_doc = frappe.get_doc('File', {'file_url': artwork_file_url})
        pdf_bytes = file_doc.get_content()

        max_size_bytes = MAX_PDF_FILE_SIZE_MB * 1024 * 1024
        if len(pdf_bytes) > max_size_bytes:
            frappe.log_error(
                f'Artwork PDF size exceeds the maximum allowed limit of {MAX_PDF_FILE_SIZE_MB} MB.',
                'techpack._extract_print_info()',
            )
            return []

        pdf_b64 = base64.standard_b64encode(pdf_bytes).decode('utf-8')

        # controlled vocabulary of print types (one name per line)
        print_types = _get_print_types()
        print_types_list = '\n'.join(
            opt['print_type'] for opt in print_types if opt.get('print_type')
        )

        system_prompt = f'''You are a garment print-costing assistant. Given an artwork PDF for a garment, your job is to identify every distinct print part (print placement) on the garment and describe it.

# OUTPUT FORMAT
Return a single raw JSON array. Each element describes one distinct print part, in the exact shape below:
[
    {{
        "print_position": "Chest",
        "print_type": ["Non-PVC", "Puff"],
        "number_of_colors": 3,
        "length_inches": 8.3,
        "width_inches": 6.5,
        "coverage_percent": 70
    }}
]

# FIELDS
- print_position: where the print sits on the garment (e.g. Chest, Back, Left Sleeve, Right Sleeve, Front, Pocket, Hem, Collar, etc.). Return it verbatim as written in the artwork where possible.
- print_type: an array of the printing technique(s) used for this part, each a closest-matching value from the controlled vocabulary below. If the part uses a single print type, return an array with one element; if multiple print types are mentioned for the part, include one element per type.
- number_of_colors: integer count of the distinct print colours used for this part. If a colour legend/spec is stated for the print, use it; otherwise infer the count from the print image and relevant context.
- length_inches: the print's length (longer dimension) in inches, as a number.
- width_inches: the print's width (shorter dimension) in inches, as a number.
- coverage_percent: the percentage (0-100) of the print's bounding box that is actually covered by ink/design, as a number.

# DIMENSION INFERENCE
- If exactly ONE of length_inches or width_inches is given, infer the missing one from the aspect ratio of the relevant print image (measure the design's proportions and scale the known dimension accordingly). If the image's aspect ratio cannot be determined, fall back to the surrounding context.
- If BOTH length_inches and width_inches are missing, infer both from the relevant context (the print position, the garment type and size, the scale of the design, and any other cues in the artwork).

# CONTROLLED VOCABULARY

Allowed print_type values:
---
{print_types_list}

# INSTRUCTIONS
- Return one object per DISTINCT print part. Do not merge separate prints, do not invent prints that aren't shown in the artwork.
- Every value in the print_type array MUST come strictly from the allowed list above — never output a value that is not present in that list verbatim. Pick the closest match for each print type mentioned for the part, and never return an empty print_type array.
- For any numeric field not explicitly stated in the artwork, infer the most likely value from context (dimensions, scale callouts, the design itself). Use sensible defaults rather than leaving fields blank: number_of_colors defaults to 1, coverage_percent reflects how filled the design is.
- Output: return ONLY the raw JSON array. No markdown fences, no prose, no commentary, no trailing text. If the artwork contains no prints, return an empty array ([]).
'''

        user_prompt = (
            'Extract the details of every distinct print part from the attached '
            'artwork PDF. Return a single raw JSON array in the exact format '
            'specified, using only print_type values from the allowed vocabulary.'
        )

        # llm call
        res = llm.extract_from_doc(system_prompt, user_prompt, pdf_b64, 'list')
        if not isinstance(res, list):
            return []

        # Pick a single `final_print_type` per part: the print type with the
        # highest cost_per_inch. With one option that one is used; on ties the
        # first (highest-priority) option in the array wins. Carry that type's
        # cost_per_inch and manpower_cost onto the part too.
        opt_by_type = {
            opt['print_type']: opt
            for opt in print_types if opt.get('print_type')
        }
        for part in res:
            types = part.get('print_type') or []
            if isinstance(types, str):
                types = [types]
            final_type = max(
                types,
                key=lambda t: opt_by_type.get(t, {}).get('cost_per_inch') or 0,
                default='',
            )
            final_opt = opt_by_type.get(final_type, {})
            part['final_print_type'] = final_type
            part['cost_per_inch'] = final_opt.get('cost_per_inch')
            part['manpower_cost'] = final_opt.get('manpower_cost')

        return res

    except Exception:
        frappe.log_error(frappe.get_traceback(), 'techpack._extract_print_info()')
        return []

def _get_garment_styles():
    rows = frappe.get_all(
        'Trim Costing',
        fields=['style_name'],
        distinct=True,
        ignore_permissions=True
    )

    return '\n'.join([row.get('style_name' or '') for row in rows])

def _get_fabric_attributes(attribute_field_name: str):
    '''
    Builds the inventory's controlled vocabulary for a specific 
    attribute field on the "Fabric Master" doctype.
    Reads every distinct, non-null value of `attribute_field_name`
    (e.g. "quality", "blend", "finish") from the doctype and returns
    them as a newline-separated string of distinct values.
    '''

    rows = frappe.get_all(
        'Fabric Master',
        filters={attribute_field_name: ['is', 'set']},
        fields=[attribute_field_name],
        distinct=True,
    )

    return '\n'.join([row.get(attribute_field_name or '') for row in rows])
    #return rows

def _get_print_types():
    rows = None
    try:
        if frappe.db.exists('DocType', 'Print Type Master'):
            rows = frappe.get_all(
                'Print Type Master',
                fields=['print_type_name as print_type', 'cost_per_inch', 'manpower_cost'],
                order_by='print_type_name asc',
                ignore_permissions=True,
            )
    except Exception:
        rows = None

    if rows:
        return rows
    else:
        return [
            {'print_type': 'Non-PVC',       'cost_per_inch': 0.17, 'manpower_cost': 6.00},
            {'print_type': 'Pigment',       'cost_per_inch': 0.01, 'manpower_cost': 5.00},
            {'print_type': 'Gel',           'cost_per_inch': 0.50, 'manpower_cost': 6.00},
            {'print_type': 'Puff',          'cost_per_inch': 0.25, 'manpower_cost': 6.00},
            {'print_type': 'Metalic',       'cost_per_inch': 0.50, 'manpower_cost': 6.00},
            {'print_type': 'Pearlescent',   'cost_per_inch': 0.35, 'manpower_cost': 6.00},
            {'print_type': 'Suede',         'cost_per_inch': 0.50, 'manpower_cost': 6.00},
            {'print_type': 'Foil',          'cost_per_inch': 0.50, 'manpower_cost': 8.00},
            {'print_type': 'Discharge',     'cost_per_inch': 0.25, 'manpower_cost': 6.00},
            {'print_type': 'Glow in Dark',  'cost_per_inch': 1.50, 'manpower_cost': 7.00},
            {'print_type': 'Stiff Crackle', 'cost_per_inch': 0.50, 'manpower_cost': 7.00},
            {'print_type': 'Glitter',       'cost_per_inch': 0.50, 'manpower_cost': 6.00},
        ]

def _get_print_costing_globals():
    '''
    Reads the global print-costing buffers and defaults from the single
    "Print Costing Rules" doctype. Falls back to module-level defaults if
    the doc is missing or values are blank/invalid.
    '''
    extra = 6.0
    rejection = 12.0
    mesh_cost = 700
    coverage = 60
    order_qty = 1200

    try:
        values = frappe.db.get_singles_dict('Print Costing Rules') or {}
        extra = _to_float(values.get('default_extra_percentage')) or extra
        rejection = _to_float(values.get('default_rejection_percentage')) or rejection
        mesh_cost = _to_float(values.get('default_mesh_cost_per_screen')) or mesh_cost
        coverage = _to_float(values.get('default_coverage_percentage')) or coverage
        order_qty = _to_float(values.get('default_order_quantity')) or order_qty
    except Exception:
        pass

    return {
        'extra_percent': extra,
        'rejection_percent': rejection,
        'mesh_cost_per_screen': mesh_cost,
        'default_coverage_percent': coverage,
        'default_order_quantity': int(order_qty),
    }

def _to_float(value):
    try:
        if value in (None, ''):
            return 0.0
        return float(value)
    except Exception:
        return 0.0

def _get_print_cost(print_parts: list, order_quantity=None):
    '''
    Builds the print-costing JSON from the print parts extracted by
    _extract_print_info(), in the same shape the Techpack Costing workbench
    saves/renders (see techpack_costing._normalize_print_costing).

    Extra %, rejection %, mesh cost per screen and the default coverage % come
    from the global "Print Costing Rules" via _get_print_costing_globals().

    Order quantity is the doc's own `order_quantity` field, passed in by the
    caller. It is the source of truth; the global default is used only when the
    passed value is missing or invalid. The default coverage % is only applied
    to a part when its own coverage_percent is missing or null.

    Per-part inputs map from the extraction as:
        final_print_type -> print_type
        number_of_colors -> no_of_prints  (one screen per print colour)
        length_inches    -> length
        width_inches     -> width
        coverage_percent -> coverage
    '''
    rules = _get_print_costing_globals()
    extra_percent = rules['extra_percent']
    rejection_percent = rules['rejection_percent']
    mesh_cost_per_screen = rules['mesh_cost_per_screen']
    default_coverage = rules['default_coverage_percent']

    # Order quantity comes from the doc's `order_quantity` field; fall back to the
    # global default (then the module default) only when it's blank/invalid.
    order_quantity = int(_to_float(order_quantity)) or rules['default_order_quantity']
    if order_quantity < 1:
        order_quantity = DEFAULT_ORDER_UNITS

    # rate card keyed by print type, so cost/manpower stay sourced from the
    # master even if the extracted part values drift.
    options_by_type = {opt['print_type']: opt for opt in _get_print_types()}

    out_prints = []
    grand_total = 0
    for part in print_parts or []:
        if not isinstance(part, dict):
            continue

        pt = part.get('final_print_type') or ''
        rate = options_by_type.get(pt) or {}
        no_of_prints = _to_float(part.get('number_of_colors'))
        length = _to_float(part.get('length_inches'))
        width = _to_float(part.get('width_inches'))

        coverage_raw = part.get('coverage_percent')
        coverage = _to_float(coverage_raw) if coverage_raw not in (None, '') else default_coverage

        cost_per_inch = _to_float(rate.get('cost_per_inch'))
        manpower_cost = _to_float(rate.get('manpower_cost'))

        area = length * width
        ink_cost = cost_per_inch * area * (no_of_prints + 1) * (coverage / 100.0)
        mesh_cost_per_garment = (no_of_prints * mesh_cost_per_screen) / order_quantity if order_quantity else 0
        material_cost = (ink_cost + mesh_cost_per_garment) * (1 + extra_percent / 100.0)

        gross_total_cost = material_cost + manpower_cost

        rejection_amount = gross_total_cost * (rejection_percent / 100.0)
        final_cost = int(math.ceil(gross_total_cost + rejection_amount)) if pt else 0

        out_prints.append({
            'id': frappe.generate_hash(length=10),
            'print_position': part.get('print_position') or '',
            'print_type': pt,
            'no_of_prints': no_of_prints,
            'length': length,
            'width': width,
            'coverage': coverage,
            'cost_per_inch': cost_per_inch,
            'manpower_cost': manpower_cost,
            'area': round(area, 4),
            'ink_cost': round(ink_cost, 4),
            'mesh_cost_per_garment': round(mesh_cost_per_garment, 4),
            'material_cost': round(material_cost, 4),
            'gross_total_cost': round(gross_total_cost, 4),
            'rejection_amount': round(rejection_amount, 4),
            'final_cost': final_cost,
        })
        grand_total += final_cost

    return {
        'order_quantity': order_quantity,
        'mesh_cost_per_screen': mesh_cost_per_screen,
        'default_coverage_percent': default_coverage,
        'extra_percent': extra_percent,
        'rejection_percent': rejection_percent,
        'prints': out_prints,
        'total_print_cost': grand_total,
    }

# excel helpers
def _normalize_payload(payload):
    '''Accepts a dict, a JSON string, or falls back to the request form data.'''
    if payload is None:
        payload = {k: v for k, v in frappe.form_dict.items() if k != 'cmd'}
    if isinstance(payload, str):
        payload = json.loads(payload)
    if not isinstance(payload, dict):
        frappe.throw('payload must be a JSON object')
    return dict(payload)

def _coerce(field, value):
    '''
    Maps a payload value to what should be written into the cell. The tech-pack
    date is always stamped with the current (formatted) date, regardless of any
    value supplied in the payload. None becomes an empty string so the template's
    sample placeholders get cleared.
    '''
    if field == 'tech_pack_date':
        return formatdate(nowdate())
    if value in (None, ''):
        return ''
    return value

def _set_cell(ws, coord, value):
    '''Writes to `coord`, redirecting to the top-left anchor if it is inside a merged range.'''
    for rng in ws.merged_cells.ranges:
        if coord in rng:
            coord = rng.coord.split(':')[0]
            break
    ws[coord] = value

def _embed_image(ws, file_url):
    '''
    Reads a Frappe file image and anchors it (aspect-preserved) in the page-1
    image box. Fails soft: a missing/unreadable file is logged and skipped so the
    rest of the tech-pack still downloads.
    '''
    raw = _read_image_file(file_url)
    if not raw:
        return

    # The template ships a placeholder image at the same anchor — drop it so the
    # new product image doesn't stack on top of it.
    anchor_row, anchor_col = coordinate_to_tuple(PRODUCT_IMAGE_ANCHOR)  # 1-based (row, col)
    ws._images = [
        im for im in ws._images
        if not (getattr(im.anchor, '_from', None)
                and im.anchor._from.col == anchor_col - 1
                and im.anchor._from.row == anchor_row - 1)
    ]

    width, height = PILImage.open(io.BytesIO(raw)).size
    max_w, max_h = PRODUCT_IMAGE_BOX

    # Scale the longer dimension to fit the box (never upscale), then derive the
    # other dimension from the original aspect ratio so it is preserved exactly —
    # no independent rounding that could distort the image.
    scale = min(max_w / width, max_h / height, 1)
    scaled_w = round(width * scale)
    scaled_h = round(scaled_w * height / width)

    img = XLImage(io.BytesIO(raw))
    img.width = scaled_w
    img.height = scaled_h
    ws.add_image(img, PRODUCT_IMAGE_ANCHOR)

def _read_image_file(file_url):
    '''
    Reads a Frappe file by its file_url (e.g. "/files/doc_1808cdb4.png", or
    "/private/files/...") and returns its bytes, enforcing a max-size cap.
    Returns None (logged) if the file is missing or too large.
    '''
    try:
        if not frappe.db.exists('File', {'file_url': file_url}):
            frappe.log_error(f'No File found for url: {file_url}', 'techpack._read_image_file()')
            return None

        raw = frappe.get_doc('File', {'file_url': file_url}).get_content()
        if len(raw) > PRODUCT_IMAGE_MAX_BYTES:
            frappe.log_error(
                f'Product image exceeds {PRODUCT_IMAGE_MAX_BYTES} bytes: {file_url}',
                'techpack._read_image_file()',
            )
            return None
        return raw
    except Exception:
        frappe.log_error(frappe.get_traceback(), 'techpack._read_image_file()')
        return None

def _safe_filename(name):
    name = str(name).strip() or 'techpack'
    for ch in '/\\:*?"<>|':
        name = name.replace(ch, '-')
    return name


# --- Tech Pack listing ----------------------------------------------------
# Paginated list of Tech Pack records for the master panel. Ported from the
# "Tech Pack list" Server Script so the endpoint lives in version control;
# output shape is kept identical ({total, limit, offset, items}) so callers
# only need to swap the URL to /api/method/prism.api.techpack.list_tech_packs.
LIST_FIELDS = [
    'name', 'style_code', 'style_name', 'brand', 'season', 'category',
    'status', 'designer', 'sample_size', 'logo_url', 'creation', 'modified',
]


@frappe.whitelist(allow_guest=True)
@auth_required
def list_tech_packs(brand=None, season=None, status=None, search=None, limit=20, offset=0):
    '''
    Paginated Tech Pack list with optional brand / season / status filters and a
    free-text search across style_code / style_name / model_no. Each row is
    augmented with measurement_count, bom_count and the first main-sketch image.

    Auth: pass the JWT in the X-Auth-Token header (handled by @auth_required).
    PSL-only — brand users are rejected (mirrors request._require_internal()).
    '''
    # Internal/PSL users have no brand on their JWT; brand users do.
    if util.get_current_brand():
        frappe.throw('Only internal (PSL) users can access Tech Packs', frappe.PermissionError)

    limit = cint(limit) or 20
    offset = cint(offset) or 0

    filters = {}
    if brand:
        filters['brand'] = brand
    if season:
        filters['season'] = season
    if status:
        filters['status'] = status

    or_filters = []
    if search:
        term = f'%{search}%'
        or_filters = [
            ['style_code', 'like', term],
            ['style_name', 'like', term],
            ['model_no', 'like', term],
        ]

    rows = frappe.get_all(
        'Tech Pack',
        filters=filters,
        or_filters=or_filters or None,
        fields=LIST_FIELDS,
        order_by='modified desc',
        start=offset,
        page_length=limit,
    )

    for r in rows:
        r['measurement_count'] = frappe.db.count('Tech Pack Measurement', {'parent': r['name']})
        r['bom_count'] = frappe.db.count('Tech Pack BOM Item', {'parent': r['name']})

        # Main sketch image — first row of the main_sketch_images child table.
        ms = frappe.db.get_value(
            'Tech Pack Image',
            {'parent': r['name'], 'parentfield': 'main_sketch_images'},
            ['image_url', 'image_type', 'caption'],
            order_by='idx asc',
            as_dict=True,
        )
        if ms and ms.get('image_url'):
            r['mainSketch'] = {
                'url': ms['image_url'],
                'imageType': ms.get('image_type'),
                'caption': ms.get('caption'),
            }
        else:
            r['mainSketch'] = None

    total = frappe.db.count('Tech Pack', filters=filters)
    return {'total': total, 'limit': limit, 'offset': offset, 'items': rows}


@frappe.whitelist(allow_guest=True)
@auth_required
def get_tech_pack(name=None):
    '''
    Full Tech Pack detail as flat, camelCase, frontend-friendly arrays that
    mirror the DocType's child-table shape. Every row carries its own `product`
    column so the frontend can filter / group as needed. Ported from the
    "get_tech_pack_clean" Server Script; output shape kept identical.

    Auth: pass the JWT in the X-Auth-Token header (handled by @auth_required).
    PSL-only — brand users are rejected (mirrors request._require_internal()).
    '''
    # Internal/PSL users have no brand on their JWT; brand users do.
    if util.get_current_brand():
        frappe.throw('Only internal (PSL) users can access Tech Packs', frappe.PermissionError)

    if not name:
        frappe.throw("Missing 'name' query parameter")
    if not frappe.db.exists('Tech Pack', name):
        frappe.throw('Tech Pack {0} not found'.format(name), frappe.DoesNotExistError)

    d = frappe.get_doc('Tech Pack', name).as_dict()

    # --- helpers ---------------------------------------------------------
    def format_date(v):
        if not v:
            return None
        s = str(v)
        if len(s) >= 10 and s[4] == '-' and s[7] == '-':
            return s[8:10] + '.' + s[5:7] + '.' + s[0:4]
        return s

    def strip_empty(obj):
        return {k: v for k, v in obj.items() if v not in (None, '', [], {})}

    def clean_str(v):
        if v is None:
            return None
        s = str(v).strip()
        return s if s else None

    def trim_number(v):
        '''Numeric child-table fields are stored as strings like
        "60.500000000" / "0.000000000"; trim trailing zeros to "60.5" / "0".
        Non-numeric strings pass through unchanged.'''
        s = clean_str(v)
        if s is None:
            return None
        chk = s[1:] if s.startswith('-') else s
        if not chk or chk.count('.') > 1:
            return s
        if not all(c.isdigit() or c == '.' for c in chk):
            return s
        if '.' in s:
            s = s.rstrip('0').rstrip('.')
        return s or '0'

    def parse_details(v):
        '''Parse Additional Details as JSON when it looks like JSON; otherwise
        return the trimmed string. Falls back to raw string on any error.'''
        s = clean_str(v)
        if s is None:
            return None
        if not (s.startswith('{') or s.startswith('[')):
            return s
        try:
            return frappe.parse_json(s)
        except Exception:
            return s

    def flat_row(r, mapping):
        '''Turn a Frappe child-row dict into a clean public-API row. `mapping`
        is a list of (output_key, input_key, transform_or_None). Empty / None
        values are dropped.'''
        out = {}
        for out_k, in_k, xform in mapping:
            v = r.get(in_k)
            v = xform(v) if xform is not None else clean_str(v)
            if v not in (None, '', [], {}):
                out[out_k] = v
        return out

    def has_real_data(row):
        '''A row is real if it has at least one non-product key.'''
        return any(k for k in row.keys() if k != 'product')

    def make_image(img):
        '''Convert a Tech Pack Image dict to {url, imageType, caption}.
        Returns None if image_url is missing.'''
        url = clean_str(img.get('image_url'))
        if not url:
            return None
        out = {'url': url}
        it = clean_str(img.get('image_type'))
        if it:
            out['imageType'] = it
        cap = clean_str(img.get('caption'))
        if cap:
            out['caption'] = cap
        return out

    def sort_images(rows):
        '''Sort image rows by sort_order ascending (rows missing it last).'''
        def key(img):
            so = img.get('sort_order')
            try:
                return (0, int(so))
            except (TypeError, ValueError):
                try:
                    return (0, float(so))
                except (TypeError, ValueError):
                    return (1, 0)
        return sorted(rows or [], key=key)

    def parse_main_sketch_notes(notes):
        '''main_sketch_notes is a parent-level freetext field with lines like:
            [Pajama Top / Front Flat] Description here (ref: 27ESO_001)
        Parse each into {product, section, description, reference}.'''
        out = []
        if not notes:
            return out
        for line in notes.split('\n'):
            line = line.strip()
            if not line.startswith('[') or ']' not in line:
                continue
            head_end = line.index(']')
            head = line[1:head_end].strip()
            rest = line[head_end + 1:].strip()
            if '/' in head:
                parts = head.split('/', 1)
                product = parts[0].strip()
                section = parts[1].strip()
            else:
                product = head
                section = None
            reference = None
            description = rest
            ref_marker = '(ref:'
            if ref_marker in rest:
                ref_idx = rest.rindex(ref_marker)
                description = rest[:ref_idx].strip()
                ref_part = rest[ref_idx + len(ref_marker):].strip()
                if ref_part.endswith(')'):
                    reference = ref_part[:-1].strip()
            row = {'product': product}
            if section:
                row['section'] = section
            if description:
                row['description'] = description
            if reference:
                row['reference'] = reference
            out.append(row)
        return out

    # --- header (parent metadata) ---------------------------------------
    header = strip_empty({
        'styleCollectionName':  clean_str(d.get('style_name')),
        'brand':                clean_str(d.get('brand')),
        'season':               clean_str(d.get('season')),
        'style':                clean_str(d.get('style_code')),
        'modelNo':              clean_str(d.get('model_no')),
        'designer':             clean_str(d.get('designer')),
        'mark':                 clean_str(d.get('mark')),
        'date':                 format_date(d.get('date')),
        'dateOfDocumentation':  format_date(d.get('date_of_documentation')),
        'pdNumber':             clean_str(d.get('pd_number')),
        'vendor':               clean_str(d.get('vendor')),
        'category':             clean_str(d.get('category')),
        'status':               clean_str(d.get('status')),
        'dateRevised':          format_date(d.get('date_revised')),
        'sampleSize':           clean_str(d.get('sample_size')),
        'sizeRange':            clean_str(d.get('size_range')),
        'mainFabric':           clean_str(d.get('main_fabric')),
        'additionalDetails':    parse_details(d.get('additional_details')),
    })

    # --- flat data arrays (one per child table) -------------------------
    main_sketch = parse_main_sketch_notes(d.get('main_sketch_notes') or '')

    construction = []
    for r in (d.get('construction_details') or []):
        row = flat_row(r, [
            ('product',           'product',            None),
            ('section',           'section',            None),
            ('description',       'description',        None),
            ('additionalDetails', 'additional_details', parse_details),
        ])
        if has_real_data(row):
            construction.append(row)

    colorways = []
    for r in (d.get('colorways') or []):
        row = flat_row(r, [
            ('product',           'product',            None),
            ('code',              'colorway_code',      None),
            ('component',         'component',          None),
            ('colorName',         'color_name',         None),
            ('pantoneCode',       'pantone_code',       None),
            ('comment',           'comment',            None),
            ('additionalDetails', 'additional_details', parse_details),
        ])
        if has_real_data(row):
            colorways.append(row)

    bom = []
    for r in (d.get('bom_items') or []):
        row = flat_row(r, [
            ('product',           'product',            None),
            ('category',          'category',           None),
            ('component',         'component',          None),
            ('description',       'description',        None),
            ('content',           'content',            None),
            ('supplier',          'supplier',           None),
            ('code',              'pact_code',          None),
            ('color',             'color_1',            None),
            ('qty',               'qty',                trim_number),
            ('placement',         'placement',          None),
            ('comment',           'comment',            None),
            ('additionalDetails', 'additional_details', parse_details),
        ])
        if has_real_data(row):
            bom.append(row)

    care_label = []
    for r in (d.get('care_labels') or []):
        row = flat_row(r, [
            ('product',           'product',            None),
            ('category',          'category',           None),
            ('property',          'property',           None),
            ('value',             'value',              None),
            ('additionalDetails', 'additional_details', parse_details),
        ])
        if has_real_data(row):
            care_label.append(row)

    # measurements — LONG format (one row per POM x Size)
    measurements = []
    for r in (d.get('measurements') or []):
        row = flat_row(r, [
            ('product',           'product',            None),
            ('code',              'pom_code',           None),
            ('description',       'pom_description',    None),
            ('comment',           'comment',            None),
            ('unit',              'unit',               None),
            ('tolMinus',          'tol_minus',          trim_number),
            ('tolPlus',           'tol_plus',           trim_number),
            ('size',              'size',               None),
            ('value',             'value',              trim_number),
            ('additionalDetails', 'additional_details', parse_details),
        ])
        # A measurement row needs at least a POM code or a size+value.
        if row.get('code') or (row.get('size') and row.get('value')):
            measurements.append(row)

    # --- products (unique names across tables, first-seen order) --------
    product_names = []
    seen_lower = set()

    def add_product(p):
        s = clean_str(p)
        if not s:
            return
        key = s.lower()
        if key in seen_lower:
            return
        seen_lower.add(key)
        product_names.append(s)

    for arr in (main_sketch, construction, colorways, bom, care_label, measurements):
        for r in arr:
            add_product(r.get('product'))

    # --- images (flat per section, every row carries product) -----------
    def image_row(img):
        url = clean_str(img.get('image_url'))
        if not url:
            return None
        out = {'url': url}
        p = clean_str(img.get('product'))
        if p:
            out['product'] = p
        it = clean_str(img.get('image_type'))
        if it:
            out['imageType'] = it
        cap = clean_str(img.get('caption'))
        if cap:
            out['caption'] = cap
        return out

    def collect_images(field_name):
        out = []
        for img in sort_images(d.get(field_name) or []):
            r = image_row(img)
            if r is not None:
                out.append(r)
        return out

    images = {}
    for field, key in [
        ('header_images',        'header'),
        ('main_sketch_images',   'mainSketch'),
        ('construction_images',  'construction'),
        ('care_label_images',    'careLabel'),
        ('colorway_images',      'colorways'),
        ('bom_images',           'bom'),
        ('measurement_images',   'measurements'),
    ]:
        arr = collect_images(field)
        if arr:
            images[key] = arr

    # --- assets (convenience: logo + cover at top level) ----------------
    assets = {}
    for img in (d.get('header_images') or []):
        if (clean_str(img.get('image_type')) or '').lower() == 'logo':
            m = make_image(img)
            if m:
                assets['logo'] = m
                break
    if 'logo' not in assets and clean_str(d.get('logo_url')):
        assets['logo'] = {'url': clean_str(d.get('logo_url')), 'imageType': 'Logo'}
    for img in sort_images(d.get('main_sketch_images') or []):
        m = make_image(img)
        if m:
            assets['cover'] = m
            break

    # --- history --------------------------------------------------------
    history = []
    for r in (d.get('revisions') or []):
        row = strip_empty({
            'date':       format_date(r.get('revision_date')),
            'updatedBy':  clean_str(r.get('updated_by')),
            'tabUpdated': clean_str(r.get('tab_updated')),
            'notes':      clean_str(r.get('notes')),
        })
        if row:
            history.append(row)

    # --- envelope -------------------------------------------------------
    products_block = {}
    if main_sketch:
        products_block['mainSketch'] = main_sketch
    if construction:
        products_block['construction'] = construction
    if colorways:
        products_block['colorways'] = colorways
    if bom:
        products_block['bom'] = bom
    if care_label:
        products_block['careLabel'] = care_label
    if measurements:
        products_block['measurements'] = measurements
    if images:
        products_block['images'] = images

    data = {'id': d.get('name'), 'header': header}
    if assets:
        data['assets'] = assets
    if products_block:
        data['products'] = products_block
    if history:
        data['history'] = history

    return {
        'status':     'success',
        'statusCode': 200,
        'message':    'Tech pack fetched successfully',
        'data':       data,
    }
