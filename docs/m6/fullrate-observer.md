<!-- SPDX-License-Identifier: Apache-2.0 -->

# Short full-rate observer supplement (C)

C supplements the quarter-rate, independently timed M6 matrix. It does not
replace that matrix or the separate B-prime capacity gate. All numbers below
are estimates; no physical run is certified by this plan or its software tests.

## Selection and schedule

Regenerate the explicit plan with:

```sh
python3 -m tools.g2.fullrate_plan --matrix examples/m6-x710-fullrate.json --out "$C_PLAN"
```

The committed result is [m6-fullrate-observer.json](../../examples/m6-fullrate-observer.json).
The source is the explicit [full-rate snapshot](../../examples/m6-x710-fullrate.json).
C copies its original 10G offered rate, C, Q, delay, late probability, seed and
trace registration without scaling. The generator rejects quarter-rate inputs;
`examples/m6-x710.json` is the quarter-rate main matrix after its rollout. The
CLI defaults to `examples/m6-x710-fullrate.json`.

| Condition | Purpose | ON acquisitions | Exposure each |
|---|---|---:|---:|
| fixed-M-CA-65-0-0-both-L9-U | fixed cable baseline, both directions | 7 | 60 s |
| fixed-E-CB-120-0-0-forward-L9-U | longer fixed delay, single direction | 7 | 60 s |
| fixed-E1-142-320-0.10-both-L9-U | late selection, long residence, bidirectional pressure | 7 | 60 s |
| fixed-E3-142-320-1-reverse-L9-U | all-late endpoint, reverse direction | 7 | 60 s |
| trace-single-A-forward-L9-U | corridor A, complete clamp prefix | 36 | 9 s |
| trace-single-B-both-L9-U | corridor B, asymmetric bidirectional prefixes | 36 | 9 s |
| outage-hold-forward-U | registered synthetic hold/recovery | 10 | 1 s |

The first six are original inventory members. The registered trace profiles
have no outage intervals. The last condition therefore uses the pinned Core's
existing `tests/fixtures/outage-hold.json`, with its source hash in the plan.
This single synthetic companion is listed outside the 360. It covers outage
hold, recovery and the resulting output burst; Q drop is covered by the fixed
and recorded-trace stress conditions. Translate the registered trace epoch to the acquisition's engine
clock before starting; preserve the profile's delay curve, outage intervals,
action and terminal horizon. Bind that derived configuration and trace digest
in the run registration. Stop if actual offers/service/drain exceed coverage.

Every active direction offers 10 Gb/s with C = 9 Gb/s and Q = 8,388,608 bytes.
This retains overload and Q-drop pressure. U uses 1,522-byte Q charges and
1,542-byte wire frames, about 810,636 offered packets/s per direction.
The small-frame X mix is excluded: the existing native stage1 identity parser
requires the complete 100-byte probe payload, which cannot fit its smallest
frames. C is not a mixed-frame capacity claim.

Fixed and recorded-trace acquisitions include 2 s warmup, the exposure above,
1 s drain and 5 s reset. The synthetic outage companion has no warmup, 1 s
exposure, 1 s drain and 5 s reset. Keep warmup arrivals in the exact Q/trace
prefix; only registered exposure samples contribute timing statistics.
There are 110 ON acquisitions: 3,198 s (53.30 min). Identical OFF comparisons
add 3,198 s. Total is 106.60 min, or 116.60 min with 10 min reserve. These are
schedule estimates, not a change to the full-matrix repeat/acceptance rules.
A failed drain, insufficient samples or any missing gate needs explicit review.

## Capture and provenance

Use the existing physical opt-in with the registered configuration:

```sh
"$DPDS_EXECUTABLE" $EAL_PCI_ARGUMENTS -- -c "$ENGINE_CONFIG" \
  --observe-binary "$NATIVE_FILE" --observe-physical \
  --observe-identity stage1 --observe-capacity 65536
```

Only mirror one physical DUT **egress** per acquisition. Alternate captured
sides across bidirectional repeats as the plan specifies. Native evidence still
covers both active directions. Preserve complete pcapng capture-health records
and before/after NIC, generator, receiver and engine counters.

