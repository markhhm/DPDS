# SPDX-License-Identifier: Apache-2.0
"""No server: control only the reserved pair and prove TX cessation from counters."""
from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))


class Client:
    def __init__(self,counts=None,active=False,fail=None):
        self.counts=list(counts or [[10,20]]*4);self.active=active;self.fail=fail
        self.calls=[];self.owned=[]
    def connect(self):self.calls.append(('connect',))
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
        return {p:dict(opackets=n) for p,n in zip(ports,counts)}
    def remove_all_streams(self,ports):self.calls.append(('remove',ports))
    def add_streams(self,streams,ports):self.calls.append(('add',streams,ports))
    def start(self,**kwargs):self.calls.append(('start',kwargs));self.active=True
    def stop(self,ports):self.calls.append(('stop',ports));self.active=self.fail=='active'


class Clock:
    now=0
    def read(self):return self.now
    def sleep(self,seconds):self.now+=seconds


class ControlTests(unittest.TestCase):
    def invoke(self,command,client,extra=(),loader=None):
        from scripts import trex_control
        calls=[];clock=Clock();out=io.StringIO();err=io.StringIO()
        def load(path,**kwargs):
            calls.append((path,kwargs));print('profile diagnostic')
            class Profile:
                def get_streams(self):return [kwargs['direction']]
            return Profile()
        with redirect_stdout(out),redirect_stderr(err):
            code=trex_control.main([command,'--server','${GEN_SERVER}','--ports','2','5',
                '--profile','${GEN_STL_PROFILE}',*extra],api=(lambda **kw:client,loader or load),
                clock=clock.read,sleep=clock.sleep)
        self.assertEqual(len(out.getvalue().splitlines()),1,out.getvalue())
        result=json.loads(out.getvalue())
        self.assertEqual(set(result),{'schema','command','status','server','ports','profile',
            'opackets','tx_active','stable_samples','error'})
        self.assertEqual(result['ports'],[2,5]);self.assertEqual(result['command'],command)
        self.assertEqual(result['schema'],'hng-trex-control-v1')
        self.assertIn(('disconnect',dict(stop_traffic=False,release_ports=False)),client.calls)
        self.assertNotIn(('acquire',[2,5],True),client.calls)
        return code,result,calls

    def test_start_both_directions_indefinitely_without_total_or_forcing(self):
        client=Client();code,result,profiles=self.invoke('start',client)
        self.assertEqual((code,result['status']),(0,'RUNNING'))
        self.assertEqual(profiles,[('${GEN_STL_PROFILE}',dict(direction=0,port_id=2)),
                                   ('${GEN_STL_PROFILE}',dict(direction=1,port_id=5))])
        self.assertIn(('start',dict(ports=[2,5],mult='1',force=False,duration=-1,total=False)),client.calls)
        self.assertEqual(result['tx_active'],[True,True])
        self.assertNotIn(('stop',[2,5]),client.calls)

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
        for extra in (['--ports','2','2'],['--timeout','nan'],['--poll-interval','0']):
            out=io.StringIO()
            with redirect_stdout(out):
                code=trex_control.main(['stop','--server','${GEN_SERVER}','--ports','2','5',
                    '--profile','${GEN_STL_PROFILE}',*extra],api=(None,None))
            self.assertEqual(code,2);self.assertEqual(json.loads(out.getvalue())['status'],'ERROR')


if __name__=='__main__':unittest.main()
