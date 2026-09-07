'''
Buyer PO analytics — a single read endpoint that turns the Buyer PO staging
records (and their line items) into chart-ready aggregates.

    get_analytics(...) -> {generatedFor, filters, kpis, kpiInfo, charts}

Design notes:

  * Works the moment data lands. Buyer PO is a staging DocType — rows arrive as
    Draft straight off the importer and are only later promoted to Verified. This
    endpoint counts EVERY status (Draft/Parsed/Verified/Failed), so the numbers
    are live as soon as an import finishes; verification quality is itself one of
    the charts rather than a precondition for any of them.

  * One round trip to the DB. We pull the header rows and the line rows in two
    bulk reads and fold everything in Python. Cardinality here is small (a staging
    queue), and doing it in memory keeps the multi-currency handling honest —
    which a pile of GROUP BY queries could not.

  * Money is never summed across currencies. A PO can be in EUR, INR, ... so every
    monetary figure is reported PER currency (`valueByCurrency`, and a `currency`
    tag on each bucket that is single-currency, `MIXED` otherwise). Quantities and
    counts are currency-agnostic and summed freely.

  * Self-describing. Every KPI and every chart ships the plain-English copy for
    its own "i" tooltip — `kpiInfo` for the tiles, and an `info` + `fields` pair
    on each chart. The frontend renders the help text, it does not author it, so
    the explanation can never drift from the aggregation that produced it. The
    copy is written for a merchandiser, not a developer, and calls out the traps
    (free-text buckets, non-comparable currencies, excluded rows) where a chart
    is easy to misread.

Auth mirrors the other frontend read APIs: X-Auth-Token JWT, internal users only
(brand users get 403).
'''

import frappe
from frappe.utils import getdate, date_diff, flt

from prism.auth.authenticator import auth_required
import prism.api.util as util

PO_DOCTYPE = 'Buyer PO'
LINE_DOCTYPE = 'Buyer PO Line Item'

# Header columns we aggregate. Pulled in one read.
_HEADER_FIELDS = [
    'name', 'brand', 'buyer_entity', 'po_date', 'delivery_date', 'po_type',
    'product_category', 'season', 'currency', 'total_order_value', 'total_order_qty',
    'computed_total_value', 'computed_total_qty', 'qty_variance', 'value_variance',
    'incoterm', 'mode_of_transport', 'country_of_origin', 'supplier_name',
    'certification', 'source_format', 'extraction_status', 'is_assorted',
]

# Line columns we aggregate (colour / size demand curves, unit-price stats).
_LINE_FIELDS = [
    'parent', 'quantity', 'unit_price', 'line_amount', 'colour_name',
    'size', 'size_range', 'currency',
]

# Fixed order for the status funnel so the chart axis is stable even when a
# status has zero rows.
_STATUS_ORDER = ('Draft', 'Parsed', 'Verified', 'Failed')

# Non-zero threshold for calling a reconciliation variance "off" — matches the
# tolerances the Buyer PO validator uses.
_QTY_TOLERANCE = 0.01
_VALUE_TOLERANCE = 0.05

# Cap for the long-tail categorical charts (colours, sizes, suppliers). Buckets
# beyond this are folded into an "Other" entry rather than silently dropped, and
# `truncated` records how many distinct values were collapsed.
_TOP_N = 20


