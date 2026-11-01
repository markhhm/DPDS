<!-- SPDX-License-Identifier: Apache-2.0 -->
# M6 sequential reporter

Run registered captures one at a time through the existing G2 bundle reporter:

```sh
python3 -m tools.g2.matrix examples/m6-short.json --out reports/m6-01
```

Replace the example's manifest paths with sealed `hng-g2-manifest-v1` captures.
Paths are relative to the matrix file. Each entry has a unique `id` equal to
the manifest's `run`, a `manifest` path, and an optional `family` (`main`,
`boundary`, or `trace`). Duplicate IDs or resolved manifest paths are rejected
before execution. Use a new output directory or resume one created by this
runner for the same matrix registration. Run/scope mismatches invalidate that run. A missing or invalid run
remains in the summary and does not suppress later runs.
IDs that collide with `summary.json` or another run's stderr filename are rejected.

Each run uses a fresh process and produces the unchanged G2 `report.json`,
`results.csv`, and `summary.txt`, plus a small `verdict.json`. The controller
reads the verdict and writes `summary.json`. Resume validation streams file hashes
without loading full captures into memory. Stderr is retained as `<id>.stderr.log`. A reporter process
failure or incomplete set of artifacts becomes INCONCLUSIVE. Any measured FAIL
makes the matrix FAIL; otherwise every run must PASS. Exit codes are 0 for PASS,
1 for FAIL, and 2 for invalid/inconclusive evidence or matrix input errors.
An interrupt stops execution; partial output must not be treated as a final
summary. After the controller and its worker have stopped, repeat the same
command with the same output directory.

## Resume an interrupted matrix

The runner records the matrix registration in `.matrix.json` and finishes each
run with an atomic `.complete.json` receipt. It skips a run only when its receipt,
input hashes, output hashes and verdict agree. Completed PASS, FAIL and
INCONCLUSIVE reports retain their original verdicts; resume does not turn a
measurement failure into a retry. Input checks include configuration, calibration,
traces and sealed streams. Pcapng mode also checks the pair registration and both
raw capture files, and retains the converted bundle when it is unchanged.

Incomplete runs, changed inputs or changed outputs cause that run's directory
and stderr log to move under `.incomplete/<unique-id>/` before a fresh worker
reruns the cell. Existing evidence is retained. The old matrix summary also
moves aside when execution restarts. A new `summary.json` is published atomically
only after every registered run has a verdict, including reused runs.

Use one controller/worker group per output directory. Stop the old worker before
resuming after a forced controller kill. A different matrix registration or an
unrecognized existing directory is rejected without replacing its contents.
Directories from older runner versions lack the registration/receipts; use a
fresh output directory for those results or after changing reporter code.

The runner preserves every required row and repeat verdict. It does not pool
repeats, change sampling, or grant physical qualification. The current G2
reporter's `g2_qualified=false` is preserved, including for software PASS.

## Registered pcapng capture mode

One optional configuration field selects the input path: `mode` is `software`
(the default) or `pcapng`. Omitted/default software mode continues to read sealed
G2 run manifests directly. The CLI stays the same:

```sh
python3 -m tools.g2.matrix examples/m6-pcapng.json --out reports/m6-captures-01
```

In `pcapng` mode, each run's `manifest` points to a registered
[capture-pair manifest](PCAPNG.md), containing that cell's native G2 manifest,
ingress/egress pcapng paths, interface-to-port maps and clock/coverage metadata.
Its native manifest's run and scope must match the matrix entry before
conversion. Pair paths are relative to the matrix; capture/native paths are
relative to their own pair manifest. No capture or hardware operation is started.

Each fresh per-run process converts with the existing pcapng adapter under
`<id>/capture/`, then evaluates the converted streams with the existing reporter.
It finishes that cell before starting the next. Reports, CSVs, small verdicts,
stderr logs and the combined `summary.json` keep their existing paths and format.
Capture loss remains INCONCLUSIVE and does not suppress later cells. A measured
FAIL retains the same aggregate precedence. Conversion failures remain invalid
or inconclusive evidence, never PASS.

Mode does not set `scope` or grant hardware qualification. Synthetic pcapng tests
use `scope=software-only`; physical capture manifests require their supplied
physical calibration. This mode retains all adapter profile, calibration,
capture-health and source-directory requirements described in its documentation.

