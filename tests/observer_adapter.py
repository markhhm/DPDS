# SPDX-License-Identifier: Apache-2.0
"""Native boundary integrity tests: loss cannot become a normalized clean stream."""
import io
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools.g2.native_adapter import NativeAdapter
from tools.g2.protocol import StreamReader, StreamError


class NativeBoundary(unittest.TestCase):
    def fixture_v2(self):
        old = [json.loads(line) for line in self.fixture().splitlines()]
        header = dict(old[0], schema='hng-native-v2', layout='per-direction', ports=[7, 13])
        events = []
        for owner, (producer, port) in enumerate(((0, 7), (5, 13))):
            packet = dict(old[1], producer=producer, order=len(events))
            item = list(packet['items'][0])
            item[11:13] = [port, owner]
            packet['items'] = [item]
            end = dict(old[2], producer=producer, order=len(events)+1, port=port)
            events += [packet, end]
        empty = old[-1]['producers'][1]
        health = dict(old[-1], producers=[dict(old[-1]['producers'][0] if p in (0,5) else empty) for p in range(7)])
        return [header, *events, health]

    def normalize_v2(self, rows):
        outputs = [io.StringIO() for _ in range(3)]
        adapter = NativeAdapter(io.StringIO('\n'.join(map(json.dumps, rows))+'\n'), *outputs, run='demo',
            lines=[dict(name='cable', ingress_port=7, egress_port=13, vlan_id=100),
                   dict(name='reverse', ingress_port=13, egress_port=7, vlan_id=100)])
        result = adapter.process()
        return result, [[json.loads(r) for r in output.getvalue().splitlines()] for output in outputs]

    def test_v2_two_rx_actors_preserve_ports_health_and_provenance(self):
        result, streams = self.normalize_v2(self.fixture_v2())
        self.assertTrue(result['complete'], result)
        packets = [r for r in streams[0] if r['kind']=='packet']
        self.assertEqual([r['ingress_port'] for r in packets], [7,13])
        self.assertEqual([r['extensions']['native_observer_v2']['producer'] for r in packets], [0,5])
        native = next(r for r in streams[2] if r.get('scope_id')=='native' and r.get('phase')=='end')
        self.assertEqual(native['values']['queue_capacity'], 7*16)

    def test_v2_rejects_wrong_rx_actor_and_duplicate_registered_ports(self):
        for change in ('actor','ports'):
            rows = self.fixture_v2()
            if change=='actor': rows[3]['producer'] = 6
            else: rows[0]['ports'] = [7,7]
            result, _ = self.normalize_v2(rows)
            self.assertFalse(result['complete'], result)

    def test_v2_rx1_loss_is_not_hidden_in_logical_ingress_health(self):
        rows=self.fixture_v2()
        rows[-1]['producers'][5].update(attempted=3,dropped=1)
        rows[-1]['complete']=False
        result,streams=self.normalize_v2(rows)
        self.assertFalse(result['complete'])
        ingress=next(r for r in streams[2] if r.get('scope_id')=='native-ingress' and r.get('phase')=='end')
        self.assertIsNone(ingress['values']['records_dropped'])
        self.assertTrue(all(not stream[-1]['complete'] for stream in streams))

    def test_shared_tx_layout_accepts_second_scheduler_offer_before_handoff_assignment(self):
        rows=self.fixture_v2()
        rows[0]['layout']='shared'
        rows[3].update(producer=0,producer_seq=3)
        rows[4].update(producer=0,producer_seq=4)
        offer=dict(rows[3],producer=2,producer_seq=1,order=4,kind=1,at_ns=150)
        rows.insert(-1,offer)
        rows[-1]['producers'][0].update(attempted=4,emitted=4,high_water=4)
        rows[-1]['producers'][5]=dict(rows[-1]['producers'][4])
        rows[-1]['producers'][2].update(attempted=1,emitted=1,high_water=1)
        result,_=self.normalize_v2(rows)
        self.assertTrue(result['complete'],result)

    def fixture(self, *, dropped=0, sealed=True):
        item = [7, 0, 0, 100, 0, 0, 0, 0, 76, 0, 0, 0, 0, 100, 1, 0, 2, 2, 0, True, False, 0]
        row = dict(producer=0, producer_seq=1, order=0, kind=0, at_ns=0,
                   gate_ns=0, submit_before_ns=0, submit_after_ns=0, actual_selection_seq=0,
                   attempt_id=0, consumed_dispatch=[0, 0], first_handoff_seq=0, handoff_queue=0,
                   port=0, accepted_count=0, reason=0, snapshot=[], items=[item], resources=[0]*10)
        end = dict(row, producer_seq=2, order=1, kind=4, items=[], actual_selection_seq=1)
        header = dict(schema='hng-native-v1', identity_policy=1, probe_port=9000, capacity=16, record_bytes=2048)
        health = dict(kind='health', complete=not dropped, abort_reasons=0, producers=[dict(attempted=2+dropped, emitted=2,
                      dropped=dropped, capture_drops=0, high_water=2, identity_errors=0, malformed_records=0)]+[
                      dict(attempted=0, emitted=0, dropped=0, capture_drops=0, high_water=0, identity_errors=0, malformed_records=0)
                      for _ in range(4)])
        return '\n'.join(json.dumps(r) for r in [header, row, end]+([health] if sealed else []))+'\n'

    def normalize(self, source):
        outputs = [io.StringIO() for _ in range(3)]
        adapter = NativeAdapter(io.StringIO(source), *outputs, run='demo',
                                lines=[dict(name='cable', ingress_port=0, egress_port=1, vlan_id=100)])
        result = adapter.process()
        return result, [[json.loads(r) for r in output.getvalue().splitlines()] for output in outputs]

    def test_exact_input_identity_and_standard_seals(self):
        result, streams = self.normalize(self.fixture())
        self.assertTrue(result['complete'])
        packet = streams[0][1]
        self.assertEqual(packet['external_identity'], 'software-l2-0:7')
        self.assertEqual(packet['selection_seq'], 0)
        self.assertIsNone(packet['selected_late'])
        self.assertEqual(streams[0][-2]['next_ingress_seq'], 1)
        self.assertTrue(all(s[-1]['complete'] for s in streams))
        self.assertTrue(any(r.get('phase') == 'start' for r in streams[2]))
        self.assertTrue(any(r.get('phase') == 'end' for r in streams[2]))

    def test_logical_stream_health_counts_match_normalized_records(self):
        result, streams = self.normalize(self.fixture())
        counters = {r['scope_id']: r for r in streams[2] if r.get('scope') == 'observer' and r.get('phase') == 'end'}
        for stream in streams[:2]:
            stream_id = stream[0]['stream_id']
            self.assertIn(stream_id, counters)
            self.assertEqual(counters[stream_id]['values']['records_emitted'], len(stream)-2)
            self.assertEqual(counters[stream_id]['values']['records_attempted'], len(stream)-2)

    def test_trailing_native_loss_remains_incomplete_without_sequence_gap(self):
        result, streams = self.normalize(self.fixture(dropped=1))
        self.assertFalse(result['complete'])
        self.assertTrue(all(not s[-1]['complete'] for s in streams))
        self.assertTrue(all(s[-1]['dropped_records'] == 1 for s in streams))
        logical = [r for r in streams[2] if r.get('scope_id') in ('native-ingress', 'native-host') and r.get('phase') == 'end']
        self.assertTrue(all(r['values']['records_attempted'] is None for r in logical))
        native = next(r for r in streams[2] if r.get('scope_id') == 'native-producer-0' and r.get('phase') == 'end')
        self.assertEqual(native['values']['records_dropped'], 1)

    def test_missing_native_health_is_incomplete(self):
        result, streams = self.normalize(self.fixture(sealed=False))
        self.assertFalse(result['complete'])
        self.assertTrue(all(not s[-1]['complete'] for s in streams))

    def test_sequence_gap_is_not_repaired_by_normalization(self):
        rows = [json.loads(r) for r in self.fixture().splitlines()]
        rows[2]['producer_seq'] = 3
        result, streams = self.normalize('\n'.join(map(json.dumps, rows))+'\n')
        self.assertFalse(result['complete'])
        self.assertTrue(any(r['kind'] == 'gap' for s in streams for r in s))

    def test_bad_scalar_and_duplicate_raw_key_are_incomplete(self):
        for text in (self.fixture().replace('"producer_seq": 1', '"producer_seq": true'),
                     self.fixture().replace('"producer_seq": 1', '"producer_seq": 1, "producer_seq": 1')):
            result, streams = self.normalize(text)
            self.assertFalse(result['complete'])
            self.assertTrue(all(not s[-1]['complete'] for s in streams))


if __name__ == '__main__':
    unittest.main()
