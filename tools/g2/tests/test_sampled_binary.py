# SPDX-License-Identifier: Apache-2.0
"""Binary sampled input keeps registration and verdict boundaries explicit."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from tools.g2.registration import validate_registration
from tools.g2.tests.test_reference import config
from tools.g2.tests.test_registration import manifest


class SampledBinaryTests(unittest.TestCase):
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
