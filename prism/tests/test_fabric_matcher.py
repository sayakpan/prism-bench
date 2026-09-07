'''
Tests for prism.api.fabric_matcher.

Two halves, deliberately:

  * the scorer and its gates, against synthetic combinations -- no database, no
    catalogue, no LLM, so a failure here is always a rule that changed
  * a handful of end-to-end matches against the real Fabric Master, because the
    thing worth protecting is that mill shorthand still finds retail English,
    and synthetic fixtures cannot fail that way

The LLM is never called: every match here passes use_llm=False, and the ranking
under test is the deterministic one the re-ranker is only allowed to reorder.
'''

import frappe
from frappe.tests.utils import FrappeTestCase

import prism.api.fabric_matcher as fm
import prism.api.surplus_recommender as sr


def _combo(construction, blend, family, composition, gsm_values=(), rows=1,
           tags=(), ratio=None, trim_kind=None):
    ''' A catalogue combination as _build_index() would have produced it. '''
    return {
        'construction': construction,
        'blend': blend,
        'family': family,
        'trim_kind': trim_kind,
        'tags': list(tags),
        'ratio': ratio,
        'composition': composition,
        'gsm_values': list(gsm_values),
        'rows': rows,
    }


class TestBlendParsing(FrappeTestCase):
    ''' Both vocabularies have to land in the same fibre-group space. '''

    def test_mill_shorthand_and_retail_english_agree(self):
        pairs = [
            ('95:5 BCI:EL', '95% Cotton 5% Spandex'),
            ('100 O', '100% Cotton'),
            ('58:39:3 FTO:T:EL', '58% Cotton 39% Lyocell 3% Spandex'),
            ('60:40 BCI:P', '60% Cotton 40% Polyester'),
        ]
        for stock, retail in pairs:
            with self.subTest(stock=stock):
                self.assertEqual(fm._parse_any_blend(stock), fm._parse_any_blend(retail))

    def test_repeated_fibre_entries_are_summed(self):
        # The catalogue writes two cotton yarns as two entries; the stock writes
        # the same cloth as BCI + RC. Both are 95/5 cotton-elastane.
        self.assertEqual(
            fm._parse_any_blend('90.25% Cotton 5% Spandex 4.75% Cotton'),
            {'cotton': 95.0, 'elastane': 5.0},
        )
        self.assertEqual(
            fm._parse_any_blend('90:5:5 BCI:RC:EL'),
            {'cotton': 95.0, 'elastane': 5.0},
        )

    def test_percent_sign_selects_the_retail_parser(self):
        # "100 P" is polyester in mill shorthand; "100% Polyester" is the same
        # cloth spelled retail. Neither may fall through to the other's parser.
        self.assertEqual(fm._parse_any_blend('100 P'), {'polyester': 100.0})
        self.assertEqual(fm._parse_any_blend('100% Polyester'), {'polyester': 100.0})

    def test_unreadable_blend_is_empty_not_an_error(self):
        for value in (None, '', '   ', 'n/a'):
            with self.subTest(value=value):
                self.assertEqual(fm._parse_any_blend(value), {})

    def test_full_name_ratio_blends_match_their_code_form(self):
        '''
        `blend_full_name` is mill ratios with retail fibre names. It used to be
        torn apart on spaces as well as colons, so "Bci Cotton" became two
        tokens, the percent-to-fibre pairing slid, and every fibre after the
        first was dropped -- silently, as a plausible 100% reading.
        '''
        pairs = [
            ('95:5 Bci Cotton:Elastane', '95:5 BCI:EL'),
            ('60:40 Bci Cotton:Polyester', '60:40 BCI:P'),
            ('71:29 Viscose:Polyester', '71:29 V:P'),
            ('58:39:3 Fair Trade Org Cotton:Tencel:Elastane', '58:39:3 FTO:T:EL'),
            ('90:5:5 Bci Cotton:Recycle_Cotton:Elastane', '90:5:5 BCI:RC:EL'),
            ('100 Organic Cotton', '100 O'),
            ('70:30 Viscose:Nylon', '70:30 V:N'),
            ('64:34:2 Polyester:Viscose:Elastane', '64:34:2 P:V:EL'),
        ]
        for full_name, code in pairs:
            with self.subTest(blend=full_name):
                self.assertEqual(fm._parse_any_blend(full_name), fm._parse_any_blend(code))

    def test_multi_fibre_blends_keep_every_fibre(self):
        # The specific regression: anything past the first fibre going missing.
        self.assertEqual(
            fm._parse_any_blend('57:38:5 Bci Cotton:Polyester:Elastane'),
            {'cotton': 57.0, 'polyester': 38.0, 'elastane': 5.0},
        )
        self.assertEqual(
            fm._parse_any_blend('42:46:12 Recycle Polyster:Viscose:Wool'),
            {'polyester': 42.0, 'viscose': 46.0, 'wool': 12.0},
        )

    def test_multi_word_fibre_names_resolve(self):
        for name, group in [('100 Vasudha Primo Cotton', 'cotton'),
                            ('100 Bci Cotton', 'cotton'),
                            ('100 Cotton Tc', 'cotton'),
                            ('100 Fair Trade Org Cotton', 'cotton')]:
            with self.subTest(name=name):
                self.assertEqual(fm._parse_any_blend(name), {group: 100.0})

    def test_mismatched_ratio_and_name_counts_are_refused(self):
        # Three percentages, two fibres: the pairing would be a guess, and a
        # guess is what produced the wrong readings in the first place.
        self.assertEqual(fm._parse_ratio_blend('58:39:3 Cotton:Polyester'), {})


