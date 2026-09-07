import json

import frappe
import requests

from prism.auth.authenticator import auth_required
import prism.api.llm as llm
import prism.api.util as util
import prism.lib.cloud as cloud

# Sorting tokens accepted by get_all -> (row field, descending?). 'brand' sorts on the
# brand label (A-Z / Z-A); the two date fields come from the linked Page Builder.
_SORTS = {
    'name_asc':       ('brand', False),            # A-Z (default)
    'name_desc':      ('brand', True),             # Z-A
    'updated_asc':    ('draft_updated_at', False), # last record updated, oldest first
    'updated_desc':   ('draft_updated_at', True),  # last record updated, newest first
    'published_asc':  ('published_at', False),     # last published date, oldest first
    'published_desc': ('published_at', True),      # last published date, newest first
}
_DEFAULT_SORT = 'name_asc'
_VALID_STATUS = ('published', 'draft', 'unpublished', 'not_started')
_DEFAULT_LIMIT = 20
_MAX_LIMIT = 100


@frappe.whitelist(allow_guest=True)
#@auth_required
def get_all(search='', status='', sort='name_asc', limit=20, offset=0):
    '''
    Brands with their Page Builder status. Returns an overall `summary` (counts per
    status across ALL brands — unaffected by the filters, so the dashboard cards stay
    stable) plus a filtered / sorted / paginated page in the same envelope shape as
    prism.api.moodboard_v2.list_moodboards: { total, limit, offset, items }.

    Query params (all optional):
      - search: case-insensitive substring match on the brand name.
      - status: one of published | draft | unpublished | not_started (else: all).
      - sort:   name_asc (default) | name_desc | updated_asc | updated_desc |
                published_asc | published_desc.
      - limit:  page size (default 20, capped 100).
      - offset: page offset (>= 0).

    `status` and the two sort dates are derived from the linked Page Builder, so the
    list is built in full, then filtered/sorted/paged in Python (the summary needs every
    brand counted anyway).
    '''
    try:
        limit, offset = _page(limit, offset)
        search = (search or '').strip().lower()
        status = (status or '').strip()
        sort = sort if sort in _SORTS else _DEFAULT_SORT

        brands = frappe.get_all('Brand',
            fields=['name as id', 'category', 'brand', 'estimated_revenue', 'primary_competitors'],
        )

        # All active page builders, indexed by brand_id.
        page_builders = frappe.get_all('Page Builder',
            filters={'is_active': 1},
            fields=['brand_id', 'status', 'draft_layout_json', 'published_layout_json',
                    'last_published_at', 'modified'],
        )
        pb_by_brand = {pb['brand_id']: pb for pb in page_builders}

        summary = {'total': len(brands), 'published': 0, 'draft': 0, 'unpublished': 0, 'not_started': 0}

        rows = []
        for brand in brands:
            row = _brand_row(brand, pb_by_brand.get(brand['id']))
            summary[row['status']] += 1
            rows.append(row)

        # Filter — search by name + status. (Summary above is over the full set.)
        if search:
            rows = [r for r in rows if search in (r['brand'] or '').lower()]
        if status in _VALID_STATUS:
            rows = [r for r in rows if r['status'] == status]

        total = len(rows)

        # Sort, then page.
        field, descending = _SORTS[sort]
        if field == 'brand':
            rows.sort(key=lambda r: (r.get('brand') or '').lower(), reverse=descending)
        else:
            rows.sort(key=_date_key(field, descending))

        items = rows[offset:offset + limit]

        return {
            'success': True,
            'data': {
                'summary': summary,
                'total': total,
                'limit': limit,
                'offset': offset,
                'items': items,
            }
        }

    except Exception as ex:
        frappe.log_error(frappe.get_traceback(), 'brand.get_all()')
        return {'success': False, 'error': str(ex)}

@frappe.whitelist(allow_guest=True)
#@auth_required
def search(search_str: str, num_max_items: int=10):
    ''' Returns the list of brands matching the search string, sorted by the closest match. '''
    try:
        term = (search_str or '').strip()
        if not term:
            return {'success': True, 'brands': []}

        like_term = f'%{term}%'
        lower_term = term.lower()

        brands = frappe.get_all('Brand',
            or_filters=[
                ['brand', 'like', like_term],
                ['category', 'like', like_term],
            ],
            fields=[
                'name as id',
                'category',
                'brand',
                'estimated_revenue',
                'primary_competitors'
            ]
        )

        def rank(b):
            brand = (b.get('brand') or '').lower()
            category = (b.get('category') or '').lower()
            if brand == lower_term or category == lower_term:
                return (0, brand)
            if brand.startswith(lower_term) or category.startswith(lower_term):
                return (1, brand)
            return (2, brand)

        brands.sort(key=rank)

        return {
            'success': True,
            'brands': brands[:num_max_items]
        }

    except Exception as ex:
        frappe.log_error(frappe.get_traceback(), 'brand.search()')
        return {'success': False, 'error': str(ex)}

