import json

import frappe
from frappe.query_builder.functions import Abs, Count
from frappe.utils import cint
from pypika.analytics import RowNumber

from prism.auth.authenticator import auth_required
import prism.api.util as util
import prism.api.llm as llm
import prism.api.surplus_stock as surplus_stock
import prism.lib.cloud as cloud

MAX_IMAGE_FILE_SIZE_MB = 5

FABRIC_NUM_MAX_LLM_MATCHES = 10
FABRIC_NUM_MAX_DB_MATCHES = 3

SURPLUS_ITEM_NUM_MAX_LLM_MATCHES = 10

GARMENT_NUM_MAX_LLM_MATCHES = 20
GARMENT_NUM_MAX_DB_MATCHES = 5

DYED_FABRIC_MASTER_DOCTYPE = 'Moodboard Dyed Fabric'
GARMENT_MASTER_DOCTYPE = 'Sample Request'
SURPLUS_STOCK_ITEM_DOCTYPE = 'Surplus Stock'

'''
ToDo:
 - remove all (ignore_permissions=True)
'''

# --- write ---
@frappe.whitelist(allow_guest=True)
#@auth_required
def create_draft(draft_json: dict, title: str):
    return _upsert(draft_json, title)

@frappe.whitelist(allow_guest=True)
#@auth_required
def update_draft(moodboard_id: str, draft_json: dict, title: str=None):
    return _upsert(draft_json, title, moodboard_id)

@frappe.whitelist(allow_guest=True)
#@auth_required
def publish(moodboard_id: str, brand_ids: list):
    ''' Publishes a Moodboard and attaches it to Brands(s). '''

    try:
        if not frappe.db.exists('Moodboard', moodboard_id):
            return {'success': False, 'error': 'Moodboard does not exist!'}

        if not _can_edit_moodboard(moodboard_id):
            return {'success': False, 'error': 'Not authorized to publish this Moodboard!'}

        if not brand_ids or not isinstance(brand_ids, list):
            return {'success': False, 'error': 'At least one Brand is required to publish Moodboard!'}

        # save in db
        doc = frappe.get_doc('Moodboard', moodboard_id)

        doc.published_layout_json = doc.draft_layout_json
        doc.status = 'Published'
        doc.last_published_at = frappe.utils.now()

        doc.save(ignore_permissions=True)

        # attach moodboard to brand(s)
        for brand_id in brand_ids:
            if frappe.db.exists('Brand Moodboard', {'brand': brand_id, 'moodboard': moodboard_id}):
                bm = frappe.get_doc('Brand Moodboard', {'brand': brand_id, 'moodboard': moodboard_id})
                bm.layout_json = doc.published_layout_json
                bm.save(ignore_permissions=True)
            else:
                bm = frappe.new_doc('Brand Moodboard')
                bm.brand = brand_id
                bm.moodboard = moodboard_id
                bm.layout_json = doc.published_layout_json
                bm.insert(ignore_permissions=True)

        frappe.db.commit()

        # Cross-system Bell feed: ping every user of the brands it reached.
        _notify_moodboard_published(doc, brand_ids)

        return {
            'success': True,
            'data': doc
        }

    except Exception as ex:
        frappe.db.rollback()
        frappe.log_error(frappe.get_traceback(), 'moodboard.publish()')
        return { 'success': False, 'error': str(ex) }


def _notify_moodboard_published(doc, brand_ids):
    '''
    Raise a Prism Notification for every user of the brands a moodboard was
    just published to (Frappe -> prism-bot -> prism-web Bell). Best-effort: a
    failure here must never break publish.
    '''
    try:
        import prism.api.notifications as notifications
        import prism.api.util as util

        recipients = set()
        for brand_id in (brand_ids or []):
            recipients |= set(
                frappe.get_all('Brand User', filters={'brand': brand_id}, pluck='user')
            )
        if not recipients:
            return

        try:
            actor = util.get_current_user_id()
        except Exception:
            actor = None

        title = doc.moodboard_title or doc.name
        notifications.notify(
            recipients,
            event_type='moodboard_published',
            title=f'New moodboard published: {title}',
            body=title,
            deeplink=f'/moodboards/{doc.name}',
            category='Moodboards',
            from_user=actor,
            ref_doctype='Moodboard',
            ref_name=doc.name,
        )
    except Exception:
        frappe.log_error(frappe.get_traceback(),
                         'moodboard._notify_moodboard_published()')

@frappe.whitelist(allow_guest=True)
#@auth_required
def unpublish(moodboard_id: str, brand_ids: list):
    ''' Detaches a Moodboard from Brand(s) '''
    
    try:
        if not frappe.db.exists('Moodboard', moodboard_id):
            return {'success': False, 'error': 'Moodboard does not exist!'}

        if not _can_edit_moodboard(moodboard_id):
            return {'success': False, 'error': 'Not authorized to unpublish this Moodboard!'}

        if not brand_ids or not isinstance(brand_ids, list):
            return {'success': False, 'error': 'At least one brand is required to unpublish Moodboard!'}

        # detach moodboard from brands
        for brand_id in brand_ids:
            bm_name = frappe.db.exists('Brand Moodboard', {'brand': brand_id, 'moodboard': moodboard_id})
            if bm_name:
                frappe.delete_doc('Brand Moodboard', bm_name, ignore_permissions=True)

        frappe.db.commit()

        return {'success': True}

    except Exception as ex:
        frappe.db.rollback()
        frappe.log_error(frappe.get_traceback(), 'moodboard.unpublish()')
        return {'success': False, 'error': str(ex)}

@frappe.whitelist(allow_guest=True)
#@auth_required
def delete_board(moodboard_id: str):
    ''' Sets the "is_active" flag of a moodboard to zero. '''
    try:
        if not frappe.db.exists('Moodboard', moodboard_id):
            return {'success': False, 'error': 'Moodboard does not exist!'}

        frappe.db.set_value('Moodboard', moodboard_id, 'is_active', 0)
        frappe.db.commit()

        return {'success': True}

    except Exception as ex:
        frappe.db.rollback()
        frappe.log_error(frappe.get_traceback(), 'moodboard.delete_board()')
        return {'success': False, 'error': str(ex)}


