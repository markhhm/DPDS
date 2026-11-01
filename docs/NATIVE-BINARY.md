<!-- SPDX-License-Identifier: Apache-2.0 -->

# Native binary capture, version 1

Native binary capture stores the complete native observation stream in a regular
file. One consumer encodes records in global order. A separate I/O thread writes
CRC frames from a bounded pool of 2,048 × 64 KiB blocks (about 128 MiB). Producers
retain their bounded queues and do not wait for disk I/O. The encoder waits when
the I/O pool fills; sustained slow storage therefore causes explicit queue loss. The decoder restores
native JSONL for the adapter, oracle, and retention consumer.

Capture supports PCAP, ring, and null software PMDs. It requires explicit software
scope and an identity policy. Physical PMD support and hardware qualification
remain separate work. Binary capture does not establish a packet-rate guarantee.

## Capture and offline conversion

Run the host with a new capture path after EAL's `--` separator:

```sh
dpds EAL_ARGUMENTS -- -c engine.json --observe-binary capture.hngb \
  --observe-software --observe-identity stage1
```

The `stage1` policy requires the probe profile in
[the observation boundary](../NATIVE-OBSERVER.md). Software fixtures can use
`--observe-identity software-l2`. Queue capacity and probe port retain their
options, `--observe-capacity` and `--observe-probe-port`.

The host creates the file exclusively with mode `0600`, subject to the process
umask. A file or symlink at that path causes an error. `--observe-binary` and
`--observe-fd` are mutually exclusive. The JSON output option still requires an
inherited pipe or socket.

After the host exits, decode the capture to a new file:

```sh
python3 -m tools.g2.native_binary capture.hngb --out native.jsonl
```

The decoder refuses an output path that already exists. Exit code `0` means that the binary
stream has a valid, complete native health seal. Exit code `2` means an incomplete
capture, malformed input, or an I/O error. An argument error also exits with code
`2`. A failed decode can leave a partial JSONL file without a final health seal.
Do not treat that partial file as complete evidence.

