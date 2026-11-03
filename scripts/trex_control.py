#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Non-interactive STL control for one or two reserved ports; stdout is one JSON reply."""
import argparse
from contextlib import redirect_stdout
import json
import math
import sys
import time


REQUIRED_XSTATS = ('tx_unicast_packets','rx_unicast_packets','rx_missed_errors',
    'rx_errors','rx_mbuf_allocation_errors') + tuple(
        f'{direction}_size_{size}_packets' for direction in ('tx','rx')
        for size in ('64','65_to_127','128_to_255','256_to_511','512_to_1023','1024_to_1522'))


class Parser(argparse.ArgumentParser):
    def error(self,message):raise ValueError(message)


def snapshot(client,ports,result):
    stats=client.get_stats(ports=ports,sync_now=True)
    counts=[stats[port]['opackets'] for port in ports]
    if any(type(value) is not int or value<0 for value in counts):
        raise ValueError('opackets must be non-negative integers for all selected ports')
    result['opackets']=counts
    result['tx_active']=[bool(client.is_traffic_active(ports=[port])) for port in ports]
    return counts


def nic_snapshot(client,ports,clock):
    rows=[]
    for port in ports:
        stats=client.ports[port].get_xstats()
        update=stats.update_sync
        def checked_update(rpc):
            rc=update(rpc)
            if rc is not None and not rc:
                raise RuntimeError(f'port {port}: NIC xstats refresh failed')
            return rc
        start=clock()
        # v3.08 get_xstats() ignores failed refresh return codes and can return stale data.
        stats.update_sync=checked_update
        try:
            counters=client.get_xstats(port_id=port)
        finally:
            stats.update_sync=update
        end=clock()
        if not isinstance(counters,dict) or not set(REQUIRED_XSTATS)<=counters.keys():
            raise ValueError(f'port {port}: required NIC xstats are unavailable')
        if any(not isinstance(name,str) or type(value) is not int or not 0<=value<2**64
               for name,value in counters.items()):
            raise ValueError(f'port {port}: NIC xstats must be named uint64 counters')
        rows.append(dict(port=port,monotonic_start_s=start,monotonic_end_s=end,
                         counters=counters.copy()))
    return rows


def control(args,client,load_profile,result,clock,sleep):
    connected=False
    try:
        if not callable(getattr(client,'_on_connect_clear_stats',None)):
            raise ValueError('matching TRex v3.08 connection stats hook required')
        # v3.08 connect() resets port baselines through this hook; its caller checks truthiness.
        client._on_connect_clear_stats=lambda:True
        client.connect();connected=True
        client.acquire(ports=args.ports,force=False)
        if args.command=='start':
            if client.is_traffic_active(ports=args.ports):raise ValueError('selected ports already have active traffic')
            streams=[load_profile(args.profile,direction=direction,port_id=port).get_streams()
                     for direction,port in enumerate(args.ports)]
            if not all(streams):raise ValueError('profile must provide streams for each selected direction')
            client.remove_all_streams(ports=args.ports)
            for port,stream in zip(args.ports,streams):client.add_streams(stream,ports=[port])
            result['xstats']['before_start']=nic_snapshot(client,args.ports,clock)
            try:
                client.start(ports=args.ports,mult='1',force=False,duration=-1,total=False)
                snapshot(client,args.ports,result)
                if not all(result['tx_active']):raise ValueError('all selected ports must have active traffic after start')
                result['xstats']['after_start']=nic_snapshot(client,args.ports,clock)
            except Exception:
                client.stop(ports=args.ports)
                raise
            result['status']='RUNNING'
        elif args.command=='stop':
            client.stop(ports=args.ports)
            deadline=clock()+args.timeout;previous=None
            while True:
                if clock()>=deadline:raise TimeoutError('timeout waiting for selected TX counters to settle')
                counts=snapshot(client,args.ports,result)
                if previous is not None and any(now<old for now,old in zip(counts,previous)):
                    raise ValueError('opackets counter reset during stop')
                result['stable_samples']=(result['stable_samples']+1 if counts==previous
                    and not any(result['tx_active']) else 0)
                if clock()>=deadline:raise TimeoutError('timeout waiting for selected TX counters to settle')
                if result['stable_samples']>=2:break
                previous=counts;sleep(min(args.poll_interval,max(0,deadline-clock())))
            result['xstats']['after_stop']=nic_snapshot(client,args.ports,clock)
            result['status']='STOPPED'
        else:
            snapshot(client,args.ports,result)
            result['xstats']['status']=nic_snapshot(client,args.ports,clock)
            result['status']='RUNNING' if any(result['tx_active']) else 'IDLE'
    finally:
        if connected:
            try:
                owned=[port for port in client.get_acquired_ports() if port in args.ports]
                if owned:client.release(ports=owned)
            finally:
                # The API default stops traffic. A short-lived start/status must preserve it.
                client.disconnect(stop_traffic=False,release_ports=False)


def main(argv=None,api=None,clock=time.monotonic,sleep=time.sleep):
    result=dict(schema='hng-trex-control-v2',command=None,status='ERROR',server=None,
        ports=[],profile=None,opackets=None,tx_active=None,stable_samples=0,error=None,
        xstats=dict(before_start=None,after_start=None,after_stop=None,status=None))
    try:
        parser=Parser(description=__doc__)
        parser.add_argument('command',choices=('start','stop','status'))
        parser.add_argument('--server',required=True)
        parser.add_argument('--ports',required=True,type=int,nargs='+')
        parser.add_argument('--profile',required=True)
        parser.add_argument('--timeout',type=float,default=5)
        parser.add_argument('--poll-interval',type=float,default=.25)
        args=parser.parse_args(argv)
        result.update(command=args.command,server=args.server,ports=args.ports,profile=args.profile)
        if not 1<=len(args.ports)<=2 or len(set(args.ports))!=len(args.ports) or min(args.ports)<0:
            raise ValueError('one or two distinct non-negative ports are required')
        if (not math.isfinite(args.timeout) or not math.isfinite(args.poll_interval)
                or args.poll_interval<=0 or args.timeout<2*args.poll_interval):
            raise ValueError('finite timeout >= two positive poll intervals is required')
        with redirect_stdout(sys.stderr):
            if api is None:
                from trex.stl.api import STLClient,STLProfile
                api=(STLClient,STLProfile.load)
            client=api[0](server=args.server,verbose_level='none')
            control(args,client,api[1],result,clock,sleep)
    except Exception as error:
        result.update(status='ERROR',error=str(error))
    print(json.dumps(result,allow_nan=False,separators=(',',':')),flush=True)
    return 2 if result['status']=='ERROR' else 0


if __name__=='__main__':sys.exit(main())
