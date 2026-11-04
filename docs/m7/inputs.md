<!-- SPDX-License-Identifier: Apache-2.0 -->

# M7 registered DPDS input preparation

Use this page with the [CX-4 operator handoff](em-handoff-cx4.md).
It prepares two simultaneous 100G L1 directions, one emulator line per direction.
The M7 sequence is STL probes, then long-RTT ASTF, then selected applications.
Report the highest rate that passes all G2 gates as `R_valid`.
A smaller passing rate remains that rate.
These are preparation inputs and capacity estimates. No hardware result follows from this document.

## Source and executable identity

The calculation baseline is DPDS `13f3fab6962a7ed658fa4d0fc19b5fce1fca00d5`
and Core `f3d255dc734b2e4dc3ed9877dcd1e691ce6a20e3`.
The integration owner supplies the approved post-merge `DPDS_REV` and its Core pin.
Use the immutable source and executable provenance procedure in
[B-prime inputs](../m5/bprime-inputs.md#1-source-ref-and-immutable-pin).
Retain the compiler, DPDK build, executable hash and accepted descriptor counts.
Source identity alone does not identify an installed executable.

## Descriptor requests and packet rates

[Current constants](../../common/config.h) request **RX 4096 and TX 1024 per port**.
There is one RX queue and one TX queue per port.
[Port preparation](../../common/common.c) calls `rte_eth_dev_adjust_nb_rx_tx_desc()`
and prints `descriptors port=PORT rx=RX tx=TX` before pool allocation.
Use the accepted counts, successful queue setup and actual mlx5 path in the attempt record.

For the proposed 100 us RX-poll pause allowance, budget **RX 16384 and TX 1024 per port**.
This is a sizing candidate, subject to device limits and measured poll-gap margin.
The present engine has no runtime RX-descriptor option and still requests 4096.
A separately approved engine change and new binary are prerequisites for requesting 16384.
This PR changes no engine setting.

The [M7 lessons](em-handoff-cx4.md#rx-descriptors-and-the-shortest-frame-budget)
include FCS in the frame size and add 20 bytes for preamble/SFD and IFG:

```text
pps = 100000000000 / (8 * wire_bytes)
nominal_pause_us = accepted_packet_slots * 8 * wire_bytes / 100000000000 * 1000000
```

| Traffic | Wire bytes | Packets/s per direction | RX 4096 pause | RX 16384 pause |
| --- | ---: | ---: | ---: | ---: |
| Smallest X frame, 64 L2 bytes | 84 | 148809523.809524 | 27.52512 us | 110.10048 us |
| Fixed U, 1522 L2 bytes | 1542 | 8106355.382620 | 505.28256 us | 2021.13024 us |
| Equal-packet X mix | (84+532+1542)/3 | 17377201.112141 | 235.711146667 us | 942.844586667 us |

These times estimate an empty, replenished queue with one packet slot per frame.
The X mean cannot cover an all-small burst. The 100 us allowance is an estimate,
not a measured pause bound or a 100G qualification.
Occupancy, replenishment and mbuf shortages reduce the margin.
MPRQ/CQE compression does not multiply available packet buffers.
Record scatter, stride, WQE and CQE settings as required by the handoff.
Do not assume RSS distributes work across queues in this engine.

TX 1024 is the current request, not a measured maximum safe pause.
The same formula gives 6.88128 us at the all-small arrival rate and 126.32064 us for U.
Retain the existing retry, terminal-backpressure and zero-loss gates.
RX depth cannot fix a sustained receive or transmit deficit.

## Stress, Q and storage envelope

The offered rate is `R=100000000000` bit/s **in each direction**.
Evaluate service `C=80000000000` and `C=90000000000` bit/s separately.
Keep the B-prime fixed boundary `t=142000000 ns`, `delta=320000000 ns`, seed 1,
with `p="1"`, as in the B-prime fixed boundary. Other probabilities need their own registration.
The memory envelope assumes every accepted packet can be late; reducing `p` does not reduce it.

The condition files preserve B-prime's calculator-compatible service-time allowance:

```text
q_ns = 7456541
Q = floor(C * q_ns / 8000000000)
Q(C=80G) = 74565410 L2 bytes
Q(C=90G) = 83886086 L2 bytes
```

This uses the existing calculator's integer-nanosecond conversion of
`--q-ms 7.456541`, as in [B-prime](../m5/bprime-inputs.md#2-expanded-config_file-and-node-memory).
The original 9G Q is 8388608 bytes: its exact service time is
`8*8388608/9000000000 = 0.007456540444... s`.
The calculator rounds this upward by less than 1 ns before deriving integer Q bytes.
Thus Q/C follows B-prime within this stated rounding; it does not silently scale rounded byte values.
The separate 50 ms estimate appears only in the capacity table below. It has no condition file.

Use the [existing storage envelope](../../tools/g2/expand_m6_x710.py),
[independent reference check](../../tools/g2/reference.py),
and [pool calculator](../../perf/pool_calc.c):

```text
Lmin, Lmax = minimum/maximum charged L2 bytes, including FCS and the single VLAN
burst = 16 packets
slots = floor(Q/Lmin) + burst + ceil(C * (t_ns+delta_ns) / (8000000000*(Lmin+20)))
storage_capacity_packets = slots
storage_capacity_bytes = slots * (Lmax-4)
```

U uses Lmin=Lmax=1522. X uses Lmin=64, Lmax=1522.
Stored mbuf lengths exclude FCS: U=1518, X=60/508/1518 bytes.
The X offer has equal packet counts at charged sizes 64/512/1522.
A 64-byte-capable pool must cover the minimum-size envelope, even for that mix.
ASTF packet sizes can also reach the minimum. The fixed-U envelope does not cover small TCP/ACK frames.
Recalculate for the registered ASTF frame range before that phase.

| C | Q allowance | U slots / direction | X slots / direction |
| --- | --- | ---: | ---: |
| 80G | 7.456541 ms | 3045116 | 56165100 |
| 90G | 7.456541 ms | 3425754 | 63185736 |

The frame-byte capacity is a logical storage admission limit.
Do not add it again to mbuf RAM: the mbufs already hold these frames.
Each direction has its own service/Q state. This prepares two independent directional queues,
not one queue shared between opposite directions.
A two-line configuration requires a new per-ingress sum of both line envelopes.

## Formula-derived engine JSON and hashes

These four JSON files are **formula-derived from existing expander templates**.
They are not direct expander outputs. The existing CLI supports 10G and quarter-rate templates;
this document adds no expander option or program feature.
Each file keeps template `t`, `delta`, `p`, seed, two port directions, VLAN and frame envelope.
It replaces C, Q and both storage capacities using the formulas above.
The committed tag is 710, inherited from the registered example, not a device identity.
Use the owner's approved `ACTIVE_VLAN` when reproducing; a different tag changes the hash.

| Engine file | C per direction | Frame range | SHA256 |
| --- | ---: | --- | --- |
| [c80-u.json](../../examples/m7/c80-u.json) | 80000000000 | U | `d4c1e583fd3c35c32116bae0ddf3b1adb023924b8eed19c6df57834db05723a4` |
| [c80-x.json](../../examples/m7/c80-x.json) | 80000000000 | X | `b16b418f2d00b332fe975e1071ad4b3ba4c0e35febcbc24e1f556385fe19a67e` |
| [c90-u.json](../../examples/m7/c90-u.json) | 90000000000 | U | `5def36abf39c00a87a25ca6e0ced0598eae258951ce5adcf93ee171735c07306` |
| [c90-x.json](../../examples/m7/c90-x.json) | 90000000000 | X | `c86b1ec7da24640cd73f4a3edb5f6cb219e1ba87c3abb9c8aa891c020790e0d9` |

`CONFIG_DIR` is a new operator-selected output directory. Reproduce from `DPDS_SRC`
with `ACTIVE_VLAN=710` to match the committed hashes. Templates are intermediate files.
The following is the complete one-off derivation, not an invocation of a new tool:

```sh
cd "$DPDS_SRC"
export CONFIG_DIR ACTIVE_VLAN
python3 - <<'PYDERIVE'
import json
import os
from pathlib import Path
import subprocess
import sys

output = Path(os.environ['CONFIG_DIR'])
output.mkdir(parents=True, exist_ok=True)
for mix in ('U', 'X'):
    template = output / ('template-' + mix + '.json')
    subprocess.run([sys.executable, '-m', 'tools.g2.expand_m6_x710',
        '--condition', 'fixed-E3-142-320-1-both-L9-' + mix,
        '--active-vlan', os.environ['ACTIVE_VLAN'], '--rate-divisor', '1',
        '--out', str(template)], check=True)
    source = json.loads(template.read_text())
    for capacity in (80_000_000_000, 90_000_000_000):
        config = json.loads(json.dumps(source))
        for line in config['lines']:
            model = line['model']
            model['c_bps'] = capacity
            model['q_bytes'] = capacity * 7_456_541 // 8_000_000_000
            low = line['envelope']['min_q_bytes']
            high = line['envelope']['max_q_bytes']
            numerator = capacity * (model['t_ns'] + model['delta_ns'])
            denominator = 8_000_000_000 * (low + 20)
            slots = (model['q_bytes'] // low + line['envelope']['burst_bound_packets']
                     + (numerator + denominator - 1) // denominator)
            model['storage_capacity_packets'] = slots
            model['storage_capacity_bytes'] = slots * (high - 4)
        destination = output / ('c' + str(capacity // 10**9) + '-' + mix.lower() + '.json')
        destination.write_text(json.dumps(config, indent=2, allow_nan=False) + '\n')
PYDERIVE
sha256sum "$CONFIG_DIR"/c*.json
```

The JSON schema contains model service C, not offered L1 rate or generator packet proportions.
Register `R=100000000000` per direction separately, with U or equal-packet X as described above.
Do not feed the quarter-rate matrix to a 100G generator.
The [X profile](../../examples/m5/stl_bprime_x.py) requires a matching 100G traffic registration;
its 10G default and a rate override cannot raise its registered offer to 100G.
The [M7 generator handoff](em-handoff-cx4.md#reuse-the-stl-x-profile-at-100g)
requires that separate registration, probe flags, per-flow sequence bounds and measured generator capacity.
These engine files alone do not supply a new M7 capture matrix or qualify a generator.

The existing pool calculator was run for all four files with the matching C, frame minimum,
Q time, burst and descriptor proposal. It returned the slot/object counts below:

| File | Slots / direction | Logical frame capacity bytes / direction | Mbuf objects / direction |
| --- | ---: | ---: | ---: |
| c80-u.json | 3045116 | 4622486088 | 3068956 |
| c80-x.json | 56165100 | 85258621800 | 56188940 |
| c90-u.json | 3425754 | 5200294572 | 3449594 |
| c90-x.json | 63185736 | 95915947248 | 63209576 |

The independent Python reference accepts all four declared envelopes without allocating their production pools.
Repeat calculation and verify the target build before hardware admission.

## Mempools and NUMA capacity

The [host object count](../../common/common.c) reserves one pool per ingress port.
For one active line per direction, seven pipeline lcores and proposed accepted RX/TX=16384/1024:

```text
objects_per_pool = slots + RX + RX_handoff + TX_handoff + caches
                  + all_line_pending + burst_reserve + TX
                 = slots + 16384 + 1024 + 1024 + 7*32 + 4*1024 + 4*16 + 1024
                 = slots + 23840
```

The constants are in [config.h](../../common/config.h); `HNG_MAX_LINES` is four.
The object reserve uses full ring sizes; drain below uses usable ring capacities.
TX descriptor reservation follows each distinct egress using that pool.
Recalculate when actual accepted descriptors or line ownership differ.

The local DPDK 26.03 x86-64 calculator returns an mbuf element of 2456 bytes,
stride 2624 bytes and Core metadata 144 bytes per slot.
These are build-specific allocator sizes, not CX-4 memory measurements.
The [B-prime allocator breakdown](../m5/bprime-inputs.md#2-expanded-config_file-and-node-memory)
provides the same ring/header values.
For 1 GiB hugepages:

```text
objects_per_page = floor(1073741824 / 2624) = 409200
object_pages = ceil(objects_per_pool / 409200)
free_ring_slots = next_power_of_two(objects_per_pool + 1)
pool_bytes = object_pages*1073741824 + (8*free_ring_slots+384) + 1056960
Core_ordinary_bytes_per_direction = slots*144
shared_EAL_bytes = 4*rte_ring_get_memsize(1024) + sizeof(pipeline_stats)
                 = 4*8576 + 4800 = 39104
```

Verify these build sizes again for the target executable. Different flags, page sizes,
DPDK headers or PMD adjustments require recalculation.
Use the existing calculator **without `--verify`** for these large envelopes;
that prints sizes without EAL, NIC access or full-pool allocation:

```sh
"$POOL_CALC" --c-bps "$C_BPS" --t-ms 142 --delta-ms 320 \
  --q-ms "$Q_MS" --p 0.01 --p 0.10 --p 1 \
  --min-frame "$MIN_FRAME" --max-frame 1522 --burst 16 \
  --lines-per-port 1 --lcores 7 --rx-desc 16384 --tx-desc 1024 \
  --page-bytes 1073741824
```

The calculator's `per_numa_node` label describes one ingress pool here.
Its `both_nodes` total describes two pools. Place them according to actual NIC/RX locality,
not those labels. If both ports share one node, that node must hold both pools.

For an example with both pools on one approved node, use this explicit estimated reserve:

```text
EAL_base = 2*pool_bytes + 39104
EAL_GiB = ceil(EAL_base / 1073741824) + 1
ordinary_required = 2*slots*144 + 256*1048576
node_accounting_required = EAL_GiB*1073741824 + ordinary_required
```

The extra EAL GiB and 256 MiB ordinary allowance are estimates, not measured PMD or observer limits.
The table excludes separate capture/observer buffers and storage-worker memory.
Add their registered budgets before node acceptance.

| C | Q allowance | Frame range | Pool bytes / direction | EAL GiB, two pools | EAL + Core + ordinary allowance, GiB (estimate) |
| --- | --- | --- | ---: | ---: | ---: |
| 80G | 7.456541 ms | U | 8624546368 | 18 | 19.067 |
| 90G | 7.456541 ms | U | 9698288192 | 20 | 21.169 |
| 80G | 7.456541 ms | X | 148714299968 | 279 | 294.315 |
| 90G | 7.456541 ms | X | 166967910976 | 313 | 330.198 |
| 90G | 50 ms, estimate only | X | 186832134720 | 350 | 369.204 |

The last row is only a worst-case capacity estimate, not an M7 input condition:
`Q=floor(90000000000*50000000/8000000000)=562500000`,
`slots=70664078`, `objects=70687918`, `object_pages=173`, `free_ring_slots=134217728`.
The same pool and node formulas produce that row. Do not generate or run a 50 ms condition from it.

For node index `DUT_NODE`, set `LOAD_SOCKET_MEM` to the MiB vector with
`EAL_GiB*1024` at that index and zero at unused nodes. Apply the same vector to `--socket-limit`.
For example, the 90G U/7.456541 ms candidate requires 20480 MiB on its selected node.
The 90G X candidate requires 320512 MiB there. These are placement examples, not host facts.
If ports occupy different nodes, compute each node's pool sum and add the shared allocations
on their actual allocation node. Never halve a same-node requirement by naming two nodes.

Before launch, the owner supplies node capacity, free 1 GiB hugepages, ordinary free RAM,
other reservations and the observer/storage budget through the existing bring-up manifest:

```text
free_hugepage_count[node] >= EAL_GiB[node]
free_ordinary_bytes[node] >= Core_metadata[node] + ordinary_allowance[node] + other_ordinary_reservations[node]
assigned_total_node_bytes >= EAL_reservation + ordinary_required + all_other_reservations
```

Reserved hugepages are not ordinary free memory. Do not count them twice.
A smaller node fails this condition; do not shrink Q, delay or the minimum-frame envelope to fit it.
No target-node capacity was read during this preparation. Hardware acceptance is pending.

## Warm-up, generator STOP, drain and engine stop

Use the [B-prime stop contract](../m5/bprime-inputs.md#4-non-interactive-generator-control-and-reply-contract).
The existing control default is a 0.25 s polling interval, two unchanged TX intervals,
and a 5 s settling timeout after the stop RPC. This needs at least three snapshots.
RPC connection and calls retain their SDK timeouts, so five seconds is not an end-to-end bound.
Require successful STOPPED, both ports inactive, stable_samples=2, matching identity and no error.
Keep independent capture active through DUT drain.

For a fixed condition with no trace outage, take the largest wire/Q ratio in its frame range:

```text
rho = (Lmin+20)/Lmin
Q_drain = 8*Q*rho/C
Q_fill = 8*Q*rho/(R-C)
D_model = (t_ns+delta_ns)/1e9 + Q_drain
I/O_packets = RX + 1023 RX_ring + 1023 TX_ring + 1024 pending + TX + 4*16 burst
            = 20542 for RX=16384, TX=1024 and one active line
D_io = I/O_packets * 1542*8/C
D_fixed_estimate = D_model + D_io + 1 s TX tolerance
WARMUP_S = ceil(D_fixed_estimate + Q_fill)
DRAIN_WAIT_S = ceil(D_fixed_estimate)
```

The 1023 usable slots follow a 1024-entry DPDK ring with one reserved slot.
The 1 s tolerance is [TERMINAL_TIMEOUT_NS](../../common/config.h), not permission for terminal loss.
These wall-time allowances are estimates; sustained slow TX can invalidate the run.
Stop on terminal/drop errors even if the configured wait has not expired.

| C | Q allowance | Frame range | Q drain s | I/O drain s | D_fixed s (estimate) | WARMUP_S (estimate) | DRAIN_WAIT_S (estimate) |
| --- | --- | --- | ---: | ---: | ---: | ---: | ---: |
| 80G | 7.456541 ms | U | 0.007554524 | 0.003167576 | 1.472722101 | 2 | 2 |
| 80G | 7.456541 ms | X | 0.009786710 | 0.003167576 | 1.474954286 | 2 | 2 |
| 90G | 7.456541 ms | U | 0.007554524 | 0.002815623 | 1.472370148 | 2 | 2 |
| 90G | 7.456541 ms | X | 0.009786710 | 0.002815623 | 1.474602334 | 2 | 2 |

Register `STOP_TIMEOUT_S=10` as an estimated control/port/EAL teardown allowance after the engine stop request.
It exceeds the largest tabulated D_fixed of 1.474954287 s; the remaining margin is at least 8.525045713 s.
This margin is not a measured shutdown guarantee. A timeout fails the attempt and preserves evidence.

After verified generator stop, wait the registered drain interval and retain later unchanged
packet counters plus final native/capture health. Then request the engine's clean stop.
Do not use a forced kill as successful drain. `SHUTDOWN_TIMEOUT_NS=5 s` handles trace outage holds;
it is not a global process-exit deadline. These fixed bounds do not cover a trace outage.
For trace, use the registered maximum delay/hold envelope and terminal policy instead.

The measurement owner still registers observation duration, sample count, clocks, storage I/O,
per-flow identity limits and observer-cost qualification before a run.
The [M7 handoff](em-handoff-cx4.md) keeps those gates separate from this capacity calculation.
