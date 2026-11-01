# SPDX-License-Identifier: Apache-2.0
"""Hand-checked custody histories; no engine deadlines or decisions are imported."""
import copy
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

try:
    from tools.g2.custody import audit_host
except ImportError:
    audit_host = None


def expected(seq=0, line="cable", release=190, outcome="accepted", selection=None):
    return dict(kind="expected", run="demo", stream_id="expected", seq=seq + 1,
                ingress_port=0, ingress_seq=seq, external_identity=f"p{seq}",
                line=line, selection_seq=seq if selection is None else selection,
                arrival_ns=10 + seq, length=60, vlan_id=101 if line == "cable" else 102,
                segments=1, length_flags=dict(fcs_present=False, vlan_retained=True),
                flow_id="f", app=None, selected_late=0, input_error=None,
                egress_port=1, l2_bytes=64, wire_bytes=84, outcome=outcome, late=False,
                service_ns=11 + seq, deadline_ns=release,
                release_ns=release if outcome == "accepted" else None,
                clamp_extra_ns=0, q_bytes=64, q_drain_ns=1)


def identity(row):
    return {key: row[key] for key in ("ingress_port", "ingress_seq", "external_identity", "line")}


def offer(row, actual=None):
    return dict(kind="offer", at_ns=20 + row["ingress_seq"],
                actual_selection_seq=row["selection_seq"] if actual is None else actual,
                **identity(row))


def snapshot(count):
    return dict(seen_next_ingress_seq={"0": count},
                frontiers={"0": dict(through_ns=None, input_complete=True)})


def dispatch(rows, first=0, at=190, queue="h", count=None):
    return dict(kind="dispatch", at_ns=at, egress_port=1, handoff_queue_id=queue,
                first_handoff_seq=first, identities=[identity(row) for row in rows],
                **snapshot(len(rows) if count is None else count))


def attempt(rows, handoffs=None, accepted=None, aid=0, at=250, consumed=None, count=None):
    if handoffs is None:
        handoffs = list(range(len(rows)))
    offered = [dict(identity(row), gate_ns=at, handoff_queue_id="h", handoff_seq=seq)
               for row, seq in zip(rows, handoffs)]
    return dict(kind="attempt", egress_port=1, queue_id=0, attempt_id=aid,
                gate_ns=at, submit_before_ns=at, submit_after_ns=at + 1,
                offered=offered, accepted_count=len(rows) if accepted is None else accepted,
                consumed_dispatch={"h": len(rows) if consumed is None else consumed},
                **snapshot(len(rows) if count is None else count))


def terminal(row, reason, at=40):
    return dict(kind="terminal", at_ns=at, reason=reason, **identity(row))


def envelop(rows, stream="host"):
    return [dict(row, run="demo", stream_id=stream, seq=i + 1) for i, row in enumerate(rows)]