# Tooltip copy for the KPI tiles, keyed exactly like the `kpis` dict so the
# frontend can hang an "i" icon off each one. `format` tells the tile how to
# render the raw number; `label` is the tile caption.
_KPI_INFO = {
    'totalPos': {
        'label': 'Purchase orders',
        'format': 'count',
        'info': 'How many Buyer POs match the current filters, counted in every '
                'state — freshly imported or fully verified. This is the '
                'denominator behind the percentages on this row.',
    },
    'totalLineItems': {
        'label': 'Line items',
        'format': 'count',
        'info': 'Total order lines across those POs. One line is a single '
                'article/colour/size combination, so a PO with a full size run '
                'contributes many lines.',
    },
    'totalOrderQty': {
        'label': 'Total quantity',
        'format': 'quantity',
        'info': 'Total pieces ordered, taken from the PO header totals. Safe to '
                'add up across every buyer and currency, because it counts units '
                'and not money.',
    },
    'valueByCurrency': {
        'label': 'Order value',
        'format': 'currencyMap',
        'info': 'Committed order value, split out per currency. It is deliberately '
                'not one blended number — EUR, USD and INR are never added '
                'together, so read each currency on its own line.',
    },
    'verifiedCount': {
        'label': 'Verified POs',
        'format': 'count',
        'info': 'POs a human has reviewed and signed off after import. Everything '
                'else is still machine-extracted output awaiting a check.',
    },
    'verifiedPct': {
        'label': 'Verified',
        'format': 'percent',
        'info': 'Share of POs that have cleared human verification. Straight after '
                'an import this sits at 0% and climbs as the team works the '
                'review queue — it measures review progress, not data quality.',
    },
    'posWithVariance': {
        'label': 'POs with variance',
        'format': 'count',
        'info': 'POs where the totals printed on the document disagree with the '
                'sum of their own extracted line items. Usually a sign the '
                'extraction missed or misread a line — review these before '
                'trusting their numbers.',
    },
    'cleanPct': {
        'label': 'Clean extractions',
        'format': 'percent',
        'info': 'Share of POs whose line items add up exactly to the printed '
                'header totals. The headline confidence number for this queue: '
                'the higher it is, the more the charts below can be trusted.',
    },
    'avgLeadTimeDays': {
        'label': 'Avg lead time',
        'format': 'days',
        'info': 'Average days between the PO date and the delivery date — how '
                'much runway buyers are giving production. POs missing either '
                'date are left out, so this can rest on fewer POs than the total.',
    },
}


@frappe.whitelist(allow_guest=True)
@auth_required
def get_analytics(brand=None, source_format=None, status=None,
                  from_date=None, to_date=None):
    '''
    All Buyer PO analytics in one payload.

    Optional filters (all header-level, all AND-ed):
      brand         : exact Brand as printed on the PO
      source_format : parser template (KIABI-ITX / Tchibo / DMart / ...)
      status        : one Extraction Status (Draft/Parsed/Verified/Failed)
      from_date/to_date : inclusive PO-date window (YYYY-MM-DD)

    -> {
         generatedFor: {totalPos, totalLineItems},
         filters:      {..echoed..},
         kpis:         {kpiKey: number, ...},
         kpiInfo:      {kpiKey: {label, format, info}, ...},
         charts:       {chartKey: {type, title, info, fields, data}, ...}
       }
    Every chart's `data` is already in the shape a bar/pie/line renderer wants:
    a list of {label, value, ...} (or {label, series...} for the stacked/time ones).

    `info` (per chart) and `kpiInfo[key].info` are the ready-to-render "i" tooltip
    copy; `fields` names what each numeric key in a data row measures. Both are
    static, so the frontend can render them without a second call.
    '''
    _require_internal()

    filters = _build_filters(brand, source_format, status, from_date, to_date)
    headers = frappe.get_all(
        PO_DOCTYPE, filters=filters, fields=_HEADER_FIELDS,
        limit_page_length=0, ignore_permissions=True,
    )

    # Line items for the headers in scope. When there are no headers, skip the
    # line read entirely (an `in` on an empty set is wasteful and confusing).
    names = [h['name'] for h in headers]
    lines = []
    if names:
        lines = frappe.get_all(
            LINE_DOCTYPE, filters={'parent': ['in', names]}, fields=_LINE_FIELDS,
            limit_page_length=0, ignore_permissions=True,
        )

    return {
        'generatedFor': {'totalPos': len(headers), 'totalLineItems': len(lines)},
        'filters': {
            'brand': brand, 'sourceFormat': source_format, 'status': status,
            'fromDate': from_date, 'toDate': to_date,
        },
        'kpis': _kpis(headers, lines),
        # Static tooltip copy for the tiles, shipped alongside the numbers rather
        # than duplicated in the frontend.
        'kpiInfo': _KPI_INFO,
        'charts': _charts(headers, lines),
    }


# --- KPI row ---

