# SPDX-License-Identifier: Apache-2.0
"""Synthetic tagged cable/trace captures bind an explicit deployment end to end."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tools.g2.matrix import completed_run,run_matrix
from tools.g2.tests.test_deployment import deployment,register
from tools.g2.tests.test_pcapng import setup


def fixture(root):
    runs=[]
    trace=dict(end_ns=10**9,loss_seed=0,outage_action='hold',terminal='error',terminal_horizon_ns=None,
               rows=[dict(at_ns=0,t_ns=10000,loss='0',outage=False),
                     dict(at_ns=150000,t_ns=5000,loss='0',outage=False)])
    for family,vlan,profile in (('cable',100,None),('leo',200,trace)):
        folder=root/family;folder.mkdir()
        setup(folder,run=family,trace=profile,line_name=family,vlan_id=vlan)
        path=folder/'source/manifest.json';reg=register(json.loads(path.read_text()),family)
        reg['verification']=dict(mode='sampled',seqchecks=[])
        path.write_text(json.dumps(reg))
        runs.append(dict(id=family,manifest=f'{family}/pair.json'))
    matrix=root/'matrix.json'
    matrix.write_text(json.dumps(dict(schema='hng-m6-matrix-v1',mode='pcapng',scope='software-only',
        plan=dict(deployment=deployment()),runs=runs)))
    return matrix


class X710MatrixTests(unittest.TestCase):
    def test_worker_binds_deployment_after_snapshot_in_software_mode(self):
        from tools.g2 import matrix as runner
        from tools.g2.pcapng import prepare_pair
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);setup(root);path=root/'source/manifest.json'
            reg=register(json.loads(path.read_text()));path.write_text(json.dumps(reg))
            path=prepare_pair(root/'pair.json',root/'converted')
            reg=json.loads(path.read_text())
            original=runner.source_hashes
            def snapshot(source,mode):
                if not (root/'out/report.json').exists():
                    reg['deployment']['capture']['host']='changed-capture'
                    path.write_text(json.dumps(reg))
                return original(source,mode)
            with patch.object(runner,'source_hashes',side_effect=snapshot):
                code=runner.report_one(path,root/'out','demo','software-only',expected_deployment=deployment())
            self.assertEqual(code,2)
            report=json.loads((root/'out/report.json').read_text())
            self.assertEqual(report['reason_codes'],['DEPLOYMENT_MISMATCH'])

    def test_example_preserves_short_plan_and_declares_two_vlan_roles(self):
        examples=Path(__file__).resolve().parents[3]/'examples'
        short=json.loads((examples/'m6-short.json').read_text())
        x710=json.loads((examples/'m6-x710.json').read_text())
        plan=x710['plan'];dep=plan['deployment']
        for field in ('main','boundary','trace','timing_mask_bits','minimum_pairs','planning_pairs_estimate',
                      'warmup_seconds','drain_seconds','reset_seconds','trace_episode_seconds','trace_episodes_maximum'):
            self.assertEqual(plan[field],short['plan'][field])
        self.assertEqual(x710['scope'],'physical');self.assertEqual(x710['mode'],'pcapng')
        self.assertEqual(dep['nic'],dict(model='X710',pmd='i40e',ports=[0,1]))
        self.assertEqual(dep['vlans'],dict(cable=710,leo=711))

    def test_cable_and_leo_trace_use_registered_vlan_and_deployment(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);matrix=fixture(root);out=root/'report';summary=run_matrix(matrix,out)
            self.assertFalse(summary['g2_qualified'])
            self.assertEqual(summary['scope'],'software-only')
            self.assertEqual(summary['measurement_status'],'INCONCLUSIVE')
            self.assertEqual(summary['plan']['deployment'],deployment())
            for family,vlan in (('cable',100),('leo',200)):
                report=json.loads((out/family/'report.json').read_text())
                self.assertEqual(report['registration']['deployment'],deployment())
                self.assertEqual(report['observer_cost']['status'],'INCONCLUSIVE')
                self.assertEqual(report['reason_codes'],['observer_cost_queue_full'])
                self.assertEqual(report['registration']['lines'][0]['family'],family)
                self.assertEqual(report['rows'][0]['timing']['main']['n'],400)
                self.assertEqual(report['rows'][0]['timing']['main']['median_abs_error_ns'],3)
                for gate in ('order','storage'):self.assertEqual(report['rows'][0]['gates'][gate]['status'],'PASS')
                samples=[json.loads(line) for line in (out/family/'samples.jsonl').read_text().splitlines()]
                self.assertTrue(all(s['before']['vlan_id']==s['receive']['vlan_id']==vlan for s in samples))
                if family=='leo':
                    self.assertTrue(report['registration']['traces'])
                    self.assertTrue(any(s['expected']['clamp_extra_ns']>0 for s in samples))
            self.assertIsNotNone(completed_run(out/'cable',root/'cable/pair.json',
                'cable','software-only','pcapng',deployment()))
            changed=copy.deepcopy(deployment());changed['capture']['host']='other-capture'
            self.assertIsNone(completed_run(out/'cable',root/'cable/pair.json',
                'cable','software-only','pcapng',changed))

    def test_matrix_requires_its_deployment_in_every_measured_manifest(self):
        for mutation in ('missing','different'):
            with self.subTest(mutation=mutation),tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp);matrix=fixture(root);path=root/'cable/source/manifest.json'
                reg=json.loads(path.read_text())
                if mutation=='missing':
                    del reg['deployment']
                    for line in reg['lines']:del line['family']
                else:reg['deployment']['capture']['host']='different-capture'
                path.write_text(json.dumps(reg));out=root/'report';run_matrix(matrix,out)
                report=json.loads((out/'cable/report.json').read_text())
                self.assertEqual(report['measurement_status'],'INVALID')
                self.assertEqual(report['reason_codes'],['DEPLOYMENT_MISMATCH'])


if __name__=='__main__':unittest.main()
