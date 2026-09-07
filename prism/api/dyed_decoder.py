import re

import frappe

from prism.auth.authenticator import auth_required


# --- Code lookups ---------------------------------------------------------
# These dictionaries define how the decoder turns shorthand tokens into
# human-readable labels. Many entries are mill-specific shorthand and the
# labels reflect the most common textile-industry interpretation; confirm
# against your fabric master where flagged.
#
# Inputs are normalised by replacing '_' with ' ' before lookup, so keys
# only need the space-separated form.

# Supply state of the cloth — i.e. how the colour gets onto it.
BASE_CODES = {
    'MLW':           'Melange (pre-dyed yarn)',
    'MEL':           'Melange',
    'GREY':          'Greige / RFD (Ready For Dyeing)',
    'GRY':           'Greige / RFD',
    'RFD':           'Ready For Dyeing',
    'SPD':           'Solid Piece-Dyed',
    'YDW':           'Yarn-Dyed (woven)',
    'YDW STRIPE':    'Yarn-Dyed Stripe',
    'YDW JACQUARD':  'Yarn-Dyed Jacquard',
    'YDW/MLW':       'Yarn-Dyed + Melange',
    'YD':            'Yarn-Dyed',
    'PD':            'Piece-Dyed',
    'DD':            'Direct/Dope-Dyed (mill-specific)',
    'SCDGB':         'Solution-Coloured (greige base, mill-specific)',
    'SCDML':         'Solution-Coloured Melange (mill-specific)',
    'SCDYD':         'Solution-Coloured Yarn-Dyed (mill-specific)',
    'WOVEN':         'Woven (greige base)',
}

# Knit / weave structures. Multi-word entries are matched longest-first
# so that "MIN WFL RIB" beats "WFL RIB" beats "RIB". Both space- and
# underscore-separated forms in the inventory are accepted (underscores
# are normalised to spaces before lookup).
STRUCTURE_CODES = {
    # 4-5 word
    'WOVEN 1 X 1 PLAIN':    'Plain-weave (1x1)',

    # 3-word
    'MIN WFL RIB':          'Mini Waffle Rib',
    'MINI WFL RIB':         'Mini Waffle Rib',
    'INL DROP NDL':         'Interlock Drop-Needle',
    'INL DROP_NDL':         'Interlock Drop-Needle',
    '3T DIA FLC':           '3-Thread Diagonal Fleece',
    '3T DIA TERRY':         '3-Thread Diagonal Terry',
    'DIA 2T FLC':           'Diagonal 2-Thread Fleece',
    '3X1 RIB PLTD':         '3x1 Rib (plated)',
    'WOVEN 1X1 PLAIN':      'Plain-weave (1x1)',
    'DOUBLE KNIT JAQUARD':  'Double-Knit Jacquard',
    'FOMA INL TWILL':       'Foma Interlock Twill',
    'FLBK RIB 4X3':         'Flatback Rib 4x3',
    'FLBK RIB 8X2':         'Flatback Rib 8x2',
    'FLBK RIB 3X3':         'Flatback Rib 3x3',
    'FLBK RIB 4X2':         'Flatback Rib 4x2',
    'OTTM RIB STP':         'Ottoman Rib Stripe',

    # 2-word
    '2T FLC':       '2-Thread Fleece',
    '3T FLC':       '3-Thread Fleece',
    '2T TERRY':     '2-Thread Terry',
    '3T TERRY':     '3-Thread Terry',
    '2T TT':        '2-Thread Triple-Tuck',
    'DIA FLC':      'Diagonal Fleece',
    'DIA TERRY':    'Diagonal Terry',
    'WFL RIB':      'Waffle Rib',
    'VER RIB':      'Vertical Rib',
    'FLBK RIB':     'Flatback Rib',
    'BER KNIT':     'Berber Knit',
    'RC KNIT':      'RC Knit (mill-specific)',
    'OTTM RIB':     'Ottoman Rib',
    'OTTM SJY':     'Ottoman Single Jersey',
    'PNTL RIB':     'Pointelle Rib',
    'PNTL SJY':     'Pointelle Single Jersey',
    'CRP SJY':      'Crepe Single Jersey',
    'HER BONE':     'Herringbone',
    'HBONE STP':    'Herringbone Stripe',
    'DT PQ':        'Double-Tuck Pique',
    'TT PQ':        'Triple-Tuck Pique',
    'FOMA RIB':     'Foma Rib (mill-specific)',
    'FOMA INL':     'Foma Interlock (mill-specific)',
    'PURL JERSEY':  'Purl Jersey',
    'POPCORN SJY':  'Popcorn Single Jersey',
    'VOIL FAB':     'Voile Fabric',
    'INL STP':      'Interlock Stripe',
    'INL PLTD':     'Interlock (plated)',
    'PQ IL':        'Pique Interlock',
    'PQ INL':       'Pique Interlock',
    'PQ FLC':       'Pique Fleece',
    'PQ STP':       'Pique Stripe',
    'SJ PLTD':      'Single Jersey (plated)',
    'SJY STP':      'Single Jersey Stripe',
    'SJY PLTD':     'Single Jersey (plated)',
    'SJY SLUB':     'Single Jersey Slub',
    'SJY TWILL':    'Single Jersey Twill',
    'SJY WRI':      'Single Jersey Wrinkle finish',
    'SJY STRUCTURE': 'Single Jersey (structured)',
    'RIB WRI':      'Rib Wrinkle',

    # 1-word
    'RIB':       'Rib knit',
    'SJY':       'Single Jersey',
    'SJ':        'Single Jersey',
    'INL':       'Interlock',
    'PQ':        'Pique',
    'DJ':        'Double Jersey',
    'HBONE':     'Herringbone',
    'HCOMB':     'Honeycomb',
    'LACE':      'Lace',
    'MESH':      'Mesh',
    'WOVEN':     'Woven',
    'POPLENE':   'Poplin',
    'TERRY':     'Terry',
}

