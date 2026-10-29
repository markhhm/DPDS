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
    def fixture(self, *, dropped=0, sealed=True):
        item = [7, 0, 0, 100, 0, 0, 0, 0, 76, 0, 0, 0, 0, 100, 1, 0, 2, 2, 0, True, False, 0]
        row = dict(producer=0, producer_seq=1, order=0, kind=0, at_ns=0,
                   gate_ns=0, submit_before_ns=0, submit_after_ns=0, actual_selection_seq=0,
                   attempt_id=0, consumed_dispatch=[0, 0], first_handoff_seq=0, handoff_queue=0,
                   port=0, accepted_count=0, reason=0, snapshot=[], items=[item], resources=[0]*10)
        end = dict(row, producer_seq=2, order=1, kind=4, items=[], actual_selection_seq=1)
        header = dict(schema='hng-native-v1', identity_policy=1, probe_port=9000, capacity=16, record_bytes=2048)
        health = dict(kind='health', complete=not dropped, producers=[dict(attempted=2+dropped, emitted=2,
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

    def test_trailing_native_loss_remains_incomplete_without_sequence_gap(self):
        result, streams = self.normalize(self.fixture(dropped=1))
        self.assertFalse(result['complete'])
        self.assertTrue(all(not s[-1]['complete'] for s in streams))
        self.assertTrue(all(s[-1]['dropped_records'] >= 1 for s in streams))

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