class TestStockInputs(FrappeTestCase):
    ''' Exactly three fields feed a match, from exactly these columns. '''

    def test_full_names_are_preferred_over_codes(self):
        fabric = fm._fabric_from_stock_row({
            'quality': 'SJY_EL', 'quality_full_name': 'Single Jersey Elastane',
            'blend': '95:5 BCI:EL', 'blend_full_name': '95:5 Bci Cotton:Elastane',
            'gsm': 190, 'material_desc': '',
        })
        self.assertEqual(fabric['construction'], 'Single Jersey Elastane')
        self.assertEqual(fabric['blend'], '95:5 Bci Cotton:Elastane')
        self.assertEqual(fabric['gsm'], 190)

    def test_codes_are_used_when_full_names_are_blank(self):
        fabric = fm._fabric_from_stock_row({
            'quality': 'SJY_EL', 'quality_full_name': '',
            'blend': '95:5 BCI:EL', 'blend_full_name': '   ',
            'gsm': 190, 'material_desc': '',
        })
        self.assertEqual(fabric['construction'], 'SJY_EL')
        self.assertEqual(fabric['blend'], '95:5 BCI:EL')

    def test_nothing_but_the_three_fields_is_read(self):
        fabric = fm._fabric_from_stock_row({
            'quality': 'SJY', 'blend': '100 O', 'gsm': 180, 'material_desc': '',
            'color': 'NAVY', 'width': 60, 'dia': 34, 'fabric_type': 'SPD',
            'shade_catagory': 'DARK', 'customer_name': 'ACME',
        })
        self.assertEqual(set(fabric), {'construction', 'blend', 'gsm'})

    def test_zero_gsm_is_recovered_from_material_desc(self):
        fabric = fm._fabric_from_stock_row({
            'quality': 'FLBK_RIB', 'blend': '100 BCI', 'gsm': 0,
            'material_desc': 'SPD FLBK_RIB 100 BCI CC COMPACT 265 NAVY',
        })
        self.assertEqual(fabric['gsm'], 265)

    def test_rows_differing_only_in_recovered_gsm_get_different_signatures(self):
        base = {'quality': 'FLBK_RIB', 'blend': '100 BCI', 'gsm': 0}
        a = fm._signature({**base, 'material_desc': 'SPD FLBK_RIB 100 BCI CC COMPACT 265 NAVY'})
        b = fm._signature({**base, 'material_desc': 'SPD FLBK_RIB 100 BCI CC COMPACT 325 BLACK'})
        self.assertNotEqual(a, b)


