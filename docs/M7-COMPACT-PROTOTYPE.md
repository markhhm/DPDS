<!-- SPDX-License-Identifier: Apache-2.0 -->

# M7 independent compact storage prototype

This prototype is **not linked into the engine**. It adds no engine option,
changes no workload, and does not alter the approved binaries or default
storage for the 10G qualification and quarter-rate matrix. Engine integration
requires a separate approved change with explicit M7 opt-in.

The two measured design gates remain separate:

- CPU: quantify both copies and allocation/free before accepting the projected
  reduction in valid rate. No new CPU acceptance threshold is assumed.
- Capacity: include at least the existing one-second terminal backpressure
  residence. The present conservative bound exceeds 64 GiB; this gate fails.

## Standalone ownership contract

common/storage.[ch] implements one single-owner allocator. Use a separate
instance for each directional owner; the instance is not thread-safe.

A 192-byte descriptor includes all 152 private packet-metadata bytes,
64-bit generation, length, port, VLAN tags, RSS hash, packet type, selected
mbuf flags, a state guard and an intrusive free-list link. The descriptor
free list needs no separate array. Payload uses 64-byte cells and one
4-byte next index per cell. There is no cell cache, allocation fallback,
refcounted external buffer, or per-packet diagnostic log.

storage_copy_in leaves the source mbuf caller-owned. Failure consumes no
record or partial cell allocation. The caller can return the original mbuf
immediately after success. storage_materialize creates a normal mbuf and
consumes the stored record only after successful copy-out. An exhausted or
incompatible TX pool leaves the record available for drain/discard. Physical
mbuf ownership flags are not copied from the source. New refcount, buffer
address and pool identity belong to the destination allocator.

The public handle contains an index and a 64-bit generation. Stale handles
and duplicate releases invalidate the experiment without releasing a newer
record at the same index. The guard and generation are included in 192 bytes.
A generation at UINT64_MAX is refused rather than wrapped. Full descriptors,
insufficient cells, bad input, invalid handles and materialize failure set
sticky INVALID aggregate counters. Existing live records can still drain;
a later successful operation never clears that failure state.

All payload loops are bounded by ceil(1518/64)=24 cells. Free lists are
bounded by their configured arrays. Destruction refuses live ownership.
The allocator uses one aligned allocation with a padded control region,
descriptor array, payload array and aligned link array. Initialization touches
the whole configured allocation before timing. It does not allocate a
production-sized pool in a test.

## TDD and software checks

The standalone tests cover 60/64/128/129/508/512/513/1518-byte payloads,
all private metadata, preserved header fields and full pool return. Further
checks cover cell/record exhaustion without partial consumption, TX pool
failure/recovery, incompatible private-data space, stale generations, double
release, refused destruction with live ownership, and shuffled mixed-length
materialize/discard cycles. Copy-in equals materialized plus discarded once
the allocator drains, and all records/cells return to zero.

The controlled layout example is one descriptor plus 24 cells: 2048 bytes
including padded control and links. The independent capacity tests use known
original and one-second envelope counts; the 69-byte charged-frame boundary
is included, rather than checking only the three registered X sizes.

## CPU experiment

Build with the existing Meson procedure, then run from the source checkout:

    python3 perf/storage_proto.py build/hng-storage-proto --full --resident --trials 5 --out "$CPU_RESULTS"
    python3 perf/storage_capacity.py build/hng-storage-proto --out "$CAPACITY_RESULTS"

The wrapper runs one child at a time with prlimit AS=4294967296/core=0.
Resident cases populate at least 2 GiB of actual descriptor/cell storage,
not just virtual reservations, plus their generation-bearing client handles.
Every timed trial walks at least the whole populated set. The hot set has
96 live records. All eight single lengths and equal-packet X are measured.

Baseline is a hot normal-mbuf alloc/fill/free loop. The storage variant
adds copy-in, record/cell alloc/free and materialize/copy-out to a normal mbuf.
The reported increment is the difference of five-trial medians from separate
processes. The resident increment includes cold storage work; it does not
subtract an unmeasured resident engine cost. Source stamping and checksum
work are present in both. No model, physical PMD, observer or cross-core
handoff is included. This is a conservative standalone cost estimate, not
an integrated R_valid measurement or a hardware qualification.

| Stored bytes | Hot increment ns/packet | Resident increment ns/packet |
|---|---:|---:|
| 60 | 25.793 | 30.781 |
| 64 | 47.184 | 30.139 |
| 128 | 16.554 | 41.116 |
| 129 | 38.103 | 38.616 |
| 508 | 50.689 | 61.777 |
| 512 | 50.072 | 61.187 |
| 513 | 41.082 | 52.945 |
| 1518 | 117.692 | 143.871 |
| X | 63.169 | 79.304 |


