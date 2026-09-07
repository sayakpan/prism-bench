'''
Tests for catalogue()'s `q` query language.

Two halves, deliberately:

  * the parser, against typed strings -- no database, because what is under test
    is a pure reading of text and a failure here is always a rule that changed
  * the group filter, against synthetic groups shaped exactly as _build_group
    returns them, so a facet that stops lining up with the field it filters
    fails here rather than in front of a merchandiser

Nothing in this file touches Surplus Stock or the LLM. `q` is deterministic on
purpose -- a filter box has to answer instantly and identically every time --
so there is nothing here that needs either.
'''

import frappe
from frappe.tests.utils import FrappeTestCase

import prism.api.surplus_recommender as sr


def _applied(query_text):
    ''' The display labels `q` would put above the results. '''
    return sr._query_summary(sr._parse_query(query_text))['applied']


def _group(**overrides):
    ''' A fab-code group as _build_group() would have produced it: 95/5 cotton
        elastane single jersey, 180 GSM, 58" and 60", 800 KG at 420/KG. '''
    group = {
        'fab_code': 'SJY001',
        'gsm': 180,
        'widths': [58, 60],
        'dia': '34"',
        'gauge': '24 GG',
        'available': 800.0,
        'price_per_uom': 420.0,
        'price_from_fabric_masters': 380.0,
        'max_ageing': 90,
        'construction_family': 'jersey',
        'quality_label': 'Single Jersey',
        'composition_pct': {'cotton': 95.0, 'elastane': 5.0},
        'texture_tags': ['slub'],
        'colors': [],
        'batch': 'B1',
        'hero_color': 'ECRU',
        'hero_shade_category': 'LIGHT',
        'image_url': 'ecru.jpg',
        'thumbnail': 'ecru_thumb.jpg',
    }
    group.update(overrides)
    return group


def _variant(color, batch, shade='DARK'):
    return {
        'color': color, 'shade_category': shade, 'batch': batch,
        'available': 100.0, 'lot_count': 1, 'has_image': True,
        'image': f'{batch}.jpg', 'thumbnail': f'{batch}_thumb.jpg',
        'image_url': f'{batch}_thumb.jpg',
    }