@frappe.whitelist(allow_guest=True)
@auth_required
def details(brand_id: str):
    return _details(brand_id)

def _details(brand_id: str):
    ''' Returns the brand record as json against the 'brand_id' '''
    try:
        brand_id = (brand_id or '').strip()
        if not brand_id or not frappe.db.exists('Brand', brand_id):
            return {'success': False, 'error': f'Brand "{brand_id}" not found.'}

        row = frappe.db.get_value('Brand', brand_id,
            [
                'name as brand_id', 
                'brand as brand_name', 
                'category',
                'estimated_revenue', 
                'all_moodboard_permission',
                'primary_competitors', 
                'style_overview',
                'brand_preferences'
            ],
            as_dict=True,
        )
        row['style_overview'] = frappe.parse_json(row['style_overview']) if row.get('style_overview') else None
        row['brand_preferences'] = frappe.parse_json(row['brand_preferences']) if row.get('brand_preferences') else None

        return {
            'success': True,
            'data': row,
        }

    except Exception as ex:
        frappe.log_error(frappe.get_traceback(), 'brand.details()')
        return {'success': False, 'error': str(ex)}

@frappe.whitelist(allow_guest=True)
@auth_required
def theme(brand_id: str):
    ''' Returns the brand's website + theme JSON for the given brand_id
        (passed as a query param). Internal (non-brand) users only. '''
    try:
        # brand/external JWT sessions carry a brand; internal/PSL users don't.
        if util.get_current_brand():
            frappe.throw('This endpoint is for internal users only.', frappe.PermissionError)

        brand_id = (brand_id or '').strip()
        if not brand_id or not frappe.db.exists('Brand', brand_id):
            return {'success': False, 'error': f'Brand "{brand_id}" not found.'}

        row = frappe.db.get_value('Brand', brand_id,
            [
                'name as brand_id',
                'brand as brand_name',
                'logo',
                'website',
                'brand_theme',
            ],
            as_dict=True,
        )
        # The Brand controller offloads the logo on save, so this is already a
        # servable S3 URL; normalise anyway so a bare key resolves too.
        row['logo'] = cloud.asset_url(row['logo']) if row.get('logo') else None
        row['brand_theme'] = frappe.parse_json(row['brand_theme']) if row.get('brand_theme') else None

        return {'success': True, 'data': row}

    except frappe.PermissionError:
        raise  # let Frappe return a 403 rather than masking it as success=False
    except Exception as ex:
        frappe.log_error(frappe.get_traceback(), 'brand.theme()')
        return {'success': False, 'error': str(ex)}

# tools
@frappe.whitelist(allow_guest=True)
@auth_required
def update_style_profile(brand_id: str):
    return _update_style_profile(brand_id)

def _update_style_profile(brand_id: str):
    try:
        if not frappe.db.exists('Brand', brand_id):
            return {'success': False, 'error': f'Brand "[{brand_id}]" not found!'}

        # the Style Overview
        gap_row = _details('gap')['data']
        gap_style_json = frappe.parse_json(gap_row['style_overview'])

        brand_row = _details(brand_id)['data']
        brand_name = brand_row['brand_name']

        system_prompt = '''You are a fashion industry research assistant specializing in brand intelligence for garment and apparel brands.

Your role is to generate structured JSON data that captures the garment identity of a fashion brand. This data will be used downstream to guide an image generation AI in producing accurate, brand-specific moodboards — so precision and brand authenticity are critical. The output must reflect only what the brand actually produces: no generic, aspirational, or off-brand garment types should be included.

When given a brand name and a reference JSON schema, you must:
1. Populate every field in the schema with accurate, brand-specific latest information.
2. Preserve the schema exactly — do not add, remove, or rename any fields.
3. Base your output strictly on the brand's actual product lines, aesthetics, target demographics, materials, price positioning, and visual style cues.

Respond only with the JSON object. No explanations, no preamble, no markdown formatting outside the JSON block.'''

        user_prompt = f'''Generate a brand intelligence JSON for {brand_name} using the schema below, which was defined using GAP as the reference brand.

Follow the schema exactly — same fields, same structure, same nesting.

Reference schema (GAP):
=====
{json.dumps(gap_style_json, indent=2)}
=====

Now produce the equivalent JSON for {brand_name}.
'''

        brand_style_json = llm.get_claude_response(system_prompt, user_prompt, 'dict')

        frappe.db.set_value('Brand',
                            brand_id,
                            'style_overview',
                            frappe.as_json(brand_style_json)
        )
        frappe.db.commit()

        return {'success': True, 'data': brand_style_json}

    except Exception as ex:
        frappe.log_error(frappe.get_traceback(), 'brand.update_style_profile()')
        return {'success': False, 'error': str(ex)}


