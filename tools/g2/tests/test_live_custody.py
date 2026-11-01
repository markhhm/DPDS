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
    def test_many_inflight_dispatches_and_attempts_require_linear_row_work(self):
        reads = [0]
        class Row(dict):
            def __getitem__(self, name):
                reads[0] += 1
                return super().__getitem__(name)
        count = 512
        rows = [Row(expected(i, release=100_000+i)) for i in range(count)]
        events = []
        for first in range(0, count, 16):
            batch = rows[first:first+16]
            events += [dispatch(batch, first=first, at=200_000, count=count),
                       attempt(batch, handoffs=range(first, first+len(batch)), aid=first//16,
                               at=200_001+first, consumed=first+len(batch), count=count)]
        checker, active, issues = self.audit(rows, events)
        self.assertEqual(issues, [])
        self.assertEqual(checker.counts['tx_accepted'], count)
        self.assertLess(reads[0], count*120, 'head lookup must not scan all in-flight rows per identity')

    def test_frontier_checks_advance_instead_of_rescanning_all_active_rows(self):
        reads = [0]
        class Row(dict):
            def __getitem__(self, name):
                reads[0] += 1
                return super().__getitem__(name)
        count = 512
        rows = [Row(expected(i)) for i in range(count)]
        checker, active, issues = self.audit(rows, [])
        reads[0] = 0
        for prefix in range(1, count+1):
            event = envelop([dispatch([], count=prefix)])[0]
            event['frontiers']['0'].update(through_ns=9+prefix, input_complete=False)
            self.assertTrue(checker.snapshot(event))
        self.assertEqual(issues, [])
        self.assertLess(reads[0], count*50, 'frontier validation must advance an ingress prefix')

    def test_unseen_earlier_release_cannot_hide_a_wrong_dispatch_head(self):
        rows = [expected(0, release=300), expected(1, release=400), expected(2, release=190)]
        event = dispatch([rows[1]], at=400, count=2)
        event['frontiers']['0'].update(through_ns=11, input_complete=False)
        _, _, issues = self.audit(rows, [event])
        self.assertEqual(issues, [('dispatch_wrong_head', 'FAIL'), ('unsafe_dispatch_frontier', 'INCONCLUSIVE')])

    def test_equal_release_and_sequence_keep_active_insertion_order_across_ports(self):
        cfg, reg = config(), manifest()
        cfg['lines'].append(dict(copy.deepcopy(cfg['lines'][0]), name='other', ingress_port=1))
        reg['ports'].append(dict(reg['ports'][0], ingress_port=1))
        first = dict(expected(line='other'), ingress_port=1, external_identity='z')
        second = dict(expected(), external_identity='a')
        for selected, want in [([first, second], []), ([second, first], [('dispatch_wrong_head', 'FAIL')])]:
            event = dispatch(selected)
            event.update(seen_next_ingress_seq={'0':1, '1':1}, frontiers={
                str(port):dict(through_ns=None, input_complete=True) for port in (0, 1)})
            _, _, issues = self.audit([first, second], [event], cfg, reg)
            self.assertEqual(issues, want)

    def test_closed_trace_line_does_not_hide_the_open_line_tx_head(self):
        cfg = config()
        other = copy.deepcopy(cfg['lines'][0])
        other.update(name='leo', trace=dict(path='fixture', sha256='0'*64))
        other['model'].update(t_ns=0, delta_ns=0, p='0')
        cfg['lines'].append(other)
        trace = dict(end_ns=1000, loss_seed=0, outage_action='hold', terminal='error', terminal_horizon_ns=None,
            rows=[dict(at_ns=at, t_ns=0, loss='0', outage=down) for at, down in [(0, False), (150, True), (250, False)]])
        rows = [expected(0, line='leo', release=70), expected(1, release=100), expected(2, release=110)]
        events = [dispatch(rows), attempt([rows[2]], handoffs=[2], consumed=3, count=3, at=200)]
        _, _, issues = self.audit(rows, events, cfg, traces={'leo':trace})
        self.assertEqual(issues, [('tx_wrong_head', 'FAIL')])

    def test_retired_states_and_stale_index_entries_do_not_accumulate(self):
        import weakref
        class State(dict):
            pass
        checker, active, issues = self.audit([], [])
        for seq in range(512):
            row = expected(seq)
            state = State(expected=row, offer=True, terminal=None, tx=False, dispatch=None, pending=False)
            identity = key(row)
            active[identity] = state
            checker.track(state)
            checker.input_next[0] = seq+1
            self.assertTrue(checker.snapshot(envelop([dispatch([], count=seq+1)])[0]))
            reference = weakref.ref(state)
            active.pop(identity)
            checker.forget(identity)
            del state
            self.assertIsNone(reference())
        self.assertEqual(issues, [])
        self.assertEqual(checker.records, {})
        self.assertTrue(all(len(heap)<=64 for heap in checker.release_index.values()))
        self.assertTrue(all(len(queue)<=64 for ports in checker.frontier_index.values() for queue in ports.values()))

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

    def audit(self, rows, events, configuration=None, registration=None, traces=None):
        active = {key(row):dict(expected=row, offer=True, terminal=None, tx=False,
                               dispatch=None, pending=False) for row in rows}
        issues = []
        checker = LiveCustody(configuration or config(), traces or {}, registration or manifest(), active,
                              lambda code,status,row=None:issues.append((code,status)))
        checker.input_next = {port:max((row["ingress_seq"]+1 for row in rows if row["ingress_port"]==port),default=0)
                              for port in checker.input_next}
        checker.input_end = set(checker.input_next)
        for event in envelop(events):
            checker.event(event)
        return checker, active, issues

    def test_dispatch_certificate_uses_closed_lower_bound_and_all_line_minima(self):
        from pathlib import Path
        import tempfile
        from tools.g2.custody import audit_host
        cases = [('fixed-boundary', 89, 100, None, False, False, True),
                 ('one-short', 88, 100, None, False, False, False),
                 ('zero-delay-boundary', 189, 0, None, False, False, True),
                 ('unknown', None, 100, None, False, False, False),
                 ('end', None, 100, None, False, True, True),
                 ('competing-line', 89, 100, None, True, False, False),
                 ('trace-boundary', 89, 0, 100, False, False, True),
                 ('trace-lower-step', 89, 0, 0, False, False, False)]
        for label, through, minimum, trace_minimum, competing, ended, safe in cases:
            cfg, reg, traces = config(), manifest(), {}
            cfg['lines'][0]['model']['t_ns'] = minimum
            if competing:
                other = copy.deepcopy(cfg['lines'][0])
                other.update(name='other', vlan_id=200)
                other['model']['t_ns'] = 0
                cfg['lines'].append(other)
            if trace_minimum is not None:
                cfg['lines'][0]['trace'] = dict(path='trace', sha256='0'*64)
                traces['cable'] = dict(end_ns=1000, terminal='error', terminal_horizon_ns=None,
                    outage_action='hold', loss_seed=0, rows=[
                        dict(at_ns=0, t_ns=100, loss='0', outage=False),
                        dict(at_ns=200, t_ns=trace_minimum, loss='0', outage=False)])
            reg['retention']['mode'] = 'full'
            reg['capacity']['join_disk_bytes'] = 8_000_000
            row = expected()
            events = [dispatch([row]), attempt([row])]
            for event in events:
                event['frontiers']['0'].update(through_ns=through, input_complete=ended)
            _, _, issues = self.audit([row], events, cfg, reg, traces)
            with tempfile.TemporaryDirectory() as directory:
                audit = audit_host(cfg, traces, reg, iter([row]),
                                   iter(envelop([offer(row), *events])), Path(directory))
            with self.subTest(case=label, checker='live'):
                self.assertEqual(('unsafe_dispatch_frontier', 'INCONCLUSIVE') in issues, not safe)
                if safe:
                    self.assertEqual(issues, [])
            with self.subTest(case=label, checker='disk'):
                self.assertEqual(bool(audit.counts['unsafe_dispatch_frontier']), not safe)
                if safe:
                    self.assertEqual(audit.issues, [])

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
