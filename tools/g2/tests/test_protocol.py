# SPDX-License-Identifier: Apache-2.0
"""Interchange fault tests use real JSONL and independently constructed seals."""
import hashlib
import io
import json
from pathlib import Path
import tempfile
import subprocess
import sys
import unittest

from tools.g2.protocol import StreamError, StreamReader, StreamWriter


def canonical(row):
    return json.dumps(row, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
                      allow_nan=False).encode("ascii") + b"\n"


def packet(**changes):
    row = dict(kind="packet", run="demo", stream_id="ingress-0", seq=1,
               ingress_port=0, ingress_seq=0, line="cable", selection_seq=0,
               external_identity="f0:0", flow_id="f0", arrival_ns=7,
               length=100, vlan_id=101, segments=1,
               length_flags=dict(fcs_present=False, vlan_retained=True), app=None,
               selected_late=0, input_error=None)
    row.update(changes)
    return row


def stream(rows=None, role="ingress", **seal_changes):
    header = dict(kind="header", schema="hng-g2-events-v1", run="demo",
                  stream_id="ingress-0", role=role, producer="fixture",
                  producer_commit="synthetic", clock_id="engine", seq=0)
    if rows is None:
        rows = [packet(), dict(kind="end", run="demo", stream_id="ingress-0", seq=2,
                               ingress_port=0, next_ingress_seq=1)]
    body = b"".join(canonical(r) for r in [header] + rows)
    seal = dict(kind="seal", run="demo", stream_id="ingress-0", seq=len(rows)+1,
                record_count=len(rows), records_sha256=hashlib.sha256(body).hexdigest(),
                complete=True, dropped_records=0)
    seal.update(seal_changes)
    return body + canonical(seal)