class TestTrimGating(FrappeTestCase):
    ''' A trim and a fabric are never substitutes, nor are two unlike trims. '''

    JERSEY = _combo('Single Jersey', '100% Cotton', 'jersey', {'cotton': 100.0}, [180])
    COLLAR = _combo('Collar', '100% Cotton', 'trim', {'cotton': 100.0}, trim_kind='collar')
    DRAWCORD = _combo('Drawcord', '100% Cotton', 'trim', {'cotton': 100.0}, trim_kind='drawcord')
    DORI = _combo('Dori', '100% Cotton', 'trim', {'cotton': 100.0}, trim_kind='dori')

    def _target(self, construction, blend='100 BCI', gsm=0):
        return fm._normalise_target({
            'construction': construction, 'construction_code': construction,
            'blend': blend, 'gsm': gsm,
        })

    def test_trim_kinds_are_read_off_both_vocabularies(self):
        self.assertEqual(fm._trim_kind('DRW_CORD'), 'drawcord')
        self.assertEqual(fm._trim_kind('Drawcord'), 'drawcord')
        self.assertEqual(fm._trim_kind('CLR_EL'), 'collar')
        self.assertEqual(fm._trim_kind('Collar'), 'collar')
        self.assertEqual(fm._trim_kind('TTP_HBONE'), 'tape')
        self.assertEqual(fm._trim_kind('Twill Tape'), 'tape')
        self.assertEqual(fm._trim_kind('SJY_EL'), None)

    def test_a_trim_never_matches_yardage(self):
        self.assertIsNone(fm._score_combo(self._target('DRW_CORD'), self.JERSEY))

    def test_yardage_never_matches_a_trim(self):
        self.assertIsNone(fm._score_combo(self._target('SJY_EL'), self.COLLAR))

    def test_unlike_trims_are_rejected(self):
        # Identical blend, identical family -- only the kind separates them, and
        # it has to be enough.
        self.assertIsNone(fm._score_combo(self._target('DRW_CORD'), self.COLLAR))

    def test_a_trim_matches_its_own_kind(self):
        scored = fm._score_combo(self._target('DRW_CORD'), self.DRAWCORD)
        self.assertIsNotNone(scored)
        self.assertEqual(scored['breakdown']['construction'], 100.0)

    def test_dori_is_a_discounted_drawcord_not_a_rejection(self):
        scored = fm._score_combo(self._target('DRW_CORD'), self.DORI)
        self.assertIsNotNone(scored)
        self.assertLess(scored['breakdown']['construction'], 100.0)
        self.assertGreater(scored['breakdown']['construction'], 0.0)


class TestScoring(FrappeTestCase):

    def _target(self, construction, blend, gsm):
        return fm._normalise_target({
            'construction': construction, 'construction_code': construction,
            'blend': blend, 'gsm': gsm,
        })

    def test_exact_match_scores_full(self):
        target = self._target('SJY_EL', '95:5 BCI:EL', 190)
        combo = _combo('Single Jersey', '95% Cotton 5% Spandex', 'jersey',
                       {'cotton': 95.0, 'elastane': 5.0}, [190])
        scored = fm._score_combo(target, combo)
        self.assertEqual(scored['score'], 100.0)

    def test_gsm_is_measured_against_the_nearest_stocked_weight(self):
        # A combination knitted at 180 and 300 must not be judged on an average
        # nobody knits.
        target = self._target('SJY', '100 O', 300)
        combo = _combo('Single Jersey', '100% Cotton', 'jersey', {'cotton': 100.0}, [180, 300])
        scored = fm._score_combo(target, combo)
        self.assertEqual(scored['matched_gsm'], 300)
        self.assertEqual(scored['breakdown']['gsm'], 100.0)

    def test_missing_stock_gsm_does_not_disqualify(self):
        target = self._target('SJY', '100 O', 0)
        combo = _combo('Single Jersey', '100% Cotton', 'jersey', {'cotton': 100.0}, [180])
        scored = fm._score_combo(target, combo)
        self.assertEqual(scored['breakdown']['gsm'], fm.GSM_UNKNOWN_SCORE)
        self.assertGreater(scored['score'], 80.0)

    def test_missing_stretch_is_penalised(self):
        target = self._target('SJY_EL', '95:5 BCI:EL', 190)
        stretch = _combo('Single Jersey', '95% Cotton 5% Spandex', 'jersey',
                         {'cotton': 95.0, 'elastane': 5.0}, [190])
        no_stretch = _combo('Single Jersey', '100% Cotton', 'jersey', {'cotton': 100.0}, [190])
        self.assertEqual(fm._score_combo(target, stretch)['breakdown']['stretch_penalty'], 1.0)
        self.assertLess(fm._score_combo(target, no_stretch)['breakdown']['stretch_penalty'], 1.0)

    def test_a_substitute_family_scores_below_an_exact_one(self):
        target = self._target('RIB_1X1_EL', '95:5 BCI:EL', 220)
        rib = _combo('Rib', '95% Cotton 5% Spandex', 'rib',
                     {'cotton': 95.0, 'elastane': 5.0}, [220])
        waffle = _combo('Waffle', '95% Cotton 5% Spandex', 'waffle',
                        {'cotton': 95.0, 'elastane': 5.0}, [220])
        self.assertGreater(fm._score_combo(target, rib)['score'],
                           fm._score_combo(target, waffle)['score'])


