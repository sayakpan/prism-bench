import json
import mimetypes
import os

import frappe

import boto3
from botocore.client import Config
from botocore.exceptions import ClientError

ASSET_PUBLIC_BASEPATH = frappe.conf.get('asset_public_basepath', 'https://prism-assets-dev.pratibhasyntex.com')
STYLE_IMG_BUCKET_NAME = frappe.conf.get('style_img_bucket_name', 'prism-assets-dev-020642895926-ap-south-1-an')

SIGNED_URL_VALIDITY_SECONDS = 600

STYLE_IMG_FOLDER_NAME = 'images/gsr_images'
STYLE_IMG_CLEAN_FOLDER_NAME = 'images/gsr_images/clean'
STYLE_IMG_3D_FOLDER_NAME = 'images/gsr_images_3D'

DYED_FABRIC_IMG_FOLDER_NAME = 'images/dyed-fabric'
DYED_FABRIC_THUMBNAIL_FOLDER_NAME = 'thumb'
DYED_FABRIC_IMG_EXTENSION = '.jpg'

# Prefix under which user-uploaded moodboard 3D models (GLB) live.
MODEL_FOLDER_NAME = 'models/moodboard'
GLB_CONTENT_TYPE = 'model/gltf-binary'

# Prefixes under which other user-uploaded moodboard documents (arbitrary file
# types — PDF, spreadsheet, etc.) live. Mirror the GLB layout.
GARMENT_FILE_FOLDER_NAME = 'files/moodboard/garment'
GARMENT_IMAGE_FOLDER_NAME = 'files/moodboard/garment_image'
BOM_FILE_FOLDER_NAME = 'files/moodboard/bom'
PRODUCT_VIDEO_FOLDER_NAME = 'files/moodboard/video'
STYLE_IMAGE_FOLDER_NAME = 'files/moodboard/style_image'   # per-garment style crop

# Sample Request media (garment gallery images + product videos), same layout,
# keyed by the Sample Request name instead of a moodboard.
SAMPLE_REQUEST_IMAGE_FOLDER_NAME = 'files/sample_request/garment_image'
SAMPLE_REQUEST_VIDEO_FOLDER_NAME = 'files/sample_request/video'

# Moodboard version images (the AI-generated full-size images + their edits), the
# compressed board thumbnail, and any images embedded in canvas_state / message
# attachments / garment cleaned images. Offloaded to S3 from the local /files
# staging path, keyed by the board name.
MOODBOARD_VERSION_FOLDER_NAME = 'files/moodboard/version'
# Reference/gallery images attached to a version (the `reference_images` child
# table), keyed by the board — separate from MOODBOARD_VERSION_FOLDER_NAME so
# generator output and hand-uploaded reference images stay in their own prefixes.
MOODBOARD_VERSION_IMAGE_FOLDER_NAME = 'files/moodboard/version_image'
MOODBOARD_THUMBNAIL_FOLDER_NAME = 'files/moodboard/thumbnail'
MOODBOARD_EMBEDDED_FOLDER_NAME = 'files/moodboard/embedded'
# Customer brief attachments — documents (PDF / PPT / Excel) as well as images,
# so these are kept out of the image folders above.
MOODBOARD_BRIEF_FOLDER_NAME = 'files/moodboard/brief'

# Brand Page Builder AI chat reference images, keyed by the chat. Uploaded
# straight to S3 (never staged as a local Frappe File), so the app server holds
# no chat media.
BRAND_PAGE_CHAT_FOLDER_NAME = 'files/brand_page/chat'

# Brand Page Builder block images (hero/section/etc.), keyed by the brand. New
# images decode straight to S3; images already stored as local /files URLs keep
# serving as-is.
PAGE_BUILDER_IMAGE_FOLDER_NAME = 'files/brand_page/image'

# Brand logo — Desk-attached, then offloaded to S3 on save, keyed by the brand.
BRAND_LOGO_FOLDER_NAME = 'files/brand/logo'

# Development Style images (the buyer's actual style photo and its artwork / CAD /
# flat sketch), Desk-attached then offloaded to S3 on save, keyed by the style.
# Both image fields share one prefix — the hash keeps them apart.
DEVELOPMENT_STYLE_IMAGE_FOLDER_NAME = 'files/development_style/image'