# --- read ---
@frappe.whitelist(allow_guest=True)
#@auth_required
def get_all():
    ''' Returns the list of all moodboards with important fields only. '''
    try:
        moodboards = frappe.get_all('Moodboard',
            filters={'is_active': 1},
            fields=[
                'name as id',
                'moodboard_title as title',
                'status',
                'draft_layout_json as layout_json',
                'creation as created_at'
            ],
            order_by='modified desc'
        )

        if moodboards:
            for board in moodboards:
                board['layout_json'] = json.loads(board['layout_json'])

        return {
            'success': True,
            'moodboards': moodboards
        }

    except Exception as ex:
        frappe.log_error(frappe.get_traceback(), 'moodboard.get_all()')
        return { 'success': False, 'error': str(ex) }

@frappe.whitelist(allow_guest=True)
#@auth_required
def get_draft(moodboard_id: str):
    ''' Returns the draft layout json of the seller'''
    try:
        if not frappe.db.exists('Moodboard', moodboard_id):
            return {'success': False, 'error': 'Moodboard does not exist!'}

        # fetch from db
        doc = frappe.get_doc('Moodboard', moodboard_id)
        if not doc.draft_layout_json:
            doc.draft_layout_json = doc.published_layout_json

        data = frappe.parse_json(doc.draft_layout_json)

        # Compat: extracted styles + per-style cost now live in the standalone
        # `Moodboard Style` doctype (source of truth). Rebuild the old draft-JSON
        # shapes from those rows so all read consumers keep working unchanged.
        _inject_styles_and_cost(moodboard_id, data)

        return {
            'success': True,
            'data': data
        }

    except Exception as ex:
        return { 'success': False, 'error': str(ex) }

@frappe.whitelist(allow_guest=True)
@auth_required
def get_brand_moodboards():
    ''' Returns the published moodboards of a brand. '''

    try:
        brand = util.get_current_brand()
        if brand:
            brand_id = brand['id']
        else:
            return {'success': False, 'error': 'Invalid brand user!'}

        if not frappe.db.exists('Brand', brand_id):
            return {'success': False, 'error': 'Brand does not exist!'}

        rows = frappe.get_all('Brand Moodboard',
            filters={'brand': brand_id},
            fields=['moodboard', 'layout_json']
        )

        if not rows:
            return {'success': True, 'moodboards': []}

        moodboard_ids = [r['moodboard'] for r in rows]

        moodboards = frappe.get_all('Moodboard',
            filters={
                'name': ['in', moodboard_ids],
                'is_active': 1,
                'status': 'Published'
            },
            fields=[
                'name as id',
                'moodboard_title as title',
                'status',
                'published_layout_json as layout_json',
                'last_published_at',
                'creation as created_at'
            ],
            order_by='last_published_at desc'
        )

        layout_by_id = {r['moodboard']: r['layout_json'] for r in rows}
        for board in moodboards:
            raw = layout_by_id.get(board['id'])
            board['layout_json'] = json.loads(raw) if raw else None

        return {
            'success': True,
            'moodboards': moodboards
        }

    except Exception as ex:
        frappe.log_error(frappe.get_traceback(), 'moodboard.get_brand_moodboards()')
        return {'success': False, 'error': str(ex)}

