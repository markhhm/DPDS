# SPDX-License-Identifier: Apache-2.0
"""Short-run inspection recomputes from retained raw, with no invented drain."""
import json
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import unittest

from tools.g2.matrix import run_matrix
from tools.g2.tests.binary_sample_fixture import make_case
from tools.g2.tests.test_raw_prune import digest,load,write
try:
    from tools.g2.prefix_report import run_prefix
except ImportError:
    run_prefix=None


class PrefixReportTests(unittest.TestCase):
    def setup_case(self,root,*,stress=False,trace=False,timing_fault=False,q_fault=False,keep_ns=None,flag_fault=False):
        raw=root/'capture';make_case(raw,count=800,stress=stress,trace=trace,mask_bits=0,
                                   q_bytes=2000 if stress else 8*1024*1024)
        pair=load(raw/'pair.json');pair['manifest']='binary.json';write(raw/'pair.json',pair)
        manifest=load(raw/'binary.json');manifest['verification']['seqchecks']=[]
        if timing_fault:
            path=raw/'receiver.pcapng';data=bytearray(path.read_bytes());cursor=0;packet=0
            while cursor<len(data):
                kind,size=struct.unpack_from('<II',data,cursor)
                if kind==6 and (timing_fault!='tail' or packet>=750):
                    high,low=struct.unpack_from('<II',data,cursor+12);stamp=(high<<32|low)+(50_000 if timing_fault=='allowed' else 2_000_000)
                    struct.pack_into('<II',data,cursor+12,stamp>>32,stamp&0xffffffff)
                if kind==6:packet+=1
                cursor+=size
            path.write_bytes(data)
        if flag_fault:
            for role in ('before','receiver'):
                path=raw/(role+'.pcapng');data=bytearray(path.read_bytes());cursor=0
                while cursor<len(data):
                    kind,size=struct.unpack_from('<II',data,cursor)
                    if kind==6:data[cursor+28+46+12]=1
                    cursor+=size
                path.write_bytes(data)
        if q_fault:
            from tools.g2.tests.test_native_binary import MAGIC,entry,frame,line
            records=[json.loads(value) for value in (raw/'native.jsonl').read_text().splitlines()]
            encoded=[]
            for row in records[1:-1]:
                common=[row['at_ns'],row['gate_ns'],row['submit_before_ns'],row['submit_after_ns'],
                    *row['resources'],row['actual_selection_seq'],row['attempt_id'],*row['consumed_dispatch'],
                    row['first_handoff_seq'],row['handoff_queue'],row['port'],row['accepted_count']]
                kind=2 if q_fault=='accepted' and row['kind']==1 else row['kind']
                encoded.append(entry(producer=row['producer'],kind=kind,seq=row['producer_seq'],order=row['order'],
                    common=common,items=row['items'],snapshot=row['snapshot'],reason=3 if kind==2 else row['reason']))
            # One record per bounded frame keeps the test mutation independent of production packing.
            data=MAGIC+frame(1,line(records[0]),0)
            data+=b''.join(frame(2,row,i+1) for i,row in enumerate(encoded))
            data+=frame(3,line(records[-1]),len(encoded)+1)
            (raw/'native.hngb').write_bytes(data)
            manifest['verification']['native_binary']['sha256']=digest(raw/'native.hngb')
        write(raw/'binary.json',manifest)
        keep=keep_ns if keep_ns is not None else 80_000_000 if timing_fault or trace else 66_000_000
        policy=dict(keep_ns=keep,byte_budget=2_000_000,capture_directory='capture')
        matrix=dict(schema='hng-m6-matrix-v1',scope='software-only',mode='pcapng',plan={},runs=[
            dict(id='demo',manifest='capture/pair.json',extensions=dict(m6=dict(acquisition_id='acq',raw_retention=policy)))])
        path=root/'matrix.json';write(path,matrix);out=root/'complete'
        run_matrix(path,out,prune_raw=True,apply_raw_prune=True,backup_scope_confirmed=True)
        return path,raw,out

    def invoke(self,path,complete,target):
        self.assertIsNotNone(run_prefix,'retained prefix reporter is missing')
        return run_prefix(path,complete,'demo',target)

    def test_retained_window_is_a_separate_short_run_with_pending_tail(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);path,raw,complete=self.setup_case(root)
            original={str(p.relative_to(complete)):p.read_bytes() for p in complete.rglob('*') if p.is_file() and not p.is_symlink()}
            report=self.invoke(path,complete,root/'short')
            self.assertEqual(report['scope'],'diagnostic-prefix');self.assertFalse(report['g2_qualified'])
            self.assertEqual(report['schema'],'hng-g2-prefix-report-v1')
            self.assertEqual(report['status'],'PASS',report)
            self.assertEqual(report['inputs']['native_packets'],800)
            row=report['rows'][0]
            self.assertGreater(row['pending_receiver'],0)
            self.assertEqual(row['unobserved_due_samples'],0)
            self.assertGreater(row['timing_samples'],431);self.assertLess(row['timing_samples'],800)
            self.assertEqual(row['q_mismatches'],0)
            self.assertGreater(report['seqchecks'][0]['totals']['missing'],0)
            self.assertEqual(report['coverage']['storage'],'unverified without drain; visible terminal drops diagnosed')
            self.assertTrue(all((root/'short'/name).is_file() for name in ('report.json','results.csv','summary.txt','samples.jsonl')))
            self.assertFalse((root/'short/.complete.json').exists())
            summary=(root/'short/summary.txt').read_text()
            self.assertIn('diagnostic',summary.lower());self.assertNotIn('0/0',summary)
            import csv
            with (root/'short/results.csv').open() as stream:
                csv_row=next(csv.DictReader(stream))
            self.assertEqual(int(csv_row['pending_receiver']),row['pending_receiver'])
            samples=[json.loads(line) for line in (root/'short/samples.jsonl').read_text().splitlines()]
            self.assertTrue(any(sample['error_ns']==1237 for sample in samples))
            cli=subprocess.run([sys.executable,'-m','tools.g2.prefix_report',str(path),'--reports',str(complete),
                                '--run','demo','--out',str(root/'cli')],capture_output=True,text=True)
            self.assertEqual(cli.returncode,0,cli.stderr)
            self.assertEqual(json.loads(cli.stdout)['scope'],'diagnostic-prefix')
            self.assertFalse((root/'cli/.complete.json').exists())
            self.assertEqual(original,{str(p.relative_to(complete)):p.read_bytes() for p in complete.rglob('*') if p.is_file() and not p.is_symlink()})

    def test_real_timing_error_and_q_decision_error_remain_visible(self):
        for name,kwargs,field in [('timing',dict(timing_fault=True),'timing'),('q',dict(stress=True,q_fault=True),'q_mismatches')]:
            with self.subTest(name=name),tempfile.TemporaryDirectory() as temp:
                root=Path(temp);path,raw,complete=self.setup_case(root,**kwargs)
                report=self.invoke(path,complete,root/'short');row=report['rows'][0]
                self.assertEqual(report['status'],'FAIL',report)
                if name=='q':self.assertGreater(row[field],0)
                else:self.assertEqual(row['timing']['median']['status'],'FAIL')

    def test_stress_and_trace_keep_exact_arrival_prefix(self):
        for kwargs in (dict(stress=True),dict(trace=True)):
            with self.subTest(kwargs=kwargs),tempfile.TemporaryDirectory() as temp:
                root=Path(temp);path,raw,complete=self.setup_case(root,**kwargs)
                report=self.invoke(path,complete,root/'short')
                self.assertNotEqual(report['status'],'FAIL',report)
                self.assertEqual(report['reference']['prefix_events'],800)
                self.assertEqual(report['rows'][0]['q_mismatches'],0)
                if kwargs.get('trace'):self.assertGreater(report['rows'][0]['max_clamp_ns'],0)
                else:self.assertGreater(report['reference']['line_counts']['cable']['q_drop'],0)

    def test_short_run_does_not_inherit_full_verdict_and_small_prefix_is_inconclusive(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);path,raw,complete=self.setup_case(root,timing_fault='tail',keep_ns=66_000_000)
            self.assertEqual(load(complete/'demo/verdict.json')['status'],'FAIL')
            report=self.invoke(path,complete,root/'short')
            self.assertEqual(report['status'],'PASS',report.get('reason',report.get('rows')))
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);path,raw,complete=self.setup_case(root,keep_ns=200000)
            report=self.invoke(path,complete,root/'short')
            self.assertEqual(report['status'],'INCONCLUSIVE',report.get('reason',report.get('rows')))
            self.assertEqual(report['rows'][0]['timing_samples'],0)
            self.assertFalse(report['g2_qualified'])

    def test_visible_terminal_drop_and_before_flag_error_are_not_hidden_by_pending(self):
        for options in (dict(q_fault='accepted'),dict(flag_fault=True,keep_ns=200000)):
            with self.subTest(options=options),tempfile.TemporaryDirectory() as temp:
                root=Path(temp);path,raw,complete=self.setup_case(root,**options)
                report=self.invoke(path,complete,root/'short')
                self.assertEqual(report['status'],'FAIL',report.get('reason',report.get('rows')))

    def test_allowed_lateness_past_cut_is_not_a_proven_drop(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);path,raw,complete=self.setup_case(root,timing_fault='allowed',keep_ns=66_000_000)
            self.assertEqual(load(complete/'demo/verdict.json')['status'],'PASS')
            report=self.invoke(path,complete,root/'short')
            self.assertEqual(report['status'],'INCONCLUSIVE',report.get('reason',report.get('rows')))
            self.assertGreater(report['rows'][0]['unobserved_due_samples'],0)
            self.assertTrue(all(row['g2_qualified'] is False for row in report['rows']))

    def test_bidirectional_seven_writer_prefix_uses_both_capture_pairs(self):
        from tools.g2.tests.binary_split_fixture import make_case as make_split
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);raw=root/'capture';make_split(raw,count=400)
            policy=dict(keep_ns=66_000_000,byte_budget=2_000_000,capture_directory='capture')
            matrix=dict(schema='hng-m6-matrix-v1',scope='software-only',mode='pcapng',plan={},runs=[
                dict(id='demo',manifest='capture/pair-forward.json',extensions=dict(m6=dict(acquisition_id='acq',raw_retention=policy)))])
            path=root/'matrix.json';write(path,matrix);complete=root/'complete'
            run_matrix(path,complete,prune_raw=True,apply_raw_prune=True,backup_scope_confirmed=True)
            report=self.invoke(path,complete,root/'short')
            self.assertNotIn(report['status'],('FAIL','INVALID'),report.get('reason',report.get('rows')))
            self.assertEqual(report['inputs']['native_packets'],800)
            self.assertEqual(len(report['rows']),2);self.assertEqual(len(report['seqchecks']),2)
            self.assertTrue(all(row['q_mismatches']==0 and row['timing_samples']==400 for row in report['rows']))

    def test_refuses_output_overlap_existing_output_and_bad_prefix_hash(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);path,raw,complete=self.setup_case(root)
            for target in (complete,complete/'demo/new',raw/'new'):
                with self.subTest(target=target),self.assertRaises(ValueError):self.invoke(path,complete,target)
            self.invoke(path,complete,root/'short')
            before=(root/'short/report.json').read_bytes()
            with self.assertRaises((ValueError,FileExistsError)):self.invoke(path,complete,root/'short')
            self.assertEqual((root/'short/report.json').read_bytes(),before)
            (raw/'native.hngb').write_bytes((raw/'native.hngb').read_bytes()+b' ')
            with self.assertRaises(ValueError):self.invoke(path,complete,root/'bad')
            self.assertFalse((root/'bad').exists())


if __name__=='__main__':unittest.main()
