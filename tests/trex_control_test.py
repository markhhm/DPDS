# SPDX-License-Identifier: Apache-2.0
"""No server: control only the reserved pair and prove TX cessation from counters."""
from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))


def nic_counters(port):
    values = {'tx_unicast_packets':1000+port, 'rx_unicast_packets':900+port,
              'tx_multicast_packets':2, 'rx_multicast_packets':3,
              'tx_broadcast_packets':4, 'rx_broadcast_packets':5,
              'rx_missed_errors':0, 'rx_errors':0, 'rx_mbuf_allocation_errors':0}
    for direction in ('tx','rx'):
        for size in ('64','65_to_127','128_to_255','256_to_511','512_to_1023','1024_to_1522'):
            values[f'{direction}_size_{size}_packets'] = port+10
    values['tx_size_1523_to_max_packets'] = 7
    return values


class XStats:
    def __init__(self,client,port):
        self.client,self.port=client,port;self.latest={}
    def get_xstats(self):return self
    def update_sync(self,rpc):
        if self.client.fail=='cached_after_start' and self.client.active:return False
        self.latest=self.client.xstats.get(self.port,nic_counters(self.port)).copy()
        # Successful AbstractStats.update_sync in v3.08 returns None.
    def to_dict(self):return self.latest.copy()


class Client:
    def __init__(self,counts=None,active=False,fail=None):
        self.counts=list(counts or [[10,20]]*4);self.active=active;self.fail=fail
        self.calls=[];self.owned=[];self.baseline={};self.xstats={}
    def _on_connect_clear_stats(self):
        # v3.08 connect resets the client port baseline, not hardware counters.
        self.calls.append(('clear_stats',))
        self.baseline=dict(zip(self.selected_ports,self.counts[0]))
        return True
    def connect(self):
        self.calls.append(('connect',))
        self.ports={port:XStats(self,port) for port in self.selected_ports}
        self._on_connect_clear_stats()
    def acquire(self,ports,force):
        self.calls.append(('acquire',ports,force))
        if self.fail=='acquire':raise RuntimeError('reserved port unavailable')
        self.owned=list(ports)
    def get_acquired_ports(self):return self.owned
    def release(self,ports):
        self.calls.append(('release',ports));self.owned=[]
        if self.fail=='release':raise RuntimeError('release failed')
    def disconnect(self,**kwargs):self.calls.append(('disconnect',kwargs))
    def is_traffic_active(self,ports):return ports if self.active else []
    def get_stats(self,ports,sync_now):
        self.calls.append(('stats',ports,sync_now));counts=self.counts[0]
        if len(self.counts)>1:self.counts.pop(0)
        return {p:dict(opackets=n-self.baseline[p] if p in self.baseline else n,
                       ipackets=100*sum(c[0]=='stats' for c in self.calls)) for p,n in zip(ports,counts)}
    def get_xstats(self,port_id):
        self.calls.append(('xstats',port_id))
        self.clock.sleep(.01)
        if self.fail=='xstats':raise RuntimeError('xstats RPC failed')
        if self.fail=='after_start_xstats' and self.active:raise RuntimeError('xstats RPC failed')
        stats=self.ports[port_id].get_xstats()
        stats.update_sync(None)  # v3.08 get_xstats ignores a failed refresh return code.
        return stats.to_dict()
    def remove_all_streams(self,ports):self.calls.append(('remove',ports))
    def add_streams(self,streams,ports):self.calls.append(('add',streams,ports))
    def start(self,**kwargs):self.calls.append(('start',kwargs));self.active=True
    def stop(self,ports):self.calls.append(('stop',ports));self.active=self.fail=='active'


class Clock:
    now=0
    def read(self):return self.now
    def sleep(self,seconds):self.now+=seconds


