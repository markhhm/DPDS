# SPDX-License-Identifier: Apache-2.0
"""The default RAM checker preserves actor boundaries and accepted prefixes."""
import copy
import unittest

from tools.g2.live_custody import LiveCustody, key
from tools.g2.protocol import StreamError, validate_record
from tools.g2.tests.test_custody import attempt, dispatch, envelop, expected
from tools.g2.tests.test_reference import config
from tools.g2.tests.test_registration import manifest


def actor(row, producer):
    return dict(row, extensions=dict(native_observer_v1=dict(producer=producer, producer_seq=0, order=0)))


class LiveCustodyTests(unittest.TestCase):
    def audit(self, rows, events, configuration=None, registration=None):
        active = {key(row):dict(expected=row, offer=True, terminal=None, tx=False,
                               dispatch=None, pending=False) for row in rows}
        issues = []
        checker = LiveCustody(configuration or config(), {}, registration or manifest(), active,
                              lambda code,status,row=None:issues.append((code,status)))
        checker.input_next = {port:max((row["ingress_seq"]+1 for row in rows if row["ingress_port"]==port),default=0)
                              for port in checker.input_next}
        checker.input_end = set(checker.input_next)
        for event in envelop(events):
            checker.event(event)
        return checker, active, issues

    def test_zero_partial_and_full_attempts_preserve_the_unaccepted_suffix(self):
        rows = [expected(i, release=190+i) for i in range(3)]
        events = [dispatch(rows,at=200), attempt(rows,accepted=0,at=250),
                  attempt(rows,accepted=1,aid=1,at=260),
                  attempt(rows[1:],handoffs=[1,2],accepted=2,aid=2,at=270,consumed=3,count=3)]
        checker, active, issues = self.audit(rows, events)
        self.assertEqual(issues, [])
        self.assertEqual(checker.counts["tx_accepted"], 3)
        self.assertTrue(all(state["tx"] and not state["pending"] for state in active.values()))

    def test_merged_stream_snapshots_are_monotone_per_native_actor(self):
        row = expected()
        first = actor(dispatch([row]), 1)
        second = actor(attempt([row]), 3)
        second["seen_next_ingress_seq"]["0"] = 0
        second["frontiers"]["0"].update(input_complete=False, through_ns=None)
        _, _, issues = self.audit([row], [first,second])
        self.assertEqual(issues, [])

    def test_native_actor_changes_cannot_hide_multiple_spsc_producers(self):
        rows = [expected(i,release=190) for i in range(2)]
        first = actor(dispatch(rows[:1],count=2), 1)
        second = actor(dispatch(rows[1:],first=1,count=2), 2)
        _, _, issues = self.audit(rows,[first,second])
        self.assertIn(("handoff_multiple_producers","INCONCLUSIVE"),issues)

    def test_one_spsc_prefix_can_transfer_pending_items_for_both_egresses(self):
        cfg, reg = config(), manifest()
        reverse = copy.deepcopy(cfg["lines"][0])
        reverse.update(name="reverse", ingress_port=1, egress_port=0)
        cfg["lines"].append(reverse)
        reg["ports"].append(dict(reg["ports"][0], ingress_port=1, egress_port=0))
        forward = expected(0)
        backward = dict(expected(0,line="reverse"), ingress_port=1, egress_port=0)
        first = actor(dispatch([forward]),1)
        second = actor(dict(dispatch([backward],first=1),egress_port=0),1)
        tx1 = actor(attempt([forward],consumed=2),3)
        tx0 = actor(dict(attempt([backward],handoffs=[1],consumed=2),egress_port=0),3)
        events = [first,second,tx1,tx0]
        for event in events:
            event.update(seen_next_ingress_seq={"0":1,"1":1},frontiers={
                str(port):dict(through_ns=None,input_complete=True) for port in (0,1)})
        checker, active, issues = self.audit([forward,backward],events,cfg,reg)
        self.assertEqual(issues, [])
        self.assertEqual(checker.counts["tx_accepted"],2)

    def test_native_provenance_is_a_validated_versioned_extension(self):
        row = envelop([actor(attempt([expected()]),3)])[0]
        validate_record(row,"host")
        for name,value in (("producer",5),("producer",True),("producer_seq",-1),("order",True)):
            broken = copy.deepcopy(row)
            broken["extensions"]["native_observer_v1"][name] = value
            with self.subTest(name=name,value=value):
                with self.assertRaises(StreamError):
                    validate_record(broken,"host")


if __name__=="__main__":
    unittest.main()
