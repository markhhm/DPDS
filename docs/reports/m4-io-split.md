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
Not tried: sending several frames per TX burst across lines (would need the
merge to emit a mixed burst), and a scheduler burst dequeue.

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