def _kpis(headers, lines):
    '''
    Headline tiles. Monetary totals are a per-currency dict, never one summed
    number — see the module note.
    '''
    value_by_currency = {}
    total_qty = 0.0
    variance_count = 0
    lead_times = []

    for h in headers:
        cur = (h.get('currency') or 'UNKNOWN').strip() or 'UNKNOWN'
        value_by_currency[cur] = value_by_currency.get(cur, 0.0) + flt(h.get('total_order_value'))
        total_qty += flt(h.get('total_order_qty'))

        if _has_variance(h):
            variance_count += 1

        lead = _lead_time_days(h)
        if lead is not None:
            lead_times.append(lead)

    verified = sum(1 for h in headers if h.get('extraction_status') == 'Verified')
    total = len(headers)

    return {
        'totalPos': total,
        'totalLineItems': len(lines),
        'totalOrderQty': round(total_qty, 2),
        # {EUR: 123456.0, INR: 78900.0} — render as tiles or the currency pie.
        'valueByCurrency': {k: round(v, 2) for k, v in value_by_currency.items()},
        'verifiedCount': verified,
        'verifiedPct': round(100.0 * verified / total, 1) if total else 0.0,
        # POs whose line totals disagree with the printed totals — the "how much
        # can I trust this queue" number.
        'posWithVariance': variance_count,
        'cleanPct': round(100.0 * (total - variance_count) / total, 1) if total else 0.0,
        'avgLeadTimeDays': round(sum(lead_times) / len(lead_times), 1) if lead_times else None,
    }


# --- charts ---

