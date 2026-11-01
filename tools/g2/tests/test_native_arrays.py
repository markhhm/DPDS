# SPDX-License-Identifier: Apache-2.0
"""Column decoder parity and hostile-wire tests, using independent wire fixtures."""
import hashlib
import io
import json
from pathlib import Path
import struct
import tempfile
import unittest

import numpy as np

try:
    from tools.g2.native_arrays import NativeArrays
except ImportError:
    NativeArrays = None
from tools.g2 import native_binary
from tools.g2.native_adapter import ITEM_FIELDS
from tools.g2.protocol import StreamError
from tools.g2.tests.test_native_binary import HEADER, MAGIC, ZERO_ENTRY, capture, entry, frame, health, integer, line


class NativeArraysTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(NativeArrays, 'columnar native binary reader is missing')
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)/'native.hngb'

    def reader(self, raw, chunk_records=65536):
        self.path.write_bytes(raw)
        return NativeArrays(self.path, chunk_records=chunk_records)

    def rejects(self, raw):
        reader = None
        with self.assertRaises(StreamError) as caught:
            reader = self.reader(raw)
            list(reader.batches())
        self.assertEqual(caught.exception.code, 'NATIVE_BINARY_FORMAT')
        self.assertEqual(caught.exception.measurement_status, 'INCONCLUSIVE')
        if reader is not None:
            self.assertIsNone(reader.health)
            self.assertIsNone(reader.integrity)

    def assert_parity(self, raw, chunk_records=65536):
        output = io.StringIO()
        native_binary.decode(io.BytesIO(raw), output)
        expected = [json.loads(row) for row in output.getvalue().splitlines()]
        reader = self.reader(raw, chunk_records)
        self.assertEqual(reader.header, expected[0])
        self.assertIsNone(reader.health)
        actual = []
        for batch in reader.batches():
            events, items, snapshots = (batch[name] for name in ('events', 'items', 'snapshots'))
            self.assertIsInstance(events, np.ndarray)
            self.assertLessEqual(len(events), chunk_records)
            self.assertTrue(events.dtype.names)
            for index, event in enumerate(events):
                row = {name: event[name].tolist() for name in event.dtype.names if name != 'count'}
                row['items'] = [[item[name].item() for name in ITEM_FIELDS]
                                for item in items[items['event_index'] == index]]
                row['snapshot'] = [[s[name].item() for name in ('port', 'seen', 'lower_bound', 'ended')]
                                   for s in snapshots[snapshots['event_index'] == index]]
                actual.append(row)
        self.assertEqual(actual, expected[1:-1])
        self.assertEqual(reader.health, expected[-1])
        self.assertEqual(reader.integrity, {'sha256': hashlib.sha256(raw).hexdigest(), 'bytes': len(raw)})
        self.assertEqual(sum(reader.counts), len(actual))
        return reader

    def test_sparse_maxima_booleans_and_u64_are_exact(self):
        maximum = 2**64-1
        common = [maximum, 2**53+1, 3, 4, *range(5, 15), 15, 16, 17, 18, 19, 65535, 65534, 65533]
        packet = [maximum, 2**53+3, 3, 4, 5, 6, 2**32-1, 8, 9, 10, 11, 12, 13, 14, 15, 65535, 255, 254, 253, 1, 1, maximum]
        raw = capture(entry(producer=6, kind=7, reason=255, common=common,
                            snapshot=((65535, maximum, maximum, 1), (1, 2, 3, 0)),
                            items=(packet, [0]*22)), seal=health((0, 0, 0, 0, 0, 0, 1)))
        self.assert_parity(raw)

    def test_batch_boundaries_preserve_all_kinds_producer_sequences_and_links(self):
        counts = [0]*7
        records = []
        for index in range(103):
            producer = index % 7
            counts[producer] += 1
            records.append(entry(producer=producer, kind=index % 8, seq=counts[producer], order=index,
                                 items=([0]*22,)*(index % 17), snapshot=((index, index+1, index+2, index % 2),)))
        raw = (MAGIC + frame(1, HEADER, 0) + frame(2, b''.join(records[:50]), 1)
               + frame(2, b''.join(records[50:]), 2) + frame(3, line(health(counts)), 3))
        reader = self.assert_parity(raw, chunk_records=7)
        self.assertEqual(reader.counts, counts)
        self.assertEqual(reader.seen, counts)

    def test_empty_capture_and_incomplete_health_are_preserved(self):
        self.assert_parity(capture(b'', seal=health((0,)*7)))
        seal = health(complete=False)
        seal['producers'][0].update(attempted=2, dropped=1)
        self.assert_parity(capture(seal=seal))
        seal = health(complete=False)
        seal['producers'][0].update(attempted=2**64-1, dropped=2**64-2)
        reader = self.assert_parity(capture(entry(seq=2**64-1), seal=seal))
        self.assertEqual(reader.seen[0], 2**64-1)

    def test_every_truncation_and_crc_corruption_remains_unsealed(self):
        raw = capture()
        for size in range(len(raw)):
            with self.subTest(size=size):
                self.rejects(raw[:size])
        for offset in (0, 8, 10, 12, 16, 24, 40, len(raw)-1):
            bad = bytearray(raw)
            bad[offset] ^= 1
            self.rejects(bad)

    def test_unknown_frames_flags_sequences_and_trailing_data_are_rejected(self):
        for bad in (frame(1, HEADER, 1), frame(1, HEADER, 0, flags=1), frame(1, HEADER, 0, reserved=1),
                    frame(9, HEADER, 0), frame(2, ZERO_ENTRY, 0), frame(1, b'', 0),
                    struct.pack('<BBHIQ', 1, 0, 0, 65517, 0)):
            self.rejects(MAGIC + bad)
        for suffix in (b'x', frame(3, line(health()), 3), frame(2, ZERO_ENTRY, 3)):
            self.rejects(capture() + suffix)
        self.rejects(MAGIC + frame(1, HEADER, 0) + frame(1, HEADER, 1))
        self.rejects(MAGIC + frame(1, HEADER, 0) + frame(2, b'', 1))

    def test_invalid_event_tags_widths_masks_and_sequences_are_rejected(self):
        bad = [ZERO_ENTRY[:1], struct.pack('<H', 0), ZERO_ENTRY+b'\x00', entry(producer=7),
               entry(kind=8), entry(items=([0]*22,)*17), entry(snapshot=((0, 0, 0, 0),)*3),
               entry(snapshot=((0, 0, 0, 2),)), entry(snapshot=((65536, 0, 0, 0),)),
               entry(order=1), entry(seq=0), entry(seq=2), ZERO_ENTRY+entry(order=1)]
        body = ZERO_ENTRY[2:]
        invalid = [bytes([tag]) for tag in range(0x88, 0x100)]
        invalid += [b'\x80\x00', b'\x80\x7f', b'\x81\x80\x00', b'\x87'+b'\xff'*7+b'\x00']
        invalid += [bytes([0x80 | (width-1)])+b'\xff'*(width-1) for width in range(1, 9)]
        for value in invalid:
            altered = body[:4]+value+body[5:]
            bad.append(struct.pack('<H', len(altered))+altered)
        for mask, value in ((1 << 22, b''), (1, b'\x00'), (1 << 19, integer(65536))):
            altered = body[:6]+struct.pack('<I', mask)+value+b'\x00'
            bad.append(struct.pack('<H', len(altered))+altered)
        for index, value in ((6, 2**32), (9, 65536), (16, 256), (19, 2), (20, 2)):
            packet = [0]*22
            packet[index] = value
            bad.append(entry(items=(packet,)))
        # Unknown item bits, a present zero and trailing entry bytes.
        for mask, value in ((1 << 22, b''), (1, b'\x00')):
            altered = bytes([0, 0, 1, 0])+body[4:]+struct.pack('<I', mask)+value
            bad.append(struct.pack('<H', len(altered))+altered)
        bad.append(struct.pack('<H', len(body)+1)+body+b'\x00')
        for value in bad:
            with self.subTest(wire=value):
                self.rejects(capture(value))

    def test_header_and_health_cannot_forge_completeness(self):
        header = json.loads(HEADER)
        for name, value in (('schema', 'hng-native-v1'), ('ports', [0, 0]), ('identity_policy', 2), ('unknown', 0)):
            self.rejects(capture(header=line(dict(header, **{name: value}))))
        for name, value in (('complete', 1), ('abort_reasons', 1), ('producers', []), ('unknown', 0)):
            self.rejects(capture(seal=dict(health(), **{name: value})))
        for name, value in (('emitted', 0), ('attempted', 2), ('high_water', 9), ('dropped', 1), ('identity_errors', 1)):
            seal = health()
            seal['producers'][0][name] = value
            self.rejects(capture(seal=seal))
        self.rejects(capture(header=b'['*30000+b'0'+b']'*30000+b'\n'))

    def test_mutated_source_never_receives_integrity_seal(self):
        reader = self.reader(capture())
        raw = bytearray(self.path.read_bytes())
        raw[-1] ^= 1
        self.path.write_bytes(raw)
        with self.assertRaises(StreamError):
            list(reader.batches())
        self.assertIsNone(reader.integrity)
        self.assertIsNone(reader.health)
        reader = self.reader(capture(ZERO_ENTRY+entry(seq=2, order=1), seal=health((2, 0, 0, 0, 0, 0, 0))), 1)
        batches = reader.batches()
        next(batches)
        self.path.write_bytes(b'')
        with self.assertRaises(StreamError):
            next(batches)
        self.assertIsNone(reader.integrity)
        self.assertIsNone(reader.health)


if __name__ == '__main__':
    unittest.main()
