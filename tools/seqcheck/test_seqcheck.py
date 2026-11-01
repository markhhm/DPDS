# SPDX-License-Identifier: Apache-2.0
"""Exercise the real C checker with independent synthetic pcapng pairs."""
import hashlib
import json
import os
from pathlib import Path
import struct
import subprocess
import tempfile
import unittest

from tools.g2.probe import encode_probe

ROOT = Path(__file__).resolve().parents[2]
BINARY = Path(os.environ.get('SEQCHECK_BINARY', ROOT / 'build' / 'seqcheck'))


def block(kind, body, order='<'):
    size = len(body) + 12
    return struct.pack(order + 'II', kind, size) + body + struct.pack(order + 'I', size)


def option(code, value, order='<'):
    return struct.pack(order + 'HH', code, len(value)) + value + bytes(-len(value) % 4)


def frame(sequence, late=False, *, vlan=100, sport=1111, options=0):
    payload = encode_probe(sequence, late)
    ip = struct.pack('!BBHHHBBHII', 0x45 + options // 4, 0,
                     20 + options + 8 + len(payload), 0, 0, 64, 17, 0,
                     0xc0000201, 0xc6336402) + bytes(options)
    udp = struct.pack('!HHHH', sport, 9000, 8 + len(payload), 0)
    return bytes.fromhex('ffffffffffff0200000000018100') + struct.pack('!HH', vlan, 0x0800) + ip + udp + payload


def capture(path, packets, *, order='<', caplen=59, drop=0, stats=True):
    """Packets are (sequence, late[, frame kwargs]); retain only identity prefix."""
    data = block(0x0a0d0d0a, struct.pack(order + 'IHHq', 0x1a2b3c4d, 1, 0, -1), order)
    data += block(1, struct.pack(order + 'HHI', 1, 0, caplen) + option(9, b'\x09', order) + bytes(4), order)
    for i, item in enumerate(packets):
        seq, late, *rest = item
        packet = frame(seq, late, **(rest[0] if rest else {}))[:caplen]
        body = struct.pack(order + 'IIIII', 0, 0, i + 1, len(packet), 1518)
        data += block(6, body + packet + bytes(-len(packet) % 4), order)
    if stats:
        opts = b''.join(option(code, value, order) for code, value in [
            (2, struct.pack(order + 'II', 0, 0)),
            (3, struct.pack(order + 'II', 0, len(packets) + 1)),
            (5, struct.pack(order + 'Q', drop)), (7, bytes(8)),
            (8, struct.pack(order + 'Q', len(packets)))]) + bytes(4)
        data += block(5, struct.pack(order + 'III', 0, 0, len(packets) + 1) + opts, order)
    path.write_bytes(data)