# Tokens between structure and blend that describe a *property* of the fabric
# (stripe, slub yarn, plated, ...) rather than the structure family itself.
# Multiple may stack and we collect them in the `modifiers` list.
STRUCTURE_MODIFIERS = {
    'STP':    'striped',
    'SLUB':   'slub yarn',
    'PLTD':   'plated',
    'TWILL':  'twill',
    'ALT':    'alt-knit',
    'WRI':    'wrinkle finish',
}

# Yarn-process annotations that may appear between blend and GSM. They describe
# *how* the yarn was spun (carded-combed, compact, siro), not what it is made
# of, so we silently skip them.
YARN_ANNOTATIONS = {'CC', 'COMPACT', 'SIRO', 'CTN'}

# Compound multi-word fibre codes. The second token is a redundant qualifier
# (e.g., "ORG CTN" = Organic Cotton); we drop it before the blend split so
# the ratio/code count still matches.
COMPOUND_FIBRES = ('ORG CTN', 'BCI CTN', 'ROC CTN', 'BCI MDL', 'BCI CC')

# Fibre / yarn shorthand. Used inside the blend section only.
FIBRE_CODES = {
    # Cotton family
    'C':     'Cotton',
    'O':     'Organic Cotton',
    'OC':    'Organic Cotton',
    'ORG':   'Organic Cotton',
    'B':     'BCI Cotton',
    'BCI':   'BCI Cotton',
    'ROC':   'Recycled Organic Cotton',
    'FTO':   'Fairtrade Organic Cotton',
    'TC':    'Tencel + Cotton blend yarn',
    'VPC':   'VPC yarn (mill-specific)',
    'SUP':   'Supima Cotton',
    'F':     'Fairtrade Cotton',
    'RC':    'Recycled Cotton',
    'CTN':   'Cotton',
    # Polyester family
    'P':     'Polyester',
    'RP':    'Recycled Polyester',
    'POLY':  'Polyester',
    'FTO':   'Fairtrade Organic Cotton',
    # Cellulosic / man-made
    'V':     'Viscose',
    'VLF':   'Viscose Linen Filament (mill-specific)',
    'T':     'Tencel (Lyocell)',
    'LY':    'Lycra (Elastane brand)',
    'M':     'Modal',
    'MDL':   'Modal',
    'MM':    'MicroModal',
    # Elastane
    'E':     'Elastane',
    'EL':    'Elastane',
    'ROICA': 'ROICA Elastane',
    'ECO':   'ECO yarn (mill-specific)',
    # Natural fibres
    'L':     'Linen',
    'W':     'Wool',
    'A':     'Acrylic',
    'N':     'Nylon',
    'S':     'Silk',
    'SK':    'Silk',
    'H':     'Hemp',
    'R':     'Rayon',
    'K':     'K-fibre (mill-specific)',
}

