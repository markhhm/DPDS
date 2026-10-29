<!-- SPDX-License-Identifier: Apache-2.0 -->
# HNG observer v1 implementation plan

**Goal:** Finish software M2 against DPDS-Core `fd4ed12`, current spec `1be2960`,
`docs/HNG-OBSERVER.md` (`hng-observer-v1`). Existing delivery is host20433e1,
core4ae06c5. All work stays on the measurement branch; no hardware access.

**Architecture:** A strict sealed interchange surrounds the existing independent
Fraction reference. Bounded active joins reconcile complete ingress, external
before, receiver, terminal, dispatch and TX facts online. Only main samples,
bounded histograms and capped mismatches persist by default. The host exports fixed
metadata to bounded producer queues, serialized outside the packet path.
Per-line/cell statistical gates consume verified pairs, never production
deadlines as expected input. Physical timestamp parsing/calibration is offline;
physical qualification remains false without actual hardware evidence.

**Tech stack:** Existing Rust/C/DPDK plus Python standard library. No new package.
**Execution:** Independent protocol/clock and host-export domains run under
dispatching-parallel-agents; root owns reference, joins/report integration.
Only root commits/pushes; agents own disjoint files and run relevant tests.

## Constraints / review focus

- Preserve CLI positional names and SHA256 hng-g2-timing-v1 / 1/1024.
- Parse integer ns/ordinals exactly; reject bool-as-int, unknown keys, gaps,
  wrong seals/end markers, unregistered identities/mappings and incomplete drain.
- Actual engine offer ordinal is distinct from intended RX ordinal. Complete
  data-path loss is FAIL; observer incompleteness alone is INCONCLUSIVE.
- External ingress / receive share calibrated O; engine arrival and gate use E.
  Physical uncertainty <=10000ns, raw/corrected OWD both retained.
- All four gates per registered line/direction/phase; no cable masking LEO.
- Main, ordinary/late main, late-extra and each app-extra remain separate.
- Real host exporter cannot wait for an observer, format JSON or allocate on
  the packet path. Loss counters and incomplete seals survive overflow.
- Test before implementation, save RED/GREEN logs in own outbox. Keep old
  comparison/check tests where their behavior is still part of the contract.

## Task 1: strict streams and clock calibration (independent owner)

Files: new `tools/g2/protocol.py`, `calibration.py`, tests/test_protocol.py,
tests/test_calibration.py. No existing files edited by this owner.

- [ ] RED: altered marker, missing seal, dropped seq, unknown kind/field, bad
  integer, metadata/identity error and all stated mapping/clock faults.
- [ ] Implement StreamReader(path, role=None, run=None), iterable data records,
  with `.header`, `.seal`, `.integrity` after exhaustion. StreamError(ValueError)
  has `.code` and `.measurement_status` (INVALID or INCONCLUSIVE).
- [ ] Implement StreamWriter(destination, run, stream_id, role, clock_id,
  producer='g2', producer_commit='software'), `.emit(kind, **fields)` and
  `.finish(complete=True,dropped_records=0)`. Destination is path (exclusive)
  or open text stream. Canonical hash includes header/data/LF, file hash separate.
- [ ] Calibration(document, scope, run), `.map_timestamp(timestamp, mapped_ns)`
  returns mapped ns; `.pair(before,receive)` returns raw_owd_ns,owd_ns,
  uncertainty_ns,pair_id,target_clock_id. Exact half-even rational arithmetic.
- [ ] GREEN relevant tests; send root file/test/results and limitations.

## Task 2: host observation exporter (independent owner)

Files: new common/observer.[ch], tests/observer.c, tests/observer_export.py;
edits common/common.[ch], core/{receive,worker,transmit,combined}.[ch], dpds.c.
Meson linkage requested to root; do not edit Python tools or meson.build.

- [ ] RED: bounded overflow visible, caller does not block, probe identity and
  pre-handoff intended ordinal, actual offer ordinal, dispatch item / consumed
  prefix, zero/partial/full TX, rejects/terminal lifecycle, queue/pool accounting.
- [ ] Implement preallocated fixed metadata queues per producer and off-path
  exporter. No new binary protocol or dependency. Raw adapter JSONL may be sealed
  offline after checking producer sequence/counter health; root supplies protocol.