class TestQueryParsing(FrappeTestCase):
    ''' What the caller typed -> the facets it asked for. '''

    def test_the_documented_examples(self):
        # The five shapes the search box advertises. If these drift, the help
        # text is lying to whoever reads it.
        self.assertEqual(_applied('Single Jersey'), ['Single Jersey'])
        self.assertEqual(_applied('Single Jersey 180 GSM'),
                         ['Single Jersey', '180 GSM'])
        self.assertEqual(_applied('Cotton Single Jersey Navy Surplus'),
                         ['Single Jersey', 'Cotton', '"navy"'])
        self.assertEqual(_applied('Single Jersey 60 inch'),
                         ['Single Jersey', '60 inch'])

    def test_construction_is_read_before_the_word_inside_it(self):
        # "single jersey" is one construction, not the word "jersey" with noise
        # in front of it -- and the mill's own shorthand lands on the same family.
        for text in ('single jersey', 'sjy', 'SJY_EL'):
            with self.subTest(text=text):
                self.assertEqual(sr._parse_query(text)['families'], ['jersey'])
        self.assertEqual(sr._parse_query('double jersey')['families'], ['interlock'])
        self.assertEqual(sr._parse_query('french terry')['families'], ['terry'])

    def test_numbers_are_read_either_way_round(self):
        for text in ('180 gsm', '180gsm', 'gsm 180'):
            with self.subTest(text=text):
                self.assertEqual(sr._parse_query(text)['ranges']['gsm'],
                                 {'min': 180, 'max': 180})
        for text in ('60 inch', 'width 60', '60"'):
            with self.subTest(text=text):
                self.assertEqual(sr._parse_query(text)['ranges']['width'],
                                 {'min': 60, 'max': 60})

    def test_a_bare_number_is_exact(self):
        # No metric widens what was typed. A filter that quietly turns 150 into
        # 135-165 leaves the caller unable to say what they actually meant, and
        # unable to tell a near miss from a hit in the results.
        for text, key, value in (('150 gsm', 'gsm', 150), ('60 inch', 'width', 60),
                                 ('dia 34', 'dia', 34), ('24 gauge', 'gauge', 24),
                                 ('price 500', 'price', 500)):
            with self.subTest(text=text):
                self.assertEqual(sr._parse_query(text)['ranges'][key],
                                 {'min': value, 'max': value})

    def test_approximate_weight_is_asked_for_as_a_range(self):
        # What the band used to do, said out loud instead.
        self.assertEqual(sr._parse_query('140-160 gsm')['ranges']['gsm'],
                         {'min': 140, 'max': 160})
        self.assertEqual(_applied('single jersey 140-160 gsm'),
                         ['Single Jersey', '140-160 GSM'])

    def test_comparators_in_symbols_and_in_words(self):
        cases = {
            'gsm > 160': {'min': 160, 'max': None},
            'gsm >= 160': {'min': 160, 'max': None},
            'gsm over 160': {'min': 160, 'max': None},
            'gsm at least 160': {'min': 160, 'max': None},
            'gsm < 200': {'min': None, 'max': 200},
            'gsm under 200': {'min': None, 'max': 200},
            'gsm 160-200': {'min': 160, 'max': 200},
            'gsm between 160 and 200': {'min': 160, 'max': 200},
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(sr._parse_query(text)['ranges']['gsm'], expected)

    def test_two_bounds_on_one_metric_intersect(self):
        # Written as two conditions, meant as one range.
        self.assertEqual(sr._parse_query('gsm > 160 gsm < 200')['ranges']['gsm'],
                         {'min': 160, 'max': 200})

    def test_price_is_the_per_kg_rate_and_make_cost_is_its_own_facet(self):
        # 'make cost' has to be claimed before 'cost', or the price filter eats
        # it and the query silently asks the wrong question.
        self.assertEqual(sr._parse_query('price under 500')['ranges'],
                         {'price': {'min': None, 'max': 500}})
        self.assertEqual(sr._parse_query('make cost < 400')['ranges'],
                         {'make_cost': {'min': None, 'max': 400}})

    def test_fibres_with_and_without_a_share(self):
        self.assertEqual(sr._parse_query('cotton')['fibres'], {'cotton': 0})
        self.assertEqual(sr._parse_query('95% cotton 5% spandex')['fibres'],
                         {'cotton': 95.0, 'elastane': 5.0})
        # Retail names and mill-adjacent ones reach the same fibre group.
        self.assertEqual(sr._parse_query('lycra')['fibres'],
                         sr._parse_query('spandex')['fibres'])

    def test_a_percentage_needs_its_sign(self):
        # Without this, a GSM that got away would be read as a fibre share.
        self.assertEqual(sr._parse_query('180 cotton')['fibres'], {'cotton': 0})

    def test_textures_are_separate_from_construction(self):
        parsed = sr._parse_query('slub single jersey')
        self.assertEqual(parsed['families'], ['jersey'])
        self.assertEqual(parsed['textures'], ['slub'])

    def test_unknown_words_survive_as_text_conditions(self):
        # A colour, a fab code, a rib ratio -- none of them facets, all of them
        # things the widened column list can still find.
        self.assertEqual(sr._parse_query('navy')['terms'], ['navy'])
        self.assertEqual(sr._parse_query('rib 2x2')['terms'], ['2x2'])
        self.assertEqual(sr._parse_query('A1234')['terms'], ['a1234'])

    def test_stranded_units_and_comparators_are_not_searched_for(self):
        # Each of these would return nothing as a text match, taking the whole
        # query down with it -- they are ANDed.
        for text in ('60 inch single jersey', 'rib at least 200 gsm up to 5% elastane',
                     'surplus fabric jersey', '500 kg rib'):
            with self.subTest(text=text):
                self.assertEqual(sr._parse_query(text)['terms'], [])

    def test_unsupported_facets_are_named_not_searched(self):
        parsed = sr._parse_query('Single Jersey FOB < 3')
        self.assertEqual(parsed['families'], ['jersey'])
        self.assertEqual([u['term'] for u in parsed['unsupported']], ['fob'])
        # The comparison goes with it: a stray "3" would match every fab code
        # containing a 3, and a stray "fob" would match none of them.
        self.assertEqual(parsed['terms'], [])
        self.assertEqual(parsed['ranges'], {})

    def test_a_dollar_amount_is_unsupported_rather_than_reinterpreted(self):
        # Stock is valued in INR per KG. Reading "$3" as a rupee rate would put
        # a per-piece dollar figure behind a per-KG rupee filter.
        parsed = sr._parse_query('single jersey under $3')
        self.assertEqual([u['term'] for u in parsed['unsupported']], ['usd'])
        self.assertEqual(parsed['ranges'], {})

    def test_an_empty_query_parses_to_nothing_at_all(self):
        for text in (None, '', '   '):
            with self.subTest(text=text):
                self.assertIsNone(sr._parse_query(text))


class TestQueryFilter(FrappeTestCase):
    ''' The parsed facets against the groups they filter. '''

    def _matches(self, text, group):
        return sr._group_matches(group, sr._parse_query(text))

    def test_construction_and_weight_together(self):
        self.assertTrue(self._matches('single jersey 180 gsm', _group()))
        self.assertFalse(self._matches('single jersey 260 gsm', _group()))
        self.assertFalse(self._matches('rib 180 gsm', _group()))

    def test_a_near_weight_is_not_a_match(self):
        # The 180 GSM group does not answer a request for 150 or 190.
        self.assertFalse(self._matches('150 gsm', _group()))
        self.assertFalse(self._matches('190 gsm', _group()))
        self.assertTrue(self._matches('170-190 gsm', _group()))

    def test_a_card_is_findable_by_the_label_printed_on_it(self):
        # QUALITY_LABELS folds ottoman and purl into "Single Jersey" for display.
        # Matching only construction_family would hide a card from its own words.
        ottoman = _group(construction_family='ottoman', quality_label='Single Jersey')
        self.assertTrue(self._matches('single jersey', ottoman))
        self.assertTrue(self._matches('ottoman', ottoman))

    def test_width_passes_on_any_width_the_group_holds(self):
        self.assertTrue(self._matches('60 inch', _group()))
        self.assertTrue(self._matches('58 inch', _group()))
        self.assertFalse(self._matches('62 inch', _group()))

    def test_dia_and_gauge_are_read_out_of_the_text_they_are_stored_as(self):
        self.assertTrue(self._matches('dia 34', _group()))
        self.assertTrue(self._matches('24 gauge', _group()))
        self.assertFalse(self._matches('dia 30', _group()))

    def test_composition_is_matched_in_fibre_groups(self):
        self.assertTrue(self._matches('cotton', _group()))
        self.assertTrue(self._matches('95% cotton', _group()))
        self.assertFalse(self._matches('polyester', _group()))
        self.assertFalse(self._matches('60% cotton', _group()))

    def test_a_fibre_share_is_a_band(self):
        # 95:5 written by a mill and "95% cotton" typed by a merchandiser are the
        # same cloth; so is a 94/6.
        self.assertTrue(self._matches('95% cotton', _group(
            composition_pct={'cotton': 94.0, 'elastane': 6.0})))

    def test_every_facet_in_the_query_has_to_hold(self):
        self.assertTrue(self._matches('slub cotton single jersey 180 gsm 60 inch', _group()))
        self.assertFalse(self._matches('melange cotton single jersey 180 gsm', _group()))

    def test_an_unknown_value_never_satisfies_a_filter_on_it(self):
        # An undecodable GSM is not 0 GSM and an unpriced group is not free --
        # the same call the sort makes when it pushes missing values last.
        self.assertFalse(self._matches('180 gsm', _group(gsm=0)))
        self.assertFalse(self._matches('price under 500', _group(price_per_uom=None)))
        self.assertFalse(self._matches('60 inch', _group(widths=[])))
        self.assertFalse(self._matches('dia 34', _group(dia=None)))

    def test_leftover_words_do_not_filter_here(self):
        # They were already resolved against the stock columns, per fab code.
        # Re-applying them to the group would drop codes whose match sits on a
        # lot the group summarises away.
        self.assertTrue(self._matches('single jersey navy', _group()))


class TestMatchedColourLeadsTheCard(FrappeTestCase):
    '''
    A fab code is a group of colourways and its default hero is just the first
    photographed lot. Searching a colour and being shown a different one is
    worse than not filtering at all.
    '''

    def test_a_colour_word_promotes_that_colourway(self):
        group = _group(colors=[_variant('ECRU', 'B1', 'LIGHT'), _variant('NAVY BLUE', 'B7')])
        kept = sr._filter_groups([group], sr._parse_query('single jersey navy'))

        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0]['batch'], 'B7')
        self.assertEqual(kept[0]['hero_color'], 'NAVY BLUE')
        self.assertEqual(kept[0]['image_url'], 'B7.jpg')
        self.assertEqual(kept[0]['thumbnail'], 'B7_thumb.jpg')
        self.assertEqual(kept[0]['matched_color'], 'NAVY BLUE')

    def test_a_word_that_is_not_a_colour_leaves_the_hero_alone(self):
        group = _group(colors=[_variant('ECRU', 'B1', 'LIGHT'), _variant('NAVY BLUE', 'B7')])
        kept = sr._filter_groups([group], sr._parse_query('single jersey 2x2'))

        self.assertEqual(kept[0]['hero_color'], 'ECRU')
        self.assertIsNone(kept[0]['matched_color'])

    def test_matched_color_is_always_present_on_a_queried_group(self):
        # Present and null, not absent: a client should not have to tell "no
        # colour matched" apart from "this response predates the field".
        kept = sr._filter_groups([_group()], sr._parse_query('single jersey'))
        self.assertIn('matched_color', kept[0])


