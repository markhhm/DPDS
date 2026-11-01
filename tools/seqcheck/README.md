<!-- SPDX-License-Identifier: Apache-2.0 -->
# Full capture sequence checker

`seqcheck` compares every probe identity in a closed ingress/receiver pcapng
pair. It is a standalone C11 program, links only libc, pthreads and OpenSSL libcrypto,
and never calls or links the packet engine or its reference model.

```sh
cc -std=c11 -O3 -Wall -Wextra -pthread tools/seqcheck/seqcheck.c -lcrypto -o seqcheck
./seqcheck --ingress ingress.pcapng --receiver receiver.pcapng --details 20 > sequence.json
```

Install the C compiler and OpenSSL development headers (`libssl-dev` on Debian
and Ubuntu). The host Meson build also builds `seqcheck` and runs its tests.
The standalone command needs neither DPDK nor Rust. Inputs are read only;
JSON is written to stdout. Exit 0 means PASS, 1 means observed differences,
and 2 means INVALID (or invalid CLI usage, reported on stderr).

## Frozen reporter contract: hng-seqcheck-v1

[contract-example.json](contract-example.json) is a real synthetic loss result.
The reporter consumes these fields, without interpreting every checker FAIL
as a storage FAIL:

- `schema`, `status` (`PASS`, `FAIL`, `INVALID`), `complete` and
  `scope="supplied-captures"`.
- `inputs.ingress` and `inputs.receiver`: `sha256` of the entire file,
  `byte_size`, parsed EPB `packets`, `health.complete`, `health.capture_drops`.
  Unknown health/drop counts are false/null, never successful zero evidence.
  `path` and `error` are diagnostic. Valid UTF-8 names are preserved; invalid
  filename bytes use Unicode byte escapes, so diagnostic paths must not replace
  registered source paths. Unreadable files have a null digest.
- `flows[]`: unique `(flow_id, vlan_id)`; `flow_id` uses the same
  `src_hex:dst_hex:sport:dport` spelling as `tools/g2/probe.py`.
  All counters also occur in `totals`, which is their sum over flows.
- Counters: `ingress`, `receiver`, `ordinary_reorder`, `missing`,
  `duplicate_ingress`, `duplicate_receiver`, `unexpected`, `flag_mismatch`.
  Counts are nonnegative uint64 JSON integers. Consumers must retain integer
  precision, including sequences above 2^53.
- `details_limit`: N (default 20, range 0..100000), **per category, globally**.
  `details` and `details_truncated` have all six anomaly categories. Details
  include `flow_id`, `vlan_id`, `sequence`, and when known `ingress_rank` and
  `receiver_index`. Both ordinals are zero based packet positions in their
  respective complete capture, not numeric sequence values or per-flow counts.
  Missing details select the earliest N ingress ordinals; other categories
  retain their first N occurrences. Truncating details never truncates counts.

`complete=true` means both supplied files are healthy, comparison finished,
and ingress identities are unambiguous. Content FAIL can still be complete.
Malformed/unsupported capture, unknown health, capture drop, changing file,
allocation failure or duplicate ingress identity makes the result INVALID
and incomplete. Partial counters/details in INVALID output are diagnostic.
A parsed EPB rejected before identity extraction may appear in input packet
counts without appearing in per-flow counters.

### Identity, order and loss

The identity is `(VLAN, IPv4 src/dst, UDP src/dst port, uint64 sequence)`.
The single-VLAN IPv4/UDP probe's payload begins with network-order `!QIB`:
sequence, magic `0x4c30504b`, and selected-late flag 0/1. Sequence need not be
contiguous, start at zero or increase numerically. Interface IDs identify
capture descriptions; they do not extend the flow key. Register ambiguous
physical/directional pairs separately.

Ingress capture order supplies the expected per-flow relative order.
For each first matching ordinary receiver identity, compare its ingress
ordinal with that flow's ordinary high-water ordinal. An ordinal below the
high-water counts one `ordinary_reorder`. This counts out-of-order packets,
not pairwise inversions or the number of packets displaced by a swap.
Selected-late packets are excluded from this ordering and never advance its
high-water. Late selection comes from ingress; changing the receiver flag
cannot hide reorder and separately increments `flag_mismatch`.