# Codes whose meaning varies by mill or by context. We surface a warning
# whenever they appear so the caller can confirm the intended fibre.
AMBIGUOUS_FIBRES = {
    'T':   ['Tencel (Lyocell)', 'Triacetate', 'Terylene (Polyester)'],
    'L':   ['Linen', 'Lycra (Elastane)'],
    'R':   ['Rayon', 'Recycled (Cotton or Polyester)'],
    'S':   ['Silk', 'Spandex (Elastane)'],
    'F':   ['Fairtrade Cotton', '(other mill-specific use)'],
    'B':   ['BCI Cotton', '(check mill convention)'],
    'LY':  ['Lycra (Elastane brand)', 'Lyocell (Tencel)'],
    'TC':  ['Tencel + Cotton blend yarn', 'Triacetate-Cotton'],
    'VPC': ['Viscose-Polyester-Cotton', 'Vintage Pre-Coloured (mill-specific)'],
    'K':   ['mill-specific — confirm against fabric master'],
    'RC':  ['Recycled Cotton', '(mill-specific recycled blend)'],
}

# Common colour suffix tokens. Compound colour codes (PC3BK, CT2BK166,
# C2C24BK3217, ...) are decoded by the regex fallback in _parse_colour().
COLOUR_TOKENS = {
    # 2-3 letter colour shorthands
    'BK':  'Black',  'BLA': 'Black', 'BLK': 'Black',
    'WH':  'White',  'WHT': 'White',
    'OFW': 'Off White', 'OFF': 'Off White',
    'NV':  'Navy',   'NVY': 'Navy',
    'GR':  'Grey',   'GRY': 'Grey',
    'RD':  'Red',
    'BL':  'Blue',   'BLU': 'Blue',
    'GRN': 'Green',
    'YL':  'Yellow', 'YLW': 'Yellow',
    'PK':  'Pink',
    'OR':  'Orange',
    'TQ':  'Turquoise',
    'PP':  'Purple',
    'CR':  'Cream',
    'BR':  'Brown',
    'IJ':  'Indigo',
    'SN':  'Sand',
    'LU':  'Lavender (mill code)',
    'TP':  'Taupe',
    'NP':  'Neon Pink (mill code)',
    'YD':  'Yarn-dyed',
    # Card / lot prefixes for compound colour codes
    'PC':  'Piece-dyed (cotton card)',
    'PV':  'Piece-dyed Polyester-Viscose card',
    'PCV': 'Piece-dyed Polyester-Cotton-Viscose card',
    'PCM': 'Piece-dyed Polyester-Cotton-Modal card',
    'PVM': 'Piece-dyed Polyester-Viscose-Modal card',
    'CT':  'Cotton-Tone colour card (mill-specific)',
    'CV':  'Cotton-Viscose colour card',
    'CM':  'Cotton-Modal colour card',
    'CMM': 'Cotton-Modal colour card (alt)',
    'TL':  'Tencel-Linen colour card',
    'TW':  'Tencel-Wool colour card',
    'TPM': 'Tencel-Polyester-Modal colour card',
    'PL':  'Polyester-Linen colour card',
    'VM':  'Viscose-Melange colour card',
    'PM':  'Polyester-Melange colour card',
    'CL':  'Cotton-Linen colour card',
    'C2C': 'Cradle-to-Cradle (sustainability card)',
    'MC':  'Mill colour card',
    'RG':  'Range colour-card prefix (mill-specific)',
    # Print / process flags
    'AOP': 'All-Over Print',
    'RFD': 'RFD (undyed)',
    # Full-word colours
    'WHITE':   'White',  'BLACK':   'Black',
    'NAVY':    'Navy',   'GREEN':   'Green',
    'BLUE':    'Blue',   'RED':     'Red',
    'YELLOW':  'Yellow', 'PINK':    'Pink',
    'ORANGE':  'Orange', 'BROWN':   'Brown',
    'PURPLE':  'Purple', 'GREY':    'Grey',
    'GRAY':    'Grey',   'OLIVE':   'Olive',
    'CREAM':   'Cream',  'MAROON':  'Maroon',
    'KHAKI':   'Khaki',  'INDIGO':  'Indigo',
    'CARBON':  'Carbon (charcoal)',
    'CHARCOAL':'Charcoal',
    'FUCHSIA': 'Fuchsia',
    'PEACOAT': 'Peacoat (deep navy)',
    'PECOAT':  'Peacoat (deep navy)',
    'NUDE':    'Nude',
    'BEIGE':   'Beige',
    'IVORY':   'Ivory',
    'ECRU':    'Ecru',
    'MUSTARD': 'Mustard',
    'BURGUNDY':'Burgundy',
    'BURGANDY':'Burgundy',
    'BORDEAUX':'Bordeaux',
    'CORAL':   'Coral',
    'MINT':    'Mint',
    'ROSE':    'Rose',
    'SAGE':    'Sage',
    'TEAL':    'Teal',
    'MAUVE':   'Mauve',
    'OCHRE':   'Ochre',
    'ROYAL':   'Royal Blue',
    'PEACH':   'Peach',
    'TURQUOISE': 'Turquoise',
    'VIOLET':  'Violet',
    'WINE':    'Wine',
    'TAN':     'Tan',
    'GOLD':    'Gold',
    'AMBER':   'Amber',
    'NEGRO':   'Negro (Black, es)',
    'BLANCO':  'Blanco (White, es)',
    # Process / state suffixes that appear on their own
    'SMO':     'Smoke',
    'BRIG':    'Bright',
    'BRIGHT':  'Bright',
}