@frappe.whitelist(allow_guest=True)
@auth_required
def get_matching_fabrics(
    trendForecast: dict, 
    withImgOnly: bool=True,
    maxItems: int=10
):
    '''
    Recommends inventory fabrics that fit a given garment trend forecast.

    Pipeline:
      1. Sends the trend forecast to the LLM along with the live inventory
         vocabulary (distinct Quality / Blend values from
         "Moodboard Dyed Fabric"), and asks for up to FABRIC_NUM_MAX_LLM_MATCHES
         fabric ideas. For each idea the LLM returns 2-5 close-matching
         values per attribute (Quality, Blend), a GSM
         range, and a RelevancyScore (0-100) ranking it against the trend.
      2. Looks up each suggestion in "Moodboard Dyed Fabric" using the
         broadened attribute lists (IN filters) and the GSM range, capped
         at FABRIC_NUM_MAX_DB_MATCHES rows per suggestion.
      3. Builds a flat "top_matches" collection (up to FABRIC_NUM_MAX_FINAL_MATCHES
         rows) by walking suggestions in RelevancyScore order, taking up
         to 3 rows from each, and deduping by row id.

    Returns:
        {
            'success': True,
            'data': [<suggestion>, ...],     # LLM ideas, each with `matches`
            'top_matches': [<db row>, ...],  # cross-suggestion shortlist
        }
    '''

    try:
        system_prompt = f'''You are a textile merchandiser at a company that manufactures and stocks fabrics for garments. Given a third-party garment trend forecast, recommend which fabrics from our existing inventory best fit the trends described, so we know what to pull forward for sampling and production.

# OUR INVENTORY VOCABULARY
These are the ONLY valid Quality and Blend values. You must copy them verbatim (exact spelling, casing, and spacing) — never invent, translate, or reformat them.

Quality:
---
{_get_fabric_attributes('clean_quality', withImgOnly)}

Blend:
---
{_get_fabric_attributes('clean_blend', withImgOnly)}

# YOUR TASK
Identify the fabrics in our inventory that best match the trend forecast. For each recommendation, return 2-5 close-matching Quality values and 2-5 close-matching Blend values (most relevant first); these lists are used to broaden the inventory search, so include plausible alternatives, not just the single best guess. Also give a GSM range (weight in grams per square meter) that brackets the typical weight for that fabric/garment type.

Return a JSON array of recommendations using exactly this shape:
[
    {{
        "Name": "short human-readable label for this fabric idea, e.g. 'Lightweight compact jersey'",
        "Quality": ["SJY_COMPACT", "SJY_REGULAR"],
        "Blend": ["60.00 :40.00 FTO:RP", "70.00 :30.00 FTO:RP"],
        "MinGSM": 160,
        "MaxGSM": 220,
        "RelevancyScore": 85
    }}
]

# RULES
- Recommend at most {FABRIC_NUM_MAX_LLM_MATCHES} fabrics, sorted by RelevancyScore descending.
- Quality and Blend values MUST be chosen verbatim from the inventory vocabulary above; drop any value you cannot match to that list rather than guessing.
- Order each Quality/Blend list from most to least relevant, 2-5 values each.
- MinGSM and MaxGSM are positive integers with MinGSM <= MaxGSM, chosen to overlap realistic inventory weights for the trend.
- RelevancyScore is an integer 0-100 measuring how strongly the fabric fits the forecast (100 = perfect fit). Base it on the trend's described drape, weight, season, and end-use.
- If the forecast provides no usable signal, return an empty array [].
- Output raw, valid JSON only: a single array, double-quoted keys/strings, no trailing commas, no markdown fences, no commentary.
'''

        user_prompt = f'''Here is a third-party garment trend forecast report:
---
{trendForecast}
---

Recommend the fabrics from our inventory that best match this forecast, following the format and rules above.'''

        fabrics = llm.get_claude_response(system_prompt, user_prompt, 'list')

        #--- db inventory matches ---
        DyedFabric = frappe.qb.DocType(DYED_FABRIC_MASTER_DOCTYPE)

        for suggestion in fabrics:
            # Rank rows within each (quality, blend) group by creation desc,
            # so we can keep only the latest row per combination (rn = 1).
            row_num = (
                RowNumber()
                .over(DyedFabric.clean_quality, DyedFabric.clean_blend)
                #.over(DyedFabric.batch)
                .orderby(DyedFabric.creation, order=frappe.qb.desc)
            )

            ranked = (
                frappe.qb.from_(DyedFabric)
                .select(
                    DyedFabric.name.as_('id'),
                    DyedFabric.code,
                    DyedFabric.description,
                    DyedFabric.custom_fabric_name,
                    DyedFabric.clean_quality.as_('quality'),
                    DyedFabric.clean_blend.as_('blend'),
                    DyedFabric.gsm,
                    DyedFabric.shade_category,
                    DyedFabric.finish,
                    DyedFabric.batch,
                    DyedFabric.creation,
                    row_num.as_('rn'),
                )
            )

            if withImgOnly:
                ranked = ranked.where(DyedFabric.has_image_file == 1)

            if suggestion.get('Quality'):
                ranked = ranked.where(DyedFabric.clean_quality.isin(suggestion['Quality']))
            if suggestion.get('Blend'):
                ranked = ranked.where(DyedFabric.clean_blend.isin(suggestion['Blend']))

            min_gsm = suggestion.get('MinGSM')
            max_gsm = suggestion.get('MaxGSM')
            if min_gsm is not None and max_gsm is not None:
                ranked = ranked.where(DyedFabric.gsm.between(min_gsm, max_gsm))

            ranked = ranked.as_('ranked')
            query = (
                frappe.qb.from_(ranked)
                .select(
                    ranked.id,
                    ranked.code,
                    ranked.description,
                    ranked.custom_fabric_name,
                    ranked.quality,
                    ranked.blend,
                    ranked.gsm,
                    ranked.shade_category,
                    ranked.finish,
                    ranked.batch,
                )
                .where(ranked.rn == 1)
                .orderby(ranked.creation, order=frappe.qb.desc)
                .limit(FABRIC_NUM_MAX_DB_MATCHES)
            )

            suggestion['matches'] = query.run(as_dict=True)


        #--- top (FABRIC_NUM_MAX_FINAL_MATCHES) final matches
        top_matches = []
        seen_keys = set()
        for suggestion in sorted(fabrics, key=lambda s: s.get('RelevancyScore', 0), reverse=True):
            for match in suggestion.get('matches', []):
                if len(top_matches) >= maxItems:
                    break

                key = match.get('batch')
                if key in seen_keys:
                    continue

                # fabric image + llm score
                match['image_url'] = cloud.fabric_thumbnail_url(key)
                match['relevancy_score'] = suggestion.get('RelevancyScore', 0)

                top_matches.append(match)
                seen_keys.add(key)
            
            if len(top_matches) >= maxItems:
                break

        # order the shortlist by fabric name, then by weight (lightest first) within a name
        top_matches.sort(key=lambda m: ((m.get('custom_fabric_name') or '').lower(), m.get('gsm') or 0))

        return {
            'success': True,
            'match_count': len(top_matches),
            'top_matches': top_matches,
            'data': fabrics,
        }

    except Exception as ex:
        frappe.log_error(frappe.get_traceback(), 'moodboard.get_matching_fabrics()')
        return {'success': False, 'error': str(ex)}

