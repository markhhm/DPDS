<!-- SPDX-License-Identifier: Apache-2.0 -->
# Draft M2 software measurement tools

The v1 stream boundary is available independently of the remaining reporter
migration. `python3 -m tools.g2.protocol STREAM.jsonl` validates the header,
all-record sequence, canonical content hash, separate file hash, end markers and
terminal seal. Missing sequence records or seals produce INCONCLUSIVE even
without a producer loss counter; corrupt data or markers produce INVALID.
It makes no network connections and never qualifies hardware. Receiver wire
order is the explicit `receive_seq`, independent of JSONL export order; the
receiver join performs its complete permutation checks.

`calibration.Calibration` verifies exact rational raw-to-observation-clock
mappings with half-even rounding, mapping validity intervals, reset/step checks,
per-direction clock pairs and conservative offset/uncertainty bounds. Before
time remains distinct from engine arrival; corrected and raw physical OWD are
both returned. Fake clocks are accepted only as software evidence. These
offline checks do not measure or certify the actual apparatus.

`custody.audit_host` is a bounded, disk-backed replay for **explicit full-mode
software fixtures only**. It reconciles intended versus actual offer ordinals,
terminal reasons per identity, dispatch queue item ordinals, consumed handoff
prefixes and actual TX selection/gate evidence. It checks zero/partial/full TX,
retry suffix ownership and cable bypass during a LEO outage. It refuses normal
stream mode and physical scope; a complete online custody join remains required
before deployment. Full fixture history is not the default recording path.

`online.OnlineCells` consumes independent model decisions and verified physical
OWD pairs. It evaluates registered line/direction/phase cells separately, keeps
main errors for exact quantiles, and updates bounded 1-microsecond signed/absolute
histograms for main, ordinary/late main, late extra and each app extra. Empty
timing populations are not applicable; a nonempty population below 368 main
pairs is inconclusive. Confirmed data-path loss fails, while missing observer
pairs remain inconclusive. The caller still owns calibration, full identity
joining, terminal/attempt evidence and capture-integrity verification; these
statistics alone cannot qualify a run.

`online.Retention` defaults to `stream`, keeping only registered hash samples,
capped mismatch records and explicit debug windows (at most two seconds each).
Complete records are consumed online, not retained by this class. Main-sample
storage has a registered capacity; exhaustion invalidates evidence. Software
fixtures can explicitly select full retention. `online.ReceiveOrder` reconstructs
wire order from `receive_seq` within a registered bound, independently of export
line order. Its gaps remain incomplete evidence.

`identity_index.IdentityIndex` implements the bounded volatile index proposal
in Core `fe20ae2`. It requires an explicit positive
`capacity.identity_index_capacity`. It retains only exact identity keys and
compact outcome/delivery/cell tags in memory. It never writes an index to disk
or retains a packet dictionary. `finish()` and context-manager teardown release
all entries, including on abort. The report retains counts and explicit heap
and capacity formulas, measured object/table widths and runtime version; shared
objects are conservatively counted more than once. Allocator/RSS qualification
is separate from this estimate.

The index does not evict known outcomes during a run. At the cap it refuses new
keys and counts `join_overflow`. Later unknown keys in that direction are
ambiguous and remain overflow evidence; an unknown original phase affects all
possible cells. `OnlineCells.finish(identity_index=INDEX_REPORT)` checks run and
exact cell inventory, records the counts and makes affected cells INCONCLUSIVE,
even if every numeric gate passes or another numeric failure exists. Receiver
classification facts still require complete upstream capture evidence before
the caller can issue a data-path verdict. Full join/host integration remains open.

Normal storage must use the registered streaming oracle and 1/1024 main sample.
Complete streams are a transport into that oracle, not a default disk archive.
Full persistence is an explicit bounded debug option. The provisional reporter
below remains limited to bounded software fixtures until the v1 integration
and online retention checks are complete.

These tools implement the independent reference/reporting portion of the
measurement implementation task. **Software-only, not G2.** They cannot qualify a
hardware run. The v1 contract is published; complete online joins, host observer
wiring and final report integration remain in progress.

## Run and check

From the host repository root, with the normal Cargo/Meson/DPDK environment:

```sh
git submodule update --init --recursive
meson compile -C build
meson test -C build --print-errorlogs
# Alternative Python checks (pytest is optional; unittest is standard library):
python3 -m unittest tools.g2.tests.test_reference tools.g2.tests.test_report tools.g2.tests.test_probe tools.g2.tests.test_differential
python3 -m pytest tools/g2/tests
```

`python3 -m tools.g2.validate manifest.json` is a registration preflight. It
checks the complete v1 manifest, config/trace bytes and explicit calibration,
without opening capture interfaces. Its `check=registration-preflight` result
does **not** evaluate capture integrity or the four data-path gates. Those
checks are listed as unavailable; `g2_qualified` remains false. Exit 0 means
the preflight passed, not that a captured run passed G2.

The executable manifest example is `tests/test_registration.py:manifest`.
Port entries declare logical ingress/egress, link rate, TX `queue_id` and
`rx_queue_ids`/`tx_queue_ids`. Lines must exactly match config port/VLAN mapping
and list registered app labels. `clocks` maps `engine` and per-ingress
`observation` IDs. Streams register unique role/ID/path/file-hash entries.
Producer versions include explicit dependency versions. Every required cell
registers its mask and `planned_survivors`; a pooled cell additionally declares
distinct `pooled_repeats`. Default hash sampling stays at 1/1024. The planner
uses the largest mask meeting the 0.001 undersampling risk target, with
`N_plan(10)=440433` and `N_plan(0)=368`. Actual paired counts still determine
timing sufficiency; registration cannot promise a passing interval.

Capacity registration includes active-join and main-sample bounds, maximum join
lag, ring/record widths, explicit processing/storage qualification booleans and
named handoff queues. Offload registration covers FCS/VLAN retention, segments,
segmentation/coalescing flags, CQE compression and timestamp mode. Full capture
and counter-to-identity reconciliation remain part of online report integration.

`python3 -m tools.g2.counters manifest.json > counters.json` reads only registered
counter streams, verifies their seals and file hashes, and emits
`hng-g2-counters-v1`. Each scope needs explicit `phase=start` and `phase=end`
snapshots; optional periodic snapshots can expose an intermediate reset even
when the final generation returns to its starting value. Deltas exclude
pre-run counts. Gauges retain final values and queue high-water maxima. The
artifact retains endpoint clocks/times/generations, descriptors and stream
integrity. It keeps bounded per-scope state, not a full snapshot history.

Every configured ingress direction needs software port counters. Observer
scope IDs match the registered ingress/before/receiver/host stream IDs. Missing
or reset required counters stay null and make evidence inconclusive. Overlap
diagnostics are retained separately from disjoint terminal accounting. An
evidence PASS means counter normalization succeeded; packet gates still need
the complete identity pipeline. The bounded draft reporter accepts the
normalized port view, preserving INCONCLUSIVE as compatibility INVALID.

Meson builds the pinned core's `examples/m2_replay.rs` adapter and includes all
Python checks in `g2-software`. The million-event test compares **1,048,576**
events in 32 cases, every admission outcome/Q occupancy, every survivor's
selection/service/deadline/release/clamp/byte fields, and full output order.
Cases cover fixed two-point delay, p=0/1/0.1, fractional C, finite Q, trace
UP/DOWN, interval loss and outage hold/drop. There is no Rust model import in
the Python reference. The Rust adapter contains no reference formulas.
`G2_CORE_REPLAY=PATH_TO_REPLAY` selects an already built adapter.

```sh
python3 -m tools.g2.reference config.json RUN ingress.jsonl > expected.jsonl
python3 -m tools.g2.report config.json expected.jsonl receiver.jsonl counters.json manifest.json > report.json
```

The reference CLI requires a sealed `hng-g2-events-v1` ingress stream and emits a
sealed expected stream, with its own envelope sequence. Both seals cover header,
packets and markers. The `hng-g2-reference-v1` summary retains the input canonical
and file hashes, config/trace hashes, per-port end counts, per-line model counts
and Q-ledger resource peaks. A source gap or missing seal makes the output seal
incomplete and returns 2, even without a supplied drop counter. Stdout is a
transport into the oracle/report pipeline; redirecting it persists full output
and is reserved for explicit software fixtures or bounded debug windows.

