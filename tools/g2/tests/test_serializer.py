# SPDX-License-Identifier: Apache-2.0
"""Hand-computed busy periods retain ordinary packets preceding a late packet."""
import unittest

from tools.g2.tests.test_online import expected
from tools.g2.tests.test_registration import manifest

try:
    from tools.g2.serializer import Serialization
except ImportError:
    Serialization = None


class SerializationTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue(callable(Serialization), "independent bounded serializer is missing")
        self.registration = manifest()

    def test_late_membership_covers_the_whole_busy_period_including_earlier_ordinary_packets(self):
        serializer = Serialization(self.registration)
        handles = []
        for seq, release, late in ((0,0,False),(1,10,False),(2,20,True),(3,400,False)):
            row = expected(seq, arrival_ns=0, release_ns=release, late=late)
            handle = serializer.enqueue(row)
            handles.append(handle)
            serializer.observe(handle, -10 if seq==1 else 0)
        serializer.advance(0, 1000)
        result = serializer.finish()["0", "cable", "steady"]
        late = result["late_involved_busy_period"]
        self.assertEqual((late["packets"], late["waiting_packets"], late["waiting_wire_bytes"]), (3,2,200))
        self.assertEqual(late["wait"]["max_ns"], 180)
        self.assertEqual(late["signed_residual"]["min_ns"], -180)
        self.assertEqual(result["modeled"]["wait"]["n"], 4)
        self.assertTrue(result["gate_uses_original_expected_release"])

    def test_link_service_uses_exact_fraction_without_per_packet_rounding(self):
        self.registration["ports"][0]["link_bps"] = 3_000_000_000
        serializer = Serialization(self.registration)
        for seq in range(3):
            handle = serializer.enqueue(expected(seq, arrival_ns=0, release_ns=0))
            serializer.observe(handle, 0)
        serializer.advance(0, 1)
        result = serializer.finish()["0", "cable", "steady"]
        self.assertEqual(result["modeled"]["wait"]["max_ns"], 534)
        self.assertEqual(result["egress_tail_ns"], dict(numerator=800, denominator=1))

    def test_actual_attempt_serializer_has_a_separate_gate_time_origin(self):
        serializer = Serialization(self.registration)
        for seq in range(2):
            serializer.attempt(expected(seq, arrival_ns=0, release_ns=0), 1000)
        result = serializer.finish()["0", "cable", "steady"]
        self.assertEqual(result["actual_attempt"]["wait"]["max_ns"], 100)
        self.assertEqual(result["actual_attempt"]["origin"], "verified actual attempt gate_ns")
        self.assertEqual(result["modeled"]["wait"]["n"], 0)


if __name__=="__main__":
    unittest.main()
