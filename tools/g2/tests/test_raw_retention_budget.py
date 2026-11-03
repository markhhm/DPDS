# SPDX-License-Identifier: Apache-2.0
"""Retention is budgeted once per acquisition, including both capture points."""
import copy
from fractions import Fraction
import json
from pathlib import Path
import unittest

try:
    from tools.g2 import raw_budget
except ImportError:
    raw_budget = None


class RawRetentionBudgetTests(unittest.TestCase):
    def module(self):
        self.assertIsNotNone(raw_budget, 'raw-prefix retention budget is missing')
        return raw_budget

    def test_complete_matrix_reserves_ten_percent_once_per_acquisition(self):
        module=self.module()
        from tools.g2.expand_m6_x710 import build_matrix
        matrix=build_matrix();module.register_retention(matrix)
        policy=matrix['plan']['raw_retention']
        self.assertEqual(policy['limit_bytes'],2_000_000_000_000)
        self.assertEqual(policy['planned_bytes'],1_800_000_000_000)
        self.assertEqual(policy['acquisitions'],1380)
        self.assertEqual(policy['sequence_policy'],'per-flow-dense')
        acquisitions={}
        for run in matrix['runs']:
            meta=run['extensions']['m6'];entry=meta['raw_retention'];aid=meta['acquisition_id']
            if aid in acquisitions:self.assertEqual(entry,acquisitions[aid])
            acquisitions[aid]=entry
            self.assertGreater(entry['keep_ns'],0)
            self.assertLessEqual(entry['estimated_bytes'],entry['byte_budget'])
            self.assertLessEqual(entry['byte_budget'],1_800_000_000_000//1380)
            self.assertEqual(entry['capture_directory'],'${CAPTURE_ROOT}/'+aid)
        self.assertEqual(len(acquisitions),1380)
        self.assertLessEqual(sum(e['estimated_bytes'] for e in acquisitions.values()),1_800_000_000_000)
        self.assertEqual(matrix,json.loads((Path('examples')/'m6-x710.json').read_text()))

    def test_rates_include_native_and_both_external_capture_points(self):
        module=self.module()
        condition=dict(traffic='U',direction_mode='forward',lines=[dict(offered_bps=9_000_000_000)])
        traffic=dict(wire_bytes=[1542],q_bytes=[1522],flows=1)
        rate=module.rates(condition,traffic,native_bytes_per_offer=316)
        self.assertEqual(rate['native'], -(-Fraction(9_000_000_000*316,1542*8).numerator//Fraction(9_000_000_000*316,1542*8).denominator))
        self.assertEqual(rate['before'], -(-Fraction(9_000_000_000*160,1542*8).numerator//Fraction(9_000_000_000*160,1542*8).denominator))
        self.assertEqual(rate['receiver'],rate['before'])
        self.assertEqual(rate['total'],sum(rate[k] for k in ('native','before','receiver')))
        both=copy.deepcopy(condition);both['direction_mode']='both'
        doubled=module.rates(both,traffic,native_bytes_per_offer=316)
        self.assertTrue(all(doubled[k] in (2*rate[k],2*rate[k]-1) for k in rate))

    def test_mixed_snap_lengths_use_equal_packet_counts_not_maximum_wire(self):
        module=self.module()
        condition=dict(traffic='X',direction_mode='both',lines=[dict(offered_bps=5_000_000_000),dict(offered_bps=5_000_000_000)])
        rate=module.rates(condition,dict(wire_bytes=[84,532,1542],q_bytes=[64,512,1522],flows=64),native_bytes_per_offer=316)
        # 2 * 10G/(8*(2158/3)) offers/s; each point has mean EPB (92+160+160)/3 B.
        expected=Fraction(2*10_000_000_000*412,8*2158)
        self.assertEqual(rate['before'],-(-expected.numerator//expected.denominator))
        self.assertEqual(rate['receiver'],rate['before'])

    def test_duplicate_acquisition_members_with_different_conditions_are_rejected(self):
        module=self.module()
        from tools.g2.expand_m6_x710 import build_matrix
        matrix=build_matrix()
        matrix['runs'][3]['extensions']['m6']['acquisition_id']=matrix['runs'][0]['extensions']['m6']['acquisition_id']
        with self.assertRaises(ValueError):module.register_retention(matrix)

    def test_invalid_budget_and_density_are_rejected(self):
        module=self.module()
        from tools.g2.expand_m6_x710 import build_matrix
        for kwargs in (dict(limit_bytes=0),dict(headroom_percent=100),dict(headroom_percent=-1),dict(native_bytes_per_offer=0),dict(limit_bytes=True)):
            with self.subTest(kwargs=kwargs),self.assertRaises(ValueError):module.register_retention(build_matrix(),**kwargs)


if __name__=='__main__':unittest.main()
