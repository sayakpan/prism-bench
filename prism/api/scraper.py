import hashlib
import io
import re

import frappe

import openpyxl

from prism.auth.authenticator import auth_required
import prism.lib.cloud as cloud
from prism.prism.doctype.scraper_brand.scraper_brand import (
    RESUMABLE_STATUSES,
    normalize_website,
    slugify,
)
from prism.prism.doctype.scraper_brand_version.scraper_brand_version import (
    normalize_html_files,
    record_version,
)

MAX_EXCEL_FILE_SIZE_MB = 25
ALLOWED_EXCEL_EXTENSIONS = ('.xlsx', '.xlsm')

DEFAULT_VERSION_LIMIT = 5
MAX_VERSION_LIMIT = 20

# A failure reason is for a human to read, not a place to dump a full traceback
# (those belong in the Error Log) -- keep the brand row small.
MAX_ENGINE_ERROR_LENGTH = 2000

# Rows are committed in chunks so a large workbook keeps partial progress instead
# of holding one transaction open for the whole import.
INGEST_COMMIT_CHUNK = 500
MAX_REPORTED_ERRORS = 50

# Scraped-sheet header (normalised) -> Scraper Product fieldname. Headers not
# listed here are not dropped -- they land in the row's `extra_data` JSON.
COLUMN_MAP = {
    'product_url': 'product_url',
    'product_name': 'product_name',
    'brand': 'brand_label',
    'original_price': 'original_price_raw',
    'price': 'price_raw',
    'gender': 'gender',
    'category': 'category',
    'subcategories': 'subcategories',
    'available_colors': 'available_colors',
    'sizes': 'sizes',
    'fit': 'fit',
    'about': 'about',
    'product_details': 'product_details',
    'composition': 'composition',
    'image_url': 'image_url',
    'sustainability_certifications': 'sustainability_certifications',
    'certificate': 'certificate',
    'esg': 'esg',
    'rating': 'rating',
    'reviews': 'reviews',
    'review_count': 'review_count',
    'origin': 'origin',
    'best_seller_selling': 'best_seller',
    'best_seller': 'best_seller',
    'product_id': 'product_id',
    'care': 'care',
}

SHEET_FIELDS = sorted(set(COLUMN_MAP.values()))

# Fieldtype Data is varchar(140) -- these get truncated rather than blowing up
# the insert. Long free text (urls, about, reviews) uses Text fields instead.
DATA_FIELDS = (
    'product_id', 'product_name', 'brand_label', 'gender', 'category',
    'origin', 'best_seller', 'currency', 'price_raw', 'original_price_raw',
    'source_file',
)
DATA_FIELD_LENGTH = 140

# Frappe's numeric columns are NOT NULL DEFAULT 0. Document.insert() coerces None
# to 0 for them, but the db.set_value() used on the update path writes raw SQL and
# would reject it -- so a row missing a price would import fine and then fail on
# re-scrape. Coerce here instead, and both paths behave the same.
NUMERIC_FIELDS = ('price', 'original_price', 'rating', 'review_count')

# Explicit read shape, so the API never leaks Frappe's internal columns
# (_user_tags, _comments, docstatus, idx...) into the scraper's payloads.
PRODUCT_READ_FIELDS = (
    'name', 'scraper_brand', 'product_id', 'product_name', 'product_url',
    'brand_label', 'gender', 'category', 'subcategories',
    'price', 'original_price', 'currency', 'price_raw', 'original_price_raw',
    'available_colors', 'sizes', 'fit', 'composition', 'care',
    'origin', 'best_seller', 'rating', 'review_count',
    'about', 'product_details', 'image_url', 'reviews',
    'sustainability_certifications', 'certificate', 'esg',
    'product_key', 'source_file', 'scraped_at', 'extra_data',
    'creation', 'modified',
)

# Sheet values that mean "nothing here". The scraper writes the literal text
# "None" for missing attributes, which must not be stored as content.
NULL_TOKENS = {'', '-', '--', 'n/a', 'na', 'nan', 'none', 'null', 'undefined'}

_PRICE_NUMBER_RE = re.compile(r'-?\d[\d,]*(?:\.\d+)?')
_CURRENCY_RE = re.compile(r'[^\d\s.,+-]+')
_NUMBER_RE = re.compile(r'-?\d[\d,]*(?:\.\d+)?')