class TestRanking(FrappeTestCase):

    def test_exact_ties_go_to_the_prevalent_spelling(self):
        # Both normalise to 95/5 cotton-elastane and score identically; the one
        # the catalogue is actually written in has to win.
        canonical = {'score': 100.0,
                     'combo': _combo('Rib', '95% Cotton 5% Spandex', 'rib',
                                     {'cotton': 95.0, 'elastane': 5.0}, rows=370)}
        oddity = {'score': 100.0,
                  'combo': _combo('Rib', '76% Cotton 19% Cotton 5% Spandex', 'rib',
                                  {'cotton': 95.0, 'elastane': 5.0}, rows=1)}
        ranked = sorted([oddity, canonical], key=fm._rank_key)
        self.assertEqual(ranked[0]['combo']['blend'], '95% Cotton 5% Spandex')

    def test_ranking_is_stable_across_runs(self):
        entries = [
            {'score': 92.0, 'combo': _combo('Rib', 'A', 'rib', {'cotton': 100.0}, rows=5)},
            {'score': 92.0, 'combo': _combo('Rib', 'B', 'rib', {'cotton': 100.0}, rows=5)},
            {'score': 92.0, 'combo': _combo('Rib', 'C', 'rib', {'cotton': 100.0}, rows=5)},
        ]
        first = [e['combo']['blend'] for e in sorted(entries, key=fm._rank_key)]
        second = [e['combo']['blend'] for e in sorted(reversed(entries), key=fm._rank_key)]
        self.assertEqual(first, second)


class TestRunawayDetection(FrappeTestCase):
    ''' When the scorer is certain, the LLM must not be consulted. '''

    def _entry(self, score, construction, composition, stretch=1.0):
        return {
            'score': score,
            'combo': _combo('Single Jersey', 'x', 'jersey', {}, rows=1),
            'breakdown': {'construction': construction, 'composition': composition,
                          'gsm': 100.0, 'stretch_penalty': stretch},
        }

    def test_exact_leader_is_a_runaway_despite_a_close_runner_up(self):
        # The case that made the whole margin test wrong: 95/5 and 96/4 sit a
        # rounding apart, and the leader is still plainly right.
        ranked = [self._entry(100.0, 100.0, 100.0), self._entry(99.98, 100.0, 99.9)]
        self.assertTrue(fm._is_runaway(ranked))

    def test_substitute_construction_is_not_a_runaway(self):
        ranked = [self._entry(84.0, 60.0, 100.0), self._entry(83.6, 60.0, 99.0)]
        self.assertFalse(fm._is_runaway(ranked))

    def test_partial_composition_is_not_a_runaway(self):
        ranked = [self._entry(90.0, 100.0, 95.0), self._entry(89.6, 100.0, 94.0)]
        self.assertFalse(fm._is_runaway(ranked))

    def test_stretch_mismatch_is_not_a_runaway(self):
        ranked = [self._entry(95.0, 100.0, 100.0, stretch=0.85),
                  self._entry(94.0, 100.0, 99.5)]
        self.assertFalse(fm._is_runaway(ranked))


