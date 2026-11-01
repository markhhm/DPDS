# SPDX-License-Identifier: Apache-2.0
"""Independent wire fixtures for the offline native binary trust boundary."""
import binascii
import io
import json
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import unittest

try:
    from tools.g2 import native_binary
except ImportError:
    native_binary = None
from tools.g2.protocol import StreamError

MAGIC = b'HNGBIN1\n'
HEADER = b'{"schema":"hng-native-v2","identity_policy":0,"probe_port":9000,"capacity":8,"record_bytes":2248,"layout":"per-direction","ports":[0,1]}\n'
ZERO_ENTRY = bytes.fromhex('0b00 00000000 01 00 00000000 00')
HEALTH_KEYS = ('attempted', 'emitted', 'dropped', 'capture_drops', 'high_water', 'identity_errors', 'malformed_records')


def frame(kind, payload, seq, *, flags=0, reserved=0):
    raw = struct.pack('<BBHIQ', kind, flags, reserved, len(payload), seq) + payload
    return raw + struct.pack('<I', binascii.crc32(raw))


def line(value):
    return json.dumps(value, separators=(',', ':')).encode() + b'\n'


def health(counts=(1, 0, 0, 0, 0, 0, 0), *, complete=True, abort_reasons=0):
    return dict(kind='health', complete=complete, abort_reasons=abort_reasons,
                producers=[dict(zip(HEALTH_KEYS, (n, n, 0, 0, int(n > 0), 0, 0))) for n in counts])


def capture(entry=ZERO_ENTRY, *, seal=None, header=HEADER):
    parts = [MAGIC, frame(1, header, 0)]
    if entry:
        parts.append(frame(2, entry, 1))
    parts.append(frame(3, line(health() if seal is None else seal), len(parts)-1))
    return b''.join(parts)


def integer(number):
    if number < 128:
        return bytes([number])
    width = (number.bit_length()+7)//8
    return bytes([0x80 | (width-1)]) + number.to_bytes(width, 'little')


def entry(*, producer=0, kind=0, reason=0, seq=1, order=0, common=None, snapshot=(), items=()):
    def sparse(values):
        return struct.pack('<I', sum(1 << i for i, v in enumerate(values) if v)) + b''.join(integer(v) for v in values if v)
    body = bytes([producer, kind, len(items), reason]) + integer(seq) + integer(order)
    body += sparse([0]*22 if common is None else common) + bytes([len(snapshot)])
    for port, seen, lower, ended in snapshot:
        body += integer(port) + integer(seen) + integer(lower) + bytes([ended])
    body += b''.join(sparse(values) for values in items)
    return struct.pack('<H', len(body)) + body


class NativeBinaryTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(native_binary, 'offline native binary decoder is missing')

    def decode(self, raw):
        output = io.StringIO()
        result = native_binary.decode(io.BytesIO(raw), output)
        return result, output.getvalue()

    def rejects(self, raw):
        output = io.StringIO()
        with self.assertRaises(StreamError) as caught:
            native_binary.decode(io.BytesIO(raw), output)
        self.assertEqual(caught.exception.measurement_status, 'INCONCLUSIVE')
        self.assertNotIn('"kind":"health"', output.getvalue())

    def test_zero_record_preserves_raw_endpoints_and_legacy_key_order(self):
        seal = line(health())
        result, output = self.decode(capture())
        expected = ('{"producer":0,"producer_seq":1,"order":0,"kind":0,"at_ns":0,"gate_ns":0,'
                    '"submit_before_ns":0,"submit_after_ns":0,"actual_selection_seq":0,"attempt_id":0,'
                    '"consumed_dispatch":[0,0],"first_handoff_seq":0,"handoff_queue":0,"port":0,'
                    '"accepted_count":0,"reason":0,"snapshot":[],"resources":[0,0,0,0,0,0,0,0,0,0],"items":[]}\n')
        self.assertEqual(output, HEADER.decode() + expected + seal.decode())
        self.assertEqual(result, health())

    def test_tagged_integer_literal_boundaries_preserve_exact_values(self):
        cases = ((128, '80 80'), (255, '80 ff'), (256, '81 00 01'),
                 (16383, '81 ff 3f'), (9007199254740992, '86 00 00 00 00 00 00 20'),
                 (18446744073709551615, '87 ff ff ff ff ff ff ff ff'))
        for want, encoded in cases:
            with self.subTest(value=want):
                body = bytes.fromhex('00000000 01 00 01000000') + bytes.fromhex(encoded) + b'\x00'
                raw = struct.pack('<H', len(body)) + body
                _, output = self.decode(capture(raw))
                self.assertEqual(json.loads(output.splitlines()[1])['at_ns'], want)

    def test_sparse_fields_keep_c_integer_widths_boolean_positions_and_u64_precision(self):
        max64 = (1 << 64)-1
        common = [max64, 2**53+1, 3, 4, *range(5, 15), 15, 16, 17, 18, 19, 65535, 65534, 65533]
        packet = [max64, 2**53+3, 3, 4, 5, 6, 2**32-1, 8, 9, 10, 11, 12, 13, 14, 15, 65535, 255, 254, 253, 1, 1, max64]
        raw = entry(producer=6, kind=7, reason=255, common=common,
                    snapshot=((65535, max64, max64, 1), (1, 2, 3, 0)), items=(packet, [0]*22))
        _, output = self.decode(capture(raw, seal=health((0, 0, 0, 0, 0, 0, 1))))
        row = json.loads(output.splitlines()[1])
        self.assertEqual(row, dict(producer=6, producer_seq=1, order=0, kind=7,
            at_ns=max64, gate_ns=9007199254740993, submit_before_ns=3, submit_after_ns=4,
            actual_selection_seq=15, attempt_id=16, consumed_dispatch=[17, 18], first_handoff_seq=19,
            handoff_queue=65535, port=65534, accepted_count=65533, reason=255,
            snapshot=[[65535, max64, max64, True], [1, 2, 3, False]], resources=list(range(5, 15)),
            items=[[max64, 9007199254740995, 3, 4, 5, 6, 4294967295, 8, 9, 10, 11, 12, 13, 14, 15, 65535, 255, 254, 253, True, True, max64],
                   [0]*19+[False, False, 0]]))
        self.assertIs(type(row['items'][0][19]), bool)
        self.assertIs(type(row['items'][1][20]), bool)

    def test_multiple_frames_and_all_producers_and_kinds(self):
        entries = [entry(producer=i % 7, kind=i, seq=1 if i < 7 else 2, order=i) for i in range(8)]
        seal = health((2, 1, 1, 1, 1, 1, 1))
        raw = MAGIC + frame(1, HEADER, 0) + frame(2, b''.join(entries[:4]), 1) + frame(2, b''.join(entries[4:]), 2) + frame(3, line(seal), 3)
        _, output = self.decode(raw)
        self.assertEqual([json.loads(x)['kind'] for x in output.splitlines()[1:-1]], list(range(8)))

    def test_empty_capture_is_valid_and_metadata_whitespace_survives(self):
        header = HEADER.replace(b'":', b'" : ')
        seal = health((0,)*7)
        _, output = self.decode(capture(b'', seal=seal, header=header))
        self.assertEqual(output, header.decode() + line(seal).decode())

    def test_declared_loss_preserves_incomplete_seal_and_sequence_gap(self):
        seal = health(complete=False)
        seal['producers'][0].update(attempted=3, dropped=1, capture_drops=1)
        result, output = self.decode(capture(entry(seq=3), seal=seal))
        self.assertFalse(result['complete'])
        self.assertTrue(output.endswith(line(seal).decode()))

    def test_every_truncation_and_corruption_is_unsealed(self):
        raw = capture()
        for size in range(len(raw)):
            with self.subTest(size=size):
                self.rejects(raw[:size])
        for offset in (0, 8, 10, 12, 16, 24, 40, len(raw)-1):
            bad = bytearray(raw)
            bad[offset] ^= 1
            with self.subTest(offset=offset):
                self.rejects(bytes(bad))

    def test_framing_rejects_unknown_flags_types_sequences_and_trailing_seals(self):
        for bad in (frame(1, HEADER, 1), frame(1, HEADER, 0, flags=1), frame(1, HEADER, 0, reserved=1),
                    frame(9, HEADER, 0), frame(2, ZERO_ENTRY, 0), frame(1, b'', 0)):
            with self.subTest(bad=bad[:16]):
                self.rejects(MAGIC + bad)
        for tail in (b'x', frame(3, line(health()), 3), frame(2, ZERO_ENTRY, 3)):
            self.rejects(capture() + tail)
        self.rejects(MAGIC + frame(1, HEADER, 0) + frame(1, HEADER, 1))
        self.rejects(MAGIC + frame(1, HEADER, 0) + frame(2, b'', 1))
        self.rejects(MAGIC + frame(1, HEADER, 0) + frame(2, ZERO_ENTRY, 2))
        self.rejects(MAGIC + struct.pack('<BBHIQ', 1, 0, 0, 65517, 0))
        self.rejects(MAGIC + struct.pack('<BBHIQ', 1, 0, 0, 2**32-1, 0))

    def test_entry_structure_rejects_noncanonical_varints_masks_and_width_overflow(self):
        bad_entries = [ZERO_ENTRY[:1], struct.pack('<H', 0), ZERO_ENTRY + b'\x00',
            entry(producer=7), entry(kind=8), entry(items=([0]*22,)*17),
            entry(snapshot=((0, 0, 0, 0),)*3), entry(snapshot=((0, 0, 0, 2),)),
            entry(snapshot=((65536, 0, 0, 0),))]
        body = ZERO_ENTRY[2:]
        for bad_seq in (b'\x80\x00', b'\x80\x7f', b'\x81\x80\x00', b'\x88', b'\xff'):
            altered = body[:4] + bad_seq + body[5:]
            bad_entries.append(struct.pack('<H', len(altered)) + altered)
        for mask, value in ((1 << 22, b''), (1, b'\x00'), (1 << 19, integer(65536))):
            altered = body[:6] + struct.pack('<I', mask) + value + b'\x00'
            bad_entries.append(struct.pack('<H', len(altered)) + altered)
        for index, bad_value in ((6, 2**32), (9, 65536), (16, 256), (19, 2), (20, 2)):
            packet = [0]*22
            packet[index] = bad_value
            bad_entries.append(entry(items=(packet,)))
        for raw in bad_entries:
            with self.subTest(raw=raw):
                self.rejects(capture(raw))

    def test_tagged_integers_reject_reserved_tags_redundant_widths_and_truncation(self):
        prefix = bytes.fromhex('00000000 01 00 01000000')
        invalid = [bytes([tag]) for tag in range(0x88, 0x100)]
        invalid += [bytes.fromhex('80 00'), bytes.fromhex('80 7f'), bytes.fromhex('81 80 00'),
                    bytes.fromhex('82 00 01 00'), bytes.fromhex('87 ff ff ff ff ff ff ff 00')]
        for encoded in invalid:
            with self.subTest(encoded=encoded):
                body = prefix + encoded + b'\x00'
                self.rejects(capture(struct.pack('<H', len(body)) + body))
        for width in range(1, 9):
            body = prefix + bytes([0x80 | (width-1)]) + b'\xff'*(width-1)
            self.rejects(capture(struct.pack('<H', len(body)) + body))

    def test_order_and_producer_sequence_cannot_be_repaired_by_decoder(self):
        for records in (entry(order=1), entry(seq=0), entry(seq=2),
                        ZERO_ENTRY + entry(seq=1, order=1), ZERO_ENTRY + entry(seq=0, order=1)):
            self.rejects(capture(records))
        # A declared loss cannot excuse repeated or decreasing producer sequences.
        seal = health((2, 0, 0, 0, 0, 0, 0), complete=False)
        seal['producers'][0].update(attempted=3, dropped=1)
        self.rejects(capture(ZERO_ENTRY + entry(order=1), seal=seal))

    def test_header_and_health_validation_cannot_forge_a_complete_capture(self):
        header = json.loads(HEADER)
        for name, value in (('schema', 'hng-native-v1'), ('identity_policy', 2), ('probe_port', 65536), ('probe_port', 0),
                            ('capacity', 0), ('record_bytes', True), ('layout', 'other'), ('ports', [0, 0]),
                            ('ports', [0]), ('ports', [False, 1]), ('unknown', 0)):
            bad = dict(header, **{name: value})
            with self.subTest(header=name, value=value):
                self.rejects(capture(header=line(bad)))
        for bad_header in (HEADER.replace(b'"capacity":8', b'"capacity":8,"capacity":8'), HEADER[:-1], b'[]\n'):
            self.rejects(capture(header=bad_header))
        for name, value in (('complete', 1), ('abort_reasons', 16), ('producers', []), ('unknown', 0)):
            self.rejects(capture(seal=dict(health(), **{name: value})))
        for name, value in (('attempted', 2), ('emitted', 0), ('dropped', 1), ('high_water', 9),
                            ('identity_errors', 1), ('malformed_records', 1), ('capture_drops', -1),
                            ('attempted', 2**64), ('high_water', True), ('unknown', 0)):
            seal = health()
            seal['producers'][0][name] = value
            with self.subTest(health=name, value=value):
                self.rejects(capture(seal=seal))
        self.rejects(capture(seal=health(abort_reasons=1)))

    def test_metadata_nesting_cannot_escape_the_inconclusive_error_contract(self):
        nested = b'['*30000 + b'0' + b']'*30000 + b'\n'
        self.rejects(capture(header=nested))
        self.rejects(MAGIC + frame(1, HEADER, 0) + frame(3, nested, 1))

    def test_maximum_frame_size_accepts_the_exact_64k_boundary(self):
        header = HEADER[:-1] + b' '*(65516-len(HEADER)) + b'\n'
        _, output = self.decode(capture(b'', seal=health((0,)*7), header=header))
        self.assertEqual(output, header.decode() + line(health((0,)*7)).decode())

    def test_stream_short_reads_are_supported_without_unbounded_reads(self):
        class ShortReads(io.BytesIO):
            def read(self, size=-1):
                if not 0 < size <= 65536:
                    raise AssertionError('unbounded binary read')
                return super().read(min(size, 3))
        output = io.StringIO()
        result = native_binary.decode(ShortReads(capture()), output)
        self.assertTrue(result['complete'])

    def test_cli_uses_exclusive_output_and_exit_two_for_loss_or_corruption(self):
        with tempfile.TemporaryDirectory() as directory:
            source, target = Path(directory)/'capture.bin', Path(directory)/'out.jsonl'
            def run(raw):
                source.write_bytes(raw)
                return subprocess.run([sys.executable, '-m', 'tools.g2.native_binary', str(source), '--out', str(target)], capture_output=True)
            result = run(capture())
            self.assertEqual(result.returncode, 0, result.stderr)
            saved = target.read_bytes()
            self.assertNotEqual(run(capture()).returncode, 0)
            self.assertEqual(target.read_bytes(), saved)
            target.unlink()
            seal = health(complete=False)
            seal['producers'][0].update(attempted=2, dropped=1)
            self.assertEqual(run(capture(seal=seal)).returncode, 2)
            target.unlink()
            self.assertEqual(run(capture()[:-1]).returncode, 2)
            self.assertNotIn(b'"kind":"health"', target.read_bytes())


if __name__ == '__main__':
    unittest.main()
