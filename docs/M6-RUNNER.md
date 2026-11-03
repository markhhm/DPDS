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

## M5 zero-delay clock and bypass qualification

Use `tools.g2.m5_qualify` on closed directional capture pairs before M6.
It runs the existing M6 pcapng path and C seqcheck; it does not acquire traffic
or access devices. First register the observation hardware, raw nanosecond
clock maps, anchors and independent quantization/mapping bounds using the
existing [calibration format](../tools/g2/calibration.py). Unknown clocks or
uncertified capture health cannot pass. This does not add a switch clock kind.

Register an ordinary `hng-m6-matrix-v1` matrix with `mode=pcapng`, the intended
`scope`, and at least two distinct observed offered rates for each line and
observation clock/device. Include the target rate. Each pair references its
existing native manifest, source configuration, calibration and sealed native
streams. Use sampled `timing_mode=calibration`, fixed `t_ns=0`, `delta_ns=0`,
`p="0"`, and no trace. Preserve the v1 sampling header; register enough main
samples (oversample low-rate cells if needed) to retain at least 368 paired
samples per run/line over a nonzero interval. Keep the same clock IDs, device
tokens, timestamp points and topology across loads. Do not change source files.

```sh
python3 -m tools.g2.m5_qualify "$M5_MATRIX" --out "$M5_REPORT_DIR" \
  --p1-manifest "$P1_0_MANIFEST" --manifest-out "$P1_0_QUALIFIED"
```

`P1_0_MANIFEST` is the original `hng-p1-0-v1` output from bring-up preflight.
Both manifest arguments are optional together for standalone analysis. All
output paths must be new. Original sources and manifests are preserved.
The tool writes `qualification.json`, `results.csv`, `summary.txt`, and the
unchanged M6 reports/checker evidence under `matrix/`.

The table reports measured mean wire offered rate, signed offset, drift in ppb
and over the sample interval, between-load offset span, maximum absolute raw
zero-reference error, the clock bound and full-capture missing identities.
Offset/drift diagnostics use exact rational least squares; bounds round upward
from exact fits, rather than the rounded display offsets. Their fitted values
are **not subtracted** from the observed error. The conservative observed-sample bound
is the maximum of absolute raw error, fitted drift span and between-load offset
span, plus the registered external quantization/mapping allowance. Every bound
must be **at most 10,000 ns**. A 50 us drift cannot be fitted away into PASS.
No DUT residence or load tail is relabeled as a removable clock offset.

Full, healthy seqcheck evidence must have zero missing, reordered, duplicate,
unexpected or flag-mismatched identities. True bypass loss is FAIL even if too
few timing pairs survive. Capture loss, unknown health, missing evidence or
insufficient load/sample coverage is INCONCLUSIVE. Exit codes are 0 PASS,
1 FAIL and 2 invalid/inconclusive input. Qualification covers only the recorded
loads, main timing samples, clock path and intervals; it does not establish
unsampled timing extremes, UTC accuracy, future drift, or separate fixed path
residence from clock error. Bypass identity coverage remains full-packet.

The exported P1-0 manifest retains its environment `status` and all other
preflight fields, embeds the full result in `clock_qualification`, and binds
the original manifest SHA-256 in its sources. Only physical-scope PASS fills
top-level `clock_uncertainty_ns`; synthetic PASS and failed/unknown results
leave it null. Synthetic tests are explicitly `software-only` and never set
`physical_qualified=true`. This clock/bypass result does not waive the separate
M6 observer-cost or G2 gates; their original decisions remain in the M6 reports.

### X710 DUT with an external generator and capture clock

[examples/m5-qualify-x710.json](../examples/m5-qualify-x710.json) is a physical
registration template for a two-port X710 using `i40e` through one switch VLAN.
Both X710 ports belong to DPDS as DUT ports. Provide traffic generation and
reception on an external machine; provide ingress and egress observation on
external capture interfaces. The generator and capture roles may share that
external machine, but neither role uses a spare DUT port. A testpmd self-send/
self-receive bring-up check does not supply this DUT measurement topology.