class TestFabricMasterPricing(FrappeTestCase):
    ''' The per-kg cost carried into recommend()/catalogue(). '''

    def test_costing_a_missing_fabric_yields_none_not_an_error(self):
        # A fabric that will not cost must never cost a row its match.
        self.assertIsNone(fm._cost_per_kg(None))
        self.assertIsNone(fm._cost_per_kg('no-such-fabric-master'))

    def test_cost_is_memoised_per_fabric(self):
        # 580 rows point at 69 fabrics; without the memo a bulk run costs each
        # fabric dozens of times, and each cold costing makes LLM calls.
        cache = {'already-costed': 512.5}
        self.assertEqual(fm._cost_per_kg('already-costed', cache), 512.5)

        cache = {}
        fm._cost_per_kg('no-such-fabric-master', cache)
        self.assertIn('no-such-fabric-master', cache)  # negatives cached too

    def test_group_takes_the_quantity_dominant_master(self):
        lots = [
            {'closest_fabric_master': 'A', 'total_qty': 796.08, 'closest_fabric_cost_per_kg': 171.66},
            {'closest_fabric_master': 'B', 'total_qty': 193.63, 'closest_fabric_cost_per_kg': 650.46},
        ]
        master, cost = sr._closest_fabric_master(lots)
        self.assertEqual(master, 'A')
        self.assertEqual(cost, 171.66)

    def test_cost_comes_from_a_lot_pointing_at_that_master(self):
        # The dominant master has no cost stored; the OTHER master's cost must
        # not be borrowed, or the group reports one fabric beside another's price.
        lots = [
            {'closest_fabric_master': 'A', 'total_qty': 900, 'closest_fabric_cost_per_kg': 0},
            {'closest_fabric_master': 'B', 'total_qty': 100, 'closest_fabric_cost_per_kg': 650.46},
        ]
        master, cost = sr._closest_fabric_master(lots)
        self.assertEqual(master, 'A')
        self.assertIsNone(cost)

    def test_unmatched_group_prices_as_none(self):
        lots = [{'closest_fabric_master': None, 'total_qty': 100,
                 'closest_fabric_cost_per_kg': 0}]
        self.assertEqual(sr._closest_fabric_master(lots), (None, None))

    def test_the_key_is_present_on_every_group(self):
        groups = sr._fab_code_groups()
        if not groups:
            self.skipTest('No Surplus Stock on this site.')
        for group in groups:
            self.assertIn('price_from_fabric_masters', group)
            self.assertIn('closest_fabric_master', group)


class TestAgainstRealCatalogue(FrappeTestCase):
    '''
    End-to-end against the site's Fabric Master. Skipped on an empty catalogue so
    a fresh site does not fail the suite; where it runs, it is the only thing
    proving the two vocabularies actually meet.
    '''

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.has_catalogue = bool(frappe.db.count(fm.DOCTYPE_FABRIC_MASTER))

    def setUp(self):
        if not self.has_catalogue:
            self.skipTest('No Fabric Master records on this site.')

    def _best(self, construction, blend, gsm):
        matches = fm.match_fabric(
            {'construction': construction, 'construction_code': construction,
             'blend': blend, 'gsm': gsm},
            use_llm=False,
        )
        self.assertTrue(matches, f'No match for {construction} / {blend}')
        return matches[0]

    def test_single_jersey_with_elastane(self):
        best = self._best('SJY_EL', '95:5 BCI:EL', 190)
        self.assertEqual(best['construction'], 'Single Jersey')
        self.assertEqual(fm._parse_any_blend(best['blend']),
                         {'cotton': 95.0, 'elastane': 5.0})

    def test_rib_is_not_flattened_into_jersey(self):
        best = self._best('RIB_1X1_EL', '95:5 O:EL', 220)
        self.assertEqual(best['construction'], 'Rib')

    def test_tencel_shorthand_finds_lyocell(self):
        # The blend-understanding case: `T` on the stock side, "Lyocell" on the
        # catalogue side, same fibre.
        best = self._best('FLBK_RIB', '58:39:3 FTO:T:EL', 240)
        self.assertIn('tencel', fm._parse_any_blend(best['blend']))

    def test_trims_stay_trims(self):
        for quality, expected in [('DRW_CORD', 'Drawcord'), ('CLR_EL', 'Collar'),
                                  ('TTP_HBONE', 'Twill Tape')]:
            with self.subTest(quality=quality):
                self.assertEqual(self._best(quality, '100 BCI', 0)['construction'], expected)

    def test_recycled_cotton_counts_as_cotton(self):
        best = self._best('SJY_EL', '90:5:5 BCI:RC:EL', 190)
        self.assertEqual(fm._parse_any_blend(best['blend']),
                         {'cotton': 95.0, 'elastane': 5.0})

    def test_every_match_carries_its_reasoning(self):
        best = self._best('SJY_EL', '95:5 BCI:EL', 190)
        for key in ('score', 'method', 'reason', 'breakdown', 'fabric_id', 'fabric_code'):
            self.assertIn(key, best)
        self.assertTrue(best['reason'])
