# SPDX-License-Identifier: Apache-2.0
"""Complete approved inventory, directional paths and acquisition-time accounting."""
from collections import Counter
import json
from pathlib import Path
import tempfile
import unittest

EXAMPLES=Path(__file__).resolve().parents[3]/'examples'


def bind(value):
    values={'DUT_HOST':'dut','GENERATOR_HOST':'generator','CAPTURE_HOST':'capture',
            'GENERATOR_PORT0_IF':'gen0','GENERATOR_PORT1_IF':'gen1',
            'CAPTURE_PORT0_IF':'cap0','CAPTURE_PORT1_IF':'cap1',
            'CAPTURE_CLOCK_ID':'capture','CAPTURE_MAPPING_ID':'ns',
            'CAPTURE_CLOCK_SOURCE':'synthetic-capture','CAPTURE_ROOT':'captures',
            'TRACE_PROFILE_ROOT':'trace-profiles'}
    if isinstance(value,dict):return {k:bind(v) for k,v in value.items()}
    if isinstance(value,list):return [bind(v) for v in value]
    if isinstance(value,str):
        for key,replacement in values.items():value=value.replace('${'+key+'}',replacement)
    return value


class FullX710Tests(unittest.TestCase):
    def test_example_has_all_conditions_repeats_episodes_and_directional_paths(self):
        matrix=json.loads((EXAMPLES/'m6-x710.json').read_text());plan=matrix['plan']
        conditions=plan['conditions'];self.assertEqual(len(conditions),360)
        self.assertEqual(Counter(c['family'] for c in conditions),dict(main=72,boundary=288))
        self.assertEqual(Counter(c['input_kind'] for c in conditions),dict(fixed=300,trace=60))
        self.assertEqual(Counter(c['subfamily'] for c in conditions),
            {'M-CA':18,'M-CB':18,'M-LA':18,'M-LB':18,'E0':24,'E1':96,'E2':24,'E3':24,'E-mid':60,'E-CB':36,'E-pair':24})
        self.assertEqual(plan['deployment']['vlans'],dict(cable=710,leo=711))
        self.assertEqual(len(matrix['runs']),1960)
        self.assertEqual(len({r['id'] for r in matrix['runs']}),1960)
        self.assertEqual(len({r['manifest'] for r in matrix['runs']}),1960)
        self.assertEqual(Counter(r['family'] for r in matrix['runs']),dict(main=144,boundary=352,trace=1464))
        by_condition={c['id']:c for c in conditions};groups={}
        for run in matrix['runs']:
            m=run['extensions']['m6'];c=by_condition[m['condition_id']]
            self.assertEqual(run['manifest'],'${CAPTURE_ROOT}/'+m['acquisition_id']+'/d'+str(m['ingress_port'])+'/pair.json')
            groups.setdefault(m['acquisition_id'],[]).append(run)
            for line in c['lines']:
                self.assertEqual(line['vlan_id'],710 if line['family']=='cable' else 711)
        self.assertEqual(len(groups),1380)
        self.assertTrue(all(len(runs)==(2 if by_condition[runs[0]['extensions']['m6']['condition_id']]['direction_mode']=='both' else 1)
                            for runs in groups.values()))

    def test_shared_reader_preflights_every_future_pair_after_binding(self):
        from tools.g2.matrix import read_matrix
        matrix=bind(json.loads((EXAMPLES/'m6-x710.json').read_text()))
        self.assertNotIn('${',json.dumps(matrix))
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);path=root/'matrix.json';path.write_text(json.dumps(matrix))
            checked=read_matrix(path)
            self.assertEqual(len(checked['runs']),1960)
            self.assertEqual(list(root.iterdir()),[path])

    def test_generator_is_deterministic_and_preserves_trace_registration(self):
        from tools.g2.expand_m6_x710 import build_matrix
        matrix=build_matrix()
        self.assertEqual(matrix,json.loads((EXAMPLES/'m6-x710.json').read_text()))
        trace=json.loads((EXAMPLES/'m6-trace-conditions.json').read_text())
        conditions={c['id']:c for c in matrix['plan']['conditions'] if c['input_kind']=='trace'}
        for original in trace['plan']['trace_conditions']:
            c=conditions[original['id']]
            self.assertEqual(c['repeats'],original['repeats'])
            self.assertEqual(c['episodes_per_repeat'],original['episodes_per_repeat'])
            for line in c['lines']:
                self.assertEqual(line['c_bps'],original['c_bps_per_line'])
                self.assertEqual(line['offered_bps'],original['offered_bps_per_line'])
                self.assertEqual(line['q_bytes'],original['q_bytes_per_line'])
        self.assertEqual(matrix['plan']['budget']['acquisition_runs'],1380)
        self.assertEqual(matrix['plan']['budget']['trace_seconds'],17136)
        self.assertEqual(matrix['plan']['budget']['acquisition_seconds'],33690)
        self.assertEqual(matrix['plan']['budget']['total_with_reserve_seconds'],48090)
        fixed=[c for c in matrix['plan']['conditions'] if c['input_kind']=='fixed']
        self.assertEqual(set(c['observation_seconds'] for c in fixed),{30,61})
        self.assertEqual(matrix['plan']['budget']['fixed_seconds'],sum(c['repeats']*(c['observation_seconds']+8) for c in fixed))


if __name__=='__main__':unittest.main()