Configured service C is 9 Gb/s on the captured side. This is not a hard bound
on instantaneous egress: delayed packets and outage recovery can be submitted
in bursts, and one physical 10G output can reach 10 Gb/s. A single-egress mirror
therefore fits one 10G capture port but has **zero peak-rate headroom**. Do not
claim guaranteed 1 Gb/s headroom or silently discard recovery bursts. Unknown
health or any capture-side drop makes the independent check INCONCLUSIVE.
The reporter can still expose a confirmed DUT error separately.

Physical native capture now accepts PCI `net_i40e` as well as the previous
mlx5 driver/bus pairs. This is the driver name registered by
[DPDK's i40e source](https://github.com/DPDK/dpdk/blob/v26.03/drivers/net/intel/i40e/i40e_ethdev.c#L719).
The device gate still rejects virtual/unknown buses, unknown drivers and i40e
on an auxiliary bus. Software/mocked tests qualify no physical device.

## Software timestamp bounds

Current `core/receive.c` calls `host_now_ns()` **for each packet after**
`rte_eth_rx_burst` returns. It does not assign one timestamp to the entire burst.
`core/transmit.c` brackets each PMD TX call with `submit_before_ns` and
`submit_after_ns`; only its accepted prefix is delivery evidence. Zero-accepted
calls and unaccepted retry suffixes are not deliveries.

For a selected accepted packet, define A = recorded software RX time,
B = TX submit-before and E = TX submit-after. The reported midpoint OWD is
`floor((B+E)/2) - A`. Its conservative software-bound uncertainty is
`ceil((E-B)/2) + U_clock`. The retained witnesses keep A, B and E, so the full
software interval `[B-A-U_clock, E-A+U_clock]` remains available.

Register a measured `U_clock` covering the run, all participating lcores and
maximum residence time. Include cross-core skew, frequency/drift and timestamp
quantization (at least `2*ceil(10^9/timer_hz)` for a pair). Retain the underlying
clock test/settings as evidence; a declared number alone is not a qualification.
Missing coverage/bounds remain INCONCLUSIVE. Existing median 100 us and p99
500 us gates apply to these software intervals and preserve minimum samples.

There is **no finite wire-OWD error bound** in current native metadata. NIC RX
queue residence, time between polls and TX/NIC/PHY residence are not measured.
Even the serialization span of 32 full frames at 10G is about 39.48 us; it is
not a bound on queue wait or timestamp error. Do not subtract that span, DUT
residence or a load-dependent tail as a calibration offset. C's OWD is a DUT
self-measurement and fails OS4 independence by construction.

## Offline path

The separate CLI reuses NativeArrays, NativeProjection, SampledReference,
OnlineCells and the existing C seqcheck. It does not change normal M6 reporting.
Use the existing G2 manifest as checked **model/sampling registration metadata**:
its configuration, traces, windows, masks, required cells, capacities and native
actor bindings must describe C. Set `timing_mode="stress"`: C always executes
the exact all-arrival Q recurrence, including fixed lines. Calibration-mode
registrations are rejected. A required cell with no selected offers remains
INCONCLUSIVE; an empty timing population cannot stand in for evidence. Set the main required-cell masks to 8 (1/256)
and provision the sample capacity for the entire exposure. Streams, external
capture-pair and calibration-path metadata are not opened by this CLI; do not
create fake captures, fake calibration files or expanded packet JSONL for it.

Register a source document (render paths and full SHA256 values before use):

```json
{
  "schema": "hng-fullrate-observer-v1",
  "registration": {"path": "${G2_REGISTRATION}", "sha256": "${REGISTRATION_SHA256}"},
  "native": {"path": "${NATIVE_FILE}", "sha256": "${NATIVE_SHA256}"},
  "receiver": {"path": "${RECEIVER_PCAPNG}", "sha256": "${RECEIVER_SHA256}", "ingress_port": 0},
  "clock_bound": {"path": "${CLOCK_BOUND_FILE}", "sha256": "${CLOCK_BOUND_SHA256}"}
}
```

`ingress_port` identifies the captured direction, not the capture NIC interface.
Its registered egress must be the only DUT output in the supplied receiver file.
Paths are relative to this source document. The clock-bound JSON contains
`schema="hng-native-clock-bound-v1"`, the same `run`, `start_ns`, `end_ns`
(half-open engine-time validity) and `pair_error_ns`. Use `clock_bound=null` to
obtain an explicit INCONCLUSIVE diagnostic rather than inventing an uncertainty.

```sh
prlimit --as=4294967296 --core=0 -- python3 -m tools.g2.fullrate_observer \
  "$C_SOURCE" --out "$C_REPORT_DIR" --seqcheck "$SEQCHECK_EXECUTABLE"
```

Each invocation needs a new output directory. Run one heavy process at a time.
The reader validates the complete binary CRC/sequence/semantic/health seal and
source hash, updates exact Q and trace state for **all** arrivals, and retains
only selected timing/Q witnesses. A second bounded pass resolves selected host
decisions and accepted TX prefixes. There is no full all-packet Python join.

For the captured direction, native RX identities are projected to a clearly
labelled pcapng reference, then C seqcheck compares it with the actual external
egress. The projection contains reconstructed probe headers and software times;
it is not original packet bytes or an independent ingress observation. Its
zero-drop health describes completeness of that projection only. Complete
seqcheck source hashes, counts, first issue details and intentional model-drop
counter reconciliation feed order/storage gates. The other direction explicitly
says NOT_CHECKED for external order/storage, even though its native timing/Q
samples are judged. Loss before native RX needs independent generator/NIC
counters; this path cannot detect it by comparing native RX to egress.

Outputs use `hng-fullrate-observer-report-v1`, always `g2_qualified=false` and
`coverage.os4_independent_owd=false`. PASS applies only to the stated supplement
scope. Full custody, unsampled Q decisions and unsampled latency remain outside
that scope. Report JSON, selected witnesses and the full checker JSON are kept
separately from normal M6 seals; this command cannot overwrite them.

## Observer cost and noninterference

For every ON acquisition, require the #28 native `observer_cost` result to be
PASS with no warnings: zero producer/capture loss, positive queue capacities,
**each** high-water strictly below capacity and zero final backlog. Also retain
the process's `observer_io` shutdown line: I/O-pool high-water must be below
capacity and pending must be zero. This separate pool gate cannot be inferred
from SPSC queue headroom. Preserve the full stdout/stderr and successful exit.
External capture health and headroom remain separate prerequisites.

For each ON/OFF pair retain the same configuration/profile hashes, generator
identity schedule, seed, frame mix, offered rate, lcore/NUMA placement, exposure,
drain and background conditions. Keep the capture process enabled in both arms.
Record OFF counters through the existing statistics output and NIC snapshots;
turn off only native observation. The OFF arm has no native timing report.

Compare generator offers, NIC RX/TX, `imissed`, `rx_nombuf`, errors, engine
received/accepted/model-drop/other-drop counts, final ownership and external
receiver counts per direction. Require zero non-model loss/errors and complete
conservation in both arms. Report delivered rate and every ON-minus-OFF delta.
Pre-register the finite-window rate/model-drop acceptance envelope before the
physical pilot; differences in software arrival timing can change legitimate
Q decisions. Do not choose a tolerance after seeing the pair. Without that
registered envelope and a successful comparison, noninterference is unproven,
even when the native SPSC `observer_cost` alone says PASS. Keep B-prime and
OS4 qualifications separate. No physical noninterference claim is made here.

## Disk/time budget (ESTIMATE)

The plan uses the existing conservative raw-budget density of 316 native bytes
per offered packet and a 160-byte pcapng EPB per 128-byte snaplen packet. The
native density is an estimate, not a format limit. At bidirectional 10G it
predicts about 512.3 MB/s native plus 116.7 MB/s captured egress on average.
Allow at least 800 MB/s sustained local disk throughput, preferably 1 GB/s;
qualify the actual filesystem/controller at load. Do not use tmpfs or treat a
virtual development disk as physical capture qualification.

For the complete ON/OFF schedule, estimated native raw is 1.015 TB; receiver
raw is 0.311 TB per arm; total raw is 1.636 TB. Derived identity projections
add 0.329 TB if all are retained. The largest acquisition raw is 39.64 GB and
its largest derived projection is 8.04 GB. Serial judging and removal of
recreatable projections after preserving their hashes avoids accumulating
0.329 TB of temporary data. Do not delete original raw under that instruction.

These are **additional** C costs. They do not fit in the main matrix's 1.8 TB
retained-raw allowance. Until a combined retention allocation is approved,
reserve separate staging space or stop; do not silently reuse A's K-1 quota.
The existing destructive M6 pruner does not accept this supplemental source
schema. Budget estimates exclude filesystem metadata, reports and selected
witnesses. Clock/device/observer qualification, physical measurements and any
combined retention policy remain operator gates.
