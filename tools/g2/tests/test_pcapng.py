# SPDX-License-Identifier: Apache-2.0
"""Synthetic same-clock pcapng feeds the real sealed-stream reporter."""
import json
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import unittest

from tools.g2.probe import encode_probe
from tools.g2.protocol import StreamReader, StreamWriter
from tools.g2.tests.test_bundle import fixture


FLOW = 'c0000201:c6336402:1111:9000'


def frame(sequence, ip_options=0):
    payload = encode_probe(sequence, False)[:100-ip_options]
    ip = bytes([0x40+(20+ip_options)//4])+bytes.fromhex('0000800000000040110000c0000201c6336402')+bytes(ip_options)
    udp = struct.pack('!HHHH', 1111, 9000, 8+len(payload), 0)
    return bytes.fromhex('ffffffffffff020000000001810000640800') + ip + udp + payload


def pcapng(path, times, *, endian='<', drop=0, os_drop=0, epb_drop=0, resolution=9,
           start=0, end=10**9, offset=0, snaplen=65535, caplen=None, wirelen=None, ip_options=0):
    def block(kind, body):
        size = len(body)+12
        return struct.pack(endian+'II', kind, size)+body+struct.pack(endian+'I', size)
    def option(code, value):
        return struct.pack(endian+'HH', code, len(value))+value+bytes((-len(value))%4)
    def options(values):
        return b''.join(option(*item) for item in values)+bytes(4)
    data = block(0x0a0d0d0a, struct.pack(endian+'IHHq', 0x1a2b3c4d, 1, 0, -1))
    data += block(1, struct.pack(endian+'HHI', 1, 0, snaplen)+options([
        (9, bytes([resolution])),(14,struct.pack(endian+'q',offset))]))
    for seq, time in enumerate(times):
        packet = frame(seq, ip_options)
        original_length = len(packet) if wirelen is None else wirelen
        packet = packet[:caplen]
        body = struct.pack(endian+'IIIII', 0, time>>32, time & 0xffffffff, len(packet), original_length)
        body += packet+bytes((-len(packet))%4)
        data += block(6, body+options([(4, struct.pack(endian+'Q', epb_drop))]))
    counts = [(2, struct.pack(endian+'II', start>>32, start & 0xffffffff)),
              (3, struct.pack(endian+'II', end>>32, end & 0xffffffff)),
              (5, struct.pack(endian+'Q', drop)), (7, struct.pack(endian+'Q', os_drop)),
              (8, struct.pack(endian+'Q', len(times)))]
    data += block(5, struct.pack(endian+'III', 0, end>>32, end & 0xffffffff)+options(counts))
    path.write_bytes(data)


def setup(root, endian='<', run='demo'):
    source=root/'source'; source.mkdir()
    _, registration, sources, calibration=fixture(source, length=146, flow_id=FLOW, selected_late=0,run=run)
    calibration['clocks'].append(dict(clock_id='capture',kind='fake',unit='ns',width_bits=64,
                                     device_token='synthetic-capture',mode='synthetic'))
    calibration['mappings']=[dict(mapping_id='ns',raw_clock_id='capture',target_clock_id='obs',
        raw_origin=0,target_origin_ns=0,scale_num=1,scale_den=1,valid_from_raw=0,
        valid_to_raw=10**12,residual_bound_ns=0)]
    calibration['mappings'].append(dict(calibration['mappings'][0],mapping_id='engine-ns',raw_clock_id='engine'))
    calibration['pairs'][0].update(ingress_clock_id='capture',egress_clock_id='capture',offset_ns=0)
    (source/'calibration.json').write_text(json.dumps(calibration))
    expected=[row for row in StreamReader(sources['expected'],role='expected') if row['kind']=='expected']
    pcapng(root/'before.pcapng',[row['arrival_ns'] for row in expected],endian=endian)
    pcapng(root/'receiver.pcapng',[row['release_ns']+3 for row in expected],endian=endian)
    pair=dict(schema='hng-pcapng-pair-v1',manifest='source/manifest.json',clock_id='capture',mapping_id='ns',
        uncertainty_ns=0,start_ns=0,end_ns=10**9,
        before=dict(path='before.pcapng',interfaces={'0':0}),
        receiver=dict(path='receiver.pcapng',interfaces={'0':1}))
    (root/'pair.json').write_text(json.dumps(pair))
    return expected


class PcapngTests(unittest.TestCase):
    def invoke(self, root):
        return subprocess.run([sys.executable,'-m','tools.g2.pcapng',str(root/'pair.json'),
                               '--out',str(root/'out')],capture_output=True,text=True)

    def assert_healthy_capture_with_cost_warning(self, root, result):
        self.assertEqual(result.returncode, 2, result.stderr)
        report = json.loads((root/'out/reports/demo/report.json').read_text())
        self.assertEqual(report['measurement_status'], 'INCONCLUSIVE', report['reason_codes'])
        self.assertEqual(report['observer_cost']['status'], 'INCONCLUSIVE')
        self.assertIn(dict(observer='before', code='observer_cost_queue_full'), report['observer_cost']['warnings'])
        health = json.loads((root/'out/conversion.json').read_text())['captures']
        self.assertTrue(all(row['complete'] for row in health.values()), health)
        return report

    def test_sampled_seqcheck_paths_follow_relocated_registration(self):
        from tools.g2.pcapng import prepare_pair
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);setup(root)
            path=root/'source/manifest.json';manifest=json.loads(path.read_text())
            (root/'source/seqcheck.json').write_text('{}')
            manifest['verification']=dict(mode='sampled',seqchecks=[dict(path='seqcheck.json',sha256='a'*64,
                ingress_port=0,egress_port=1,ingress_path='../before.pcapng',receiver_path='../receiver.pcapng')])
            path.write_text(json.dumps(manifest))
            converted=prepare_pair(root/'pair.json',root/'converted')
            item=json.loads(converted.read_text())['verification']['seqchecks'][0]
            self.assertEqual((converted.parent/item['path']).resolve(),root/'source/seqcheck.json')
            self.assertEqual((converted.parent/item['ingress_path']).resolve(),root/'before.pcapng')

    def test_nanosecond_pair_uses_real_reporter_and_preserves_identities(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary)
            setup(root)
            result=self.invoke(root)
            report = self.assert_healthy_capture_with_cost_warning(root, result)
            self.assertFalse(report['g2_qualified'])
            self.assertEqual(report['rows'][0]['timing']['main']['n'],400)
            before=list(StreamReader(root/'out/bundle/before.jsonl',role='before'))
            self.assertEqual(before[0]['external_identity'],FLOW+':0')
            self.assertEqual(before[0]['timestamp']['raw'],100)
            self.assertTrue((root/'out/reports/demo/results.csv').exists())

    def test_snaplen_preserves_probe_identity_and_original_wire_length(self):
        for endian, retained, ip_options in [('<', 128, 0), ('>', 59, 0), ('<', 99, 40)]:
            with self.subTest(endian=endian, retained=retained, ip_options=ip_options), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary); expected = setup(root, endian=endian)
                for role, key in [('before', 'arrival_ns'), ('receiver', 'release_ns')]:
                    pcapng(root/f'{role}.pcapng', [row[key]+(3 if role=='receiver' else 0) for row in expected],
                           endian=endian, snaplen=retained, caplen=retained, ip_options=ip_options)
                result = self.invoke(root)
                self.assert_healthy_capture_with_cost_warning(root, result)
                for role in ('before', 'receiver'):
                    rows = list(StreamReader(root/f'out/bundle/{role}.jsonl', role=role))
                    self.assertEqual([row['external_identity'] for row in rows], [FLOW+f':{i}' for i in range(400)])
                    self.assertEqual({row['length'] for row in rows}, {146})

    def test_missing_probe_prefix_and_inconsistent_lengths_are_inconclusive(self):
        for caplen, snaplen, wirelen in [(58, 58, 146), (18, 18, 146), (146, 65535, 145),
                                        (59, 59, 145), (128, 127, 146)]:
            with self.subTest(caplen=caplen, snaplen=snaplen, wirelen=wirelen), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary); expected = setup(root)
                pcapng(root/'receiver.pcapng', [row['release_ns']+3 for row in expected],
                       snaplen=snaplen, caplen=caplen, wirelen=wirelen)
                result = self.invoke(root)
                self.assertEqual(result.returncode, 2, result.stderr)
                report = json.loads((root/'out/reports/demo/report.json').read_text())
                self.assertEqual(report['measurement_status'], 'INCONCLUSIVE')
                health = json.loads((root/'out/conversion.json').read_text())['captures']['receiver']
                self.assertFalse(health['complete'])

    def test_counter_rewrite_cannot_launder_a_source_hash_mismatch(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary); setup(root)
            path=root/'source/counters.jsonl'
            rows=list(StreamReader(path,role='counters'))
            with path.open('w') as output:
                writer=StreamWriter(output,'demo','counters','counters','engine',producer='changed-after-registration')
                for row in rows:
                    writer.emit('counter',**{key:value for key,value in row.items()
                                             if key not in ('kind','run','stream_id','seq')})
                writer.finish()
            result=self.invoke(root)
            self.assertEqual(result.returncode,2,result.stdout)
            self.assertIn('hash',result.stderr.lower())

    def test_bad_clock_capture_drop_and_truncation_remain_inconclusive(self):
        for fault in ('clock','isb_drop','epb_drop','truncated','missing_final_statistics'):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as temporary:
                root=Path(temporary); expected=setup(root)
                times=[row['release_ns']+3 for row in expected]
                if fault=='clock': times[20]=times[19]-1
                path=root/'receiver.pcapng'
                pcapng(path,times,drop=int(fault=='isb_drop'),epb_drop=int(fault=='epb_drop'))
                if fault=='truncated': path.write_bytes(path.read_bytes()[:-1])
                if fault=='missing_final_statistics':
                    data=path.read_bytes(); size=struct.unpack('<I',data[-4:])[0]
                    path.write_bytes(data[:-size])
                result=self.invoke(root)
                self.assertEqual(result.returncode,2,result.stderr)
                report=json.loads((root/'out/reports/demo/report.json').read_text())
                self.assertEqual(report['measurement_status'],'INCONCLUSIVE',report['reason_codes'])
                self.assertFalse(report['g2_qualified'])
                conversion=json.loads((root/'out/conversion.json').read_text())
                self.assertFalse(conversion['captures']['receiver']['complete'])
                self.assertIsNone(conversion['captures']['receiver']['dropped'])

    def test_big_endian_complete_capture_retains_missing_delivery_with_cost_warning(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary); expected=setup(root,endian='>')
            pcapng(root/'receiver.pcapng',[row['release_ns']+3 for row in expected[:-1]],endian='>')
            result=self.invoke(root)
            report = self.assert_healthy_capture_with_cost_warning(root, result)
            self.assertIn('missing_delivery',report['rows'][0]['observed_failures'])
            conversion=json.loads((root/'out/conversion.json').read_text())
            self.assertTrue(conversion['captures']['receiver']['complete'])

    def test_large_nanosecond_epochs_and_signed_offsets_remain_exact(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary); expected=setup(root)
            epoch=2**54
            for role,key in [('before','arrival_ns'),('receiver','release_ns')]:
                pcapng(root/f'{role}.pcapng',[epoch+row[key]+(3 if role=='receiver' else 0) for row in expected],
                       start=epoch,end=epoch+10**9,offset=-1,endian='>')
            pair=json.loads((root/'pair.json').read_text())
            pair.update(start_ns=epoch-10**9,end_ns=epoch)
            (root/'pair.json').write_text(json.dumps(pair))
            path=root/'source/calibration.json'; cal=json.loads(path.read_text())
            cal['mappings'][0]['valid_to_raw']=2**64-1
            cal['mappings'][1]['target_origin_ns']=epoch-10**9
            path.write_text(json.dumps(cal))
            result=self.invoke(root)
            self.assert_healthy_capture_with_cost_warning(root, result)
            rows=list(StreamReader(root/'out/bundle/before.jsonl',role='before'))
            self.assertEqual(rows[0]['timestamp']['raw'],epoch+100-10**9)

    def test_unsupported_resolution_missing_file_and_corrupt_trailer_are_inconclusive(self):
        for fault in ('resolution','missing','trailer','os_drop','drop_unknown'):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as temporary:
                root=Path(temporary); expected=setup(root)
                path=root/'receiver.pcapng'
                pcapng(path,[row['release_ns']+3 for row in expected],resolution=6 if fault=='resolution' else 9,
                       os_drop=int(fault=='os_drop'),epb_drop=2**64-1 if fault=='drop_unknown' else 0)
                if fault=='missing': path.unlink()
                if fault=='trailer':
                    data=path.read_bytes(); path.write_bytes(data[:-4]+struct.pack('<I',12))
                result=self.invoke(root)
                self.assertEqual(result.returncode,2,result.stderr)
                report=json.loads((root/'out/reports/demo/report.json').read_text())
                self.assertEqual(report['measurement_status'],'INCONCLUSIVE',report['reason_codes'])

    def test_configuration_errors_preserve_inputs_and_existing_output(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary); setup(root)
            pair=json.loads((root/'pair.json').read_text())
            for field,value in [('before',dict(path='before.pcapng',interfaces={'0':999})),
                                ('receiver',dict(path='before.pcapng',interfaces={'0':1})),
                                ('end_ns',0)]:
                bad=dict(pair); bad[field]=value
                (root/'pair.json').write_text(json.dumps(bad))
                result=self.invoke(root)
                self.assertEqual(result.returncode,2,result.stderr)
                self.assertFalse((root/'out').exists())
            (root/'pair.json').write_text(json.dumps(pair))
            output=root/'out'; output.mkdir(); (output/'sentinel').write_text('keep')
            result=self.invoke(root)
            self.assertEqual(result.returncode,2,result.stderr)
            self.assertEqual((output/'sentinel').read_text(),'keep')

    def test_hardlink_capture_alias_is_rejected_before_output(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary); setup(root)
            (root/'receiver.pcapng').unlink()
            (root/'receiver.pcapng').hardlink_to(root/'before.pcapng')
            result=self.invoke(root)
            self.assertEqual(result.returncode,2,result.stdout)
            self.assertFalse((root/'out').exists())

    def test_statistics_reset_or_capture_start_after_first_packet_is_inconclusive(self):
        for fault in ('reset','late_start'):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as temporary:
                root=Path(temporary); expected=setup(root)
                path=root/'receiver.pcapng'
                if fault=='reset':
                    data=path.read_bytes(); size=struct.unpack('<I',data[-4:])[0]
                    final=data[-size:]
                    earlier=final.replace(struct.pack('<HHQ',8,8,400),struct.pack('<HHQ',8,8,500))
                    self.assertNotEqual(earlier,final)
                    path.write_bytes(data[:-size]+earlier+final)
                else:
                    pcapng(path,[row['release_ns']+3 for row in expected],start=1000)
                    pair=json.loads((root/'pair.json').read_text()); pair['start_ns']=1000
                    (root/'pair.json').write_text(json.dumps(pair))
                result=self.invoke(root)
                self.assertEqual(result.returncode,2,result.stdout)
                report=json.loads((root/'out/reports/demo/report.json').read_text())
                self.assertEqual(report['measurement_status'],'INCONCLUSIVE')

    def test_early_capture_end_or_missing_clock_anchor_cannot_certify_a_phase(self):
        for fault in ('short_phase','early_missing_delivery','missing_engine_anchor','capture_map_expires'):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as temporary:
                root=Path(temporary); expected=setup(root)
                if fault in ('missing_engine_anchor','capture_map_expires'):
                    path=root/'source/calibration.json'; cal=json.loads(path.read_text())
                    if fault=='missing_engine_anchor': cal['mappings']=cal['mappings'][:1]
                    else: cal['mappings'][0]['valid_to_raw']=10**6
                    path.write_text(json.dumps(cal))
                else:
                    times=[row['release_ns']+3 for row in expected]
                    if fault=='early_missing_delivery': times.pop()
                    end=times[-1]+1
                    pcapng(root/'receiver.pcapng',times,end=end)
                    pair=json.loads((root/'pair.json').read_text()); pair['end_ns']=end
                    (root/'pair.json').write_text(json.dumps(pair))
                result=self.invoke(root)
                self.assertEqual(result.returncode,2,result.stdout)
                report=json.loads((root/'out/reports/demo/report.json').read_text())
                self.assertEqual(report['measurement_status'],'INCONCLUSIVE')
