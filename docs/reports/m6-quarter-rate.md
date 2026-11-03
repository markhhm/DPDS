<!-- SPDX-License-Identifier: Apache-2.0 -->
# M6 quarter-rate A matrix: resource estimate

Status: **ESTIMATE**, software-only arithmetic. No hardware, traffic or capture ran.
The [generated matrix](../../examples/m6-x710.json) retains 360 conditions, 1,380 acquisitions and 1,960 directional reports.
Its [machine-readable estimate](m6-quarter-rate.json) contains exact byte counts and cost coefficients.
The separate short full-rate campaign is outside this budget.

## Scaling and evidence scope

| Parameter | A matrix rule |
| --- | --- |
| Per-line offer R, service C, byte capacity Q | Divide each by four, including both VLAN lines in paired conditions |
| Fixed L0: C / R | 2.5 / 2.25 Gbit/s, retaining the calibration ratio 0.9 |
| Fixed L8: C / R | 2 / 2.5 Gbit/s, retaining overload ratio 1.25 |
| Fixed L9: C / R | 2.25 / 2.5 Gbit/s, retaining overload ratio 10/9 |
| Q per line | 2,097,152 bytes instead of 8,388,608; the byte charge still includes FCS and excludes preamble/IFG |
| Q/C, R/C | Unchanged; queue residence in time and nominal overload ratios are preserved |
| t, delta, p, seed, trace curves and hashes | Unchanged |
| Frame lengths, equal-packet X mix, flows, VLANs, dense per-flow sequences | Unchanged |
| IDs, repeats, trace episodes, direction pairs | Unchanged; each pair of directions runs simultaneously |
| Mask, sample thresholds, PCAPNG snaplen | Still 10 (1/1024), 431 planned / 368 required, and 128 bytes |
| Fixed exposure | Recompute with the existing sample formula at the lower surviving rate |
| Trace exposure | Still 9 s plus 2 s warmup, 1 s drain and 5 s reset per episode |

L9 is the 2.5G offer / 2.25G service case. L0 and L8 retain their own ratios.
Discrete packet rounding can change exact queue occupancy and dropped identities.
The reduced matrix does not assert identical packet-level outcomes to the original full-rate matrix.
Its external before/after measurements establish quarter-rate correctness only.
B-prime and the separate short full-rate campaign supply capacity evidence with their own stated limits.

The producer defaults to divisor four for a matrix, but divisor one for existing engine-config exports.
Thus the committed M5 B-prime configurations remain full-rate and byte-identical.
Use `--rate-divisor 4` with `--condition` and `--active-vlan` for an A fixed-condition engine config.
Use `--rate-divisor 1` to obtain the original full-rate matrix basis for separate work.
Do not dispatch that full-rate basis onto the single-port A capture topology.

## Four-source mirror budget

For each condition and its active directions, the producer registers `mirror_estimate`:

```text
ingress = active_directions * sum(R per line)
egress = active_directions * sum(min(R, C) per line)
total = ingress + egress
headroom = 10,000,000,000 - total
```

These are steady L1 bit-rate estimates, including the registered preamble and IFG charge.
Using C alone for an underloaded paired line counts output that has no offered packets.
Every condition fits this steady envelope:

| Aggregate L1 Gbit/s | Conditions | Headroom Gbit/s |
| ---: | ---: | ---: |
| 4.5 | 152 | 5.5 |
| 4.75 | 88 | 5.25 |
| 9 | 76 | 1 |
| 9.5 | 44 | 0.5 |

Maximum steady load is **9.5 Gbit/s**, leaving **0.5 Gbit/s (5%)**.
Snaplen limits stored bytes, not mirror-link load.
Any mirror encapsulation needs a separate wire-byte allowance within that headroom.

Service C does not pace physical NIC TX bursts. Drain, decreasing trace delay or hold/release can exceed this steady estimate temporarily.
Mirror buffering and loss-free capture therefore require physical qualification before A can run.
The [capture procedure](../m6/quarter-rate-capture.md) also requires unambiguous source labels and a shared independent clock.

## Acquisition and judgment time

The fixed-exposure formula remains `max(30, ceil(431*1024*8*1542/(min(C,R)*p_late)))`.
For p=0 or p=1, use the main stratum instead.
Of 300 fixed conditions, 222 still observe for 30 s.
The 78 p=.01 conditions change from 61 s to **242 s**.
Keeping 61 s at the lower rate violates the original rare-late planning target.

