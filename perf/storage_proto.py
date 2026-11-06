#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Serial standalone CPU experiment. Resident allocators hold >=2 GiB of live data.

This measures storage operations, not the integrated engine or NIC capacity.
Every child has an AS4GiB limit. No heavy cases overlap. Default runs a hot
mixed smoke; --full adds all lengths, --resident enables the >=2GiB cases.
"""
import argparse
import json
import os
from pathlib import Path
import re
import resource
import statistics
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
LENGTHS = [60, 64, 128, 129, 508, 512, 513, 1518]


def run(binary, mode, kind, trials):
    command = ['prlimit', '--as=4294967296', '--core=0', '--', sys.executable,
               str(ROOT/'tests/eal_args.py'), str(binary), '--no-pci', '--no-huge',
               '--no-telemetry', '--in-memory', '-m', '128',
               f'--file-prefix=hng-compact-{os.getpid()}', '--', mode, str(kind), str(trials)]
    # time's max RSS belongs to this one child. /proc identities are not retained.
    if Path('/usr/bin/time').exists():
        command = ['/usr/bin/time', '-f', 'RSS_KIB=%M', *command]
    result = subprocess.run(command, capture_output=True, text=True, timeout=300)
    if result.returncode:
        raise RuntimeError(f'{mode} length-index={kind}: {result.stdout}\n{result.stderr}')
    rows = [json.loads(line) for line in result.stdout.splitlines() if line.startswith('{')]
    assert len(rows) == trials, result.stdout
    if mode == 'resident':
        assert all(row['allocator_bytes'] >= 2**31 for row in rows), rows
        assert all(row['operations'] >= row['window'] for row in rows), rows
    rss = re.search(r'RSS_KIB=(\d+)', result.stderr)
    return rows, int(rss[1]) if rss else None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('binary', type=Path)
    parser.add_argument('--full', action='store_true')
    parser.add_argument('--resident', action='store_true')
    parser.add_argument('--trials', type=int, default=3, choices=range(1, 11))
    parser.add_argument('--out', type=Path)
    args = parser.parse_args()
    binary = args.binary.resolve()
    layout = json.loads(subprocess.check_output([binary, '--describe'], text=True))
    data = dict(layout=layout, trials=args.trials, AS_bytes=4294967296,
                resident_target_bytes=2**31, heavy_jobs_serial=True, cases=[])
    for kind in ([-1, *range(8)] if args.full else [-1]):
        baseline, rss0 = run(binary, 'baseline', kind, args.trials)
        base = statistics.median(row['ns_per_packet'] for row in baseline)
        for mode in (['hot', 'resident'] if args.resident else ['hot']):
            rows, rss = run(binary, mode, kind, args.trials)
            elapsed = statistics.median(row['ns_per_packet'] for row in rows)
            delta = elapsed-base
            # Affected owner is assumed to have been the engine bottleneck.
            projection = [1/(1/rate+delta/1000) for rate in (13, 14.5)]
            data['cases'].append(dict(length='X' if kind == -1 else LENGTHS[kind], mode=mode,
                baseline_ns=base, compact_ns=elapsed, incremental_ns=delta,
                projected_engine_Mpps=projection, baseline_rows=baseline, rows=rows,
                baseline_max_rss_kib=rss0, max_rss_kib=rss))
            print(f'length={data["cases"][-1]["length"]} mode={mode} baseline_ns={base:.3f} '
                  f'compact_ns={elapsed:.3f} incremental_ns={delta:.3f} '
                  f'projected_Mpps={projection[0]:.3f}..{projection[1]:.3f}', flush=True)
            if args.out:
                args.out.write_text(json.dumps(data, indent=2)+'\n')
    data['process_tree_max_rss_kib'] = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss
    if args.out:
        args.out.write_text(json.dumps(data, indent=2)+'\n')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