class TestSearchIsUnchanged(FrappeTestCase):
    '''
    `q` was added beside `search`, not on top of it. `search` stays a literal
    substring over four columns, because that is the contract its callers hold:
    read as a query, a batch number like "SJ-180" would start filtering on GSM.
    '''

    def test_the_two_parameters_read_different_columns(self):
        self.assertEqual(sr.CATALOGUE_SEARCH_FIELDS,
                         ['fab_code', 'batch', 'quality', 'blend'])
        for field in ('quality_full_name', 'blend_full_name', 'color', 'material_desc'):
            with self.subTest(field=field):
                self.assertNotIn(field, sr.CATALOGUE_SEARCH_FIELDS)
                self.assertIn(field, sr.QUERY_SEARCH_FIELDS)

    def test_search_still_resolves_over_its_own_four_columns(self):
        seen = {}

        def _capture(doctype, filters=None, or_filters=None, **kwargs):
            seen.setdefault('or_filters', or_filters)
            return []

        original = frappe.get_all
        frappe.get_all = _capture
        try:
            sr._fab_code_groups(search='SJ-180')
        finally:
            frappe.get_all = original

        self.assertEqual([condition[0] for condition in seen['or_filters']],
                         sr.CATALOGUE_SEARCH_FIELDS)
        # Literal, not parsed: the "180" is part of the string being looked for.
        self.assertEqual(seen['or_filters'][0][2], '%SJ-180%')

    def test_an_empty_query_leaves_the_catalogue_untouched(self):
        groups = [_group(), _group(fab_code='RIB002', construction_family='rib')]
        self.assertIsNone(sr._parse_query(None))
        self.assertEqual(len(sr._filter_groups(groups, sr._parse_query('surplus fabric'))), 2)
