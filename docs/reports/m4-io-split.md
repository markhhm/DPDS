<!-- SPDX-License-Identifier: Apache-2.0 -->

# M4 item 8: one RX and one TX lcore per direction

Status: software-only results on one development machine, 2026-10-30. This
adds to the M4 performance report in the core repository
(`docs/reports/m4-perf.md` on the core's `hng/core` branch); no core change
was needed.

**All numbers are reference values from a shared WSL2 virtual machine
(AMD Ryzen 7 5800X, 16 logical CPUs, 4 KiB pages, `--no-huge`, other load
present, no CPU isolation). They are not evidence of 100 Gbit/s performance.**

## Layout

The 7-lcore layout runs, for each direction k (ingress port `host.ports[k]`):

```
RX k (polls only port k) -> owner ring k -> scheduler k -> TX ring k -> TX k (egress ports of port k's lines)
```

| Shared object | Producers | Consumers | Flags |
|---|---|---|---|
| owner ring k | RX k | scheduler k | `RING_F_SP_ENQ \| RING_F_SC_DEQ` |
| TX ring k | scheduler k | TX k | `RING_F_SP_ENQ \| RING_F_SC_DEQ` |
| ingress frontier k | RX k | scheduler k | seqlock of atomics, one cache line per port |
| `rx_owners[k]`, `owners[k]`, `tx_owners[k]` counters | that lcore only | main lcore after join | 64-byte aligned, whole lines |
| NIC RX queue of port k | RX k only | | one queue per port |
| NIC TX queue of each egress port fed by port k | TX k only | | one queue per port |

Startup requires the 5-lcore rule (each egress port fed by one ingress port).
That alone gives each egress queue exactly one TX lcore: an egress port is fed
by one scheduler, whose output goes to one TX ring. An earlier check that also
required each ingress port to feed a single egress port was removed; the case
it rejected (one ingress port feeding both egress ports) is safe, with one TX
lcore serving both egress ports and the other idle. TX policies are per line, and each line is drained by one TX lcore.

Shutdown: SIGINT or an invalidation stops both RX lcores. The last RX lcore to
finish (`receivers_running`) sets the worker quit signal, so no scheduler
ends its input while the other direction's RX can still enqueue. The last
scheduler (`schedulers_running`) sets the TX quit signal; each TX lcore then
drains its ring and pending table and exits. The main lcore merges the
per-direction counters before the conservation check and the NIC-loss check
(`imissed`/`rx_nombuf`), so both still apply to the whole run. RX ring-full
skips, RX handoff drops, `TX_PENDING_PER_LINE = 1024` and the TX invalidation
paths are the same code per direction.

## Equivalence

- `tests/split.c` runs the same input and clock schedule through three
  layouts: one owner of all lines with one TX state; one owner per ingress
  port sharing one TX state; one owner and one TX state per direction (TX
  counters merged with `tx_stats_merge`). Departure is the tick of the TX
  burst. Per egress, records (sequence, VLAN, length, release, deadline,
  departure) are identical: 2938 and 2890 packets, 612 Q drops, 1824 late
  selections; digests `5b82251578ffd8a5` and `df43f1c0f8df37c0` match the
  pre-M4 golden `tests/split-golden.csv`. Line counters, worker counters, TX
  totals and per-line TX counts match. TX burst histograms and `pending_peak`
  depend on how the scheduler batches its output, so the 7-lcore layout matches
  the 5-lcore layout on every TX field, and the single owner on all but these.
- `split` also has a second pass with a fake TX that refuses the first offer
  of every fifth sequence number. TX retries at the same tick, so records stay
  equal to the golden output; retries, port blocking and arbitration after a
  partial send run in all three layouts, and the 7-lcore layout matches the
  5-lcore layout on every TX counter (including `late_submit_max_ns`).
- `io-ring-io-split-lcores`: the production `lcore_receive` (fixed port) and
  `lcore_transmit` per direction, with a fixture-clock scheduler owner between
  them, 32 seeds x 1 epoch (65,536 inputs). Shutdown goes through the real
  chain: `quit_signal_receive`, `receivers_running`, the schedulers, then
  `quit_signal_transmit`. Every frame is checked against the oracle.
- `rx_split`: a per-direction RX state polls only its port, a full owner ring
  skips only that port, and the merged counters add up.
- `io-ring-io-split` and `io-ring-stepped-io-split`: the PMD integrity oracle
  with one RX, scheduler and TX owner per direction, 32 seeds x 153 epochs
  (10,027,008 inputs, 9,321,984 outputs, 705,024 Q drops, the same as
  `io-ring-split`). Each direction's RX and TX owners must see only their own
  port's packets.
- `null-layouts` adds 7 lcores, including SIGINT 1-100 ms after the first EAL
  line.

## net_ring rate

```sh
python3 perf/ring_rate.py [--split|--io-split] [--cpus 0,2,4,6,8,10,12,14] \
  build/hng-ring-rate -- -b 1518 -d 3 -w 0.5 -n N
```

The first measurement of this layout capped the generator population at 1023
frames for all layouts. With one TX ring per direction that cap limited the
rate: it still rose with the population. The generator now gives each
direction its own pool and recycles a frame into the ingress port it came
from, so each direction holds at most 1023 frames and the 7-lcore layout can
use 2046 (`-n` default). Rates from that first round are superseded.

Sweep, 1518-byte mbufs: three rounds, each running every case below back to
back, so all layouts saw the same machine state. One-minute load average
1.5-6.9 during the sweep. "SMT" maps lcore i to CPU i (8 lcores on 4 physical
cores, two per core); "physical" pins one lcore per physical core
(`--cpus 0,2,...,14`). Total Mpps, median of 3 (range):

| Layout | population | SMT | physical |
|---|---:|---|---|
| 4 lcores | 256 | 11.42 (10.54-11.59) | 11.27 (11.23-11.50) |
| 4 lcores | 512 | 11.86 (11.67-11.95) | 11.91 (11.75-11.91) |
| 4 lcores | 1023 | 11.76 (11.58-11.86) | 11.89 (11.83-12.04) |
| 5 lcores | 256 | 10.42 (10.11-10.82) | 10.46 (10.02-10.86) |
| 5 lcores | 512 | 11.27 (10.50-11.37) | 11.06 (10.98-11.13) |
| 5 lcores | 1023 | 11.32 (11.11-11.51) | 11.41 (11.20-11.55) |
| 7 lcores | 256 | 17.41 (17.28-18.82) | 17.22 (16.84-20.11) |
| 7 lcores | 512 | 19.33 (18.94-20.43) | 19.40 (18.71-19.63) |
| 7 lcores | 1023 | 20.33 (20.30-20.51) | 20.35 (20.33-20.91) |
| 7 lcores | 2046 | 20.62 (19.75-20.92) | 20.31 (20.27-20.50) |

Every run was conserved and drained. **Measured: the 7-lcore layout reaches
about 20.3 Mpps total (10.2 Mpps per direction) on this machine and levels
off from a population of 1023.** The 4- and 5-lcore layouts level off at about
11-12 Mpps from 512. Placement on separate physical cores does not change
the plateau.

What limits it (diagnostic counters, not committed; 7 lcores, physical
placement, load average about 4-5):

| population | total Mpps | mean frames in sink rings | source rings | owner rings | TX rings |
|---:|---:|---:|---:|---:|---:|
| 512 | 20.4 | 1.7 | 6.1 | 91 | 335 |
| 1023 | 20.0 | 5.0 | 24.0 | 318 | 534 |
| 2046 | 20.5 | 5.7 | 26.6 | 667 | 1199 |

The generator is not the limit: the sink rings it drains are almost empty
and RX bursts are mostly 1-3 frames. Added frames queue in the owner rings
and, most, in the TX rings, so the scheduler and TX lcores of each direction
are both at capacity, about 10.2 Mpps each (about 98 ns per packet). Every TX
burst carried one frame: with two lines per egress whose heads alternate in
release order, the release-order merge cuts each burst at the other line's
head, so TX pays one `rte_eth_tx_burst` call and one arbitration pass per
packet. The 5-lcore layout shows the same pattern for its single TX lcore
(about 900 frames in the shared TX ring at 1023). On this machine 10.2 Mpps
per direction is above the 8.127 Mpps that 100G needs at 1538-byte frames
(*estimate*), but these are WSL2 numbers under load, not a NIC measurement.
This limit is superseded by the merged TX burst below.

## Merged TX burst across lines

TX used to end a burst at the first frame of another line that comes earlier
in release order. With two interleaved lines per egress every call carried
one frame. TX now merges the pending frames of all eligible lines of one
egress port in global release order (release, then ingress sequence) into one
burst of up to 16 and calls `rte_eth_tx_burst` once. On a partial accept the
accepted prefix of the merge is a prefix of each line, so each line removes
only its accepted frames; the refused ones stay at its head in FIFO order and
are marked for retry. `tests/tx_burst.c` checks a 16-frame two-line burst and
every partial-accept point (0-15); the split golden and all oracle runs still
pass.

The rate fixture's Q was 64 KiB (43 MTU frames per line). With the merged
burst and 2046 frames it dropped 40-83 frames per run, so Q is now 1.5 MiB
(1033 MTU frames, more than one direction's 1023): the fixture cannot drop
in Q, and `ring_rate.py` fails on a Q drop. All runs below had q_drop=0.

Before (c84b2fe plus the histogram print) and after, same rounds: 1518-byte
mbufs, one lcore per physical core, three rounds each running all eight
cases back to back, one-minute load average 3.4-6.4. Total Mpps, median
(range); TX burst histogram of one run, calls with 1, 2-3, 4-7, 8-15 and 16
frames:

| Layout, population | before | after | TX bursts before | TX bursts after |
|---|---|---|---|---|
| 4 lcores, 1023 | 11.67 (11.52-11.92) | 14.51 (14.23-14.56) | 40,803,354 / 12,713 / 0 / 0 / 0 | 1,822 / 4,927 / 158,555 / 6,154,952 / 5,000 |
| 5 lcores, 1023 | 11.36 (11.23-11.37) | 17.43 (17.43-17.84) | 39,782,122 / 2,331 / 0 / 0 / 0 | 259,906 / 521,095 / 961,378 / 1,986,067 / 1,956,816 |
| 7 lcores, 1023 | 20.27 (20.22-20.72) | 25.87 (25.51-27.02) | 70,433,974 / 0 / 0 / 0 / 0 | 124 / 127 / 204 / 1,179 / 5,627,303 |
| 7 lcores, 2046 | 20.36 (19.85-20.45) | 25.68 (24.76-26.85) | 69,510,207 / 36,472 / 0 / 0 / 0 | 34 / 22 / 48 / 408 / 5,860,687 |

Mean frames per TX call: 1.00 before in every layout; after 8.0 (4 lcores),
10.8 (5 lcores) and 16.0 (7 lcores). In the 4-lcore layout one scheduler
alternates egress ports, so each TX call sees about 8 frames per port.

**Measured: the 7-lcore layout reaches about 25.9 Mpps total (12.9 Mpps per
direction) on this machine, the same at 1023 and 2046 frames.** The limit
is now the scheduler (diagnostic counters, not committed; load average about
5): RX bursts are almost all 16 frames, the frames queue in the owner rings
(mean 699 at 1023, 1646 at 2046 over both directions) and the TX rings hold
175-244. Each scheduler owner runs at about 77 ns per packet. With 2046 frames
the owner rings are close to full, so more population adds no rate.
Not tried: a scheduler burst dequeue. These are WSL2 numbers under load, not
a NIC measurement; the 8.127 Mpps per direction for 100G at 1538-byte frames
is an *estimate*.

## Pool calculator

`hng-pool-calc` defaults to 7 lcores (accepts 1-7, like `host_pool_objects`).
A port's mbufs still sit in one RX and one TX handoff ring in every layout, so
the only change is one more mempool cache per extra lcore: 64 objects per pool
over 5 lcores. Roadmap case per NUMA node: 4,164,516 objects, 10.335 GiB
(20.670 GiB for both nodes; unchanged at three decimals). Verify (4 KiB pages,
7 lcores): 18,772 objects calculated and in the mempool on both ports, total
error 0.03% (gate 5%: PASS).

## Not tested

NICs, hugepages, NUMA, 100G, an isolated fixed-frequency core, and the 7-lcore
layout with an open-loop generator.

## 2 MiB hugepage allocation and virtual I/O checks (2026-11-01)

This section adds a small real-allocation check and software PMD integrity
checks with hugepages. It supersedes the earlier "hugepages not tested"
limitation only for these cases. The host source was `749d554`, with core
`2afe493` (merged M4 scheduler and M2 observation support). The environment
was WSL2, kernel `6.18.33.2-microsoft-standard-WSL2`, on the shared development
machine.

The system had 1,024 reserved 2 MiB pages and a hugetlbfs mount at
`/dev/hugepages`. No reservation or mount setting changed during these checks.
These results do not establish physical NIC accuracy, NUMA behavior or 100G.

### Pool calculator: actual allocation

Reproduce with an available CPU and a unique prefix:

```sh
build/hng-pool-calc -l "$CPU" --no-pci --no-telemetry -m 512 \
  --in-memory --huge-dir="$HUGE_DIR" --file-prefix="$RUN_PREFIX" \
  --vdev=net_null0 --vdev=net_null1 -- \
  --c-bps 1000000000 --t-ms 20 --delta-ms 40 --q-ms 5 --burst 256 \
  --lines-per-port 2 --page-bytes 4096 --verify
```

There is no `--no-huge` argument. The planning input of 4,096 bytes
deliberately exercises the verifier's actual-page detection. It reported
`verify page_bytes=2097152 (from the EAL memory segment list)` and recomputed
the plan for that page size before comparison. Core metadata was 144 bytes
per slot.

The plan reserves seven lcores' pool caches. The allocation verifier
itself uses one EAL lcore. Each port had 18,772 objects, all populated, with
calculated and actual stride both 2,496 bytes.

| Allocation | Calculated bytes | Actual bytes | Relative error |
|---|---:|---:|---:|
| Port 0 mempool | 49,553,984 | 48,189,184 | 2.83% |
| Port 1 mempool | 49,553,984 | 48,189,184 | 2.83% |
| Core metadata | 3,185,280 | 3,185,280 reserved; 3,189,872 process-heap growth | 0.14% against heap growth |
| Both pools plus core | 102,293,248 | 99,568,240 | **2.74%: PASS**, limit 5% |

The error uses `abs(calculated - actual) / actual`, as in
`perf/pool_calc.c`. It compares allocated pool/core bytes, not the whole
512 MiB EAL reservation. This verifies the small 2 MiB allocation case.
Production-sized long-delay pools and 1 GiB pages remain unverified.

For each pool, the page calculation is explicit:

| Planning component | Bytes |
|---|---:|
| 18,772 objects times 2,496-byte stride | 46,854,912 |
| 23 pages times 2,097,152 bytes, at 840 objects per page | 48,234,496 |
| Page rounding above the object bytes | 1,379,584 |
| Additional ring and mempool-header estimate | 1,319,488 |
| Planned pool total | 49,553,984 |

The measured pool heap growth exceeds the object bytes by 1,334,272 B.
This is the combined overhead reported by the DPDK heap counter for the
pool, ring and allocation padding. The verifier does not isolate memzone
costs or charge every reserved EAL page to this pool. Thus the conservative page
rounding and header estimate is not a measurement of memzone overhead alone.
The planned total exceeds the measured total by 1,364,800 B per pool.

### Null and ring PMDs with hugepages

The existing `tests/null_smoke.py` and `tests/io_pmd.py` assertions ran
unchanged. A local invocation adapter replaced only the EAL argument
`--no-huge` with `--huge-dir=/dev/hugepages` and retained `--in-memory`.
Null runs retained their 256 MiB EAL budget. Ring runs used an explicit
512 MiB budget. CPU mapping still came from `tests/eal_args.py`.

The adapter recorded every actual command and checked these argument
conditions. No repository test or production source changed for this run.
One-minute load samples before/after the null/ring invocations ranged
from 6.42 to 7.39. These are integrity checks, not performance comparisons.

| Existing test mode | Lcores | Result |
|---|---:|---|
| `null_smoke.py`: RX enabled/disabled and startup/shutdown signals | 2, 3, 4, 5, 7 | All 22 cases passed; conservation, byte conservation, DRAINED and zero owned mbufs |
| `ring` | 4 | PASS |
| `ring-stepped` | 4 | PASS |
| `ring-split` | 5 | PASS |
| `ring-stepped-split` | 5 | PASS |
| `ring-io-split` | 7 | PASS |
| `ring-stepped-io-split` | 7 | PASS |
| `ring-io-split-lcores` | 7 | PASS, including production RX/TX functions and the shutdown release chain |

Each of the first six ring modes completed 32 seeds and 153 epochs per seed:
10,027,008 inputs, 9,321,984 outputs and 705,024 intentional Q drops.
Every seed reported full identity/bytes/order agreement, conservation and
complete drain.

The production-lcore mode completed 32 seeds with 65,536 inputs, 60,928
outputs and 4,608 intentional Q drops. Its oracle and release-chain checks
also passed. Null PMDs establish lifecycle behavior. Their synthetic
zero-content RX is not valid-traffic evidence.
The ring oracle provides the valid-frame integrity evidence here.

The allocation and I/O checks above did not include a performance comparison.
The large memory-envelope figures remain estimates. These checks do not
qualify hardware G2 or sustained packet rate.

### Scheduler-owner A/B: 4 KiB versus 2 MiB pages

A later comparison on 2026-11-01 used host `40f42c8` and core `2afe493`.
The preflight check confirmed 1,024 reserved 2 MiB pages, 1,005 free pages,
five reserved-but-unallocated pages and the 2 MiB hugetlbfs mount.
Each hugepage invocation checked that sufficient unreserved free pages
remained for its EAL budget. All 60 invocations completed successfully.

The same executable, core library and fixture snapshots served both variants.
Main and owner lcores used CPUs 2/6/10. Both variants retained `--no-pci`,
`--no-telemetry` and `--in-memory`. Variant A used `--no-huge`. Variant B
instead used `--huge-dir=/dev/hugepages`.

There was no THP hint through
`HNG_BENCH_THP`. This changes the EAL/mempool page allocation. The Rust
FIFOs and ledger still use the ordinary allocator.

Run from the build directory with `--lcores=0@2,1@6,2@10` and a unique
file prefix. The small fixture uses a 512 MiB EAL budget and application
arguments `-r 1 -l split`. The production-sized fixture uses 1,800 MiB and:

```text
-c sched_bench_prod.json -g 493440 -n 3000000 -w 470000000 -m 640000 -r 1 -l split
```

Each of 15 rounds ran both variants back to back for both fixtures.
Order alternated A/B and B/A between rounds. Both owners ran concurrently.
One-minute load samples after each invocation ranged from **7.44 to 11.16**.

All pairs and outliers remain in the results. Each invocation retained the
benchmark's rejection of model/resource drops, invalid state and unreclaimed
mbufs. An independent check confirmed that each pair's command differed
only in its page arguments.

Values are ns/packet. The paired difference is **2 MiB minus 4 KiB**:

| Fixture | Owner | 4 KiB p50 median | 2 MiB p50 median | Paired difference median (range) | 2 MiB faster |
|---|---|---:|---:|---:|---:|
| small | port 0 | 67.06 | 59.79 | -5.24 (-32.05..+28.57) | 11/15 |
| small | port 1 | 82.50 | 57.44 | -8.86 (-31.84..+32.31) | 11/15 |
| production-sized | port 0 | 85.71 | 79.36 | +0.51 (-31.80..+24.34) | 7/15 |
| production-sized | port 1 | 73.75 | 67.43 | +2.22 (-31.23..+25.15) | 6/15 |

**Reference result:** the production-sized paired results do not show a
consistent speedup. The small fixture favors hugepages in this set, but
both fixtures have wide paired ranges on the shared machine. In particular,
subtracting the two unpaired production medians suggests a gain that
the paired medians do not support. These loaded absolute values do not
supersede earlier quieter scheduler-gate measurements. No physical NIC,
100G or general performance claim follows from this comparison.