`missing` counts **unique ingress identities absent at receiver**, including
late identities. It never infers loss from numeric sequence gaps. Receiver
counts include duplicates and unexpected identities. Every occurrence after
the first receiver identity increments `duplicate_receiver`, including a
repeated unexpected identity; every unknown occurrence increments `unexpected`.
Flag mismatches count occurrences with a matching ingress identity. Duplicate
receiver occurrences do not add order events. Duplicate ingress makes the
reference ambiguous and INVALID; its first identity is retained diagnostically.

Missing may include intentional queue/trace loss. The reporter must bind this
JSON and both input digests/sizes to its registration, then reconcile missing
with certified intentional-drop counters and sampled independent decisions.
This tool does not verify Q decisions, latency, clock calibration, engine
counters, drain coverage outside supplied captures, or phase-specific coverage.
An unhealthy capture is not evidence of DUT storage loss.

### Supported capture health

One pcapng 1.0 section in either byte order; Ethernet IDBs with explicit
`if_tsresol=9`, no retained FCS, and EPBs containing at least the full identity
prefix. Minimum caplen is 59 bytes without IPv4 options, up to 99 with options;
128 covers both. Captured length can be smaller than original wire length.
IPv4/UDP lengths must fit wire length and identity headers must fit caplen.
Non-probes, other link/protocol profiles and fragmented IPv4 are rejected.

Every interface needs final ISB statistics after its last packet:
start/end, ifdrop, osdrop and usrdeliv; delivered count must match EPBs,
time bounds must cover captured packets, and known drop counts must be zero.
EPB drop options, timestamp regressions and statistics resets are checked.
Raw per-interface timestamps and ISB bounds use the same IDB offset, so order
and coverage comparisons do not require floating-point time conversion.
This certifies the supplied capture, not an external experiment/drain interval.
Simple/obsolete packet blocks, multiple sections and blocks over 1 MiB are
unsupported. Block sizes/trailers/options and complete EOF are validated.
The input file's size/mtime/ctime must remain unchanged while it is read.

## Cost and tests

Identity lookup uses a hash table of flows and sparse sequence pages. Each
4096-sequence page holds ingress/late/received bitmaps and uint64 arrival
ordinals (34312 bytes plus the page table). Ordinary checks are constant work
per packet on average; there is no repeated scan over all active identities.
Final missing enumeration is linear in present identities plus allocated
pages. Dense sequential sequences cost about 8.38 bytes/identity before flow
and allocator overhead; highly sparse sequences allocate more pages and may
reach the process memory limit. An allocation failure returns INVALID, never a
partial PASS. This checker retains identities for the full supplied run.

```sh
SEQCHECK_BINARY=./seqcheck python3 -m unittest -v tools.seqcheck.test_seqcheck
prlimit --as=4294967296 -- python3 -m tools.seqcheck.benchmark \
  --binary ./seqcheck --out benchmark-output --packets 2000000 --caplen 59
```

`benchmark.py` streams synthetic input generation and runs the checker serially.
The checker uses one comparison thread and up to two SHA-256 reader threads;
hashes run concurrently, but identity comparison is sequential ingress then receiver.
Runs are serial, and inputs must remain immutable until all readers finish.
Its timings include process startup, both file scans and SHA-256, health/order/
loss checks, JSON output and cleanup. It reports **both observed EPBs/s (2N/t)**
and **ingress/receiver identity pairs/s (N/t)**. Warm page cache, synthetic
in-order probes and no NIC are explicit; the files are immutable hardlinks.
The adopted target is >=4.9 M complete pairs/s at the median, derived from
three times the bidirectional 10G MTU pair rate. Do not call 2N/t a pair rate. See
[benchmark-results.md](benchmark-results.md) for measured results and limits.
