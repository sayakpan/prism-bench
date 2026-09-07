import base64
import mimetypes
import io
from io import BytesIO

import frappe
from PIL import Image, ImageOps, ImageFilter

DYED_FABRIC_MASTER_DOCTYPE = 'Moodboard Dyed Fabric'

# High-quality downscale filter. Pillow >= 9.1 exposes it under Resampling; older
# builds keep it on the Image module. LANCZOS is markedly sharper than the default
# thumbnail() resample, which is the usual cause of "blurry" thumbnails.
try:
    _RESAMPLE = Image.Resampling.LANCZOS
except AttributeError:  # Pillow < 9.1
    _RESAMPLE = Image.LANCZOS


def fiber_sort_key(row):
    '''
    Sort key for `Dyed Fabric Fiber` child rows. Orders by the linked Fiber Type
    master's `sort_order` (resolved into `type_sort_order`) ascending; types left at
    0 (unset) fall to the end and keep their row order (`idx`), so numbered types
    always come first. Ordering is thus defined once on the Fiber Type master and
    applied consistently everywhere the type is listed.
    '''
    order = row.get('type_sort_order') or 0
    return (order if order > 0 else float('inf'), row.get('idx') or 0)


def fiber_type_lookup():
    '''
    Build a resolver for the value stored in a `Dyed Fabric Fiber` row's
    `fiber_type` field. Depending on how rows were created/imported, that value may
    be the Fiber Type's docname (`name`) or its human `type_name`, so we key the
    map by BOTH and look up by name first, then label.

    Returns (by_name, by_label), each mapping the key to a `(label, sort_order)`
    tuple where `label` is the display `type_name` (falling back to the docname).
    Callers resolve a row with:

        label, order = by_name.get(raw) or by_label.get(raw) or (raw, 0)

    so an unmatched/orphaned value keeps its raw label and sorts as unset (0).
    '''
    by_name, by_label = {}, {}
    for r in frappe.get_all(
        'Fiber Type', fields=['name', 'type_name', 'sort_order']
    ):
        info = (r.type_name or r.name, r.sort_order or 0)
        by_name[r.name] = info
        if r.type_name:
            by_label.setdefault(r.type_name, info)
    return by_name, by_label


def get_user_info(user):
    ''' Get user information '''

    # check if user is enabled
    if user.enabled == 0:
        raise ValueError('User is disabled.')

    user_id = user.name

    user_info = {
        'id': user_id,
        'email': user.email,
        'full_name': user.full_name,
        'roles': [d.role for d in user.roles],
        'enabled': True,
        'status': 'Active'
    }

    # locate Brand (if exists)
    if frappe.db.exists('Brand User', {'user': user_id}):
        brand_user = frappe.get_doc('Brand User', {'user': user_id})
        brand_id = brand_user.brand
        if frappe.db.exists('Brand', brand_id):
            brand = frappe.get_doc('Brand', brand_id)
            user_info['brand'] = {
                'id': brand.name,
                'brand': brand.brand,
                'category': brand.category
            }
            #user_info['status'] = buyer.status
            #if buyer.status not in ['Active', 'Pending']:
            #    user_info['enabled'] = False

    return user_info

def get_current_user_id():
    jwt_payload = frappe.local.jwt_payload
    user_id = jwt_payload['user']['email']

    return user_id

def get_current_brand():
    ''' Returns the Brand of the current user . '''
    jwt_payload = frappe.local.jwt_payload
    brand = jwt_payload['user'].get('brand')
    return brand

def user_has_roles(roles: list):
    ''' 
    Checks whether the current user belongs
    to any role within a specified list.
    '''
    user_roles = set(frappe.get_roles(frappe.session.user))
    return bool(user_roles.intersection(roles))


def save_file(base64_content, max_size_mb, file_name=None):
    ''' Saves a base64 encoded doc and return the file URL. '''

    extension = 'png'  # default fallback extension

    max_size_bytes = max_size_mb * 1024 * 1024

    # remove data URI prefix if present
    if ',' in base64_content:
        #header, base64_content = base64_content.split(',')[1]
        header, base64_content = base64_content.split(',', 1)
        # header format: data:image/png;base64
        if ':' in header and ';' in header:
            mime_type = header.split(':')[1].split(';')[0]
            # get extension from mime type
            ext = mimetypes.guess_extension(mime_type)
            if ext:
                extension = ext.lstrip('.')

    # decode the base64 content
    file_content = base64.b64decode(base64_content)
    
    # check file size
    if len(file_content) > max_size_bytes:
        raise ValueError(f'File size exceeds the maximum allowed limit of {max_size_mb} MB.')

    # generate filename if not provided
    if not file_name:
        file_name = f'doc_{frappe.generate_hash()[:8]}.{extension}'
    
    file_doc = frappe.get_doc({
        'doctype': 'File',
        'file_name': file_name,
        'content': file_content,
        'is_private': 0,  # set to 1 for private files
    })
    file_doc.save(ignore_permissions=True)
    
    return file_doc.file_url

