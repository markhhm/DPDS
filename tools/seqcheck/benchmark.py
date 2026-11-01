#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Generate closed snaplen captures; time the whole independent checker process."""
import argparse
import json
import os
from pathlib import Path
import struct
import subprocess
import time

from tools.seqcheck.test_seqcheck import block, frame, option


def generate(path, n, flows, caplen):
    templates = [frame(0, vlan=100 + f)[:caplen] for f in range(flows)]
    start = block(0x0a0d0d0a, struct.pack('<IHHq', 0x1a2b3c4d, 1, 0, -1))
    start += block(1, struct.pack('<HHI', 1, 0, caplen) + option(9, b'\x09') + bytes(4))
    with path.open('wb') as output:
        output.write(start)
        chunk = bytearray()
        for i in range(n):
            packet = bytearray(templates[i % flows])
            struct.pack_into('!Q', packet, 46, i // flows)
            # Include selected-late probes without weakening ordinary checks.
            packet[58] = int(i % 10 == 0)
            tick = i + 1
            body = struct.pack('<IIIII', 0, tick >> 32, tick & 0xffffffff, len(packet), 1518)
            chunk += block(6, body + packet + bytes(-len(packet) % 4))
            if len(chunk) >= 1024 * 1024:
                output.write(chunk)
                chunk.clear()
        output.write(chunk)
        end = n + 1
        opts = b''.join(option(code, value) for code, value in [
            (2, struct.pack('<II', 0, 0)),
            (3, struct.pack('<II', end >> 32, end & 0xffffffff)),
            (5, bytes(8)), (7, bytes(8)), (8, struct.pack('<Q', n))]) + bytes(4)
        output.write(block(5, struct.pack('<III', 0, end >> 32, end & 0xffffffff) + opts))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--binary', type=Path, required=True)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--packets', type=int, default=2_000_000)
    parser.add_argument('--flows', type=int, default=4)
    parser.add_argument('--caplen', type=int, choices=(59, 128), default=59)
    parser.add_argument('--repeats', type=int, default=3)
    args = parser.parse_args()
    if args.packets <= 0 or not 1 <= args.flows <= 1000 or args.repeats <= 0:
        parser.error('positive packet/repeat counts and 1..1000 flows required')
    args.out.mkdir(parents=True, exist_ok=False)
    ingress, receiver = args.out / 'ingress.pcapng', args.out / 'receiver.pcapng'
    generate(ingress, args.packets, args.flows, args.caplen)
    # Byte-identical immutable files exercise both complete scans; no packet data is cached by the checker.
    os.link(ingress, receiver)
    runs = []
    for repeat in range(args.repeats):
        report_path = args.out / f'report-{repeat}.json'
        error_path = args.out / f'stderr-{repeat}.txt'
        start = time.perf_counter()
        with report_path.open('w') as output, error_path.open('w') as errors:
            process = subprocess.Popen([str(args.binary.resolve()), '--ingress', str(ingress),
                                        '--receiver', str(receiver)], stdout=output, stderr=errors)
            _, status, usage = os.wait4(process.pid, 0)
            process.returncode = os.waitstatus_to_exitcode(status)
        elapsed = time.perf_counter() - start
        report = json.loads(report_path.read_text())
        assert process.returncode == 0 and report['status'] == 'PASS' and report['complete'], error_path.read_text()
        assert report['totals']['ingress'] == args.packets == report['totals']['receiver']
        assert all(report['totals'][k] == 0 for k in report['totals'] if k not in ('ingress', 'receiver'))
        assert report['inputs']['ingress']['sha256'] == report['inputs']['receiver']['sha256']
        assert all(report['inputs'][k]['health'] == dict(complete=True, capture_drops=0)
                   for k in ('ingress', 'receiver'))
        runs.append(dict(wall_seconds=elapsed,
                         cpu_seconds=usage.ru_utime + usage.ru_stime,
                         max_rss_kib=usage.ru_maxrss,
                         observed_packets_per_second=2 * args.packets / elapsed,
                         ingress_pairs_per_second=args.packets / elapsed))
    summary = dict(packets_per_capture=args.packets, flows=args.flows, caplen=args.caplen,
                   input_bytes_each=ingress.stat().st_size, warm_page_cache=True,
                   serial=True, runs=runs,
                   scope='synthetic pcapng; process startup, both scans, SHA256, health, comparison, JSON and cleanup; no engine',
                   min_observed_mpps=min(r['observed_packets_per_second'] for r in runs) / 1e6,
                   min_pair_mpps=min(r['ingress_pairs_per_second'] for r in runs) / 1e6)
    (args.out / 'benchmark.json').write_text(json.dumps(summary, indent=2) + '\n')
    print(json.dumps(summary, indent=2))


if __name__ == '__main__':
    main()
