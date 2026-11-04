<!-- SPDX-License-Identifier: Apache-2.0 -->

# TX handoff backpressure and owner diagnostics

The separate scheduler/TX layouts (4, 5 and 7 lcores) reserve TX handoff
space before removing packets from the delay model. The combined layouts
continue to hand scheduler output directly to their local TX state.
No build or deployment command changes are required.

## Bounded handoff

The ring allocation remains `SCHED_TX_RING_SZ = 1024` in
`common/config.h`. DPDK rings created without `RING_F_EXACT_SZ` have
**1023 usable slots**. Each ring has exactly one scheduler producer.

On each scheduler iteration, `core/worker.c:lcore_worker` samples
`rte_ring_free_count()` and caps model output at
`min(free_slots, MAX_BURST_SIZE)`. This is the same free-space-first rule
used for RX handoff in `core/receive.c:rx_iteration`. A consumer can only
increase the producer's free-space budget before enqueue.

Admission and frontier processing still run when the TX ring is full.
The output suffix stays in the model. Its release time, deadline,
per-egress merge order and ownership remain unchanged. END seals the
input and advances the service ledger as before; it does not bypass the
output budget. TX computes submit lateness against the original deadline.
Backpressure can therefore increase reported lateness; it does not make
a late packet appear timely or prove sufficient sustained throughput.
Model storage and RX queues remain finite, with their existing gates.

A ring that is continuously observed full **while the model has backlog**
for **more than `TERMINAL_TIMEOUT_NS` (1,000,000,000 ns)** invalidates the run
with `TX handoff terminal timeout`. The timer starts at the first such
observation and resets when the scheduler observes available space or
no model backlog. It measures continuous full-ring observations, not packet
age or a maximum permissible submit lateness. Available space after a short
TX pause permits normal catch-up without loss. There is no enqueue retry
spin: each owner iteration has its usual bounded burst work, and an idle
iteration calls `rte_pause()`.

The timeout requests RX stop through `host_invalidate`. The existing RX END
handoff then seals scheduler input. The independent
`SHUTDOWN_TIMEOUT_NS` (5,000,000,000 ns) scheduler drain gate remains active:
if the suffix still cannot drain, model-owned packets are explicitly
aborted, freed and counted as `shutdown_drop` in an INVALID run.
Timeouts are evaluated when their owner runs; they do not guarantee wall-clock
termination of a thread that the OS never schedules.

The unexpected short-enqueue `TX handoff ring exhausted` gate is retained.
So are RX handoff loss, finite model storage, PMD pending capacity,
terminal TX retry, trace policy, shutdown drain, NIC loss and software
packet/byte conservation gates. A timeout or invariant failure is never
converted into a normal Q drop or a passing run.

## Aggregate diagnostics

With normal DPDK telemetry enabled, request:

```sh
RUNTIME_DIRECTORY="$DPDK_RUNTIME_ROOT" python3 "$TELEMETRY_CLIENT" -f "$FILE_PREFIX"
# At the client prompt, enter: /dpds/diagnostics
```

The command takes no parameters. It returns one flat JSON dictionary under
`/dpds/diagnostics`. The same key names appear as `key=value` lines in stdout
and the `-o "$FINAL_STATS"` file. Reason values can contain spaces; parse the
whole value after the first `=`, rather than splitting that line on spaces.

