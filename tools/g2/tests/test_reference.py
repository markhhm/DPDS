import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from tools.g2.reference import Reference, load_config, selection_word


def config():
    return {"schema": "hng-v1", "lines": [{
        "name": "cable", "ingress_port": 0, "egress_port": 1, "vlan_id": 100,
        "model": {"c_bps": 8_000_000_000, "q_bytes": 160, "t_ns": 10,
                  "delta_ns": 101, "p": "0.5", "seed": 3,
                  "storage_capacity_packets": 4096, "storage_capacity_bytes": 10_000_000},
        "envelope": {"min_q_bytes": 64, "max_q_bytes": 1522, "burst_bound_packets": 16},
        "trace": None}]}


def packet(seq, arrival=0, length=76, **changes):
    return dict(kind="packet", run="fixture", ingress_port=0, line="cable",
                ingress_seq=seq, selection_seq=seq, external_identity=str(seq),
                arrival_ns=arrival, length=length, **changes)


class ReferenceTests(unittest.TestCase):
    def test_independent_selector_vector(self):
        self.assertEqual(selection_word(0, 0), 0xe220a8397b1dcdaf)
        self.assertEqual(selection_word(0, (1 << 64) - 1), 0)

    def test_finite_q_tie_drop_selection_and_release_equality(self):
        ref = Reference(config(), "fixture")
        rows = [ref.consume(packet(i, a)) for i, a in enumerate([0, 0, 0, 100])]
        self.assertEqual([r["outcome"] for r in rows],
                         ["accepted", "accepted", "q_drop", "accepted"])
        self.assertEqual([(r["service_ns"], r["deadline_ns"]) for r in rows if r["outcome"] == "accepted"],
                         [(100, 211), (200, 210), (300, 411)])
        self.assertEqual([r["q_bytes"] for r in rows], [80, 160, 160, 160])
        self.assertEqual([r["q_drain_ns"] for r in rows], [100, 200, 200, 200])
        # Design-check vectors: ordinal 2=0x9ceb... (ordinary), 3=0x12a7... (late).
        self.assertEqual([r["late"] for r in rows if r["outcome"] == "accepted"], [True, False, True])

    def test_fraction_does_not_accumulate_per_packet_rounding(self):
        cfg = config()
        cfg["lines"][0]["model"].update(c_bps=3_000_000_000, q_bytes=1000, p="0", delta_ns=0)
        ref = Reference(cfg, "fixture")
        rows = [ref.consume(packet(i)) for i in range(3)]
        self.assertEqual([r["service_ns"] for r in rows], [267, 534, 800])

    def test_q_and_selection_are_independent_per_direction_and_line(self):
        cfg = config()
        other = copy.deepcopy(cfg["lines"][0])
        other.update(name="reverse", ingress_port=1, egress_port=0)
        cfg["lines"].append(other)
        ref = Reference(cfg, "fixture")
        ref.consume(packet(0))
        row = packet(0)
        row.update(line="reverse", ingress_port=1)
        out = ref.consume(row)
        self.assertEqual((out["q_bytes"], out["service_ns"], out["late"]), (80, 100, True))

    def test_frontier_end_and_input_gaps_cannot_silently_pass(self):
        ref = Reference(config(), "fixture")
        ref.consume(packet(0))
        with self.assertRaises(ValueError):
            ref.finish()
        with self.assertRaises(ValueError):
            ref.consume(packet(2))
        ref.consume(dict(kind="frontier", run="fixture", ingress_port=0, through_ns=99, next_ingress_seq=1))
        with self.assertRaises(ValueError):
            ref.consume(packet(1, 98))
        ref.consume(packet(1, 100))
        ref.consume(dict(kind="end", run="fixture", ingress_port=0, next_ingress_seq=2))
        self.assertTrue(ref.finish()["input_complete"])
        with self.assertRaises(ValueError):
            ref.consume(packet(2, 200))

    def test_trace_down_step_loss_and_outage_consumes_service(self):
        cfg = config()
        cfg["lines"][0]["model"].update(q_bytes=1000, t_ns=0, delta_ns=0, p="0")
        cfg["lines"][0]["trace"] = {"path": "fixture", "sha256": "0" * 64}
        trace = dict(end_ns=10000, terminal="error", terminal_horizon_ns=None,
                     outage_action="hold", loss_seed=0,
                     rows=[dict(at_ns=0, t_ns=200, loss="0", outage=False),
                           dict(at_ns=200, t_ns=0, loss="1", outage=False),
                           dict(at_ns=300, t_ns=0, loss="0", outage=True),
                           dict(at_ns=500, t_ns=0, loss="0", outage=False)])
        ref = Reference(cfg, "fixture", traces={"cable": trace})
        rows = [ref.consume(packet(i)) for i in range(4)]
        self.assertEqual([r["outcome"] for r in rows], ["accepted", "trace_loss", "accepted", "accepted"])
        self.assertEqual([r["service_ns"] for r in rows], [100, 200, 300, 400])
        self.assertEqual([r["release_ns"] for r in rows if r["outcome"] == "accepted"], [500, 500, 500])
        self.assertEqual(rows[2]["deadline_ns"], 300)
        self.assertEqual(rows[2]["q_bytes"], 240)

    def test_uncovered_trace_and_malformed_values_are_rejected(self):
        for field, value in [("c_bps", 0), ("p", "nan"), ("p", "1.1"), ("q_bytes", -1), ("seed", True)]:
            cfg = config()
            cfg["lines"][0]["model"][field] = value
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                Reference(cfg, "fixture")
        ref = Reference(config(), "fixture")
        bad = packet(0)
        bad["arrival_ns"] = 1.5
        with self.assertRaises(ValueError):
            ref.consume(bad)

    def test_loader_hashes_the_same_bytes_it_parses(self):
        fixture = Path(__file__).resolve().parents[3] / "dpds-core/tests/fixtures/trace.json"
        data = fixture.read_bytes()
        cfg = config()
        cfg["lines"][0]["trace"] = {"path": "trace.json", "sha256": hashlib.sha256(data).hexdigest()}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "trace.json").write_bytes(data)
            (root / "config.json").write_text(json.dumps(cfg))
            actual, traces = load_config(root / "config.json")
            self.assertEqual(actual, cfg)
            self.assertEqual(traces["cable"]["end_ns"], 1000)
            (root / "trace.json").write_bytes(data + b" ")
            with self.assertRaisesRegex(ValueError, "SHA-256"):
                load_config(root / "config.json")