# Presentation + help text for every chart, separated from the number crunching
# below. Each entry carries:
#   type   : which renderer to use
#   title  : chart heading
#   info   : the "i" tooltip — what this is, what it shows, and how to not
#            misread it (free-text buckets, excluded rows, currency traps)
#   fields : what each numeric key inside a `data` row actually measures, so a
#            legend or tooltip never has to guess whether `value` is pieces,
#            money or a PO count
_CHART_SPECS = {
    'statusFunnel': {
        'type': 'bar',
        'title': 'Extraction status',
        'info': 'Where every PO in this view sits in the import workflow: Draft '
                '(extracted, not yet checked), Parsed, Verified (signed off by a '
                'person) and Failed. Straight after an import everything is '
                'Draft — read this as your review backlog.',
        'fields': {'value': 'POs in this status'},
    },
    'ordersOverTime': {
        'type': 'line',
        'title': 'Orders over time (by PO month)',
        'info': 'Order intake by the month the PO was raised. Shows whether '
                'business is speeding up or slowing down. Quantity and PO count '
                'are plotted together — the two diverging means order sizes are '
                'changing, not just order numbers.',
        'fields': {'pos': 'POs raised', 'qty': 'Pieces ordered'},
    },
    'deliveryTimeline': {
        'type': 'bar',
        'title': 'Order book by delivery month',
        'info': 'The same orders arranged by when the goods are due out, rather '
                'than when they were ordered. This is the capacity view: a tall '
                'month is a production crunch you can see coming.',
        'fields': {'pos': 'POs due', 'qty': 'Pieces due'},
    },
    'valueByBrand': {
        'type': 'bar',
        'title': 'Order value by brand',
        'info': 'Committed order value per buyer brand — who your biggest '
                'customers are by money. Each bar is tagged with its own '
                'currency; a bar marked MIXED means that brand ordered in more '
                'than one currency, so its total is a raw sum and not comparable '
                'with the others.',
        'fields': {'value': 'Order value (in the bar\'s own currency)',
                   'pos': 'POs from this brand'},
    },
    'qtyByBrand': {
        'type': 'bar',
        'title': 'Order quantity by brand',
        'info': 'The same brands ranked by pieces instead of money, so every bar '
                'is directly comparable regardless of currency. Compare against '
                'order value to spot brands that buy high volume at low price, '
                'or the reverse.',
        'fields': {'value': 'Pieces ordered', 'pos': 'POs from this brand'},
    },
    'categoryMix': {
        'type': 'pie',
        'title': 'Product category mix',
        'info': 'Share of pieces ordered by garment category — what you actually '
                'make. Categories are taken verbatim off the PO, so variant '
                'spellings of the same thing (for example "Strap Top" and "STRAP '
                'TOP") show up as separate slices until they are mapped to '
                'masters.',
        'fields': {'value': 'Pieces ordered', 'pos': 'POs in this category'},
    },
    'seasonMix': {
        'type': 'bar',
        'title': 'Demand by season',
        'info': 'Pieces ordered per buying season, showing how the order book is '
                'loaded across seasons. "Unspecified" collects POs where no '
                'season was printed or extracted.',
        'fields': {'value': 'Pieces ordered', 'pos': 'POs in this season'},
    },
    'currencyExposure': {
        'type': 'pie',
        'title': 'Currency exposure (order value)',
        'info': 'How much order value is committed in each currency — your FX '
                'exposure at a glance. Read the numbers, not the slice sizes: '
                'the amounts are in different currencies and have not been '
                'converted, so a large INR slice is not necessarily more money '
                'than a small EUR one.',
        'fields': {'value': 'Order value (in that currency)',
                   'pos': 'POs in this currency'},
    },
    'transportMode': {
        'type': 'pie',
        'title': 'Mode of transport',
        'info': 'How the goods ship — sea, air, road or rail. A rising air share '
                'usually means orders are running late or urgent, and it costs '
                'far more per piece than sea.',
        'fields': {'value': 'POs shipping this way'},
    },
    'incoterm': {
        'type': 'bar',
        'title': 'Incoterm distribution',
        'info': 'The delivery term agreed on each PO, which decides who pays '
                'freight and who carries the risk, and from which point. Terms '
                'are free text off the document, so wording varies between '
                'buyers even where the term is the same.',
        'fields': {'value': 'POs on this term'},
    },
    'countryOfOrigin': {
        'type': 'bar',
        'title': 'Country of origin',
        'info': 'Where the goods are declared to be made, by pieces ordered — '
                'relevant for duty and compliance. Values come straight off the '
                'PO in the buyer\'s own language, so the same country can appear '
                'under more than one spelling (for example "India" and "INDE").',
        'fields': {'value': 'Pieces ordered', 'pos': 'POs from this origin'},
    },
    'topSuppliers': {
        'type': 'bar',
        'title': 'Top suppliers by quantity',
        'info': 'Which manufacturing units the volume is going to — your '
                'concentration risk. Supplier names are free text, so the same '
                'factory can appear more than once if buyers write its address '
                f'differently. Top {_TOP_N} shown; the rest are grouped into '
                '"Other".',
        'fields': {'value': 'Pieces ordered', 'pos': 'POs to this supplier'},
    },
    'parserQuality': {
        'type': 'stackedBar',
        'title': 'Extraction status by source format',
        'info': 'Progress through the workflow broken down by importer template '
                '(one per buyer document layout). A format stuck in Draft is '
                'simply unreviewed; one accumulating Failed rows points at a '
                'parser that needs work on that layout.',
        'fields': {'Draft': 'Awaiting review', 'Parsed': 'Parsed',
                   'Verified': 'Signed off', 'Failed': 'Extraction failed'},
    },
    'reconciliationHealth': {
        'type': 'pie',
        'title': 'Reconciliation health',
        'info': 'A self-check on each PO: do its line items add up to the totals '
                'printed on the document? "Clean" means both quantity and value '
                'agree. A variance almost always means the extraction dropped or '
                'misread a line, so those POs need a look before their numbers '
                'are used anywhere.',
        'fields': {'value': 'POs in this state'},
    },
    'certificationCoverage': {
        'type': 'pie',
        'title': 'Certified vs uncertified orders',
        'info': 'How many orders carry a sustainability or compliance '
                'certification (GOTS, OCS and similar). Counts any PO with a '
                'certification recorded, so it measures coverage, not which '
                'standard or whether the certificate is still valid.',
        'fields': {'value': 'POs'},
    },
    'unitPriceByCurrency': {
        'type': 'stat',
        'title': 'Unit price stats (per currency)',
        'info': 'The cheapest, dearest and average price per piece across all '
                'order lines, kept separate per currency because prices in '
                'different currencies cannot be averaged together. A wide '
                'min-to-max spread means the mix runs from basic to premium.',
        'fields': {'count': 'Priced lines', 'min': 'Lowest unit price',
                   'max': 'Highest unit price', 'avg': 'Average unit price'},
    },
    'sizeCurve': {
        'type': 'bar',
        'title': 'Size demand curve (quantity)',
        'info': 'Pieces ordered per size — the demand curve you cut and buy '
                'fabric against. Where a PO only states a span rather than sizes '
                '(for example "S-2XL") the whole span appears as one bar, so '
                'those quantities are not split across individual sizes.',
        'fields': {'value': 'Pieces ordered'},
    },
    'colourPopularity': {
        'type': 'bar',
        'title': 'Top colours by quantity',
        'info': 'Which colours the volume sits in. Names are the buyer\'s own '
                'colour codes off the PO, untranslated, so the same shade may be '
                f'named differently by different buyers. Top {_TOP_N} shown; the '
                'rest are grouped into "Other".',
        'fields': {'value': 'Pieces ordered', 'lines': 'Order lines'},
    },
    'leadTimeDistribution': {
        'type': 'bar',
        'title': 'Lead time (PO date -> delivery date)',
        'info': 'How many days you get between the PO being raised and the goods '
                'being due, grouped into bands. Weight in the short bands means '
                'buyers are giving you little runway. POs missing either date, '
                'or dated delivery-before-order, are left out.',
        'fields': {'value': 'POs in this band'},
    },
}