@frappe.whitelist(allow_guest=True)
@auth_required
def get_matching_surplus_fabrics(
    trendForecast: dict, 
    withImgOnly: bool=True,
    maxItems: int=10
):
    '''
    Recommends surplus stock fabrics that fit a given garment trend forecast.

    Pipeline:
      1. Sends the trend forecast to the LLM along with the live inventory
         vocabulary (the distinct construction + blend pairs that actually
         exist in SURPLUS_STOCK_ITEM_DOCTYPE), and asks for up to
         SURPLUS_ITEM_NUM_MAX_LLM_MATCHES fabric ideas. For each idea the LLM
         returns one Construction, one Blend, the single most suitable GSM, and
         a RelevancyScore (0-100) ranking it against the trend.
      2. Walks the suggestions in RelevancyScore order and looks each one up in
         "Surplus Stock" by exact construction + blend match, keeping the single
         row whose GSM is closest to the suggested one (newest row wins ties).
         The row is attached to the suggestion as `db_match`.
      3. Collects those rows into a flat "top_matches" shortlist, deduped by
         batch and capped at `maxItems`. Each shortlisted row is enriched with
         `relevancy_score` plus signed `image_url` / `thumbnail` (None when the
         row has no image file).

    Note that a suggestion contributes at most one row, so `top_matches` holds
    at most one row per LLM idea, and fewer once duplicate batches are dropped.
    Suggestions dropped as duplicates, or left unvisited once `maxItems` is
    reached, carry an unenriched `db_match` or none at all -- read `top_matches`
    rather than `llm_suggestions` for display.

    Args:
        trendForecast: the third-party trend forecast report.
        withImgOnly: restrict the (construction + blend) vocabulary offered to
            the LLM to stock rows that have an image file. The row lookup itself
            is currently not filtered, so a match may still come back imageless.
        maxItems: max number of rows in `top_matches`.

    Returns:
        {
            'success': True,
            'data': {
                'match_count': <len(top_matches)>,
                'top_matches': [<db row>, ...],      # the shortlist
                'llm_suggestions': [<suggestion>, ...],  # LLM ideas, see note
            },
        }
        or {'success': False, 'error': <message>} when there is no
        quality/blend master data, or on error.
    '''

    try:
        combinations = _get_surplus_fabric_combinations(withImgOnly)
        if not combinations:
            return {'success': False, 'error': 'no quality/blend master data found!'}

        # Only real, existing (construction + blend) pairs are needed
        catalog_lines = '\n'.join(
            f'{c["construction"]}\t{c["blend"]}' for c in combinations
        )

        system_prompt = f'''You are an expert assistant for a textile manufacturing company that produces knitted fabrics for garments. Given a third-party garment trend forecast, recommend which fabrics from the existing inventory best fit the trends described, so they know what to pull forward for sampling and production.

# INVENTORY VOCABULARY
These are the ONLY valid (construction + blend) combinations (construction<TAB>blend, one per line).
You must copy them verbatim (exact spelling, casing, and spacing) — never invent, translate, or reformat them:
---
{catalog_lines}
---

# YOUR TASK
Identify the fabrics in our inventory that best match the trend forecast. Also give the single most suitable GSM (weight in grams per square meter) for that fabric/garment type.

Return a JSON array of recommendations using exactly this shape:
[
    {{
        "Construction": "SJY_COMPACT",
        "Blend": "60.00 :40.00 FTO:RP",
        "GSM": 190,
        "RelevancyScore": 85
    }}
]

# RULES
- Recommend at most {SURPLUS_ITEM_NUM_MAX_LLM_MATCHES} fabrics, sorted by RelevancyScore descending.
- Return exactly these four keys per recommendation — no label, name, description or any other invented field.
- Construction and Blend are single values, and the two together MUST be one of the (construction + blend) pairs listed above, copied verbatim; drop any recommendation you cannot match to that list rather than guessing.
- GSM is a single positive integer: the ideal weight for this fabric idea, not a range. We stock a fixed set of weights, so we will pick the stock item closest to it — give the target you would order if you could.
- RelevancyScore is an integer 0-100 measuring how strongly the fabric fits the forecast (100 = perfect fit). Base it on the trend's described drape, weight, season, and end-use.
- If the forecast provides no usable signal, return an empty array [].
- Output raw, valid JSON only: a single array, double-quoted keys/strings, no trailing commas, no markdown fences, no commentary.
'''

        user_prompt = f'''Here is a third-party garment trend forecast report:
---
{trendForecast}
---

Recommend the fabrics from the inventory that best match this forecast, following the format and rules above.'''

        fabrics = llm.get_claude_response(system_prompt, user_prompt, 'list')
        fabrics = [f for f in fabrics if isinstance(f, dict)] if isinstance(fabrics, list) else []

        #--- db inventory matches ---
        SurplusItems = frappe.qb.DocType(SURPLUS_STOCK_ITEM_DOCTYPE)

        top_matches = []
        seen_keys = set()
        for suggestion in sorted(fabrics, key=lambda s: cint(s.get('RelevancyScore')), reverse=True):
            query = (
                frappe.qb.from_(SurplusItems)
                .select(*surplus_stock._row_columns(SurplusItems))
            )

            #if withImgOnly:
            #    query = query.where(SurplusItems.has_image_file == 1)

            if suggestion.get('Construction'):
                query = query.where(SurplusItems.quality == suggestion['Construction'])
            if suggestion.get('Blend'):
                query = query.where(SurplusItems.blend == suggestion['Blend'])

            # rank the construction/blend matches by how close is the GSM
            target_gsm = cint(suggestion.get('GSM'))
            if target_gsm:
                query = query.orderby(Abs(SurplusItems.gsm - target_gsm), order=frappe.qb.asc)

            rows = (
                query
                .orderby(SurplusItems.creation, order=frappe.qb.desc)
                .limit(1)
            ).run(as_dict=True)
            if not rows:
                continue

            db_row = rows[0] if rows else None
            suggestion['db_match'] = db_row

            key = db_row.get('batch')
            if key in seen_keys:
                continue

            # fabric images + llm score
            has_image = bool(db_row.get('has_image_file'))
            db_row['image_url'] = cloud.fabric_image_signed_url(db_row.get('batch')) if has_image else None
            db_row['thumbnail'] = cloud.fabric_thumbnail_signed_url(db_row.get('batch')) if has_image else None
            db_row['relevancy_score'] = cint(suggestion.get('RelevancyScore'))

            top_matches.append(db_row)
            seen_keys.add(key)

            if len(top_matches) >= maxItems:
                break

        return {
            'success': True,
            'data': {
                'match_count': len(top_matches),
                'top_matches': top_matches,
                'llm_suggestions': fabrics,
            },
        }

    except Exception as ex:
        frappe.log_error(frappe.get_traceback(), 'moodboard.get_matching_surplus_fabrics()')
        return {'success': False, 'error': str(ex)}

