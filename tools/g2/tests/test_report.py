# SPDX-License-Identifier: Apache-2.0
import copy
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from tools.g2.reference import canonical
from tools.g2.report import build_report, timing_sample, timing_stats
from tools.g2.tests.test_reference import config


def sealed_summary(rows, cfg):
    return dict(kind="summary", schema="hng-g2-reference-draft-v1", run="fixture",
                input_complete=True, event_count=len(rows), ingress_sha256="1"*64,
                config_sha256=hashlib.sha256(canonical(cfg)).hexdigest(),
                expected_sha256=hashlib.sha256(b"".join(canonical(row)+b"\n" for row in rows)).hexdigest())


class ReportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cfg = config()
        cls.cfg["lines"][0]["model"].update(p="0", t_ns=1_000_000, delta_ns=0,
                                              storage_capacity_packets=32768, storage_capacity_bytes=100_000_000)
        cls.manifest = dict(scope="software-only", timing_mode="stress", sampling_seed=17,
                            oversample_apps=[], clock=dict(common=True, uncertainty_ns=0),
                            egress_bps={"1": 8_000_000_000})
        cls.expected, cls.received = [], []
        candidate = 0
        while len(cls.expected) < 600:
            row = dict(run="fixture", ingress_port=0, external_identity=str(candidate))
            candidate += 1
            if not timing_sample(row, 17):
                continue
            seq = len(cls.expected)
            arrival = seq * 1_000_000
            row.update(kind="expected", line="cable", ingress_seq=seq, selection_seq=seq,
                       arrival_ns=arrival, length=76, l2_bytes=80, wire_bytes=100, egress_port=1,
                       outcome="accepted", service_ns=arrival+100, deadline_ns=arrival+1_000_100,
                       release_ns=arrival+1_000_100, late=False, clamp_extra_ns=0, q_bytes=80, q_drain_ns=100)
            cls.expected.append(row)
            cls.received.append(dict(run="fixture", ingress_port=0, external_identity=row["external_identity"],
                                     receive_ns=arrival+1_000_100))
        cls.summary = sealed_summary(cls.expected, cls.cfg)
        cls.counters = {"0": dict(rx_returned=600, tx_accepted=600, still_owned=0,
            q_drop=0, trace_loss_drop=0, trace_outage_drop=0, delay_storage_drop=0,
            invalid_input_drop=0, handoff_drop=0, terminal_tx_drop=0,
            missed_deadline_outage_drop=0, shutdown_drop=0, observer_drop_packets=0)}

    def run_report(self, received=None, counters=None, manifest=None, expected=None, summary=None):
        if expected is not None and summary is None:
            summary = sealed_summary(expected, self.cfg)
        return build_report(self.expected if expected is None else expected,
                            self.summary if summary is None else summary,
                            self.received if received is None else received,
                            self.counters if counters is None else counters,
                            self.cfg, self.manifest if manifest is None else manifest)

    def test_clean_run_has_four_gates_ci_bytes_drain_time_and_software_label(self):
        result = self.run_report()
        self.assertEqual(result["status"], "PASS")
        self.assertFalse(result["g2_qualified"])
        self.assertEqual(result["scope"], "software-only, not G2")
        direction = result["directions"]["0"]
        self.assertEqual([gate["status"] for gate in direction["gates"].values()], ["PASS"] * 4)
        self.assertEqual(direction["timing"]["main"]["n"], 600)
        self.assertEqual(direction["timing"]["main"]["p99_abs_error_ns"], 0)
        self.assertEqual(direction["q"]["cable"]["peak_l2_bytes"], 80)
        self.assertEqual(direction["q"]["cable"]["peak_drain_ns_at_c"], 100)
        self.assertEqual(result["rows"][0]["status"], "PASS")
        self.assertEqual(result["rows"][0]["error_p99_ms"], 0)
        self.assertEqual(result["stage1_source_commit"], "de035a63f5d0135ee70b0bceb27d5051897b85a1")

    def test_point_quantiles_follow_stage1_linear_interpolation(self):
        stats = timing_stats([0, 10, 20, 30])
        self.assertEqual(stats["median_abs_error_ns"], 15)
        self.assertAlmostEqual(stats["p99_abs_error_ns"], 29.7)

    def test_capture_invalid_maps_to_stage1_invalid_and_nic_loss_fails(self):
        counters = copy.deepcopy(self.counters)
        counters["0"]["observer_drop_packets"] = 6
        result = self.run_report(received=self.received[:-6], counters=counters)
        self.assertEqual(result["rows"][0]["status"], "INVALID")
        counters = copy.deepcopy(self.counters)
        counters["0"]["nic_drops"] = 1
        self.assertEqual(self.run_report(counters=counters)["status"], "FAIL")

    def test_swapped_ordinary_packets_fail_order(self):
        received = copy.deepcopy(self.received)
        received[40], received[41] = received[41], received[40]
        result = self.run_report(received=received)
        self.assertEqual(result["status"], "FAIL")
        self.assertEqual(result["directions"]["0"]["gates"]["order"]["status"], "FAIL")

    def test_storage_loss_is_not_reclassified_as_observer_loss(self):
        counters = copy.deepcopy(self.counters)
        counters["0"].update(delay_storage_drop=1, tx_accepted=599)
        result = self.run_report(received=self.received[:-1], counters=counters)
        self.assertEqual(result["status"], "FAIL")
        self.assertEqual(result["directions"]["0"]["gates"]["storage"]["status"], "FAIL")

    def test_p99_injection_crosses_fixed_half_millisecond_gate(self):
        received = copy.deepcopy(self.received)
        for row in received[-30:]:
            row["receive_ns"] += 600_000
        result = self.run_report(received=received)
        self.assertEqual(result["directions"]["0"]["gates"]["p99"]["status"], "FAIL")
        self.assertEqual(result["directions"]["0"]["timing"]["main"]["p99_abs_error_ns"], 600_000)

    def test_point_two_ms_does_not_falsify_point_five_ms_threshold(self):
        received = copy.deepcopy(self.received)
        for row in received[-30:]:
            row["receive_ns"] += 200_000
        result = self.run_report(received=received)
        self.assertEqual(result["directions"]["0"]["gates"]["p99"]["status"], "PASS")
        self.assertEqual(result["directions"]["0"]["timing"]["main"]["p99_abs_error_ns"], 200_000)
        for row in received[-30:]:
            row["receive_ns"] += 400_000
        self.assertEqual(self.run_report(received=received)["directions"]["0"]["gates"]["p99"]["status"], "FAIL")

    def test_one_percent_observer_loss_is_inconclusive(self):
        counters = copy.deepcopy(self.counters)
        counters["0"]["observer_drop_packets"] = 6
        result = self.run_report(received=self.received[:-6], counters=counters)
        self.assertEqual(result["status"], "INCONCLUSIVE")

    def test_unknown_counters_clock_uncertainty_and_missing_evidence_cannot_pass(self):
        manifest = copy.deepcopy(self.manifest)
        manifest["clock"]["uncertainty_ns"] = 100_001
        self.assertEqual(self.run_report(manifest=manifest)["status"], "INCONCLUSIVE")
        counters = copy.deepcopy(self.counters)
        counters["0"]["observer_drop_packets"] = None
        self.assertEqual(self.run_report(counters=counters)["status"], "INCONCLUSIVE")
        self.assertEqual(self.run_report(received=[])["status"], "INCONCLUSIVE")
        summary = dict(self.summary, input_complete=False)
        self.assertEqual(self.run_report(summary=summary)["status"], "INCONCLUSIVE")

    def test_sparse_timing_cannot_claim_population_p99_bound(self):
        received = copy.deepcopy(self.received)
        for row in received[2:]:
            del row["receive_ns"]
        self.assertEqual(self.run_report(received=received)["status"], "INCONCLUSIVE")

    def test_full_sequence_detects_duplicate_identity(self):
        received = self.received + [self.received[0]]
        result = self.run_report(received=received)
        self.assertEqual(result["status"], "INCONCLUSIVE")
        self.assertEqual(result["rows"][0]["status"], "INVALID")

    def test_foreign_receiver_records_and_changed_probe_flag_invalidate_evidence(self):
        extra = dict(self.received[0], ingress_port=9)
        self.assertEqual(self.run_report(received=self.received+[extra])["status"], "INCONCLUSIVE")
        received = copy.deepcopy(self.received)
        received[0]["selected_late"] = True
        result = self.run_report(received=received)
        self.assertEqual(result["rows"][0]["status"], "INVALID")
        self.assertEqual(result["rows"][0]["probe_flag_mismatches"], 1)

    def test_wrong_counter_shape_is_invalid_without_traceback(self):
        with self.assertRaises(ValueError):
            self.run_report(counters=[])
        with self.assertRaises(ValueError):
            self.run_report(counters={"0": []})

    def test_changed_expected_timing_and_wrong_run_summary_are_invalid(self):
        expected = copy.deepcopy(self.expected)
        for row in expected[-30:]:
            row["deadline_ns"] += 600_000
            row["release_ns"] += 600_000
        with self.assertRaisesRegex(ValueError, "expected.*hash|seal"):
            self.run_report(expected=expected, summary=self.summary)
        for change in [dict(run="wrong-run"), dict(ingress_sha256="not-a-hash"), dict(expected_sha256="0"*64)]:
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.run_report(summary=dict(self.summary, **change))

    def test_negative_owd_invalidates_evidence_with_small_absolute_error(self):
        received = copy.deepcopy(self.received)
        for row in received:
            row["receive_ns"] -= 1_000_101
            row["receive_ns"] = max(0, row["receive_ns"])
        result = self.run_report(received=received)
        self.assertEqual(result["status"], "INCONCLUSIVE")
        self.assertEqual(result["rows"][0]["status"], "INVALID")

    def test_modeled_trace_hold_uses_release_for_expected_owd(self):
        cfg = copy.deepcopy(self.cfg)
        cfg["lines"][0]["trace"] = dict(path="registered", sha256="0"*64)
        cfg["lines"][0]["model"]["t_ns"] = 0
        trace = dict(end_ns=10**12, terminal="error", terminal_horizon_ns=None,
                     loss_seed=0, outage_action="hold",
                     rows=[dict(at_ns=0, t_ns=1_000_000, loss="0", outage=False),
                           dict(at_ns=1_000_100, t_ns=1_000_000, loss="0", outage=True),
                           dict(at_ns=2_000_100, t_ns=1_000_000, loss="0", outage=False)])
        expected, received = copy.deepcopy(self.expected), copy.deepcopy(self.received)
        expected[0]["release_ns"] = received[0]["receive_ns"] = 2_000_100
        summary = sealed_summary(expected, cfg)
        result = build_report(expected, summary, received, self.counters, cfg, self.manifest, {"cable": trace})
        self.assertEqual(result["status"], "PASS")
        self.assertEqual(result["directions"]["0"]["timing"]["main"]["p99_abs_error_ns"], 0)

    def test_late_collision_serialization_is_reported_without_changing_error(self):
        expected = copy.deepcopy(self.expected)
        expected[0]["late"] = True
        expected[1]["deadline_ns"] = expected[0]["deadline_ns"]
        expected[1]["release_ns"] = expected[0]["release_ns"]
        received = copy.deepcopy(self.received)
        received[1]["receive_ns"] = received[0]["receive_ns"] + 100
        result = self.run_report(expected=expected, received=received)
        collision = result["directions"]["0"]["serialization"]
        self.assertEqual(collision["late_collision_packets"], 1)
        self.assertEqual(collision["late_collision_max_ns"], 100)
        self.assertEqual(collision["main_expected_serialization_ns"]["n"], 600)
        self.assertTrue(collision["gate_uses_original_deadline"])

    def test_nonfinite_or_negative_clock_and_counter_data_are_invalid(self):
        manifest = copy.deepcopy(self.manifest)
        manifest["clock"]["uncertainty_ns"] = float("nan")
        with self.assertRaises(ValueError):
            self.run_report(manifest=manifest)
        counters = copy.deepcopy(self.counters)
        counters["0"]["delay_storage_drop"] = -1
        with self.assertRaises(ValueError):
            self.run_report(counters=counters)

    def test_reorder_of_unsampled_packets_is_still_detected(self):
        expected, received = copy.deepcopy(self.expected), copy.deepcopy(self.received)
        identities = []
        for i in range(10000):
            row = dict(run="fixture", ingress_port=0, external_identity=f"unsampled-{i}")
            if not timing_sample(row, 17):
                identities.append(row["external_identity"])
                if len(identities) == 2:
                    break
        for index in range(2):
            expected[index]["external_identity"] = received[index]["external_identity"] = identities[index]
        received[0], received[1] = received[1], received[0]
        result = self.run_report(expected=expected, received=received)
        self.assertEqual(result["directions"]["0"]["gates"]["order"]["status"], "FAIL")

    def test_reference_and_report_cli_contract_and_invalid_exit(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            def save(name, value):
                path = root / name
                path.write_text(json.dumps(value))
                return str(path)
            cfg = save("config.json", self.cfg)
            ingress = root / "ingress.jsonl"
            fields = ("run", "ingress_port", "line", "ingress_seq", "selection_seq", "external_identity", "arrival_ns", "length")
            events = [dict(kind="packet", **{key: row[key] for key in fields}) for row in self.expected]
            events.append(dict(kind="end", run="fixture", ingress_port=0, next_ingress_seq=600))
            ingress.write_text("".join(json.dumps(row) + "\n" for row in events))
            ref = subprocess.run([sys.executable, "-m", "tools.g2.reference", cfg, "fixture", str(ingress)], capture_output=True, text=True)
            self.assertEqual(ref.returncode, 0, ref.stderr)
            output = root / "expected.jsonl"
            output.write_text(ref.stdout)
            receiver = root / "receiver.jsonl"
            receiver.write_text("".join(json.dumps(row) + "\n" for row in self.received))
            command = [sys.executable, "-m", "tools.g2.report", cfg, str(output), str(receiver),
                       save("counters.json", self.counters), save("manifest.json", self.manifest)]
            result = subprocess.run(command, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(json.loads(result.stdout)["status"], "PASS")
            destination = root / "report"
            result = subprocess.run(command + ["--out", str(destination)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertTrue((destination / "results.csv").is_file())
            self.assertIn("PASS", (destination / "summary.txt").read_text())
            receiver.write_text('{"run":"fixture","run":"duplicate"}\n')
            result = subprocess.run(command, capture_output=True, text=True)
            self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
            self.assertEqual(json.loads(result.stdout)["status"], "INVALID")