def _charts(headers, lines):
    '''
    Every chart, keyed. Each value is the chart's spec from _CHART_SPECS
    (type/title/info/fields) with its aggregated `data` attached — so a chart
    can never ship without the help text that explains it.
    '''
    data = {
        'statusFunnel': _status_funnel(headers),
        'ordersOverTime': _orders_over_time(headers),
        'deliveryTimeline': _delivery_timeline(headers),
        'valueByBrand': _money_by(headers, 'brand'),
        'qtyByBrand': _qty_by(headers, 'brand'),
        'categoryMix': _qty_by(headers, 'product_category'),
        'seasonMix': _qty_by(headers, 'season'),
        'currencyExposure': _currency_exposure(headers),
        'transportMode': _count_by(headers, 'mode_of_transport'),
        'incoterm': _count_by(headers, 'incoterm'),
        'countryOfOrigin': _qty_by(headers, 'country_of_origin'),
        'topSuppliers': _qty_by(headers, 'supplier_name', top_n=_TOP_N),
        'parserQuality': _parser_quality(headers),
        'reconciliationHealth': _reconciliation_health(headers),
        'certificationCoverage': _certification_coverage(headers),
        'unitPriceByCurrency': _unit_price_stats(lines),
        'sizeCurve': _size_curve(lines),
        'colourPopularity': _line_qty_by(lines, 'colour_name', top_n=_TOP_N),
        'leadTimeDistribution': _lead_time_distribution(headers),
    }
    # KeyError here is intentional: a new chart added without its help text is a
    # bug to catch in development, not something to ship with an empty tooltip.
    return {key: {**_CHART_SPECS[key], 'data': rows} for key, rows in data.items()}


# --- categorical aggregators ---

def _count_by(headers, field):
    ''' [{label, value:count}] over a header field, blanks -> "Unspecified". '''
    buckets = {}
    for h in headers:
        key = _label(h.get(field))
        buckets[key] = buckets.get(key, 0) + 1
    return _sorted_counts(buckets)


def _qty_by(headers, field, top_n=None):
    ''' [{label, value:qty, pos:count}] over a header field, sorted by qty desc. '''
    qty, pos = {}, {}
    for h in headers:
        key = _label(h.get(field))
        qty[key] = qty.get(key, 0.0) + flt(h.get('total_order_qty'))
        pos[key] = pos.get(key, 0) + 1
    rows = [{'label': k, 'value': round(v, 2), 'pos': pos[k]} for k, v in qty.items()]
    rows.sort(key=lambda r: r['value'], reverse=True)
    return _cap(rows, top_n)


def _money_by(headers, field, top_n=None):
    '''
    [{label, value, currency, pos}] over a header field. `currency` is the single
    ISO code when the bucket is homogeneous, else "MIXED" (and value is then the
    raw sum of differing currencies — flagged, not silently blended).
    '''
    value, pos, currencies = {}, {}, {}
    for h in headers:
        key = _label(h.get(field))
        value[key] = value.get(key, 0.0) + flt(h.get('total_order_value'))
        pos[key] = pos.get(key, 0) + 1
        currencies.setdefault(key, set()).add((h.get('currency') or 'UNKNOWN').strip() or 'UNKNOWN')
    rows = []
    for k, v in value.items():
        curs = currencies[k]
        rows.append({
            'label': k, 'value': round(v, 2), 'pos': pos[k],
            'currency': next(iter(curs)) if len(curs) == 1 else 'MIXED',
        })
    rows.sort(key=lambda r: r['value'], reverse=True)
    return _cap(rows, top_n)


