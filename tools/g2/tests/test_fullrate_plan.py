# SPDX-License-Identifier: Apache-2.0
"""Full-rate supplement stays distinct from the quarter-rate inventory."""
import copy
import json
from pathlib import Path
import unittest

ROOT=Path(__file__).resolve().parents[3]


class FullratePlanTests(unittest.TestCase):
    def test_small_selection_preserves_models_and_covers_both_trace_corridors(self):
        from tools.g2.fullrate_plan import build_plan
        source=json.loads((ROOT/'examples/m6-x710.json').read_text())
        plan=build_plan(source)
        self.assertEqual(len(plan['conditions']),8)
        source_rows={row['id']:row for row in source['plan']['conditions']}
        for row in plan['conditions']:
            if row['input_kind']!='outage':
                original=source_rows[row['id']]['lines'][0]
                for field in ('t_ns','delta_ns','p','seed','trace_windows'):
                    self.assertEqual(row['lines'][0].get(field),original.get(field))
            self.assertTrue(all(line['q_bytes']==8_388_608 for line in row['lines']))
            self.assertEqual(row['traffic'],'U')
            self.assertEqual(sum(line['offered_bps'] for line in row['lines']),10_000_000_000)
            self.assertLessEqual(sum(line['c_bps'] for line in row['lines']),9_000_000_000)
        self.assertEqual({row['input_kind'] for row in plan['conditions']},{'fixed','trace','outage'})
        self.assertTrue(any(row['direction_mode']=='both' for row in plan['conditions']))
        self.assertEqual(set(plan['trace_windows']),{'ul-A','ul-B','dl-B'})
        self.assertGreaterEqual(plan['budget']['on_off_seconds'],3600)
        self.assertLessEqual(plan['budget']['on_off_seconds'],7200)
        self.assertEqual(plan['coverage']['outage'],'registered synthetic hold and drop companions; outside the 360 conditions')
        self.assertFalse(plan['coverage']['os4_independent_owd'])
        self.assertLessEqual(plan['budget']['on_off_with_reserve_seconds'],7200)
        self.assertEqual({c['outage_action'] for c in plan['conditions'] if c['input_kind']=='outage'},{'hold','drop'})
        self.assertEqual(len({row['id'] for row in plan['acquisitions']}),len(plan['acquisitions']))
        for row in plan['acquisitions']:
            self.assertEqual(len(row['mirrored_egress_ports']),1)
        self.assertEqual(plan['budget']['observer_off_extra_seconds'],plan['budget']['observer_on_seconds'])

    def test_quarter_rate_input_restores_full_rate_c_and_q_without_changing_time(self):
        from tools.g2.fullrate_plan import build_plan
        source=json.loads((ROOT/'examples/m6-x710.json').read_text());quarter=copy.deepcopy(source)
        for condition in quarter['plan']['conditions']:
            for line in condition['lines']:
                if sum(item['offered_bps'] for item in condition['lines'])>=9_000_000_000:
                    for field in ('c_bps','offered_bps','q_bytes'):line[field]//=4
        self.assertEqual(build_plan(source),build_plan(quarter))

    def test_committed_plan_is_reproducible_and_unknown_rate_is_rejected(self):
        from tools.g2.fullrate_plan import build_plan
        source=json.loads((ROOT/'examples/m6-x710.json').read_text())
        self.assertEqual(build_plan(source),json.loads((ROOT/'examples/m6-fullrate-observer.json').read_text()))
        for condition in source['plan']['conditions']:
            if condition['id']=='fixed-M-CA-65-0-0-both-L9-U':condition['lines'][0]['offered_bps']=1
        with self.assertRaises(ValueError):build_plan(source)


if __name__=='__main__':unittest.main()
