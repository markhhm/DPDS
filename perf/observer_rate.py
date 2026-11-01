#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Opt-in software-only binary capture load check; requires seven available CPUs."""
import argparse
import json
import os
from pathlib import Path
import re
import subprocess


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('binary')
    parser.add_argument('output', type=Path)
    parser.add_argument('--seconds', type=float, default=60)
    parser.add_argument('--discard', action='store_true', help='C-path rate only; write to /dev/null and retain no evidence')
    parser.add_argument('--cpus', help='five pipeline CPUs, encoder CPU, generator CPU; I/O shares control CPU')
    args = parser.parse_args()
    cpus = list(map(int, args.cpus.split(','))) if args.cpus else sorted(os.sched_getaffinity(0))[:7]
    if (len(cpus) != 7 or len(set(cpus)) != 7 or not set(cpus) <= os.sched_getaffinity(0)
            or not 0 < args.seconds <= 600):
        parser.error('seven distinct available CPUs and a duration in (0,600] are required')
    args.output.mkdir(parents=True, exist_ok=False)
    command = [str(Path(args.binary).resolve()), '--lcores='+','.join(f'{i}@{c}' for i,c in enumerate(cpus[:5])),
        '--no-pci', '--no-huge', '--no-telemetry', '-m', '512', '--in-memory', f'--file-prefix=hng-binary-rate-{os.getpid()}',
        '--', '-c', str(Path(__file__).with_name('ring_rate.json').resolve()), '-d', str(args.seconds), '-w', '0',
        '-o', '/dev/null' if args.discard else str((args.output/'native.hngb').resolve()), '-r', '850000', '-a', str(cpus[5]), '-g', str(cpus[6])]
    if args.discard:
        command.append('-D')
    (args.output/'command.json').write_text(json.dumps(dict(command=command,scope='software-only',
        discard=args.discard,main_mask_bits=8,sampling_location='no evidence retention' if args.discard else 'downstream retention; all native evidence retained'), indent=2))
    with (args.output/'dut.log').open('w') as log:
        result = subprocess.run(command, stdout=log, stderr=subprocess.STDOUT, text=True, timeout=args.seconds+30)
    output = (args.output/'dut.log').read_text()
    assert result.returncode == 0, output[-16000:]
    assert 'conservation=PASS status=DRAINED' in output, output[-16000:]
    assert 'q_drop=0 other_drops=0' in output, output[-16000:]
    rates = {str(p):int(re.search(fr'egress{p}_pps=(\d+)',output)[1]) for p in (0,1)}
    health = [{name:int(value) for name,value in re.findall(r'(\w+)=(\d+)', line)}
              for line in output.splitlines() if line.startswith('observer producer=')]
    assert len(health) == 7 and 'observer_complete=1' in output, output[-16000:]
    for row in health:
        assert row['dropped'] == row['capture_drops'] == row['identity_errors'] == row['malformed'] == 0, row
        assert row['attempted'] == row['emitted'] and row['high_water'] < row['capacity'], row
    assert min(rates.values()) >= 820000, rates
    storage = {name:int(value) for name,value in re.findall(r'(\w+)=(\d+)',
        next(line for line in output.splitlines() if line.startswith('observer_io ')))}
    assert storage['pending'] == 0 and storage['high_water'] < storage['capacity'], storage
    summary = dict(status='PASS',scope='software-only',duration_s=args.seconds,pps=rates,health=health,
        capture_bytes=None if args.discard else (args.output/'native.hngb').stat().st_size,discard=args.discard,cpus=cpus,storage=storage)
    (args.output/'result.json').write_text(json.dumps(summary,indent=2))
    print(json.dumps(summary))


if __name__ == '__main__':
    main()
