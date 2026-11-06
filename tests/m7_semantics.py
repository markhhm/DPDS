#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Canonical values/bytes/custody ledger from the merged pre-optimization M7 path."""
import hashlib
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from eal_args import eal_arguments

# Generated twice with a9b92962 production C objects and the unchanged 8a32ae58
# Core tree, linked to these test-only emitters. No field/time/order normalization.
EXPECTED='c64b71e25bbb118f340705fd18510c0b05ed0a11b047f41f2d7b1169bb415012'
with tempfile.TemporaryDirectory() as directory:
    output=Path(directory)/'semantics.ledger'
    env=dict(os.environ,HNG_M7_ORACLE=str(output))
    for name,binary in zip(('worker','engine'),sys.argv[1:]):
        command=[str(Path(binary).resolve()),*eal_arguments(1),'--no-pci','--no-huge',
                 '--no-telemetry','--in-memory','-m','256','--file-prefix=hng-m7-semantics']
        if name=='engine':command+=['--vdev=net_null0,no-rx=1','--vdev=net_null1,no-rx=1']
        result=subprocess.run(command,env=env,capture_output=True,text=True,timeout=25)
        assert result.returncode==0,result.stdout+result.stderr
    assert len(sys.argv)==3,'need worker and engine fixtures'
    actual=hashlib.sha256(output.read_bytes()).hexdigest()
    assert actual==EXPECTED,f'M7 model/metadata/bytes/custody/INVALID ledger differs from a9b92962: {actual}'
print('merged M7 model, full bytes/metadata, Q/selection/sequence/deadline, custody and INVALID ledger: byte-identical')