# Reference schema for the brand theme. The LLM must mirror this shape exactly.
_BRAND_THEME_SCHEMA = {
    "style": ["luxury", "minimal", "editorial", "timeless", "high-fashion"],
    "palette": {
        "background": {"name": "White", "hex": "#FFFFFF"},
        "surface": {"name": "White", "hex": "#FFFFFF"},
        "primary": {"name": "Black", "hex": "#000000"},
        "secondary": {"name": "Dark Gray", "hex": "#4A4A4A"},
        "text": {"name": "Charcoal", "hex": "#111111"},
        "textMuted": {"name": "Medium Gray", "hex": "#6F6F6F"},
        "border": {"name": "Light Gray", "hex": "#E5E5E5"},
    },
    "components": {
        "buttons": "minimal monochrome with subtle hover transitions",
        "cards": "borderless, image-first, no shadows",
    },
    "imagery": {
        "focus": "editorial fashion photography",
        "lighting": "soft natural and studio lighting",
        "background": "neutral, white, beige, or architectural",
        "mood": "luxurious, sophisticated, aspirational",
    },
    "overallMood": ["elegant", "refined", "exclusive", "modern", "confident", "minimal"],
}


@frappe.whitelist(allow_guest=True)
@auth_required
def update_brand_theme(brand_id: str, force: bool = False):
    return _update_brand_theme(brand_id, force=frappe.utils.cint(force))

@frappe.whitelist(methods=['POST'])
def regenerate_brand_theme(brand_id=None):
    '''
    "Update Theme" for ONE brand, driven by the Desk form button. Re-extracts the
    theme from the brand's website and overwrites any existing one — a manual run
    is always an explicit regeneration, so it skips the "already set" guard.

    Separate from update_brand_theme() because that one is @auth_required (JWT,
    for the SPA); Desk authenticates by session cookie and would fail its header
    check. Session-authed + System Manager only, mirroring
    moodboard_v2.regenerate_moodboard_thumbnail.
    '''
    frappe.only_for('System Manager')

    if not brand_id:
        frappe.throw('brand_id is required')
    if not frappe.db.exists('Brand', brand_id):
        frappe.throw(f'Brand "{brand_id}" not found!')

    return _update_brand_theme(brand_id, force=True)

