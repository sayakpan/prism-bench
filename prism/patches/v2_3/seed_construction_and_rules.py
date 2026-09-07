'''
v2_3 patch: reseed Fabric Construction Family from the catalogue itself, and
fill Garment Fabric Rule with real weight bands and permitted constructions for
every trim-costing style.

Replaces the v2_2 seed, which was wrong twice over:

  * Fabric Construction Family held nineteen abstract family keys ("jersey",
    "rib") rather than the constructions the catalogue actually names. It did
    not reconcile against Fabric Master grouped by `fabric`, which is the view
    anyone would check it against, and half its labels were reporting folds --
    Ottoman and Purl Jersey both displayed as "Single Jersey".
  * Garment Fabric Rule carried unreviewed drafts whose weight bands were
    inferred from thirty-nine techpacks, thirteen of the twenty-one styles
    having no evidence at all.

Now: one construction row per distinct `Fabric Master.fabric` value, with counts
and weights measured rather than asserted; and one reviewed rule per style,
banded from published apparel-industry weight guidance and cross-checked against
what this catalogue can actually supply.

The rule bands are industry norms, not this mill's house standards -- they are
marked reviewed because they are defensible starting policy rather than
placeholders, and `reviewed_by` says where they came from so a merchandiser can
see what they are overriding. Sources are recorded on each row.

Idempotent: construction rows are refreshed from the catalogue on every run;
rule rows are rewritten unless a human has since changed `reviewed_by`.
'''

import frappe

import prism.api.surplus_recommender as sr

FAMILY_DOCTYPE = 'Fabric Construction Family'
RULE_DOCTYPE = 'Garment Fabric Rule'
FABRIC_MASTER = 'Fabric Master'

SEED_AUTHOR = 'Seeded (industry standard)'

# Values sitting in Fabric Master.fabric that are not constructions. They name
# how the yarn was spun or are a stray mill code that escaped the column, so
# they are recorded (the catalogue has them) but marked inactive.
INACTIVE_CONSTRUCTIONS = {'EL', 'COMPACT', 'EL COMPACT', 'RIB_1X1 EL'}

SOURCES = (
    'Sources: fabricconsult.com/knitted-fabric-gsm-guide, '
    'romiegroup.com knit-fabric-gsm-guide-for-apparel-buyers, '
    'massindia.in how-to-select-the-right-gsm-for-knitted-garments, '
    'eysan.com.tw knit fabric guide.'
)