# Scraped-data workbooks posted by the AI scraper, keyed by the Scraper Brand.
# Unlike every other prefix here the object name is the human file name
# (<slug>_<datetime>.xlsx), not a hash — the brand's `scrape_files` JSON is a
# link list a person reads, and the per-brand folder plus second-resolution
# timestamp already make it unique.
SCRAPER_FILE_FOLDER_NAME = 'files/scraper'

DEFAULT_FILE_CONTENT_TYPE = 'application/octet-stream'


def get_style_images_by_gsr(img_prefix: str):
    '''
    Returns a list of filenames stored in the specified AWS S3 bucket
    where each filename begins with the provided "gsr_number" prefix.
    '''
    if not img_prefix:
        return []

    s3 = _get_s3_client()
    prefix = f'{STYLE_IMG_FOLDER_NAME}/{img_prefix}'
    response = s3.list_objects_v2(
        Bucket=STYLE_IMG_BUCKET_NAME, 
        Prefix=prefix, 
        MaxKeys=10
    )

    #region = frappe.conf.get('aws_region')
    return [obj['Key'] for obj in response.get('Contents', [])]

def get_all_latest_style_image_keys(folder_name: str = STYLE_IMG_FOLDER_NAME):
    '''
    Returns the latest front/back object keys (those containing '_front_1' or
    '_back_1') under the given style-image folder in the configured AWS S3
    bucket (defaults to the 2D gsr_images folder; pass STYLE_IMG_3D_FOLDER_NAME
    for the 3D images).

    When a gsr_no has more than one front (or back) image, only the most
    recently uploaded one (latest S3 LastModified) is returned, so callers get
    at most one front and one back key per gsr_no.

    Unlike get_style_images_by_gsr(), this performs a single full, paginated
    listing of the folder (list_objects_v2 returns at most 1000 keys per call)
    so callers can build an in-memory index once instead of issuing one S3
    request per gsr_no.
    '''
    s3 = _get_s3_client()
    paginator = s3.get_paginator('list_objects_v2')
    prefix = f'{folder_name}/'

    # (gsr_no, side) -> {'key', 'last_modified'} of the latest upload so far
    latest = {}
    for page in paginator.paginate(Bucket=STYLE_IMG_BUCKET_NAME, Prefix=prefix):
        for obj in page.get('Contents', []):
            key = obj['Key']
            filename = key.rsplit('/', 1)[-1]

            if 'front' in filename.lower():
                side = 'front'
            elif 'back' in filename.lower():
                side = 'back'
            else:
                continue

            # gsr_no is the first 11 characters of the filename
            gsr_no = filename[:11]
            if not gsr_no:
                continue

            last_modified = obj['LastModified']
            existing = latest.get((gsr_no, side))
            if existing is None or last_modified > existing['last_modified']:
                latest[(gsr_no, side)] = {'key': key, 'last_modified': last_modified}

    return [winner['key'] for winner in latest.values()]

def list_folder_objects(folder_name: str):
    '''
    Returns every object stored directly under the given folder in the
    configured AWS S3 bucket as a list of {'key', 'last_modified'} dicts
    (last_modified is the timezone-aware S3 LastModified datetime, in UTC).

    Folder placeholders / sub-prefixes (keys ending in '/') are skipped. The
    listing is fully paginated (list_objects_v2 returns at most 1000 keys per
    call), so callers see every object regardless of folder size.
    '''
    s3 = _get_s3_client()
    paginator = s3.get_paginator('list_objects_v2')
    prefix = f'{folder_name}/'

    objects = []
    for page in paginator.paginate(Bucket=STYLE_IMG_BUCKET_NAME, Prefix=prefix):
        for obj in page.get('Contents', []):
            key = obj['Key']
            if key.endswith('/'):
                continue  # skip the folder placeholder / sub-prefixes
            objects.append({'key': key, 'last_modified': obj['LastModified']})

    return objects


def public_asset_url(object_key: str):
    '''
    The public URL an S3 object key is served at, the same way
    format_garment_image_urls() builds one. Keys, not URLs, are what get stored
    against a record, so the basepath stays out of the database and a bucket or
    CDN move is a config change rather than a data migration.
    '''
    if not object_key:
        return None

    return f'{ASSET_PUBLIC_BASEPATH}/{object_key}'

