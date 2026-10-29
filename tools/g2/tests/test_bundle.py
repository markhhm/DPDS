# SPDX-License-Identifier: Apache-2.0
"""Complete synthetic streams exercise the default bounded oracle boundary."""
import copy
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from tools.g2.protocol import StreamWriter
from tools.g2.reference import Reference, canonical
from tools.g2.tests.test_calibration import document
from tools.g2.tests.test_counters import OBSERVER_FIELDS, PORT_FIELDS, snapshot
from tools.g2.tests.test_reference import config
from tools.g2.tests.test_registration import manifest

try:
    from tools.g2.bundle import build_bundle_report
except ImportError:
    build_bundle_report = None


def fixture(root, count=400, mutate=None, mode="stream"):
    cfg, reg, cal = config(), manifest(), document()
    cfg["lines"][0]["model"].update(p="0", delta_ns=0)
    reg["sampling"]["oversample_apps"] = []
    reg["required_cells"][0].update(mask_bits=0, planned_survivors=400)
    reg["capacity"].update(identity_index_capacity=count+10, max_unresolved=32)
    reg["retention"]["mode"] = mode
    for mapping in cal["mappings"]:
        mapping.update(scale_num=1, scale_den=1, target_origin_ns=0,
                       valid_to_raw=10**12, residual_bound_ns=0)
    for name in ("offset_bound_ns", "drift_bound_ns", "quantization_bound_ns",
                 "load_bound_ns", "mapping_bound_ns", "uncertainty_ns"):
        cal["pairs"][0][name] = 0
    cal["clocks"].append(dict(clock_id="engine", kind="fake", unit="ns", width_bits=64,
                              device_token="fake-engine", mode="synthetic"))
    rows = {role:[] for role in ("ingress", "expected", "before", "receiver", "host", "counters")}
    ref = Reference(cfg, "demo")
    for seq in range(count):
        packet = dict(kind="packet", run="demo", stream_id="ingress", seq=seq+1,
            ingress_port=0, ingress_seq=seq, line="cable", selection_seq=seq,
            external_identity=f"f:{seq}", flow_id="f", arrival_ns=100+seq*1000,
            length=60, vlan_id=100, segments=1,
            length_flags=dict(fcs_present=False, vlan_retained=True), app=None,
            selected_late=None, input_error=None)
        want = ref.consume(packet)
        rows["ingress"].append(packet)
        rows["expected"].append(want)
        identity = {name:want[name] for name in ("ingress_port", "ingress_seq", "external_identity", "line")}
        state = dict(seen_next_ingress_seq={"0":seq+1}, frontiers={"0":dict(
            through_ns=want["release_ns"], input_complete=False)})
        rows["host"] += [dict(kind="offer", **identity, at_ns=want["arrival_ns"], actual_selection_seq=seq),
            dict(kind="dispatch", at_ns=want["release_ns"], egress_port=1, handoff_queue_id="h",
                 first_handoff_seq=seq, identities=[identity], **state),
            dict(kind="attempt", egress_port=1, queue_id=0, attempt_id=seq,
                 gate_ns=want["release_ns"], submit_before_ns=want["release_ns"],
                 submit_after_ns=want["release_ns"]+1, consumed_dispatch={"h":seq+1},
                 offered=[dict(identity, gate_ns=want["release_ns"], handoff_queue_id="h", handoff_seq=seq)],
                 accepted_count=1, **state)]
        common = {name:want[name] for name in ("ingress_port", "egress_port", "line", "external_identity",
                                              "flow_id", "length", "vlan_id", "app", "selected_late")}
        for role, clock, mapping, time_key, ordinal, time in (
                ("before", "before", "b", "ingress_ts_ns", "before_seq", want["arrival_ns"]),
                ("receiver", "after", "a", "receive_ns", "receive_seq", want["release_ns"]+3)):
            rows[role].append(dict(common, kind="before" if role=="before" else "receive", **{
                time_key:time, ordinal:seq}, timestamp=dict(raw=time, clock_id=clock,
                    mapping_id=mapping, point="fake", uncertainty_ns=0)))
    end = dict(kind="end", run="demo", stream_id="ingress", seq=count+1,
               ingress_port=0, next_ingress_seq=count)
    rows["ingress"].append(end)
    ref.consume(end)
    summary = ref.finish()
    summary["schema"] = "hng-g2-reference-v1"
    rows["expected"].append(summary)
    for phase in ("start", "end"):
        values = {name:0 for name in PORT_FIELDS}
        if phase=="end":
            values.update(rx_returned=count, tx_accepted=count)
        rows["counters"].append(snapshot("port", "0", phase, values, at_ns=0 if phase=="start" else 10**9))
    for role in ("ingress", "before", "receiver", "host"):
        for phase in ("start", "end"):
            values = {name:0 for name in OBSERVER_FIELDS}
            values.update(queue_capacity=4096)
            if phase=="end":
                values.update(records_attempted=len(rows[role]), records_emitted=len(rows[role]))
            rows["counters"].append(snapshot("observer", role, phase, values,
                                             at_ns=0 if phase=="start" else 10**9))
    if mutate:
        mutate(rows, reg)
    for row in rows["counters"]:
        if row["scope"]=="observer" and row["phase"]=="end" and not row["values"]["records_dropped"]:
            row["values"].update(records_attempted=len(rows[row["scope_id"]]),
                                 records_emitted=len(rows[row["scope_id"]]))
    sources = {}
    for role, events in rows.items():
        path = root / f"{role}.jsonl"
        writer = StreamWriter(path, "demo", role, role, "engine" if role not in {"before", "receiver"} else "obs")
        digest = hashlib.sha256()
        for event in events:
            values = {key:value for key,value in event.items() if key not in {"kind", "run", "stream_id", "seq"}}
            if event["kind"]=="summary":
                values["expected_sha256"] = digest.hexdigest()
            written = writer.emit(event["kind"], **values)
            if event["kind"]=="expected":
                digest.update(canonical(written)+b"\n")
        writer.finish()
        sources[role] = path
        next(stream for stream in reg["streams"] if stream["role"]==role)["file_sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    raw = json.dumps(cfg).encode()
    (root/"engine.json").write_bytes(raw)
    reg["config"]["sha256"] = hashlib.sha256(raw).hexdigest()
    (root/"calibration.json").write_text(json.dumps(cal))
    (root/"manifest.json").write_text(json.dumps(reg))
    return cfg, reg, sources, cal


class BundleTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue(callable(build_bundle_report), "default bounded stream integration is missing")
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)

    def run_bundle(self, **changes):
        cfg, reg, sources, cal = fixture(self.root, **changes)
        return build_bundle_report(cfg, {}, reg, sources, cal)

    def test_complete_streams_produce_final_report_and_raw_corrected_distributions(self):
        result = self.run_bundle()
        self.assertEqual((result["schema"], result["status"]), ("hng-g2-report-v1", "PASS"))
        row = result["rows"][0]
        self.assertEqual(row["timing"]["main"]["n"], 400)
        self.assertEqual(row["timing"]["main"]["raw_owd"]["min_ns"], 97)
        self.assertEqual(row["timing"]["main"]["corrected_owd"]["max_ns"], 94)
        self.assertEqual(row["counts"]["probe_sent"], 0)
        self.assertLessEqual(result["observer_cost"]["active_join_peak"], 32)
        self.assertTrue(result["identity_index"]["released"])
        self.assertFalse(result["g2_qualified"])

    def test_receive_wire_order_is_independent_of_export_order(self):
        result = self.run_bundle(mutate=lambda rows, reg: rows["receiver"].reverse())
        self.assertEqual(result["status"], "INVALID")
        self.assertIn("receive_order_capacity", result["reason_codes"])

    def test_pair_export_reordering_within_bound_does_not_create_order_failure(self):
        def reorder(rows, reg):
            rows["receiver"][0], rows["receiver"][1] = rows["receiver"][1], rows["receiver"][0]
        self.assertEqual(self.run_bundle(mutate=reorder)["status"], "PASS")

    def test_complete_missing_survivor_is_proved_loss(self):
        result = self.run_bundle(mutate=lambda rows, reg: rows["receiver"].pop())
        self.assertEqual(result["status"], "FAIL")
        self.assertIn("missing_delivery", result["rows"][0]["observed_failures"])

    def test_observer_gap_prevents_missing_survivor_from_becoming_datapath_loss(self):
        def fault(rows, reg):
            rows["receiver"].pop()
            rows["counters"][-1]["values"].update(records_dropped=1, records_attempted=1201)
        result = self.run_bundle(mutate=fault)
        self.assertEqual(result["rows"][0]["measurement_status"], "INCONCLUSIVE")
        self.assertNotIn("missing_delivery", result["rows"][0]["observed_failures"])

    def test_independent_reference_rejects_a_forged_expected_deadline(self):
        def fault(rows, reg):
            rows["expected"][10]["release_ns"] += 1
        result = self.run_bundle(mutate=fault)
        self.assertEqual(result["status"], "INVALID")
        self.assertIn("reference_decision_mismatch", result["reason_codes"])

    def test_unknown_duplicate_and_forbidden_delivery_have_distinct_classes(self):
        def duplicate(rows, reg):
            row = copy.deepcopy(rows["receiver"][-1])
            row["receive_seq"] += 1
            rows["receiver"].append(row)
        result = self.run_bundle(mutate=duplicate)
        self.assertEqual(result["status"], "FAIL")
        self.assertIn("duplicate_delivery", result["rows"][0]["observed_failures"])

    def test_unknown_receiver_identity_invalidates_identity_evidence(self):
        def unknown(rows, reg):
            rows["receiver"][-1]["external_identity"] = "never-ingressed"
        result = self.run_bundle(mutate=unknown)
        self.assertEqual(result["status"], "INVALID")
        self.assertIn("receiver_unknown_identity", result["reason_codes"])

    def test_complete_wire_swap_fails_even_when_export_lines_stay_in_ingress_order(self):
        def swap(rows, reg):
            rows["receiver"][0]["receive_seq"], rows["receiver"][1]["receive_seq"] = 1, 0
        result = self.run_bundle(mutate=swap)
        self.assertEqual(result["status"], "FAIL")
        self.assertIn("unexpected_order", result["rows"][0]["observed_failures"])

    def test_counter_terminal_identity_counts_cannot_excuse_missing_attempts(self):
        def fault(rows, reg):
            rows["host"] = [row for row in rows["host"] if row["kind"]!="attempt"]
        result = self.run_bundle(mutate=fault)
        self.assertEqual(result["rows"][0]["measurement_status"], "INCONCLUSIVE")
        self.assertIn("missing_tx_evidence", result["reason_codes"])

    def test_selected_pair_without_before_is_inconclusive_even_with_receiver_timestamp(self):
        result = self.run_bundle(mutate=lambda rows, reg: rows["before"].pop())
        self.assertEqual(result["rows"][0]["measurement_status"], "INCONCLUSIVE")
        self.assertIn("before_missing", result["reason_codes"])

    def test_declared_pooled_row_cannot_qualify_from_one_run(self):
        def pooled(rows, reg):
            reg["required_cells"][0].update(repeat="pooled", pooled_repeats=[0, 1])
        result = self.run_bundle(mutate=pooled)
        self.assertEqual(result["status"], "INVALID")
        self.assertIn("pooled_repeat_evidence_unavailable", result["rows"][0]["reason_codes"])

    def test_sample_and_full_retention_have_identical_gates_and_keep_no_unsampled_packets(self):
        cfg, reg, sources, cal = fixture(self.root, count=2000)
        reg["required_cells"][0].update(mask_bits=10, planned_survivors=440_433)
        destination = self.root/"retained"
        destination.mkdir()
        sampled = build_bundle_report(cfg, {}, reg, sources, cal, destination)
        reg["retention"]["mode"] = "full"
        full = build_bundle_report(cfg, {}, reg, sources, cal)
        self.assertEqual(sampled["rows"], full["rows"])
        self.assertTrue((destination/"online.json").exists())
        for stream in sampled["stream_integrity"]:
            self.assertEqual(stream["producer_records_sha256"], stream["oracle_records_sha256"])
        selected = set()
        for row in (destination/"ingress.jsonl").read_text().splitlines():
            event = json.loads(row)
            if event["kind"]=="packet":
                selected.add(event["external_identity"])
        self.assertGreater(len(selected), 0)
        self.assertLess(len(selected), 10)
        after = [json.loads(row) for row in (destination/"receiver.jsonl").read_text().splitlines()]
        self.assertEqual({row["external_identity"] for row in after if row["kind"]=="receive"}, selected)

    def test_mismatch_cap_exhaustion_retains_exact_counts_and_blocks_pass(self):
        def fault(rows, reg):
            reg["retention"]["mismatch_cap"] = 1
            for i in range(2):
                extra = copy.deepcopy(rows["receiver"][-1])
                extra["receive_seq"] = 400+i
                rows["receiver"].append(extra)
        result = self.run_bundle(mutate=fault)
        self.assertEqual(result["rows"][0]["measurement_status"], "INCONCLUSIVE")
        self.assertIn("mismatch_cap_exhausted", result["rows"][0]["reason_codes"])
        self.assertEqual(result["mismatches"]["counts"]["duplicate_delivery"], 2)

    def test_join_lag_bound_expiry_is_inconclusive_and_never_silently_evicts(self):
        def fault(rows, reg):
            reg["capacity"]["max_join_lag_ns"] = 1
        result = self.run_bundle(mutate=fault)
        self.assertEqual(result["status"], "INVALID")
        self.assertIn("join_lag_exceeded", result["reason_codes"])

    def test_index_capacity_invalidates_a_numeric_failure_and_releases_memory(self):
        def fault(rows, reg):
            reg["capacity"]["identity_index_capacity"] = 2
            rows["receiver"][-1]["receive_ns"] += 1_000_000
            rows["receiver"][-1]["timestamp"]["raw"] += 1_000_000
        result = self.run_bundle(mutate=fault)
        self.assertEqual(result["rows"][0]["measurement_status"], "INCONCLUSIVE")
        self.assertGreater(result["identity_index"]["join_overflow"], 0)
        self.assertEqual(result["identity_index"]["live_entries"], 0)

    def test_cli_schema_error_still_writes_all_requested_outputs(self):
        fixture(self.root)
        (self.root/"receiver.jsonl").write_text("invalid\n")
        out = self.root/"result"
        command = [sys.executable, "-m", "tools.g2.report", *[str(self.root/name) for name in (
            "engine.json", "expected.jsonl", "receiver.jsonl", "counters.jsonl", "manifest.json")], "--out", str(out)]
        result = subprocess.run(command, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 2)
        self.assertTrue({"report.json", "results.csv", "summary.txt"}<={path.name for path in out.iterdir()})


if __name__ == "__main__":
    unittest.main()
