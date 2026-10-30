# SPDX-License-Identifier: Apache-2.0
import os
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from tools.g2.compare import compare
from tools.g2.reference import Reference


def executable():
    default = Path(__file__).resolve().parents[3] / "dpds-core/target/release/examples/m2_replay"
    return Path(os.environ.get("G2_CORE_REPLAY", default)).resolve()


def model(**changes):
    return dict(dict(c_bps=8_000_000_000, q_bytes=160, t_ns=0, delta_ns=0,
                     p="0", seed=0, storage_capacity_packets=4096,
                     storage_capacity_bytes=4096 * 1518), **changes)


def config(models):
    return dict(schema="hng-v1", lines=[dict(name=f"line-{i}", ingress_port=i // 2,
                egress_port=1 - i // 2, vlan_id=100 + i % 2, model=m, trace=None,
                envelope=dict(min_q_bytes=64, max_q_bytes=1522, burst_bound_packets=16))
                for i, m in enumerate(models)])


def replay(header, events, *, check=True):
    result = subprocess.run([str(executable())],
                            input="".join(json.dumps(row) + "\n" for row in [header, *events]),
                            capture_output=True, text=True, check=check)
    return [json.loads(line) for line in result.stdout.splitlines()] if check else result


class DifferentialTests(unittest.TestCase):
    def test_legacy_replay_reports_exact_drain_and_q_reject_selection(self):
        rows = replay(dict(model=model(c_bps=3_000_000_000, p="1")), [
            dict(ingress_seq=i, arrival_ns=arrival, length=length)
            for i, (arrival, length) in enumerate([(0, 76), (0, 76), (0, 1518), (267, 1518)])])
        admissions = [row for row in rows if row["kind"] == "admission"]
        self.assertEqual([row.get("q_drain_ns") for row in admissions], [267, 534, 534, 267])
        self.assertEqual([row.get("late") for row in admissions], [True] * 4)
        self.assertEqual([row.get("selection_seq") for row in admissions], list(range(4)))
        self.assertEqual([row["outcome"] for row in admissions], ["accepted"] * 2 + ["q_drop"] * 2)
        self.assertEqual([row for row in rows if row["kind"] == "output"], [
            dict(kind="output", ingress_seq=i, selection_seq=i, service_ns=service,
                 deadline_ns=service, release_ns=service, late=True, clamp_extra_ns=0,
                 l2_bytes=80, wire_bytes=100) for i, service in enumerate([267, 534])])

    def test_shared_egress_uses_certified_frontier_and_end_in_both_directions(self):
        cfg = config([model(t_ns=10_000, delta_ns=32_000, p="1"), model(), model(), model()])
        events = [dict(kind="packet", line=i, ingress_port=i // 2, ingress_seq=i % 2,
                       arrival_ns=0, length=76) for i in range(4)]
        events += [dict(kind="frontier", ingress_port=0, through_ns=99, next_ingress_seq=2, now_ns=100_000),
                   dict(kind="frontier", ingress_port=0, through_ns=100, next_ingress_seq=2, now_ns=100_000),
                   dict(kind="end", ingress_port=0, next_ingress_seq=2, now_ns=100_000),
                   dict(kind="frontier", ingress_port=1, through_ns=100, next_ingress_seq=2, now_ns=100_000),
                   dict(kind="end", ingress_port=1, next_ingress_seq=2, now_ns=100_000)]
        rows = replay(dict(schema="hng-m2-replay-v1", config=cfg), events)
        outputs = [row for row in rows if row["kind"] == "output"]
        self.assertEqual([(row["line"], row["release_ns"], row["trigger"]) for row in outputs],
                         [(1, 100, 5), (0, 42_100, 6), (2, 100, 7), (3, 100, 7)])
        # Popping at a later wall time must not change the admission ledger.
        markers = [row for row in rows if row["kind"] == "marker"]
        self.assertEqual(markers[0]["lines"][0]["ledger_watermark_ns"], 0)
        self.assertEqual(markers[0]["lines"][0]["q_drain_ns"], 100)
        self.assertTrue(all(row["q_bytes"] == 0 and row["q_drain_ns"] == 0
                            for row in rows[-1]["lines"]))

    def test_multiline_replay_rejects_uncertified_history(self):
        header = dict(schema="hng-m2-replay-v1", config=config([model()]))
        packet = dict(kind="packet", line=0, ingress_port=0, ingress_seq=0, arrival_ns=0, length=76)
        for events, message in [
            ([packet], "missing end"),
            ([dict(kind="end", ingress_port=0, next_ingress_seq=1, now_ns=0)], "marker sequence"),
            ([dict(kind="frontier", ingress_port=0, through_ns=0, next_ingress_seq=0, now_ns=0), packet],
             "packet history"),
            ([dict(kind="end", ingress_port=0, next_ingress_seq=0, now_ns=0), packet], "after end"),
        ]:
            with self.subTest(message=message):
                result = replay(header, events, check=False)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(message, result.stderr)

    def test_software_storage_fault_is_actual_loss_and_never_intended_loss(self):
        # Software fault fixture: deliberately violate the registered storage envelope.
        actual_model = model(t_ns=1000, p="1", storage_capacity_packets=1)
        events = [dict(ingress_seq=i, arrival_ns=t, length=76)
                  for i, t in enumerate([0, 100, 1100, 1101])]
        rows = replay(dict(model=actual_model), events)
        admissions = [row for row in rows if row["kind"] == "admission"]
        self.assertEqual([row["outcome"] for row in admissions],
                         ["accepted", "storage_drop", "storage_drop", "accepted"])
        self.assertEqual([row.get("q_drain_ns") for row in admissions], [100, 0, 0, 100])
        intended = Reference(config([model(t_ns=1000, p="1")]), "storage-fault")
        expected = [intended.consume(dict(event, kind="packet", run="storage-fault",
                    ingress_port=0, line="line-0", selection_seq=event["ingress_seq"],
                    external_identity=str(event["ingress_seq"]))) for event in events]
        intended.consume(dict(kind="end", run="storage-fault", ingress_port=0, next_ingress_seq=4))
        summary = intended.finish()
        self.assertEqual([row["outcome"] for row in expected], ["accepted"] * 4)
        self.assertEqual(summary["line_counts"]["line-0"]["accepted"], 4)
        self.assertEqual([(row["ingress_seq"], row["service_ns"]) for row in rows if row["kind"] == "output"],
                         [(0, 100), (3, 1201)])
        self.assertEqual(expected[-1]["service_ns"], 1300)

    def test_software_byte_storage_fault_remains_an_intended_survivor(self):
        # Software fault fixture: a 76-byte packet exceeds the actual 75-byte store.
        events = [dict(ingress_seq=i, arrival_ns=i * 100, length=76) for i in range(3)]
        rows = replay(dict(model=model(storage_capacity_bytes=75)), events)
        self.assertEqual([(row["outcome"], row["q_bytes"], row.get("q_drain_ns")) for row in rows],
                         [("storage_drop", 0, 0)] * 3)
        intended = Reference(config([model()]), "byte-storage-fault")
        expected = [intended.consume(dict(event, kind="packet", run="byte-storage-fault",
                    ingress_port=0, line="line-0", selection_seq=event["ingress_seq"],
                    external_identity=str(event["ingress_seq"]))) for event in events]
        intended.consume(dict(kind="end", run="byte-storage-fault", ingress_port=0, next_ingress_seq=3))
        self.assertEqual(intended.finish()["line_counts"]["line-0"]["accepted"], 3)
        self.assertEqual([row["outcome"] for row in expected], ["accepted"] * 3)

    def test_legacy_diagnostics_use_actual_selector_after_invalid_and_storage_faults(self):
        # Software fault fixture: invalid offers consume no selector; storage faults do.
        events = [dict(ingress_seq=i, arrival_ns=t, length=length)
                  for i, (t, length) in enumerate([(0, 76), (100, 0), (100, 0),
                                                   (100, 76), (1100, 76), (1101, 76)])]
        rows = replay(dict(model=model(t_ns=1000, p="0.5", seed=3, storage_capacity_packets=1)), events)
        admissions = [row for row in rows if row["kind"] == "admission"]
        self.assertEqual([row["outcome"] for row in admissions],
                         ["accepted", "invalid_input", "invalid_input", "storage_drop", "storage_drop", "accepted"])
        self.assertEqual([row["selection_seq"] for row in admissions], [0, 1, 1, 1, 2, 3])
        self.assertEqual([row["late"] for row in admissions], [True, False, False, False, False, True])
        outputs = [row for row in rows if row["kind"] == "output"]
        self.assertEqual([(row["ingress_seq"], row["selection_seq"], row["late"]) for row in outputs],
                         [(0, 0, True), (5, 3, True)])

    def test_optimized_python_cannot_disable_mismatch_detection(self):
        with tempfile.TemporaryDirectory() as directory:
            bad = Path(directory) / "bad-core"
            bad.write_text('#!/bin/sh\ncat >/dev/null\nprintf \'%s\\n\' \'{"kind":"admission","ingress_seq":0,"outcome":"q_drop","q_bytes":0}\'\n')
            bad.chmod(0o755)
            command = [sys.executable, "-O", "-c",
                       "import sys; from tools.g2.compare import compare; compare(sys.argv[1], total=32, seeds=32)", str(bad)]
            result = subprocess.run(command, capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("mismatch", result.stderr)

    def test_optimized_python_detects_corrupted_real_drain_and_q_reject_selection(self):
        with tempfile.TemporaryDirectory() as directory:
            adapter = Path(directory) / "corrupt-core"
            for field in ("q_drain_ns", "late"):
                adapter.write_text(
                    f"#!{sys.executable}\nimport json, subprocess, sys\n"
                    f"result = subprocess.run([{str(executable())!r}], input=sys.stdin.buffer.read(), "
                    "capture_output=True, check=True)\nchanged = False\n"
                    "for line in result.stdout.splitlines():\n    row = json.loads(line)\n"
                    f"    if not changed and row['kind'] == 'admission' and "
                    f"({field!r} != 'late' or row['outcome'] == 'q_drop'):\n"
                    f"        row[{field!r}] = "
                    f"(not row['late']) if {field!r} == 'late' else row[{field!r}] + 1\n"
                    "        changed = True\n    print(json.dumps(row))\n"
                    "if not changed:\n    raise RuntimeError('corruption fixture found no admission')\n")
                adapter.chmod(0o755)
                result = subprocess.run([sys.executable, "-O", "-c",
                    "import sys; from tools.g2.compare import compare; compare(sys.argv[1], total=4096, seeds=32)",
                    str(adapter)], capture_output=True, text=True)
                with self.subTest(field=field):
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn("admission mismatch", result.stderr)
                    self.assertIn(f"field={field}", result.stderr)

    def test_million_ingress_events_match_production_core(self):
        default = Path(__file__).resolve().parents[3] / "dpds-core/target/release/examples/m2_replay"
        executable = Path(os.environ.get("G2_CORE_REPLAY", default))
        result = compare(executable, total=1 << 20, seeds=32)
        self.assertEqual(result["events"], 1048576)
        self.assertEqual(result["cases"], 32)
        self.assertEqual(result["mismatches"], 0)
        self.assertEqual(result["coverage"]["directions"], [[0, 1], [1, 0]])
        self.assertEqual(result["coverage"]["fixed_states"], [[0, 0], [0, 32000], [6500, 0], [6500, 32000]])
        self.assertEqual(result["coverage"]["trace_actions"], ["drop", "hold"])
        self.assertEqual(result["coverage"]["shared_egress_cases"], 32)
        for name in ("frontiers", "ends", "q_drop_late", "trace_holds", "clamped", "outputs_before_end"):
            self.assertGreater(result["coverage"][name], 0, name)
        print(json.dumps(result, sort_keys=True))