@frappe.whitelist(allow_guest=True)
@auth_required
def get_matching_garments(
    trendForecast: dict, 
    genders: list=[],
    styles: list=[],
    withImgOnly: bool=True,
    maxItems: int=20
):
    '''
    Recommends inventory garments that fit a given garment trend forecast.

    Admin-only. Non-admin callers and any exception return
    {'success': False, 'error': ...}.

    Args:
        trendForecast: 3rd-party trend forecast payload, passed to the LLM as-is.
        withImgOnly:   Currently unused (the batch/image filter is commented
                       out); kept for signature parity with get_matching_fabrics().

    Pipeline:
      1. Sends the trend forecast to the LLM along with the live inventory
         vocabulary (distinct GenderCategory / GarmentType / Quality / Blend from Frappe db),
         and asks for up to GARMENT_NUM_MAX_LLM_MATCHES garment ideas.
         For each idea the LLM returns 2-5 close-matching values per attribute
         (GenderCategory, GarmentType, Quality, Blend), a MinGSM/MaxGSM weight
         range, a short Name, and a RelevancyScore
         (0-100) ranking it against the trend.
      2. Looks up each suggestion in "Sample Request" (restricted to
         garment_element = 'Main Body') using the broadened attribute lists
         as IN filters, plus a finished_gsm BETWEEN filter from
         MinGSM/MaxGSM. Keeps only the
         latest row (by creation desc) per unique (gender, product_category,
         fabric_quality, fabric_blend) combination and caps results at
         GARMENT_NUM_MAX_DB_MATCHES rows per suggestion. The resulting rows
         are attached to the suggestion as `db_matches`.
      3. Builds a flat `top_matches` shortlist (up to GARMENT_NUM_MAX_FINAL_MATCHES
         rows) by walking suggestions in RelevancyScore order, taking up
         to 3 rows from each, deduping by row id, and attaching `image_urls`
         (front/back S3 URLs resolved via _get_garment_images(gsr_no)) when
         the row has a gsr_no.

    Returns:
        {
            'success': True,
            'top_matches': [<db row with image_urls>, ...],  # cross-suggestion shortlist
            'data':        [<suggestion with `db_matches`>, ...],  # full LLM ideas
        }
    '''

    try:
        # When the caller supplies explicit genders/styles, infer the closest
        # matching vocabulary value(s) from those selections; otherwise infer
        # the attribute from the trend forecast.
        selection_lines = []
        if genders:
            selection_lines.append(f'- GenderCategory: {genders}')
        if styles:
            selection_lines.append(f'- GarmentType: {styles}')

        if selection_lines:
            inference_instruction = (
                'The user has explicitly requested the following garment attribute value(s):\n'
                + '\n'.join(selection_lines)
                + '\n\nFor each attribute listed above, infer the closest matching value(s) '
                'from the corresponding inventory vocabulary based on the requested value(s) '
                '(do NOT use the trend forecast for these attributes). '
                'For any attribute NOT listed above, infer its value(s) from the trend forecast report.'
            )
        else:
            inference_instruction = (
                'Infer all garment attribute value(s) from the trend forecast report.'
            )

        system_prompt = f'''Our company manufactures and stocks garments. Suggest what types of garments we should manufacture/stock, described in terms of GenderCategory, GarmentType, Quality and Blend.

For each suggested garment, return arrays of 2-5 close-matching values (most relevant first) for each attribute so we can broaden the inventory search. Use the following JSON format,
[
    {{
        "Name": "",
        "GenderCategory": ["Men", "Kids"],
        "GarmentType": ["Hoodie", "Sweatshirt"],
        "Quality": ["Single Jersey", "Rib"],
        "Blend": ["100% Cotton", "Cotton/Polyester"],
        "MinGSM": 240,
        "MaxGSM": 320,
        "RelevancyScore": 85
    }}
]

We have garments in the inventory with the following attributes,

GenderCategory:
---
{_get_garment_attributes('gender', withImgOnly)}

GarmentType:
---
{_get_garment_attributes('product_category', withImgOnly)}

Quality:
---
{_get_garment_attributes('clean_construction', withImgOnly)}

Blend:
---
{_get_garment_attributes('clean_blend', withImgOnly)}


# INSTRUCTIONS:
- {inference_instruction}
- Suggest at most {GARMENT_NUM_MAX_LLM_MATCHES} garments, ordered by RelevancyScore descending
- Pick values for GenderCategory, GarmentType, Quality and Blend strictly from the inventory vocabularies above; do not invent, translate, or paraphrase values, and preserve their exact casing/punctuation
- MinGSM and MaxGSM are positive integers (garment fabric weight in grams per square meter) with MinGSM <= MaxGSM, chosen to bracket realistic inventory weights for the suggested garment
- For each attribute, return a list of 2-5 close matches, ordered from most to least relevant; if there is not enough signal to infer an attribute, return an empty list for that field rather than guessing
- Cover meaningfully distinct garment ideas — avoid near-duplicate suggestions that differ only in attribute ordering
- Name should be a short, human-readable label summarizing the suggested garment (e.g. "Oversized chocolate hoodie")
- RelevancyScore is an integer 0-100 indicating how strongly this garment fits the requested attributes and/or trend forecast (100 = perfect fit); use the full range and reserve high scores for strong matches
- Return raw JSON array only, no markdown fences, no commentary
'''

        user_prompt = f'''Here is one 3rd party trend forecast report on garments,
---
{trendForecast}
---

Suggest matching garments from our inventory.'''

        garments = llm.get_claude_response(system_prompt, user_prompt, 'list')

        #--- db inventory matches ---
        GarmentMaster = frappe.qb.DocType(GARMENT_MASTER_DOCTYPE)
        #GarmentPart = frappe.qb.DocType('Sample Fabric Element')

        for suggestion in garments:
            # ranking based on gsr_no
            row_num = (
                RowNumber()
                .over(GarmentMaster.gsr_no)
                .orderby(GarmentMaster.creation, order=frappe.qb.desc)
            )

            ranked = (
                frappe.qb.from_(GarmentMaster)
                .select(
                    GarmentMaster.name.as_('id'),
                    GarmentMaster.gsr_no,
                    GarmentMaster.ai_description.as_('garment_name'),
                    GarmentMaster.gender,
                    GarmentMaster.product_category,
                    GarmentMaster.clean_construction.as_('fabric_quality'),
                    GarmentMaster.clean_blend.as_('fabric_blend'),
                    GarmentMaster.element_colour,
                    GarmentMaster.finished_gsm,
                    GarmentMaster.image_urls.as_('image_urls_raw'),
                    GarmentMaster.image_urls_clean.as_('image_urls_clean_raw'),
                    GarmentMaster.image_urls_3d.as_('image_urls_3d_raw'),
                    GarmentMaster.creation,
                    row_num.as_('rn'),
                )
                .where(GarmentMaster.garment_element == 'Main Body')
            )

            if withImgOnly:
                ranked = ranked.where(
                    GarmentMaster.image_urls.notnull()
                    | GarmentMaster.image_urls_3d.notnull()
                )

            if suggestion.get('GenderCategory'):
                ranked = ranked.where(GarmentMaster.gender.isin(suggestion['GenderCategory']))
            if suggestion.get('GarmentType'):
                ranked = ranked.where(GarmentMaster.product_category.isin(suggestion['GarmentType']))
            if suggestion.get('Quality'):
                ranked = ranked.where(GarmentMaster.clean_construction.isin(suggestion['Quality']))
            if suggestion.get('Blend'):
                ranked = ranked.where(GarmentMaster.clean_blend.isin(suggestion['Blend']))

            min_gsm = suggestion.get('MinGSM')
            max_gsm = suggestion.get('MaxGSM')
            if min_gsm is not None and max_gsm is not None:
                ranked = ranked.where(GarmentMaster.finished_gsm.between(min_gsm, max_gsm))

            ranked = ranked.as_('ranked')
            query = (
                frappe.qb.from_(ranked)
                .select(
                    ranked.id,
                    ranked.gsr_no,
                    ranked.garment_name,
                    ranked.gender,
                    ranked.product_category,
                    ranked.fabric_quality,
                    ranked.fabric_blend,
                    ranked.element_colour,
                    ranked.finished_gsm,
                    ranked.image_urls_raw,
                    ranked.image_urls_clean_raw,
                    ranked.image_urls_3d_raw,
                )
                .where(ranked.rn == 1)
                .orderby(ranked.creation, order=frappe.qb.desc)
                .limit(GARMENT_NUM_MAX_DB_MATCHES)
            )

            suggestion['db_matches'] = query.run(as_dict=True)

        #--- top (GARMENT_NUM_MAX_FINAL_MATCHES) final matches
        top_matches = []
        seen_keys = set()
        for suggestion in sorted(garments, key=lambda s: s.get('RelevancyScore', 0), reverse=True):
            for db_match in suggestion.get('db_matches', []):
                if len(top_matches) >= maxItems:
                    break

                key = db_match.get('gsr_no')
                if key in seen_keys:
                    continue

                #db_match['image_urls'] = _format_garment_image_urls(db_match['image_urls_raw']) if db_match.get('image_urls_raw') else {}                    
                if db_match.get('image_urls_3d_raw'):
                    db_match['image_urls'] = cloud.format_garment_image_urls(db_match['image_urls_3d_raw'])
                elif db_match.get('image_urls_clean_raw'):
                    db_match['image_urls'] = cloud.format_garment_image_urls(db_match['image_urls_clean_raw'])
                elif db_match.get('image_urls_raw'):
                    db_match['image_urls'] = cloud.format_garment_image_urls(db_match['image_urls_raw'])
                else:
                    db_match['image_urls'] = {}
                
                db_match['relevancy_score'] = suggestion.get('RelevancyScore', 0)

                top_matches.append(db_match)
                seen_keys.add(key)

            if len(top_matches) >= maxItems:
                break

        return {
            'success': True,
            'match_count': len(top_matches),
            'top_matches': top_matches,
            'data': garments,
        }

    except Exception as ex:
        frappe.log_error(frappe.get_traceback(), 'moodboard.get_matching_garments()')
        return {'success': False, 'error': str(ex)}

