# SPDX-License-Identifier: Apache-2.0
"""The default RAM checker preserves actor boundaries and accepted prefixes."""
import copy
import unittest

from tools.g2.live_custody import LiveCustody, key
from tools.g2.protocol import StreamError, validate_record
from tools.g2.tests.test_custody import attempt, dispatch, envelop, expected, offer, terminal
from tools.g2.tests.test_reference import config
from tools.g2.tests.test_registration import manifest


def actor(row, producer):
    return dict(row, extensions=dict(native_observer_v1=dict(producer=producer, producer_seq=0, order=0)))


class LiveCustodyTests(unittest.TestCase):
    def test_native_v2_wrong_stage_cannot_supply_an_offer_or_model_terminal(self):
        row=expected(outcome='q_drop')
        for event in (offer(row),terminal(row,'q_drop')):
            event['extensions']=dict(native_observer_v2=dict(producer=3,producer_seq=1,order=0))
            normalized=envelop([event])[0]
            with self.subTest(kind=event['kind'],boundary='protocol'),self.assertRaises(StreamError):
                validate_record(normalized,'host')
            checker,active,issues=self.audit([row],[])
            active[key(row)]['offer']=None
            checker.event(normalized)
            with self.subTest(kind=event['kind'],boundary='live'):
                self.assertIn(('native_actor','INCONCLUSIVE'),issues)

    def two_owner_history(self):
        cfg, reg = config(), manifest()
        cfg['lines'].append(dict(copy.deepcopy(cfg['lines'][0]), name='reverse', ingress_port=1, egress_port=0))
        reg['ports'].append(dict(reg['ports'][0], ingress_port=1, egress_port=0))
        reg['capacity']['handoff_queues'] = [dict(handoff_queue_id=f'handoff-{i}',producer=f'w{i}',consumer=f't{i}',capacity=8,
            extensions=dict(native_actors=dict(producer=dict(stream_id='host',producer=1+i),
                                               consumer=dict(stream_id='host',producer=3 if i==0 else 6)))) for i in (0,1)]
        rows = [expected(), dict(expected(line='reverse'), ingress_port=1, egress_port=0)]
        events=[]
        for i,row in enumerate(rows):
            e=dict(dispatch([row],queue=f'handoff-{i}'),egress_port=row['egress_port'])
            e['extensions']=dict(native_observer_v2=dict(producer=1+i,producer_seq=1,order=i))
            events.append(e)
        for i,row in enumerate(rows):
            e=dict(attempt([row]),egress_port=row['egress_port'],consumed_dispatch={f'handoff-{q}':int(q==i) for q in (0,1)})
            e['offered'][0]['handoff_queue_id']=f'handoff-{i}'
            e['extensions']=dict(native_observer_v2=dict(producer=3 if i==0 else 6,producer_seq=1,order=2+i))
            events.append(e)
        for i,e in enumerate(events):
            owner=i%2
            e.update(seen_next_ingress_seq={str(p):int(p==owner) for p in (0,1)},
                     frontiers={str(p):dict(through_ns=None,input_complete=p==owner) for p in (0,1)})
        return cfg,reg,rows,events

    def test_two_tx_owners_do_not_claim_each_others_handoff_or_frontier(self):
        cfg,reg,rows,events=self.two_owner_history()
        checker,active,issues=self.audit(rows,events,cfg,reg)
        self.assertEqual(issues,[])
        self.assertEqual(checker.counts['tx_accepted'],2)
        self.assertTrue(all(s['tx'] for s in active.values()))

    def test_foreign_prefix_wrong_actor_and_missing_relevant_frontier_are_rejected(self):
        for fault,code in [('foreign','handoff_owner'),('actor','handoff_owner'),('frontier','unsafe_dispatch_frontier'),('binding','handoff_actor_missing')]:
            cfg,reg,rows,events=self.two_owner_history()
            if fault=='foreign': events[2]['consumed_dispatch']['handoff-1']=1
            elif fault=='actor': events[0]['extensions']['native_observer_v2']['producer']=2
            elif fault=='binding': del reg['capacity']['handoff_queues'][0]['extensions']
            else: events[0]['frontiers']['0']['input_complete']=False
            with self.subTest(fault=fault):
                _,_,issues=self.audit(rows,events,cfg,reg)
                self.assertIn((code,'INCONCLUSIVE'),issues)

    def test_v2_provenance_is_distinct_and_bounded(self):
        row = envelop([attempt([expected()])])[0]
        row['extensions'] = dict(native_observer_v2=dict(producer=6,producer_seq=1,order=0))
        validate_record(row,'host')
        from tools.g2.protocol import actor as native_actor
        self.assertEqual(native_actor(row), ('host',6))
        for change in ('overflow','conflict'):
            broken = copy.deepcopy(row)
            if change=='overflow': broken['extensions']['native_observer_v2']['producer'] = 7
            else: broken['extensions']['native_observer_v1'] = dict(producer=3,producer_seq=1,order=0)
            with self.assertRaises(StreamError): validate_record(broken,'host')

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
