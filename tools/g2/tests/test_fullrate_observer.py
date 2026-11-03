# SPDX-License-Identifier: Apache-2.0
"""Short full-rate evidence is explicitly DUT-timed, with one captured egress."""
import hashlib
import io
import json
from pathlib import Path
import struct
import tempfile
import unittest

from tools.g2.tests.binary_sample_fixture import END, _pcapng, make_case

ROOT = Path(__file__).resolve().parents[3]


def checksum(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def source_case(root, **options):
    make_case(root, **options)
    clock = root / 'clock-bound.json'
    clock.write_text(json.dumps(dict(schema='hng-native-clock-bound-v1', run='demo',
        start_ns=0, end_ns=END + 1, pair_error_ns=0)))
    source = dict(schema='hng-fullrate-observer-v1',
        registration=dict(path='binary.json', sha256=checksum(root/'binary.json')),
        native=dict(path='native.hngb', sha256=checksum(root/'native.hngb')),
        receiver=dict(path='receiver.pcapng', sha256=checksum(root/'receiver.pcapng'), ingress_port=0),
        clock_bound=dict(path='clock-bound.json', sha256=checksum(clock)))
    (root/'fullrate.json').write_text(json.dumps(source))
    return source


def save_source(root, source):
    for name in ('native', 'receiver', 'registration'):
        source[name]['sha256'] = checksum(root/source[name]['path'])
    (root/'fullrate.json').write_text(json.dumps(source))


def rewrite_native(root, change, seal_change=None):
    from tools.g2.native_binary import decode
    from tools.g2.tests.test_native_binary import MAGIC, entry, frame, line
    output = io.StringIO()
    with (root/'native.hngb').open('rb') as stream:
        decode(stream, output)
    rows = [json.loads(raw) for raw in output.getvalue().splitlines()]
    events = change(rows[1:-1])
    counts = [0]*7
    parts = [MAGIC, frame(1, line(rows[0]), 0)]
    for order, row in enumerate(events):
        counts[row['producer']] += 1
        common = [row['at_ns'],row['gate_ns'],row['submit_before_ns'],row['submit_after_ns'],*row['resources'],
            row['actual_selection_seq'],row['attempt_id'],*row['consumed_dispatch'],row['first_handoff_seq'],
            row['handoff_queue'],row['port'],row['accepted_count']]
        raw = entry(producer=row['producer'],kind=row['kind'],reason=row['reason'],seq=counts[row['producer']],
            order=order,common=common,snapshot=row['snapshot'],items=row['items'])
        parts.append(frame(2, raw, order+1))
    for producer,count in zip(rows[-1]['producers'],counts):
        producer.update(attempted=count, emitted=count)
    if seal_change:seal_change(rows[-1])
    parts.append(frame(3,line(rows[-1]),len(parts)-1))
    (root/'native.hngb').write_bytes(b''.join(parts))


class FullrateObserverTests(unittest.TestCase):
    def run_case(self, root):
        from tools.g2.fullrate_observer import run
        return run(root/'fullrate.json',root/'out',seqcheck=ROOT/'build/seqcheck')

    def test_fixed_stress_and_trace_reuse_sampled_model_without_external_timing(self):
        for options in ({},dict(stress=True,q_bytes=32768),dict(trace=True)):
            with self.subTest(options=options),tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp);source_case(root,count=500,**options)
                result=self.run_case(root)
                self.assertEqual(result['status'],'PASS',result)
                self.assertEqual(result['schema'],'hng-fullrate-observer-report-v1')
                self.assertFalse(result['g2_qualified'])
                self.assertFalse(result['coverage']['os4_independent_owd'])
                self.assertEqual(result['coverage']['external_egress_ports'],[1])
                self.assertEqual(result['coverage']['ingress_reference'],'native-rx; not an independent capture')
                self.assertIsNone(result['timing_bound']['wire_owd_error_bound_ns'])
                self.assertEqual(result['sampled_reference']['sample_checked'],500)
                self.assertEqual(result['observer_cost']['status'],'PASS')
                self.assertEqual(result['seqcheck']['0']['storage']['status'],'PASS')
                self.assertEqual(result['rows'][0]['timing']['main']['absolute_error']['max_ns'],1234 if not options.get('stress') and not options.get('trace') else 0)
                self.assertTrue((root/'out/samples.jsonl').is_file())
                if options.get('trace'):
                    self.assertGreater(result['rows'][0]['counts']['clamped'],0)
                if options.get('stress'):
                    self.assertGreater(result['rows'][0]['counts']['q_drop'],0)

    def test_zero_accept_retries_and_partial_prefix_do_not_create_deliveries(self):
        import copy
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);source=source_case(root,count=500)
            def change(rows):
                result=[]
                for row in rows:
                    if row['kind']==6:
                        retry=copy.deepcopy(row);retry['accepted_count']=0
                        result.append(retry)
                        row['items'].append(copy.deepcopy(row['items'][0]))
                    result.append(row)
                return result
            rewrite_native(root,change);save_source(root,source)
            result=self.run_case(root)
            self.assertEqual(result['status'],'PASS',result)
            self.assertEqual(result['timing_bound']['accepted_sample_packets'],500)

    def test_missing_clock_bound_is_inconclusive(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);source=source_case(root,count=500)
            source['clock_bound']=None;(root/'fullrate.json').write_text(json.dumps(source))
            result=self.run_case(root)
            self.assertEqual(result['status'],'INCONCLUSIVE')
            self.assertIn('native_clock_bound_missing',result['reason_codes'])

    def test_extra_missing_and_reordered_receiver_fail(self):
        for reorder in (False,True):
            with self.subTest(reorder=reorder),tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp);source=source_case(root,count=500)
                native=[]
                from tools.g2.native_arrays import NativeArrays
                for batch in NativeArrays(root/'native.hngb').batches():
                    events=batch['events']
                    for item in batch['items']:
                        event=events[int(item['event_index'])]
                        if event['kind']==6:native.append((int(item['identity']),int(event['submit_after_ns'])))
                if reorder:
                    times=[stamp for _,stamp in native]
                    native[0],native[1]=native[1],native[0]
                    native=[(pair[0],stamp) for pair,stamp in zip(native,times)]
                else:native.pop()
                (root/'receiver.pcapng').unlink();_pcapng(root/'receiver.pcapng',native);save_source(root,source)
                result=self.run_case(root)
                self.assertEqual(result['status'],'FAIL',result)
                gate='order' if reorder else 'storage'
                self.assertEqual(result['seqcheck']['0'][gate]['status'],'FAIL')

    def test_unknown_capture_health_and_native_corruption_never_pass(self):
        for corrupt in ('native','capture'):
            with self.subTest(corrupt=corrupt),tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp);source=source_case(root,count=500)
                if corrupt=='native':
                    path=root/'native.hngb';raw=bytearray(path.read_bytes());raw[28]^=1;path.write_bytes(raw)
                else:
                    path=root/'receiver.pcapng';raw=path.read_bytes();offset=0
                    while offset<len(raw):
                        kind,size=struct.unpack_from('<II',raw,offset)
                        if kind==5:break
                        offset+=size
                    path.write_bytes(raw[:offset])
                save_source(root,source);result=self.run_case(root)
                self.assertEqual(result['status'],'INCONCLUSIVE',result)

    def test_both_native_directions_are_timed_but_only_one_egress_is_checked(self):
        from tools.g2.tests.binary_split_fixture import make_case as split_case
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);source=source_case(root/'single',count=500)
            split_case(root/'both',count=500);both=root/'both'
            (both/'clock-bound.json').write_bytes((root/'single/clock-bound.json').read_bytes())
            source['receiver']['path']='forward/receiver.pcapng'
            save_source(both,source)
            result=self.run_case(both)
            self.assertEqual(result['status'],'PASS',result)
            self.assertEqual(result['sampled_reference']['sample_checked'],1000)
            self.assertEqual(result['seqcheck']['1']['order']['status'],'INCONCLUSIVE')
            row=next(row for row in result['rows'] if row['direction']=='1')
            self.assertEqual(row['gates']['order']['status'],'NOT_CHECKED')
            self.assertFalse(row['g2_qualified'])

    def test_selected_q_mismatch_fails_and_saturated_queue_is_inconclusive(self):
        for fault in ('q','queue'):
            with self.subTest(fault=fault),tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp);source=source_case(root,count=500)
                def change(rows):
                    if fault=='q':next(row for row in rows if row['kind']==1)['actual_selection_seq']=999
                    return rows
                def seal_change(seal):
                    if fault=='queue':seal['producers'][0]['high_water']=65536
                rewrite_native(root,change,seal_change);save_source(root,source)
                result=self.run_case(root)
                self.assertEqual(result['status'],'FAIL' if fault=='q' else 'INCONCLUSIVE',result)

    def test_nonzero_submit_bracket_is_retained_and_clock_validity_is_required(self):
        for expired in (False,True):
            with self.subTest(expired=expired),tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp);source=source_case(root,count=500,stress=True)
                def change(rows):
                    for row in rows:
                        if row['kind']==6:row['submit_after_ns']+=100
                    return rows
                rewrite_native(root,change);save_source(root,source)
                if expired:
                    path=root/'clock-bound.json';clock=json.loads(path.read_text());clock['end_ns']=1
                    path.write_text(json.dumps(clock));source['clock_bound']['sha256']=checksum(path)
                    (root/'fullrate.json').write_text(json.dumps(source))
                result=self.run_case(root)
                self.assertEqual(result['status'],'INCONCLUSIVE' if expired else 'PASS',result)
                if not expired:
                    self.assertEqual(result['timing_bound']['max_submit_bracket_ns'],100)
                    self.assertEqual(result['rows'][0]['timing']['main']['absolute_error']['max_ns'],50)
                    sample=json.loads((root/'out/samples.jsonl').read_text().splitlines()[0])
                    self.assertEqual(sample['submit_after_ns']-sample['submit_before_ns'],100)

    def test_source_hash_mismatch_and_existing_output_are_rejected(self):
        from tools.g2.fullrate_observer import run
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);source=source_case(root,count=500)
            source['native']['sha256']='0'*64;(root/'fullrate.json').write_text(json.dumps(source))
            result=self.run_case(root)
            self.assertEqual(result['status'],'INCONCLUSIVE')
            with self.assertRaises(FileExistsError):run(root/'fullrate.json',root/'out')


if __name__=='__main__':unittest.main()