class ControlTests(unittest.TestCase):
    def invoke(self,command,client,extra=(),loader=None,ports=(2,5)):
        from scripts import trex_control
        calls=[];clock=Clock();out=io.StringIO();err=io.StringIO()
        client.clock=clock;client.selected_ports=list(ports)
        def load(path,**kwargs):
            calls.append((path,kwargs));print('profile diagnostic')
            class Profile:
                def get_streams(self):return [kwargs['direction']]
            return Profile()
        with redirect_stdout(out),redirect_stderr(err):
            code=trex_control.main([command,'--server','${GEN_SERVER}','--ports',*map(str,ports),
                '--profile','${GEN_STL_PROFILE}',*extra],api=(lambda **kw:client,loader or load),
                clock=clock.read,sleep=clock.sleep)
        self.assertEqual(len(out.getvalue().splitlines()),1,out.getvalue())
        result=json.loads(out.getvalue())
        self.assertEqual(set(result),{'schema','command','status','server','ports','profile',
            'opackets','tx_active','stable_samples','error','xstats'})
        self.assertEqual(result['ports'],list(ports));self.assertEqual(result['command'],command)
        self.assertEqual(result['schema'],'hng-trex-control-v2')
        self.assertEqual(set(result['xstats']),{'before_start','after_start','after_stop','status'})
        self.assertIn(('disconnect',dict(stop_traffic=False,release_ports=False)),client.calls)
        self.assertIn(('acquire',list(ports),False),client.calls)
        return code,result,calls

    def test_connection_does_not_reset_the_software_counter_baseline(self):
        from scripts import trex_control
        client=Client(counts=[[100,200]])
        clock=Clock();client.clock=clock;client.selected_ports=[2,5]
        result={'xstats':{},'stable_samples':0}
        trex_control.control(SimpleNamespace(command='status',ports=[2,5]),client,None,
                             result,clock.read,clock.sleep)
        self.assertEqual(result['opackets'],[100,200])
        self.assertNotIn(('clear_stats',),client.calls)

    def test_every_command_preserves_counter_epoch_and_publishes_all_nic_counters(self):
        for command,stages in (('start',('before_start','after_start')),
                               ('stop',('after_stop',)),('status',('status',))):
            with self.subTest(command=command):
                client=Client(counts=[[100,200]])
                code,result,_=self.invoke(command,client)
                self.assertEqual(code,0,result)
                self.assertEqual(result['opackets'],[100,200])
                self.assertNotIn(('clear_stats',),client.calls)
                for stage,rows in result['xstats'].items():
                    if stage not in stages:
                        self.assertIsNone(rows)
                        continue
                    self.assertEqual([row['port'] for row in rows],[2,5])
                    for row in rows:
                        self.assertEqual(set(row),{'port','monotonic_start_s','monotonic_end_s','counters'})
                        self.assertEqual(row['counters'],nic_counters(row['port']))
                        self.assertGreaterEqual(row['monotonic_start_s'],0)
                        self.assertAlmostEqual(row['monotonic_end_s']-row['monotonic_start_s'],.01)
                if command=='start':
                    events=[call[0] for call in client.calls]
                    self.assertEqual(events[events.index('xstats'):events.index('start')],['xstats','xstats'])
                    self.assertGreaterEqual(result['xstats']['after_start'][0]['monotonic_start_s'],
                                            result['xstats']['before_start'][-1]['monotonic_end_s'])

    def test_missing_bad_or_failed_nic_evidence_is_error_not_stopped(self):
        cases=[]
        for key in ('tx_unicast_packets','rx_unicast_packets','rx_errors',
                    'rx_missed_errors','rx_mbuf_allocation_errors','tx_size_64_packets','rx_size_64_packets'):
            values=nic_counters(5);del values[key];cases.append(values)
        for value in (True,None,-1,1.5,2**64):
            values=nic_counters(5);values['rx_errors']=value;cases.append(values)
        for values in cases:
            with self.subTest(values=values):
                client=Client();client.xstats[5]=values
                code,result,_=self.invoke('stop',client)
                self.assertEqual((code,result['status']),(2,'ERROR'))
                self.assertIsNone(result['xstats']['after_stop'])
                self.assertTrue(result['error'])
        code,result,_=self.invoke('stop',Client(fail='xstats'))
        self.assertEqual((code,result['status']),(2,'ERROR'))
        self.assertIn('xstats RPC failed',result['error'])

    def test_failed_nic_baseline_never_starts_and_failed_post_start_snapshot_stops(self):
        for failure,started in (('xstats',False),('after_start_xstats',True)):
            client=Client(fail=failure)
            code,result,_=self.invoke('start',client)
            self.assertEqual((code,result['status']),(2,'ERROR'))
            self.assertEqual(any(call[0]=='start' for call in client.calls),started)
            self.assertEqual(('stop',[2,5]) in client.calls,started)

    def test_nonraising_failed_refresh_cannot_reuse_the_before_start_cache(self):
        client=Client(fail='cached_after_start')
        code,result,_=self.invoke('start',client)
        self.assertEqual((code,result['status']),(2,'ERROR'))
        self.assertIsNotNone(result['xstats']['before_start'])
        self.assertIsNone(result['xstats']['after_start'])
        self.assertIn(('stop',[2,5]),client.calls)

    def test_one_port_stop_preserves_a_later_rx_drain_snapshot(self):
        client=Client(counts=[[7]],active=True)
        client.xstats[5]=nic_counters(5)
        code,stopped,_=self.invoke('stop',client,ports=(5,))
        self.assertEqual((code,stopped['status']),(0,'STOPPED'))
        client.xstats[5]['rx_unicast_packets']+=9
        code,later,_=self.invoke('status',client,ports=(5,))
        self.assertEqual((code,later['status']),(0,'IDLE'))
        before=stopped['xstats']['after_stop'][0]['counters']
        after=later['xstats']['status'][0]['counters']
        self.assertEqual(before['tx_unicast_packets'],after['tx_unicast_packets'])
        self.assertEqual(after['rx_unicast_packets']-before['rx_unicast_packets'],9)
        self.assertEqual(stopped['opackets'],later['opackets'])

    def test_start_both_directions_indefinitely_without_total_or_forcing(self):
        client=Client();code,result,profiles=self.invoke('start',client)
        self.assertEqual((code,result['status']),(0,'RUNNING'))
        self.assertEqual(profiles,[('${GEN_STL_PROFILE}',dict(direction=0,port_id=2)),
                                   ('${GEN_STL_PROFILE}',dict(direction=1,port_id=5))])
        self.assertIn(('start',dict(ports=[2,5],mult='1',force=False,duration=-1,total=False)),client.calls)
        self.assertEqual(result['tx_active'],[True,True])
        self.assertNotIn(('stop',[2,5]),client.calls)

    def test_one_port_start_loads_only_direction_zero_on_that_port(self):
        client=Client(counts=[[7]])
        code,result,profiles=self.invoke('start',client,ports=(5,))
        self.assertEqual((code,result['status']),(0,'RUNNING'))
        self.assertEqual(profiles,[('${GEN_STL_PROFILE}',dict(direction=0,port_id=5))])
        self.assertIn(('start',dict(ports=[5],mult='1',force=False,duration=-1,total=False)),client.calls)
        self.assertIn(('release',[5]),client.calls)
        self.assertEqual(result['opackets'],[7]);self.assertEqual(result['tx_active'],[True])
        self.assertNotIn(('stop',[5]),client.calls)

    def test_one_port_status_preserves_tx_and_reports_one_counter(self):
        client=Client(counts=[[7]],active=True)
        code,result,profiles=self.invoke('status',client,ports=(5,))
        self.assertEqual((code,result['status']),(0,'RUNNING'))
        self.assertEqual(result['opackets'],[7]);self.assertEqual(result['tx_active'],[True])
        self.assertFalse(profiles)
        self.assertFalse(any(c[0] in ('remove','add','start','stop') for c in client.calls))

    def test_one_port_stop_proves_tx_settled_while_rx_counters_increase(self):
        client=Client(counts=[[7],[8],[8],[8]],active=True)
        code,result,profiles=self.invoke('stop',client,ports=(5,))
        self.assertEqual((code,result['status']),(0,'STOPPED'))
        self.assertEqual(result['opackets'],[8]);self.assertEqual(result['tx_active'],[False])
        self.assertEqual(result['stable_samples'],2);self.assertFalse(profiles)
        self.assertEqual(sum(c[0]=='stats' for c in client.calls),4)
        self.assertIn(('stop',[5]),client.calls)

    def test_one_port_reset_or_timeout_fails_closed(self):
        for client in (Client(counts=[[7],[6]]),Client(counts=[[i] for i in range(20)])):
            code,result,_=self.invoke('stop',client,ports=(5,),extra=('--timeout','1','--poll-interval','.1'))
            self.assertEqual((code,result['status']),(2,'ERROR'));self.assertTrue(result['error'])

    def test_status_preserves_traffic_and_streams(self):
        client=Client(active=True);code,result,profiles=self.invoke('status',client)
        self.assertEqual((code,result['status']),(0,'RUNNING'))
        self.assertEqual(result['opackets'],[10,20]);self.assertFalse(profiles)
        self.assertFalse(any(c[0] in ('remove','add','start','stop') for c in client.calls))

    def test_stop_waits_for_two_unchanged_intervals_for_both_ports(self):
        client=Client(counts=[[10,20],[11,20],[11,21],[11,21],[11,21]],active=True)
        code,result,profiles=self.invoke('stop',client)
        self.assertEqual((code,result['status']),(0,'STOPPED'))
        self.assertEqual(result['stable_samples'],2);self.assertEqual(result['opackets'],[11,21])
        self.assertEqual(result['tx_active'],[False,False]);self.assertFalse(profiles)
        self.assertEqual(sum(c[0]=='stats' for c in client.calls),5)
        self.assertEqual(sum(c[0]=='stop' for c in client.calls),1)

    def test_reset_or_bad_counter_is_not_stopped(self):
        for counts in ([[10,20],[9,20]],[[True,20]],[[None,20]],[[-1,20]]):
            with self.subTest(counts=counts):
                code,result,_=self.invoke('stop',Client(counts=counts))
                self.assertEqual((code,result['status']),(2,'ERROR'));self.assertTrue(result['error'])

    def test_timeout_or_active_stream_does_not_claim_stopped(self):
        for client in (Client(counts=[[i,i] for i in range(20)]),Client(fail='active')):
            code,result,_=self.invoke('stop',client,extra=('--timeout','1','--poll-interval','.1'))
            self.assertEqual((code,result['status']),(2,'ERROR'));self.assertIn('timeout',result['error'])

    def test_busy_pair_and_active_start_are_not_forced(self):
        for client in (Client(fail='acquire'),Client(active=True)):
            code,result,_=self.invoke('start',client)
            self.assertEqual((code,result['status']),(2,'ERROR'))
            self.assertFalse(any(c[0] in ('remove','add','start','stop') for c in client.calls))

    def test_profile_failure_precedes_stream_mutation(self):
        def bad(*args,**kwargs):raise RuntimeError('bad profile')
        client=Client();code,result,_=self.invoke('start',client,loader=bad)
        self.assertEqual((code,result['status']),(2,'ERROR'))
        self.assertFalse(any(c[0] in ('remove','add','start','stop') for c in client.calls))

    def test_cleanup_failure_is_not_success(self):
        code,result,_=self.invoke('status',Client(fail='release'))
        self.assertEqual((code,result['status']),(2,'ERROR'))

    def test_invalid_args_never_connect_and_emit_one_error_json(self):
        from scripts import trex_control
        for extra in (['--ports','2','2'],['--ports','2','5','7'],['--ports','-1'],['--ports'],
                      ['--timeout','nan'],['--poll-interval','0']):
            out=io.StringIO();factory=Mock()
            with redirect_stdout(out):
                code=trex_control.main(['stop','--server','${GEN_SERVER}','--ports','2','5',
                    '--profile','${GEN_STL_PROFILE}',*extra],api=(factory,None))
            self.assertEqual(code,2);self.assertEqual(json.loads(out.getvalue())['status'],'ERROR')
            factory.assert_not_called()


if __name__=='__main__':unittest.main()
