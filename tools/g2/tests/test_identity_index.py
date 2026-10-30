# SPDX-License-Identifier: Apache-2.0
"""A volatile index keeps only exact membership/outcome tags, never packet logs."""
import unittest
import gc
import json
import weakref
from tools.g2.online import OnlineCells

from tools.g2.tests.test_online import expected
from tools.g2.tests.test_registration import manifest

try:
    from tools.g2.identity_index import IdentityIndex
except ImportError:
    IdentityIndex = None


class IdentityIndexTests(unittest.TestCase):
    def registration(self, capacity=4):
        registration = manifest()
        registration["capacity"]["identity_index_capacity"] = capacity
        return registration

    def test_complete_membership_distinguishes_duplicate_forbidden_and_unknown_delivery(self):
        self.assertTrue(callable(IdentityIndex), "bounded volatile identity index is missing")
        index = IdentityIndex(self.registration())
        accepted = expected(0)
        dropped = expected(1, outcome="q_drop")
        self.assertEqual(index.remember(accepted)["class"], "indexed")
        self.assertEqual(index.remember(dropped)["class"], "indexed")
        self.assertEqual(index.receive(accepted, input_complete=True)["class"], "delivery")
        self.assertEqual(index.receive(accepted, input_complete=True)["class"], "duplicate_delivery")
        self.assertEqual(index.receive(dropped, input_complete=True)["class"], "delivered_intended_drop")
        self.assertEqual(index.receive(expected(2), input_complete=False)["class"], "pending")
        self.assertEqual(index.receive(expected(2), input_complete=True)["class"], "receiver_unknown_identity")
        report = index.finish()
        self.assertEqual(report["join_overflow"], 0)
        self.assertTrue(report["released"])

    def test_capacity_failure_taints_unknown_keys_without_evicting_known_outcomes(self):
        index = IdentityIndex(self.registration(2))
        first, second, rejected = (expected(i) for i in range(3))
        index.remember(first)
        index.remember(second)
        self.assertEqual(index.remember(rejected)["class"], "join_overflow")
        self.assertEqual(index.receive(rejected, input_complete=True)["class"], "join_overflow")
        self.assertEqual(index.receive(first, input_complete=True)["class"], "delivery")
        self.assertEqual(index.receive(first, input_complete=True)["class"], "duplicate_delivery")
        report = index.finish()
        self.assertEqual(report["peak_entries"], 2)
        self.assertEqual(report["join_overflow"], 2)
        self.assertEqual(report["cells"][0]["join_overflow"], 2)
        self.assertEqual(report["breakdown"], dict(allocation=1, unknown_after_discard=1))

    def test_overflow_marks_every_possible_phase_when_receiver_has_no_ingress_time(self):
        registration = self.registration(1)
        registration["windows"].append(dict(phase="drain", start_ns=10**9, end_ns=2*10**9, clock_id="engine"))
        registration["required_cells"].append(dict(registration["required_cells"][0], phase="drain"))
        index = IdentityIndex(registration)
        index.remember(expected(0))
        index.remember(expected(1))
        self.assertEqual(index.receive(expected(1), input_complete=True)["class"], "join_overflow")
        rows = {row["phase"]:row for row in index.finish()["cells"]}
        self.assertEqual(rows["steady"]["join_overflow"], 2)
        self.assertEqual(rows["drain"]["join_overflow"], 1)

    def test_overflow_prevents_cell_pass_and_takes_precedence_over_numeric_failure(self):
        for error in (0, 200_000):
            registration = self.registration(2)
            registration["required_cells"][0].update(mask_bits=0, planned_survivors=400)
            index = IdentityIndex(registration)
            cells = OnlineCells(registration)
            for seq in range(400):
                row = expected(seq)
                index.remember(row)
                index.receive(row, input_complete=True)
                cells.expect(row)
                cells.delivery(row, 1000+(error if seq%5<3 else 0), 0)
            result = cells.finish(identity_index=index.finish())
            row = result["rows"][0]
            self.assertEqual(row["gates"]["median"]["status"], "FAIL" if error else "PASS")
            self.assertEqual(row["counts"]["join_overflow"], 796)
            self.assertEqual(row["measurement_status"], "INCONCLUSIVE")
            self.assertIn("join_overflow", row["reason_codes"])
            self.assertFalse(result["g2_qualified"])

    def test_index_releases_packet_records_and_reports_memory_formula_without_identities(self):
        class Packet(dict):
            pass
        row = Packet(expected(0, external_identity="z"*128))
        row["unneeded_payload"] = bytearray(1_000_000)
        reference = weakref.ref(row)
        index = IdentityIndex(self.registration(2))
        index.remember(row)
        del row
        gc.collect()
        self.assertIsNone(reference(), "index retained a complete packet record")
        report = index.finish()
        self.assertNotIn("z"*128, json.dumps(report))
        self.assertIn("memory", report)
        memory = report["memory"]
        self.assertIn("capacity", memory["capacity_formula"])
        self.assertGreater(memory["peak_estimated_bytes"], 128)
        self.assertGreaterEqual(memory["estimated_capacity_bytes"], memory["peak_estimated_bytes"])
        self.assertEqual(index.live_entries, 0)
        self.assertTrue(report["released"])
        with self.assertRaises(ValueError):
            index.receive(expected(0), input_complete=True)

    def test_abort_context_clears_index_and_namespace_validation_is_strict(self):
        index = IdentityIndex(self.registration())
        with self.assertRaisesRegex(RuntimeError, "abort"):
            with index:
                index.remember(expected(0))
                raise RuntimeError("abort")
        self.assertEqual(index.live_entries, 0)
        for row in [expected(0, run="other"), expected(0, ingress_port=True),
                    expected(0, external_identity="x"*129), expected(0, outcome="made_up")]:
            with self.subTest(row=row), self.assertRaises(ValueError):
                IdentityIndex(self.registration()).remember(row)

    def test_index_requires_a_registered_limit(self):
        registration = manifest()
        registration["capacity"].pop("identity_index_capacity")
        with self.assertRaises(ValueError):
            IdentityIndex(registration)

    def test_index_report_cannot_rescue_another_run_or_omit_cells(self):
        registration = self.registration()
        report = IdentityIndex(registration).finish()
        for change in [dict(run="other"), dict(cells=[]), dict(released=False),
                       dict(cells=report["cells"]+report["cells"])]:
            with self.subTest(change=change), self.assertRaises(ValueError):
                OnlineCells(registration).finish(identity_index=dict(report, **change))
