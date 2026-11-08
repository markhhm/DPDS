<!-- SPDX-License-Identifier: Apache-2.0 -->
# Register and judge an M7 R_valid search

**Draft procedure: register the final plan before sending traffic.**
This prepares a search; it does not authorize an apparatus run.
Use the [M7 inputs](inputs.md), qualified capture path and existing
[G2 reporter](../M6-RUNNER.md). Do not change engine behavior or G2 thresholds.

R_valid is the highest **tested, independently confirmed PASS** offered L1
rate per direction in a registered ascending ladder. Both directions run
simultaneously. It is not the delivered rate, the sum of two directions,
an interpolation, or a claim about untested rates between steps.
A result below 100G remains that result.

## 1. Register before the first step

Use one immutable plan per composition **and** fixed model condition.
U and X have separate searches. C=80G and C=90G also have separate searches;
do not combine their PASS steps or change C to make a step pass.
Register:

- Source/Core pins, executable hashes, NIC/PMD, descriptors, CPU/NUMA placement,
  hugepages, observer cost qualification, capture/calibration and clock bounds.
- Composition: U is charged L2 1522 B; X is equal packet counts of charged
  64/512/1522 B. Stored bytes exclude 4 B FCS: 1518 and 60/508/1518 B.
  Preserve flow/VLAN layout, t, delta, p, seed, Q, C and M7 T/H/W/envelope.
- Exact ascending offered **L1 bit/s per direction**, step order, required cells,
  scope, duration, warmup/drain and exactly one independent confirmation.
  Freeze a registration ID and SHA256 of the plan's actual bytes.
- The expected bundle `sources.config_sha256`. The full reporter uses its
  registered config hash; the sampled reporter uses SHA256 of the canonical
  validated config. Use the producer's documented convention consistently.
  A whitespace-only config rewrite can therefore matter for the full path.
- Generator offered-rate readback and its relation to the registered target.
  Preserve each direction's actual offer, receive, L1/L2/L7 and packet rate.
  A commanded rate without delivery by the generator is not evidence of that
  offer. Keep the existing apparatus/registration checks; this tool adds no
  new rate tolerance and cannot qualify the generator from sampled pps.

The [X draft](../../examples/m7/rvalid-x-plan.json) and
[U draft](../../examples/m7/rvalid-u-plan.json) use C=90G, the unchanged compact
conditions, 600 s per observation and the sampled producer's config hash.
For C=80G, copy the corresponding draft and register the c80 config hash;
keep all other intended conditions explicit. Replace the draft registration
ID only as part of pre-run approval, then seal the final bytes.
These examples contain no apparatus addresses or CPU assignments.

## 2. Draft ladders and their estimate basis

The rates below are candidate design values, **not measured R_valid**.
The starting points deliberately leave room below software estimates.
Register different values before the search if apparatus evidence requires it;
do not add lower steps after seeing a failure in this search.

| Composition | Ascending candidate offered Gbit/s per direction | Starting pps per direction |
|---|---|---:|
| X | 20, 25, 30, 35, 40, 50, 60, 70, 80, 90, 100 | 3.475440 Mpps |
| U | 60, 70, 80, 85, 90, 95, 100 | 4.863813 Mpps |

Use FCS-inclusive L2 plus 20 B preamble/SFD/IFG:

```text
U mean wire bytes = 1522 + 20 = 1542
X mean wire bytes = (64 + 512 + 1522)/3 + 20 = 719.333333...
pps = offered_L1_bps / (8 * mean_wire_bytes)
100G U = 8.106355 Mpps/direction
100G X = 17.377201 Mpps/direction
```