@frappe.whitelist(allow_guest=True)
@auth_required
def search_fabrics(
    quality: str = None,
    blend: str = None,
    min_gsm: float = None,
    max_gsm: float = None,
    withImgOnly: bool = True,

    order_by: str = 'creation',
    order_dir: str = 'desc',

    page: int = 1,
    page_size: int = 20,
):
    '''
    Returns a paginated list of moodboard fabrics (DYED_FABRIC_MASTER_DOCTYPE),
    optionally filtered by quality, blend (like search) and/or gsm.

    Args:
        quality:     Optional quality term matched (case-insensitive LIKE)
                     against clean_quality.
        blend:       Optional blend term matched (case-insensitive LIKE) against
                     clean_blend.
        min_gsm:     Optional lower bound (inclusive) on gsm.
        max_gsm:     Optional upper bound (inclusive) on gsm.
        withImgOnly: When True, only rows that have an image file are returned and
                     each row is enriched with an `image_url`.
        order_by:    Column to sort by (whitelisted; defaults to 'creation').
        order_dir:   Sort direction, 'asc' or 'desc' (defaults to 'desc').
        page:        1-based page number.
        page_size:   Number of rows per page (clamped to [1, 100]).

    Returns:
        {
            'success': True,
            'data': [<fabric row>, ...],
            'pagination': {
                'page', 'page_size', 'total_count', 'total_pages',
                'has_next', 'has_previous',
            }
        }
    '''

    try:
        #--- normalize paging / sorting params (whitelisted to avoid injection) ---
        ALLOWED_ORDER_FIELDS = ['creation', 'quality', 'blend', 'gsm', 'shade_category', 'finish', 'custom_fabric_name']

        page = max(1, int(page or 1))
        page_size = min(100, max(1, int(page_size or 20)))
        offset = (page - 1) * page_size

        if order_by not in ALLOWED_ORDER_FIELDS:
            order_by = 'creation'
        direction = frappe.qb.asc if str(order_dir).lower() == 'asc' else frappe.qb.desc

        #--- db inventory matches ---
        DyedFabric = frappe.qb.DocType(DYED_FABRIC_MASTER_DOCTYPE)

        def _get_filters(q):
            if quality:
                q = q.where(DyedFabric.clean_quality.like(f'%{str(quality).strip()}%'))
            if blend:
                q = q.where(DyedFabric.clean_blend.like(f'%{str(blend).strip()}%'))
            if min_gsm is not None:
                q = q.where(DyedFabric.gsm >= float(min_gsm))
            if max_gsm is not None:
                q = q.where(DyedFabric.gsm <= float(max_gsm))
            if withImgOnly:
                q = q.where(DyedFabric.has_image_file == 1)
            return q

        # Rank rows within each batch by creation desc, so we keep
        # only the latest row per batch (rn == 1).
        row_num = (
            RowNumber()
            .over(DyedFabric.batch)
            .orderby(DyedFabric.creation, order=frappe.qb.desc)
        )

        ranked = _get_filters(
            frappe.qb.from_(DyedFabric)
            .select(
                DyedFabric.name.as_('id'),
                DyedFabric.code,
                DyedFabric.description,
                DyedFabric.custom_fabric_name,
                DyedFabric.clean_quality.as_('quality'),
                DyedFabric.clean_blend.as_('blend'),
                DyedFabric.gsm,
                DyedFabric.shade_category,
                DyedFabric.finish,
                DyedFabric.batch,
                DyedFabric.creation,
                row_num.as_('rn'),
            )
        ).as_('ranked')

        #--- total count for pagination metadata (one row per batch) ---
        count_query = (
            frappe.qb.from_(ranked)
            .select(Count('*'))
            .where(ranked.rn == 1)
        )
        total_count = count_query.run()[0][0]
        total_pages = (total_count + page_size - 1) // page_size

        #--- page of rows (top row per batch) ---
        query = (
            frappe.qb.from_(ranked)
            .select(
                ranked.id,
                ranked.code,
                ranked.description,
                ranked.custom_fabric_name,
                ranked.quality,
                ranked.blend,
                ranked.gsm,
                ranked.shade_category,
                ranked.finish,
                ranked.batch,
            )
            .where(ranked.rn == 1)
            .orderby(getattr(ranked, order_by), order=direction)
            .limit(page_size)
            .offset(offset)
        )

        rows = query.run(as_dict=True)

        # fabric images
        for row in rows:
            row['image_url'] = cloud.fabric_thumbnail_url(row.get('batch'))

        return {
            'success': True,
            'data': rows,
            'pagination': {
                'page': page,
                'page_size': page_size,
                'total_count': total_count,
                'total_pages': total_pages,
                'has_next': page < total_pages,
                'has_previous': page > 1,
            }
        }

    except Exception as ex:
        frappe.log_error(frappe.get_traceback(), 'moodboard.search_fabrics()')
        return {'success': False, 'error': str(ex)}


