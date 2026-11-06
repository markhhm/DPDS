#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Serial hybrid experiment: explicit M7 opt-in, registered threshold and hold."""
import argparse
import json
import os
from pathlib import Path
import re
import statistics
import subprocess
import sys
ROOT=Path(__file__).resolve().parents[1]


def run(binary,mode,threshold,hold,trials):
    sizing=json.loads(subprocess.check_output([binary,'--sizing',str(0 if mode=='baseline-resident' else threshold),'-1',
                                               'resident' if 'resident' in mode else 'hot'],text=True))
    cmd=['/usr/bin/time','-f','RSS_KIB=%M','prlimit','--as=4294967296','--core=0','--',sys.executable,
         str(ROOT/'tests/eal_args.py'),str(binary),'--no-pci','--no-huge','--no-telemetry','--in-memory',
         '-m',str(sizing['eal_mib']),f'--file-prefix=hng-hybrid-{os.getpid()}','--',mode,'-1',str(trials),str(threshold),str(hold)]
    p=subprocess.run(cmd,capture_output=True,text=True,timeout=300)
    if p.returncode:raise RuntimeError(f'{mode} T={threshold}:\n{p.stdout}\n{p.stderr}')
    rows=[json.loads(x) for x in p.stdout.splitlines() if x.startswith('{')]
    assert len(rows)==trials, p.stdout
    rss=int(re.search(r'RSS_KIB=(\d+)',p.stderr)[1])
    if 'resident' in mode:
        assert all(x['live_storage_bytes']>=2**31 and x['operations']>=x['window'] for x in rows),rows
        assert rss>=2**21,rss
    return dict(rows=rows,median_ns=statistics.median(x['ns_per_packet'] for x in rows),max_rss_kib=rss,sizing=sizing)


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('binary',type=Path)
    p.add_argument('--m7-opt-in',action='store_true')
    p.add_argument('--thresholds',nargs='+',type=int,default=[128,256,512,663,1024])
    p.add_argument('--hold-ns',type=int,default=10_000_000)
    p.add_argument('--resident',action='store_true')
    p.add_argument('--trials',type=int,choices=range(1,11),default=5)
    p.add_argument('--out',type=Path)
    a=p.parse_args()
    if not a.m7_opt_in:p.error('this standalone experiment requires --m7-opt-in; no engine option is changed')
    if not 0<a.hold_ns<2**64 or any(not 64<=x<=1522 for x in a.thresholds):p.error('hold must be positive uint64; T must be64..1522 charged L2 bytes')
    binary=a.binary.resolve();layout=json.loads(subprocess.check_output([binary,'--describe'],text=True))
    data=dict(layout=layout,m7_opt_in=True,registered_hold_ns=a.hold_ns,AS_bytes=4294967296,
              live_resident_target_bytes=2**31,heavy_jobs_serial=True,cpu_affinity=sorted(os.sched_getaffinity(0)),references=[],cases=[])
    for t in a.thresholds:
        hot=run(binary,'baseline',t,a.hold_ns,a.trials)
        cold=None
        for mode in (['hot','resident'] if a.resident else ['hot']):
            if mode=='resident':cold=run(binary,'baseline-resident',t,a.hold_ns,a.trials)
            result=run(binary,mode,t,a.hold_ns,a.trials)
            reference=hot if mode=='hot' else cold
            delta=result['median_ns']-reference['median_ns']
            hot_delta=result['median_ns']-hot['median_ns']
            charged=max(0.0,delta) # Do not credit a negative cold-memory difference as an engine speedup.
            projected=[1/(1/rate+charged/1000) for rate in [13,14.5]]
            case=dict(threshold_l2=t,mode=mode,compact_packet_fraction=sum(x<=t for x in [64,512,1522])/3,
                      matched_reference_increment_ns=delta,charged_increment_ns=charged,hot_reference_increment_ns=hot_delta,reference_median_ns=reference['median_ns'],hot_reference_median_ns=hot['median_ns'],
                      projected_Mpps=projected,**result)
            data['cases'].append(case)
            data['references'].append(dict(threshold_l2=t,mode=mode,result=reference))
            print(f'T={t} {mode} matched_delta_ns={delta:.3f} hot_reference_delta_ns={hot_delta:.3f} projection_Mpps={projected}',flush=True)
            if a.out:a.out.write_text(json.dumps(data,indent=2)+'\n')
    return 0
if __name__=='__main__':raise SystemExit(main())