def dyed_fabric_image_key(batch: str):
    ''' S3 object key for a dyed-fabric image: images/dyed-fabric/<batch>.jpg '''
    return f'{DYED_FABRIC_IMG_FOLDER_NAME}/{batch}{DYED_FABRIC_IMG_EXTENSION}'

def dyed_fabric_thumbnail_key(batch: str):
    ''' S3 object key for a dyed-fabric thumbnail: images/dyed-fabric/thumb/<batch>.jpg '''
    return f'{DYED_FABRIC_IMG_FOLDER_NAME}/{DYED_FABRIC_THUMBNAIL_FOLDER_NAME}/{batch}{DYED_FABRIC_IMG_EXTENSION}'

def fabric_image_url(batch_nr: str):
    if not batch_nr:
        return None

    return (
        f'{ASSET_PUBLIC_BASEPATH}/'
        f'{DYED_FABRIC_IMG_FOLDER_NAME}/'
        f'{batch_nr}{DYED_FABRIC_IMG_EXTENSION}'
    )

def fabric_thumbnail_url(batch_nr: str):
    if not batch_nr:
        return None

    return (
        f'{ASSET_PUBLIC_BASEPATH}/'
        f'{DYED_FABRIC_IMG_FOLDER_NAME}/'
        f'{DYED_FABRIC_THUMBNAIL_FOLDER_NAME}/'
        f'{batch_nr}{DYED_FABRIC_IMG_EXTENSION}'
    )

def fabric_image_signed_url(batch_nr: str):
    object_key = dyed_fabric_image_key(batch_nr)
    return _get_signed_url(STYLE_IMG_BUCKET_NAME, object_key)

def fabric_thumbnail_signed_url(batch_nr: str):
    object_key = dyed_fabric_thumbnail_key(batch_nr)
    return _get_signed_url(STYLE_IMG_BUCKET_NAME, object_key)

def delete_orphan_fabric_thumbnails():
    '''
    Deletes orphan fabric thumbnails from S3 -- those whose original image no
    longer exists in the bucket. Returns the number of thumbnails removed.
    '''
    try:
        img_prefix = f'{DYED_FABRIC_IMG_FOLDER_NAME}/'
        thumb_folder = f'{DYED_FABRIC_IMG_FOLDER_NAME}/{DYED_FABRIC_THUMBNAIL_FOLDER_NAME}'

        #--- 1. batches that still have an original image in S3 ---
        # (the main folder listing also contains the `thumb/` subfolder, so keep
        #  only the keys that sit directly under it: <batch>.jpg, no extra slash)
        original_batches = set()
        for obj in list_folder_objects(DYED_FABRIC_IMG_FOLDER_NAME):
            relative = obj['key'][len(img_prefix):]
            if '/' in relative or not relative.lower().endswith(DYED_FABRIC_IMG_EXTENSION):
                continue

            batch = relative[:-len(DYED_FABRIC_IMG_EXTENSION)]
            if batch:
                original_batches.add(batch)

        #--- 2. delete every thumbnail whose original image is gone ---
        deleted = 0
        for obj in list_folder_objects(thumb_folder):
            filename = obj['key'].rsplit('/', 1)[-1]
            if not filename.lower().endswith(DYED_FABRIC_IMG_EXTENSION):
                continue
            
            batch = filename[:-len(DYED_FABRIC_IMG_EXTENSION)]
            if batch and batch not in original_batches:
                delete_object(obj['key'])
                deleted += 1

        return {'success': True, 'data': {'deleted': deleted}}

    except Exception as ex:
        frappe.log_error(frappe.get_traceback(), 'cloud._delete_orphan_thumbnails()')
        return {'success': False, 'error': str(ex)}


def download_object(key: str):
    '''
    Returns the raw bytes of the S3 object at `key`, or None if no such object
    exists. Requires s3:GetObject on STYLE_IMG_BUCKET_NAME.
    '''
    s3 = _get_s3_client()
    try:
        resp = s3.get_object(Bucket=STYLE_IMG_BUCKET_NAME, Key=key)
        return resp['Body'].read()
    except s3.exceptions.NoSuchKey:
        return None
    except ClientError as ex:
        # treat a missing object (404/NoSuchKey) as absent, re-raise anything else
        if ex.response.get('Error', {}).get('Code') in ('NoSuchKey', '404', 'NotFound'):
            return None
        raise

