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
before execution. The output directory must be new; previous evidence is never
overwritten. Run/scope mismatches invalidate that run. A missing or invalid run
remains in the summary and does not suppress later runs.
IDs that collide with `summary.json` or another run's stderr filename are rejected.

Each run uses a fresh process and produces the unchanged G2 `report.json`,
`results.csv`, and `summary.txt`, plus a small `verdict.json`. The controller
reads only that verdict and writes `summary.json`; it does not load full reports
or capture streams. Stderr is retained as `<id>.stderr.log`. A reporter process
failure or incomplete set of artifacts becomes INCONCLUSIVE. Any measured FAIL
makes the matrix FAIL; otherwise every run must PASS. Exit codes are 0 for PASS,
1 for FAIL, and 2 for invalid/inconclusive evidence or matrix input errors.
An interrupt stops execution; partial output must not be treated as a final
summary. Re-run into a fresh output directory.

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
