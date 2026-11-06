<!-- SPDX-License-Identifier: Apache-2.0 -->

# M7 hybrid storage and registered hold experiment

This extends the [all-compact prototype](M7-COMPACT-PROTOTYPE.md).
It remains **independent of the engine**. No production scheduler, packet
path, configuration parser or approved binary is changed.

## Registration and ownership

T is charged L2 bytes including the FCS; an mbuf stores L-4 bytes.
With explicit M7 opt-in, L<=T is copied into compact storage and the source
mbuf is returned. L>T retains the original mbuf. Get returns that exact
pointer with its metadata and refcount unchanged; it makes no copy.
Tagged items are 16 bytes, including their record generation or mbuf pointer.

The single-owner hybrid wrapper preserves transactional failure behavior.
Failed put leaves source ownership and the destination item unchanged.
Failed materialize retains the record. Items are linear and must not be
copied while live; get/discard empties the item. Normal and compact ownership
must both drain. Aggregate counters account for retained, returned and
discarded mbufs. Full private metadata preservation uses the original
compact allocator.

The standalone registration has these fields:

    m7_opt_in = true
    threshold_l2 = T
    handoff_hold_ns = H

H defaults to **10,000,000 ns (10 ms)** and can be registered using
--hold-ns. It is a **failure threshold, not a measured maximum pause**.
The model of the M7 handoff invalidates with
**TX handoff hold exceeded** when continuous observed full-ring time is
strictly greater than H. Equality is allowed; observing room resets the
timer. The M7 condition does not require remaining model backlog. The
non-M7 reference retains the existing 1 s terminal threshold with backlog.
The actual engine is not connected to either new policy or backend here.

Tests cover T and T+1, exact original-pointer return, metadata, discard and
double consumption, failed put/materialize ownership, custom H, H/H+1,
timer reset, and non-M7 no-copy/one-second behavior. The original allocator
tests remain unchanged.

## Joint live-byte capacity

Use the registered unchanged C, Q, D=462 ms, and H=10 ms:

    W = C*(D+H)/8e9
    S = floor(Q/64) +16 +ceil(W/84)
    k_T(L) = 192 +68*ceil((L-4)/64) +16, if L<=T
           = 2624 +16, otherwise
    B_joint = W*max(k_T(L)/(L+20)) +Q*max(k_T(L)/L) +16*max(k_T(L))
    L ranges over every charged size 64..1522

The 16-byte active item is included in packet cost. Core still reserves
144*S bytes per direction. Extra normal-pool free-ring capacity, allocator
control/guard/alignment, two directional page rounding, fixed transport
pool/cache reserves, EAL margin, ordinary margin and observer allowance
are also charged. No sum of the small/medium/large live maxima is used.

Within the retained-mbuf branch k is constant, so its ratio is largest at
L=T+1. That is the global worst point for T=128/256/512. For larger T the
compact cell boundary at L=69 can dominate: 344/89 memory/wire and 344/69
memory/Q. Treating T+1 as the global maximizer for every T would be wrong.

| C (Gbit/s) | T | Worst wire L | Joint budget (GiB) |
|---:|---:|---:|---:|
| 80 | 128 | 129 | 181.133 |
| 80 | 256 | 257 | 107.133 |
| 80 | 512 | 513 | 66.883 |
| 80 | 645 | 646 | 56.758 |
| 80 | 663 | 69 | 56.758 |
| 80 | 1024 | 69 | 56.758 |
| 80 | 1521 | 69 | 56.696 |
| 80 | 1522 | 69 | 56.634 |
| 90 | 128 | 129 | 203.556 |
| 90 | 256 | 257 | 119.056 |
| 90 | 512 | 513 | 72.806 |
| 90 | 645 | 646 | 62.681 |
| 90 | 663 | 69 | 62.681 |
| 90 | 1024 | 69 | 62.681 |
| 90 | 1521 | 69 | 62.619 |
| 90 | 1522 | 69 | 62.557 |

The table uses 1 GiB future arena rounding. The actual standalone allocator
uses ordinary pages. The target is min(64 GiB,node_capacity/2); unknown node
capacity never establishes hardware acceptance.

At 90G, the first threshold whose rounded joint bound fits 64 GiB is
**T=645**. The compact-dominated knee is **T=663**. The strictly smallest
joint budget in the full scan is **T=1522**, where every packet is copied;
that is not a CPU optimum. T=1521 preserves the largest X mbuf while copying
only the other two sizes. There is no single accepted CPU/capacity optimum.

### Live bound versus actual allocation

The joint bound is a **live occupancy bound for a future shared backing
budget**. The current prototype still reserves a compact arena and a
normal mbuf pool independently. Each can have a different maximum state;
their preallocated bytes remain resident even when the corresponding
state is not active. Their actual reserve cannot be replaced by the
smaller joint number.

