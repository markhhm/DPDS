import unittest

from tools.g2.probe import decode_probe, encode_probe


class ProbeTests(unittest.TestCase):
    def test_stage1_network_order_sequence_magic_flag_and_padding(self):
        payload = encode_probe(0x0102030405060708, True)
        self.assertEqual(payload, bytes.fromhex("01020304050607084c30504b01") + bytes(87))
        self.assertEqual(decode_probe(payload), (0x0102030405060708, True))
        self.assertEqual(decode_probe(encode_probe(0, False)), (0, False))

    def test_truncated_wrong_magic_and_changed_flags_do_not_decode(self):
        good = encode_probe(1, False)
        for bad in [good[:12], good[:8] + bytes(4) + good[12:], good[:12] + b"\x02" + good[13:]]:
            with self.subTest(payload=bad), self.assertRaises(ValueError):
                decode_probe(bad)