# style -> (min gsm, max gsm, permitted constructions, requires stretch, rationale)
#
# Bands are the published ranges for the garment, widened only where two sources
# disagreed. Constructions are named exactly as Fabric Master names them, and
# every one listed here exists in the catalogue.
RULES = {
    'T-shirt': (
        140, 200,
        ['Single Jersey', 'Interlock', 'Pique', 'Pointelle', 'Waffle'], 0,
        'Single jersey is the standard T-shirt construction: 140 gsm lightweight, '
        '160 gsm the global workhorse, 180-200 gsm premium and oversized. '
        'Interlock is used for premium tees where dimensional stability matters.'),
    'Tank Top': (
        120, 180,
        ['Single Jersey', 'Rib', 'Pointelle'], 0,
        'Lighter than a tee because there are no sleeves to hold shape; rib is '
        'common for a fitted vest, pointelle for open-work summer styles.'),
    'Crop Top': (
        140, 200,
        ['Single Jersey', 'Rib', 'Pointelle', 'Waffle'], 0,
        'Tee weights. Rib is frequent here because a cropped body relies on the '
        'fabric for shape rather than on a hem.'),
    'Tops': (
        140, 220,
        ['Single Jersey', 'Interlock', 'Pointelle', 'Pique', 'Rib', 'Waffle'], 0,
        'A catch-all category, so the band spans tee through light knit top. '
        'Narrow this if the style is used for something more specific.'),
    'Polo': (
        180, 240,
        ['Pique', 'Interlock', 'Single Jersey', 'Waffle'], 0,
        'Traditional cotton pique polos run 200-240 gsm, which is what gives the '
        'collar its body; performance polos in poly or poly-spandex sit lighter '
        'at 160-200. Band opens at 180 to cover both.'),
    'Shirt': (
        130, 220,
        ['Woven', 'Single Jersey', 'Pique', 'Interlock'], 0,
        'Woven shirting is lighter than knit; the knit constructions cover '
        'jersey and pique shirts.'),
    'Hoody': (
        280, 400,
        ['Fleece', 'Terry'], 0,
        'Hoodies and crewnecks run 280-340 gsm standard, to 400 for heavyweight. '
        'French terry (smooth face, looped back) is the lighter option, brushed '
        'fleece the warmer one.'),
    'Full Zip Hoody': (
        280, 400,
        ['Fleece', 'Terry'], 0,
        'Same construction and weight as a pullover hoody; the zip does not '
        'change the fabric.'),
    'Sweat': (
        260, 340,
        ['Fleece', 'Terry'], 0,
        'Sweatshirt weight, marginally lighter at the bottom than a hoody '
        'because there is no hood to carry.'),
    'Cardigan': (
        260, 450,
        ['Fleece', 'Rib', 'Waffle', 'Jaquard', 'Ber Knit'], 0,
        'Knit outerwear runs 350-450 gsm; the band opens at 260 to cover lighter '
        'cardigans. Rib and jacquard are structural choices here, not trims.'),
    'Joggers': (
        260, 340,
        ['Fleece', 'Terry'], 0,
        'Streetwear joggers are 280-320 gsm fleece or French terry -- the same '
        'cloth as the matching hoody.'),
    'Legging': (
        200, 260,
        ['Single Jersey', 'Interlock'], 1,
        'Leggings are 200-250 gsm with 5-10% spandex. Stretch is required, not '
        'optional: without elastane the garment does not recover and bags at the '
        'knee.'),
    'Shorts': (
        180, 280,
        ['Single Jersey', 'Terry', 'Fleece', 'Pique', 'Interlock'], 0,
        'Spans jersey summer shorts through terry and fleece sweat shorts.'),
    'Pants': (
        240, 340,
        ['Fleece', 'Terry', 'Interlock'], 0,
        'Knit trousers need enough weight to hang rather than cling; below about '
        '240 gsm they read as pyjamas.'),
    'Trousers': (
        240, 340,
        ['Fleece', 'Terry', 'Interlock', 'Woven'], 0,
        'As Pants, plus woven for tailored styles.'),
    'Bottoms': (
        180, 340,
        ['Single Jersey', 'Fleece', 'Terry', 'Interlock'], 0,
        'A catch-all covering shorts through joggers, so the band is wide. '
        'Narrow it if the style is used for one kind of bottom.'),
    'Skirt': (
        180, 280,
        ['Interlock', 'Ottoman', 'Fleece', 'Rib'], 0,
        'Knit skirts need body and opacity, which is why interlock and ottoman '
        'suit them better than single jersey.'),
    'Dress': (
        160, 260,
        ['Single Jersey', 'Interlock', 'Ottoman', 'Pointelle', 'Rib'], 0,
        'Interlock is the usual choice for knit dresses: it is smooth on both '
        'faces, does not curl at the edges, and holds its shape.'),
    'Boxer': (
        120, 200,
        ['Single Jersey', 'Rib', 'Interlock'], 0,
        'Underwear runs 120-160 gsm for next-to-skin comfort; the band extends '
        'to 200 for heavier loungewear boxers.'),
    'Button Boxer': (
        120, 200,
        ['Single Jersey', 'Woven', 'Rib', 'Interlock'], 0,
        'As Boxer, plus woven -- a button fly is commonly cut in woven poplin.'),
    'Brief': (
        120, 180,
        ['Single Jersey', 'Rib', 'Interlock'], 0,
        'Briefs sit at the light end of underwear weights throughout.'),
}


def execute():
	_seed_constructions()
	_seed_rules()


def _seed_constructions():
	''' One row per distinct Fabric Master.fabric, measured from the catalogue. '''

	# v2_2 named its rows after family keys ("fleece", "jersey"). Those have to
	# go before the real constructions are written, not after: MySQL compares
	# docnames case-insensitively, so exists("Fleece") finds the old "fleece",
	# the seed tries to update it instead of inserting, and the update dies on
	# the now-mandatory `construction` field being empty.
	for stale in frappe.get_all(FAMILY_DOCTYPE,
	                            filters={'construction': ['in', ['', None]]},
	                            pluck='name'):
		frappe.delete_doc(FAMILY_DOCTYPE, stale,
		                  ignore_permissions=True, force=True)

	rows = frappe.db.sql(
		f'''
		select
			fabric,
			count(*)                          as catalogue_rows,
			count(distinct blend)             as distinct_blends,
			min(nullif(finish_gsm, 0))        as gsm_min,
			max(nullif(finish_gsm, 0))        as gsm_max
		from `tab{FABRIC_MASTER}`
		where ifnull(fabric, '') != ''
		group by fabric
		''',
		as_dict=True,
	)

	seen = set()
	for row in rows:
		construction = row['fabric'].strip()
		seen.add(construction)
		classified = sr._classify_construction(construction)
		family = classified.get('family')
		active = construction not in INACTIVE_CONSTRUCTIONS and bool(family)

		values = {
			'family_key': family or '',
			'is_active': 1 if active else 0,
			'is_trim': 1 if family == 'trim' else 0,
			'catalogue_rows': row['catalogue_rows'],
			'distinct_blends': row['distinct_blends'],
			'typical_gsm_min': row['gsm_min'] or 0,
			'typical_gsm_max': row['gsm_max'] or 0,
			'median_gsm': _median_gsm(construction),
			'notes': _construction_note(construction, family, active, row),
		}

		if frappe.db.exists(FAMILY_DOCTYPE, construction):
			doc = frappe.get_doc(FAMILY_DOCTYPE, construction)
			doc.update(values)
			doc.save(ignore_permissions=True)
		else:
			frappe.get_doc({'doctype': FAMILY_DOCTYPE,
			                'construction': construction,
			                **values}).insert(ignore_permissions=True)

	# v2_2 seeded family keys ("jersey", "rib") as docnames. They are not
	# constructions and nothing should link to them.
	for stale in frappe.get_all(FAMILY_DOCTYPE, pluck='name'):
		if stale not in seen:
			frappe.delete_doc(FAMILY_DOCTYPE, stale,
			                  ignore_permissions=True, force=True)


