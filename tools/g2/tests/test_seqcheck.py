# SPDX-License-Identifier: Apache-2.0
"""Small independently authored fixtures for the external seqcheck bridge."""
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from tools.g2.protocol import StreamError

try:
    from tools.g2.seqcheck import load_checks
except ImportError:
    load_checks = None


METRICS = ("ingress", "receiver", "ordinary_reorder", "missing", "duplicate_ingress",
           "duplicate_receiver", "unexpected", "flag_mismatch")
ISSUES = METRICS[2:]


class SeqcheckTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.manifest = dict(ports=[dict(ingress_port=0, egress_port=1)],
                             lines=[dict(name="forward", ingress_port=0, egress_port=1, vlan_id=100)],
                             identity_profile=dict(kind="probe"))
        self.counters = dict(ports={"0": dict(q_drop=0, trace_loss_drop=0,
            trace_outage_drop=0, delay_storage_drop=0, rx_returned=3)})
        self.doc = dict(schema="hng-seqcheck-v1", scope="supplied-captures", error=None,
            status="PASS", complete=True,
            inputs={}, totals=dict.fromkeys(METRICS, 0), flows=[], details_limit=2,
            details={key: [] for key in ISSUES},
            details_truncated=dict.fromkeys(ISSUES, False))
        for role, data in (("ingress", b"independent ingress fixture"),
                           ("receiver", b"independent receiver fixture")):
            (self.root / (role + ".pcapng")).write_bytes(data)
            self.doc["inputs"][role] = dict(path=role + ".pcapng", error=None,
                sha256=hashlib.sha256(data).hexdigest(),
                byte_size=len(data), packets=3, health=dict(complete=True, capture_drops=0))
        self.doc["totals"].update(ingress=3, receiver=3)
        self.doc["flows"] = [dict(self.doc["totals"],
            flow_id="0a000001:0a000002:1000:9000", vlan_id=100)]

    def registration(self, doc=None):
        raw = json.dumps(self.doc if doc is None else doc).encode()
        (self.root / "check.json").write_bytes(raw)
        return dict(path="check.json", sha256=hashlib.sha256(raw).hexdigest(),
            ingress_port=0, egress_port=1, ingress_path="ingress.pcapng", receiver_path="receiver.pcapng")

    def check(self, doc=None):
        self.assertTrue(callable(load_checks), "external seqcheck JSON bridge is missing")
        return load_checks([self.registration(doc)], self.root, self.manifest, self.counters)["0"]

    def metric(self, key, value):
        self.doc["totals"][key] = self.doc["flows"][0][key] = value
        if key in {"ingress", "receiver"}:
            self.doc["inputs"][key]["packets"] = value
        if key in ISSUES and value:
            self.doc["status"] = "INVALID" if key == "duplicate_ingress" else "FAIL"
            if key == "duplicate_ingress":
                self.doc["complete"] = False
            self.doc["details_truncated"][key] = True

    def test_bound_full_run_pass_keeps_raw_counts_and_scope(self):
        result = self.check()
        self.assertEqual(result["order"], dict(status="PASS", threshold=0, value=0))
        self.assertEqual(result["storage"], dict(status="PASS", threshold=0, value=0))
        self.assertEqual(result["coverage_scope"], "input-run")
        self.assertEqual(result["totals"]["ingress"], 3)
        self.assertEqual(result["flows"][0]["vlan_id"], 100)

    def test_frozen_diagnostics_are_typed_and_unknown_fields_rejected(self):
        self.assertEqual(self.check()["storage"]["status"], "PASS")
        for mutate in (lambda d: d.update(scope="unknown"),
                       lambda d: d.update(error=1),
                       lambda d: d["inputs"]["receiver"].update(error=[]),
                       lambda d: d["inputs"]["receiver"].update(path=3),
                       lambda d: d.update(unrecognized="field")):
            with self.subTest(mutate=mutate):
                doc = copy.deepcopy(self.doc)
                mutate(doc)
                with self.assertRaises(StreamError):
                    self.check(doc)

    def test_invalid_partial_capture_counts_are_diagnostic_not_dut_failure(self):
        self.doc.update(status="INVALID", complete=False, error="capture rejected")
        self.doc["inputs"]["receiver"].update(packets=4, error="packet rejected")
        self.doc["inputs"]["receiver"]["health"].update(complete=False, capture_drops=None)
        result = self.check()
        self.assertEqual(result["order"]["status"], "INCONCLUSIVE")
        self.assertEqual(result["storage"]["status"], "INCONCLUSIVE")
        self.doc["inputs"]["receiver"]["sha256"] = None
        self.assertEqual(self.check()["storage"]["status"], "INCONCLUSIVE")

    def test_repeated_unexpected_packets_overlap_duplicate_counter(self):
        self.metric("unexpected", 3)
        self.metric("duplicate_receiver", 2)
        result = self.check()
        self.assertEqual(result["order"]["status"], "FAIL")

    def test_frozen_details_limit_supports_one_hundred_thousand(self):
        self.doc["details_limit"] = 100000
        self.assertEqual(self.check()["order"]["status"], "PASS")

    def test_raw_missing_reconciles_all_three_intentional_drop_causes(self):
        self.metric("missing", 3)
        self.metric("receiver", 0)
        self.counters["ports"]["0"].update(q_drop=1, trace_loss_drop=1, trace_outage_drop=1)
        result = self.check()
        self.assertEqual(result["storage"]["status"], "PASS")
        self.assertEqual(result["storage"]["value"], 0)
        self.assertEqual(result["totals"]["missing"], 3)

    def test_unexplained_missing_and_delay_storage_are_failures(self):
        self.metric("missing", 1)
        self.metric("receiver", 2)
        self.assertEqual(self.check()["storage"], dict(status="FAIL", threshold=0, value=1))
        self.counters["ports"]["0"].update(q_drop=1, delay_storage_drop=1)
        self.assertEqual(self.check()["storage"]["status"], "FAIL")

    def test_every_order_violation_including_late_flag_mismatch_fails(self):
        for key in ("ordinary_reorder", "duplicate_receiver", "unexpected", "flag_mismatch"):
            with self.subTest(key=key):
                original = copy.deepcopy(self.doc)
                self.metric(key, 1)
                self.assertEqual(self.check()["order"]["status"], "FAIL")
                self.doc = original

    def test_missing_counters_or_probe_coverage_cannot_pass(self):
        for change in (lambda: self.counters["ports"]["0"].update(q_drop=None),
                       lambda: self.counters["ports"]["0"].update(rx_returned=4),
                       lambda: self.manifest.update(identity_profile=dict(kind="opaque"))):
            with self.subTest(change=change):
                self.setUp()
                change()
                self.assertEqual(self.check()["storage"]["status"], "INCONCLUSIVE")

    def test_absent_registration_and_unregistered_manifest_port_are_inconclusive(self):
        self.assertTrue(callable(load_checks), "external seqcheck JSON bridge is missing")
        self.assertEqual(load_checks([], self.root, self.manifest, self.counters)["0"]["order"]["status"],
                         "INCONCLUSIVE")
        self.manifest["ports"].append(dict(ingress_port=1, egress_port=0))
        result = load_checks([self.registration()], self.root, self.manifest, self.counters)
        self.assertEqual(result["1"]["storage"]["status"], "INCONCLUSIVE")

    def test_native_storage_failure_survives_missing_seqcheck_registration(self):
        self.assertTrue(callable(load_checks), "external seqcheck JSON bridge is missing")
        self.counters["ports"]["0"]["delay_storage_drop"] = 2
        result = load_checks([], self.root, self.manifest, self.counters)["0"]
        self.assertEqual(result["order"]["status"], "INCONCLUSIVE")
        self.assertEqual(result["storage"], dict(status="FAIL", threshold=0, value=2))
        self.assertIn("seqcheck_missing", result["reason_codes"])

    def test_two_directional_results_keep_independent_port_totals(self):
        forward=self.registration()
        reverse=copy.deepcopy(self.doc)
        reverse['flows'][0]['flow_id']='0a000002:0a000001:1000:9000'
        reverse['flows'][0]['vlan_id']=200
        raw=json.dumps(reverse).encode();(self.root/'reverse.json').write_bytes(raw)
        backward=dict(forward,path='reverse.json',sha256=hashlib.sha256(raw).hexdigest(),ingress_port=1,egress_port=0)
        self.manifest['ports'].append(dict(ingress_port=1,egress_port=0))
        self.manifest['lines'].append(dict(name='reverse',ingress_port=1,egress_port=0,vlan_id=200))
        self.counters['ports']['1']=dict(self.counters['ports']['0'])
        result=load_checks([forward,backward],self.root,self.manifest,self.counters)
        self.assertEqual({p:c['storage']['status'] for p,c in result.items()},{'0':'PASS','1':'PASS'})
        self.assertEqual(result['1']['flows'][0]['vlan_id'],200)

    def test_malformed_counter_evidence_raises_stream_error(self):
        self.counters["ports"]["0"] = []
        with self.assertRaises(StreamError):
            self.check()

    def test_invalid_capture_health_and_duplicate_ingress_are_inconclusive(self):
        self.doc["status"] = "INVALID"
        self.doc["complete"] = False
        self.doc["inputs"]["receiver"]["health"]["capture_drops"] = 1
        self.assertEqual(self.check()["order"]["status"], "INCONCLUSIVE")
        self.doc["inputs"]["receiver"]["health"]["capture_drops"] = 0
        self.metric("duplicate_ingress", 1)
        self.assertEqual(self.check()["storage"]["status"], "INCONCLUSIVE")

    def test_json_and_actual_capture_hashes_are_verified(self):
        self.assertTrue(callable(load_checks), "external seqcheck JSON bridge is missing")
        registration = self.registration()
        (self.root / "check.json").write_bytes(b"{}")
        with self.assertRaises(StreamError):
            load_checks([registration], self.root, self.manifest, self.counters)
        registration = self.registration()
        (self.root / "receiver.pcapng").write_bytes(b"changed")
        with self.assertRaises(StreamError):
            load_checks([registration], self.root, self.manifest, self.counters)

    def test_malformed_counts_totals_pairs_and_details_are_rejected(self):
        mutations = [lambda d: d["totals"].update(missing=True),
            lambda d: d["totals"].update(receiver=2),
            lambda d: d["flows"].append(copy.deepcopy(d["flows"][0])),
            lambda d: d["flows"][0].update(vlan_id=101),
            lambda d: d.update(status="FAIL"),
            lambda d: d["inputs"]["receiver"].update(byte_size=1),
            lambda d: d["details"]["missing"].extend([{}, {}, {}]),
            lambda d: d["details_truncated"].update(missing=True)]
        for mutate in mutations:
            with self.subTest(mutate=mutate):
                doc = copy.deepcopy(self.doc)
                mutate(doc)
                with self.assertRaises(StreamError):
                    self.check(doc)
        self.assertTrue(callable(load_checks), "external seqcheck JSON bridge is missing")
        registration = self.registration()
        for registrations in ([registration, registration], [dict(registration, egress_port=2)]):
            with self.assertRaises(StreamError):
                load_checks(registrations, self.root, self.manifest, self.counters)

    def test_details_bind_validated_flow_and_uint64_sequence(self):
        self.metric("flag_mismatch", 1)
        self.doc["details_truncated"]["flag_mismatch"] = False
        self.doc["details"]["flag_mismatch"] = [dict(flow_id="0a000001:0a000002:1000:9000",
            vlan_id=100, sequence=2**64-1, ingress_rank=2, receiver_index=1)]
        self.assertEqual(self.check()["details"]["flag_mismatch"][0]["sequence"], 2**64-1)
        self.doc["details"]["flag_mismatch"][0]["sequence"] = 2**64
        with self.assertRaises(StreamError):
            self.check()


if __name__ == "__main__":
    unittest.main()
