# SPDX-License-Identifier: Apache-2.0
"""All-row semantics and selected-row/counter parity with the legacy adapter."""
from collections import Counter
import copy
import io
import json
from pathlib import Path
import tempfile
import struct
import unittest

from tools.g2.native_adapter import NativeAdapter, REASONS
from tools.g2.native_arrays import NativeArrays
from tools.g2.native_binary import decode
from tools.g2.protocol import StreamError
from tools.g2.tests.test_native_binary import MAGIC, entry, frame, health, line
from tools.g2.tests.test_reference import config
from tools.g2.tests.test_registration import manifest

try:
    from tools.g2.native_projection import NativeProjection
except ImportError:
    NativeProjection = None


def item(identity=1, ordinal=0, arrival=100, owner=0):
    return [identity, ordinal, ordinal, arrival, ordinal, 120, 0x0a000001, 0x0a000002,
            76, 1, 9000, owner, owner, 100, 1, owner, 1, 2, 0, True, False, 0]


def raw_event(kind, producer, items=(), **values):
    row = dict(producer=producer, producer_seq=0, order=0, kind=kind, at_ns=0, gate_ns=0,
        submit_before_ns=0, submit_after_ns=0, actual_selection_seq=0, attempt_id=0,
        consumed_dispatch=[0, 0], first_handoff_seq=0, handoff_queue=0, port=0,
        accepted_count=0, reason=0, snapshot=[], resources=[0]*10, items=list(items))
    return dict(row, **values)


def fixture(reverse=False, layout='per-direction'):
    cfg, reg = config(), manifest()
    if reverse:
        cfg['lines'].append(dict(copy.deepcopy(cfg['lines'][0]), name='reverse', ingress_port=1, egress_port=0))
        reg['lines'].append(dict(reg['lines'][0], name='reverse', ingress_port=1, egress_port=0))
    header = dict(schema='hng-native-v2', identity_policy=0, probe_port=9000,
                  capacity=128, record_bytes=1632, layout=layout, ports=[0, 1])
    snapshots = [[0, 1, 101, False], [1, int(reverse), 151 if reverse else 2**64-1, not reverse]]
    first = item()
    second = item(2, 1, 200)
    events = [raw_event(7, 4, port=port, resources=[0, 100, 100, 1, 10, 20, 0, 0, 0, 0]) for port in (0, 1)]
    events += [raw_event(0, 0, [first], at_ns=100), raw_event(1, 1, [first], at_ns=101),
               raw_event(5, 1, [first], at_ns=120, port=1, snapshot=copy.deepcopy(snapshots)),
               raw_event(6, 3, [first], port=1, gate_ns=120, submit_before_ns=121,
                         submit_after_ns=122, accepted_count=1, consumed_dispatch=[1, 0], snapshot=copy.deepcopy(snapshots)),
               raw_event(0, 0, [second], at_ns=200),
               raw_event(1, 1, [second], at_ns=201, actual_selection_seq=1),
               raw_event(2, 1, [second], at_ns=202, reason=0)]
    if reverse:
        other = item(3, 0, 150, 1)
        events[3:3] = [raw_event(0, 5 if layout == 'per-direction' else 0, [other], at_ns=150),
                       raw_event(1, 2, [other], at_ns=151)]
        events += [raw_event(5, 2, [other], at_ns=220, port=0, handoff_queue=1, snapshot=copy.deepcopy(snapshots)),
                   raw_event(6, 6 if layout == 'per-direction' else 3, [other], port=0, gate_ns=120,
                             submit_before_ns=221, submit_after_ns=222, accepted_count=1,
                             consumed_dispatch=[0, 1] if layout == 'per-direction' else [1, 1], snapshot=copy.deepcopy(snapshots))]
    events += [raw_event(3, 0, port=0, at_ns=250, actual_selection_seq=2),
               raw_event(4, 0, port=0, at_ns=251, actual_selection_seq=2),
               raw_event(4, 5 if layout == 'per-direction' else 0, port=1, at_ns=300, actual_selection_seq=int(reverse))]
    events += [raw_event(7, 4, port=port, at_ns=400,
                         resources=[1, 100, 100, 1, 12, 22, 0, 0, 0, 0]) for port in (0, 1)]
    return cfg, reg, header, events