class CustodyTests(unittest.TestCase):
    def test_v2_two_owner_replay_keeps_prefixes_and_frontiers_separate(self):
        from tools.g2.tests.test_live_custody import LiveCustodyTests
        cfg,reg,rows,events=LiveCustodyTests().two_owner_history()
        self.config=cfg
        self.manifest=reg
        self.manifest['retention']['mode']='full'
        self.manifest['capacity']['join_disk_bytes']=8_000_000
        events=[offer(row) for row in rows]+events
        audit=self.audit(rows,events)
        self.assertEqual(audit.issues,[])
        for row in rows:
            self.assertTrue(audit.was_accepted(row['ingress_port'],row['external_identity']))

    def setUp(self):
        self.assertTrue(callable(audit_host), "independent host custody checker is missing")
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        model = dict(c_bps=3_360_000_000_000, q_bytes=4096, t_ns=0, delta_ns=0,
                     p="0", seed=0, storage_capacity_packets=4096,
                     storage_capacity_bytes=10_000_000)
        self.config = dict(schema="hng-v1", lines=[
            dict(name=name, ingress_port=0, egress_port=1, vlan_id=vlan,
                 model=model, trace=None,
                 envelope=dict(min_q_bytes=64, max_q_bytes=1522, burst_bound_packets=16))
            for name, vlan in [("cable", 101), ("leo", 102)]])
        self.manifest = dict(scope="software-only",retention=dict(mode="full",debug_windows=[],mismatch_cap=64),
                             ports=[dict(ingress_port=0, egress_port=1,
                                       link_bps=100_000_000_000, queue_id=0)],
                             capacity=dict(join_disk_bytes=8_000_000, max_unresolved=64,
                                           max_diagnostics=64, handoff_queues=[
                                               dict(handoff_queue_id="h", producer="scheduler",
                                                    consumer="tx", capacity=64)]))
        self.traces = {}

    def audit(self, rows, events):
        return audit_host(self.config, self.traces, self.manifest, iter(rows),
                          iter(envelop(events)) if not events or "stream_id" not in events[0]
                          else iter(events), Path(self.directory.name))

    def test_native_v2_wrong_stage_cannot_make_a_model_drop_conclusive(self):
        row=expected(outcome='q_drop')
        events=envelop([offer(row),terminal(row,'q_drop')])
        for i,event in enumerate(events):
            event['extensions']=dict(native_observer_v2=dict(producer=3,producer_seq=i+1,order=i))
        audit=self.audit([row],events)
        self.assertIn('native_actor',self.codes(audit,'INCONCLUSIVE'))
        self.assertNotEqual(audit.measurement_status,'PASS')

    def test_history_replay_requires_explicit_software_debug_retention(self):
        for change in [dict(retention=dict(mode="stream",debug_windows=[],mismatch_cap=64)),dict(scope="physical")]:
            registration=dict(self.manifest,**change)
            with self.subTest(change=change),self.assertRaises(ValueError):
                audit_host(self.config,self.traces,registration,iter([]),iter([]),Path(self.directory.name))

    def test_database_handles_close_after_replay_and_lookup(self):
        real_connect=sqlite3.connect
        opened=[]
        def connect(*args,**kwargs):
            connection=real_connect(*args,**kwargs)
            opened.append(connection)
            return connection
        row=expected()
        with patch("tools.g2.custody.sqlite3.connect",side_effect=connect):
            audit=self.audit([row],[offer(row),dispatch([row]),attempt([row])])
            self.assertTrue(audit.was_accepted(0,"p0"))
            self.assertIsNone(audit.terminal_reason(0,"p0"))
        for connection in opened:
            with self.assertRaises(sqlite3.ProgrammingError):
                connection.execute("select 1")

    def codes(self, audit, status=None):
        return {r["code"] for r in audit.issues
                if status is None or r["measurement_status"] == status}

    def test_full_acceptance_has_disk_lookup_and_no_issues(self):
        row = expected()
        audit = self.audit([row], [offer(row), dispatch([row]), attempt([row])])
        self.assertEqual(audit.issues, [])
        self.assertTrue(audit.was_accepted(0, "p0"))
        self.assertIsNone(audit.terminal_reason(0, "p0"))

    def test_held_leo_bypasses_cable_at_actual_gate_then_resumes(self):
        leo, cable = expected(line="leo", selection=0), expected(1, release=220, selection=0)
        self.config["lines"][1]["trace"] = dict(path="trace", sha256="0" * 64)
        self.traces["leo"] = dict(end_ns=1000, terminal="error", terminal_horizon_ns=None,
                                  loss_seed=0, outage_action="hold", rows=[
                                      dict(at_ns=0, t_ns=0, loss="0", outage=False),
                                      dict(at_ns=200, t_ns=0, loss="0", outage=True),
                                      dict(at_ns=300, t_ns=0, loss="0", outage=False)])
        events = [offer(leo), offer(cable), dispatch([leo], at=190, count=2),
                  dispatch([cable], first=1, at=220, count=2),
                  attempt([cable], handoffs=[1], at=250, consumed=2, count=2),
                  attempt([leo], handoffs=[0], aid=1, at=300, consumed=2, count=2)]
        self.assertEqual(self.audit([leo, cable], events).issues, [])
        events[-2]["offered"] = [dict(identity(leo), handoff_queue_id="h", handoff_seq=0, gate_ns=250)]
        self.assertIn("tx_gate_closed", self.codes(self.audit([leo, cable], events), "FAIL"))

    def test_zero_partial_full_retries_preserve_failed_suffix(self):
        rows = [expected(i, release=190 + i) for i in range(3)]
        events = [offer(row) for row in rows] + [dispatch(rows, at=200),
                  attempt(rows, accepted=0, at=210),
                  attempt(rows, accepted=1, aid=1, at=220),
                  attempt(rows[1:], handoffs=[1, 2], aid=2, at=230, consumed=3, count=3)]
        self.assertEqual(self.audit(rows, events).issues, [])
        events[-1]["offered"].reverse()
        self.assertIn("tx_wrong_head", self.codes(self.audit(rows, events), "FAIL"))

    def test_one_physical_queue_transfers_both_egresses_and_keeps_partial_suffix(self):
        self.config["lines"].append(dict(self.config["lines"][0], name="reverse",
                                         ingress_port=1, egress_port=0))
        self.manifest["ports"].append(dict(ingress_port=1, egress_port=0,
                                           link_bps=100_000_000_000, queue_id=0))
        a, c = expected(), expected(1, release=200)
        b = dict(expected(line="reverse"), ingress_port=1, egress_port=0, external_identity="r0")
        events = [offer(a), offer(b), offer(c), dispatch([a], at=190),
                  dict(dispatch([b], first=1, at=190), egress_port=0),
                  dispatch([c], first=2, at=200),
                  dict(attempt([b], handoffs=[1], at=250, consumed=3), egress_port=0),
                  attempt([a,c], handoffs=[0,2], accepted=1, at=251, consumed=3),
                  attempt([c], handoffs=[2], aid=1, at=252, consumed=3)]
        for event in events:
            if event["kind"] in {"dispatch", "attempt"}:
                event["seen_next_ingress_seq"] = {"0":2, "1":1}
                event["frontiers"] = {str(port):dict(through_ns=None, input_complete=True) for port in (0,1)}
        audit = self.audit([a,b,c], events)
        self.assertEqual(audit.issues, [])
        self.assertTrue(audit.was_accepted(1, "r0"))
        self.assertTrue(audit.was_accepted(0, "p1"))
        events[-1]["egress_port"] = 0
        self.assertIn("port_mapping", self.codes(self.audit([a,b,c], events), "INVALID"))

    def test_handoff_loss_is_failure_and_selection_gap_is_downstream_failure(self):
        a, b = expected(), expected(1)
        events = [terminal(a, "handoff_drop"), offer(b, actual=0),
                  dispatch([b], at=200, count=2), attempt([b], at=210, count=2)]
        # Terminal before the surviving offer is a real pre-offer loss.
        events[0]["at_ns"] = 15
        audit = self.audit([a, b], events)
        self.assertIn("non_model_drop", self.codes(audit, "FAIL"))
        self.assertIn("selection_divergence", self.codes(audit, "FAIL"))
        self.assertNotIn("missing_offer", self.codes(audit))
        self.assertEqual(audit.terminal_reason(0, "p0"), "handoff_drop")

    def test_missing_offer_or_dispatch_evidence_is_inconclusive(self):
        row = expected()
        audit = self.audit([row], [dispatch([row]), attempt([row])])
        self.assertIn("missing_offer", self.codes(audit, "INCONCLUSIVE"))
        audit = self.audit([row], [offer(row), attempt([row])])
        self.assertIn("missing_dispatch", self.codes(audit, "INCONCLUSIVE"))

    def test_model_reasons_join_by_identity_not_equal_total_count(self):
        a, b = expected(outcome="q_drop"), expected(1, outcome="trace_loss")
        audit = self.audit([a, b], [offer(a), offer(b), terminal(a, "trace_loss_drop"),
                                    terminal(b, "q_drop")])
        self.assertIn("model_terminal_mismatch", self.codes(audit, "FAIL"))
        self.assertEqual(audit.counts["model_terminal_mismatch"], 2)

    def test_fresh_unconsumed_earlier_head_does_not_force_wrong_tx(self):
        earlier, cable = expected(line="leo", release=190, selection=0), expected(1, release=220, selection=0)
        # Earlier item is scheduler-owned; actual consumed prefix contains cable only.
        events = [offer(cable), dispatch([cable], at=220, count=2), offer(earlier),
                  attempt([cable], at=250, count=2), terminal(earlier, "shutdown_drop", at=300)]
        events[0]["at_ns"] = 20
        events[2]["at_ns"] = 230
        audit = self.audit([earlier, cable], events)
        self.assertNotIn("tx_wrong_head", self.codes(audit))
        self.assertNotIn("dispatch_wrong_head", self.codes(audit))

    def test_enqueued_unconsumed_earlier_head_does_not_force_wrong_tx(self):
        a, b = expected(line="leo", selection=0), expected(1, release=220, selection=0)
        self.manifest["capacity"]["handoff_queues"].append(
            dict(handoff_queue_id="later", producer="scheduler", consumer="tx", capacity=64))
        tx = attempt([b], handoffs=[0], at=250, consumed=0, count=2)
        tx["consumed_dispatch"]["later"] = 1
        tx["offered"][0]["handoff_queue_id"] = "later"
        events = [offer(a), offer(b), dispatch([a], count=2),
                  dispatch([b], queue="later", at=220, count=2), tx,
                  terminal(a, "shutdown_drop", at=300)]
        self.assertNotIn("tx_wrong_head", self.codes(self.audit([a, b], events)))

    def test_dispatch_skipping_safe_earlier_head_is_failure(self):
        a, b = expected(), expected(1, release=200)
        audit = self.audit([a, b], [offer(a), offer(b), dispatch([b, a], at=210),
                                    attempt([b, a], at=220)])
        self.assertIn("dispatch_wrong_head", self.codes(audit, "FAIL"))

    def test_terminal_and_acceptance_are_disjoint(self):
        row = expected()
        audit = self.audit([row], [offer(row), dispatch([row]), attempt([row]),
                                  terminal(row, "terminal_tx_drop", at=300)])
        self.assertIn("double_terminal", self.codes(audit, "INVALID"))

    def test_attempt_prefix_regression_and_past_dispatch_are_inconclusive(self):
        row = expected()
        events = [offer(row), dispatch([row]), attempt([row], accepted=0),
                  attempt([row], aid=1, at=300, consumed=0)]
        self.assertIn("consumed_dispatch_regression", self.codes(self.audit([row], events), "INCONCLUSIVE"))
        events[-1]["consumed_dispatch"]["h"] = 2
        self.assertIn("missing_dispatch", self.codes(self.audit([row], events), "INCONCLUSIVE"))

    def test_impossible_times_identity_port_and_attempt_id_are_rejected(self):
        row = expected()
        for change, code in [(dict(attempt_id=2), "attempt_sequence"),
                             (dict(submit_before_ns=249), "attempt_time"),
                             (dict(queue_id=1), "port_mapping")]:
            tx = attempt([row])
            tx.update(change)
            self.assertIn(code, self.codes(self.audit([row], [offer(row), dispatch([row]), tx]), "INVALID"))
        tx = attempt([row])
        tx["offered"][0]["ingress_port"] = 2
        self.assertIn("unknown_identity", self.codes(self.audit([row], [offer(row), dispatch([row]), tx]), "INVALID"))

    def test_frontiers_are_consumed_prefix_evidence_not_late_wall_clock(self):
        row = expected()
        event = dispatch([row])
        event["frontiers"]["0"] = dict(through_ns=15, input_complete=False)
        audit = self.audit([row], [offer(row), event, attempt([row])])
        self.assertIn("unsafe_dispatch_frontier", self.codes(audit, "INCONCLUSIVE"))

    def test_capacity_limits_diagnostics_and_disk_are_explicit(self):
        rows = [expected(i) for i in range(3)]
        self.manifest["capacity"].update(max_unresolved=1, max_diagnostics=1)
        audit = self.audit(rows, [offer(row) for row in rows])
        self.assertIn("unresolved_capacity", audit.counts)
        self.assertLessEqual(len(audit.issues), 1)
        self.manifest["capacity"]["join_disk_bytes"] = 1
        self.assertIn("join_disk_capacity", self.audit([], []).counts)


if __name__ == "__main__":
    unittest.main()
