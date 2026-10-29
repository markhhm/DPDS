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

Normal storage must use the registered streaming oracle and 1/1024 main sample.
Complete streams are a transport into that oracle, not a default disk archive.
Full persistence is an explicit bounded debug option. The provisional reporter
below remains limited to bounded software fixtures until the v1 integration
and online retention checks are complete.

These tools implement the independent reference/reporting portion of the
measurement implementation task. **Software-only, not G2.** They cannot qualify a
hardware run. Observer wiring, final event/payload format and actual-time
cross-line TX attempt evidence await the design owner’s specification.

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

The reference emits each decision immediately, retaining only finite-Q service
ledgers and constant line/port state. It uses exact Python Fraction arithmetic,
an independently written `splitmix64-counter-v1`, exact decimal probabilities,
service-time trace lookup, persistent retained-deadline clamp and explicit
outage/terminal policies. Q drops still consume a line-local selection ordinal.
Trace hashes cover the same bytes that are parsed. Paths are relative to the
registered host configuration. Unsupported/uncovered inputs fail explicitly.

## Provisional JSONL boundary

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

The output has one `kind=expected` row per packet, including intentional drops,
plus one required terminal summary with config/input hashes, canonical
expected-row SHA-256 and event count. The reporter verifies expected rows
against the seal and enforces schema/kind/run/count/hash consistency.
This detects changed/mixed reference output; final observer stream seals
will establish end-to-end source completeness.
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

The task's 0.2 ms tail injection cannot fail a fixed 0.5 ms threshold from a
zero-error baseline. Tests preserve the thresholds: 5% at 0.2 ms passes the
tail gate; the same 5% at 0.6 ms fails (also interpretable as +0.2 ms over a
registered 0.4 ms tail). Ordinary swaps, storage loss and 1% observer loss are
tested with their required FAIL/FAIL/INCONCLUSIVE verdicts.