def png_to_jpeg(png_bytes: bytes, quality: int = 90) -> bytes:
    img = Image.open(BytesIO(png_bytes)).convert('RGB')
    out = BytesIO()
    img.save(out, format='JPEG', quality=quality, optimize=True, progressive=True)
    return out.getvalue()

def make_image_thumbnail(img_bytes: bytes, size: int):
    '''
    Cuts a `size` x `size` square from the centre of the image and returns it as
    JPEG bytes.

    Returns None (does nothing) if either side of the source image is smaller
    than `size`, since there is no full square to crop.
    '''
    with Image.open(io.BytesIO(img_bytes)) as img:
        # honour EXIF orientation, normalise to RGB (sources may be CMYK/palette/RGBA)
        img = ImageOps.exif_transpose(img)
        img = img.convert('RGB')

        if img.width < size or img.height < size:
            return None

        # centre-crop a size x size square
        left = (img.width - size) // 2
        top = (img.height - size) // 2
        img = img.crop((left, top, left + size, top + size))

        out = io.BytesIO()
        img.save(out, format='JPEG', quality=82, optimize=True, progressive=True)

        return out.getvalue()

def make_image_thumbnail_fit(img_bytes: bytes, max_dim: int = 512, quality: int = 80, sharpen: bool = False):
    '''
    Aspect-preserving thumbnail: downscale the image to fit within a max_dim x
    max_dim box (the longest edge becomes max_dim, the aspect ratio is preserved)
    and return it as WebP bytes.

    Unlike make_image_thumbnail (which centre-crops a square), this keeps the whole
    image and its shape — the moodboard-card use case. It never upscales.

    `sharpen=False` (default) is the lightweight path used automatically on every
    write/migration. `sharpen=True` is the high-quality path (LANCZOS resampling +
    a light unsharp mask to counter downscale softening) used on demand by the
    "Upgrade Thumbnail Quality" action. Alpha is preserved for PNG/RGBA sources;
    EXIF orientation honoured. Returns None for an unreadable image.
    '''
    with Image.open(io.BytesIO(img_bytes)) as img:
        img = ImageOps.exif_transpose(img)
        # WebP supports alpha, so keep it when present; otherwise flatten to RGB.
        img = img.convert('RGBA') if img.mode in ('RGBA', 'LA', 'P') else img.convert('RGB')

        if sharpen:
            # High quality: aspect-preserving downscale with LANCZOS, then a mild
            # unsharp mask (avoids halos) to restore crispness lost in the downscale.
            w, h = img.size
            scale = min(max_dim / w, max_dim / h, 1.0)
            if scale < 1.0:
                img = img.resize((max(1, round(w * scale)), max(1, round(h * scale))), _RESAMPLE)
                img = img.filter(ImageFilter.UnsharpMask(radius=1.2, percent=80, threshold=2))
        else:
            # Default: fast, small — the original behaviour.
            img.thumbnail((max_dim, max_dim))

        out = io.BytesIO()
        img.save(out, format='WEBP', quality=quality, method=6)

        return out.getvalue()

def fabric_image_thumbnail(img_bytes: bytes, cut_size: int, final_size: int):
    '''
    Cuts a `cut_size` x `cut_size` square from the centre of the image, then
    resizes it to `final_size` x `final_size` and returns it as JPEG bytes.

    Returns None (does nothing) if either side of the source image is smaller
    than `cut_size`, since there is no full square to crop.
    '''
    with Image.open(io.BytesIO(img_bytes)) as img:
        # honour EXIF orientation, normalise to RGB (sources may be CMYK/palette/RGBA)
        img = ImageOps.exif_transpose(img)
        img = img.convert('RGB')

        if img.width < cut_size or img.height < cut_size:
            return None

        # centre-crop a cut_size x cut_size square
        left = (img.width - cut_size) // 2
        top = (img.height - cut_size) // 2
        img = img.crop((left, top, left + cut_size, top + cut_size))

        # resize the cropped square down (or up) to final_size x final_size
        img = img.resize((final_size, final_size), Image.LANCZOS)

        out = io.BytesIO()
        img.save(out, format='JPEG', quality=82, optimize=True, progressive=True)

        return out.getvalue()


# --- test ---
def get_test_buyer():
    ''' Returns the top 1 Buyer doc order by "creation" field. '''
    buyer_id = frappe.db.get_value('Buyer', filters={}, fieldname='name', order_by='creation asc')
    if not buyer_id:
        return None
    return frappe.get_doc('Buyer', buyer_id)