V1 packets add `flow_id`, raw `vlan_id`, `segments`, `length_flags`, `app`,
`selected_late` and `input_error` to the draft fields below. The reference checks
raw port/VLAN classification independently. An explicit invalid raw frame keeps
its port ordinal and produces `invalid_input`, without consuming valid line
service or a line selection ordinal. A host rejection of otherwise intact input
does not become an intended model loss. The in-process draft API remains for
the differential harness; the bounded draft reporter accepts both reference
schemas. Its receiver/counter/manifest inputs remain provisional.

The reference emits each decision immediately, retaining only finite-Q service
ledgers and constant line/port state. It uses exact Python Fraction arithmetic,
an independently written `splitmix64-counter-v1`, exact decimal probabilities,
service-time trace lookup, persistent retained-deadline clamp and explicit
outage/terminal policies. Q drops still consume a line-local selection ordinal.
Trace hashes cover the same bytes that are parsed. Paths are relative to the
registered host configuration. Unsupported/uncovered inputs fail explicitly.

## Draft in-process boundary

Ingress packets contain all of:

```json
{"kind":"packet","run":"RUN","ingress_port":0,"line":"cable-forward","ingress_seq":0,"selection_seq":0,"external_identity":"probe-0","arrival_ns":0,"length":76}
{"kind":"frontier","run":"RUN","ingress_port":0,"through_ns":99,"next_ingress_seq":1}
{"kind":"end","run":"RUN","ingress_port":0,"next_ingress_seq":1}
```

`length` is retained tagged mbuf length, excluding FCS. Q charges
`max(length+4,64)` and C charges a further 20 bytes. Physical ingress ordinals
start at zero, are contiguous per port, and timestamps never move backward.
Selection ordinals are contiguous per line, including Q/trace losses. Each
configured ingress port must end. A frontier certifies every arrival <= W has
already appeared; no later event may violate it. This first schema assumes all
observed packets are classified into the declared lines; unsupported frame
events require the forthcoming observer schema rather than silently skipping
sequence gaps. Optional packet `app` is an oversampling stratum label.

The in-process output has one `kind=expected` row per packet, including
intentional drops, plus one terminal draft summary. The CLI adds the final
envelope and complete input/output seals described above. The reporter verifies
expected rows against the summary hash and validates a supplied v1 stream seal.
Never feed production-computed deadlines into this reference. The reporter
requires the summary; truncated reference output cannot pass.

Receiver records contain `run`, `ingress_port`, `external_identity`, and
`receive_ns` when timing is captured. Every survivor identity must be present
in capture order even without a timestamp. Identity is namespaced by run/port;
retransmission/fingerprint ambiguity must be resolved before this draft schema
can be used for production apps. Missing selected timestamps are INCONCLUSIVE.
The future observer supplies pairing and clock mapping; this tool assumes
integer relative ns in a common clock, with declared uncertainty.

`counters.json` maps ingress-port strings to integer terminal counters:
`rx_returned`, `tx_accepted`, `still_owned`, `q_drop`, `trace_loss_drop`,
`trace_outage_drop`, `delay_storage_drop`, `invalid_input_drop`, `handoff_drop`,
`terminal_tx_drop`, `missed_deadline_outage_drop`, `shutdown_drop`,
`observer_drop_packets`. Missing/null counters mean unknown, never zero.
Additional raw NIC/mempool counters are retained, not added to terminal drops
because they may overlap. A production observer must also supply hardware
counter semantics and capture coverage; this draft does not certify them.

Example manifest:

```json
{"scope":"software-only","timing_mode":"stress","sampling_seed":17,"oversample_apps":[],"clock":{"common":true,"uncertainty_ns":0},"egress_bps":{"0":10000000000,"1":10000000000}}
```

## Interpretation

