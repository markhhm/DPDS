# SPDX-License-Identifier: Apache-2.0
"""Typed start/end evidence is independent of packet-model results."""
import copy
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from tools.g2.protocol import StreamWriter, TERMINALS
from tools.g2.tests.test_registration import manifest

try:
    from tools.g2.counters import normalize_counters
except ImportError:
    normalize_counters = None


PORT_FIELDS = {"rx_returned", "tx_accepted", "still_owned"} | TERMINALS
OBSERVER_FIELDS = {"records_attempted", "records_emitted", "records_dropped", "capture_drops",
                   "queue_high_water", "queue_capacity", "malformed_records", "identity_errors",
                   "timestamp_missing", "clock_invalid"}


def snapshot(scope, token, phase, values, generation=0, **changes):
    gauges = {"still_owned", "queue_high_water", "queue_capacity"}
    row = dict(kind="counter", run="demo", stream_id="counters", seq=1,
               scope=scope, scope_id=token, phase=phase, at_ns=0 if phase=="start" else 100,
               clock_id="engine", reset_generation=generation, values=values,
               descriptors={name:dict(unit="packets" if scope=="port" else "events",
                   semantics="gauge" if name in gauges else "terminal_disjoint",
                   source="software", status="measured", reason=None) for name in values})
    row.update(changes)
    return row


def records():
    start = {name:0 for name in PORT_FIELDS}
    start.update(rx_returned=1000, tx_accepted=900, q_drop=100)
    end = dict(start, rx_returned=1010, tx_accepted=909, q_drop=101)
    rows = [snapshot("port", "0", "start", start), snapshot("port", "0", "end", end)]
    for token in ("ingress", "before", "receiver", "host"):
        values = {name:0 for name in OBSERVER_FIELDS}
        values.update(queue_capacity=4096)
        rows += [snapshot("observer", token, "start", values),
                 snapshot("observer", token, "end", dict(values, records_attempted=10,
                     records_emitted=10, queue_high_water=4))]
    return [dict(row, seq=i+1) for i,row in enumerate(rows)]