# --- brand ---
@frappe.whitelist(allow_guest=True)
@auth_required
def create_brand(brand_name: str, website: str):
    '''
    Registers a brand for the scraper. Idempotent on the normalised website (and
    on the brand name), so re-running the engine against the same site returns
    the existing brand instead of forking a second one -- the response's
    `created` flag says which happened.
    '''
    try:
        brand_name = (brand_name or '').strip()
        site = normalize_website(website)

        if not brand_name:
            return {'success': False, 'error': 'Brand name is required!'}
        if not site:
            return {'success': False, 'error': 'Website is required!'}

        existing = frappe.db.exists('Scraper Brand', {'brand_name': brand_name}) \
            or frappe.db.exists('Scraper Brand', {'website': site})

        if existing:
            return {
                'success': True,
                'created': False,
                'data': _brand_payload(frappe.get_doc('Scraper Brand', existing)),
            }

        doc = frappe.new_doc('Scraper Brand')
        doc.brand_name = brand_name
        doc.website = site
        doc.status = 'New'
        doc.insert(ignore_permissions=True)
        frappe.db.commit()

        return {'success': True, 'created': True, 'data': _brand_payload(doc)}

    except Exception as ex:
        frappe.db.rollback()
        frappe.log_error(frappe.get_traceback(), 'scraper.create_brand()')
        return {'success': False, 'error': str(ex)}


@frappe.whitelist(allow_guest=True)
@auth_required
def get_brands(search: str = None, page: int = 1, page_size: int = 20):
    ''' Paginated brand list with stats. '''
    try:
        page, page_size = _paging(page, page_size, max_size=100)

        filters = {}
        if search:
            filters['brand_name'] = ['like', f'%{search}%']

        total = frappe.db.count('Scraper Brand', filters)
        names = frappe.get_all(
            'Scraper Brand',
            filters=filters,
            pluck='name',
            order_by='modified desc',
            limit_start=(page - 1) * page_size,
            limit_page_length=page_size,
        )

        return {
            'success': True,
            'data': {
                'brands': [_brand_payload(frappe.get_doc('Scraper Brand', n)) for n in names],
                'total': total,
                'page': page,
                'page_size': page_size,
            },
        }

    except Exception as ex:
        frappe.log_error(frappe.get_traceback(), 'scraper.get_brands()')
        return {'success': False, 'error': str(ex)}


@frappe.whitelist(allow_guest=True)
@auth_required
def get_brand(brand: str):
    ''' One brand with its stats and scraped-file links. '''
    try:
        return {'success': True, 'data': _brand_payload(_resolve_brand(brand))}
    except Exception as ex:
        frappe.log_error(frappe.get_traceback(), 'scraper.get_brand()')
        return {'success': False, 'error': str(ex)}


@frappe.whitelist(allow_guest=True)
@auth_required
def mark_engine_started(brand: str):
    '''
    Stamps `last_engine_started_at`. Called by the frontend as the AI engine boots.

    Also clears any `last_engine_error` left by a previous run -- once a new run
    starts, the old failure reason is stale and would otherwise sit on the brand
    looking current.
    '''
    try:
        doc = _resolve_brand(brand)
        doc.last_engine_started_at = frappe.utils.now()
        doc.last_engine_error = None
        doc.status = 'Engine Running'
        doc.save(ignore_permissions=True)
        frappe.db.commit()

        return {'success': True, 'data': _brand_payload(doc)}

    except Exception as ex:
        frappe.db.rollback()
        frappe.log_error(frappe.get_traceback(), 'scraper.mark_engine_started()')
        return {'success': False, 'error': str(ex)}