def _currency_exposure(headers):
    ''' [{label:currency, value, pos}] — FX exposure by order value. '''
    value, pos = {}, {}
    for h in headers:
        cur = (h.get('currency') or 'UNKNOWN').strip() or 'UNKNOWN'
        value[cur] = value.get(cur, 0.0) + flt(h.get('total_order_value'))
        pos[cur] = pos.get(cur, 0) + 1
    rows = [{'label': k, 'value': round(v, 2), 'pos': pos[k]} for k, v in value.items()]
    rows.sort(key=lambda r: r['value'], reverse=True)
    return rows


# --- status / quality ---

def _status_funnel(headers):
    ''' Counts per Extraction Status, in the fixed workflow order. '''
    counts = {s: 0 for s in _STATUS_ORDER}
    for h in headers:
        s = h.get('extraction_status') or 'Draft'
        counts[s] = counts.get(s, 0) + 1
    # keep the canonical order first, then any unexpected status at the end
    ordered = [{'label': s, 'value': counts[s]} for s in _STATUS_ORDER]
    ordered += [{'label': s, 'value': c} for s, c in counts.items() if s not in _STATUS_ORDER]
    return ordered


def _parser_quality(headers):
    '''
    Stacked bar: one row per source_format, a count per status. Shows which parser
    template is landing clean Verified rows vs stalling in Draft/Failed.
    '''
    by_format = {}
    for h in headers:
        fmt = _label(h.get('source_format'))
        row = by_format.setdefault(fmt, {'label': fmt, **{s: 0 for s in _STATUS_ORDER}})
        s = h.get('extraction_status') or 'Draft'
        row[s] = row.get(s, 0) + 1
    rows = list(by_format.values())
    rows.sort(key=lambda r: sum(r[s] for s in _STATUS_ORDER), reverse=True)
    return rows


def _reconciliation_health(headers):
    '''
    Clean vs qty-variance vs value-variance vs both. A PO can fail on either the
    quantity or the value reconciliation (or both), so it is classed once by the
    strongest problem it has.
    '''
    clean = qty_only = value_only = both = 0
    for h in headers:
        q = abs(flt(h.get('qty_variance'))) > _QTY_TOLERANCE
        v = abs(flt(h.get('value_variance'))) > _VALUE_TOLERANCE
        if q and v:
            both += 1
        elif q:
            qty_only += 1
        elif v:
            value_only += 1
        else:
            clean += 1
    return [
        {'label': 'Clean', 'value': clean},
        {'label': 'Qty variance', 'value': qty_only},
        {'label': 'Value variance', 'value': value_only},
        {'label': 'Qty + value variance', 'value': both},
    ]


def _certification_coverage(headers):
    ''' Certified vs uncertified order count. '''
    certified = sum(1 for h in headers if (h.get('certification') or '').strip())
    return [
        {'label': 'Certified', 'value': certified},
        {'label': 'Uncertified', 'value': len(headers) - certified},
    ]


# --- time series ---

def _orders_over_time(headers):
    ''' [{label:'YYYY-MM', pos, qty}] over PO month, chronological. '''
    return _month_series(headers, 'po_date')


def _delivery_timeline(headers):
    ''' [{label:'YYYY-MM', pos, qty}] over delivery month, chronological. '''
    return _month_series(headers, 'delivery_date')


def _month_series(headers, field):
    buckets = {}
    for h in headers:
        d = h.get(field)
        if not d:
            continue
        key = getdate(d).strftime('%Y-%m')
        row = buckets.setdefault(key, {'label': key, 'pos': 0, 'qty': 0.0})
        row['pos'] += 1
        row['qty'] += flt(h.get('total_order_qty'))
    rows = sorted(buckets.values(), key=lambda r: r['label'])
    for r in rows:
        r['qty'] = round(r['qty'], 2)
    return rows


def _lead_time_distribution(headers):
    '''
    Histogram of (delivery_date - po_date) in day bands. Negative or missing spans
    are skipped rather than bucketed as 0 (a delivery-before-PO date is a data
    error, not a zero lead time).
    '''
    bands = [
        ('0-30', 0, 30), ('31-60', 31, 60), ('61-90', 61, 90),
        ('91-120', 91, 120), ('121-180', 121, 180), ('180+', 181, None),
    ]
    counts = {b[0]: 0 for b in bands}
    for h in headers:
        lead = _lead_time_days(h)
        if lead is None or lead < 0:
            continue
        for label, lo, hi in bands:
            if lead >= lo and (hi is None or lead <= hi):
                counts[label] += 1
                break
    return [{'label': label, 'value': counts[label]} for label, _, _ in bands]