The draft report has four gates **per ingress direction**: median absolute
error <=100,000 ns; p99 <=500,000 ns; zero unexplained order mismatches; zero
delay-storage drops. Fixed `stress` uses independently calculated
`deadline-arrival`; trace `stress` uses `release-arrival`, including modeled
outage hold, as clarified by the M2 specification owner.
Fixed `calibration` uses `t+I*delta`, retaining serialization in the error.
No fitted baseline is subtracted. Trace calibration needs its registered trace
release model and currently uses `stress` instead. Any paired reception before
ingress beyond declared clock uncertainty invalidates evidence before sampling.
The draft uses software engine arrival for ingress; the final observer schema
will separate engine arrival from external ingress/common-clock timestamps.

Timing uses a distinct SHA-256 domain, a recorded uint64 seed, and ten zero
low hash bits (1/1024) of run/port/external identity. The uniform main sample
alone drives timing gates. Additional late/app strata are reported separately,
excluded from main and potentially overlapping each other. The report includes
sample counts and 95% binomial order-statistic quantile intervals (iid sample
assumption). A bound outside observed ranks is unbounded, represented as null,
not the maximum observed error. Insufficient samples, missing pairs, unknown
counters, observer loss, incomplete drain/conservation, or clock uncertainty
that can change a timing verdict produce INCONCLUSIVE.

Full receiver checking includes unsampled packets and allowed late overtakes.
Shared-port trace arbitration needs actual TX attempt/gate evidence; this
draft marks that case INCONCLUSIVE. It does not infer gate evidence from
receiver timestamps. Ordinary/late stable release order remains checked for
each line. The reporter currently loads expected/receiver records into memory
(O(N)); the reference itself is streaming. Long hardware runs need the final
observer/reporting pipeline and a bounded-memory receiver join.

Q output includes peak L2 bytes and maximum **remaining exact wire service
time at C**. `8*L2_bytes/C` is also shown separately as a byte normalization;
it excludes wire overhead and is not the actual drain time. Late collision
serialization is independently predicted by a physical egress serializer in
release/ingress order, with first-bit departure timestamps. Main-sample
serialization and residual-error distributions are diagnostics. Gate errors
continue to use the original model deadlines, including collision error.

Status/exit codes are provisionally `PASS/0`, `FAIL/1`, `INVALID/2`,
`INCONCLUSIVE/2`, and always `g2_qualified=false`. Stage 1 source was fetched after the source maintainer pushed it:
`the Stage 1 harness de035a63f5d0135ee70b0bceb27d5051897b85a1` (36d54a2 + de035a6),
**not second-reviewed**. Read sources: `C2/docs/vm126-target-check.md`,
`C2/scripts/vm126_target_check.py`, `C2/harness/target_rate_check.py`,
`C2/harness/target_rate_metrics.py`, and `C2/scripts/target_rate_remote.py`.

`--out NEW_DIRECTORY` writes `results.csv`, `summary.txt`, and full `report.json`.
Flat rows retain Stage 1's `status`, `error_median_ms`, `error_p99_ms`, probe and
loss field names. Point quantiles use the same linear interpolation. CSV rows
map insufficient measurement evidence to `INVALID`; detailed JSON retains
`measurement_status=INCONCLUSIVE`. A valid failing direction takes precedence
over invalid directions in the overall result, matching Stage 1. Within each
row evidence invalidity takes precedence. NIC drops supplied as measured
software evidence also fail; absent hardware counters are explicitly software
not-applicable, with null value. P1 adds C/Q/trace, CI and hash-sampling fields;
it does not implement VM126's specific target-load/app/steal admission check.

`probe.py` preserves the Stage 1 100-byte UDP payload: network-order uint64
sequence, uint32 magic `0x4c30504b`, one 0/1 late flag, and 87 zero bytes. It
validates malformed/truncated headers. Receiver records may include
`selected_late`; flag changes and identities absent from ingress invalidate
evidence. With complete evidence, duplicate delivery or delivery of an intended
Q/trace drop fails the data path, following the updated observer specification.
Whole-line stable order is additionally checked
against the independent P1 model, beyond Stage 1's per-flow high-water count.

Tests preserve the thresholds: 5% at 0.2 ms is detected and passes the timing
gates; 60% at 0.2 ms fails the median gate; 5% at 0.6 ms fails the p99 gate.
Ordinary swaps, storage loss and 1% observer loss are tested with their required
FAIL/FAIL/INCONCLUSIVE verdicts.