@frappe.whitelist(allow_guest=True)
@auth_required
def mark_engine_finished(brand: str, ok=1, message: str = None):
    '''
    Closes an engine run -- the bookend to mark_engine_started.

    A run that posts a version closes itself (create_version stamps
    `last_engine_finished_at` and moves the status on), so this is for every
    other ending: the engine crashed, the user aborted, or it finished without
    producing anything. Without it a dead run leaves the brand on
    "Engine Running" forever, and the "Failed" status is unreachable.

    `ok=0` records the failure with its reason in `last_engine_error`; `ok=1`
    clears any previous error. Safe to call more than once, and safe to call
    after create_version has already closed the run.
    '''
    try:
        doc = _resolve_brand(brand)

        succeeded = _as_bool(ok)
        doc.last_engine_finished_at = frappe.utils.now()

        if succeeded:
            doc.last_engine_error = None
            # Only advance a brand that is mid-run or in an error state, so closing
            # a re-run never drags an already-Scraped brand backwards -- but a
            # success does clear Failed, which would otherwise be stuck alongside
            # the error message this just wiped.
            if doc.status in RESUMABLE_STATUSES:
                doc.status = 'Prototype Ready'
        else:
            doc.last_engine_error = (message or 'Engine run failed.')[:MAX_ENGINE_ERROR_LENGTH]
            doc.status = 'Failed'

        doc.save(ignore_permissions=True)
        frappe.db.commit()

        return {'success': True, 'data': _brand_payload(doc)}

    except Exception as ex:
        frappe.db.rollback()
        frappe.log_error(frappe.get_traceback(), 'scraper.mark_engine_finished()')
        return {'success': False, 'error': str(ex)}


# --- versions ---
@frappe.whitelist(allow_guest=True)
@auth_required
def get_versions(brand: str, limit: int = DEFAULT_VERSION_LIMIT, include_content=1):
    '''
    The GET the AI engine calls on every run: the brand's stats plus its most
    recent versions, newest first, each carrying the prototype JSON, the HTML
    files and the scraping code.

    `limit` defaults to the last 5 and is capped at 20 -- versions are heavy
    (5-6 HTML bodies each), so an unbounded fetch is never served. Pass
    `include_content=0` for just the version headers.
    '''
    try:
        doc = _resolve_brand(brand)
        limit = max(1, min(int(limit or DEFAULT_VERSION_LIMIT), MAX_VERSION_LIMIT))
        with_content = _as_bool(include_content)

        rows = frappe.get_all(
            'Scraper Brand Version',
            filters={'scraper_brand': doc.name},
            fields=[
                'name', 'version_no', 'parent_version', 'source', 'actor',
                'content_hash', 'change_summary', 'creation',
            ],
            order_by='version_no desc, creation desc',
            limit_page_length=limit,
        )

        versions = [_version_payload(row, with_content) for row in rows]

        return {
            'success': True,
            'data': {
                'brand': _brand_payload(doc),
                'versions': versions,
                'latest': versions[0] if versions else None,
            },
        }

    except Exception as ex:
        frappe.log_error(frappe.get_traceback(), 'scraper.get_versions()')
        return {'success': False, 'error': str(ex)}


@frappe.whitelist(allow_guest=True)
@auth_required
def create_version(brand: str, prototype_json=None, html_files=None,
                   scraper_code: str = None, source: str = 'AI Engine'):
    '''
    Posts the engine's output as a new version.

    The engine sends all three artefacts on every run whether or not it changed
    them, so identical content is de-duplicated against the latest version: no
    row is inserted and `created` comes back False. `html_files` accepts either
    a list of {file_name, content} or a {filename: content} mapping.
    '''
    try:
        doc = _resolve_brand(brand)

        if prototype_json in (None, '') and html_files in (None, '') and not scraper_code:
            return {'success': False, 'error': 'Nothing to version -- prototype, html files and code are all empty!'}

        version, created = record_version(
            doc,
            prototype_json=prototype_json,
            html_files=html_files,
            scraper_code=scraper_code,
            source=source or 'AI Engine',
        )
        frappe.db.commit()

        return {
            'success': True,
            'created': created,
            'data': {
                'brand': _brand_payload(frappe.get_doc('Scraper Brand', doc.name)),
                'version': _version_payload(version.as_dict(), True),
            },
        }

    except Exception as ex:
        frappe.db.rollback()
        frappe.log_error(frappe.get_traceback(), 'scraper.create_version()')
        return {'success': False, 'error': str(ex)}


@frappe.whitelist(allow_guest=True)
@auth_required
def get_version(version: str):
    ''' One version in full, by name. '''
    try:
        if not frappe.db.exists('Scraper Brand Version', version):
            return {'success': False, 'error': 'Version does not exist!'}

        doc = frappe.get_doc('Scraper Brand Version', version)
        return {'success': True, 'data': _version_payload(doc.as_dict(), True)}

    except Exception as ex:
        frappe.log_error(frappe.get_traceback(), 'scraper.get_version()')
        return {'success': False, 'error': str(ex)}


