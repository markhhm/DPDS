<!-- SPDX-License-Identifier: Apache-2.0 -->

# Native software observation boundary

The opt-in host exporter implements the native boundary of `hng-observer-v1` for
local PCAP, ring and null PMDs. It does not qualify hardware timing or capacity.
A run with capture disabled follows the normal host pipeline. No packet bytes
are changed and no packet file is opened by the exporter or adapter.

Supply a distinct inherited writable pipe/socket FD, an explicit software scope
and an occurrence policy after EAL's `--` separator:

```sh
dpds EAL_ARGUMENTS -- -c engine.json --observe-fd 3 --observe-software \
  --observe-identity software-l2 --observe-capacity 4096
```

`stage1` selects a retained-VLAN IPv4 UDP probe with the original 100-byte payload:
big-endian sequence, magic `0x4c30504b`, flag 0/1 and 87 zero bytes. Set the UDP
destination port with `--observe-probe-port` (default 9000). The flow token is the
source/destination IPv4 integers and UDP ports; the identity appends the decimal
probe sequence. Production traffic without this occurrence profile is explicitly
incomplete. Neither a process pointer nor a modeled selection flag is an identity.

`software-l2` is an explicit fixture policy for EtherType `0x88b5`: little-endian
64-bit occurrence ID, fixture arrival and fixture sequence at offsets 18, 26 and
34 after a retained VLAN header. The ID is namespaced by input port. Embedded
fixture arrival/sequence bytes do not replace the host's observed RX clock or
physical input ordinal. Each occurrence ID must be unique in its input port/run.
The test fixtures compare forwarded frames byte for byte.

The stream adapter receives already-open text streams; the CLI accepts only
pipe/socket descriptors:

```sh
python3 -m tools.g2.native_adapter engine.json demo --input-fd 3 \
  --ingress-fd 4 --host-fd 5 --counter-fd 6 --producer-commit COMMIT
```

The caller connects all normalized records to the streaming oracle and retention
consumer. Default retention remains the oracle's 1/1024 main sample. The adapter
never creates full-record files. Software tests may explicitly retain full
normalized fixtures. External receiver/before observations and calibration remain
separate inputs; software RX and TX submission do not supply physical timestamps.

## Binary file capture and offline decode

The host can write the same native metadata to a compact binary regular file.
The consumer encodes records; one I/O thread writes ordered CRC frames from a
bounded pool. Packet bytes, adapter semantics, and
retention policy remain unchanged. The software scope and identity options still
apply. Nonblocking output below describes `--observe-fd`. When the I/O pool fills, file writes can
block the consumer, but producers never wait for it. Physical PMDs remain
unsupported by native capture.

```sh
dpds EAL_ARGUMENTS -- -c engine.json --observe-binary capture.hngb \
  --observe-software --observe-identity stage1
python3 -m tools.g2.native_binary capture.hngb --out native.jsonl
```

Both paths must be new. The host rejects a combination of `--observe-binary` and
`--observe-fd`. The decoder restores `hng-native-v2` JSONL, with the original
header, health, event values, and record order. Feed `native.jsonl` through a pipe
to the existing adapter. Its CLI descriptors still require pipes or sockets.
A valid incomplete capture exits decode with code `2`. A malformed capture cannot
produce a final health seal.

See [Native binary capture, version 1](docs/NATIVE-BINARY.md) for the wire format,
complete offline commands, integer widths, and failure behavior.

## Bounded transport and custody

The current v2 exporter allocates seven fixed-capacity queues: RX, scheduler
owner 0, scheduler owner 1, TX, control, RX owner 1, and TX owner 1. Capacity is
1..65536 records per producer, default 4096. Queue
storage is `7 * capacity * sizeof(struct observer_record)` bytes, reported by the
raw header's capacity and record width. Every queue has one producer and one
consumer. Only metadata is copied on the packet path; there is no allocation,
JSON serialization, file I/O, retry wait or consumer backpressure there.

A producer increments its own sequence before attempting to reserve a record.
Overflow increments an independent exact loss count even when no gap can be
emitted. A global reservation ordinal orders successfully reserved records.
Dispatch is reserved before the real enqueue and committed only after its
successful prefix is known. This keeps dispatch ahead of a concurrent TX consume
without blocking the DUT. Unsuccessful suffixes retain no successful handoff ID
and receive a terminal record when discarded.