| Field | Meaning |
| --- | --- |
| `tx_owner0_ring_high_water`, `tx_owner1_ring_high_water` | Maximum producer-observed ring occupancy, in packets, using the enqueue operation's returned remaining space; at most 1023 for the current allocation. Scheduler owner index identifies the ring, not the egress port number. |
| `rx0_loop_gap_max_ns`, `rx1_loop_gap_max_ns` | Maximum elapsed time between consecutive RX owner loop starts. |
| `scheduler0_loop_gap_max_ns`, `scheduler1_loop_gap_max_ns` | Maximum elapsed time between consecutive scheduler owner loop starts. |
| `tx0_loop_gap_max_ns`, `tx1_loop_gap_max_ns` | Maximum elapsed time between consecutive TX owner loop starts. |
| `rx0_stopped_mono_ns`, `rx1_stopped_mono_ns` | `CLOCK_MONOTONIC` time when the owner observes RX stop, after its final poll/publication iteration and before emitting END. Zero means no stop has been recorded. |
| `first_invalid_recorded` | 1 after the first INVALID record has been completely published; otherwise 0. |
| `first_invalid_mono_ns` | Absolute `CLOCK_MONOTONIC` nanoseconds for that first INVALID. Zero before publication. |
| `first_invalid_at_ns` | That event's engine-relative monotonic nanoseconds, using the same epoch as native observations. |
| `first_invalid_owner` | `main:0`, `rx:k`, `scheduler:k`, `tx:k` or `combined:0`, identifying the calling owner. `k` is 0 or 1. `none` before publication. |
| `first_invalid_lcore` | Calling DPDK logical lcore ID, not OS CPU ID. `LCORE_ID_ANY` (4294967295) means unregistered/unavailable. |
| `first_invalid_reason` | First reason text, bounded to 127 bytes (the telemetry string limit). `none` before publication. Later failures do not replace it. |

For a 7-lcore layout, owner `k` follows `host.ports[k]` ingress. The 5-lcore
layout has two scheduler owners but one RX and one TX owner; the 4-lcore
layout has one of each. In the 3-lcore layout scheduler/TX gaps describe
their shared physical loop; in the 2-lcore layout all stage gaps describe
the shared loop (RX stops being measured once polling stops). Unused owner
slots and rings report zero.

Loop gaps include model computation, PMD calls and descheduling. A large
gap is evidence of a pause in that loop, not proof of OS preemption or a
particular cause. Ring peaks cannot establish a duration by themselves.

Each owner writes a separate aligned cache line. Hot-loop diagnostics use
one engine clock read per physical loop, relaxed atomic aggregate reads,
and stores when a maximum increases. They allocate no per-packet records
and issue no diagnostic syscall per iteration. The first INVALID record
uses a release/acquire publication flag; telemetry never reads its strings
until they are complete. Live aggregate fields are independent snapshots,
not a transaction over all owners. Final output is taken after owner joins,
port/pool cleanup and native observer close, so cleanup failures are included.
A zero publication flag during a live INVALID transition means the first
record is still being published; it does not establish a valid run.

## Pool reclamation

Mbufs belong to their **ingress pool**, but accepted TX descriptors on the
**peer egress port** can retain them until PMD stop/close. Checking/freeing
pool 0 immediately after closing port 0 is therefore unsafe while port 1
is still open. All ports now stop/close before any pool is checked/freed.
NIC counters are saved before close, and final statistics use those snapshots.
The `owned != 0` failure gate remains in place after all closes.

`tests/pool_cleanup.c` reproduces this lifetime contract with real pools
and mbufs and a test-only PMD retention substitute: each egress retains
986 peer-pool mbufs until close. The previous cleanup produces
`pool port=0 ... owned=986` and attempts to free that pool too soon.
The corrected cleanup reclaims both pools in normal and INVALID paths.
This proves the cleanup ordering defect; it does not prove the exact
physical PMD descriptor contents in a previous run. No physical PMD was
run for this regression.

`tests/handoff.c` also executes both production scheduler loops against
1024-slot rings with both TX consumers stopped. It covers partial free-space
budgets, END while full, catch-up, exact comparison with unpaused model
release/deadline/sequence records, zero loss/duplicates, first-failure
preservation, the terminal timeout boundary, shutdown abort accounting and
complete pool return. Its compact synthetic model is an integrity fixture;
it does not change any registered workload or establish hardware capacity.
`tests/diagnostics.py` checks live telemetry and final output across all five
supported layouts, and verifies RX stop against the process monotonic clock.

## Monitoring during engine exit

Ports close before process exit. A concurrent `/ethdev/list` reply can
therefore become empty while the process is still finishing cleanup.
A monitor must retain the last valid NIC sample and recheck process/unit
state and exit status. Report `ENGINE_EXITED` or `ENGINE_INVALID` with the
first INVALID record and final status when available. Preserve the empty
reply or transport error as secondary evidence. If exit/final evidence
is unavailable, report an unconfirmed/incomplete outcome.
Do not turn an empty port list into zero counters or PASS, and do not replace
an earlier engine failure with the secondary two-port validation error.