class ProtocolTests(unittest.TestCase):
    def test_invalid_input_terminal_keeps_known_identity_with_an_unknown_line(self):
        from tools.g2.protocol import validate_record
        row=dict(kind="terminal",run="demo",stream_id="host",seq=1,
                 ingress_port=0,ingress_seq=0,external_identity="known",line=None,
                 at_ns=10,reason="invalid_input_drop")
        validate_record(row,"host")
        row["reason"]="handoff_drop"
        with self.assertRaises(StreamError):
            validate_record(row,"host")

    def read(self, data, **kwargs):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "events.jsonl"
            path.write_bytes(data)
            reader = StreamReader(path, **kwargs)
            rows = list(reader)
        return reader, rows

    def assert_error(self, data, status="INVALID", **kwargs):
        with self.assertRaises(StreamError) as caught:
            self.read(data, **kwargs)
        self.assertEqual(caught.exception.measurement_status, status)
        self.assertTrue(caught.exception.code)
        return caught.exception

    def test_reads_envelope_and_separate_canonical_and_file_hashes(self):
        raw = stream()
        # Reformat valid JSON to prove actual bytes and canonical bytes differ.
        reformatted = b"".join(json.dumps(json.loads(line), indent=None).encode()+b"\n"
                                for line in raw.splitlines())
        reader, rows = self.read(reformatted, role="ingress", run="demo")
        self.assertEqual((rows[0]["run"], rows[0]["stream_id"], rows[0]["seq"]),
                         ("demo", "ingress-0", 1))
        self.assertEqual(reader.header["clock_id"], "engine")
        self.assertEqual(reader.integrity["file_sha256"], hashlib.sha256(reformatted).hexdigest())
        self.assertEqual(reader.integrity["records_sha256"], reader.seal["records_sha256"])
        self.assertNotEqual(reader.integrity["records_sha256"], reader.integrity["file_sha256"])
        self.assertEqual(reader.integrity["record_count"], 2)
        self.assertTrue(reader.integrity["complete"])

    def test_final_summary_and_unknown_input_decisions_retain_evidence(self):
        row = packet(kind="expected", line=None, selection_seq=None, external_identity=None,
                     flow_id=None, input_error="unclassified", egress_port=None,
                     l2_bytes=104, wire_bytes=124, late=False, outcome="invalid_input",
                     service_ns=None, deadline_ns=None, release_ns=None, clamp_extra_ns=0,
                     q_bytes=0, q_drain_ns=0)
        summary = dict(kind="summary",run="demo",stream_id="ingress-0",seq=2,
                       schema="hng-g2-reference-v1",input_complete=True,event_count=1,
                       ingress_sha256="a"*64,expected_sha256="b"*64,config_sha256="c"*64,
                       trace_sha256={},input_streams=[],end_counts={"0":1},line_counts={},
                       oracle_version="independent-fraction-v1",resource_peaks={"packets":1})
        self.assertEqual(len(self.read(stream([row,summary],role="expected"))[1]),2)
        for changes in [dict(end_counts={"0":True}),dict(trace_sha256={"cable":"bad"}),
                        dict(line_counts={"cable":{"offers":True}}),dict(resource_peaks={"packets":-1}),
                        dict(input_streams=[{}]),dict(oracle_version="é")]:
            with self.subTest(changes=changes):
                self.assert_error(stream([row,dict(summary,**changes)],role="expected"))

    def test_counter_null_reason_is_valid_only_for_available_measurement(self):
        counter = dict(kind="counter",run="demo",stream_id="ingress-0",seq=1,
                       scope="port",scope_id="0",at_ns=10,clock_id="engine",reset_generation=0,
                       values={"rx_returned":1},descriptors={"rx_returned":dict(unit="packets",
                       semantics="overlapping_diagnostic",source="host",status="measured",reason=None)})
        self.assertEqual(self.read(stream([counter],role="counters"))[1][0]["values"],{"rx_returned":1})
        for changes in [dict(values={"rx_returned":None}),dict(reasons=[]),
                        dict(descriptors={"rx_returned":dict(counter["descriptors"]["rx_returned"],status="invented")})]:
            self.assert_error(stream([dict(counter,**changes)],role="counters"))

    def test_marker_tampering_is_covered_by_digest(self):
        self.assert_error(stream().replace(b'"next_ingress_seq":1', b'"next_ingress_seq":0'))
        frontier = dict(kind="frontier", run="demo", stream_id="ingress-0", seq=2,
                        ingress_port=0, through_ns=9, next_ingress_seq=1)
        end = dict(kind="end", run="demo", stream_id="ingress-0", seq=3,
                   ingress_port=0, next_ingress_seq=1)
        self.assert_error(stream([packet(), frontier, end]).replace(b'"through_ns":9', b'"through_ns":8'))

    def test_missing_seal_and_missing_end_are_incomplete(self):
        self.assert_error(b"\n".join(stream().splitlines()[:-1])+b"\n", "INCONCLUSIVE")
        self.assert_error(stream([packet()]), "INCONCLUSIVE")

    def test_capture_loss_and_gap_do_not_qualify(self):
        self.assert_error(stream(complete=False), "INCONCLUSIVE")
        self.assert_error(stream(dropped_records=1), "INCONCLUSIVE")
        end = dict(kind="end", run="demo", stream_id="ingress-0", seq=3,
                   ingress_port=0, next_ingress_seq=1)
        self.assert_error(stream([packet(), end]), "INCONCLUSIVE")
        gap = dict(kind="gap", run="demo", stream_id="ingress-0", seq=2,
                   first_missing_seq=2, last_missing_seq=2, reason="overflow")
        self.assert_error(stream([packet(), gap]), "INCONCLUSIVE")

    def test_only_missing_envelope_sequence_is_inconclusive_without_drop_counter(self):
        # Neither a producer counter nor a declared gap event is needed.
        end = dict(kind="end", run="demo", stream_id="ingress-0", seq=3,
                   ingress_port=0, next_ingress_seq=1)
        error=self.assert_error(stream([packet(), end]), "INCONCLUSIVE")
        self.assertEqual(error.code,"STREAM_SEQUENCE_GAP")

    def test_validation_cli_reports_incomplete_stream_without_counter(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/"events.jsonl"
            path.write_bytes(stream())
            command=[sys.executable,"-m","tools.g2.protocol",str(path)]
            good=subprocess.run(command,capture_output=True,text=True)
            self.assertEqual(good.returncode,0,good.stderr)
            self.assertTrue(json.loads(good.stdout)["stream_integrity"]["complete"])
            path.write_bytes(stream().replace(b'"seq":2',b'"seq":3'))
            bad=subprocess.run(command,capture_output=True,text=True)
            self.assertEqual(bad.returncode,2,bad.stderr)
            self.assertEqual(json.loads(bad.stdout)["measurement_status"],"INCONCLUSIVE")
            self.assertEqual(json.loads(bad.stdout)["reason_codes"],["STREAM_SEQUENCE_GAP"])

    def test_receive_ordinal_can_differ_from_file_order(self):
        def received(seq, receive_seq):
            return dict(kind="receive",run="demo",stream_id="ingress-0",seq=seq,
                        ingress_port=0,egress_port=1,line="cable",external_identity=f"f0:{receive_seq}",
                        flow_id="f0",receive_seq=receive_seq,receive_ns=None,timestamp=None,
                        selected_late=None,length=100,vlan_id=101,app=None)
        _,rows=self.read(stream([received(1,1),received(2,0)],role="receiver"))
        self.assertEqual([row["receive_seq"] for row in rows],[1,0])

    def test_rejects_unknown_fields_kind_bad_integer_and_identity(self):
        for changes in [dict(typo=1), dict(kind="mystery"), dict(seq=True),
                        dict(ingress_port=65536), dict(arrival_ns=1.0),
                        dict(length=2**32), dict(seq=2**64), dict(external_identity="é"),
                        dict(flow_id="bad\nflow"), dict(line=""), dict(selected_late=2),
                        dict(length_flags=dict(fcs_present=True, vlan_retained=True))]:
            with self.subTest(changes=changes):
                self.assert_error(stream([packet(**changes)]))
        self.assert_error(stream(), role="receiver")
        self.assert_error(stream(), run="different")
        self.assert_error(stream().replace(b'"producer":"fixture"', b'"producer":"fixture","seq":0'))
        self.assert_error(stream().replace(b'"arrival_ns":7', b'"arrival_ns":NaN'))
        self.assert_error(stream()[:-1])
        self.assert_error(stream()+b"{}\n")

    def test_invalid_frontier_and_port_sequences_are_rejected(self):
        frontier = dict(kind="frontier", run="demo", stream_id="ingress-0", seq=2,
                        ingress_port=0, through_ns=8, next_ingress_seq=1)
        second = packet(seq=3, ingress_seq=1, selection_seq=1, arrival_ns=8,
                        external_identity="f0:1")
        self.assert_error(stream([packet(), frontier, second]))
        self.assert_error(stream([packet(ingress_seq=1)]))
        self.assert_error(stream([packet(), dict(frontier, through_ns=6)]))
        self.assert_error(stream([packet(), dict(frontier, through_ns=2**64-1)]))

    def test_hash_count_sequence_and_identity_changes_are_detected(self):
        for changes in [dict(records_sha256="0"*64), dict(record_count=1), dict(seq=2),
                        dict(run="other"), dict(stream_id="other")]:
            with self.subTest(changes=changes):
                self.assert_error(stream(**changes))
        self.assert_error(stream().replace(b'"seq":2', b'"seq":1'))

    def test_writer_roundtrip_exclusive_output_and_override_rejection(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "events.jsonl"
            writer = StreamWriter(path, "demo", "ingress-0", "ingress", "engine")
            row = packet()
            writer.emit("packet", **{k:v for k,v in row.items() if k not in {"kind", "run", "stream_id", "seq"}})
            writer.emit("end", ingress_port=0, next_ingress_seq=1)
            writer.finish()
            reader = StreamReader(path)
            self.assertEqual(len(list(reader)), 2)
            with self.assertRaises(FileExistsError):
                StreamWriter(path, "demo", "ingress-0", "ingress", "engine")
        output = io.StringIO()
        writer = StreamWriter(output, "demo", "host", "host", "engine")
        with self.assertRaises(StreamError):
            writer.emit("terminal", run="other")
        writer.finish()
        self.assertFalse(output.closed)
        with self.assertRaises(StreamError):
            writer.emit("terminal")

    def test_receiver_timestamp_and_nested_attempt_fields_are_strict(self):
        receive = dict(kind="receive", run="demo", stream_id="ingress-0", seq=1,
                       ingress_port=0, egress_port=1, line="cable", external_identity="f0:0",
                       flow_id="f0", receive_seq=0, receive_ns=12, timestamp=dict(raw=12,
                       clock_id="obs", mapping_id="map", point="fake", uncertainty_ns=0),
                       selected_late=None, length=100, vlan_id=101, app=None)
        self.assertEqual(self.read(stream([receive], role="receiver"))[1][0]["receive_ns"], 12)
        for changes in [dict(timestamp=None), dict(timestamp=dict(receive["timestamp"], raw=True)),
                        dict(timestamp=dict(receive["timestamp"], typo=1)), dict(receive_seq=True)]:
            self.assert_error(stream([dict(receive, **changes)], role="receiver"))
        attempt = dict(kind="attempt", run="demo", stream_id="ingress-0", seq=1,
                       egress_port=1, queue_id=0, attempt_id=0, gate_ns=2, submit_before_ns=3,
                       submit_after_ns=4, seen_next_ingress_seq={"0":1}, frontiers={"0":{
                       "through_ns":None, "input_complete":True}}, consumed_dispatch={"h":1},
                       offered=[dict(ingress_port=0, ingress_seq=0, external_identity="f0:0",
                       line="cable", gate_ns=2, handoff_queue_id="h", handoff_seq=0)], accepted_count=1)
        self.assertEqual(self.read(stream([attempt], role="host"))[1][0]["accepted_count"], 1)
        for changes in [dict(accepted_count=2), dict(submit_before_ns=1), dict(offered=[dict(
                       attempt["offered"][0], typo=1)]), dict(frontiers={"0":{"through_ns":None}})]:
            self.assert_error(stream([dict(attempt, **changes)], role="host"))


if __name__ == "__main__":
    unittest.main()
