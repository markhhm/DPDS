<!-- SPDX-License-Identifier: Apache-2.0 -->
# Registered M7 hybrid storage

This is an explicit per-line option for fixed-delay M7 inputs. Leave
`m7_storage` absent to use the original mbuf storage, conservative all-small
startup envelope, one-second handoff gate, counters and observer output.
Existing B-prime and M6 configurations need no changes. An explicit null is
invalid. Trace lines cannot use this option.

```json
"m7_storage": {
  "mode": "m7-hybrid-v1",
  "threshold_l2": 1024,
  "hold_ns": 10000000,
  "window_ns": 10000000,
  "offered_bps": 100000000000,
  "margin_numerator": 5,
  "margin_denominator": 4,
  "composition": [
    {"l2_bytes": 64, "weight": 1},
    {"l2_bytes": 512, "weight": 1},
    {"l2_bytes": 1522, "weight": 1}
  ]
}
```

The example shows every default. Composition is required: 1–16 distinct
charged-L2 lengths (including FCS), each 64–1522 bytes, with positive integer
packet weights whose sum is at most 65535. Unknown fields, duplicate lengths,
invalid ranges and arithmetic overflow fail startup. Every class must fit the
line's existing frame envelope. `offered_bps` must be at least C. W=10 ms and
margin=5/4 identify this registered policy; other values are rejected. T may
be 64–1522 and H must be positive. Neither is a measured hardware limit.
These settings never change C, Q, t, delta, p, seed or traffic generation.

## Ownership and failure

The scheduler pays copy-in and materialize costs. RX and TX retain their
original ownership. Only packets accepted by the unchanged model enter storage;
Q/loss decisions do not allocate provisional records. For charged L <= T,
accepted admission copies stored bytes and the complete `packet_meta`, RSS hash,
packet type, VLAN tags and applicable mbuf flags into a compact record, then
releases the source mbuf reference. Larger packets retain the original mbuf.
A 16-byte stable tag is the model's payload pointer in either case.
Compact data uses the ingress pool's NUMA socket and the DPDK heap; tags and
Core metadata use ordinary process memory. Arenas are prefaulted before launch.

The existing TX-ring free-slot budget limits output. The scheduler selects the
eligible merged model head once, materializes it, then consumes its one-use
Core ticket and recycles its tag without scanning the group again. A failed materialize leaves both the record and model head
owned until bounded abort cleanup. Record/cell storage returns only after
successful copy-out; the original deadline, release time and physical ordering
remain unchanged. NIC refused suffixes remain in the existing TX retry path.
END, terminal failure and forced abort discard compact records directly; they
do not need a free output mbuf. Every port closes before any peer ingress pool
is reclaimed, as in the existing production cleanup.

A continuously full scheduler-to-TX ring for **strictly more than H** causes
`INVALID RUN: TX handoff hold exceeded`, including when no model head is
currently eligible. Ring capacity remains 1024 (1023 usable). No deadline reset,
unbounded retry or hidden delay extension is permitted. The existing shutdown,
TX retry, NIC loss, storage and terminal gates remain in force. Non-M7 handoff
timeout remains one second. Mixed owners retain the legacy terminal gate too.

Resource exhaustion is a non-model failure: the first cause invalidates the
run and stops RX. It cannot become a Q drop or a passing zero-loss result.
Separate reasons identify compact record, compact cell, retained mbuf, tag and
materialization pool failures. Already accepted model payloads are then aborted
and reclaimed; prior outputs retain their existing TX custody.

## Exact capacity policy

For registered classes i, let L_i include FCS, w_i=L_i+20, a_i be the packet
weight, tau=t+delta+H, and B=C*tau/(8*10^9) wire bytes. Q remains L2 bytes.
For each resource cost r_i, define:

```text
Ar = B * sum(a_i*r_i)/sum(a_i*w_i) + Q * sum(a_i*r_i)/sum(a_i*L_i)
Pr = ceil(5/4 * Ar + burst_bound_packets * max(r_i))
E  = offered_bps * W / (8*10^9)
bD = min(B,E); bQ = min(Q,E)
Wr = ceil((B-bD)*sum(a_i*r_i)/sum(a_i*w_i) + bD*max(r_i/w_i)
        + (Q-bQ)*sum(a_i*r_i)/sum(a_i*L_i) + bQ*max(r_i/L_i)
        + burst_bound_packets*max(r_i))
live_limit_r = min(Pr,Wr)
```

| Resource | r_i |
| --- | --- |
| Core slots / tags | 1 |
| Compact records | 1 when L_i<=T, else 0 |
| Compact cells | ceil((L_i-4)/64) when L_i<=T, else 0 |
| Retained mbufs | 1 when L_i>T, else 0 |

Calculations use checked exact fractions and one final ceiling, not float
rounding. Physical reserves use the **1.25x version**, keeping the selected
28 GiB target. The W formula adds logical occupancy limits; it does not increase
physical reserves to the union of both plans. This intersection is the
implementation interpretation of the priority on the 28 GiB version.
The W bound represents an arbitrary-composition allowance in each residence
and Q domain, with registered mix elsewhere. It is not a claim that a global
mix average or a recurring independent arbitrary 10 ms window proves safety.
The implementation enforces the resulting live resource caps and class list;
it does not estimate composition by sampling. Exceeding any cap is INVALID.