# --- API endpoints ---
@frappe.whitelist(allow_guest=True)
#@auth_required
def decode(num_rows:int=500):
    '''
    Decodes the "fabric_code" column value of the top 500 rows with null "decoded_on" of the
    doctype "Dyed Fabric Master" and saves the decoded values using the _decoded() function.
    '''
    try:
        rows = frappe.get_all(
            'Dyed Fabric Master',
            filters={'decoded_at': ['is', 'not set']},
            fields=['fabric_id', 'fabric_code'],
            limit=num_rows,
            order_by='creation asc',
        )

        #return {
        #    'success': True,
        #    'data': rows
        #}


        succeeded, failed = 0, 0
        for row in rows:
            if _decoded(row.fabric_id, row.fabric_code) == 0:
                succeeded += 1
            else:
                failed += 1

        frappe.db.commit()

        return {
            'success': True,
            'data': {
                'attempted': len(rows),
                'succeeded': succeeded,
                'failed': failed,
            },
        }

    except Exception as ex:
        frappe.db.rollback()
        frappe.log_error(frappe.get_traceback(), 'dyed.decode()')
        return {'success': False, 'error': str(ex)}


# --- helpers ---
def _decoded(id: str, code: str):
    try:
        parsed = _parse_fabric_code(code)

        if not frappe.db.exists('Dyed Fabric Master', id):
            raise Exception(f'ID [{id}] does not exist!')

        doc = frappe.get_doc('Dyed Fabric Master', id)

        doc.base_code       = (parsed['base']      or {}).get('code')
        doc.base_label      = (parsed['base']      or {}).get('label')
        doc.structure_code  = (parsed['structure'] or {}).get('code')
        doc.structure_label = (parsed['structure'] or {}).get('label')
        doc.structure_ratio = (parsed['structure'] or {}).get('ratio')
        doc.modifiers       = ', '.join(parsed['modifiers'])
        doc.has_elastane    = parsed['elastane_flag']
        doc.gsm             = parsed['gsm']
        doc.colour_code     = (parsed['colour'] or {}).get('code')
        doc.colour_label    = (parsed['colour'] or {}).get('label')
        doc.colour_shade    = (parsed['colour'] or {}).get('shade')
        doc.warnings        = '\n'.join(parsed['warnings'])
        doc.decoded_at      = frappe.utils.now()

        doc.set('blend', [{'percent': b['pct'], 'fibre_code': b['code'], 'fibre_name': b['name']}
                        for b in parsed['blend']])
        
        doc.save(ignore_permissions=True)
        
    except Exception as ex:
        frappe.log_error(frappe.get_traceback(), 'dyed._decoded()')
        return -1

    return 0