```text
external generator -> switch -> DUT port 0 -> DPDS -> DUT port 1 -> switch -> external receiver
                              before observation    receiver observation
                                      \               /
                              external capture machine
                               one common capture clock
```

Register the actual observation feeds and packet path; a shared switch VLAN
alone does not prove that traffic traverses the DUT. Exclude switch shortcuts,
loops and duplicate mirror feeds. Reverse runs swap the DUT direction and
observation-feed assignments, not the clock source. The template has low and
target loads for each direction in separate capture pairs; the existing
directional seqcheck restriction still applies.

The `i40e` hardware timestamp path is for PTP packets, not general UDP probes
(see [DPDK 26.03 i40e timesync implementation](https://github.com/DPDK/dpdk/blob/v26.03/drivers/net/intel/i40e/i40e_ethdev.c#L9888)).
Timestamp the ordinary probe packets at the capture machine. Both capture
interfaces must use the same qualified clock source, declared clock ID,
identity mapping and device token across loads. Merely using the same host,
or two independently synchronized clocks, does not establish that shared clock.
Record the calibration bounds and capture-health evidence there; DUT, generator
or software submit timestamps do not replace these external observations.

Copy the template to `M5_MATRIX` and replace every `${...}` value with private
registered values. `CAPTURE_ROOT` locates the four closed pair directories;
use an absolute path or resolve it relative to the rendered matrix. The runner
does not expand shell variables. `plan` records topology metadata; it does not
configure traffic, interfaces, clocks or the switch. Each pair and its native
manifest/configuration must independently contain the matching port/VLAN,
clock mappings, zero-delay model and observed low/target traffic settings.
Then run the M5 command above with the matching P1-0 manifest.

This example uses one registered line on one VLAN. The reader still requires
retained single-VLAN 802.1Q probes. If access ports produce untagged captures,
resolve the capture/tagging arrangement before qualification; changing a VLAN
number in metadata does not restore an absent wire tag. This M5 template tests
one line at a time. The M6 deployment below registers two VLANs on a trunk.
No X710 hardware result is claimed.

## X710 M6: cable and LEO on separate VLANs

[examples/m6-x710.json](../examples/m6-x710.json) preserves every condition,
sampling rule and approved repeat policy of G-short. It expands the inventory
below and selects physical
pcapng input and registers an X710 with `i40e`, DUT ports 0 and 1, and distinct
710 (cable) / 711 (LEO) trunk VLANs. Cable and LEO lines share
the two DUT ports; each packet carries its line's single 802.1Q tag. The three
entries in `m6-short.json` remain representatives; `m6-x710.json` contains the
complete expanded registration. Synthetic two-point LEO boundary conditions
use VLAN 711 even when they do not load a trace profile.

Use the external topology described above for both VLANs: an external generator
and receiver, plus ingress and receiver observation on an external capture
machine. The two observations must use one qualified clock, including for
reverse runs. The DUT's PTP timestamp capability does not timestamp these
ordinary probes. Record private host, interface and clock identifiers as the
template variables. Interface keys `"0"` and `"1"` identify the corresponding
DUT port's external feed, not a capture-device port number.

Copy the example to `M6_MATRIX`. Replace **all** `${...}` placeholders before
running it. The two registered VLANs are already JSON **integers**; do not
replace them with quoted numeric strings. Resolve capture paths
relative to that matrix, or use private absolute paths. The runner does not
expand variables or configure the switch, interfaces, generator or clocks.

Every native `hng-g2-manifest-v1` used by a physical run now requires an explicit
top-level `deployment`. Copy the rendered `plan.deployment` from the matrix to
each manifest without changing its fields:

| Field | Required declaration and check |
| --- | --- |
| `nic` | `model`, `pmd`, distinct DUT `ports`; X710 requires `i40e` and two ports. |
| `dut_host` | Private DUT identifier; generator and capture must be external to it. |
| `vlans` | Family-to-VLAN mapping; X710 requires both cable and LEO with distinct values, including in single-family runs. |
| `generator` | External `host`, plus `interfaces` keyed by each DUT port. |
| `capture` | External `host`, `interfaces`, `clock_id`, `mapping_id`, `clock_source`. |

Each manifest `lines[]` entry also declares `family: "cable"` or `"leo"`.
Its VLAN must equal that family's deployment VLAN and its validated engine
configuration VLAN. A trace line must declare `leo`. A directional cell may
contain one family while retaining the complete two-family deployment.
Existing physical manifests must add this registration; omitted fields do not
receive defaults. Unmarked software-only legacy fixtures remain supported.

`capture.clock_source` must equal the calibration clock's `device_token`.
The registered mapping must map that clock into the observation domain.
Each capture pair and each retained normalized observation must select exactly
the declared clock and mapping. Merely
renaming a pair to another calibrated clock is invalid. The matrix worker
compares its expected deployment with the measured manifest before and after
conversion; missing or changed deployment is INVALID. Resume checks also
validate the retained report's deployment. All declarations remain in
`report.json` and the matrix summary, with the existing source-hash receipts.
Interface declarations record the operator's topology; they do not prove the
physical cabling or the capture device's clock wiring.

Software verification uses two synthetic 400-packet captures through the real
pcapng converter, C seqcheck and sampled reporter: cable VLAN 100 and a LEO
trace on VLAN 200, including its clamp prefix. Both cells retain all 400
samples, 3 ns median absolute timing error, and PASS order/storage gates.
The matrix remains **software-only, INCONCLUSIVE**, with
`g2_qualified=false`, because synthetic captures do not supply observer-cost
qualification. Reproduce this wiring test with:

```sh
python3 -m unittest tools.g2.tests.test_x710_matrix
```

Before the first physical M6 run, complete M5 using the external common clock
and zero missing identities. Also complete the registered A/B′/A coexistence
check: B′ runs the real DPDS engine with external traffic. A NIC bring-up
self-loop alone does not satisfy this check. This preparation does not execute
hardware or establish physical acceptance; an eventual NIC/PMD change requires
clock and topology qualification again.

### Generator sequence and index budget

Register probe generators in `plan.generator`, before capture or offline reporting.
Matrices with deployment/traffic registration require this block. Legacy matrices
without those registrations can retain their original input form.
The reader rejects a missing policy, a global sequence or a nonzero start.
It also rejects a stride other than one or an index estimate above the limit.
CLI errors exit 2 with `INVALID generator` before capture reads or output creation.

```json
{
  "profile": "examples/m7-traffic/stl_probe.py",
  "sequence_policy": "per-flow-dense",
  "sequence_start": 0,
  "sequence_step": 1,
  "seqcheck_memory_limit_bytes": 4294967296,
  "seqcheck_profiles": {
    "X": {"flows": 64, "max_sequence_per_flow": 1710568}
  }
}
```

Each profile describes an alternative worst directional capture. `flows` includes
all distinct five-tuple/VLAN groups in that capture. `max_sequence_per_flow` is
an inclusive upper bound. Include warmup offers, including packets that the DUT
later drops. Require balanced per-flow packet counts for the generated bounds.
After any change to flow counts, exposure, rates or frame mix, regenerate the bounds.

The [STL recipe](../examples/m7-traffic/README.md) keeps each flow's counter on
one data core. The generated X710 matrix records conservative U/X bounds for
all conditions, with both VLAN groups where applicable. No generator or capture
starts during preflight. For M5's registration template, replace the flow/range
placeholders with JSON integers, not quoted numeric strings.

The LP64 estimate uses the existing C index: 4096 sequence slots and 34312 bytes
per page, plus allocation/hash-table growth allowance. It does not allocate the
index. Preflight reports `generator_index_bytes` per registered profile.

A full 63-second X direction at 10G has at most 109,476,368 offers by the
registered equal-packet frame mix. Sixty-four dense flows need **918,887,424 bytes**
by this estimate, below 1 GB (decimal). Global-strided IDs require about 58.76 GB.

The full two-VLAN X bound is 1,837,770,752 bytes. The process limit remains 4 GiB.

These are index estimates, not measured total RSS. Capture buffers, libraries and
other processing also need memory. Registration checks declarations, not actual
profile behavior or captures. Retain the process limit and full stream checks.
An allocation failure still yields INVALID, never partial PASS. ASTF TCP is
outside this UDP-probe index contract.

### Complete G-short inventory and acquisition budget

The 72 main and 288 boundary conditions include the 60 trace conditions;
the trace count is **not** an additional 60. Main is M-CA/M-CB fixed cable
(36 conditions) plus M-LA/M-LB single-LEO trace (36). Boundary is E0/E1/E2/E3,
E-mid and E-CB fixed (264) plus E-pair cable/LEO trace (24).
Fixed main repeats three times, fixed boundary once, and every trace condition
uses the existing two repeats and registered episode counts from
[m6-trace-conditions.json](../examples/m6-trace-conditions.json). The source
registration hash, four window hashes and per-direction profile selections are
retained. Natural profiles remain external; no trace conversion is run here.

Each both-directions acquisition yields two separate directional capture-pair
registrations, as required by the current reader and seqcheck. Their
`extensions.m6.acquisition_id` is shared; their IDs and pair paths are distinct.
Capture both directions concurrently, with common start/stop, rather than
turning this condition into two isolated runs. Register one native manifest
whose `run` equals each directional run ID. Paired cable/LEO traffic remains
two VLANs in that direction's capture pair. This file registers future evidence;
it does not acquire or split captures.

| Inventory | Conditions | Acquisitions | Directional reporter inputs | Acquisition time estimate |
| --- | ---: | ---: | ---: | ---: |
| Fixed main | 36 | 108 | 144 | 1 h 08 min 24 s |
| Fixed boundary | 264 | 264 | 352 | 3 h 27 min 30 s |
| Trace, including all registered episodes | 60 | 1,008 | 1,464 | 4 h 45 min 36 s |
| Total | 360 | 1,380 | 1,960 | 9 h 21 min 30 s |

Fixed observation uses the G rule, with surviving rate `min(C,R)` per line:
`max(30, ceil(431*1024*8*1542/(surviving_bps*p_late)))` seconds.
Empty ordinary/late strata at p=0 or p=1 use main. Of the 300 fixed conditions,
222 observe for 30 s and 78 for 61 s (the p=.01 cells at 9G); each acquisition
adds 2 s warmup, 1 s drain and 5 s reset. This applies the 30–70 s rule directly,
without the conditional 1/256 shortening. Both directions count once in elapsed
time. Trace episodes retain 2+9+1+5=17 s; repetitions/episodes never pool gates.

Adding the planning reserve of 4 h for setup, recalibration and reruns gives
**13 h 21 min 30 s**. A conservative alternative with every fixed acquisition
at 70 s gives 12 h 49 min 12 s before reserve, or **16 h 49 min 12 s** with it.
These are estimates, not measured runtimes. Offline reporting, capture disk
throughput, equipment availability and hardware prerequisites are not included.
Measured sample counts and unchanged reporter gates remain authoritative.

Regenerate the committed inventory without reading profiles or captures:

```sh
python3 -m tools.g2.expand_m6_x710 --out examples/m6-x710.json
```

After binding private deployment, capture-root and trace-profile-root variables,
run a registration-only check with the **same reader** used by reporting:

```sh
python3 -m tools.g2.matrix "$M6_MATRIX" --preflight
```

It validates matrix schema, deployment structure, all 1,960 safe unique IDs and
resolved pair paths. It neither writes output nor opens capture/native manifest
files nor starts reporter workers. Future evidence paths may be absent.
The result explicitly says `scope=registration-only`, `g2_qualified=false`,
and lists capture files, native manifests and hardware qualification as
unavailable. Unbound deployment values or capture paths are rejected.
The full-inventory tests bind synthetic identifiers and preflight all future
paths, without capture acquisition:

```sh
python3 -m unittest tools.g2.tests.test_m6_x710_full
```

## Two-judge handoff

Use the [judge shard procedure](m6/judge-shards.md) to assign complete conditions,
prepare a macOS judge and transfer immutable sources/results while capture continues.
Its cost balance is an estimate. Each job retains the existing source/output-bound seals.

## Hardware-day runbook

Complete [BRING-UP](BRING-UP.md) first. Unresolved hardware decisions block this procedure.
Export its private deployment variables, source revisions and calibration path.
Also set `M6_MATRIX`, `REPORT_DIR`, `PILOT_MANIFEST`, `PILOT_MATRIX`, `PILOT_REPORT_DIR` and `RUN_MANIFEST`.
Set `M5_MATRIX`, `M5_REPORT_DIR`, `P1_0_MANIFEST` and a new `P1_0_QUALIFIED` path
for [M5 qualification](#m5-zero-delay-clock-and-bypass-qualification).

The `*_COMMAND` variables contain the operator's deployment commands, not repository tools.
Register their traffic, capture, drain, reset and restoration parameters first.
Capture commands must retain native evidence and closed pairs per [PCAPNG](PCAPNG.md). The matrix runner does not acquire traffic.

Use native physical observer evidence for the pilot: `PILOT_MATRIX` uses `scope=physical`, `mode=software` and native manifest paths.
Here `software` selects native input, not software scope. Pcapng adapter queue counters are synthetic and cannot prove physical queue headroom.
On failure, stop traffic, drain and restore the allocated resources per [Handoff](BRING-UP.md#handoff). Keep all evidence.

| Step | Commands from `DPDS_SRC` | Expected output and stop condition |
|---|---|---|
| 1. Bypass and clocks | `sh -c "$BYPASS_COMMAND"`<br>`sh -c "$CALIBRATION_COMMAND"`<br>`prlimit --as=4294967296 -- python3 -m tools.g2.m5_qualify "$M5_MATRIX" --out "$M5_REPORT_DIR" --p1-manifest "$P1_0_MANIFEST" --manifest-out "$P1_0_QUALIFIED"` | Register zero-delay captures at low and target loads. Keep identities/counters and `CALIBRATION_FILE`. Require physical M5 PASS, zero bypass missing identities and uncertainty ≤10,000 ns; the exported P1-0 environment status must also be PASS. Otherwise stop here. |
| 2. Observer cost pilot | `python3 -m tools.g2.validate "$PILOT_MANIFEST"`<br>`OFFERED_BPS=10000000000 sh -c "$PILOT_CAPTURE_COMMAND"`<br>`prlimit --as=4294967296 -- python3 -m tools.g2.matrix "$PILOT_MATRIX" --out "$PILOT_REPORT_DIR"` | Register the pilot with required-cell `mask_bits=8`. Require preflight PASS before capture. Require zero observer drops, positive queue capacity, high-water below capacity and no `observer_cost` warnings or missing evidence. Record `OBSERVER_COST_REPORT`, `EVIDENCE_COMMIT` and `RUN_ID` per the [cost gate](BRING-UP.md#observer-cost-gate-before-m6). On failure, retain the previous plan and stop for review. |
| 3. Register and capture M6 | `cp -n examples/m6-x710.json "$M6_MATRIX"`<br>`python3 -m tools.g2.matrix "$M6_MATRIX" --preflight`<br>`python3 -m tools.g2.validate "$RUN_MANIFEST"`<br>`sh -c "$M6_CAPTURE_COMMAND"` | Bind private variables, register the expanded repeat/episode pair paths and copy `plan.deployment` to every native manifest; declare each line family. Preserve simultaneous-direction acquisition groups. After step 2 passes, register fixed cells at 1/256 and 30 s exposure and recalculate the budget. Keep the v1 header, sample gates and trace plan. Require registration-only PASS and preflight PASS for every native run manifest before capture. Require closed pairs, native seals and capture-health evidence. On rejection or incomplete capture, stop here. |
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
For simultaneous bidirectional acquisition, a native binary manifest may register
one directional capture pair per port in `verification.capture_pairs`. Point the
pcapng run at either registered pair: the runner generates or reuses a separate
C seqcheck result for **every** registered pair and retains them all. The sampled
reporter validates the whole native acquisition and both directions, regardless
of which pair was the entry point. Explicit registrations also remain supported
through `mode=software`. Neither mode changes physical/software scope. Do not copy
a combined result into two directional registrations or split captures without
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

Use matrix `mode=software` with explicit sequence registrations, or point a
`mode=pcapng` run at its directional pair document to generate/reuse C seqcheck
automatically. In the latter case, no packet JSONL conversion is performed:
the runner writes a derived manifest binding the original native binary,
capture pairs and sealed counters, then invokes the same sampled reporter.
The original inputs are unchanged and completed reports remain resumable.
The manifest's `scope` independently declares software-only or
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
Raw sources remain complete unless the operator enables the explicit [retention stage](#raw-retention-after-complete-judgment).

## Raw retention after complete judgment

The complete matrix registers `extensions.m6.raw_retention` for each acquisition.
Bind its `capture_directory` with the same `CAPTURE_ROOT` as the pair paths.
Freeze the matrix before the first report. Two directional runs share one byte quota.
The default plan estimates at most 1.8 TB of raw prefixes across 1,380 acquisitions.
See the [formula and limits](reports/m6-raw-retention.md).

First, register the complete raw SHA256 values without deletion:

```sh
prlimit --as=4294967296 -- python3 -m tools.g2.matrix "$M6_MATRIX" \
  --out "$REPORT_DIR" --prune-raw
```

The stage requires complete seals for every member of an acquisition.
It writes `$REPORT_DIR/.raw-retention/ACQUISITION_ID/manifest.json`.
This manifest contains the full original SHA256, size and range of each declared raw file.
It also binds the complete member seals and producer versions.
The stage does not change a sealed scientific result.
An incomplete or changed seal causes refusal.

After the operator confirms the registered backup scope, apply the time cuts:

```sh
prlimit --as=4294967296 -- python3 -m tools.g2.matrix "$M6_MATRIX" \
  --out "$REPORT_DIR" --prune-raw --apply-raw-prune --backup-scope-confirmed
```

This command replaces full raw files with diagnostic prefixes.
Before the first replacement, the stage creates all prefixes and checks their total against the acquisition quota.
A larger total causes refusal and leaves all originals unchanged.
Before deletion, the stage flushes complete scientific files, seals, registration and directory entries to storage.
Each replacement requires a durable JSON record with the original filename, size, SHA256, retained range and UTC time.
The record also contains the retained SHA256, size and replacement state.
Atomic replacement preserves a separate hardlinked backup.

The time range starts at the first ingress packet across the acquisition directions.
The prefix includes warmup when warmup precedes the observation window.
PCAPNG cuts use capture timestamps with interface offsets.
Native cuts use `at_ns` through the registered engine-to-observation clock map.
The stage scans every native record because native time order can differ from event order.
Only single-interface Ethernet, nanosecond, same-clock PCAPNG pairs with final healthy statistics are supported.
Unsupported profiles cause refusal.

A retained PCAPNG remains readable with its original packet blocks and new prefix statistics.
A retained native binary uses the diagnostic `hng-native-prefix-v1` envelope.
Its event bytes, global order and producer sequences remain unchanged, including gaps.
The stage recalculates the registered time interval and clock before it accepts a stored retention manifest.
Read this native prefix explicitly:

```sh
python3 -m tools.g2.raw_prefix "$NATIVE_PREFIX" --out "$DIAGNOSTIC_JSONL"
```

A prefix cannot reproduce the complete judgment.
Resume verifies each retained hash, original seal, non-raw source and report artifact before it returns the original verdict.
The complete samples, C results, report files and seals remain unchanged.
A fresh full reporter rejects the native prefix.
Missing receipts, changed prefixes or changed report evidence cause refusal and preserve the complete result.
The usual resume command remains valid after a successful cut.
Use the three explicit flags again to finish an interrupted replacement.

The output directory supports one controller at a time.
Stop capture and close the registered raw files before this stage.
Do not change source paths, receipts or raw files during this stage.
Do not copy partial prefixes over the only external backup of a complete capture.
The confirmation flag records the operator decision. It does not create or verify a remote backup.