# --- line-item aggregators ---

def _line_qty_by(lines, field, top_n=None):
    ''' [{label, value:qty, lines:count}] over a line field, sorted by qty desc. '''
    qty, cnt = {}, {}
    for ln in lines:
        key = _label(ln.get(field))
        qty[key] = qty.get(key, 0.0) + flt(ln.get('quantity'))
        cnt[key] = cnt.get(key, 0) + 1
    rows = [{'label': k, 'value': round(v, 2), 'lines': cnt[k]} for k, v in qty.items()]
    rows.sort(key=lambda r: r['value'], reverse=True)
    return _cap(rows, top_n)


def _size_curve(lines):
    '''
    Quantity by size. A line carries either a discrete `size` or, for orders that
    only give a span (DMart), a `size_range` — fall back to the range so those
    rows still land on the curve.
    '''
    qty = {}
    for ln in lines:
        key = _label(ln.get('size') or ln.get('size_range'))
        qty[key] = qty.get(key, 0.0) + flt(ln.get('quantity'))
    rows = [{'label': k, 'value': round(v, 2)} for k, v in qty.items()]
    rows.sort(key=lambda r: r['value'], reverse=True)
    return _cap(rows, _TOP_N)


def _unit_price_stats(lines):
    '''
    Per-currency unit-price min/max/avg/count. Prices are NOT comparable across
    currencies, so this is a dict keyed by currency rather than one histogram.
    '''
    by_cur = {}
    for ln in lines:
        price = flt(ln.get('unit_price'))
        if not price:
            continue
        cur = (ln.get('currency') or 'UNKNOWN').strip() or 'UNKNOWN'
        agg = by_cur.setdefault(cur, {'count': 0, 'sum': 0.0, 'min': price, 'max': price})
        agg['count'] += 1
        agg['sum'] += price
        agg['min'] = min(agg['min'], price)
        agg['max'] = max(agg['max'], price)
    return {
        cur: {
            'count': a['count'],
            'min': round(a['min'], 4),
            'max': round(a['max'], 4),
            'avg': round(a['sum'] / a['count'], 4),
        }
        for cur, a in by_cur.items()
    }


# --- small helpers ---

def _has_variance(h):
    return (abs(flt(h.get('qty_variance'))) > _QTY_TOLERANCE
            or abs(flt(h.get('value_variance'))) > _VALUE_TOLERANCE)


def _lead_time_days(h):
    ''' delivery_date - po_date in days, or None when either date is missing. '''
    po, delivery = h.get('po_date'), h.get('delivery_date')
    if not po or not delivery:
        return None
    return date_diff(delivery, po)


def _label(value):
    ''' Normalise a grouping key; blank/None -> "Unspecified". '''
    s = (value or '').strip() if isinstance(value, str) else value
    return s if s else 'Unspecified'


def _sorted_counts(buckets):
    rows = [{'label': k, 'value': v} for k, v in buckets.items()]
    rows.sort(key=lambda r: r['value'], reverse=True)
    return rows


def _cap(rows, top_n):
    '''
    Keep the top `top_n` rows; fold the remainder into a single "Other (+N)" row
    carrying the summed numeric fields, so the long tail is disclosed, not dropped.
    '''
    if not top_n or len(rows) <= top_n:
        return rows

    head, tail = rows[:top_n], rows[top_n:]
    numeric_keys = [k for k, v in head[0].items() if k != 'label' and isinstance(v, (int, float))]
    other = {'label': f'Other (+{len(tail)})'}
    for k in numeric_keys:
        other[k] = round(sum(r.get(k, 0) for r in tail), 2)
    head.append(other)
    return head


def _build_filters(brand, source_format, status, from_date, to_date):
    ''' Header-level filter dict from the optional query params. '''
    filters = {}
    if brand:
        filters['brand'] = brand
    if source_format:
        filters['source_format'] = source_format
    if status:
        filters['extraction_status'] = status
    if from_date and to_date:
        filters['po_date'] = ['between', [from_date, to_date]]
    elif from_date:
        filters['po_date'] = ['>=', from_date]
    elif to_date:
        filters['po_date'] = ['<=', to_date]
    return filters


def _require_internal():
    if util.get_current_brand():
        frappe.throw('Only internal users can access this resource.', frappe.PermissionError)