@frappe.whitelist(allow_guest=True)
@auth_required
def restore_version(brand: str, version: str):
    '''
    Rolls an older version forward as a new one. History is never mutated: the
    old content is re-posted, so it becomes the newest version and the brand's
    `current_version`.
    '''
    try:
        doc = _resolve_brand(brand)

        if not frappe.db.exists('Scraper Brand Version', version):
            return {'success': False, 'error': 'Version does not exist!'}

        source_version = frappe.get_doc('Scraper Brand Version', version)
        if source_version.scraper_brand != doc.name:
            return {'success': False, 'error': 'Version does not belong to this brand!'}

        restored, created = record_version(
            doc,
            prototype_json=source_version.prototype_json,
            html_files=source_version.html_files,
            scraper_code=source_version.scraper_code,
            source='Restored',
        )
        frappe.db.commit()

        return {
            'success': True,
            'created': created,
            'data': {
                'restored_from': version,
                'version': _version_payload(restored.as_dict(), True),
            },
        }

    except Exception as ex:
        frappe.db.rollback()
        frappe.log_error(frappe.get_traceback(), 'scraper.restore_version()')
        return {'success': False, 'error': str(ex)}


# --- scraped data ---
@frappe.whitelist(allow_guest=True)
@auth_required
def upload_scrape(brand: str, partial=0):
    '''
    Ingests a scraped workbook. Send as multipart/form-data with `brand` (form
    field or query param) and the workbook as the `file` part.

    Each sheet row becomes one Scraper Product, upserted on
    `<brand>::<product_id>` so a re-scrape updates rows in place instead of
    duplicating them. The original file is stored on S3 as
    `<brand slug>_<datetime>.xlsx` and its link appended to the brand's
    `scrape_files` JSON, along with `last_data_scraped_at`.

    A malformed row is recorded in `errors` and skipped -- one bad row never
    fails the whole import.
    '''
    try:
        doc = _resolve_brand(brand)

        files = getattr(frappe.request, 'files', None) or {}
        fileobj = next((files[name] for name in ('file', 'excel', 'data_file') if files.get(name)), None)
        if not fileobj:
            return {'success': False, 'error': 'No file uploaded (expected multipart field "file").'}

        raw = fileobj.stream.read()
        if not raw:
            return {'success': False, 'error': 'Uploaded file is empty!'}

        size_mb = len(raw) / (1024 * 1024)
        if size_mb > MAX_EXCEL_FILE_SIZE_MB:
            return {
                'success': False,
                'error': f'File is {size_mb:.1f} MB, over the {MAX_EXCEL_FILE_SIZE_MB} MB limit.',
            }

        original_name = getattr(fileobj, 'filename', '') or ''
        ext = cloud.file_ext(original_name) or '.xlsx'
        if ext not in ALLOWED_EXCEL_EXTENSIONS:
            return {'success': False, 'error': f'Unsupported file type "{ext}" -- expected .xlsx.'}

        # The scraper marks incomplete runs in the file name (…_partial_…); keep
        # that signal rather than silently treating it as a full scrape.
        is_partial = _as_bool(partial) or '_partial' in original_name.lower()

        records = _read_workbook(raw)
        if not records:
            return {'success': False, 'error': 'No data rows found in the sheet!'}

        scraped_at = frappe.utils.now_datetime()
        file_name = _scrape_file_name(doc.slug or slugify(doc.brand_name), scraped_at, is_partial, ext)
        key = cloud.build_scraper_file_key(doc.name, file_name)
        cloud.upload_file(io.BytesIO(raw), key, cloud.content_type_for(file_name))
        file_url = cloud.asset_url(key)

        result = _ingest_rows(doc, records, file_name, scraped_at)

        entry = {
            'file_name': file_name,
            'url': file_url,
            'uploaded_at': str(scraped_at),
            'row_count': len(records),
            'partial': is_partial,
        }

        scrape_files = frappe.parse_json(doc.scrape_files) or []
        if not isinstance(scrape_files, list):
            scrape_files = []
        scrape_files.append(entry)

        doc.scrape_files = frappe.as_json(scrape_files)
        doc.last_data_scraped_at = scraped_at
        doc.last_scrape_row_count = len(records)
        doc.total_products = frappe.db.count('Scraper Product', {'scraper_brand': doc.name})
        doc.status = 'Scraped'
        doc.save(ignore_permissions=True)
        frappe.db.commit()

        return {
            'success': True,
            'data': {
                'brand': _brand_payload(doc),
                'file': entry,
                'row_count': len(records),
                'created': result['created'],
                'updated': result['updated'],
                'skipped': result['skipped'],
                'error_count': result['error_count'],
                'errors': result['errors'],
            },
        }

    except Exception as ex:
        frappe.db.rollback()
        frappe.log_error(frappe.get_traceback(), 'scraper.upload_scrape()')
        return {'success': False, 'error': str(ex)}


