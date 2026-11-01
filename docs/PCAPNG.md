<!-- SPDX-License-Identifier: Apache-2.0 -->
# Same-clock pcapng observations

Convert a closed ingress/egress capture pair and run the existing G2 reporter:

```sh
prlimit --as=4294967296 -- python3 -m tools.g2.pcapng examples/pcapng-pair.json --out reports/capture-01
```

Replace the example paths and numeric values with the registered run's values.
Paths are relative to the pair JSON. The output directory must be new. Inputs
are read only. No capture process, interface, NIC, or hardware setting is changed.

`manifest` supplies the existing native ingress, host, reference and counter
streams, configuration, calibration, and required rows. Exactly one before,
receiver, and counter stream must be registered. The adapter replaces those
observations and their observer-health counters. It verifies the original
counter file's registered SHA before rewriting it. Other native counters and
source hashes remain part of the reporter's checks.

`before.interfaces` maps pcapng interface IDs to physical ingress port IDs;
`receiver.interfaces` maps them to physical egress port IDs. Each mapping is
explicit and unique. Registered port/VLAN pairs identify lines. The packet
profile is the existing single-VLAN Ethernet/IPv4/UDP Stage 1 probe, including
its 64-bit sequence and selected-late payload flag. No flow identity is guessed
from a NIC name or from timing. Other identity profiles are outside this adapter.

## Clock and capture-health requirements

Both files must use the same declared `clock_id` and `mapping_id`, an explicit
nanosecond timestamp resolution, and a registered identity mapping into the
manifest's observation clock. `uncertainty_ns` is a supplied bound, not something
estimated from the pcapng resolution. The example value is illustrative.
Calibration is supplied and validated; the adapter does not create hardware
calibration or promote `g2_qualified`.

`start_ns` and `end_ns` are the declared capture-clock observation interval.
The calibration must also contain engine-to-observation anchors covering the
native start/end counters and every registered phase. These boundaries are
mapped with the existing exact calibration arithmetic and expanded by mapping
residual plus capture uncertainty. The declared interval and both captures must
cover that whole measurement/drain interval. Missing anchors, extrapolation,
or an early end remain INCONCLUSIVE, including when the last expected delivery
falls outside the recording. An anchor may carry the engine epoch offset;
the adapter never assumes that engine and capture epochs are equal.
The capture mapping must remain valid over this whole interval, including an
empty drain after the last recorded packet.

The supported pcapng profile follows the
[IETF pcapng draft](https://www.ietf.org/ietf-ftp/internet-drafts/draft-ietf-opsawg-pcapng-06.html):
one version 1.0 section, either byte order, Ethernet Interface Description Blocks
with `if_tsresol=9`, and full Enhanced Packet Blocks. Signed `if_tsoffset` is
applied with integer arithmetic. Per-interface capture order is retained;
timestamps are never sorted or repaired. Simple/obsolete packet blocks, retained
FCS, fragmented/non-probe packets, unknown interfaces, invalid options, and
blocks larger than 1 MiB remain unsupported/inconclusive.

Each mapped interface needs final Interface Statistics after its last packet:
`isb_starttime`, `isb_endtime`, `isb_ifdrop`, `isb_osdrop`, and `isb_usrdeliv`.
The time bounds must cover the declared interval and captured packets; delivered
count must match the file's packet count. Drop counts must be known and zero.
Positive EPB/ISB drop counts, counter resets, timestamp regressions, missing
statistics, packet truncation, and file truncation make the corresponding
sealed stream incomplete. Unknown loss counts are retained as null, not zero.
This also detects truncation at a complete packet-block boundary before final
statistics. A capture tool that omits this health evidence stays INCONCLUSIVE.

With complete healthy capture evidence, a missing expected delivery is left to
the existing reporter's loss gate. It can be FAIL; it is not relabelled as a
capture failure.

## Output and limits

The adapter supplies synthetic queue counters for the converted before/receiver
streams: capacity is 1, and high-water is 1 for a nonempty capture.
These counters cannot prove physical queue headroom. The reporter's
[`observer_cost` assessment](../tools/g2/README.md#observer-cost-assessment)
therefore warns and keeps such cells INCONCLUSIVE, even with complete healthy
pcapng files. Capture health and gate diagnostics remain in the report.
Use native physical observer evidence for the bring-up cost pilot.

`bundle/` holds before/receiver/counter streams and an updated manifest.
Its `source` symlink references the original native bundle, so keep that source
directory available when rerunning or archiving. `conversion.json` retains
capture health, full-file hashes when successfully consumed, observed drop
counts, and original manifest/counter hashes. `matrix.json` feeds the existing
sequential runner; `reports/<run>/` contains its standard report, CSV, summary,
and small verdict. Exit codes remain 0 PASS, 1 FAIL, and 2 invalid/inconclusive.
Interrupted or failed conversions have no valid final summary.

Parsing and counter rewriting are streamed; metadata is retained only for
registered interfaces. The existing reporter runs in a fresh process. Run one
large conversion at a time under the inherited memory limit.

Synthetic tests cover exact epochs above 2^53, signed offsets, both byte orders,
capture loss versus true DUT loss, truncated files, time regressions, early
capture termination, unknown health, source-hash mismatches, and input/output
alias protection. No physical NIC, capture driver, or clock accuracy is verified.