def _parse_fabric_code(code: str) -> dict:
    ''' Pure parser. Returns parsed parts and any warnings about unknown/ambiguous tokens. '''
    out = {
        'raw': code,
        'base': None,
        'structure': None,
        'modifiers': [],
        'elastane_flag': False,
        'blend': [],
        'gsm': None,
        'colour': None,
        'warnings': [],
    }

    if not code or not code.strip():
        out['warnings'].append('Empty input')
        return out

    if '/' in code:
        fabric_part, _, colour_part = code.partition('/')
    else:
        fabric_part, colour_part = code, ''
        out['warnings'].append("Missing '/' separator — colour suffix not found")

    # Underscores can stand in for spaces inside multi-word codes
    # (e.g. WFL_RIB, 2T_FLC, SJY_EL). Normalise both forms.
    normalised = fabric_part.strip().replace('_', ' ')
    tokens = normalised.split()
    n = len(tokens)
    i = 0

    # 1. Base
    base_code, used = _longest_match(tokens, i, BASE_CODES, max_span=2)
    if base_code:
        out['base'] = {'code': base_code, 'label': BASE_CODES[base_code]}
        i += used
    else:
        seen = tokens[i] if i < n else '<missing>'
        out['warnings'].append(f"Unknown base/state token: '{seen}'")

    # 2. Structure (may be multi-word — longest match wins)
    struct_code, used = _longest_match(tokens, i, STRUCTURE_CODES, max_span=5)
    structure_ratio = None
    if struct_code:
        i += used
        # 3. Optional structure ratio (1X1, 2X2, 9X1, 3X3...)
        if i < n and re.fullmatch(r'\d+[Xx]\d+', tokens[i]):
            structure_ratio = tokens[i].upper()
            i += 1
        out['structure'] = {
            'code': struct_code,
            'label': STRUCTURE_CODES[struct_code],
            'ratio': structure_ratio,
        }
    else:
        out['warnings'].append('Structure not recognised')

    # 4. Optional EL flag and structure modifiers (interleaved order varies)
    while i < n:
        t = tokens[i].upper()
        if t == 'EL':
            out['elastane_flag'] = True
            i += 1
        elif t in STRUCTURE_MODIFIERS:
            label = STRUCTURE_MODIFIERS[t]
            if label and label not in out['modifiers']:
                out['modifiers'].append(label)
            i += 1
        else:
            break

    # 5. Find GSM as the LAST plain integer in the remaining fabric tokens.
    gsm_idx = None
    for j in range(n - 1, i - 1, -1):
        if tokens[j].isdigit():
            gsm_idx = j
            break

    if gsm_idx is not None:
        out['gsm'] = int(tokens[gsm_idx])
        blend_tokens = tokens[i:gsm_idx]
        leftover = tokens[gsm_idx + 1:]
        if leftover:
            leftover_str = ' '.join(leftover)
            # Recovery: if no '/' was supplied, treat the leftover as the colour.
            if not colour_part.strip():
                colour_part = leftover_str
            else:
                out['warnings'].append(f"Unparsed tokens after GSM: '{leftover_str}'")
    else:
        blend_tokens = tokens[i:]
        out['warnings'].append('GSM not found')

    # 6. Blend
    if blend_tokens:
        out['blend'] = _parse_blend(blend_tokens, out['warnings'])
    else:
        out['warnings'].append('Blend specification not found')

    # 7. Colour suffix
    colour_part = colour_part.strip()
    if colour_part:
        out['colour'] = _parse_colour(colour_part)

    # Cross-check: EL flag should agree with the blend. LY may be Lycra (an
    # elastane brand) or Lyocell — accept it here so we don't double-warn.
    elastane_codes = {'E', 'EL', 'ROICA', 'LY'}
    if out['elastane_flag'] and not any(b['code'] in elastane_codes for b in out['blend']):
        out['warnings'].append('EL flag set but no Elastane in blend ratio')

    return out