@frappe.whitelist(allow_guest=True)
@auth_required
def get_products(brand: str, search: str = None, page: int = 1, page_size: int = 50):
    ''' Paginated read-back of a brand's scraped products. '''
    try:
        doc = _resolve_brand(brand)
        page, page_size = _paging(page, page_size, max_size=200)

        filters = {'scraper_brand': doc.name}
        or_filters = None
        if search:
            like = ['like', f'%{search}%']
            or_filters = {'product_name': like, 'product_id': like, 'category': like}

        total = _count_products(filters, or_filters)
        products = frappe.get_all(
            'Scraper Product',
            filters=filters,
            or_filters=or_filters,
            fields=list(PRODUCT_READ_FIELDS),
            order_by='modified desc',
            limit_start=(page - 1) * page_size,
            limit_page_length=page_size,
        )

        for product in products:
            product['extra_data'] = frappe.parse_json(product.get('extra_data')) or {}

        return {
            'success': True,
            'data': {
                'brand': doc.name,
                'products': products,
                'total': total,
                'page': page,
                'page_size': page_size,
            },
        }

    except Exception as ex:
        frappe.log_error(frappe.get_traceback(), 'scraper.get_products()')
        return {'success': False, 'error': str(ex)}


@frappe.whitelist(allow_guest=True)
@auth_required
def get_scrape_files(brand: str):
    ''' Just the brand's stored scraped-file links, newest last. '''
    try:
        doc = _resolve_brand(brand)
        return {
            'success': True,
            'data': {
                'brand': doc.name,
                'last_data_scraped_at': doc.last_data_scraped_at,
                'files': frappe.parse_json(doc.scrape_files) or [],
            },
        }

    except Exception as ex:
        frappe.log_error(frappe.get_traceback(), 'scraper.get_scrape_files()')
        return {'success': False, 'error': str(ex)}


# --- workbook parsing ---
def _read_workbook(raw):
    '''
    Sheet bytes -> list of per-row dicts keyed by Scraper Product fieldname.

    The header row drives the mapping, so a column order change in the scraper
    is harmless. Columns with no mapped field are collected into the row's
    `extra_data` instead of being dropped.
    '''
    workbook = openpyxl.load_workbook(io.BytesIO(raw), read_only=True, data_only=True)
    try:
        sheet = workbook.active
        rows = sheet.iter_rows(values_only=True)

        header = next(rows, None)
        if not header:
            frappe.throw('The sheet has no header row!')

        mapped, unmapped = {}, {}
        for idx, cell in enumerate(header):
            label = _clean(cell)
            if label is None:
                continue
            field = COLUMN_MAP.get(_norm_header(label))
            if field:
                mapped[idx] = field
            else:
                unmapped[idx] = str(label)

        if not mapped:
            frappe.throw('No recognised columns in the sheet header!')

        records = []
        for values in rows:
            if values is None or all(_clean(v) is None for v in values):
                continue

            record, extra = {}, {}
            for idx, value in enumerate(values):
                cleaned = _clean(value)
                if idx in mapped:
                    record[mapped[idx]] = cleaned
                elif idx in unmapped and cleaned is not None:
                    extra[unmapped[idx]] = cleaned

            record['extra_data'] = extra
            records.append(record)

        return records

    finally:
        workbook.close()