| C (Gbit/s) | T | Independent reserves, 1 GiB arena pages | Independent reserves, 4 KiB arena pages |
|---:|---:|---:|---:|
| 80 | 128 | 214.343 | 212.564 |
| 80 | 256 | 142.343 | 140.564 |
| 80 | 512 | 102.343 | 100.564 |
| 80 | 645 | 92.343 | 90.564 |
| 80 | 663 | 92.343 | 90.564 |
| 80 | 1024 | 80.343 | 78.564 |
| 80 | 1521 | 72.343 | 70.564 |
| 80 | 1522 | 58.343 | 56.564 |
| 90 | 128 | 241.480 | 239.978 |
| 90 | 256 | 158.480 | 156.978 |
| 90 | 512 | 112.480 | 110.978 |
| 90 | 645 | 102.480 | 100.978 |
| 90 | 663 | 102.480 | 100.978 |
| 90 | 1024 | 88.480 | 86.978 |
| 90 | 1521 | 80.480 | 78.978 |
| 90 | 1522 | 64.480 | 62.978 |

The independent-reserve column charges compact record/cell maxima, the
normal L=T+1 reserve, actual free rings/headers/cache, fixed tagged-item
array and Core allocation. It is deliberately distinguished from the
required joint analysis. No memory borrowing or common arena for normal
mbufs and compact records is implemented. A joint budget below 64 GiB
therefore does **not** mean the present hybrid allocation passes that gate.

H includes nominal full-ring residence for capacity planning. It is not
a promise that an OS-descheduled owner checks the deadline exactly at H.
Actual exhaustion must remain honest INVALID; a deadline must not be
reset or a packet silently converted to Q loss.

## CPU experiment and uncertainty

All X cases keep equal packet counts at charged sizes 64/512/1522.
T=128/256 copies one third. T=512 through 1521 copies two thirds with
identical branches and working-set sizes for this mix. T=1522 copies all.
Equivalent policies are grouped instead of ranking timing noise by T.

Every threshold has five trials in hot and >=2 GiB actively populated
resident conditions. Resident trials traverse the whole active window.
The hybrid working set includes both compact records and retained normal
mbufs. Processes are serial, AS-limited to 4 GiB with core dumps disabled.

Without hugepages, the normal software mempool needs its 4 KiB page
padding charged to EAL, not just the 2624-byte object stride. Sizing is
queried before EAL launch; the initial too-small-pool experiment failed
without changing the AS limit. The corrected benchmark remains inside
that limit. This software-page behavior is not a hardware DMA result.

The final controlled run fixes CPU affinity before timing, selected using
load observations rather than benchmark speed. A fresh corresponding
baseline precedes each threshold/mode. All exploratory measurements are
retained. Two references are reported:

- Hot: normal mbuf alloc/fill/free with a small working set.
- Resident: normal retained mbufs with at least 2 GiB active storage.
  The byte-sized footprint is matched; packet counts differ because
  compact storage is denser. It is not an integrated engine baseline.

| T | Hot increment (ns/packet) | Resident increment vs resident normal (ns/packet) | Resident total (ns/packet) |
|---:|---:|---:|---:|
| 128 | 21.275 | 69.065 | 279.798 |
| 256 | 16.130 | 46.246 | 280.137 |
| 512 | 45.011 | -0.991 | 222.547 |
| 645 | 43.142 | -30.793 | 214.370 |
| 663 | 29.477 | -14.720 | 211.800 |
| 1024 | 36.509 | -42.919 | 204.365 |
| 1521 | 35.967 | 66.295 | 268.707 |
| 1522 | 91.014 | -48.529 | 138.657 |

| Mode | Copied X packets | Median increment (ns) | Estimated directional Mpps |
|---|---|---:|---:|
| hot | one-third | 18.421 | 10.49–11.44 |
| hot | two-thirds | 37.612 | 8.73–9.38 |
| hot | all | 91.014 | 5.95–6.25 |
| resident | one-third | 50.549 | 7.84–8.37 |
| resident | two-thirds | -9.666 | 13.00–14.50 |
| resident | all | -48.529 | 13.00–14.50 |

Projection adds the measured positive increment to the prior 13–14.5 Mpps
bottleneck cost. Negative cold-reference differences are retained in raw
ns but receive zero speedup credit: they can reflect density, page/cache
behavior and scheduling, not faster engine computation. A cold case minus
the hot reference is also retained as a conservative scenario including
the original cold-mbuf cost. Neither reference is a G2 R_valid measurement.

The proposed directional scheduler pays copy-in/materialize; the large
branch pays only wrapper/ownership work. Model, physical PMD, observer,
NUMA and pipeline placement still need an integrated measurement after
approval. No rate, composition, service/Q or propagation delay is reduced
to make a benchmark pass.

## Reproduce

Build with the existing procedure. No new engine option is introduced:

    M7_HANDOFF_HOLD_NS=10000000
    python3 perf/storage_hybrid_capacity.py build/hng-storage-hybrid --hold-ns "$M7_HANDOFF_HOLD_NS" --out "$CAPACITY_RESULTS"
    taskset -c "$BENCH_CPU" python3 perf/storage_hybrid.py build/hng-storage-hybrid --m7-opt-in --thresholds 128 256 512 645 663 1024 1521 1522 --hold-ns "$M7_HANDOFF_HOLD_NS" --resident --trials 5 --out "$CPU_RESULTS"

The wrappers keep one heavy child at a time under AS4GiB/core=0. The
default smoke test uses no resident multi-GiB allocation. Keep the existing
10G paths, binaries and one-second terminal behavior unchanged.

The result is still a prototype. Any shared backing allocator, choice of T,
and later engine integration need a separate review of the measured cost
and actual physical budget.