def encode(header, events):
    counts, entries = [0]*7, []
    for order, row in enumerate(events):
        producer = row['producer']
        counts[producer] += 1
        row['producer_seq'], row['order'] = counts[producer], order
        common = [row[name] for name in ('at_ns', 'gate_ns', 'submit_before_ns', 'submit_after_ns')]
        common += row['resources']+[row['actual_selection_seq'], row['attempt_id']]
        common += row['consumed_dispatch']+[row[name] for name in ('first_handoff_seq', 'handoff_queue', 'port', 'accepted_count')]
        entries.append(entry(producer=producer, kind=row['kind'], seq=row['producer_seq'], order=order,
                             reason=row['reason'], common=common, snapshot=row['snapshot'], items=row['items']))
    seal = health(counts)
    return MAGIC+frame(1, line(header), 0)+frame(2, b''.join(entries), 1)+frame(3, line(seal), 2)


class NativeProjectionTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(NativeProjection, 'native array semantic projection is missing')
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)/'native.hngb'

    def project(self, cfg, reg, header, events, chunk_records=65536):
        self.path.write_bytes(encode(header, events))
        reader = NativeArrays(self.path, chunk_records=chunk_records)
        projection = NativeProjection(reader.header, reg, cfg)
        sampled = {'ingress': [], 'host': []}
        ordinals = []
        for batch in reader.batches():
            links = projection.accept(batch)
            ordinals.append(links)
            for index, event in enumerate(batch['events']):
                items = batch['items'][batch['items']['event_index'] == index]
                if event['kind'] == 0:
                    sampled['ingress'].append(projection.packet(event, items[0], int(links['ingress_seq'][index])))
                elif event['kind'] in (1, 2):
                    sampled['host'].append(projection.host(event, items[0], int(links['host_seq'][index])))
        counters = projection.counter_rows(reader.health, reader.counts, reader.seen)
        return projection, sampled, counters, ordinals

    def test_selected_rows_counts_runtime_and_all_counters_equal_legacy(self):
        for reverse, layout, chunk in ((False, 'per-direction', 65536), (True, 'per-direction', 3), (True, 'shared', 1)):
            with self.subTest(reverse=reverse, layout=layout, chunk=chunk):
                cfg, reg, header, events = fixture(reverse, layout)
                raw = encode(header, copy.deepcopy(events))
                text = io.StringIO()
                decode(io.BytesIO(raw), text)
                outputs = [io.StringIO() for _ in range(3)]
                adapter = NativeAdapter(io.StringIO(text.getvalue()), *outputs, run=reg['run'], lines=cfg['lines'])
                self.assertTrue(adapter.process()['complete'])
                expected = [[json.loads(row) for row in output.getvalue().splitlines()][1:-1] for output in outputs]
                projection, sampled, counters, ordinals = self.project(cfg, reg, header, events, chunk)
                self.assertEqual(sampled['ingress'], [row for row in expected[0] if row['kind'] == 'packet'])
                self.assertEqual(sampled['host'], [row for row in expected[1] if row['kind'] in ('offer', 'terminal')])
                self.assertEqual(counters, expected[2])
                self.assertEqual(projection.logical_emitted, [len(expected[0]), len(expected[1])])
                self.assertEqual(projection.diagnostics, Counter(row['kind'] for row in expected[1]))
                self.assertEqual(projection.at_ns, 400)
                self.assertEqual(projection.runtime['0'], (100, 202))
                self.assertEqual(projection.custody[0]['rx_returned'], 2)
                self.assertEqual(projection.custody[0]['tx_accepted'], 1)
                self.assertEqual(projection.custody[0]['q_drop'], 1)
                self.assertEqual(projection.port_state[0], dict(next_seq=2, arrival=200, frontier=250, ended=True))
                self.assertTrue(all(int(v) == -1 or int(v) >= 1 for block in ordinals for values in block.values() for v in values))

    def reject(self, change, reverse=False, chunk=3):
        cfg, reg, header, events = fixture(reverse)
        change(header, events)
        with self.assertRaises(StreamError) as caught:
            self.project(cfg, reg, header, events, chunk)
        self.assertEqual(caught.exception.measurement_status, 'INCONCLUSIVE')

    def test_unsampled_actor_reason_owner_and_handoff_errors_are_rejected(self):
        mutations = [lambda h, e: e[2].update(producer=5), lambda h, e: e[3].update(producer=2),
            lambda h, e: e[8].update(producer=3), lambda h, e: e[8].update(reason=9),
            lambda h, e: e[4].update(port=0), lambda h, e: e[4].update(first_handoff_seq=9),
            lambda h, e: e[4].update(handoff_queue=1), lambda h, e: e[5].update(accepted_count=2),
            lambda h, e: e[5].update(consumed_dispatch=[1, 1]), lambda h, e: e[5].update(port=0),
            lambda h, e: e[0].update(producer=1),
            lambda h, e: e.insert(3, raw_event(5, 0, port=1, handoff_queue=65535))]
        for change in mutations:
            with self.subTest(change=change):
                self.reject(change)

    def test_unsampled_item_flags_identity_line_vlan_and_lengths_are_rejected(self):
        for field, value in ((11, 1), (12, 1), (13, 4095), (13, 101), (14, 2), (8, 0),
                             (15, 2), (16, 0), (16, 2), (17, 3), (18, 3), (19, False), (20, True)):
            with self.subTest(field=field, value=value):
                self.reject(lambda h, e: e[2]['items'][0].__setitem__(field, value))

    def test_raw_ingress_sequence_frontier_and_ends_are_checked_across_batches(self):
        mutations = [lambda h, e: e[6]['items'][0].__setitem__(1, 2),
            lambda h, e: e[6]['items'][0].__setitem__(2, 2),
            lambda h, e: e[6]['items'][0].__setitem__(3, 50),
            lambda h, e: e[9].update(actual_selection_seq=1),
            lambda h, e: e[9].update(at_ns=199),
            lambda h, e: e[10].update(kind=3),
            lambda h, e: e.append(raw_event(0, 0, [item(3, 2, 500)]))]
        for change in mutations:
            with self.subTest(change=change):
                self.reject(change, chunk=1)

    def test_snapshot_duplicates_closed_frontiers_and_actor_regressions_are_rejected(self):
        mutations = [lambda h, e: e[4].update(snapshot=[[0, 1, 0, False], [0, 1, 0, False]]),
            lambda h, e: e[4].update(snapshot=[[0, 1, 2**64-1, False], [1, 0, 0, False]]),
            lambda h, e: e[4].update(snapshot=[[0, 1, 101, False], [1, 1, 0, False]]),
            lambda h, e: e[4].update(snapshot=[[99, 0, 0, False]]),
            lambda h, e: e[4].update(snapshot=[[0, 1, 101, False], [1, 0, 2**64-1, False]])]
        for change in mutations:
            with self.subTest(change=change):
                self.reject(change, chunk=1)

    def test_tx_timestamps_and_item_selection_gates_are_checked_without_sampling(self):
        for change in (lambda h, e: e[5].update(submit_before_ns=119),
                       lambda h, e: e[5].update(submit_after_ns=120),
                       lambda h, e: e[5]['items'][0].__setitem__(5, 122)):
            self.reject(change)

    def test_unused_markers_and_resource_endpoints_cannot_hide_evidence(self):
        for change in (lambda h, e: e[11].update(actual_selection_seq=1),
                       lambda h, e: e[11].update(items=[item()]),
                       lambda h, e: e.insert(12, raw_event(3, 5, port=1, at_ns=301)),
                       lambda h, e: e[-1].update(resources=[1, 100, 101, 1]+[0]*6),
                       lambda h, e: e[-1].update(port=0),
                       lambda h, e: h.update(ports=[0, 2])):
            self.reject(change)

    def test_ignored_marker_and_resource_items_keep_legacy_normalization(self):
        cfg, reg, header, events = fixture()
        ignored = [0]*22
        ignored[3], ignored[12], ignored[17] = 900, 65535, 2
        events[0]['items'] = [ignored]
        events[9]['items'] = [ignored]
        raw = encode(header, copy.deepcopy(events))
        text, outputs = io.StringIO(), [io.StringIO() for _ in range(3)]
        decode(io.BytesIO(raw), text)
        adapter = NativeAdapter(io.StringIO(text.getvalue()), *outputs, run=reg['run'], lines=cfg['lines'])
        self.assertTrue(adapter.process()['complete'])
        projection, _, counters, _ = self.project(cfg, reg, header, events, 1)
        self.assertEqual(projection.at_ns, adapter.at_ns)
        self.assertEqual(projection.at_ns, 900)
        self.assertEqual(projection.logical_emitted, adapter.logical_emitted[:2])
        self.assertEqual(counters, [json.loads(row) for row in outputs[2].getvalue().splitlines()][1:-1])
        self.assertEqual(projection.logical_emitted, [4, 5])
        self.assertEqual(projection.resources[0, 'start']['items'], [ignored])

    def test_host_observations_on_unused_physical_ingress_remain_diagnostic(self):
        for layout in ('shared', 'per-direction'):
            with self.subTest(layout=layout):
                cfg, reg, header, events = fixture(layout=layout)
                other = item(99, 0, 100, 0)
                other[11] = 1
                events += [raw_event(1, 2, [other], at_ns=500), raw_event(2, 2, [other], at_ns=501, reason=0)]
                if layout == 'shared':
                    offered = copy.deepcopy(other)
                    offered[5] = 502
                    events.append(raw_event(6, 3, [offered], port=1, gate_ns=502, submit_before_ns=503,
                        submit_after_ns=504, accepted_count=1, consumed_dispatch=[1, 0]))
                raw = encode(header, copy.deepcopy(events))
                text, outputs = io.StringIO(), [io.StringIO() for _ in range(3)]
                decode(io.BytesIO(raw), text)
                adapter = NativeAdapter(io.StringIO(text.getvalue()), *outputs, run=reg['run'], lines=cfg['lines'])
                self.assertTrue(adapter.process()['complete'])
                projection, sampled, counters, _ = self.project(cfg, reg, header, events, 1)
                self.assertEqual(sampled['host'][-1]['ingress_port'], 1)
                self.assertEqual(projection.custody[1]['q_drop'], 1)
                self.assertEqual(projection.custody[1]['tx_accepted'], int(layout == 'shared'))
                self.assertEqual(projection.custody, adapter.custody)
                self.assertEqual(projection.logical_emitted, adapter.logical_emitted[:2])
                self.assertEqual(projection.at_ns, adapter.at_ns)
                self.assertEqual(counters, [json.loads(row) for row in outputs[2].getvalue().splitlines()][1:-1])

    def test_terminal_reason_counts_and_stage_actors_match_legacy_in_mixed_batches(self):
        for layout in ('shared', 'per-direction'):
            with self.subTest(layout=layout):
                cfg, reg, header, events = fixture(reverse=True, layout=layout)
                events[3]['reason'] = 255  # Nonterminal reason bytes are not normalized.
                for owner in (0, 1):
                    for reason in range(len(REASONS)):
                        producer = (3 if owner == 0 or layout == 'shared' else 6) if reason in (6, 7) else 1+owner
                        if reason == 5:
                            producer = 5 if owner and layout == 'per-direction' else 0
                        packet = item(100+reason, 0, 100, owner)
                        packet[4], packet[5] = 2**64-1, 2**64-1
                        events.append(raw_event(2, producer, [packet], reason=reason, at_ns=500+reason))
                raw = encode(header, copy.deepcopy(events))
                text, outputs = io.StringIO(), [io.StringIO() for _ in range(3)]
                decode(io.BytesIO(raw), text)
                adapter = NativeAdapter(io.StringIO(text.getvalue()), *outputs, run=reg['run'], lines=cfg['lines'])
                self.assertTrue(adapter.process()['complete'])
                projection, _, counters, _ = self.project(cfg, reg, header, events, 7)
                self.assertEqual(projection.custody, adapter.custody)
                self.assertEqual(projection.logical_emitted, adapter.logical_emitted[:2])
                self.assertEqual(counters, [json.loads(row) for row in outputs[2].getvalue().splitlines()][1:-1])

    def test_mixed_ingress_tx_accepted_prefix_preserves_per_port_counters(self):
        cfg, reg, header, events = fixture(reverse=True, layout='shared')
        cfg['lines'][1]['egress_port'] = reg['lines'][1]['egress_port'] = 1
        events = [row for row in events if not (row['kind'] in (5, 6) and row['port'] == 0)]
        offered = []
        for index, owner in enumerate((0, 1, 0, 1)):
            packet = item(100+index, index, 500, owner)
            packet[5] = 600
            offered.append(packet)
        events.append(raw_event(6, 3, offered, port=1, gate_ns=600, submit_before_ns=610,
            submit_after_ns=611, accepted_count=3, consumed_dispatch=[1, 0]))
        raw = encode(header, copy.deepcopy(events))
        text, outputs = io.StringIO(), [io.StringIO() for _ in range(3)]
        decode(io.BytesIO(raw), text)
        adapter = NativeAdapter(io.StringIO(text.getvalue()), *outputs, run=reg['run'], lines=cfg['lines'])
        self.assertTrue(adapter.process()['complete'])
        projection, _, counters, _ = self.project(cfg, reg, header, events, 3)
        self.assertEqual(projection.custody[0]['tx_accepted'], 3)
        self.assertEqual(projection.custody[1]['tx_accepted'], 1)
        self.assertEqual(projection.custody, adapter.custody)
        self.assertEqual(counters, [json.loads(row) for row in outputs[2].getvalue().splitlines()][1:-1])

    def test_wrong_actual_selection_sequence_is_preserved_for_sampled_q_failure(self):
        cfg, reg, header, events = fixture()
        events[3]['actual_selection_seq'] = 999
        _, sampled, _, _ = self.project(cfg, reg, header, events, 1)
        self.assertEqual(sampled['host'][0]['kind'], 'offer')
        self.assertEqual(sampled['host'][0]['actual_selection_seq'], 999)

    def test_custody_snapshot_history_remains_diagnostic(self):
        cfg, reg, header, events = fixture()
        events[4]['snapshot'] = []
        events.insert(6, raw_event(6, 3, port=1, gate_ns=120, submit_before_ns=121,
            submit_after_ns=123, snapshot=[[0, 0, 0, False], [1, 0, 0, False]]))
        projection, _, _, _ = self.project(cfg, reg, header, events, 1)
        self.assertEqual(projection.diagnostics['attempt'], 2)

    def test_host_item_profile_fields_omitted_from_normalization_remain_diagnostic(self):
        cfg, reg, header, events = fixture()
        events[3]['items'] = copy.deepcopy(events[3]['items'])
        events[3]['items'][0][8] = 0
        events[3]['items'][0][13] = 4095
        events[3]['items'][0][14] = 2
        _, sampled, _, _ = self.project(cfg, reg, header, events, 1)
        self.assertEqual(sampled['host'][0]['kind'], 'offer')

    def test_uint64_identity_and_large_timestamps_remain_exact(self):
        cfg, reg, header, events = fixture()
        offset = 2**63+1
        for event in events:
            for key in ('at_ns', 'gate_ns', 'submit_before_ns', 'submit_after_ns'):
                event[key] += offset
            # Copy each item: the fixture shares packet objects between stages.
            event['items'] = copy.deepcopy(event['items'])
            for packet in event['items']:
                packet[0] = 2**64-1 if packet[1] == 0 else 2**64-2
                packet[3] += offset
                packet[5] += offset
            for snapshot in event['snapshot']:
                if snapshot[2] != 2**64-1:
                    snapshot[2] += offset
        projection, sampled, _, _ = self.project(cfg, reg, header, events, 2)
        self.assertEqual(sampled['ingress'][0]['external_identity'], '0a000001:0a000002:1:9000:18446744073709551615')
        self.assertEqual(sampled['ingress'][0]['arrival_ns'], 2**63+101)
        self.assertEqual(projection.at_ns, 2**63+401)

    def test_incomplete_native_health_keeps_legacy_counter_values(self):
        cfg, reg, header, events = fixture()
        raw = encode(header, events)
        offset = len(MAGIC)
        for _ in range(2):
            length, = struct.unpack_from('<I', raw, offset+4)
            offset += 16+length+4
        seal = json.loads(raw[offset+16:-4])
        seal['complete'] = False
        seal['producers'][0]['attempted'] += 1
        seal['producers'][0]['dropped'] += 1
        raw = raw[:offset]+frame(3, line(seal), 2)
        text, outputs = io.StringIO(), [io.StringIO() for _ in range(3)]
        decode(io.BytesIO(raw), text)
        adapter = NativeAdapter(io.StringIO(text.getvalue()), *outputs, run=reg['run'], lines=cfg['lines'])
        self.assertFalse(adapter.process()['complete'])
        self.path.write_bytes(raw)
        reader = NativeArrays(self.path)
        projection = NativeProjection(reader.header, reg, cfg)
        for batch in reader.batches():
            projection.accept(batch)
        actual = projection.counter_rows(reader.health, reader.counts, reader.seen)
        expected = [json.loads(row) for row in outputs[2].getvalue().splitlines()][1:-1]
        self.assertEqual(actual, expected)
        end = next(row for row in actual if row.get('scope_id') == 'native-ingress' and row.get('phase') == 'end')
        self.assertIsNone(end['values']['records_dropped'])

    def test_native_sequence_gap_is_not_repaired_by_projection(self):
        cfg, reg, header, events = fixture()
        self.path.write_bytes(encode(header, events))
        reader = NativeArrays(self.path)
        batch = next(reader.batches())
        batch['events']['producer_seq'][0] += 1
        projection = NativeProjection(reader.header, reg, cfg)
        with self.assertRaises(StreamError) as caught:
            projection.accept(batch)
        self.assertEqual(caught.exception.measurement_status, 'INCONCLUSIVE')
        with self.assertRaises(StreamError):
            projection.counter_rows(health((0,)*7), [0]*7, [0]*7)

    def test_attempt_handoff_ordinal_cannot_use_uint64_sentinel(self):
        def change(header, events):
            events[5]['items'] = copy.deepcopy(events[5]['items'])
            events[5]['items'][0][4] = 2**64-1
        self.reject(change)

    def test_empty_shared_attempt_keeps_legacy_unmapped_egress_diagnostic(self):
        cfg, reg, header, events = fixture(layout='shared')
        events.append(raw_event(6, 3, port=9, gate_ns=320, submit_before_ns=321,
            submit_after_ns=322, snapshot=[[0, 2, 201, False], [1, 0, 2**64-1, True]]))
        projection, _, _, _ = self.project(cfg, reg, header, events)
        self.assertEqual(projection.diagnostics['attempt'], 2)

    def test_empty_dispatch_does_not_consume_a_normalized_host_ordinal(self):
        cfg, reg, header, events = fixture()
        events.insert(3, raw_event(5, 1, port=1))
        projection, _, _, links = self.project(cfg, reg, header, events)
        self.assertEqual(int(links[0]['host_seq'][3]), -1)
        self.assertEqual(projection.diagnostics, Counter(offer=2, terminal=1, dispatch=1, attempt=1))
        events[3].update(producer=3, handoff_queue=2)
        projection, _, _, links = self.project(cfg, reg, header, events)
        self.assertEqual(int(links[0]['host_seq'][3]), -1)
        self.assertEqual(projection.diagnostics['dispatch'], 1)


if __name__ == '__main__':
    unittest.main()