def delete_object(key: str):
    '''
    Deletes the S3 object at `key`. S3 delete is idempotent, so this succeeds
    whether or not the object existed. Requires s3:DeleteObject on
    STYLE_IMG_BUCKET_NAME.
    '''
    s3 = _get_s3_client()
    s3.delete_object(Bucket=STYLE_IMG_BUCKET_NAME, Key=key)

def get_all_dyed_fabric_batches():
    '''
    Returns the set of "batch" identifiers for which a dyed-fabric image
    (named "<batch>.jpg") exists in the configured AWS S3 bucket.

    The bucket can hold more objects than a single list_objects_v2 call
    returns (1000 max), so results are paginated through completely.
    '''
    s3 = _get_s3_client()
    paginator = s3.get_paginator('list_objects_v2')
    prefix = f'{DYED_FABRIC_IMG_FOLDER_NAME}/'

    batches = set()
    for page in paginator.paginate(Bucket=STYLE_IMG_BUCKET_NAME, Prefix=prefix):
        for obj in page.get('Contents', []):
            key = obj['Key']
            filename = key[len(prefix):]
            if not filename or filename.lower().endswith('/'):
                continue  # skip the folder placeholder / sub-prefixes
            if not filename.lower().endswith(DYED_FABRIC_IMG_EXTENSION):
                continue
            batch = filename[:-len(DYED_FABRIC_IMG_EXTENSION)]
            if batch:
                batches.add(batch)

    return batches

def upload_glb(fileobj, key):
    '''
    Stream a GLB file object straight to S3 under `key` (no base64, no full-file
    buffering) and return the stored key. Caller builds the servable URL via
    asset_url(). Requires s3:PutObject on STYLE_IMG_BUCKET_NAME.
    '''
    s3 = _get_s3_client()
    s3.upload_fileobj(
        fileobj,
        STYLE_IMG_BUCKET_NAME,
        key,
        ExtraArgs={'ContentType': GLB_CONTENT_TYPE},
    )
    return key

def build_model_key(moodboard, style):
    ''' Object key for a moodboard style's GLB: models/moodboard/<board>/<hash>.glb '''
    return f'{MODEL_FOLDER_NAME}/{moodboard}/{frappe.generate_hash()[:12]}.glb'

def upload_file(fileobj, key, content_type=DEFAULT_FILE_CONTENT_TYPE):
    '''
    Stream an arbitrary file object straight to S3 under `key` (no base64, no
    full-file buffering) and return the stored key — the file-type-agnostic
    sibling of upload_glb() for documents like the garment file / BOM. Caller
    builds the servable URL via asset_url(). Requires s3:PutObject on
    STYLE_IMG_BUCKET_NAME.
    '''
    s3 = _get_s3_client()
    s3.upload_fileobj(
        fileobj,
        STYLE_IMG_BUCKET_NAME,
        key,
        ExtraArgs={'ContentType': content_type or DEFAULT_FILE_CONTENT_TYPE},
    )
    return key


def build_garment_file_key(moodboard, style, ext=''):
    ''' Object key for a style's garment file: files/moodboard/garment/<board>/<hash><ext> '''
    return f'{GARMENT_FILE_FOLDER_NAME}/{moodboard}/{frappe.generate_hash()[:12]}{ext or ""}'

def build_garment_image_key(moodboard, style, ext=''):
    ''' Object key for one of a style's garment gallery images: files/moodboard/garment_image/<board>/<hash><ext> '''
    return f'{GARMENT_IMAGE_FOLDER_NAME}/{moodboard}/{frappe.generate_hash()[:12]}{ext or ""}'

def build_style_image_key(moodboard, style, ext=''):
    ''' Object key for a style's per-garment crop image: files/moodboard/style_image/<board>/<hash><ext> '''
    return f'{STYLE_IMAGE_FOLDER_NAME}/{moodboard}/{frappe.generate_hash()[:12]}{ext or ""}'

def build_bom_file_key(moodboard, style, ext=''):
    ''' Object key for a style's BOM file: files/moodboard/bom/<board>/<hash><ext> '''
    return f'{BOM_FILE_FOLDER_NAME}/{moodboard}/{frappe.generate_hash()[:12]}{ext or ""}'

def build_product_video_key(moodboard, style, ext=''):
    ''' Object key for a style's product video: files/moodboard/video/<board>/<hash><ext> '''
    return f'{PRODUCT_VIDEO_FOLDER_NAME}/{moodboard}/{frappe.generate_hash()[:12]}{ext or ""}'

