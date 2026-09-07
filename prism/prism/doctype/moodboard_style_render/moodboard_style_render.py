'''
Moodboard Style Render — the fabric <-> image pairs of one style.

One row per distinct fabric identity ever rendered for the style, so switching
back to a fabric already rendered costs nothing: the row's image is copied up to
`Moodboard Style.image` with no call to the renderer and no 3-4 minute wait.

The cache identity is `render_key` (see build_render_key). It is NOT the fab code:
keyed on the quality alone, changing colourway and changing back would serve an
image of the previous colour, so the dye lot (batch), the colourway (TCX), the
print and any recoloured swatch are all folded into the hash.
'''

import hashlib
import re

from frappe.model.document import Document

# How many hex chars of the digest are kept. 16 chars = 64 bits — far past any
# realistic per-style row count, and short enough to read in a grid.
_KEY_LENGTH = 16

_WHITESPACE = re.compile(r'\s+')


class MoodboardStyleRender(Document):
    pass


def build_render_key(fab_code=None, element_colour_tcx=None, print_label=None,
                     recolour=None, fab_batch=None):
    '''
    The cache key for one render: hash(fab_code, element_colour_tcx, print_label,
    recolour[, fab_batch]).

    Components are trimmed / lower-cased / whitespace-collapsed before hashing, so
    a colourway that arrives as "19-4052 TCX" and as "19-4052  tcx" is one render,
    not two. An empty component is part of the key as an empty string, which is what
    makes "solid" (no print) and "no recolour" stable identities rather than
    wildcards.

    `fab_batch` is the exception: it is APPENDED, and only when it has a value.
    Two reasons, and they are the same reason twice.

      * Two batches of one fab code are two dye lots of the same cloth — genuinely
        different colourways, so they genuinely need separate renders. Folding the
        batch in is what stops a switch between them returning the previous lot's
        picture.
      * Every render cached before the batch existed was keyed on four components.
        Appending a fifth unconditionally would change all of those keys at once,
        stranding every board's cache behind a key nothing looks up any more — and
        with no per-style render endpoint on the AI service today (see
        apply_style_fabric), a stranded cache cannot be refilled. Absent batch =
        the legacy code-level identity, byte-for-byte.

    So "no batch" is not a wildcard either: it is the identity of a render whose
    lot was never recorded, which is exactly what every pre-batch row is.

    Returns None when `fab_code` is empty — the quality is the identity, and there
    is nothing to cache a render against without it. A batch alone is not enough:
    it is a lot *of* a code, meaningless on its own.
    '''
    fab_code = _norm(fab_code)
    if not fab_code:
        return None
    parts = [fab_code, _norm(element_colour_tcx), _norm(print_label), _norm(recolour)]
    if _norm(fab_batch):
        parts.append(_norm(fab_batch))
    return hashlib.sha1('|'.join(parts).encode('utf-8')).hexdigest()[:_KEY_LENGTH]


def _norm(value):
    if value is None:
        return ''
    return _WHITESPACE.sub(' ', str(value).strip().lower())
