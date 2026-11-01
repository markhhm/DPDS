# SPDX-License-Identifier: Apache-2.0
"""Binary sampled input keeps registration and verdict boundaries explicit."""
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest

from tools.g2.registration import validate_registration
from tools.g2.tests.test_reference import config
from tools.g2.tests.test_registration import manifest


class SampledBinaryTests(unittest.TestCase):
    def test_seven_writer_bidirectional_verdicts_are_byte_identical(self):
        from tools.g2.matrix import report_one
        from tools.g2.tests.binary_split_fixture import make_case
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);make_case(root)
            for mode in ('legacy','binary'):
                self.assertEqual(report_one(root/f'{mode}.json',root/f'{mode}-report','demo','software-only'),0)
            for name in ('verdict.json','results.csv','summary.txt'):
                self.assertEqual((root/'binary-report'/name).read_bytes(),(root/'legacy-report'/name).read_bytes(),name)

    def test_observed_q_mismatch_stays_a_byte_identical_fail(self):
        from tools.g2.matrix import report_one
        from tools.g2.native_adapter import NativeAdapter
        from tools.g2.native_binary import decode
        from tools.g2.tests.binary_sample_fixture import make_case
        from tools.g2.tests.test_native_binary import MAGIC,entry,frame,line
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);data=make_case(root)
            decoded=io.StringIO()
            with (root/'native.hngb').open('rb') as source:decode(source,decoded)
            records=[json.loads(raw) for raw in decoded.getvalue().splitlines()]
            output=[MAGIC,frame(1,line(records[0]),0)]
            for i,row in enumerate(records[1:-1],1):
                if row['kind']==1 and row['actual_selection_seq']==0:row['actual_selection_seq']=999
                common=[row['at_ns'],row['gate_ns'],row['submit_before_ns'],row['submit_after_ns'],*row['resources'],
                    row['actual_selection_seq'],row['attempt_id'],*row['consumed_dispatch'],row['first_handoff_seq'],
                    row['handoff_queue'],row['port'],row['accepted_count']]
                value=entry(producer=row['producer'],kind=row['kind'],reason=row['reason'],seq=row['producer_seq'],
                    order=row['order'],common=common,snapshot=row['snapshot'],items=row['items'])
                output.append(frame(2,value,i))
            output.append(frame(3,line(records[-1]),len(output)-1))
            (root/'native.hngb').write_bytes(b''.join(output))
            targets=[io.StringIO() for _ in range(3)]
            raw=''.join(json.dumps(row)+'\n' for row in records)
            self.assertTrue(NativeAdapter(io.StringIO(raw),*targets,run='demo',lines=data['config']['lines']).process()['complete'])
            for role,text in zip(('ingress','host'),targets):
                path=root/f'{role}.jsonl';path.write_text(text.getvalue())
                next(s for s in data['legacy_manifest']['streams'] if s['role']==role)['file_sha256']=hashlib.sha256(path.read_bytes()).hexdigest()
            data['binary_manifest']['verification']['native_binary']['sha256']=hashlib.sha256((root/'native.hngb').read_bytes()).hexdigest()
            (root/'legacy.json').write_text(json.dumps(data['legacy_manifest']))
            (root/'binary.json').write_text(json.dumps(data['binary_manifest']))
            for mode in ('legacy','binary'):
                self.assertEqual(report_one(root/f'{mode}.json',root/f'{mode}-report','demo','software-only'),1)
            for name in ('verdict.json','results.csv','summary.txt'):
                self.assertEqual((root/'binary-report'/name).read_bytes(),(root/'legacy-report'/name).read_bytes(),name)

    def test_crc_failure_cannot_be_a_sampled_pass(self):
        from tools.g2.matrix import report_one
        from tools.g2.tests.binary_sample_fixture import make_case
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);data=make_case(root)
            path=root/'native.hngb';raw=bytearray(path.read_bytes());raw[28]^=1;path.write_bytes(raw)
            data['binary_manifest']['verification']['native_binary']['sha256']=hashlib.sha256(raw).hexdigest()
            (root/'binary.json').write_text(json.dumps(data['binary_manifest']))
            self.assertEqual(report_one(root/'binary.json',root/'out','demo','software-only'),2)
            report=json.loads((root/'out/report.json').read_text())
            self.assertEqual(report['measurement_status'],'INCONCLUSIVE')
            self.assertEqual(report['reason_codes'],['NATIVE_BINARY_FORMAT'])

    def test_native_and_jsonl_verdict_artifacts_are_byte_identical(self):
        from tools.g2.matrix import report_one
        from tools.g2.tests.binary_sample_fixture import make_case
        for stress,trace in ((False,False),(True,False),(False,True)):
            with self.subTest(stress=stress,trace=trace),tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp);data=make_case(root,count=800 if stress else 400,stress=stress,trace=trace,q_bytes=32768 if stress else 8*1024*1024)
                run=data['legacy_manifest']['run']
                legacy=report_one(root/'legacy.json',root/'legacy-report',run,'software-only')
                binary=report_one(root/'binary.json',root/'binary-report',run,'software-only')
                self.assertEqual(binary,legacy)
                if stress:
                    report=json.loads((root/'legacy-report/report.json').read_text())
                    self.assertGreater(report['rows'][0]['counts']['q_drop'],0)
                for name in ('verdict.json','results.csv','summary.txt'):
                    self.assertEqual((root/'binary-report'/name).read_bytes(),(root/'legacy-report'/name).read_bytes(),name)

    def test_binary_needs_no_expanded_packet_jsonl_or_native_counter_prepass(self):
        from tools.g2.matrix import report_one
        from tools.g2.protocol import StreamReader,StreamWriter
        from tools.g2.tests.binary_sample_fixture import make_case
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);data=make_case(root)
            self.assertEqual(report_one(root/'legacy.json',root/'legacy-report','demo','software-only'),0)
            rows=[row for row in StreamReader(root/'counters.jsonl','counters') if row['scope']=='observer' and row['scope_id'] in ('before','receiver')]
            (root/'counters.jsonl').unlink()
            writer=StreamWriter(root/'counters.jsonl','demo','native-counters','counters','engine')
            for row in rows:writer.emit('counter',**{k:v for k,v in row.items() if k not in ('kind','run','stream_id','seq')})
            writer.finish()
            binary=data['binary_manifest']
            binary['streams'][0]['file_sha256']=hashlib.sha256((root/'counters.jsonl').read_bytes()).hexdigest()
            (root/'binary.json').write_text(json.dumps(binary))
            for name in ('native.jsonl','native-counters.jsonl','ingress.jsonl','host.jsonl','before.jsonl','receiver.jsonl'):(root/name).unlink()
            self.assertEqual(report_one(root/'binary.json',root/'binary-report','demo','software-only'),0)
            for name in ('verdict.json','results.csv','summary.txt'):
                self.assertEqual((root/'binary-report'/name).read_bytes(),(root/'legacy-report'/name).read_bytes(),name)

    def test_binary_registration_needs_only_small_counter_stream(self):
        reg=manifest()
        reg['verification']=dict(mode='sampled',seqchecks=[],
            native_binary=dict(path='native.hngb',sha256='0'*64),capture_pairs=['pair.json'])
        reg['streams']=[row for row in reg['streams'] if row['role']=='counters']
        validate_registration(reg,config())

    def test_binary_registration_requires_hash_and_capture_metadata(self):
        for missing in ('sha256','capture_pairs'):
            reg=manifest()
            reg['verification']=dict(mode='sampled',seqchecks=[],
                native_binary=dict(path='native.hngb',sha256='0'*64),capture_pairs=['pair.json'])
            if missing=='capture_pairs':del reg['verification'][missing]
            else:del reg['verification']['native_binary'][missing]
            with self.assertRaises(ValueError):validate_registration(reg,config())

    def test_resume_binds_native_pair_metadata_and_raw_captures(self):
        from tools.g2.matrix import source_hashes
        from tools.g2.tests.test_bundle import fixture
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);cfg,reg,_,cal=fixture(root)
            for name in ('native.hngb','before.pcapng','receiver.pcapng'):(root/name).write_bytes(name.encode())
            pair=dict(before=dict(path='before.pcapng'),receiver=dict(path='receiver.pcapng'))
            (root/'pair.json').write_text(json.dumps(pair))
            reg['streams']=[s for s in reg['streams'] if s['role']=='counters']
            reg['verification']=dict(mode='sampled',seqchecks=[],native_binary=dict(path='native.hngb',sha256='0'*64),capture_pairs=['pair.json'])
            (root/'binary.json').write_text(json.dumps(reg))
            hashes=source_hashes(root/'binary.json','software')
            self.assertTrue({str(root/name) for name in ('native.hngb','pair.json','before.pcapng','receiver.pcapng')}<=hashes.keys())
            self.assertNotIn(str(root/'ingress.jsonl'),hashes)


if __name__=='__main__':unittest.main()