## G short-plan example

The example records planning metadata: main 72 conditions × 3 repeats,
boundary 288 × 1, and trace 60 × 2. Fixed acquisition uses 2 s warmup,
70 s exposure, 1 s drain, and 5 s reset (78 s total). Trace episodes use
11 s (2 s warmup + 9 s steady), then 1 s drain and 5 s reset (17 s total);
10 episodes are typical and 20 are the maximum. Physical timing sampling remains
1/1024 (`mask_bits=10`), with at least 368 paired samples. The 431-pair value
is a planning estimate; existing registration and measured-sample gates remain
authoritative.

`plan` is copied as metadata. The runner does not acquire traffic, wait those
durations, schedule resets, or validate a complete experiment inventory.
The three entries are representatives, not the full M6 matrix. Register each
actual repeat and trace episode with its own run and evidence before reporting.
Hardware clocks, calibration under load, trace provenance, complete matrix
registration, physical execution, and acceptance remain separate work.

## Hardware-day runbook

Complete [BRING-UP](BRING-UP.md) first. Unresolved hardware decisions block this procedure.
Export its private deployment variables, source revisions and calibration path.
Also set `M6_MATRIX`, `REPORT_DIR`, `PILOT_MANIFEST`, `PILOT_MATRIX`, `PILOT_REPORT_DIR` and `RUN_MANIFEST`.

The `*_COMMAND` variables contain the operator's deployment commands, not repository tools.
Register their traffic, capture, drain, reset and restoration parameters first.
Capture commands must retain native evidence and closed pairs per [PCAPNG](PCAPNG.md). The matrix runner does not acquire traffic.

