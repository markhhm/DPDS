<!-- SPDX-License-Identifier: Apache-2.0 -->
# Offline sequence-check throughput

Measured 2026-11-01 on shared Linux/WSL2, AMD Ryzen 7 5800X (8 physical
cores/16 threads), GCC 14.2.0, Meson release `-O3`, OpenSSL libcrypto 3.5.6.
One checker at a time, `prlimit --as=4294967296`, one comparison thread and
two SHA-256 reader threads. No engine, DPDK, NIC or traffic was executed.

## Acceptance unit and target

Acceptance uses **complete ingress/receiver identity pairs per second, N/t**.
An input identity counts once; the two capture EPBs are not two pairs.
For bidirectional 10 Gbit/s with 1518-byte captured frames and 1542-byte
L1 occupancy (including preamble, FCS and inter-frame gap):

`source_pair_rate = 2 * 10^10 / (1542 * 8) = 1.62127 million pairs/capture-second`.

Three times headroom is `4.86381 million pairs/s`, rounded upward to the
adopted **4.9 M pair/s median offline-throughput threshold**. Smaller frames
have higher pair rates and need a separate budget. This is a sustained offline
capacity check, not a worst-case per-run wall-time or physical receiver SLO.

## Complete 5-million-packet-per-side runs

Each pair has **5,000,000 ingress + 5,000,000 receiver EPBs**, four VLAN
flows, 10% selected-late, and zero injected loss/reorder. The two input files
are immutable byte-identical hardlinks. Original frame length is 1518 bytes;
retained prefixes are 59 or 128 bytes. Page cache is warm. Every result is
PASS, complete=true, zero anomalies, and healthy final interface statistics.
Complete input digests were independently checked with Python hashlib.

Timing includes process startup, both comparison scans, both complete SHA-256
scans, capture health, full loss enumeration, JSON output and cleanup.
Input generation is excluded. Hashing rereads each file in its worker.
Rows retain all five measured runs; no slow final run is excluded.

| Affinity, logical CPUs | caplen | Median wall s | Wall range s | Median EPB Mpps (2N/t) | Median pair Mpps (N/t) | Pair Mpps range | Peak RSS MiB | >=4.9 M pair/s median |
| --- | ---: | ---: | --- | ---: | ---: | --- | ---: | --- |
| 0,1,2 | 59 | 0.612164 | 0.544802–0.691843 | 16.335 | 8.168 | 7.227–9.178 | 49.16 | PASS |
| 0,1,2 | 128 | 0.947591 | 0.706324–1.068660 | 10.553 | 5.277 | 4.679–7.079 | 49.13 | PASS |
| 0,2,4 | 59 | 0.834545 | 0.722300–1.006422 | 11.983 | 5.991 | 4.968–6.922 | 49.12 | PASS |
| 0,2,4 | 128 | 1.015097 | 0.951566–1.161676 | 9.851 | 4.926 | 4.304–5.254 | 49.16 | PASS |

The primary 128-byte series (CPUs 0,1,2) measures **5.277 M complete pairs/s**
(**10.553 M EPBs/s**), above 4.9. The additional distinct-core series is
4.926 M pair/s, also above 4.9 at the median, with limited headroom. Individual
runs can be slower; shared scheduling and page-cache effects are visible.
CPU 0/1 share a physical core; CPU 2 is a second core. CPUs 0,2,4 use three
distinct physical cores. These are not single-core results.

[benchmark-results.json](benchmark-results.json) includes every final run,
source/binary/input digests, CPU seconds and per-child RSS. Total CPU time
includes all hashing threads; maximum concurrency is three logical CPUs.
Each 59-byte file is 460,000,148 bytes; each 128-byte file is 800,000,148 bytes.
Earlier development measurements, including a 1.998-second unrestricted
59-byte run after generation, remain retained outside the repository.

Reproduce from the repository root (Python and the existing probe codec are
used only for synthetic generation):

```sh
cc -std=c11 -O3 -pthread tools/seqcheck/seqcheck.c -lcrypto -o seqcheck
taskset -c 0,1,2 prlimit --as=4294967296 -- python3 -m tools.seqcheck.benchmark \
  --binary ./seqcheck --out bench-59 --packets 5000000 --caplen 59 --repeats 5
taskset -c 0,1,2 prlimit --as=4294967296 -- python3 -m tools.seqcheck.benchmark \
  --binary ./seqcheck --out bench-128 --packets 5000000 --caplen 128 --repeats 5
```

Choose allowed logical CPUs and record their physical-core mapping. A newly
generated file can include writeback/cache/scheduling effects. This fixture
uses a few dense sequential flows. Sparse uint64 identities, many flows,
loss-heavy diagnostics, cold storage and concurrent whole-matrix processing
need separate capacity budgets. No physical M6 acceptance claim follows.