Large sampled M6 reports can instead read this binary file directly, using
bounded NumPy columns and raw directional pcapng inputs. They do not need the
JSONL decode/adapter prepass. Register `verification.native_binary` and
`verification.capture_pairs` as described in the
[M6 runner](M6-RUNNER.md#direct-binary-input-for-sampled-runs).
The direct reader validates every frame, tagged value, producer sequence and
health seal before a complete result. Sampling does not exempt unselected
records from wire/actor/profile checks. Full custody remains diagnostic under
the sampled policy; independent full-capture seqcheck gates remain mandatory.

Connect inherited descriptors 4, 5, and 6 to the oracle and retention
consumer. Then pass decoded input through a pipe:

```sh
cat native.jsonl | python3 -m tools.g2.native_adapter engine.json demo \
  --input-fd 0 --ingress-fd 4 --host-fd 5 --counter-fd 6 \
  --producer-commit COMMIT
```

All adapter descriptors remain pipes or sockets. The adapter retains its native
semantic checks and normalized seals. A complete binary seal alone does not
certify those downstream checks or physical timing.

## File and frame layout

The file starts with eight magic bytes, ASCII `HNGBIN1\n`:

```text
48 4e 47 42 49 4e 31 0a
```

The magic identifies version 1. All fixed-width integers use little-endian byte
order. Each subsequent frame has a 16-byte header, a nonempty payload, and a
four-byte checksum. A whole frame is at most 65,536 bytes.

| Header offset | Size | Type | Meaning |
| --- | --- | --- | --- |
| 0 | 1 | u8 | Frame type: 1, 2, or 3 |
| 1 | 1 | u8 | Flags, always zero |
| 2 | 2 | u16 | Reserved, always zero |
| 4 | 4 | u32 | Payload length, 1..65,516 bytes |
| 8 | 8 | u64 | Frame sequence, consecutive from zero |

The Python header format is `<BBHIQ`. The checksum follows the payload as a
little-endian u32. It is CRC-32/IEEE over the header and payload together. Its
value equals `binascii.crc32(frame_header + payload)`. The C writer uses DPDK
`RTE_NET_CRC32_ETH` with the same result.

| Frame type | Payload | Position |
| --- | --- | --- |
| 1 | Raw native header JSONL | First frame, exactly once |
| 2 | One or more complete event entries | After the header and before health |
| 3 | Raw native health JSONL | Final frame, exactly once |

Header and health payloads contain the original UTF-8 JSONL bytes, with the
final LF. The decoder preserves these bytes. Each payload contains exactly one
JSON line. No bytes can follow the health frame. An empty capture has a header
frame and a health frame, with no event frame.

The header schema is `hng-native-v2`. Its fields are `schema`, `identity_policy`,
`probe_port`, `capacity`, `record_bytes`, `layout`, and `ports`.

Identity policy is
0 for `stage1` and 1 for `software-l2`. Probe port is 1..65,535. Capacity and
`record_bytes` are 1..65,536. Layout is `shared` or `per-direction`. Ports form a
unique list of one or two u16 values. The per-direction layout requires two ports.

`record_bytes` reports `sizeof(struct observer_record)`, currently 1,632 bytes in
the host build. It describes the C queue record, not an encoded entry. Entry
length varies with the values, snapshots, and packet count. Read the actual
header value for queue memory calculations.

## Tagged integers

A value from 0 through 127 occupies one literal byte. A larger value starts with
`0x80 | (N - 1)`, then contains `N` little-endian data bytes. `N` is the minimum
byte width and lies in 1..8. The total encoded width is therefore 1..9 bytes.

| Value | Encoded hexadecimal bytes |
| --- | --- |
| 0 | `00` |
| 127 | `7f` |
| 128 | `80 80` |
| 255 | `80 ff` |
| 256 | `81 00 01` |
| 16,383 | `81 ff 3f` |
| 2^53 | `86 00 00 00 00 00 00 20` |
| 2^64 - 1 | `87 ff ff ff ff ff ff ff ff` |

Tags `0x88` through `0xff` are invalid. A tagged value below 128 is invalid. For
`N > 1`, the last data byte must be nonzero. Each field also retains the unsigned
width in the tables below. The decoder rejects an out-of-range value.

## Event entries

Each entry starts with a little-endian u16 length. This length covers the entry
body and excludes the two-byte length itself. A frame contains whole entries.
An entry cannot continue into another frame.

The entry body has these fields in order:

| Field | Encoding | Bounds |
| --- | --- | --- |
| `producer` | u8 | 0..6 |
| `kind` | u8 | 0..7 |
| Packet count | u8 | 0..16 |
| `reason` | u8 | 0..255 |
| `producer_seq` | Tagged integer | u64, positive |
| `order` | Tagged integer | u64 |
| Common presence mask | u32 | Only bits 0..21 can be set |
| Present common values | Tagged integers, ascending bit order | See common fields |
| Snapshot count | u8 | 0..2 |
| Snapshots | Repeated snapshot entry | See below |
| Packet items | Packet count repeated entries | See packet fields |

Producer IDs follow the native actor order:

| ID | Actor |
| --- | --- |
| 0 | RX, or RX owner 0 in the per-direction layout |
| 1 | Scheduler owner 0 |
| 2 | Scheduler owner 1 |
| 3 | TX, or TX owner 0 in the per-direction layout |
| 4 | Control |
| 5 | RX owner 1 |
| 6 | TX owner 1 |

Kinds 0..7 are packet, offer, terminal, frontier, end, dispatch, attempt, and
resources. The adapter checks which actors can attest each kind and reason.

Each snapshot contains `port` as a tagged u16, then `seen` and `lower_bound` as tagged u64 values.
The final byte, `ended`, is 0 or 1.

For each presence mask, an unset bit supplies exactly zero, or false for a
boolean field. A set bit supplies a nonzero tagged value. Unknown mask bits and
explicit zero values for set bits are invalid. Packet masks follow the same rules
as the common mask.

### Common fields

| Bit | Field | Original unsigned type |
| --- | --- | --- |
| 0 | `at_ns` | u64 |
| 1 | `gate_ns` | u64 |
| 2 | `submit_before_ns` | u64 |
| 3 | `submit_after_ns` | u64 |
| 4..13 | `resources[0]` through `resources[9]` | u64 each |
| 14 | `actual_selection_seq` | u64 |
| 15 | `attempt_id` | u64 |
| 16 | `consumed_dispatch[0]` | u64 |
| 17 | `consumed_dispatch[1]` | u64 |
| 18 | `first_handoff_seq` | u64 |
| 19 | `handoff_queue` | u16 |
| 20 | `port` | u16 |
| 21 | `accepted_count` | u16 |

### Packet fields

Each packet starts with its own u32 presence mask and present tagged values.
The bit order also defines the native JSON packet array order. It does not follow
C struct memory order.

| Bit | Field | Original unsigned type |
| --- | --- | --- |
| 0 | `identity` | u64 |
| 1 | `ingress_seq` | u64 |
| 2 | `selection_seq` | u64 |
| 3 | `arrival_ns` | u64 |
| 4 | `handoff_seq` | u64 |
| 5 | `gate_ns` | u64 |
| 6 | `source` | u32 |
| 7 | `destination` | u32 |
| 8 | `length` | u32 |
| 9 | `source_port` | u16 |
| 10 | `destination_port` | u16 |
| 11 | `ingress_port` | u16 |
| 12 | `line` | u16 |
| 13 | `vlan_id` | u16 |
| 14 | `segments` | u16 |
| 15 | `handoff_queue` | u16 |
| 16 | `identity_kind` | u8 |
| 17 | `selected_late` | u8 |
| 18 | `input_error` | u8 |
| 19 | `classified` | Boolean, 0 or 1 |
| 20 | `stripped` | Boolean, 0 or 1 |
| 21 | `rx_flags` | u64 |

The decoder preserves every representable observation. This includes semantically
invalid observations. It does not replace adapter checks with narrower packet
policy rules. Packet positions 19 and 20 become JSON booleans. Snapshots become
`[port, seen, lower_bound, ended]`, with a JSON boolean at position 3.

The decoder emits compact event JSON, followed by LF, with this key order:

```text
producer, producer_seq, order, kind, at_ns, gate_ns, submit_before_ns,
submit_after_ns, actual_selection_seq, attempt_id, consumed_dispatch,
first_handoff_seq, handoff_queue, port, accepted_count, reason,
snapshot, resources, items
```

## Health and failure behavior

Event `order` values must be consecutive from zero. Each producer sequence must
strictly increase. Declared native loss must account for sequence gaps.
The decoder does not change sequences or remove records to repair a capture.

The final health object contains exactly `kind`, `complete`, `abort_reasons`, and
`producers`. Its kind is `health`. Its complete flag is a JSON boolean. Its abort
mask is 0..15. The producer list has seven entries in producer ID order. Each
entry contains these u64 counters:

```text
attempted, emitted, dropped, capture_drops, high_water,
identity_errors, malformed_records
```

For every producer, `emitted` must equal the decoded event count.
`attempted` must equal `emitted + dropped + capture_drops` and must cover the last
producer sequence. `high_water` cannot exceed the header capacity. Any abort,
drop, capture loss, identity error, or malformed-record count prevents a complete
seal. A valid incomplete seal remains byte-exact in decoded output. The CLI exits
with code `2` for that capture.

The C consumer flushes event frames before it creates final health. A failed write
permanently stops output. A failed buffered write transfers its buffered emitted
counts to capture-loss counts. The writer does not append a success seal after
an output failure.

Unknown magic, frame types, flags, reserved bits, metadata fields, or schema
versions cause an error. So do bad checksums, bad lengths, malformed integers,
sequence errors, inconsistent health, truncation, missing health, and bytes after
health. The decoder raises `StreamError` with status `INCONCLUSIVE` for malformed
input. It writes health only after the final EOF check. Earlier decoded records
can remain in the destination, but they have no final health seal.

## Rate validation and storage requirements

Run the software C-path check with five pipeline lcores, a separate encoder CPU,
and a separate synthetic traffic generator CPU. The I/O thread shares the control
CPU. Choose distinct available CPUs; CPU identifiers below are illustrative.
The seven CPU assignments include the generator, which is outside the DUT.

```sh
python3 perf/observer_rate.py build/hng-ring-rate rate-check \
  --cpus 0,1,2,3,4,5,6 --seconds 60 --discard
```

This sends at least 0.82 Mpps per direction for 60 seconds through the SPSC
queues, C encoder, CRC frames, and write calls to `/dev/null`. It requires zero
DUT/native loss, every queue peak below capacity, and zero pending I/O blocks at
close. It retains no native evidence and does not measure disk bandwidth.
The production `dpds --observe-binary` path still requires a new regular file;
it cannot select the discard sink. File round-trip and partial-write tests check
the actual storage path independently. Neither this check nor software PMDs
establish physical timing or hardware qualification.

For physical capture, provision a **local NVMe sustaining at least 500 MB/s**
for the native spool alone. A software capture at 0.85 Mpps per direction produced
about 407 MB/s of native binary data; 500 MB/s leaves headroom. The per-packet size
varies with bursts and metadata. At that measured rate, budget about 24.4 GB
(decimal) for a 60-second cell, plus drain and file-system overhead. At the
500 MB/s provisioning rate, reserve at least 30 GB for that native spool.
Add each cell's external pcapng files separately; their capture I/O must not
consume the native spool's guaranteed bandwidth. Compute capacity from the
registered cell duration and retain the full input spool until its offline
report and retention result are verified. Raw files are not deleted automatically.

WSL virtual-disk speed does not qualify physical storage. A `/dev/null` PASS
must be reported as C-path throughput only. Do not substitute a memory-backed
file system for this disk requirement.

The 60-second software precheck passed at 849,999 pps in each direction, with
101,999,920 transmitted frames, zero DUT/native loss, maximum native queue
watermark 13,547/65,536, and I/O-pool watermark 47/2,048. Every producer and the
I/O pool drained. Maximum process RSS was about 877 MiB. This result used five
pipeline lcores plus the encoder and external generator assignments described
above, with a discard sink. A different CPU assignment had one short overflow;
CPU scheduling and storage headroom must still be checked per physical run.
