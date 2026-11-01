# SPDX-License-Identifier: Apache-2.0
"""Binary sampled input keeps registration and verdict boundaries explicit."""
import hashlib
import io
import json
import os
from pathlib import Path
import struct
import tempfile
import unittest

from tools.g2.registration import validate_registration
from tools.g2.tests.test_reference import config
from tools.g2.tests.test_registration import manifest


class SampledBinaryTests(unittest.TestCase):
    def test_unsampled_capture_timestamps_still_require_valid_calibration(self):
        from tools.g2.calibration import Calibration
        from tools.g2.matrix import report_one
        from tools.g2.online import sample
        from tools.g2.pcapng import convert_stream
        from tools.g2.tests.binary_sample_fixture import END, FLOW, make_case
        for width,code in ((64,'CLOCK_EXTRAPOLATION'),(50,'CLOCK_WIDTH')):
            with self.subTest(width=width),tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp);data=make_case(root,count=1001,mask_bits=1)
                self.assertFalse(sample(dict(run='demo',ingress_port=0,external_identity=f'{FLOW}:1000'),17,1))
                path=root/'receiver.pcapng';raw=bytearray(path.read_bytes());position=0
                while position<len(raw):
                    kind,size=struct.unpack_from('<II',raw,position)
                    if kind==6:last_packet=position
                    if kind==5:stats=position
                    position+=size
                stamp=2*10**15
                for at in (last_packet+12,stats+12):struct.pack_into('<II',raw,at,stamp>>32,stamp&0xffffffff)
                position=stats+20
                while True:
                    kind,size=struct.unpack_from('<HH',raw,position)
                    if not kind:break
                    if kind==3:struct.pack_into('<II',raw,position+4,stamp>>32,stamp&0xffffffff)
                    position+=4+((size+3)&~3)
                path.write_bytes(raw)
                data['calibration']['clocks'][0]['width_bits']=width
                (root/'calibration.json').write_text(json.dumps(data['calibration']))
                check=json.loads((root/'seqcheck.json').read_text())
                check['inputs']['receiver']['sha256']=hashlib.sha256(raw).hexdigest()
                (root/'seqcheck.json').write_text(json.dumps(check))
                reg=data['binary_manifest']
                reg['verification']['seqchecks'][0]['sha256']=hashlib.sha256((root/'seqcheck.json').read_bytes()).hexdigest()
                (root/'binary.json').write_text(json.dumps(reg))
                pair=json.loads((root/'pair.json').read_text())
                stream=next(s for s in data['legacy_manifest']['streams'] if s['role']=='receiver')
                converted=convert_stream(pair,'receiver',root,data['legacy_manifest'],stream,root/'invalid.jsonl',
                    Calibration(data['calibration'],'software-only','demo'),(0,END))
                self.assertFalse(converted['complete']);self.assertEqual(converted['reason_codes'],[code])
                self.assertEqual(report_one(root/'binary.json',root/'report','demo','software-only'),2)
                self.assertEqual(json.loads((root/'report/report.json').read_text())['reason_codes'],[code])

    def test_before_and_receiver_cannot_alias_the_same_capture(self):
        from tools.g2.matrix import report_one
        from tools.g2.tests.binary_sample_fixture import make_case
        for hardlink in (False,True):
            with self.subTest(hardlink=hardlink),tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp);data=make_case(root)
                data['config']['lines'][0]['model']['t_ns']=0
                reg=data['binary_manifest'];config_path=root/reg['config']['path']
                config_path.write_text(json.dumps(data['config']))
                reg['config']['sha256']=hashlib.sha256(config_path.read_bytes()).hexdigest()
                receiver='alias.pcapng' if hardlink else 'before.pcapng'
                if hardlink:os.link(root/'before.pcapng',root/receiver)
                pair=json.loads((root/'pair.json').read_text());pair['receiver']['path']=receiver
                (root/'pair.json').write_text(json.dumps(pair))
                check=json.loads((root/'seqcheck.json').read_text())
                check['inputs']['receiver'].update(path=receiver,sha256=hashlib.sha256((root/receiver).read_bytes()).hexdigest())
                (root/'seqcheck.json').write_text(json.dumps(check))
                registration=reg['verification']['seqchecks'][0];registration['receiver_path']=receiver
                registration['sha256']=hashlib.sha256((root/'seqcheck.json').read_bytes()).hexdigest()
                (root/'binary.json').write_text(json.dumps(reg))
                self.assertEqual(report_one(root/'binary.json',root/'report','demo','software-only'),2)
                self.assertEqual(json.loads((root/'report/report.json').read_text())['reason_codes'],['PCAPNG_SOURCE'])

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