def _median_gsm(construction):
	''' The middle weight the catalogue knits a construction at. More use than
	the mean, which one 520 gsm rib drags off the truth. '''

	rows = frappe.db.sql(
		f'''select finish_gsm from `tab{FABRIC_MASTER}`
		    where fabric = %s and ifnull(finish_gsm, 0) > 0
		    order by finish_gsm''',
		(construction,),
	)
	if not rows:
		return 0
	return int(rows[len(rows) // 2][0])


def _construction_note(construction, family, active, row):
	if construction in INACTIVE_CONSTRUCTIONS:
		return (f'Not a construction -- "{construction}" names a yarn process or is a '
		        f'stray mill code that landed in the fabric column on '
		        f'{row["catalogue_rows"]} row(s). Marked inactive; never suggested.')
	if not family:
		return (f'{row["catalogue_rows"]} row(s), but the classifier does not place '
		        f'this construction in a knit family, so it cannot be compared '
		        f'against others. Add a keyword to surplus_recommender.FAMILY_KEYWORDS '
		        f'to activate it.')
	if family == 'trim':
		return (f'{row["catalogue_rows"]} row(s). A finding, not yardage -- never '
		        f'offered as a substitute for fabric.')
	return (f'{row["catalogue_rows"]} row(s) across {row["distinct_blends"]} blend(s), '
	        f'knitted {row["gsm_min"]}-{row["gsm_max"]} gsm. Family "{family}".')


def _seed_rules():
	''' A reviewed rule per trim-costing style, from published weight guidance. '''

	styles = frappe.db.sql_list(
		'''select distinct style_name from `tabTrim Costing`
		   where ifnull(style_name, '') != '' order by style_name'''
	)

	for style in styles:
		spec = RULES.get(style)
		name = f'{style}-Any'

		if spec is None:
			# A style added since this patch was written. Leave a draft rather
			# than a guess -- an unreviewed row changes nothing.
			if not frappe.db.exists(RULE_DOCTYPE, name):
				frappe.get_doc({
					'doctype': RULE_DOCTYPE,
					'garment_style': style,
					'section': 'Any',
					'is_reviewed': 0,
					'allow_family_change': 1,
					'notes': f'No seeded band for "{style}". Fill in Min/Max GSM and '
					         f'permitted constructions, then tick Reviewed.',
				}).insert(ignore_permissions=True)
			continue

		min_gsm, max_gsm, constructions, stretch, rationale = spec

		# A person's reviewed rule outranks this patch. An unreviewed row does
		# not -- v2_2's drafts have no `reviewed_by` at all, and testing on that
		# field alone would protect exactly the rows this patch exists to
		# replace.
		existing = frappe.db.get_value(RULE_DOCTYPE, name,
		                               ['name', 'is_reviewed', 'reviewed_by'],
		                               as_dict=True)
		if (existing and existing.get('is_reviewed')
				and (existing.get('reviewed_by') or '') != SEED_AUTHOR):
			continue

		# Only name constructions the catalogue actually holds, so a rule can
		# never narrow a garment down to something unbuyable.
		available = [c for c in constructions if frappe.db.exists(FAMILY_DOCTYPE, c)]
		missing = sorted(set(constructions) - set(available))

		note = rationale + ' ' + SOURCES
		if missing:
			note += (f' Not seeded because the catalogue has no such construction: '
			         f'{", ".join(missing)}.')

		doc = (frappe.get_doc(RULE_DOCTYPE, name) if existing
		       else frappe.get_doc({'doctype': RULE_DOCTYPE,
		                            'garment_style': style, 'section': 'Any'}))
		doc.update({
			'is_reviewed': 1,
			'reviewed_by': SEED_AUTHOR,
			'min_gsm': min_gsm,
			'max_gsm': max_gsm,
			'requires_stretch': stretch,
			'allow_family_change': 1,
			'notes': note,
		})
		doc.set('allowed_families', [{'family': c} for c in available])
		if existing:
			doc.save(ignore_permissions=True)
		else:
			doc.insert(ignore_permissions=True)
