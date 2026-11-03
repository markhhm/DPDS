# SPDX-License-Identifier: Apache-2.0
"""Complete approved inventory, directional paths and acquisition-time accounting."""
from collections import Counter
from fractions import Fraction
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

EXAMPLES=Path(__file__).resolve().parents[3]/'examples'


def bind(value):
    values={'DUT_HOST':'dut','GENERATOR_HOST':'generator','CAPTURE_HOST':'capture',
            'GENERATOR_PORT0_IF':'gen0','GENERATOR_PORT1_IF':'gen1',
            'CAPTURE_PORT0_IF':'cap0','CAPTURE_PORT1_IF':'cap1','CAPTURE_IF':'cap',
            'CAPTURE_CLOCK_ID':'capture','CAPTURE_MAPPING_ID':'ns',
            'CAPTURE_CLOCK_SOURCE':'synthetic-capture','CAPTURE_ROOT':'captures',
            'TRACE_PROFILE_ROOT':'trace-profiles'}
    if isinstance(value,dict):return {k:bind(v) for k,v in value.items()}
    if isinstance(value,list):return [bind(v) for v in value]
    if isinstance(value,str):
        for key,replacement in values.items():value=value.replace('${'+key+'}',replacement)
    return value


class FullX710Tests(unittest.TestCase):
    def test_fullrate_snapshot_preserves_every_pre_quarter_condition_and_bprime_rates(self):
        from tools.g2.expand_m6_x710 import build_matrix
        full=json.loads((EXAMPLES/'m6-x710-fullrate.json').read_text())
        self.assertEqual(full,build_matrix(rate_divisor=1))
        conditions=full['plan']['conditions'];self.assertEqual(len(conditions),360)
        # Canonical complete condition list from cf8a026, before quarter scaling.
        # Freeze the old evidence independently of the current producer, without
        # requiring Git history or another duplicate inventory at test runtime.
        digest=hashlib.sha256(json.dumps(conditions,sort_keys=True,separators=(',',':')).encode()).hexdigest()
        self.assertEqual(digest,'a8df1f952edafcfb76626817ce0e9d1fc0d334d0b3219cc52a014b8f190b8c97')
        condition=next(c for c in conditions if c['id']=='fixed-E3-142-320-1-both-L9-X')
        self.assertEqual(condition['direction_mode'],'both')
        self.assertEqual(condition['lines'][0]['offered_bps'],10_000_000_000)
        self.assertEqual(condition['lines'][0]['c_bps'],9_000_000_000)
        self.assertEqual(condition['lines'][0]['q_bytes'],8_388_608)
        with tempfile.TemporaryDirectory() as tmp:
            out=Path(tmp)/'fullrate.json'
            result=subprocess.run([sys.executable,'-B','-m','tools.g2.expand_m6_x710',
                '--rate-divisor','1','--out',str(out)],capture_output=True,text=True)
            self.assertEqual(result.returncode,0,result.stderr)
            self.assertEqual(out.read_bytes(),(EXAMPLES/'m6-x710-fullrate.json').read_bytes())

    def test_quarter_rate_preserves_ratios_queue_time_and_trace_curves(self):
        from tools.g2.expand_m6_x710 import build_matrix
        full=build_matrix(rate_divisor=1);quarter=build_matrix()
        self.assertEqual(len(quarter['plan']['conditions']),360)
        for old,new in zip(full['plan']['conditions'],quarter['plan']['conditions']):
            for key in ('id','repeats','episodes_per_repeat','direction_mode','traffic'):
                self.assertEqual(old[key],new[key])
            for a,b in zip(old['lines'],new['lines']):
                for key in ('c_bps','offered_bps','q_bytes'):self.assertEqual(a[key],4*b[key])
                self.assertEqual(Fraction(a['q_bytes'],a['c_bps']),Fraction(b['q_bytes'],b['c_bps']))
                self.assertEqual(Fraction(a['offered_bps'],a['c_bps']),Fraction(b['offered_bps'],b['c_bps']))
                for key in ('t_ns','delta_ns','p','seed','vlan_id','trace_windows'):
                    self.assertEqual(a.get(key),b.get(key))
        for key in ('trace_windows','timing_mask_bits','minimum_pairs','planning_pairs_estimate'):
            self.assertEqual(full['plan'][key],quarter['plan'][key])
        self.assertEqual(full['plan']['budget']['acquisition_seconds'],33690)
        self.assertEqual(quarter['plan']['budget']['acquisition_seconds'],47808)
        self.assertEqual(quarter['plan']['budget']['total_with_reserve_seconds'],62208)

    def test_every_condition_fits_nominal_four_mirror_budget(self):
        from tools.g2.expand_m6_x710 import build_matrix,mirror_budget
        matrix=build_matrix();totals=[]
        for c in matrix['plan']['conditions']:
            ports=2 if c['direction_mode']=='both' else 1
            ingress=ports*sum(l['offered_bps'] for l in c['lines'])
            egress=ports*sum(min(l['offered_bps'],l['c_bps']) for l in c['lines'])
            rate=mirror_budget(c)
            self.assertEqual(rate,dict(ingress_bps=ingress,egress_bps=egress,
                total_bps=ingress+egress,headroom_bps=10**10-ingress-egress))
            self.assertEqual(c['mirror_estimate'],rate)
            self.assertLessEqual(rate['total_bps'],10**10);totals.append(rate['total_bps'])
        self.assertEqual(max(totals),9_500_000_000)
        self.assertEqual(matrix['plan']['capture_mirror']['snaplen_bytes'],128)
        capture=matrix['plan']['deployment']['capture']
        self.assertEqual(capture['interfaces'],{'0':'${CAPTURE_IF}','1':'${CAPTURE_IF}'})

    def test_sparse_late_exposure_retains_existing_sample_target(self):
        from tools.g2.expand_m6_x710 import build_matrix
        fixed=[c for c in build_matrix()['plan']['conditions'] if c['input_kind']=='fixed']
        self.assertEqual(Counter(c['observation_seconds'] for c in fixed),{30:222,242:78})
        for c in fixed:
            for line in c['lines']:
                p=Fraction(line['p']);fraction=min(p,1-p) if 0<p<1 else Fraction(1)
                expected=Fraction(min(line['c_bps'],line['offered_bps'])*c['observation_seconds'],8*1542*1024)*fraction
                self.assertGreaterEqual(expected,431)

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

    def test_full_length_x_per_flow_index_is_below_one_decimal_gigabyte(self):
        from tools.seqcheck.budget import index_bytes
        from tools.g2.matrix import generator_budget
        from fractions import Fraction
        matrix=json.loads((EXAMPLES/'m6-x710.json').read_text());plan=matrix['plan']
        condition=next(c for c in plan['conditions'] if c['id']=='fixed-E1-65-5-0.01-forward-L9-X')
        self.assertEqual(condition['observation_seconds'],242)
        bits=condition['lines'][0]['offered_bps']*(condition['observation_seconds']+plan['warmup_seconds'])
        packets=Fraction(bits*3,8*(84+532+1542))
        packets=-(-packets.numerator//packets.denominator)
        self.assertEqual(packets,106000927)
        maximum=(packets+63)//64-1
        self.assertEqual(maximum,1656264)
        dense=index_bytes(64,maximum);global_ids=index_bytes(64,packets-1)
        self.assertLessEqual(dense,1000000000)
        self.assertGreater(global_ids,4294967296)
        self.assertTrue(all(size<=4294967296 for size in generator_budget(plan).values()))

    def test_trace_only_snapshot_and_producer_keep_generator_registration(self):
        import copy
        from tools.g2.matrix import read_matrix
        from tools.g2.register_m6_trace import register_episode_acceptance
        path=EXAMPLES/'m6-trace-conditions.json'
        snapshot=json.loads(path.read_text())
        self.assertEqual(len(read_matrix(path)['runs']),len(snapshot['runs']))
        original=copy.deepcopy(snapshot)
        for condition in original['plan']['trace_conditions']:
            condition['all_steady_planning_targets_met']=condition.pop('all_late_diagnostic_targets_met')
            for row in condition['directions'].values():
                row['steady_planning_target_431_met']=row.pop('late_diagnostic_target_431_met')
                row['steady_expected_368_met']=row.pop('late_diagnostic_expected_368_met')
        original['plan'].pop('generator',None)
        self.assertEqual(register_episode_acceptance(original),snapshot)

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
                self.assertEqual(4*line['c_bps'],original['c_bps_per_line'])
                self.assertEqual(4*line['offered_bps'],original['offered_bps_per_line'])
                self.assertEqual(4*line['q_bytes'],original['q_bytes_per_line'])
        self.assertEqual(matrix['plan']['budget']['acquisition_runs'],1380)
        self.assertEqual(matrix['plan']['budget']['trace_seconds'],17136)
        self.assertEqual(matrix['plan']['budget']['acquisition_seconds'],47808)
        self.assertEqual(matrix['plan']['budget']['total_with_reserve_seconds'],62208)
        fixed=[c for c in matrix['plan']['conditions'] if c['input_kind']=='fixed']
        self.assertEqual(set(c['observation_seconds'] for c in fixed),{30,242})
        self.assertEqual(matrix['plan']['budget']['fixed_seconds'],sum(c['repeats']*(c['observation_seconds']+8) for c in fixed))


if __name__=='__main__':unittest.main()
