# SPDX-License-Identifier: Apache-2.0
"""Exercise the final reference boundary using sealed bytes and real CLI calls."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from tools.g2.protocol import StreamReader
from tools.g2.protocol import StreamError
from tools.g2.tests.test_protocol import canonical, packet, stream
from tools.g2.tests.test_reference import config


class ReferenceV1Tests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.config = config()

    def invoke(self, rows=None, raw=None):
        source = self.root / "ingress.jsonl"
        source.write_bytes(stream(rows) if raw is None else raw)
        configuration = self.root / "engine.json"
        configuration.write_text(json.dumps(self.config))
        result = subprocess.run([sys.executable, "-m", "tools.g2.reference",
                                 str(configuration), "demo", str(source)],
                                capture_output=True, text=True, timeout=10)
        output = self.root / "expected.jsonl"
        output.write_text(result.stdout)
        return result, output, source, configuration

    def test_cli_emits_independent_envelope_and_complete_v1_summary(self):
        row = packet(vlan_id=100, selected_late=1)
        end = dict(kind="end", run="demo", stream_id="ingress-0", seq=2,
                   ingress_port=0, next_ingress_seq=1)
        result, output, source, configuration = self.invoke([row, end])
        self.assertEqual(result.returncode, 0, result.stderr)
        reader = StreamReader(output, role="expected", run="demo")
        decision, summary = list(reader)
        self.assertEqual((reader.header["stream_id"], decision["seq"], summary["seq"]),
                         ("expected", 1, 2))
        self.assertEqual((decision["service_ns"], decision["release_ns"], decision["q_bytes"]),
                         (131, 242, 104))
        self.assertEqual(summary["schema"], "hng-g2-reference-v1")
        self.assertEqual(summary["end_counts"], {"0": 1})
        self.assertEqual(summary["line_counts"], {"cable": dict(offers=1, accepted=1,
            q_drop=0, trace_loss=0, trace_outage=0, invalid_input=0)})
        self.assertEqual(summary["trace_sha256"], {})
        self.assertEqual(summary["config_file_sha256"], hashlib.sha256(configuration.read_bytes()).hexdigest())
        self.assertEqual(summary["expected_sha256"], hashlib.sha256(canonical(decision)).hexdigest())
        self.assertEqual(summary["input_streams"][0]["file_sha256"], hashlib.sha256(source.read_bytes()).hexdigest())
        self.assertEqual(summary["input_streams"][0]["record_count"], 2)
        self.assertEqual(summary["resource_peaks"], dict(q_ledger_packets=1, q_bytes=104, q_drain_ns=124))
        self.assertTrue(reader.integrity["complete"])

    def test_invalid_input_keeps_port_ordinal_without_consuming_line_service(self):
        rows = [packet(vlan_id=100),
                packet(seq=2, ingress_seq=1, line=None, selection_seq=None, length=0,
                       vlan_id=0, arrival_ns=200, input_error="unclassified"),
                packet(seq=3, ingress_seq=2, selection_seq=1, arrival_ns=300, vlan_id=100),
                dict(kind="end", run="demo", stream_id="ingress-0", seq=4,
                     ingress_port=0, next_ingress_seq=3)]
        result, output, _, _ = self.invoke(rows)
        self.assertEqual(result.returncode, 0, result.stderr)
        first, invalid, last, summary = list(StreamReader(output, role="expected"))
        self.assertEqual([r["outcome"] for r in (first, invalid, last)],
                         ["accepted", "invalid_input", "accepted"])
        self.assertEqual((invalid["ingress_seq"], invalid["service_ns"], invalid["release_ns"]),
                         (1, None, None))
        self.assertEqual((last["selection_seq"], last["service_ns"], last["release_ns"]), (1, 424, 434))
        self.assertEqual(summary["end_counts"], {"0": 3})
        self.assertEqual(summary["line_counts"]["cable"]["offers"], 2)
        self.assertEqual(summary["event_count"], 3)

    def test_invalid_classified_frame_reports_queue_at_its_arrival(self):
        rows = [packet(vlan_id=100),
                packet(seq=2, ingress_seq=1, selection_seq=None, length=1519,
                       vlan_id=100, arrival_ns=200, input_error="frame_length"),
                dict(kind="end", run="demo", stream_id="ingress-0", seq=3,
                     ingress_port=0, next_ingress_seq=2)]
        result, output, _, _ = self.invoke(rows)
        self.assertEqual(result.returncode, 0, result.stderr)
        _, invalid, summary = list(StreamReader(output, role="expected"))
        self.assertEqual((invalid["q_bytes"], invalid["q_drain_ns"]), (0, 0))
        self.assertEqual(summary["line_counts"]["cable"]["invalid_input"], 1)

    def test_one_percent_gap_without_drop_counter_cannot_seal_complete_output(self):
        rows = [packet(seq=i+1, ingress_seq=i, selection_seq=i, arrival_ns=i*1000,
                       vlan_id=100, external_identity=str(i)) for i in range(100)]
        rows.append(dict(kind="end", run="demo", stream_id="ingress-0", seq=101,
                         ingress_port=0, next_ingress_seq=100))
        raw = stream(rows).splitlines(keepends=True)
        del raw[50]
        result, output, _, _ = self.invoke(raw=b"".join(raw))
        self.assertEqual(result.returncode, 2)
        self.assertIn("sequence is not contiguous", result.stderr)
        self.assertFalse(json.loads(output.read_text().splitlines()[-1])["complete"])
        with self.assertRaises(StreamError) as caught:
            list(StreamReader(output, role="expected"))
        self.assertEqual(caught.exception.measurement_status, "INCONCLUSIVE")

    def test_host_rejection_with_intact_input_is_not_an_intended_model_drop(self):
        rows = [packet(vlan_id=100, input_error="host_reject"),
                dict(kind="end", run="demo", stream_id="ingress-0", seq=2,
                     ingress_port=0, next_ingress_seq=1)]
        result, output, _, _ = self.invoke(rows)
        self.assertEqual(result.returncode, 0, result.stderr)
        decision, _ = list(StreamReader(output, role="expected"))
        self.assertEqual(decision["outcome"], "accepted")

    def test_host_line_cannot_override_raw_vlan_mapping(self):
        rows = [packet(vlan_id=100, line="other"),
                dict(kind="end", run="demo", stream_id="ingress-0", seq=2,
                     ingress_port=0, next_ingress_seq=1)]
        result, _, _, _ = self.invoke(rows)
        self.assertEqual(result.returncode, 2)
        self.assertIn("raw port/VLAN mapping", result.stderr)