def _update_brand_theme(brand_id: str, force: bool = False):
    ''' Extract the brand's visual theme from its website via the LLM and store
        it on `brand_theme`. Requires a website. By default never overwrites an
        existing theme; pass force=True to regenerate (e.g. after a website
        redesign). Normally run in the background from Brand.on_update. '''
    lock_key = f'brand_theme_lock:{brand_id}'
    try:
        if not frappe.db.exists('Brand', brand_id):
            return {'success': False, 'error': f'Brand "[{brand_id}]" not found!'}

        website, brand_theme, brand_name = frappe.db.get_value(
            'Brand', brand_id, ['website', 'brand_theme', 'brand']
        )

        # guard the preconditions again at run time — the state may have
        # changed between enqueue and execution. `force` skips the "already
        # set" guard to allow an explicit regeneration.
        if brand_theme and not force:
            return {'success': False, 'error': 'Brand theme already set.'}
        if not website:
            return {'success': False, 'error': 'Brand has no website.'}

        site_html = _fetch_website_text(website)

        system_prompt = '''You are a brand design analyst. Given a fashion/apparel brand's website, you extract its visual identity into a structured JSON "theme" that will be used downstream to style UI and guide image generation.

When given a brand name, its website URL, the raw HTML of its homepage (when available), and a reference JSON schema, you must:
1. Populate every field in the schema based on the brand's actual visual identity — colours, typography feel, component styling, imagery and overall mood.
2. Prefer evidence from the provided HTML (inline styles, CSS colours, hero copy, imagery cues); fall back to your knowledge of the brand only where the HTML is thin or missing.
3. Preserve the schema exactly — same fields, same structure, same nesting. Every palette colour must include a human-readable "name" and a valid "#RRGGBB" hex value.

Respond only with the JSON object. No explanations, no preamble, no markdown formatting outside the JSON block.'''

        html_block = (
            f'''Homepage HTML (truncated):
=====
{site_html}
=====
'''
            if site_html
            else 'Homepage HTML could not be retrieved — infer the theme from the brand and URL.\n'
        )

        user_prompt = f'''Extract the brand theme JSON for "{brand_name}" (website: {website}).

Follow the schema exactly — same fields, same structure, same nesting.

Reference schema:
=====
{json.dumps(_BRAND_THEME_SCHEMA, indent=2)}
=====

{html_block}
Now produce the equivalent theme JSON for "{brand_name}".
'''

        theme_json = llm.get_claude_response(system_prompt, user_prompt, 'dict')

        frappe.db.set_value('Brand', brand_id, 'brand_theme', frappe.as_json(theme_json))
        frappe.db.commit()

        return {'success': True, 'data': theme_json}

    except Exception as ex:
        frappe.log_error(frappe.get_traceback(), 'brand.update_brand_theme()')
        return {'success': False, 'error': str(ex)}
    finally:
        frappe.cache.delete_value(lock_key)

def _fetch_website_text(url: str, max_chars: int = 30000) -> str:
    ''' Best-effort fetch of a brand homepage's HTML for theme extraction.
        Returns '' if the page can't be retrieved. '''
    try:
        if not url.startswith(('http://', 'https://')):
            url = f'https://{url}'
        resp = requests.get(
            url,
            timeout=15,
            headers={'User-Agent': 'Mozilla/5.0 (compatible; PrismBrandBot/1.0)'},
        )
        resp.raise_for_status()
        return resp.text[:max_chars]
    except Exception:
        frappe.log_error(frappe.get_traceback(), 'brand.fetch_website()')
        return ''


#--- helpers ---
def _brand_row(brand, pb):
    ''' One brand row + its derived Page Builder status (status logic unchanged). '''
    if not pb:
        status = 'not_started'
        has_published = has_draft = False
        section_count = 0
        preview_image = published_at = draft_updated_at = None
    else:
        has_published = bool(pb.get('published_layout_json'))
        has_draft = bool(pb.get('draft_layout_json'))

        if pb.get('status') == 'Published':
            status = 'published'
        elif has_published:
            # previously published, now reverted to Draft
            status = 'unpublished'
        else:
            status = 'draft'

        raw_layout = pb.get('published_layout_json')
        layout = frappe.parse_json(raw_layout) if raw_layout else {}
        section_count = len(layout.get('layout', []))
        preview_image = layout.get('IMAGE_thumbnail')
        published_at = pb.get('last_published_at')
        draft_updated_at = pb.get('modified')

    return {
        'id': brand['id'],
        'category': brand['category'],
        'brand': brand['brand'],
        'estimated_revenue': brand['estimated_revenue'],
        'primary_competitors': brand['primary_competitors'],
        'status': status,
        'has_published': has_published,
        'has_draft': has_draft,
        'section_count': section_count,
        'preview_image': preview_image,
        'published_at': published_at,
        'draft_updated_at': draft_updated_at,
    }

def _page(limit, offset):
    ''' Coerce request limit/offset to safe ints: limit in [1, _MAX_LIMIT], offset >= 0. '''
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        limit = _DEFAULT_LIMIT
    try:
        offset = int(offset)
    except (TypeError, ValueError):
        offset = 0
    limit = max(1, min(limit, _MAX_LIMIT))
    offset = max(0, offset)
    return limit, offset

def _date_key(field, descending):
    '''
    Sort key for a datetime field. Rows missing the date always sort to the END (in
    both directions); present rows order by timestamp asc/desc.
    '''
    def key(row):
        value = row.get(field)
        if value is None:
            return (1, 0.0)
        ts = frappe.utils.get_datetime(value).timestamp()
        return (0, -ts if descending else ts)
    return key
