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

## Bounded transport and custody

Setup allocates five fixed-capacity queues: RX, scheduler owner 0, scheduler owner
1, TX and control. Capacity is 1..65536 records per producer, default 4096. Queue
storage is `5 * capacity * sizeof(struct observer_record)` bytes, reported by the
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
actual gate checks. A busy or unstable publication latches malformed evidence;
it never becomes an invented frontier. Each offered item records the actual
final whole-burst gate-check time for its line. The submit call has separate
before/after times and the actual accepted prefix, including zero and retries.

The consumer thread alone serializes metadata. It writes nonblocking output and
never waits for a slow reader. A failed or partial write permanently stops output
rows, preventing a partial JSON line from being followed by apparently valid
records. The thread blocks SIGPIPE locally. On shutdown, producers have stopped;
the consumer visits only the finite remaining queue records, then emits final
health when possible. Missing final health, truncation, native sequence gaps,
queue loss, identity errors or incomplete drain make normalized seals incomplete.
Capture failure does not become DUT packet loss.

Start/end counter records cover observer health, software PMD counters, pool
ownership and per-ingress software terminal conservation. Queue high-water is a
conservative sum of each producer's measured peak, not a simultaneous aggregate
occupancy. Periodic snapshots and hardware counter qualification are not provided.

## Adapter-private raw format

`hng-native-v1` is an internal JSONL pipe format, normalized only by
`tools.g2.native_adapter`. Its header contains `identity_policy`, `probe_port`,
`capacity` and `record_bytes`. Event fields and fixed-vector positions are defined
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