def _ingest_rows(brand_doc, records, file_name, scraped_at):
    ''' Upsert every parsed row, collecting (not raising) per-row failures. '''
    existing = {
        row.product_key: row.name
        for row in frappe.get_all(
            'Scraper Product',
            filters={'scraper_brand': brand_doc.name},
            fields=['name', 'product_key'],
            limit_page_length=0,
        )
        if row.product_key
    }

    created = updated = skipped = 0
    errors = []
    processed = 0

    # Sheet row numbers start at 2 -- row 1 is the header.
    for offset, record in enumerate(records, start=2):
        try:
            values = _to_product_values(brand_doc, record, file_name, scraped_at)
            if not values:
                skipped += 1
                _add_error(errors, offset, 'Row has neither a product_id nor a product_url')
                continue

            name = existing.get(values['product_key'])
            if name:
                frappe.db.set_value('Scraper Product', name, values, update_modified=True)
                updated += 1
            else:
                product = frappe.get_doc(dict(doctype='Scraper Product', **values))
                product.insert(ignore_permissions=True)
                existing[values['product_key']] = product.name
                created += 1

        except Exception as ex:
            skipped += 1
            _add_error(errors, offset, str(ex))

        processed += 1
        if processed % INGEST_COMMIT_CHUNK == 0:
            frappe.db.commit()

    frappe.db.commit()

    return {
        'created': created,
        'updated': updated,
        'skipped': skipped,
        'error_count': skipped,
        'errors': errors,
    }


def _to_product_values(brand_doc, record, file_name, scraped_at):
    ''' One parsed row -> the Scraper Product field values, or None if unidentifiable. '''
    values = {field: _stringify(record.get(field)) for field in SHEET_FIELDS}

    price, currency = _parse_price(values.get('price_raw'))
    original_price, original_currency = _parse_price(values.get('original_price_raw'))

    values['price'] = price
    values['original_price'] = original_price
    values['currency'] = currency or original_currency
    values['rating'] = _to_float(record.get('rating'))
    values['review_count'] = _to_int(record.get('review_count'))

    # product_id is the natural key; a sheet missing it still upserts stably on
    # a hash of the product url.
    identifier = values.get('product_id') or _url_identifier(values.get('product_url'))
    if not identifier:
        return None

    values['scraper_brand'] = brand_doc.name
    values['product_key'] = f'{brand_doc.name}::{identifier}'[:DATA_FIELD_LENGTH]
    values['source_file'] = file_name
    values['scraped_at'] = scraped_at

    extra = record.get('extra_data') or {}
    values['extra_data'] = frappe.as_json(extra) if extra else None

    for field in DATA_FIELDS:
        if isinstance(values.get(field), str):
            values[field] = values[field][:DATA_FIELD_LENGTH]

    for field in NUMERIC_FIELDS:
        if values.get(field) is None:
            values[field] = 0

    return values


def _clean(value):
    '''
    Sheet cell -> usable value, or None. Blank cells and the placeholder text the
    scraper writes for missing attributes ("None", "N/A", "-") both collapse to
    None so they are never stored as content.
    '''
    if value is None:
        return None
    if isinstance(value, str):
        text = value.strip()
        return None if text.lower() in NULL_TOKENS else text
    return value


def _stringify(value):
    ''' Cell value as text, without the ".0" openpyxl adds to whole numbers. '''
    if value is None:
        return None
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    if isinstance(value, str):
        return value
    return str(value)


def _norm_header(label):
    ''' "Best seller/Selling" -> "best_seller_selling" so header casing/punctuation stops mattering. '''
    return re.sub(r'_+', '_', re.sub(r'[^a-z0-9]+', '_', str(label).lower())).strip('_')


def _parse_price(raw):
    ''' "$82.00" -> (82.0, "$"). Returns (None, None) when there is no number. '''
    if raw in (None, ''):
        return None, None
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        return float(raw), None

    text = str(raw)
    number = _PRICE_NUMBER_RE.search(text)
    symbol = _CURRENCY_RE.search(text)

    amount = float(number.group().replace(',', '')) if number else None
    return amount, (symbol.group().strip() if symbol else None)


def _to_float(raw):
    if raw in (None, ''):
        return None
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        return float(raw)
    match = _NUMBER_RE.search(str(raw))
    return float(match.group().replace(',', '')) if match else None