Use native physical observer evidence for the pilot: `PILOT_MATRIX` uses `scope=physical`, `mode=software` and native manifest paths.
Here `software` selects native input, not software scope. Pcapng adapter queue counters are synthetic and cannot prove physical queue headroom.
On failure, stop traffic, drain and restore the allocated resources per [Handoff](BRING-UP.md#handoff). Keep all evidence.

| Step | Commands from `DPDS_SRC` | Expected output and stop condition |
|---|---|---|
| 1. Bypass and clocks | `sh -c "$BYPASS_COMMAND"`<br>`sh -c "$CALIBRATION_COMMAND"` | Keep identities/counters and `CALIBRATION_FILE`. Require target delivery, zero non-model loss and physical pair uncertainty ≤10,000 ns. Otherwise stop here. |
| 2. Observer cost pilot | `python3 -m tools.g2.validate "$PILOT_MANIFEST"`<br>`OFFERED_BPS=10000000000 sh -c "$PILOT_CAPTURE_COMMAND"`<br>`prlimit --as=4294967296 -- python3 -m tools.g2.matrix "$PILOT_MATRIX" --out "$PILOT_REPORT_DIR"` | Register the pilot with required-cell `mask_bits=8`. Require preflight PASS before capture. Require zero observer drops, positive queue capacity, high-water below capacity and no `observer_cost` warnings or missing evidence. Record `OBSERVER_COST_REPORT`, `EVIDENCE_COMMIT` and `RUN_ID` per the [cost gate](BRING-UP.md#observer-cost-gate-before-m6). On failure, retain the previous plan and stop for review. |
| 3. Register and capture M6 | `cp -n examples/m6-short.json "$M6_MATRIX"`<br>`python3 -m tools.g2.validate "$RUN_MANIFEST"`<br>`sh -c "$M6_CAPTURE_COMMAND"` | Edit the private matrix before traffic: `mode=pcapng`, `scope=physical`, every repeat/episode and its capture-pair path. Expand the three example entries. After step 2 passes, register fixed cells at 1/256 and 30 s exposure. Align fixed-cell plan metadata. Keep the v1 header, sample gates and trace plan. Require preflight PASS for every native run manifest before capture. Require closed pairs, native seals and capture-health evidence. On rejection or incomplete capture, stop here. |
| 4. Release the testbed | `sh -c "$POST_BYPASS_COMMAND"`<br>`sh -c "$RESTORE_COMMAND"` | Require the post-run bypass check, drain and restoration records. On failure, record the reason and stop here. Preserve source directories for adapter symlinks. |
| 5. Report offline | `prlimit --as=4294967296 -- python3 -m tools.g2.matrix "$M6_MATRIX" --out "$REPORT_DIR"` | Run after capture, away from the testbed. Expect per-run `report.json`, `results.csv`, `verdict.json` and final `summary.json`. On interruption, stop controller and worker, then repeat this exact command/directory. [Resume](#resume-an-interrupted-matrix) skips verified sealed reports and preserves partial attempts. It resumes reports, not capture. |
| 6. Read the aggregate | `cat "$REPORT_DIR/summary.json"` | Read `status` and `measurement_status`. Require all registered run IDs, repeats and required rows. Step 5 reporter exits: 0 all PASS, 1 FAIL, 2 invalid/inconclusive evidence. The `cat` exit is not a verdict. Keep verdicts and stderr logs. If the summary is absent, stop here and resume step 5. |

Freeze the matrix and source paths before step 5. Preserve `g2_qualified=false` and the [software limits](../tools/g2/README.md).
A reporter PASS does not qualify hardware.

## Three-cell software smoke

```sh
meson test -C build m6-smoke --print-errorlogs
```

This test acquires three real bidirectional PCAP PMD runs using the production
five-lcore application: fixed delay, two-point delay, and a synthetic trace.
Each run has 400 frames per direction. It checks actual accepted identities,
complete native observer capture, shutdown conservation, and every output byte
against the input, then invokes the matrix CLI. The independent Fraction
reference constructs expectations from actual ingress observations.

For bounded CI fixtures, delays are scaled to microseconds, frames are fully
sampled, and acquisition is short. These are not G exposure or load measurements.
Before/receiver timestamp pairs are same-engine software fixtures derived from
actual ingress and accepted TX submission times. They are not independently
calibrated receiver timestamps. Reporter timing verdicts, including FAIL or
INCONCLUSIVE, are preserved and must never qualify hardware. Separate CLI tests
exercise conclusive PASS, FAIL, and INCONCLUSIVE through the actual reporter.
Current software captures can report `unsafe_dispatch_frontier`; the smoke
checks orchestration and packet integrity, not a PASS of that timing gate.

The smoke and reporter runs are sequential. On a memory-constrained Linux
worker, apply a per-process 4 GiB address-space limit to the entire invocation:

```sh
prlimit --as=4294967296 -- python3 -m tools.g2.matrix matrix.json --out reports/m6-02
```

A memory-limited reporter that cannot finish remains INCONCLUSIVE; do not
replace it with a PASS or retry with an unbounded process.

## Sampled timing and Q verification

The default reporter remains a full per-packet reference/custody check. An
explicit `verification` block on a run manifest selects sampled verification:

```json
"verification": {
  "mode": "sampled",
  "seqchecks": [{
    "path": "sequence.json",
    "sha256": "SHA256_OF_SEQUENCE_JSON",
    "ingress_port": 0,
    "egress_port": 1,
    "ingress_path": "ingress.pcapng",
    "receiver_path": "receiver.pcapng"
  }]
}
```

Paths are relative to that manifest. Register a separate unambiguous directional
capture pair for each input port. The JSON uses `hng-seqcheck-v1`; both complete
input file digests and byte sizes must match. In pcapng mode these must be the
same captures that the adapter converts for the reporter. Conversion rewrites
relative paths together with the other registered sources. Resume validation
hashes the sequence result and both captures as well as the normalized streams.
Missing, unhealthy, changed, ambiguous or incomplete coverage is INCONCLUSIVE.
The checker can report raw missing identities as FAIL while the reporter accepts
them as intentional Q/trace loss; its status is not copied blindly into a gate.

### Automatic directional seqcheck in pcapng mode

For a sampled run in `mode=pcapng`, `verification.seqchecks` may be empty.
The runner infers the direction from the registered pair's interface maps,
converts the captures, then supplies a checked sequence result to the reporter.
No additional CLI option is needed. Build the checker before reporting:

```sh
meson compile -C build seqcheck
```

The runner finds `seqcheck` on `PATH`, falling back to the checkout's
`build/seqcheck`. A supplied result is reused only when its registered JSON
SHA-256, both capture digests/sizes, direction and schema agree. Otherwise a
valid runner cache is tried, or C seqcheck is executed on the two original
captures. Checker PASS, FAIL and INVALID exits must agree with their JSON.
An INVALID diagnostic with unknown input hashes cannot be reused or sealed;
process/output failures remain INCONCLUSIVE and retryable.

Generated JSON and an atomic hash registration live under
`.seqchecks/<id>/` in the matrix output, outside the report directory archived
on interruption. Changed or corrupt cache entries are retained under its
`.incomplete/` directory before replacement. This permits a partially completed
report to reuse a verified checker result. Each report also receives its own
JSON copy and updated converted manifest, covered by its completion receipt.
The original native manifest, registrations and capture files are not modified.
No cache receipt is published until both input hashes are known and checked.

Completed reports use the existing resume checks; changed captures invalidate
them. New reports verify their derived result hash again before sealing.
This automation applies only to the existing sampled pcapng path. Full
verification is preserved; native/software input still uses explicit external
sequence registrations. Capture loss remains INCONCLUSIVE. A normal external
sequence check does not waive the adapter's synthetic observer-cost warning,
and an ordinary swap is retained as an order-gate FAIL even if another gate
keeps the overall report INCONCLUSIVE.

The current sampled pcapng adapter accepts **one direction per capture pair**.
A combined multi-interface bidirectional pair is INCONCLUSIVE with
`SEQCHECK_DIRECTIONAL_CAPTURE_REQUIRED`: the checker has no interface filter,
so its combined totals cannot be assigned to a single native port counter.
Use separate directional acquisitions/configurations as independent matrix runs.
For simultaneous bidirectional acquisition, supply sealed normalized streams
through native (`mode=software`) input with one registered seqcheck result and
capture pair per direction. That input mode can cover both registered ports;
it does not change the manifest's physical/software scope. Do not copy a
combined result into two directional registrations or split captures without
retaining health and identity coverage evidence.

The two verification layers are:

- **Order and storage: full supplied-capture/run coverage.** Order checks every
  ordinary identity's ingress rank, excluding selected-late identities from that
  ordering while still matching them and checking their flags. Duplicate and
  unexpected receiver identities fail. Missing identities are reconciled with
  complete native Q-drop, trace-loss and trace-outage-drop counters. Any native
  `delay_storage_drop` fails. These are run-level checks, not phase-local
  attribution and not an independent per-packet audit of Q decisions.
- **Timing and Q decisions: sampled.** The existing cell seed/mask selects
  offered identities before admission. Every selected offer across the entire
  registered cell is checked, including selected Q drops. Timing uses complete
  before/after pairs for the selected survivors and the existing uncertainty,
  median, p99 and sample-count rules. The ordinary-main and late-main subsets
  remain part of the same uniform main sample. Unsampled late/app extras are
  marked `not_checked` in this mode.

Stress runs update the exact Fraction/deque Q state for every arrival. Only
selected identities produce complete expected records and host Q comparisons.
Fixed L0 calibration uses its registered no-queue assumption. Trace L0 still
updates the complete clamp/loss/outage prefix; a sample cannot start with an
empty trace floor. Original ingress and line ordinals are retained. Neither
mode uses DUT queue occupancy or computed deadlines as independent predictions.
The `expected` stream is optional in sampled mode; an old full expected stream
is not consumed as a replacement for the sampled reference.

Large-run custody is diagnostic: host event counts are reported, but the full
LiveCustody join is not a prerequisite. Input transport seals, hashes, observer
health, calibration, sample pairing and selected Q evidence remain mandatory.
The initial implementation streams existing sealed normalized JSONL inputs;
full input parsing and source verification still cost time. A Q-only microbench
must not be presented as end-to-end reporter throughput.

JSON `coverage`, `counts_scope` and `sampled_reference`, plus the summary and
CSV coverage columns, identify what was checked. Row packet counts describe
sampled offers; full capture totals remain under `seqcheck` and native counters.
Unsampled Q decisions, unsampled latency, the full custody audit and physical
hardware qualification are explicitly unverified.

For independent runs, add `"workers": 2` to the matrix document. The default is
one worker. The value must be positive and no larger than the available CPU
count. Each run keeps its own process and output directory; summary ordering and
resume behavior follow the registration order. Bound the sum of worker RSS and
file-cache use to the deployment memory budget. A process address-space limit
is not a bound on aggregate memory. Arbitrary time shards of one Q/trace history
are not supported: they would require exact prefix state at every boundary.

Run the matrix as usual, after capture and sequence checking:

```sh
python3 -m tools.g2.matrix matrix.json --out reports
```

No result from this mode alone qualifies physical timing, storage throughput or
an observation window outside the supplied complete captures.
The [million-packet measurement and updated budget](reports/m6-sampled.md)
separate complete reporter throughput from the Q-only microbenchmark. The
current normalized-input pipeline has not demonstrated one-day M6 processing.

Run the sampled and matrix regression suites with:

```sh
meson test -C build g2-sampled g2-matrix --print-errorlogs
```

### Direct binary input for sampled runs

Add the raw native capture and one or two directional capture-pair documents:

```json
"verification": {
  "mode": "sampled",
  "native_binary": {"path": "native.hngb", "sha256": "FULL_NATIVE_FILE_SHA256"},
  "capture_pairs": ["forward-pair.json", "reverse-pair.json"],
  "seqchecks": [{
    "path": "forward-sequence.json", "sha256": "FORWARD_CHECK_SHA256",
    "ingress_port": 0, "egress_port": 1,
    "ingress_path": "forward-before.pcapng", "receiver_path": "forward-receiver.pcapng"
  }, {
    "path": "reverse-sequence.json", "sha256": "REVERSE_CHECK_SHA256",
    "ingress_port": 1, "egress_port": 0,
    "ingress_path": "reverse-before.pcapng", "receiver_path": "reverse-receiver.pcapng"
  }]
}
```

Replace the SHA256 placeholders with complete file digests. Each pair uses `hng-pcapng-pair-v1`, including the
registered nanosecond clock/mapping, measurement/drain bounds and physical
interface-to-port mappings. Its raw files must be the same files named by that
direction's seqcheck registration. The pairs must cover every registered
direction once. This path supports separate bidirectional pairs and all seven
native writers. Combined bidirectional capture files remain INCONCLUSIVE:
`hng-seqcheck-v1` totals do not attribute identities to physical input ports.

For this input mode, `streams` contains only the small sealed `counters` stream.
Keep capture observer health/cost endpoints for `before` and `receiver` in that
stream. Native actor, logical ingress/host, pool, NIC and terminal counters are
rebuilt from the complete binary capture. If native counter rows are also
provided, they must agree exactly with the reconstructed rows. An absent or
unknown capture-health/cost record remains INCONCLUSIVE. Do not synthesize
physical queue headroom from a complete file or from a zero drop count.

The binary file supplies logical `native-ingress` and `native-host` streams;
the aggregate external observation IDs are `before` and `receiver`. Native
handoff actor bindings can name `native-host` without registering a JSONL
packet stream. There is no native decode-to-JSONL prepass. Source/resume checks
bind the native file, pair metadata, raw pcapng files, checker JSON, counters,
configuration, traces and calibration.

Use matrix `mode=software` for this native input path, as for existing native
manifests. The manifest's `scope` independently declares software-only or
physical evidence. Physical calibration and observer-cost requirements still
apply; choosing the binary reader does not qualify hardware.

The reader uses bounded NumPy arrays. HNGBIN1 entries are variable-length, so
the reader verifies their CRC frames and tagged fields before exposing columns.
Only selected witnesses become packet dictionaries. Stress Q uses Python
integer ticks of 1/C nanoseconds: service adds exactly `8*(q+20)*10^9` ticks,
and draining compares completion with `arrival_ns*C`. Python integers prevent
overflow and preserve the Fraction model, including equality boundaries and
non-integral service times. Trace prefixes use the same service ceiling and
clamp/loss/outage rules. The normalized JSONL route remains the default when
`native_binary` is absent.

Host samples reuse an ingress decision only after matching the entire native
identity key. Unknown or changed keys use the original hash. Different phase
masks use a separate verified native pass to match the actual selected ingress
identities, rather than trusting host arrival metadata. This preserves sample
selection; the different-mask path has additional processing cost.

Both input routes share verdict/statistics code. Regression tests compare
`verdict.json`, `results.csv` and `summary.txt` byte for byte on identical raw
evidence, including Q drops, trace clamp, seven writers and a Q mismatch FAIL.
`report.json` retains input-specific provenance, integrity and processing times.
No raw source is deleted automatically.