# --- helpers ---
# Access model:
#   - Create / read: open to anyone (no role required).
#   - Edit / publish / unpublish an existing board: the board's owner, or an admin.
# Admin roles resolve against frappe.session.user, which the JWT before_request
# hook (set_user_from_jwt_header) sets to the real User.
ADMIN_ROLES = ['Moodboard Admin', 'System Manager']

def _is_admin_user():
    ''' Holds an admin role — may edit any board. '''
    return util.user_has_roles(ADMIN_ROLES)

def _can_edit_moodboard(moodboard):
    '''
    True if the current user may mutate this board: an admin (any board) or the
    board's owner. `moodboard` may be a Moodboard doc or its name. Returns False
    if the board can't be resolved.
    '''
    if _is_admin_user():
        return True
    owner = getattr(moodboard, 'owner', None)
    if owner is None:
        owner = frappe.db.get_value('Moodboard', moodboard, 'owner')
    return bool(owner) and owner == frappe.session.user

def _upsert(draft_json: dict, title: str=None, moodboard_id: str=None):
    try:
        is_new = not moodboard_id

        # initiate doc object
        if is_new:
            doc = frappe.new_doc('Moodboard')
        else:
            if frappe.db.exists('Moodboard', moodboard_id):
                doc = frappe.get_doc('Moodboard', moodboard_id)
            else:
                return { 'success': False, 'error': 'Moodboard does not exist!' }

            # creators may only edit their own boards; admins edit any
            if not _can_edit_moodboard(doc):
                return { 'success': False, 'error': 'Not authorized to edit this Moodboard!' }

        # Styles + per-style cost are owned by the `Moodboard Style` doctype now;
        # never let them persist inside the draft blob (the get_draft serializer
        # rebuilds them from the rows). Strip before saving.
        _strip_style_keys(draft_json)

        # save images (replace base64 "IMAGE_*"" attributes with file URLs)
        _process_images(draft_json)

        # save in db
        if title:
            doc.moodboard_title = title
        if draft_json:
            doc.draft_layout_json = json.dumps(draft_json)

        if is_new:
            doc.insert(ignore_permissions=True)
        else:
            doc.save(ignore_permissions=True)

        frappe.db.commit()

        return {
            'success': True,
            'data': {
                'id': doc.name,
                'draft_json': draft_json
            }
        }

    except Exception as ex:
        frappe.db.rollback()
        frappe.log_error(frappe.get_traceback(), 'moodboard._upsert()')
        return { 'success': False, 'error': str(ex) }

