# SPDX-License-Identifier: Apache-2.0
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from tools.g2.bundle import build_bundle_report
from tools.g2.tests.test_bundle import fixture

METRICS=('ingress','receiver','ordinary_reorder','missing','duplicate_ingress','duplicate_receiver','unexpected','flag_mismatch')


def checker(root,reg,n,missing=0):
    inputs={}
    for role in ('ingress','receiver'):
        data=(role+' synthetic capture').encode();(root/(role+'.pcapng')).write_bytes(data)
        inputs[role]=dict(path=role+'.pcapng',sha256=hashlib.sha256(data).hexdigest(),byte_size=len(data),packets=n-(missing if role=='receiver' else 0),health=dict(complete=True,capture_drops=0),error=None)
    totals=dict.fromkeys(METRICS,0);totals.update(ingress=n,receiver=n-missing,missing=missing)
    doc=dict(schema='hng-seqcheck-v1',scope='supplied-captures',status='FAIL' if missing else 'PASS',complete=True,error=None,
        inputs=inputs,totals=totals,flows=[dict(totals,ingress=n//len(reg['lines']),receiver=(n-missing)//len(reg['lines']),flow_id='0a000001:0a000002:1:9000',vlan_id=line['vlan_id']) for line in reg['lines']],details_limit=0,
        details={k:[] for k in METRICS[2:]},details_truncated={k:bool(totals[k]) for k in METRICS[2:]})
    raw=json.dumps(doc).encode();(root/'seqcheck.json').write_bytes(raw)
    reg['verification']=dict(mode='sampled',seqchecks=[dict(path='seqcheck.json',sha256=hashlib.sha256(raw).hexdigest(),ingress_port=0,egress_port=1,ingress_path='ingress.pcapng',receiver_path='receiver.pcapng')])


class SampledBundleTests(unittest.TestCase):
    def test_same_all_sample_timing_and_full_external_gates_without_custody(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);cfg,reg,sources,cal=fixture(root,flow_id='0a000001:0a000002:1:9000')
            baseline=build_bundle_report(cfg,{},reg,sources,cal)
            checker(root,reg,400)
            with patch('tools.g2.bundle.LiveCustody',side_effect=AssertionError('full custody invoked')):
                result=build_bundle_report(cfg,{},reg,sources,cal,registration_root=root)
            self.assertEqual(result['measurement_status'],'PASS',result.get('reason_codes'))
            self.assertEqual(result['rows'][0]['timing']['main'],baseline['rows'][0]['timing']['main'])
            self.assertEqual(result['rows'][0]['gates']['order']['status'],'PASS')
            self.assertEqual(result['coverage']['custody'],'diagnostic')
            self.assertEqual(result['coverage']['q'],'sampled')
            self.assertEqual(result['sampled_reference']['sample_checked'],400)

    def test_failure_stays_in_its_registered_line(self):
        def mutate(rows,reg):
            rows['host'][0]['actual_selection_seq']=999
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);cfg,reg,sources,cal=fixture(root,count=800,lines=2,mutate=mutate,flow_id='0a000001:0a000002:1:9000')
            checker(root,reg,800)
            result=build_bundle_report(cfg,{},reg,sources,cal,registration_root=root)
            states={row['line']:row['measurement_status'] for row in result['rows']}
            self.assertEqual(states,{'cable':'FAIL','leo':'PASS'})

    def test_human_outputs_label_sampled_scope(self):
        import csv
        from tools.g2.report import write_outputs
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);cfg,reg,sources,cal=fixture(root)
            checker(root,reg,400)
            result=build_bundle_report(cfg,{},reg,sources,cal,registration_root=root)
            output=root/'out';output.mkdir();write_outputs(result,output)
            text=(output/'summary.txt').read_text()
            self.assertIn('timing/Q: sampled',text)
            self.assertIn('custody: diagnostic',text)
            with (output/'results.csv').open() as source:row=next(csv.DictReader(source))
            self.assertEqual(row['verification_mode'],'sampled')
            self.assertEqual(row['q_coverage'],'sampled')

    def test_sampled_mode_keeps_explicit_approved_mask(self):
        from tools.g2.registration import validate_registration
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);cfg,reg,sources,cal=fixture(root)
            checker(root,reg,400)
            reg['required_cells'][0].update(mask_bits=8,planned_survivors=1_000_000)
            validate_registration(reg,cfg)
            del reg['verification']
            with self.assertRaisesRegex(ValueError,'largest qualifying mask'):validate_registration(reg,cfg)

    def test_sample_flow_must_belong_to_external_checker_coverage(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);cfg,reg,sources,cal=fixture(root,flow_id='different')
            checker(root,reg,400)
            result=build_bundle_report(cfg,{},reg,sources,cal,registration_root=root)
            self.assertEqual(result['measurement_status'],'INCONCLUSIVE')
            self.assertIn('seqcheck_sample_flow_mismatch',result['reason_codes'])

    def test_sparse_selection_uses_existing_cell_hash(self):
        from tools.g2.online import sample
        from tools.g2.protocol import StreamReader
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);cfg,reg,sources,cal=fixture(root,flow_id='0a000001:0a000002:1:9000')
            checker(root,reg,400)
            reg['required_cells'][0].update(mask_bits=1,planned_survivors=0)
            expected=sum(sample(row,reg['sampling']['seed'],1) for row in StreamReader(sources['ingress'],'ingress') if row['kind']=='packet')
            result=build_bundle_report(cfg,{},reg,sources,cal,registration_root=root)
            self.assertEqual(result['sampled_reference']['sample_checked'],expected)
            self.assertLess(expected,400)
            self.assertEqual(result['rows'][0]['gates']['order']['status'],'PASS')

    def test_changed_stream_hash_cannot_pass(self):
        from tools.g2.protocol import StreamError
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);cfg,reg,sources,cal=fixture(root)
            checker(root,reg,400)
            with sources['ingress'].open('a') as out:out.write('garbage')
            with self.assertRaises(StreamError):build_bundle_report(cfg,{},reg,sources,cal,registration_root=root)

    def test_missing_seqcheck_cannot_become_pass(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);cfg,reg,sources,cal=fixture(root,flow_id='0a000001:0a000002:1:9000')
            reg['verification']=dict(mode='sampled',seqchecks=[])
            result=build_bundle_report(cfg,{},reg,sources,cal,registration_root=root)
            self.assertEqual(result['measurement_status'],'INCONCLUSIVE')
            self.assertEqual(result['rows'][0]['gates']['order']['status'],'INCONCLUSIVE')

    def test_sampled_q_decision_mismatch_fails(self):
        def mutate(rows,reg):
            rows['host'][0]['actual_selection_seq']=999
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);cfg,reg,sources,cal=fixture(root,mutate=mutate,flow_id='0a000001:0a000002:1:9000')
            checker(root,reg,400)
            result=build_bundle_report(cfg,{},reg,sources,cal,registration_root=root)
            self.assertEqual(result['measurement_status'],'FAIL')
            self.assertIn('sample_q_decision_mismatch',result['reason_codes'])

    def test_unsampled_custody_diagnostics_do_not_invalidate_sampled_verdict(self):
        def mutate(rows,reg):
            rows['host']=[r for r in rows['host'] if r['kind']!='dispatch']
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);cfg,reg,sources,cal=fixture(root,mutate=mutate,flow_id='0a000001:0a000002:1:9000')
            checker(root,reg,400)
            result=build_bundle_report(cfg,{},reg,sources,cal,registration_root=root)
            self.assertEqual(result['measurement_status'],'PASS',result.get('reason_codes'))
            self.assertTrue(result['coverage']['unverified'])

if __name__=='__main__':unittest.main()