The software cost reports used as planning input measured an optimized X
median of 6.398 Mpps/direction, with a later same-tree repeat at 4.801445;
retained U-sized traffic measured 9.546 Mpps/direction. These are **WSL2
reference estimates, not 100G evidence**. The X start uses about 72% of the
lower X estimate; the U start is about 51% of the U estimate. These fractions
are planning headroom, not acceptance thresholds. Source tree was DPDS
`659a0d5fa015de81da3a2a527f767052e57ca71d`, Core
`327ba7223bad9ad7c3b7b6d06f39dcfba436743f` (same trees as the profiled revisions).
The integration cost and limitations are described in
[M7 storage](../M7-STORAGE.md#software-rate-comparison). Shared-workstation
variation, real PMDs, long residence and observation overhead can lower rates.

## 3. One step, stopping and one confirmation

1. Check the registered apparatus and start a fresh run/capture pair for both
   directions. Use the registered warmup, observe, stop, drain and clean exit.
   Produce the existing G2 bundle `report.json`; retain all raw references.
2. A step is PASS only when the bundle and every registered required cell are
   PASS and all four gates (`median`, `p99`, `order`, `storage`) are PASS.
   Reuse their existing limits: no fitting, rounding or new confidence rule.
   `not_applicable` timing from an empty/all-dropped population cannot qualify
   an offered rate for this search. No direction or required phase is optional.
3. Preserve a reporter FAIL. Preserve an INCONCLUSIVE measurement (including
   the existing `status=INVALID` representation of incomplete evidence).
   Structural INVALID, missing/corrupt/hash-mismatched evidence or a missing
   required cell is unusable and stops the search as INCONCLUSIVE; it is never
   a PASS. List the reporter's reasons and failed gates.
4. **Stop at the first FAIL or INCONCLUSIVE.** Do not run a later ladder step,
   skip a missing step, retry to select a nicer outcome, or bisect afterward.
   Retain the stop rate, status, evidence and diagnostics.
5. The candidate is the highest PASS **before** that stop. If every registered
   step passes, it is the highest registered tested rate; report that the
   search hit its tested ceiling, not a discovered continuous upper bound.
   If the first step does not pass, there is no candidate and no R_valid.
6. **Design value: exactly one independent confirmation run**, in addition to
   the candidate's first run. Start new input streams/captures and a new run
   ID; use the same model, composition, target rate, duration and gate rules.
   Never pool observations or choose between multiple confirmations.
   This count is fixed before running, not chosen from the results.
7. Report R_valid only if that confirmation also passes. A missing confirmation
   is pending/INCONCLUSIVE; a failing or inconclusive confirmation leaves
   R_valid unavailable. Do not fall back to a different lower PASS. Preserve
   the candidate, both results and first stop. Any new search needs a new
   pre-registered plan; it does not erase this search.

The tool checks distinct run IDs across consumed stages and confirmation.
A fresh label alone does not prove an independent experiment: the operator
must retain the separate raw-input/capture provenance. The tool consumes
existing decisions and cannot independently attest physical qualification.
Software-only fixtures always leave hardware `R_valid_bps_l1` null.

## 4. Time budget for each step

**M7 search design value: observe 600 s per step and 600 s for confirmation.**
This adopts the preceding G2 matrix's at-least-ten-minute observation floor.
Freeze it in the M7 plan before running. Timing/Q are judged using the existing
sampling design; order/drop use the existing full-packet evidence. Insufficient
samples or observer/capture loss remain INCONCLUSIVE. Do not shorten a run
because a quick preview appears to pass. Additional registered M7 conditions
need their own time allocation; these two ladders are only fixed STL searches.

For t=142 ms, delta=320 ms, H=10 ms and the registered frame range:

```text
rho = max((L+20)/L) in the registered composition
Q_wire_bits_bound = 8*Q*rho
fill_s = Q_wire_bits_bound/(R-C), only if R>C
warmup_s > (t+delta)/1e9 + fill_s, if R>C
warmup_s > (t+delta)/1e9,          if R<=C
Q_drain_s <= Q_wire_bits_bound/C
```

R is the *offered* L1 rate per direction; C is the unchanged model service rate.
Below or at C, a sustained Q does not fill: record `fill=not_applicable`.
Such a step is an underload result, not proof of the 80/90G overload/Q stress
condition. Do not reduce C or Q to force a stress interpretation.
A burst can still transiently queue. Keep the existing G2 Q checks and record
its actual peak; the warmup formula does not assert an observed full queue.

For the two draft ladders and C=80/90G, choose **2 s warmup** per run. The
smallest positive R-C is 5G; the conservative X Q bound is at most 0.176161 s
at C=90G. Thus 2 s is strictly greater than 0.462+0.176161 s (U is smaller).
These are scheduling estimates; if registering a closer overload step,
recompute before running, rather than assuming two seconds always suffice.

Use [M7 STOP/DRAIN](inputs.md#warm-up-generator-stop-drain-and-engine-stop):
register 2 s drain after confirmed generator stop, retain stable final counters,
and allow 10 s engine-stop/teardown. The M7 H failure gate is still 10 ms;
the wall-time allocation does not relax it. Generator stop control has its
own bounded wait and SDK RPC overhead; its settling allowance alone is not
an end-to-end bound.

A scheduling slot is **614 s plus generator-control, setup, capture and reporter
cost**: 2 s warmup + 600 s observation + 2 s drain + 10 s teardown allowance.
The timing-only maximum for an all-PASS X ladder plus confirmation is
12*614 = 7368 s (122.8 min); U is 8*614 = 4912 s (81.87 min), per fixed C.
Both compositions together are 204.67 min per C, or 409.33 min for C=80 and 90G,
before apparatus/reporter overhead. Stop early according to the fixed rule;
do not turn the saved time into extra selectable attempts.

## 5. Drop location and bottleneck evidence

For every consumed step and its confirmation, retain normalized counter
endpoints/deltas, native diagnostics/final-stats, seqcheck health and the G2
reasons. Use existing names; never interpret an unavailable counter as zero.
The CLI exposes counters present in the bundle's `counters.ports`,
`counters.diagnostics` and `counters.observer`:

| Layer | Existing evidence examples | Interpretation |
|---|---|---|
| NIC | nic_imissed, nic_ierrors, nic_oerrors, nic_rx_nombuf; device error aliases | Non-model loss/error evidence; retain per-port xstats endpoints |
| ring | handoff_drop, rx_handoff_drop, tx_handoff_drop, rx_ring_drops, tx_ring_drops | Non-model handoff loss; ring high-water/loop gaps help diagnose but are not drop counts |
| storage | delay_storage_drop, storage_drops, m7_lineN_*_exhaustions | Non-model exhaustion; retain item/record/cell/normal/materialize/Core cause |
| Q | q_drop, q_drops | Intended shared-Q model decisions; not NIC loss |
| model | trace_loss_drop, trace_outage_drop and their diagnostic plurals | Intended registered trace decisions; not hidden storage failures |
| other | invalid_input_drop, terminal_tx_drop, shutdown_drop, missed_deadline_outage_drop | Preserve terminal/invalid cause; never relabel as Q |
| observer | records_dropped, capture_drops, malformed_records, identity_errors, timestamp_missing, clock_invalid | Measurement loss; existing G2 completeness rules apply |

These are overlapping diagnostics, not a sum of exclusive packet losses.
The CLI does not add them or infer causality. It lists nonzero non-model layers
as `bottleneck_layers` and separately preserves failed timing/order gates.
A timing FAIL with no recorded drops still matters. Missing layer evidence is
`unavailable`; `observed_zero` means only the counters actually present are zero.
If M7 failure counters, first INVALID reason, RX stop, ring high-water or loop
gaps are only in native sidecars, keep those sidecars with the bundle and
cite them in the operator report. This CLI does not invent missing diagnostics
or change an existing G2 verdict from raw telemetry.

## 6. Offline CLI and evidence index

Run from the repository root; no engine or remote access is needed:

```sh
python3 -m tools.g2.rvalid "$SEARCH_PLAN" "$EVIDENCE_INDEX"
```

The immutable plan uses `hng-m7-rvalid-plan-v1`, as in the draft JSON files.
The separate index is filled as runs finish; it must reference the frozen plan
hash. Paths are relative to the index's directory (absolute operator paths
also work). Each `bundle` is the reporter's **report.json**, not its input
manifest, verdict.json, matrix summary, sampled packet stream or raw capture.
Use file SHA256, not a reserialized JSON digest:

```json
{
  "schema": "hng-m7-rvalid-evidence-v1",
  "registration_id": "REGISTERED_ID",
  "plan_sha256": "PLAN_FILE_SHA256",
  "trials": [
    {"rate_bps_l1": 20000000000, "bundle": "step-01/report.json", "sha256": "REPORT_FILE_SHA256"},
    {"rate_bps_l1": 25000000000, "bundle": "step-02/report.json", "sha256": "REPORT_FILE_SHA256"}
  ],
  "confirmation": {
    "rate_bps_l1": 20000000000,
    "bundle": "confirmation/report.json",
    "sha256": "REPORT_FILE_SHA256"
  }
}
```

This example confirms step 1 after a non-PASS step 2. Omit unrun trials; use
`confirmation: null` until its result exists. Duplicate rates or unregistered
rates are errors, not retry candidates. The file is read-only; the tool opens
bundles in ladder order, stops before opening higher entries, and reports
`ignored_trials`. Their presence does not authorize running them.

Output is one JSON line, schema `hng-m7-rvalid-result-v1`:

- `status`: PASS means the candidate has its required confirmation, even if the
  next step failed. FAIL means no candidate before a failing first step, or a
  failing confirmation. INCONCLUSIVE means unusable/missing evidence, pending
  confirmation or an inconclusive confirmation. Exit codes are 0/1/2 respectively.
- `candidate_rate_bps_l1`, `confirmed_rate_bps_l1`, `R_valid_bps_l1`: registered
  per-direction offers; the last is null for software-only evidence or an
  unconfirmed candidate. It is not computed from sampled throughput.
- `stop`: first non-PASS step, including its rate, status, reasons, failed gates,
  drop layers and nonzero diagnostic bottleneck layers. Null for all-PASS.
- `tested_ceiling`: true for an all-PASS ladder. Do not extrapolate beyond it.
- `stages`, `confirmation`: consumed evidence hashes, run IDs, original coverage,
  gate/reason summaries and available measured L1/L2/L7/pps. Sampled reporters
  may omit measured rates; those remain null, never guessed from sample counts.
- `hardware_qualification`: explicitly not assessed by this aggregation tool.
  Existing sampled/full evidence limits remain attached to each stage.

Bundle identity/config/scope/cell mismatch, short or nonsimultaneous windows,
a missing four-gate result and a pooled repeat cannot qualify a step. The
sampled producer stores its measurement window in `registration.windows`;
the full producer also exposes it on each row. Both existing formats work.
A reused run ID cannot confirm a candidate. This detects obvious reuse, not
all possible dependence between captures; retain operator provenance too.