class CounterTests(unittest.TestCase):
    def test_deltas_exclude_pre_run_counts_and_preserve_terminal_descriptors(self):
        self.assertTrue(callable(normalize_counters), "typed counter normalization is missing")
        result = normalize_counters(iter(records()), manifest())
        self.assertEqual(result["schema"], "hng-g2-counters-v1")
        self.assertEqual(result["measurement_status"], "PASS")
        self.assertEqual(result["ports"]["0"]["rx_returned"], 10)
        self.assertEqual(result["ports"]["0"]["tx_accepted"], 9)
        self.assertEqual(result["ports"]["0"]["q_drop"], 1)
        self.assertEqual(result["observer"]["receiver"]["queue_high_water"], 4)
        self.assertEqual(result["descriptors"]["port:0"]["q_drop"]["semantics"], "terminal_disjoint")

    def test_normalized_endpoints_retain_clock_window_and_reset_state(self):
        result = normalize_counters(records(), manifest())
        self.assertIn("endpoints", result)
        self.assertEqual(result["endpoints"]["port:0"], dict(start_ns=0, end_ns=100,
            start_clock_id="engine", end_clock_id="engine", start_generation=0,
            end_generation=0, reset_seen=False))

    def test_endpoint_order_and_descriptor_changes_are_structurally_invalid(self):
        changes = [lambda rows: rows.append(copy.deepcopy(rows[1])),
                   lambda rows: rows[1].update(at_ns=0),
                   lambda rows: rows[1]["descriptors"]["rx_returned"].update(unit="bytes")]
        for index, change in enumerate(changes):
            rows = records()
            if index==1:
                rows[0]["at_ns"] = 1
            change(rows)
            result = normalize_counters(rows, manifest())
            with self.subTest(index=index):
                self.assertEqual(result["measurement_status"], "INVALID")

    def test_reset_or_regression_cannot_fabricate_zero_loss(self):
        for reset in (True, False):
            rows = records()
            if reset:
                rows[1]["reset_generation"] = 1
                rows[1]["values"] = dict(rows[0]["values"])
            else:
                rows[1]["values"]["q_drop"] = 0
            result = normalize_counters(rows, manifest())
            self.assertEqual(result["measurement_status"], "INCONCLUSIVE")
            self.assertIsNone(result["ports"]["0"]["q_drop"])
            self.assertIn("counter_reset" if reset else "counter_regression", result["reason_codes"])

    def test_missing_endpoints_or_required_health_cannot_pass(self):
        for rows in ([], records()[1:], records()[:-2]):
            result = normalize_counters(rows, manifest())
            self.assertEqual(result["measurement_status"], "INCONCLUSIVE")
            self.assertIn("counter_endpoint_missing", result["reason_codes"])
        rows = records()
        for row in rows:
            row["values"].pop("timestamp_missing", None)
            row["descriptors"].pop("timestamp_missing", None)
        result = normalize_counters(rows, manifest())
        self.assertIn("required_counter_missing", result["reason_codes"])
        self.assertEqual(result["measurement_status"], "INCONCLUSIVE")

    def test_unsupported_hardware_diagnostics_stay_null_and_are_not_added_to_drops(self):
        rows = records()
        for row in rows[:2]:
            row["values"]["nic_missed"] = None
            row["descriptors"]["nic_missed"] = dict(unit="packets", semantics="overlapping_diagnostic",
                source="software", status="not_applicable", reason="software_only")
        result = normalize_counters(rows, manifest())
        self.assertEqual(result["measurement_status"], "PASS")
        self.assertIsNone(result["ports"]["0"]["nic_missed"])
        rows[1]["values"]["q_drop"] = None
        rows[1]["descriptors"]["q_drop"].update(status="unsupported", reason="not_available")
        result = normalize_counters(rows, manifest())
        self.assertEqual(result["measurement_status"], "INCONCLUSIVE")
        self.assertIsNone(result["ports"]["0"]["q_drop"])

    def test_observer_drop_imbalance_or_overflow_invalidates_evidence(self):
        changes = [dict(records_dropped=1, records_emitted=9),
                   dict(records_emitted=9), dict(queue_high_water=4097), dict(clock_invalid=1)]
        for change in changes:
            rows = records()
            rows[-1]["values"].update(change)
            result = normalize_counters(rows, manifest())
            with self.subTest(change=change):
                self.assertEqual(result["measurement_status"], "INCONCLUSIVE")

    def test_periodic_reset_is_latched_even_if_end_generation_returns_to_start(self):
        rows = records()
        middle = copy.deepcopy(rows[0])
        middle.update(phase="periodic", at_ns=50, reset_generation=1)
        rows.insert(1, middle)
        result = normalize_counters(rows, manifest())
        self.assertEqual(result["measurement_status"], "INCONCLUSIVE")
        self.assertIsNone(result["ports"]["0"]["rx_returned"])

    def test_terminal_semantics_and_complete_drain_are_required(self):
        rows = records()
        for row in rows[:2]:
            row["descriptors"]["q_drop"]["semantics"] = "overlapping_diagnostic"
        result = normalize_counters(rows, manifest())
        self.assertEqual(result["measurement_status"], "INVALID")
        self.assertIn("counter_terminal_semantics", result["reason_codes"])
        rows = records()
        rows[1]["values"]["still_owned"] = 1
        result = normalize_counters(rows, manifest())
        self.assertEqual(result["ports"]["0"]["still_owned"], 1)
        self.assertEqual(result["measurement_status"], "INCONCLUSIVE")
        self.assertIn("counter_drain_incomplete", result["reason_codes"])
        rows[1]["values"].update(still_owned=0, tx_accepted=908)
        result = normalize_counters(rows, manifest())
        self.assertIn("counter_conservation", result["reason_codes"])

    def test_required_packet_and_observer_counters_cannot_be_redeclared_as_gauges(self):
        for scope, name in [("port", "rx_returned"), ("observer", "records_dropped")]:
            rows = records()
            for row in rows:
                if row["scope"] == scope:
                    row["descriptors"][name]["semantics"] = "gauge"
            result = normalize_counters(rows, manifest())
            with self.subTest(scope=scope):
                self.assertEqual(result["measurement_status"], "INVALID")

    def test_periodic_queue_overflow_and_scope_capacity_are_latched(self):
        rows = records()
        middle = copy.deepcopy(rows[-1])
        middle.update(phase="periodic", at_ns=50)
        middle["values"]["queue_high_water"] = 4097
        rows.insert(-1, middle)
        result = normalize_counters(rows, manifest())
        self.assertIn("observer_queue_overflow", result["reason_codes"])
        registration = manifest()
        registration["capacity"]["max_unresolved"] = 1
        result = normalize_counters(records(), registration)
        self.assertIn("counter_scope_capacity", result["reason_codes"])
        self.assertEqual(result["measurement_status"], "INCONCLUSIVE")

    def test_cli_checks_registered_snapshot_hash_and_keeps_stream_seal(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "counters.jsonl"
            writer = StreamWriter(source, "demo", "counters", "counters", "engine")
            for row in records():
                writer.emit("counter", **{key:value for key,value in row.items()
                    if key not in {"kind", "run", "stream_id", "seq"}})
            writer.finish()
            registration = manifest()
            counter_stream = next(row for row in registration["streams"] if row["role"]=="counters")
            counter_stream["file_sha256"] = hashlib.sha256(source.read_bytes()).hexdigest()
            path = root / "manifest.json"
            path.write_text(json.dumps(registration))
            command = [sys.executable, "-m", "tools.g2.counters", str(path)]
            result = subprocess.run(command, capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            self.assertTrue(result.stdout, "counter CLI did not emit its evidence artifact")
            output = json.loads(result.stdout)
            self.assertEqual(output["ports"]["0"]["rx_returned"], 10)
            self.assertTrue(output["streams"][0]["complete"])
            self.assertEqual(output["streams"][0]["file_sha256"], counter_stream["file_sha256"])
            self.assertFalse(output["g2_qualified"])
            original = source.read_bytes()
            source.write_bytes(b"".join(json.dumps(json.loads(line)).encode()+b"\n" for line in original.splitlines()))
            result = subprocess.run(command, capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 2)
            self.assertIn("SOURCE_HASH_MISMATCH", json.loads(result.stdout)["reason_codes"])
            source.write_bytes(original.replace(b'"records_attempted":10', b'"records_attempted":11'))
            result = subprocess.run(command, capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 2)
            self.assertEqual(json.loads(result.stdout)["measurement_status"], "INVALID")