def _longest_match(tokens: list, start: int, table: dict, max_span: int = 4):
    ''' Find the longest run of tokens (uppercased, joined by spaces) that is a key
    in ``table``. Returns (matched_key, num_tokens_consumed) or (None, 0). '''
    upper_limit = min(max_span, len(tokens) - start)
    for span in range(upper_limit, 0, -1):
        candidate = ' '.join(t.upper() for t in tokens[start:start + span])
        if candidate in table:
            return candidate, span
    return None, 0

def _parse_blend(blend_tokens: list, warnings: list) -> list:
    text = ' '.join(blend_tokens)

    # Strip parenthesised annotations like "(BABY LOOP)".
    text = re.sub(r'\([^)]*\)', ' ', text)

    # Drop yarn-process annotations.
    cleaned = [t for t in text.split() if t.upper() not in YARN_ANNOTATIONS]
    text = ' '.join(cleaned)

    # Merge known compound fibres so the second word doesn't survive splitting
    # as a separate fibre code.
    for compound in COMPOUND_FIBRES:
        text = re.sub(
            rf'\b{re.escape(compound)}\b',
            compound.split()[0],
            text,
            flags=re.IGNORECASE,
        )

    # Split on whitespace and colons.
    parts = [p for p in re.split(r'[\s:]+', text.strip()) if p]
    nums, codes = [], []
    for p in parts:
        if re.fullmatch(r'\d+(?:\.\d+)?', p):
            nums.append(p)
        else:
            codes.append(p)

    if len(nums) != len(codes):
        warnings.append(
            f"Blend ratio/fibre count mismatch in '{' '.join(blend_tokens)}': "
            f"{len(nums)} ratios vs {len(codes)} fibre codes"
        )

    blend = []
    for pct_str, fcode in zip(nums, codes):
        fcode_u = fcode.upper()
        name = FIBRE_CODES.get(fcode_u)
        if name is None:
            warnings.append(f"Unknown fibre code '{fcode}'")
            name = f"Unknown ({fcode})"
        if fcode_u in AMBIGUOUS_FIBRES:
            warnings.append(
                f"Fibre '{fcode_u}' is mill-dependent — could mean: "
                + ', '.join(AMBIGUOUS_FIBRES[fcode_u])
            )
        try:
            pct_val = float(pct_str)
            pct = int(pct_val) if pct_val == int(pct_val) else pct_val
        except ValueError:
            pct = 0
        blend.append({'pct': pct, 'code': fcode_u, 'name': name})

    total = sum(b['pct'] for b in blend)
    if blend and abs(total - 100) > 0.5:
        warnings.append(f"Blend ratios sum to {total}, not 100")

    return blend

def _parse_colour(token: str) -> dict:
    t = token.upper().strip()

    # Direct match (BLA, NAVY, BRIGHT WHITE, ...).
    if t in COLOUR_TOKENS:
        return {'code': token, 'label': COLOUR_TOKENS[t], 'shade': None}

    # Compound colour code: try matching the longest known prefix from
    # COLOUR_TOKENS, then decode the rest as <shade><suffix><lot>.
    for prefix in sorted(COLOUR_TOKENS, key=len, reverse=True):
        if not t.startswith(prefix) or len(t) == len(prefix):
            continue
        rest = t[len(prefix):]
        m = re.match(r'^(\d+)([A-Z]+)?(\d*)$', rest)
        if not m:
            continue
        shade, suffix, lot = m.group(1), m.group(2), m.group(3)
        labels = [COLOUR_TOKENS[prefix]]
        if suffix and suffix in COLOUR_TOKENS:
            labels.append(COLOUR_TOKENS[suffix])
        shade_lot = '/'.join(s for s in (shade, lot) if s)
        return {
            'code': token,
            'label': ', '.join(labels),
            'shade': shade_lot or None,
        }

    # No structured match — return the original text title-cased.
    return {'code': token, 'label': token.title(), 'shade': None}

#import json
#code = 'SPD RIB 1X1 EL 97:3:C:E 280 / NAVY'
#res = decode(code)
#print(json.dumps(res, indent=2))