The new Core reservation is P_slots (144 bytes/slot); the JSON declares at
least that many slots and slots*(max_L-4) payload bytes. The physical compact
arena is 192 + 192*P_records + 64*P_cells + align64(4*P_cells) bytes.
Each tag costs 16 bytes. Each normal pool reserves P_normal plus the existing
transport reserve, adjusted RX/TX descriptor counts, caches and pending rings.
The measured DPDK mbuf stride in the calculation build is 2624 bytes.
An all-large registration allocates only a tiny unused compact controller;
its 512 bytes/line are covered by EAL margin and its logical compact caps are zero.
See [M7 inputs](m7/inputs.md) for the page-rounded node ledger.

## Diagnostics and evidence

For registered line index L, final-stats and `/dpds/diagnostics` add the same
flat `m7_lineL_FIELD` unsigned fields. They are absent on classic lines.
Owner-local counters are copied into atomics once per scheduler step;
telemetry never reads the mutable allocator. Values are independent atomic
snapshots, not a transaction across fields. Final values follow owner joins.

| FIELD | Meaning |
| --- | --- |
| items_live, items_peak | Live tags / maximum |
| records_live, records_peak | Live compact descriptors / maximum |
| cells_live, cells_peak | Live 64-byte payload cells / maximum |
| normal_live, normal_peak | Retained original mbufs / maximum |
| copied_in, materialized, compact_discarded | Successful compact admission, copy-out, abort counts |
| normal_retained, normal_returned, normal_discarded | Original-mbuf admission, output, abort counts |
| item_exhaustions, record_exhaustions, cell_exhaustions, normal_exhaustions | Backend quota failures by resource |
| materialize_exhaustions, core_exhaustions, invalid_inputs | Failed output allocations / Core slot or service capacity failures / invalid backend custody operations |
| slot_reserve, record_reserve, cell_reserve, normal_reserve | Physical quotas |
| slot_limit, record_limit, cell_limit, normal_limit | Enforced logical occupancy caps |
| hold_ns | Registered full-handoff limit |
| perf_population_bound | Zero in production; nonzero only in the software population fixture |

Core slot/service-ledger shortages retain the existing Core storage-drop and
worker storage-drop counters and add `core_exhaustions`; they occur before the
accepted-storage callback and do not increment a backend tag failure counter. Backend failures also retain
that non-model storage-drop accounting. Materialize failure increments its
separate counter and reclaims queued packets as shutdown drops. All live counts
must return to zero. A residual tag/record/cell invalidates native completion.
The native binary records, JSONL fields and ten-element observer resource record
are unchanged. The independent Python reference validates registration and
class membership while applying the original exact model decisions.

## Software rate comparison

The `perf/ring_rate.py --io-split` harness supports `-b X` (equal fresh-packet
counts of stored 60/508/1518 bytes) and the existing fixed stored-byte arguments
64/512/1518. Closed-loop recycling retains the chosen frame length. M7 needs
logical population credit: a copied source mbuf becoming free does **not** allow
an additional packet. Model drops and retired sink packets release credit.

The perf target alone wraps the loader with an explicit live population bound,
clips external backing quotas to that bound, and prints `m7_population_bound`.
The production loader cannot use this shortcut. Both paths retain the original
four-line fixture's C=400G, Q=1572864, t=2000 ns, delta=0, p=0, S=32768 and burst=64.
The benchmark's registered offered rate is also 400G. These are software fixture
parameters, not the 100G M7 registration or a memory-capacity qualification.

One workstation software comparison used seven distinct allowed CPUs, population
ceiling 2046, 0.2 s warmup and 0.8 s measurement, three serial trials per case/mode.
Every trial drained, conserved packets and reclaimed both pools with zero Q
or other drops. Median per-direction mean throughput:

| Stored bytes | Classic Mpps | M7 Mpps | Change |
| --- | ---: | ---: | ---: |
| X (60/508/1518) | 11.055 | 4.636 | -58.1% |
| 64 | 11.910 | 4.638 | -61.1% |
| 512 | 11.458 | 3.797 | -66.9% |
| 1518 | 11.131 | 6.208 | -44.2% |

This integration costs more than the isolated allocator estimate: it also
includes transactional admission, stable-tag management, head peek/pop and
aggregate diagnostic publication. Even all-large traffic pays this overhead.
This is a CPU bottleneck, not evidence of `R_valid`, NIC losslessness or 100G
operation. A hardware run must retain the unchanged offered load and report its
actual passing rate and failure cause. No production-size allocation or testbed
execution was performed for this comparison.

For a fixed stored length, register that length plus four FCS bytes in the perf
configuration; for X register 64/512/1522 with equal weights. Set its registered
`offered_bps` to 400000000000. Copy the original `perf/ring_rate.json` and add
only that registration to each line. Repeat each mode three times, serially:

```sh
for RATE_CONFIG in perf/ring_rate.json "$M7_RATE_CONFIG"; do
  prlimit --as=4294967296 --core=0 -- python3 perf/ring_rate.py \
    --io-split --cpus "$RATE_CPUS" build/hng-ring-rate "$RATE_CONFIG" \
    -- -b "$RATE_SIZE" -n 2046 -w 0.2 -d 0.8
done
```

`RATE_CPUS` lists seven distinct allowed CPU IDs; `RATE_SIZE` is X, 64, 512 or
1518. The configured population is a ceiling. Actual live counts can vary with
bulk allocation and RX progress, so these short shared-workstation runs estimate
cost rather than establish a precise saturation rate.