| Inventory | Acquisitions | Acquisition time estimate |
| --- | ---: | ---: |
| Fixed main | 108 | 1 h 08 min 24 s |
| Fixed boundary | 264 | 7 h 22 min 48 s |
| Trace | 1,008 | 4 h 45 min 36 s |
| Total | 1,380 | **13 h 16 min 48 s** |
| Setup, calibration and rerun reserve | — | 4 h |
| Total with reserve | — | **17 h 16 min 48 s** |

This exceeds the old 13 h 21 min 30 s total by **3 h 55 min 18 s**.
The original 30–70 s fixed window cannot retain the existing sample rule at quarter rate.
No 1/256 sampling shortcut or weaker gate is applied.
Actual sample counts and measured drain completion remain mandatory.

The existing [judge model](../m6/judge-shards.md#assignment-and-cost-model) estimates **116.12 core-hours**, versus 329.18 for the full-rate basis.
Each recalculated shard owns 180 conditions and estimates **58.06 core-hours**, with 0% estimated imbalance.
The model includes 23.246 billion processed offers and startup for every report/profile.
The shared native acquisition is read by both directional reports, so processed work exceeds 15.095 billion unique offers.

Do not divide this cost by four again for sampling.
Trace parsing has a fixed cost, and rare-late exposures became longer.
CPU/core division assumes ideal scaling. These estimates do not qualify concurrent judge speed or storage throughput.
The acquisition time excludes offline judgment, transfer time and a full-raw backlog.

## Raw, K-1, samples and reports

TB and GB below are decimal. Raw assumes that every offer reaches both external points.
Intentional Q drops can reduce receiver bytes.
PCAPNG uses 128-byte snaplen and per-packet blocks of 160 bytes for U and mean `(92+160+160)/3` for X.
The native density scenarios are estimates, not upper bounds for every event stream.

| Native bytes / offered packet | Complete raw before K-1 | Raw plus samples/reports |
| ---: | ---: | ---: |
| 188 | 7.264 TB | 7.308 TB |
| 252 | 8.230 TB | 8.274 TB |
| 316 | 9.196 TB | 9.240 TB |

K-1 still allocates 1,304,347,826 bytes per acquisition, with a 65,536-byte metadata reserve.
The [updated per-acquisition prefix table](m6-raw-retention.md#complete-matrix-allocation) gives N from 2.541436727 to 11.243624126 s.
For 116 trace acquisitions, N reaches the full 11 s capture interval.
The deduplicated retained raw estimate is **1,796,721,738,612 bytes**, below the 1.8 TB allocation and 2 TB limit.
Actual prefix sizes remain subject to the existing destructive-stage quota check.

Expected selection is about **22.701 million** processed offers at 1/1024.
The sample JSONL projection is **44,305,918,855 bytes**.
Other report artifacts project to **138,491,296 bytes**, or **44.444 GB** combined.
Complete judgments, samples, C results, provenance and seals remain intact after K-1.
Raw prefixes plus these projected artifacts total about **1.841 TB**.

Sample/report coefficients come from two software-only million-offer runs per input class, using the larger artifact size.
The six class coefficients are recorded in the estimate JSON and `raw_budget.py`.
They reuse the same workload classes as the judge fits and the same 1/1024 mask.
Longer identities, retries, different event density and report contents can change sizes.
This projection is not a hard storage bound.

K-1 requires complete judgment, valid seals, hashes and explicit backup confirmation before any cut.
Its limit excludes full acquisitions awaiting judgment, backups, filesystem overhead and temporary staged prefixes.
An acquisition/transfer/judge backlog can exceed 2 TB even when retained prefixes fit.
Provision that backlog separately. This change neither deletes raw files nor dispatches jobs.

## Reproduce without traffic or capture reads

```sh
python3 -m tools.g2.expand_m6_x710 --out examples/m6-x710.json
python3 - <<'PY'
import json
from pathlib import Path
from tools.g2.expand_m6_x710 import build_matrix
from tools.g2.raw_budget import estimate_resources
result = estimate_resources(build_matrix())
Path('docs/reports/m6-quarter-rate.json').write_text(json.dumps(result, indent=2) + '\n')
PY
python3 -m unittest tools.g2.tests.test_m6_x710_full tools.g2.tests.test_raw_retention_budget tools.g2.tests.test_bprime_inputs
```

The tests compare every original/scaled line, Q/C, R/C, timing curves, all steady mirror totals and the regenerated estimate.
They also preserve full-rate B-prime exports and preflight all 1,960 future pair registrations with synthetic deployment values.
These checks do not certify mirror hardware, clocks, actual sample populations or loss-free capture.