def build_sample_request_image_key(sample_request, ext=''):
    ''' Object key for a Sample Request gallery image: files/sample_request/garment_image/<sr>/<hash><ext> '''
    return f'{SAMPLE_REQUEST_IMAGE_FOLDER_NAME}/{sample_request}/{frappe.generate_hash()[:12]}{ext or ""}'

def build_sample_request_video_key(sample_request, ext=''):
    ''' Object key for a Sample Request product video: files/sample_request/video/<sr>/<hash><ext> '''
    return f'{SAMPLE_REQUEST_VIDEO_FOLDER_NAME}/{sample_request}/{frappe.generate_hash()[:12]}{ext or ""}'

def build_moodboard_version_image_key(moodboard, ext=''):
    ''' Object key for a moodboard version image: files/moodboard/version/<board>/<hash><ext> '''
    return f'{MOODBOARD_VERSION_FOLDER_NAME}/{moodboard}/{frappe.generate_hash()[:12]}{ext or ""}'

def build_moodboard_version_gallery_image_key(moodboard, ext=''):
    ''' Object key for one version reference-gallery image: files/moodboard/version_image/<board>/<hash><ext> '''
    return f'{MOODBOARD_VERSION_IMAGE_FOLDER_NAME}/{moodboard}/{frappe.generate_hash()[:12]}{ext or ""}'

def build_moodboard_thumbnail_key(moodboard, ext='.webp'):
    ''' Object key for a moodboard's compressed thumbnail: files/moodboard/thumbnail/<board>/<hash>.webp '''
    return f'{MOODBOARD_THUMBNAIL_FOLDER_NAME}/{moodboard}/{frappe.generate_hash()[:12]}{ext or ".webp"}'

def build_moodboard_embedded_image_key(moodboard, ext=''):
    ''' Object key for an image embedded in a board (canvas/message/garment): files/moodboard/embedded/<board>/<hash><ext> '''
    return f'{MOODBOARD_EMBEDDED_FOLDER_NAME}/{moodboard}/{frappe.generate_hash()[:12]}{ext or ""}'

def build_moodboard_brief_file_key(moodboard, ext=''):
    ''' Object key for a customer brief attachment: files/moodboard/brief/<board>/<hash><ext> '''
    return f'{MOODBOARD_BRIEF_FOLDER_NAME}/{moodboard}/{frappe.generate_hash()[:12]}{ext or ""}'

def build_brand_page_chat_attachment_key(chat, ext=''):
    ''' Object key for a Brand Page chat reference image: files/brand_page/chat/<chat>/<hash><ext> '''
    return f'{BRAND_PAGE_CHAT_FOLDER_NAME}/{chat}/{frappe.generate_hash()[:12]}{ext or ""}'

def build_brand_logo_key(brand, ext=''):
    ''' Object key for a brand's logo: files/brand/logo/<brand>/<hash><ext> '''
    return f'{BRAND_LOGO_FOLDER_NAME}/{brand}/{frappe.generate_hash()[:12]}{ext or ""}'

def build_development_style_image_key(style, ext=''):
    ''' Object key for a development style image: files/development_style/image/<style>/<hash><ext> '''
    return f'{DEVELOPMENT_STYLE_IMAGE_FOLDER_NAME}/{style}/{frappe.generate_hash()[:12]}{ext or ""}'

def build_page_builder_image_key(brand, ext=''):
    ''' Object key for a Page Builder block image: files/brand_page/image/<brand>/<hash><ext> '''
    return f'{PAGE_BUILDER_IMAGE_FOLDER_NAME}/{brand}/{frappe.generate_hash()[:12]}{ext or ""}'

def build_scraper_file_key(brand, file_name):
    ''' Object key for a scraped workbook: files/scraper/<brand>/<slug>_<datetime>.xlsx '''
    return f'{SCRAPER_FILE_FOLDER_NAME}/{brand}/{file_name}'

def file_ext(filename):
    ''' Lower-cased extension (incl. leading dot) of a filename/path, or '' if none. '''
    if not filename:
        return ''
    return os.path.splitext(filename)[1].lower()

def content_type_for(filename):
    ''' Best-guess MIME type for a filename, falling back to octet-stream. '''
    guessed, _ = mimetypes.guess_type(filename or '')
    return guessed or DEFAULT_FILE_CONTENT_TYPE