def _inject_styles_and_cost(moodboard_id: str, data):
    '''
    Overlay editorV2.foundation.extracted_styles + editorV2.cost_calculation with
    the values rebuilt from the `Moodboard Style` rows (source of truth, §4).
    '''
    if not isinstance(data, dict):
        return
    import prism.api.moodboard_style as ms

    ev2 = data.get('editorV2')
    if not isinstance(ev2, dict):
        ev2 = {}

    foundation = ev2.get('foundation')
    if not isinstance(foundation, dict):
        foundation = {}
    foundation['extracted_styles'] = ms.build_extracted_styles(moodboard_id)
    ev2['foundation'] = foundation

    cost_calc = ms.build_cost_calculation(moodboard_id)
    if cost_calc is not None:
        ev2['cost_calculation'] = cost_calc
    else:
        ev2.pop('cost_calculation', None)

    data['editorV2'] = ev2

def _strip_style_keys(draft_json):
    ''' Remove serializer-owned keys so they don't get persisted in the blob. '''
    if not isinstance(draft_json, dict):
        return
    ev2 = draft_json.get('editorV2')
    if not isinstance(ev2, dict):
        return
    ev2.pop('cost_calculation', None)
    foundation = ev2.get('foundation')
    if isinstance(foundation, dict):
        foundation.pop('extracted_styles', None)

def _process_images(obj):
    ''' Recursively find "IMAGE_*" attributes and replace base64 data with saved file URLs. '''

    if isinstance(obj, dict):
        for key, value in obj.items():
            if key.startswith('IMAGE_') and isinstance(value, str) and value:
                if len(value) > 100:
                    obj[key] = util.save_file(value, MAX_IMAGE_FILE_SIZE_MB)
            else:
                _process_images(value)
    elif isinstance(obj, list):
        for item in obj:
            _process_images(item)
    else:
        pass

    return

def _resolve_product_cards(obj):
    ''' Updates product card attributes with corresponding current db values. '''

    if isinstance(obj, dict):
        props = obj.get('props')
        if isinstance(props, dict) and 'productId' in props:
            _replace_product_placeholders(props['productId'], props)
        else:
            for value in obj.values():
                _resolve_product_cards(value)
    elif isinstance(obj, list):
        for item in obj:
            _resolve_product_cards(item)

def _replace_product_placeholders(product_id: str, product_obj: dict):
    res = {}    #productapi.details(product_id)
    product = res.get('data')

    if product:
        for key in product_obj.keys():
            match key:
                # product
                case 'title':
                    product_obj[key] = product.get('product_name')
                case 'image':
                    product_obj[key] = product.get('default_image')
                case 'price':
                    product_obj[key] = f"{product.get('currency_symbol')}{product.get('unit_price')}"
                case 'category':
                    product_obj[key] = product.get('category')
                case 'subCategory':
                    product_obj[key] = product.get('sub_category')
                case 'moq':
                    product_obj[key] = product.get('moq')
                case 'leadTime':
                    product_obj[key] = product.get('lead_time')
                case 'rating':
                    product_obj[key] = product.get('average_rating')
                case 'reviewCount':
                    product_obj[key] = product.get('total_review_count')

                # seller
                case 'sellerName':
                    product_obj[key] = product.get('seller_name')
                case 'sellerLogo':
                    product_obj[key] = product.get('seller_logo')
                case 'sellerCity':
                    product_obj[key] = product.get('seller_city')
                case 'sellerState':
                    product_obj[key] = product.get('seller_state')

                case _:
                    pass

def _get_fabric_attributes(attribute_field_name: str, withImgOnly: bool=False):
    filters = {attribute_field_name: ['is', 'set']}
    if withImgOnly:
        filters['has_image_file'] = 1

    rows = frappe.get_all(
        DYED_FABRIC_MASTER_DOCTYPE,
        filters=filters,
        fields=[attribute_field_name],
        distinct=True,
    )

    return '\n'.join([row.get(attribute_field_name or '') for row in rows])

def _get_garment_attributes(attribute_field_name: str, withImgOnly: bool=False):
    filters = {
        attribute_field_name: ['is', 'set'],
        'garment_element': 'Main Body'
    }
    if withImgOnly:
        filters['image_urls'] = ['is', 'set']

    rows = frappe.get_all(
        GARMENT_MASTER_DOCTYPE,
        filters=filters,
        fields=[attribute_field_name],
        distinct=True,
    )

    return '\n'.join([row.get(attribute_field_name or '') for row in rows])

def _get_garment_part_attributes(attribute_field_name: str, withImgOnly: bool=False):
    filters = {
        attribute_field_name: ['is', 'set'],
        'garment_element': 'Main Body'
    }
    if withImgOnly:
        filters['image_urls'] = ['is', 'set']

    rows = frappe.get_all(
        GARMENT_MASTER_DOCTYPE,
        filters=filters,
        fields=[attribute_field_name],
        distinct=True,
    )

    return '\n'.join([row.get(attribute_field_name or '') for row in rows])

def _get_surplus_fabric_combinations(withImgOnly: bool=True):
    '''
    Returns the distinct (construction, blend) combinations that actually exist
    in surplus stock fabric master, as a list of {"construction", "blend"} dicts.
    Only these pairings are valid match targets.

    Args:
        withImgOnly: when True, only consider stock rows that have an image file,
            so the vocabulary offered to the LLM matches what the search can return.
    '''
    #filters = {'has_image_file': 1} if withImgOnly else None

    rows = frappe.get_all(
        SURPLUS_STOCK_ITEM_DOCTYPE,
        #filters=filters,
        fields=['quality as construction', 'blend'],
        distinct=True,
        order_by='quality asc, blend asc',
    )
    return [
        {'construction': row.get('construction'), 'blend': row.get('blend')}
        for row in rows
        if row.get('construction') and row.get('blend')
    ]