Split schedulers use separate physical SPSC TX rings. The other layouts use one
physical handoff queue, which can contain both egress ports. The fused layouts
use a declared local queue with the same item ordinal and consumed-prefix rules.
TX records freeze consumed handoff prefixes before arbitration. They include
coherent, completed-step scheduler input/frontier publications read before the
actual gate checks. Each scheduler publishes through its own three-slot SPSC
mailbox with one TX reader. A shared TX reads both scheduler mailboxes; dedicated
TX owners read only their own scheduler's mailbox. Unrelated port frontiers
remain unknown in dedicated-owner snapshots. The writer, reader, and atomic middle slot have
distinct ownership; an acquire/release exchange transfers a completed slot.
A writer paused during its next copy leaves its last completed publication
readable. Neither path waits or retries, and no frontier or input prefix is
invented. Invalid snapshot port sets still latch malformed evidence.
Normalized frontiers are closed: a native lower bound W becomes `through_ns=W-1`.
Dispatch proof uses `through_ns+1+minimum_delay` for every line feeding the
selected egress. The trace minimum covers the whole registered profile, including
future delay reductions; unknown relevant frontiers still cannot certify output.
Each offered item records the actual
final whole-burst gate-check time for its line. The submit call has separate
before/after times and the actual accepted prefix, including zero and retries.

The consumer thread alone serializes metadata. It writes nonblocking output and
never waits for a slow reader. A failed or partial write permanently stops output
rows, preventing a partial JSON line from being followed by apparently valid
records. The thread blocks SIGPIPE locally. On shutdown, producers have stopped;
the consumer visits only the finite remaining queue records, then emits final
health when possible. An abort between reservation and publication counts the
unpublished record and any blocked suffix as exact capture losses; shutdown does
not wait for a missing global ordinal. Missing final health, truncation, native sequence gaps,
queue loss, identity errors or incomplete drain make normalized seals incomplete.
Capture failure does not become DUT packet loss. Fully observed DUT terminal
loss does not make capture incomplete. Failed ownership drain, a failed producer,
forced timeout or an explicit capture abort latches an `abort_reasons` bit mask in
final native health and makes normalized seals incomplete. Normal requested
SIGINT can still complete after all inputs and ownership drain.

Each normalized event preserves its actor in
`extensions.native_observer_v2={producer,producer_seq,order}` for v2 input.
Legacy v1 input retains `extensions.native_observer_v1`. Actor-local
snapshots can regress between different native producers in a combined host
stream. A native sequence need not be contiguous within a single normalized
role: RX also produces host terminal events. The adapter checks the complete
native sequence before separating logical roles.

Start/end counter records cover observer health, software PMD counters, pool
ownership and per-ingress software terminal conservation. Queue high-water is a
conservative sum of each producer's measured peak, not a simultaneous aggregate
occupancy. Logical ingress/host health scope IDs match their actual stream IDs, and emitted
counts equal the full logical data record count. Native per-producer diagnostics
preserve exact raw loss. When a lost native record cannot be assigned to a logical
role, affected logical counters are explicitly unavailable and capture remains
incomplete; zero is never substituted. Periodic snapshots and hardware counter
qualification are not provided.

## Adapter-private raw format

`hng-native-v1` and `hng-native-v2` are internal JSONL formats, normalized only by
`tools.g2.native_adapter`. Both headers contain `identity_policy`, `probe_port`,
`capacity`, and `record_bytes`. Version 1 describes the legacy five-producer
boundary. The current exporter emits version 2 with seven producers and explicit
`layout` and `ports`. Binary decode restores this same version 2 format. Event
fields and fixed-vector positions are defined
by `common/observer.h` and the adapter's `ROW_FIELDS` / `ITEM_FIELDS`; the public
interchange remains the standard versioned event protocol.

The final raw health row exposes each producer's attempted, emitted, queue-dropped,
consumer-dropped, high-water, identity-error and malformed-record counts. Native
sequence and health checks happen before clean normalized seals can be issued.
Normalized headers, canonical record hashes and seals use `StreamWriter`; actual
persisted file hashes belong to the downstream retention writer. The raw digest
returned by the adapter covers bytes consumed, including incomplete prefixes.
Raw VLAN/strip/error flags remain in packet extensions; unrepresentable profiles
are explicit incomplete evidence. An invalid packet whose terminal identity or
line cannot be represented by the current strict terminal schema emits a gap;
its pre-Q packet remains in the ingress stream.