def asset_url(key):
    ''' S3 object key -> public servable URL (passes through full URLs). '''
    if not key:
        return ''
    if key.startswith('http://') or key.startswith('https://'):
        return key
    return f'{ASSET_PUBLIC_BASEPATH}/{key.lstrip("/")}'

def asset_key(url_or_key):
    '''
    Inverse of asset_url(): the S3 object key for a stored asset value, or None
    when there's nothing in our bucket to act on. Strips the public base path
    from a CDN URL and passes a bare key through; returns None for empty values,
    local Frappe file paths (/files/...), and foreign URLs served elsewhere.
    '''
    if not url_or_key:
        return None
    value = url_or_key.strip()
    base = ASSET_PUBLIC_BASEPATH.rstrip('/') + '/'
    if value.startswith(base):
        return value[len(base):].lstrip('/') or None
    if value.startswith('http://') or value.startswith('https://'):
        return None  # some other host — not ours to delete
    if value.startswith('/files/') or value.startswith('/private/files/'):
        return None  # a local Frappe File, not an S3 object
    return value.lstrip('/') or None  # already a bare key

def format_garment_image_urls(img_urls_raw: str):
    ret_urls = {}
    try:
        if img_urls_raw:
            img_urls = json.loads(img_urls_raw)
            # front
            fornt_img = img_urls.get('front')
            if fornt_img:
                ret_urls['front'] = f'{ASSET_PUBLIC_BASEPATH}/{fornt_img}'
                #ret_urls['front'] = _get_signed_url(STYLE_IMG_BUCKET_NAME, fornt_img)
            # back
            back_img = img_urls.get('back')
            if back_img:
                ret_urls['back'] = f'{ASSET_PUBLIC_BASEPATH}/{back_img}'
                #ret_urls['back'] = _get_signed_url(STYLE_IMG_BUCKET_NAME, back_img)
    except:
        frappe.log_error(frappe.get_traceback(), 'cloud.format_garment_image_urls()')
    
    return ret_urls

def garment_image_signed_urls(img_urls_raw: str):
    ret_urls = {}
    try:
        if img_urls_raw:
            img_urls = json.loads(img_urls_raw)
            # front
            fornt_img = img_urls.get('front')
            if fornt_img:
                ret_urls['front'] = _get_signed_url(STYLE_IMG_BUCKET_NAME, fornt_img)
            # back
            back_img = img_urls.get('back')
            if back_img:
                ret_urls['back'] = _get_signed_url(STYLE_IMG_BUCKET_NAME, back_img)
    except:
        frappe.log_error(frappe.get_traceback(), 'cloud.format_garment_image_urls()')

    return ret_urls

def garment_front_signed_url(img_urls_raw: str):
    '''
    Signed URL for just the `front` object key in a garment `image_urls*` JSON blob
    (e.g. '{"front": "images/gsr_images_3D/..png"}'), or None when there's no front.
    The front-only sibling of garment_image_signed_urls(), for a thumbnail.
    '''
    try:
        if img_urls_raw:
            front = json.loads(img_urls_raw).get('front')
            if front:
                return _get_signed_url(STYLE_IMG_BUCKET_NAME, front)
    except Exception:
        frappe.log_error(frappe.get_traceback(), 'cloud.garment_front_signed_url()')
    return None


#--- helpers ---
def _get_s3_client():
    return boto3.client(
        's3',
        aws_access_key_id=frappe.conf.get('aws_access_key'),
        aws_secret_access_key=frappe.conf.get('aws_secret_key'),
        region_name=frappe.conf.get('aws_region'),
        # Force the regional, virtual-hosted endpoint + SigV4 so presigned URLs
        # are signed for the bucket's region. Without this boto3 targets the
        # global s3.amazonaws.com endpoint, which 307-redirects to the regional
        # host and breaks the (un-re-signable) presigned URL -> SignatureDoesNotMatch.
        config=Config(signature_version='s3v4', s3={'addressing_style': 'virtual'}),
    )

def _get_signed_url(bucket_name: str, object_key: str, validity: int = SIGNED_URL_VALIDITY_SECONDS):
    s3 = _get_s3_client()
    url = s3.generate_presigned_url(
        'get_object',
        Params={
            'Bucket': bucket_name,
            'Key': object_key
        },
        ExpiresIn=validity
    )

    return url