For the X estimate, add incremental time to the previous 13–14.5 Mpps per
direction bottleneck cost:

    projected_Mpps = 1 / (1 / baseline_Mpps + incremental_ns / 1000)

The hot X increment is 63.169 ns/packet: projected 7.138–7.568 Mpps/direction, a 45.1–47.8% reduction under this assumption.

The resident X increment is 79.304 ns/packet: projected 6.401–6.744 Mpps/direction, a 50.8–53.5% reduction under this assumption.



The affected directional scheduler is assumed to pay both copy-in and
materialize in any later integration. Actual bottleneck distribution, Q-drop
mix and batching can change this estimate. Do not change the registered
offer, composition, delay or service rate to make the estimate pass. The
148.8 Mpps minimum-frame target remains CPU-limited independently of storage.

## Capacity including terminal backpressure

The source of sizes is build/hng-storage-proto --describe. Descriptor=192,
cell=64, cell link=4, control sizeof=144/padded=192, client handle=16 bytes.
There is no extra descriptor-index array or hidden per-cell guard/cache.
A 4 KiB allowance covers each allocator/client allocation's malloc/alignment
bookkeeping. The current prototype uses ordinary pages; 1 GiB arena rounding
is also shown as a future hugepage-placement estimate, not implemented here.

Use the unchanged original equations, Q/C=7.456541 ms and D=462 ms:

    S(D) = floor(Q/64) + 16 + ceil(C*D/(8e9*84))
    a(L) = 68*ceil((L-4)/64), L=64..1522
    rw = max(a(L)/(L+20)) = 136/89, at L=69
    rq = max(a(L)/L) = 136/69, at L=69
    cells = ceil((C*(D+T)/(8e9)*rw + Q*rq + 16*24*68)/68)

The first one-second case keeps the existing Core slot ceiling S(D), extends
cell residence by T=1 second, and keeps Core metadata at 144*S(D) bytes per
direction. Its independently provisioned maxima are conservative; this is
not a proof that every feasible mixed state needs that many bytes.

The second case explicitly extends slots to S(D+T) and charges both the
larger descriptor/client-handle arrays and larger Core reservation. It is
an alternative capacity budget, not an engine/configuration change.

Both include two fixed transport mbuf pools using the existing proposed
16384 RX/1024 TX reserve, their free rings and headers, the original 144-byte
Core reservation, real client-handle RAM, 1 GiB EAL margin, 256 MiB ordinary
allowance and 1 GiB observer allowance. Count all transport pools after
closing all ports, as required by the existing peer-pool cleanup contract.

| C | Extra residence | Extend slots? | 4 KiB arena node GiB | 1 GiB arena node GiB | Capacity gate |
|---|---|---|---:|---:|---|
| 80G | 0 s | no | 55.498 | 55.989 | INCONCLUSIVE |
| 80G | 1 s | no | 83.961 | 83.989 | FAIL |
| 80G | 1 s | yes | 162.015 | 163.467 | FAIL |
| 90G | 0 s | no | 61.779 | 62.081 | INCONCLUSIVE |
| 90G | 1 s | no | 93.800 | 94.081 | FAIL |
| 90G | 1 s | yes | 181.611 | 181.995 | FAIL |


At 90G, the original slot ceiling is 63,185,736 per direction; the one-second
cell budget is 372,038,607 cells. Extending slots too needs 197,114,308 records
per direction. The 64 GiB target is exceeded even without increasing slots.
The node-half target can only be stricter. Without known node capacity a
sub-64-GiB no-hold estimate is INCONCLUSIVE, not qualified.

One second is the nominal minimum residence budget, not a scheduling
termination guarantee: timeout evaluation, RX stop and drain may be delayed
by owner scheduling. No smaller bound is asserted. A future bounded-capacity
integration must invalidate honestly when exhausted and preserve all original
storage, handoff, terminal, shutdown and conservation gates. It must not hide
capacity pressure as Q loss, reset a deadline, spin indefinitely, or silently
use a shorter pause allowance.

## Next decision

The prototype provides costs and accounting, not permission to connect the
engine. The measured CPU increment requires acceptance of the lower projected
valid rate. The conservative one-second memory budget needs further design
work or an explicitly approved, justified bound. Existing 10G behavior and
binaries remain outside this experiment.
