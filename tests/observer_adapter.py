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
    def test_physical_v2_preserves_scope_and_nic_counter_provenance(self):
        rows = self.fixture_v2()
        rows[0].update(capture_scope='physical', identity_policy=0)
        for row in rows[1:-1]:
            for item in row['items']:
                item[16] = 1
        resources = [dict(rows[1], kind=7, producer=4, items=[], port=port,
                          resources=[phase,64,64,1,phase,0,0,0,0,0])
                     for phase in (0,1) for port in (7,13)]
        rows = [rows[0], *resources[:2], *rows[1:-1], *resources[2:], rows[-1]]
        counts = [0]*7
        for order, row in enumerate(rows[1:-1]):
            counts[row['producer']] += 1
            row.update(order=order, producer_seq=counts[row['producer']])
        rows[-1]['producers'][4].update(attempted=4, emitted=4, high_water=4)
        result, streams = self.normalize_v2(rows)
        self.assertTrue(result['complete'], result)
        self.assertEqual(result['scope'], 'physical')
        self.assertFalse(result['g2_qualified'])
        nic = [descriptor for row in streams[2] if row['kind'] == 'counter'
               for key, descriptor in row['descriptors'].items() if key.startswith('nic_')]
        self.assertTrue(nic)
        self.assertTrue(all(row['source'] == 'physical-pmd' for row in nic))
        self.assertTrue(all(row['semantics'] == 'overlapping_diagnostic' for row in nic))
        for header in (dict(rows[0], capture_scope='software-only'),
                       dict(rows[0], identity_policy=1)):
            result, _ = self.normalize_v2([header, *rows[1:]])
            self.assertFalse(result['complete'])
        legacy = [json.loads(row) for row in self.fixture().splitlines()]
        legacy[0]['capture_scope'] = 'physical'
        result, _ = self.normalize('\n'.join(map(json.dumps, legacy))+'\n')
        self.assertFalse(result['complete'])

    def test_v2_terminal_accepts_the_correct_stage_for_both_owners(self):
        for owner in (0,1):
            for reason in range(9):
                actors=(0 if owner==0 else 5,1+owner) if reason==5 else (
                    (3 if owner==0 else 6,) if reason in (6,7) else (1+owner,))
                for producer in actors:
                    rows=self.fixture_v2()
                    rows.insert(2+2*owner,dict(rows[1+2*owner],kind=2,producer=producer,at_ns=150,reason=reason))
                    counts=[0]*7
                    for order,row in enumerate(rows[1:-1]):
                        counts[row['producer']]+=1
                        row.update(order=order,producer_seq=counts[row['producer']])
                    for p,count in enumerate(counts):
                        rows[-1]['producers'][p].update(attempted=count,emitted=count,high_water=count)
                    with self.subTest(owner=owner,reason=reason,producer=producer):
                        result,_=self.normalize_v2(rows)
                        self.assertTrue(result['complete'],result)

    def test_v2_terminal_reason_rejects_an_actor_from_the_wrong_stage(self):
        for reason in range(9):
            rows=self.fixture_v2()
            producer=1 if reason in (6,7) else 3
            row=dict(rows[1],kind=2,producer=producer,producer_seq=1,order=4,at_ns=150,reason=reason)
            rows.insert(-1,row)
            rows[-1]['producers'][producer].update(attempted=1,emitted=1,high_water=1)
            with self.subTest(reason=reason):
                result,streams=self.normalize_v2(rows)
                self.assertFalse(result['complete'])
                self.assertTrue(all(not stream[-1]['complete'] for stream in streams))

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

    def normalize(self, source, ingress_port=0):
        outputs = [io.StringIO() for _ in range(3)]
        adapter = NativeAdapter(io.StringIO(source), *outputs, run='demo',
                                lines=[dict(name='cable', ingress_port=ingress_port, egress_port=1-ingress_port, vlan_id=100)])
        result = adapter.process()
        return result, [[json.loads(r) for r in output.getvalue().splitlines()] for output in outputs]

    def single_direction(self, *, inactive_count=0, inactive_packet=False, dropped=0):
        rows = [json.loads(row) for row in self.fixture().splitlines()]
        extra = dict(rows[2], port=1, actual_selection_seq=inactive_count)
        attempt = dict(rows[2], producer=3, kind=6, producer_seq=1, port=1,
                       snapshot=[[0, 1, (1 << 64)-1, True],
                                 [1, inactive_count, (1 << 64)-1, True]])
        resources = [dict(rows[2], kind=7, producer=4, port=port, resources=[phase, 10, 10, 0]+[0]*6)
                     for port in (0, 1) for phase in (0, 1)]
        events = [rows[1], rows[2], extra, attempt, *resources]
        if inactive_packet:
            packet = dict(rows[1], items=[list(rows[1]['items'][0])])
            packet['items'][0][11] = 1
            events.insert(2, packet)
        counts = [0]*5
        for order, row in enumerate(events):
            counts[row['producer']] += 1
            row.update(order=order, producer_seq=counts[row['producer']])
        for producer, count in enumerate(counts):
            rows[-1]['producers'][producer].update(attempted=count, emitted=count, high_water=count)
        rows[-1]['producers'][0].update(attempted=counts[0]+dropped, dropped=dropped)
        return '\n'.join(map(json.dumps, [rows[0], *events, rows[-1]]))+'\n'

    def test_single_direction_projects_empty_physical_port_to_registered_ingress(self):
        result, streams = self.normalize(self.single_direction())
        self.assertTrue(result['complete'], result)
        ingress = [row for row in streams[0] if row['kind'] in ('packet', 'end')]
        self.assertEqual([row['ingress_port'] for row in ingress], [0, 0])
        attempt = next(row for row in streams[1] if row['kind'] == 'attempt')
        self.assertEqual(attempt['seen_next_ingress_seq'], {'0': 1})
        self.assertEqual(set(attempt['frontiers']), {'0'})
        self.assertEqual({row['scope_id'] for row in streams[2] if row.get('scope') == 'port'}, {'0'})
        self.assertEqual({row['scope_id'] for row in streams[2] if row.get('scope') == 'pool'}, {'0', '1'})
        native = next(row for row in streams[2]
                      if row.get('scope_id') == 'native-producer-0' and row.get('phase') == 'end')
        self.assertEqual(native['values']['records_emitted'], 3)

    def test_inactive_port_evidence_cannot_hide_packets_or_capture_loss(self):
        for changes in ({'inactive_count': 1}, {'inactive_packet': True}, {'dropped': 1}):
            with self.subTest(changes=changes):
                result, streams = self.normalize(self.single_direction(**changes))
                self.assertFalse(result['complete'])
                self.assertTrue(all(not stream[-1]['complete'] for stream in streams))

    def test_inactive_snapshot_cannot_hide_nonzero_or_duplicate_prefixes(self):
        for snapshot in ([[0, 1, (1 << 64)-1, True], [1, 1, 0, False]],
                         [[1, 0, 0, False], [1, 0, 0, False]]):
            rows = [json.loads(row) for row in self.single_direction().splitlines()]
            next(row for row in rows if row.get('kind') == 6)['snapshot'] = snapshot
            with self.subTest(snapshot=snapshot):
                result, streams = self.normalize('\n'.join(map(json.dumps, rows))+'\n')
                self.assertFalse(result['complete'])
                self.assertTrue(all(not stream[-1]['complete'] for stream in streams))

    def test_single_direction_both_owners_and_native_layouts_reach_reference(self):
        from tools.g2.reference import Reference
        from tools.g2.tests.test_reference import config
        for owner in (0, 1):
            for layout in ('v1', 'shared', 'per-direction'):
                rows = [json.loads(row) for row in self.single_direction().splitlines()]
                if owner:
                    for row in rows[1:-1]:
                        row['port'] = 1-row['port']
                        for item in row['items']:
                            item[11] = 1-item[11]
                        for entry in row['snapshot']:
                            entry[0] = 1-entry[0]
                if layout != 'v1':
                    rows[0].update(schema='hng-native-v2', ports=[0, 1], layout=layout)
                    rows[-1]['producers'] += [dict(rows[-1]['producers'][1]) for _ in range(2)]
                    counts = [0]*7
                    for row in rows[1:-1]:
                        if row['kind'] in (0, 4):
                            row['producer'] = 5 if layout == 'per-direction' and row['port'] == 1 else 0
                        elif row['kind'] == 6:
                            row['producer'] = 6 if layout == 'per-direction' and owner else 3
                        counts[row['producer']] += 1
                        row['producer_seq'] = counts[row['producer']]
                    for producer, count in enumerate(counts):
                        rows[-1]['producers'][producer].update(attempted=count, emitted=count, high_water=count)
                with self.subTest(owner=owner, layout=layout):
                    result, streams = self.normalize('\n'.join(map(json.dumps, rows))+'\n', owner)
                    self.assertTrue(result['complete'], result)
                    cfg = config()
                    cfg['lines'][0].update(ingress_port=owner, egress_port=1-owner)
                    reference = Reference(cfg, 'demo')
                    for row in streams[0][1:-1]:
                        reference.consume(row)
                    self.assertEqual(reference.finish()['end_counts'], {str(owner): 1})
                    attempt = next(row for row in streams[1] if row['kind'] == 'attempt')
                    self.assertEqual(attempt['consumed_dispatch'], {'handoff-0': 0, 'handoff-1': 0})

    def test_discarded_port_cannot_hide_marker_items(self):
        rows = [json.loads(row) for row in self.single_direction().splitlines()]
        next(row for row in rows if row.get('kind') == 4 and row['port'] == 1)['items'] = rows[1]['items']
        result, streams = self.normalize('\n'.join(map(json.dumps, rows))+'\n')
        self.assertFalse(result['complete'])
        self.assertTrue(all(not stream[-1]['complete'] for stream in streams))

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
