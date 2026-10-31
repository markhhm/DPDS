# SPDX-License-Identifier: Apache-2.0
"""Complete synthetic streams exercise the default bounded oracle boundary."""
import copy
import csv
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from tools.g2.protocol import StreamWriter
from tools.g2.protocol import StreamReader
from tools.g2.counters import normalize_counters
from tools.g2.reference import Reference, canonical
from tools.g2.tests.test_calibration import document
from tools.g2.tests.test_counters import OBSERVER_FIELDS, PORT_FIELDS, snapshot
from tools.g2.tests.test_reference import config
from tools.g2.tests.test_registration import manifest

try:
    from tools.g2.bundle import build_bundle_report
except ImportError:
    build_bundle_report = None


def fixture(root, count=400, mutate=None, mode="stream", lines=1, run="demo"):
    cfg, reg, cal = config(), manifest(), document()
    reg["run"] = cal["run"] = run
    # Extra lines share the first line's ingress and egress port and differ by VLAN.
    names, vlans = ["cable", "leo"][:lines], {"cable": 100, "leo": 200}
    for name in names[1:]:
        cfg["lines"].append(dict(copy.deepcopy(cfg["lines"][0]), name=name, vlan_id=vlans[name]))
        reg["lines"].append(dict(reg["lines"][0], name=name, vlan_id=vlans[name]))
        reg["required_cells"].append(dict(reg["required_cells"][0], line=name))
    for line in cfg["lines"]:
        line["model"].update(p="0", delta_ns=0)
    reg["sampling"]["oversample_apps"] = []
    for cell in reg["required_cells"]:
        cell.update(mask_bits=0, planned_survivors=count//lines)
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
    ref = Reference(cfg, run)
    for seq in range(count):
        packet = dict(kind="packet", run=run, stream_id="ingress", seq=seq+1,
            ingress_port=0, ingress_seq=seq, line=names[seq%lines], selection_seq=seq//lines,
            external_identity=f"f:{seq}", flow_id="f", arrival_ns=100+seq*1000,
            length=60, vlan_id=vlans[names[seq%lines]], segments=1,
            length_flags=dict(fcs_present=False, vlan_retained=True), app=None,
            selected_late=None, input_error=None)
        want = ref.consume(packet)
        rows["ingress"].append(packet)
        rows["expected"].append(want)
        identity = {name:want[name] for name in ("ingress_port", "ingress_seq", "external_identity", "line")}
        state = dict(seen_next_ingress_seq={"0":seq+1}, frontiers={"0":dict(
            through_ns=want["release_ns"], input_complete=False)})
        rows["host"] += [dict(kind="offer", **identity, at_ns=want["arrival_ns"], actual_selection_seq=seq//lines),
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
    end = dict(kind="end", run=run, stream_id="ingress", seq=count+1,
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
    sources, input_integrity = {}, []
    for role, events in rows.items():
        path = root / f"{role}.jsonl"
        writer = StreamWriter(path, run, role, role, "engine" if role not in {"before", "receiver"} else "obs")
        digest = hashlib.sha256()
        for event in events:
            values = {key:value for key,value in event.items() if key not in {"kind", "run", "stream_id", "seq"}}
            if event["kind"]=="summary":
                values["expected_sha256"] = digest.hexdigest()
                values.setdefault("input_streams", input_integrity)
                values.setdefault("trace_sha256", {})
                values.setdefault("config_file_sha256", hashlib.sha256(json.dumps(cfg).encode()).hexdigest())
            written = writer.emit(event["kind"], **values)
            if event["kind"]=="expected":
                digest.update(canonical(written)+b"\n")
        seal = writer.finish()
        sources[role] = path
        next(stream for stream in reg["streams"] if stream["role"]==role)["file_sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
        if role=="ingress":
            input_integrity.append(dict(stream_id=role, role=role,
                file_sha256=hashlib.sha256(path.read_bytes()).hexdigest(), records_sha256=seal["records_sha256"],
                record_count=seal["record_count"],complete=True,dropped_records=0,issues=[]))
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
        self.assertTrue({"counters", "q", "serialization", "clamp", "outage", "clock",
                         "offered_bps_l1", "delivered_bps_l1", "delivered_bps_l2", "pps", "l7_bps"}<=set(row))
        self.assertEqual(row["shared_q_drops"], 0)
        self.assertEqual(row["probe_sent"], 0)
        self.assertEqual(row["serialization"]["modeled"]["wait"]["n"], 400)
        self.assertEqual(row["serialization"]["actual_attempt"]["wait"]["n"], 400)

    def test_two_lines_sharing_one_egress_port_pass_with_per_line_cells(self):
        result = self.run_bundle(count=800, lines=2)
        self.assertEqual(result["status"], "PASS", result.get("reason_codes"))
        self.assertEqual(sorted((row["line"], row["timing"]["main"]["n"]) for row in result["rows"]),
                         [("cable", 400), ("leo", 400)])

    def test_shared_egress_dispatch_out_of_release_order_is_not_a_pass(self):
        def swap(rows, reg):
            dispatch = [row for row in rows["host"] if row["kind"]=="dispatch"][10:12]
            attempt = [row for row in rows["host"] if row["kind"]=="attempt"][10:12]
            dispatch[0]["identities"], dispatch[1]["identities"] = dispatch[1]["identities"], dispatch[0]["identities"]
            for row, other in zip(attempt, attempt[::-1]):
                row["offered"] = [dict(other["offered"][0], handoff_seq=row["offered"][0]["handoff_seq"],
                                       gate_ns=row["offered"][0]["gate_ns"])]
        result = self.run_bundle(count=800, lines=2, mutate=swap)
        self.assertNotEqual(result["status"], "PASS")

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
            for index, time in ((0, 2001), (1, 2000)):
                rows["receiver"][index]["receive_ns"] = time
                rows["receiver"][index]["timestamp"]["raw"] = time
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

    def test_clock_regression_is_checked_in_wire_order(self):
        def regress(rows, reg):
            rows["receiver"][1]["receive_ns"] = 190
            rows["receiver"][1]["timestamp"]["raw"] = 190
        result = self.run_bundle(mutate=regress)
        self.assertIn("CLOCK_BACKWARD", result["reason_codes"])
        self.assertEqual(result["rows"][0]["measurement_status"], "INCONCLUSIVE")

    def test_reference_summary_end_counts_are_verified(self):
        def fault(rows, reg):
            rows["expected"][-1]["end_counts"] = {"0":399}
        result = self.run_bundle(mutate=fault)
        self.assertEqual(result["status"], "INVALID")
        self.assertIn("reference_summary_mismatch", result["reason_codes"])

    def test_reference_summary_must_bind_full_ingress_marker_streams(self):
        def fault(rows, reg):
            rows["expected"][-1]["input_streams"] = []
        result = self.run_bundle(mutate=fault)
        self.assertEqual(result["status"], "INVALID")
        self.assertIn("reference_input_stream_mismatch", result["reason_codes"])

    def test_input_end_certificate_is_available_to_last_dispatch(self):
        def ended(rows, reg):
            for row in rows["host"][-2:]:
                row["frontiers"]["0"].update(input_complete=True, through_ns=None)
        self.assertEqual(self.run_bundle(mutate=ended)["status"], "PASS")

    def test_counter_endpoints_must_cover_the_complete_host_drain(self):
        def early(rows, reg):
            for row in rows["counters"]:
                if row["phase"]=="end":
                    row["at_ns"] = 100
        result = self.run_bundle(mutate=early)
        self.assertEqual(result["status"], "INVALID")
        self.assertIn("counter_window_incomplete", result["reason_codes"])

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
        error = json.loads((out/"report.json").read_text())
        self.assertTrue({"run", "condition_id", "sources", "rows", "directions", "stream_integrity",
                         "observer_cost", "registration", "unavailable"}<=set(error))

    def test_cli_csv_has_stable_columns_and_numeric_cell_counters(self):
        fixture(self.root)
        out = self.root/"result"
        command = [sys.executable, "-m", "tools.g2.report", *[str(self.root/name) for name in (
            "engine.json", "expected.jsonl", "receiver.jsonl", "counters.jsonl", "manifest.json")], "--out", str(out)]
        result = subprocess.run(command, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stdout+result.stderr)
        with (out/"results.csv").open() as stream:
            reader = csv.DictReader(stream)
            self.assertEqual(",".join(reader.fieldnames), "condition_id,repeat,direction,line,phase,status,measurement_status,g2_pass,g2_qualified,error_median_ms,error_p99_ms,delay_storage_drops,capture_drops,nic_drops,shared_q_status,shared_q_drops,trace_loss_drops,trace_outage_drops,probe_sent,probe_received,probe_lost,probe_selected_late,probe_actual_ooo,probe_selected_late_ooo,probe_unexpected_ooo,timing_samples,reasons")
            row = next(reader)
            self.assertEqual((row["capture_drops"],row["shared_q_drops"],row["probe_sent"],row["timing_samples"]),
                             ("0","0","0","400"))
            self.assertEqual(row["nic_drops"], "")
        before = {path.name:path.read_bytes() for path in out.iterdir()}
        again = subprocess.run(command, capture_output=True, text=True, timeout=10)
        self.assertEqual(again.returncode, 2)
        self.assertEqual({path.name:path.read_bytes() for path in out.iterdir()}, before)

    def test_preflight_error_report_has_final_shape_and_registered_invalid_rows(self):
        fixture(self.root)
        (self.root/"engine.json").write_text("{}")
        out = self.root/"result"
        command = [sys.executable, "-m", "tools.g2.report", *[str(self.root/name) for name in (
            "engine.json", "expected.jsonl", "receiver.jsonl", "counters.jsonl", "manifest.json")], "--out", str(out)]
        result = subprocess.run(command, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 2)
        report = json.loads((out/"report.json").read_text())
        self.assertEqual(report["run"], "demo")
        self.assertEqual(report["rows"][0]["status"], "INVALID")
        self.assertTrue({"sources","directions","stream_integrity","observer_cost","registration","unavailable"}<=set(report))

    def test_normalized_counter_positional_view_is_verified_against_the_sealed_snapshots(self):
        _, registration, sources, _ = fixture(self.root)
        reader = StreamReader(sources["counters"])
        normalized = normalize_counters(reader,registration)
        normalized["streams"] = [reader.integrity]
        view = self.root/"counters.json"
        view.write_text(json.dumps(normalized,indent=2))
        command = [sys.executable,"-m","tools.g2.report",*[str(self.root/name) for name in (
            "engine.json","expected.jsonl","receiver.jsonl","counters.json","manifest.json")]]
        result = subprocess.run(command,capture_output=True,text=True,timeout=10)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        normalized["ports"]["0"]["tx_accepted"] -= 1
        view.write_text(json.dumps(normalized))
        result = subprocess.run(command,capture_output=True,text=True,timeout=10)
        self.assertEqual(result.returncode,2)
        self.assertIn("COUNTER_VIEW_MISMATCH",json.loads(result.stdout)["reason_codes"])


if __name__ == "__main__":
    unittest.main()
