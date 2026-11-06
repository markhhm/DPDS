#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Production M7 quotas without prefaulting production-size Core metadata."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile

# Direct script execution puts perf/, not the repository root, on sys.path.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools.g2.m7_storage import plan

registration={'mode':'m7-hybrid-v1','composition':[{'l2_bytes':l,'weight':1} for l in (64,512,1522)]}
with tempfile.TemporaryDirectory() as directory:
    path=Path(directory)/'registration.json';path.write_text(json.dumps(registration))
    args=['--c-bps','90e9','--t-ms','142','--delta-ms','320','--q-ms','7.456541',
          '--min-frame','64','--max-frame','1522','--burst','16','--rx-desc','16384',
          '--page-bytes','1073741824','--m7-registration',str(path)]
    r=subprocess.run([sys.argv[1],*args],capture_output=True,text=True)
    assert r.returncode==0,r.stdout+r.stderr
    expected=plan(registration,dict(c_bps=90_000_000_000,q_bytes=83_886_086,t_ns=142_000_000,delta_ns=320_000_000),
                  dict(min_q_bytes=64,max_q_bytes=1522,burst_bound_packets=16))
    for key,value in expected.items():assert f'{key}={value}' in r.stdout,(key,value,r.stdout)
    assert 'slots=9377250' in r.stdout and 'eal_reserve_gib=24' in r.stdout,r.stdout
    assert 'normal_mbufs=3125761' in r.stdout and 'total_gib=28.045' in r.stdout,r.stdout
    path.write_text('{}')
    bad=subprocess.run([sys.argv[1],*args],capture_output=True,text=True)
    assert bad.returncode==2,bad.stdout+bad.stderr
print('independent exact M7 quotas, no allocation, 28 GiB split and strict registration: PASS')