- [ ] Wire full RX events before classification/handoff; offer events; every
  terminal reason; successful dispatch handoff IDs; consumed-prefix TX attempts.
  Prefix evidence is captured when used, not reconstructed from later clocks.
- [ ] Parameterized software observer enablement; disabled baseline unchanged.
  Unknown production identity is explicit incomplete coverage, never invented ID.
- [ ] GREEN local C/export checks; send raw format/CLI and build requirements.

## Task 3: reference and bounded bundle integration (root)

Files: reference.py, new bundle.py / custody.py / validate.py, tests and fixtures.

- [ ] RED sealed CLI, full per-port ends, invalid physical packet records,
  intended-vs-actual divergence, duplicate/lost terminal identity, before/after
  joins, resource bounds and incomplete/expired identity handling.
- [ ] Upgrade reference CLI to read/write final sealed streams and summary,
  preserve consume/finish arithmetic, independently derive raw VLAN/length facts.
- [ ] Use bounded active per-identity joins and retire resolved facts; persist
  main-sample witnesses and capped mismatch diagnostics. Full-history disk joins
  are allowed only for explicitly bounded debug/software fixtures.
- [ ] Replay dispatch availability / consumed prefixes and trace gates. Preserve
  failed suffixes exactly, allow only evidenced gate bypass. Missing evidence
  is incomplete; fully observed incorrect selections or terminal loss fail.
- [ ] Normalize start/end counter deltas with descriptors/reset semantics.
- [ ] GREEN, then commit logical changes.

## Task 4: final report, fixtures and integration (root)

Files: report.py, stats helpers if needed, README.md, tests, Meson.

- [ ] RED per-line/cell aggregation, true probe-only counters, separate strata,
  signed/absolute raw/corrected OWD, exact small-n CI bounds, delivery conditional
  timing, clock uncertainty, trace hold, serialization busy period diagnostics.
- [ ] Implement final schema/CSV/status/exit rules, registration inventory and
  source integrity; every software PASS has g2_qualified=false.
- [ ] Sealed virtual-device fixture(s) using actual mbufs/TX evidence plus
  >=1,000,000 core events / >=32 seeds across both directions/four states.
- [ ] Inject ordinary/unsampled reorder, storage loss, .2ms detection, .6ms tail,
  1% observation gap, partial TX/outage, calibration faults and capture overflow.
- [ ] Run core fmt/clippy/test, host compile/Meson/pre-push; fresh independent
  review, fix real findings RED->GREEN, update core reports and KS notebook.
- [ ] Push only own hng/m2-obs branches, report SHAs/results/limits to the project maintainer.

## Format rulings / ledger

- Published spec resolves handoff identity via handoff_queue_id/first_handoff_seq
  and offered handoff_seq plus consumed_dispatch. No implementation blocker.
- Normalize frontiers as per-port objects `{through_ns:null|uint64,
  input_complete:bool}`; null means no certified closed frontier yet. An end
  permits release despite absent prior idle frontier. Preserve actual prefixes.
- Missing nested manifest JSON shapes are fixed in executable software fixtures
  and README without changing normative names/semantics. All adapters use one
  shape; no silent legacy-mode qualification.
- Hardware observer drivers/capacity calibration are external M5 requirements;
  software M2 validates timestamp adapters with retained raw fixtures only.
- Latest storage contract: stream mode is the default; full per-packet recording
  needs an explicit debug window <=2 s. Extra strata retain histograms rather
  than unbounded full pairs. Main samples retain exact quantiles and bounds.
- Seal validation is complete in protocol.py; integration into the complete
  streaming reference/report CLI remains separate. Receiver wire-order and
  complete-evidence anomaly classification have focused regressions.
- OnlineCells, Retention and ReceiveOrder provide per-line/direction/phase
  statistics, separate strata, bounded sample/error storage and wire-order
  reconstruction. Their caller must still verify complete identities, clocks,
  terminal custody and actual TX attempts before any final qualification.
- Reference CLI now consumes/emits v1 sealed streams, preserves invalid-input
  port ordinals and exports source integrity, end/line counts and resource peaks.
  The in-process draft remains for differential checks. Debug custody replay and
  calibration are tested components; default online integration remains open.
- Registration preflight verifies topology/queue layout, sources and calibration,
  required-cell sample planning, pool declarations and bounded debug retention.
  It is explicitly separate from captured-stream integrity and data-path gates.