def _to_int(raw):
    value = _to_float(raw)
    return int(value) if value is not None else None


def _url_identifier(url):
    ''' Stable short key for a product with no id of its own. '''
    if not url:
        return None
    return hashlib.sha1(str(url).encode('utf-8')).hexdigest()[:16]


def _scrape_file_name(slug, moment, is_partial, ext):
    ''' <brand slug>[_partial]_<YYYY-MM-DD_HH-MM-SS>.xlsx '''
    marker = '_partial' if is_partial else ''
    return f'{slug}{marker}_{moment.strftime("%Y-%m-%d_%H-%M-%S")}{ext}'


# --- shared ---
def _resolve_brand(brand):
    '''
    Accepts the Scraper Brand docname, its brand name, or its website, so the
    frontend can address a brand by whatever it happens to hold.
    '''
    ident = (brand or '').strip()
    if not ident:
        frappe.throw('Brand is required!')

    if frappe.db.exists('Scraper Brand', ident):
        return frappe.get_doc('Scraper Brand', ident)

    name = frappe.db.exists('Scraper Brand', {'brand_name': ident}) \
        or frappe.db.exists('Scraper Brand', {'website': normalize_website(ident)})

    if not name:
        frappe.throw(f'Scraper Brand "{ident}" does not exist!')

    return frappe.get_doc('Scraper Brand', name)


def _brand_payload(doc):
    return {
        'name': doc.name,
        'brand_name': doc.brand_name,
        'website': doc.website,
        'slug': doc.slug,
        'status': doc.status,
        'last_engine_started_at': doc.last_engine_started_at,
        'last_engine_finished_at': doc.last_engine_finished_at,
        'last_engine_error': doc.last_engine_error,
        'last_data_scraped_at': doc.last_data_scraped_at,
        'current_version': doc.current_version,
        'version_count': doc.version_count or 0,
        'total_products': doc.total_products or 0,
        'last_scrape_row_count': doc.last_scrape_row_count or 0,
        'scrape_files': frappe.parse_json(doc.scrape_files) or [],
    }


def _version_payload(row, include_content):
    payload = {
        'name': row.get('name'),
        'version_no': row.get('version_no'),
        'parent_version': row.get('parent_version'),
        'source': row.get('source'),
        'actor': row.get('actor'),
        'content_hash': row.get('content_hash'),
        'change_summary': row.get('change_summary'),
        'creation': row.get('creation'),
    }

    if not include_content:
        return payload

    # Listing queries carry only the header fields, so pull the bodies once here
    # rather than fetching every column for versions the caller may not want.
    doc = row if 'prototype_json' in row else frappe.get_doc('Scraper Brand Version', row.get('name'))

    payload['prototype_json'] = frappe.parse_json(doc.get('prototype_json'))
    payload['html_files'] = normalize_html_files(doc.get('html_files'))
    payload['scraper_code'] = doc.get('scraper_code') or ''

    return payload


def _count_products(filters, or_filters):
    if not or_filters:
        return frappe.db.count('Scraper Product', filters)
    return len(frappe.get_all(
        'Scraper Product',
        filters=filters,
        or_filters=or_filters,
        pluck='name',
        limit_page_length=0,
    ))


def _paging(page, page_size, max_size):
    page = max(1, int(page or 1))
    page_size = max(1, min(int(page_size or 20), max_size))
    return page, page_size


def _add_error(errors, row_number, reason):
    '''
    Record a row failure. The list is capped so a wholly broken workbook returns
    a readable sample rather than megabytes of error text -- the true total is
    carried separately as `error_count`.
    '''
    if len(errors) < MAX_REPORTED_ERRORS:
        errors.append({'row': row_number, 'reason': reason})


def _as_bool(value):
    '''
    Truthiness for the flag params, which arrive as whatever the caller sent --
    a JSON bool, an int, or the string "true"/"1"/"on" from a form post.

    The flag params are deliberately left un-annotated: Frappe coerces a
    whitelisted method's arguments to their annotations *before* the method
    runs, so an `int` annotation would make `?ok=true` raise a FrappeTypeError
    that never reaches this function.
    '''
    if isinstance(value, bool):
        return value
    return str(value if value is not None else '').strip().lower() in ('1', 'true', 'yes', 'y', 'on')
