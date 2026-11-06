#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""A compact copy must not create closed-loop generator population credit."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
root=Path(__file__).resolve().parents[1]
with tempfile.TemporaryDirectory() as directory:
    cfg=json.loads((root/'perf/ring_rate.json').read_text())
    for line in cfg['lines']:
        line['m7_storage']={'mode':'m7-hybrid-v1','offered_bps':400_000_000_000,
            'composition':[{'l2_bytes':l,'weight':1} for l in (64,512,1522)]}
    path=Path(directory)/'rate.json';path.write_text(json.dumps(cfg))
    result=subprocess.run([sys.executable,str(root/'perf/ring_rate.py'),'--io-split',sys.argv[1],str(path),
        '--','-b','X','-n','128','-w','0.02','-d','0.1'],capture_output=True,text=True,timeout=30)
    assert result.returncode==0,result.stdout+result.stderr
    assert 'm7_population_bound=64' in result.stdout,result.stdout
    assert 'conservation=PASS status=DRAINED' in result.stdout,result.stdout
    print(result.stdout)
