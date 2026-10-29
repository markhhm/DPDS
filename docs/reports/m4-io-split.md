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
python3 perf/ring_rate.py [--split|--io-split] build/hng-ring-rate -- -b 1518 -d 3 -w 0.5
```

Three rounds, each running all six cases back to back, so every layout saw the
same machine state. One-minute load average 3.1-5.3 during the runs.

| Layout | mbuf bytes | total Mpps, 3 runs | median | per direction (median) |
|---|---:|---|---:|---:|
| 4 lcores, one scheduler | 1518 | 12.06 / 11.17 / 11.89 | 11.89 | 5.95 |
| 4 lcores, one scheduler | 60 | 11.83 / 11.19 / 11.22 | 11.22 | 5.61 |
| 5 lcores, scheduler per ingress port | 1518 | 11.14 / 10.68 / 11.40 | 11.14 | 5.57 |
| 5 lcores, scheduler per ingress port | 60 | 11.35 / 10.80 / 10.78 | 10.80 | 5.40 |
| 7 lcores, RX/scheduler/TX per direction | 1518 | 17.96 / 16.10 / 17.02 | 17.02 | 8.51 |
| 7 lcores, RX/scheduler/TX per direction | 60 | 17.44 / 17.40 / 18.36 | 17.44 | 8.72 |

Every run was conserved and drained (`pending_peak` 8-9). The 7-lcore layout
is about 1.5x the 5-lcore layout and above the 16.25 Mpps total that two
directions at 8.127 Mpps need (*estimate*, 100G at 1538-byte untagged frames)
in 5 of 6 runs; the lowest run (16.10 M) is just below it. The closed-loop
generator is one main lcore serving both ports with at most 1023 frames in
flight, so it may now limit the measurement; the rate is not a bound on the
host.

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
