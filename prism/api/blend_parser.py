import frappe

# Fabric component mappings
# `RC` is recycled COTTON, not recycled polyester: "90:5:5 BCI:RC:EL" parses to
# 95% cotton / 5% elastane, which is exactly Fabric Master's
# "90.25% Cotton 5% Spandex 4.75% Cotton" -- one of 121 rows carrying the
# two-cotton-yarn pattern. Reading it as poly split those blends in half.
# surplus_recommender.FIBRE_GROUP_BY_CODE has always grouped it as cotton; this
# is the copy that disagreed.
COTTON_CODES = ['C', 'O', 'BCI', 'FTO', 'OCA', 'ROC', 'VPC', 'TC', 'IC2', 'CIR', 'MM', 'GC', 'B', 'EV', 'LV', 'FT', 'RC']
POLY_CODES    = ['P', 'RP', 'PCL']
NYLON_CODES   = ['N']
ELASTANE_CODES = ['EL', 'ELR', 'E']

# Keywords in 100% blends mapped to fiber
HUNDRED_PERCENT_MAP = {
    'cotton':   ['COTTON', 'ORGANIC COTTON', 'FAIR TRADE ORG COTTON', 'FAIR TRADE COTTON',
                 'BCI COTTON', 'OCA COTTON', 'ROC VASUDHA PRIMO COTTON', 'VASUDHA PRIMO COTTON',
                 'TRANSITIONAL COTTON', 'SUPIMA', 'FAIR TRADE ORG COTTON'],
    'poly':     ['POLYESTER', 'RECYCLE_POLY'],
    'nylon':    [],
    'elastane': ['EXCEL'],  # Excel = Elastane brand
    'viscose':  ['VISCOSE'],
}


@frappe.whitelist(allow_guest=True)
def analyze_blend(blend_str):
    ret_val = {}

    full_blend = _parse_hundred_percent(blend_str)
    if full_blend:
        return full_blend

    for code in COTTON_CODES:
        if f' {code}:' in blend_str or f':{code}:' in blend_str or blend_str.endswith(f':{code}'):
            ret_val['cotton'] = 'Y'
            break

    for code in POLY_CODES:
        if f' {code}:' in blend_str or f':{code}:' in blend_str or blend_str.endswith(f':{code}'):
            ret_val['poly'] = 'Y'
            break

    for code in NYLON_CODES:
        if f' {code}:' in blend_str or f':{code}:' in blend_str or blend_str.endswith(f':{code}'):
            ret_val['nylon'] = 'Y'
            break

    for code in ELASTANE_CODES:
        if f' {code}:' in blend_str or f':{code}:' in blend_str or blend_str.endswith(f':{code}'):
            ret_val['elastane'] = 'Y'
            break
 
    return ret_val


#--- helpers ---
def _parse_hundred_percent(blend_str):
    """
    Parse blends like '100% ORGANIC COTTON' or '100% POLYESTER'.
    Returns dict like {'cotton': 100.0} or {'poly': 100.0}
    """
    # Normalize: remove '100%' and extra whitespace
    text = blend_str.upper()
    text = text.replace('100%', '').strip()
    # Also handle malformed entries like "100%66.00 : ORGANIC COTTONO:"
    # by extracting just the meaningful word content
    import re
    text = re.sub(r'[\d\.\:\s]+', ' ', text).strip()

    for fiber, keywords in HUNDRED_PERCENT_MAP.items():
        for kw in keywords:
            if kw.upper() in text:
                #return {fiber: 100.0}
                return {fiber: 'Y'}

    return None