class SeqcheckTests(unittest.TestCase):
    def check_pair(self, ingress, receiver, *, ingress_options=None, receiver_options=None, details=10, mutate_receiver=None):
        self.assertTrue(BINARY.is_file(), 'C seqcheck executable has not been implemented/built')
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            before, after = root / 'ingress.pcapng', root / 'receiver.pcapng'
            capture(before, ingress, **(ingress_options or {}))
            capture(after, receiver, **(receiver_options or {}))
            if mutate_receiver:
                mutate_receiver(after)
            result = subprocess.run([str(BINARY), '--ingress', str(before), '--receiver', str(after),
                                     '--details', str(details)], capture_output=True, text=True)
            report = json.loads(result.stdout)
            self.assertEqual(report['schema'], 'hng-seqcheck-v1')
            self.assertEqual(report['details_limit'], details)
            for role, path in [('ingress', before), ('receiver', after)]:
                self.assertEqual(report['inputs'][role]['sha256'], hashlib.sha256(path.read_bytes()).hexdigest())
                self.assertEqual(report['inputs'][role]['byte_size'], path.stat().st_size)
            for name, count in report['totals'].items():
                self.assertEqual(count, sum(flow[name] for flow in report['flows']), name)
            return result.returncode, report

    def test_stdout_write_failure_returns_invalid(self):
        self.assertTrue(BINARY.is_file())
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'capture.pcapng'
            capture(path, [(0, False)])
            with open('/dev/full', 'wb') as full:
                result = subprocess.run([str(BINARY), '--ingress', str(path), '--receiver', str(path)],
                                        stdout=full, stderr=subprocess.PIPE)
            self.assertEqual(result.returncode, 2)
            self.assertTrue(result.stderr)

    def test_filename_bytes_cannot_break_json_and_utf8_is_preserved(self):
        self.assertTrue(BINARY.is_file())
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / 'capture.pcapng'
            capture(source, [(0, False)])
            cases = [(b'input-\xff.pcapng', False), (b'truncated-\xe2', False),
                     (b'overlong-\xe0\x80\x80', False), (b'surrogate-\xed\xa0\x80', False),
                     (b'too-large-\xf4\x90\x80\x80', False),
                     ('入口.pcapng'.encode(), True), ('packet-é-😀.pcapng'.encode(), True)]
            for suffix, valid_utf8 in cases:
                path = os.fsencode(temp) + b'/' + suffix
                os.link(source, path)
                result = subprocess.run([os.fsencode(BINARY), b'--ingress', path, b'--receiver', path],
                                        capture_output=True)
                try:
                    text = result.stdout.decode('utf-8')
                except UnicodeDecodeError:
                    self.fail('checker JSON output must be valid UTF-8')
                report = json.loads(text)
                self.assertEqual((result.returncode, report['status']), (0, 'PASS'))
                if valid_utf8:
                    self.assertEqual(report['inputs']['ingress']['path'], os.fsdecode(path))

    def test_ordinary_swap_is_reorder(self):
        code, r = self.check_pair([(0, False), (1, False), (2, False)],
                                  [(1, False), (0, False), (2, False)])
        self.assertEqual((code, r['status'], r['complete']), (1, 'FAIL', True))
        self.assertEqual((r['totals']['ordinary_reorder'], r['totals']['missing']), (1, 0))
        self.assertEqual(r['details']['ordinary_reorder'][0]['sequence'], 0)

    def test_late_arrival_does_not_reorder_ordinary(self):
        code, r = self.check_pair([(0, False), (1, True), (2, False)],
                                  [(0, False), (2, False), (1, True)])
        self.assertEqual((code, r['status'], r['totals']['ordinary_reorder']), (0, 'PASS', 0))

    def test_missing_identity_is_counted(self):
        code, r = self.check_pair([(0, False), (1, True), (2, False)], [(0, False), (2, False)])
        self.assertEqual((code, r['complete'], r['totals']['missing']), (1, True, 1))
        self.assertEqual(r['details']['missing'][0]['sequence'], 1)

    def test_capture_statistics_cannot_reset_between_packets(self):
        def reset_statistics(path):
            data = path.read_bytes()
            blocks = []
            offset = 0
            while offset < len(data):
                size = struct.unpack_from('<I', data, offset + 4)[0]
                blocks.append(data[offset:offset + size])
                offset += size
            final = bytearray(blocks[-1])
            # Final ISB start tick changes from zero to one: both bounds cover
            # the packet times, but a statistics epoch reset is not healthy.
            struct.pack_into('<I', final, 28, 1)
            initial = block(5, struct.pack('<III', 0, 0, 2) + b''.join(option(code, value) for code, value in [
                (2, struct.pack('<II', 0, 0)), (3, struct.pack('<II', 0, 2)),
                (5, bytes(8)), (7, bytes(8)), (8, struct.pack('<Q', 1))]) + bytes(4))
            path.write_bytes(b''.join(blocks[:3]) + initial + blocks[3] + final)
        code, r = self.check_pair([(0, False), (1, False)], [(0, False), (1, False)],
                                  mutate_receiver=reset_statistics)
        self.assertEqual((code, r['status'], r['complete']), (2, 'INVALID', False))

    def test_positive_capture_drop_cannot_be_reset_to_zero(self):
        def reset_drops(path):
            data = path.read_bytes()
            offset = 0
            while struct.unpack_from('<I', data, offset)[0] != 5:
                offset += struct.unpack_from('<I', data, offset + 4)[0]
            positive = bytearray(data[offset:])
            struct.pack_into('<Q', positive, 48, 1)
            path.write_bytes(data[:offset] + positive + data[offset:])
        code, r = self.check_pair([(0, False)], [(0, False)], mutate_receiver=reset_drops)
        self.assertEqual((code, r['status'], r['complete']), (2, 'INVALID', False))

    def test_late_flag_cannot_hide_ordinary_reorder(self):
        code, r = self.check_pair([(0, False), (1, False)], [(1, False), (0, True)])
        self.assertEqual((code, r['totals']['ordinary_reorder'], r['totals']['flag_mismatch']), (1, 1, 1))

    def test_noncontiguous_uint64_follows_ingress_rank(self):
        packets = [(2**64-1, False), (7, False), (2**63, False)]
        code, r = self.check_pair(packets, packets, receiver_options={'order': '>'})
        self.assertEqual((code, r['totals']['ordinary_reorder'], r['totals']['missing']), (0, 0, 0))

    def test_vlan_flows_are_independent(self):
        a, b = (9, False, {'vlan': 100}), (9, False, {'vlan': 101})
        code, r = self.check_pair([a, b], [b, a])
        self.assertEqual((code, len(r['flows'])), (0, 2))

    def test_many_udp_flows_keep_independent_identity_and_order(self):
        packets = [(0, False, {'sport': 1111 + i}) for i in range(40)]
        code, r = self.check_pair(packets, list(reversed(packets)))
        self.assertEqual((code, len(r['flows']), r['totals']['ordinary_reorder']), (0, 40, 0))

    def test_receiver_only_flow_is_unexpected_and_source_flow_missing(self):
        code, r = self.check_pair([(0, False)], [(0, False, {'vlan': 101})])
        self.assertEqual((code, len(r['flows']), r['totals']['missing'], r['totals']['unexpected']), (1, 2, 1, 1))

    def test_duplicates_unexpected_and_flag_mismatch(self):
        code, r = self.check_pair([(0, False), (1, True)],
                                  [(0, True), (0, False), (1, True), (99, False), (99, False)])
        self.assertEqual(code, 1)
        self.assertEqual({k: r['totals'][k] for k in ('duplicate_receiver', 'unexpected', 'flag_mismatch', 'missing')},
                         dict(duplicate_receiver=2, unexpected=2, flag_mismatch=1, missing=0))

    def test_duplicate_ingress_is_invalid(self):
        code, r = self.check_pair([(0, False), (0, False)], [(0, False)])
        self.assertEqual((code, r['status'], r['complete'], r['totals']['duplicate_ingress']),
                         (2, 'INVALID', False, 1))

    def test_details_truncation_does_not_truncate_counts(self):
        code, r = self.check_pair([(i, False) for i in range(5)], [], details=2)
        self.assertEqual((code, r['totals']['missing'], len(r['details']['missing']), r['details_truncated']['missing']),
                         (1, 5, 2, True))

    def test_unknown_health_and_capture_drop_are_invalid(self):
        for opts in ({'stats': False}, {'drop': 1}):
            with self.subTest(opts=opts):
                code, r = self.check_pair([(0, False)], [(0, False)], receiver_options=opts)
                self.assertEqual((code, r['status'], r['complete']), (2, 'INVALID', False))

    def test_truncated_eof_and_bad_block_trailer_are_invalid(self):
        def truncate(path):
            path.write_bytes(path.read_bytes()[:-1])
        def bad_trailer(path):
            data = bytearray(path.read_bytes())
            data[-1] ^= 1
            path.write_bytes(data)
        for mutation in (truncate, bad_trailer):
            with self.subTest(mutation=mutation.__name__):
                code, r = self.check_pair([(0, False)], [(0, False)], mutate_receiver=mutation)
                self.assertEqual((code, r['status'], r['complete']), (2, 'INVALID', False))

    def test_missing_details_follow_arrival_not_numeric_sequence(self):
        code, r = self.check_pair([(2**64-1, False), (7, False), (2**63, False)], [], details=2)
        self.assertEqual((code, r['totals']['missing']), (1, 3))
        self.assertEqual([item['sequence'] for item in r['details']['missing']], [2**64-1, 7])

    def test_late_arrival_before_ordinary_does_not_advance_high_water(self):
        packets = [(0, False), (1, False), (2, True)]
        code, r = self.check_pair(packets, [packets[2], packets[0], packets[1]], details=0)
        self.assertEqual((code, r['totals']['ordinary_reorder']), (0, 0))

    def test_truncated_identity_is_invalid_but_ipv4_options_work(self):
        code, r = self.check_pair([(0, False)], [(0, False)], receiver_options={'caplen': 58})
        self.assertEqual((code, r['complete']), (2, False))
        packets = [(0, False, {'options': 40})]
        code, r = self.check_pair(packets, packets, ingress_options={'caplen': 99}, receiver_options={'caplen': 99})
        self.assertEqual(code, 0)


if __name__ == '__main__':
    unittest.main()
