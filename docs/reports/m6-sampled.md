<!-- SPDX-License-Identifier: Apache-2.0 -->
# Sampled M6 reporter: software measurements

This report covers `verification.mode=sampled`: the JSONL baseline merged in
PR #33 and the direct-binary update based on `364ca2e`. It is software-only. It does not
qualify a NIC, clock, capture disk or DUT timing. See [runner usage](../M6-RUNNER.md#sampled-timing-and-q-verification).

## Verification scope

| Evidence | Check |
|---|---|
| Complete ingress/receiver captures | External `hng-seqcheck-v1`: every ordinary identity's relative order, duplicate/unexpected identities, matching flags and missing identities |
| Complete native counters | Missing reconciled with intentional Q/trace drops; zero delay-storage drops |
| Every registered timing sample, selected before Q admission | Independent expected Q decision and OWD, calibrated timestamps and existing statistical gates |
| All stress arrivals and trace prefixes | Exact rational Q/drain state and trace clamp/loss/outage prefix; only samples emit expected records |
| Large-run host custody | Diagnostic event counts; full custody audit unverified |

The default full mode remains available. No seed, sample-count rule, timeout or
physical calibration requirement is weakened. Saturated observer queues,
absolute end backlog, missing input population, invalid capture/clock evidence
and incomplete counter endpoints cannot yield a sampled PASS. Gate coverage is
the complete supplied input run, not independent attribution to each phase.
When healthy complete capture evidence proves unexplained missing delivery,
the result is FAIL even if that missing packet also removes a timing sample.
Unhealthy/incomplete capture evidence remains INCONCLUSIVE.

The external checker is [DPDS PR #32](https://github.com/markhhm/DPDS/pull/32),
revision `13aa2ba`. Its frozen contract and real missing=1 fixture were checked.
The checker reports complete pairs/s (N/t), distinct from EPBs/s (2N/t).
Its worker reports median **5.277 M pairs/s** at 128-byte snaplen against the
PM's **4.9 M pairs/s** requirement. That is the checker's separate synthetic
measurement, not reporter throughput or a physical capture result.

## Historical JSONL measurement (#33)

The synthetic fixture streams sealed ingress, before, receiver, host and counter
records plus healthy pcapng files. There is no full expected stream. The actual
C checker processes both captures and produces the JSON consumed by the
reporter. Fake timestamp mappings have zero uncertainty; there is no NIC or DUT.
The fixed frame is 1518 bytes (1522-byte Q charge; 1542 bytes on wire), C=10G,
Q=8 MiB, t=65 ms, p=0. Registered main selection is 1/256 with seed 17.

Only offers and Q-drop terminals are generated in the host diagnostic stream;
dispatch/attempt records are absent. Real native captures have more diagnostic
records to parse. These results must not be used as a conservative production
throughput guarantee.

Measurements use one pinned Linux CPU, AS limit 4 GiB, CPU limit 600 s and no
other heavy campaign from this worker. The process streams input and retains
bounded selected identities; it does not retain all packet histories. Files
were read from ordinary storage with warm cache, not tmpfs. Generation is
excluded from reporter timing. The matrix measurement includes registration,
source hashes, the reporter child, report files and completion seals. The
precomputed C checker's execution is excluded; its JSON and raw source hashes
are verified by the reporter.

| Metric | Fixed L0, 9G | Stress, 12G |
|---|---:|---:|
| Offered / received | 1,000,000 / 1,000,000 | 1,000,000 / 838,843 |
| Selected offers / checked Q decisions | 4,012 / 4,012 | 4,012 / 4,012 |
| Selected Q drops / timed deliveries | 0 / 4,012 | 664 / 3,348 |
| Q drops / unexplained missing | 0 / 0 | 161,157 / 0 |
| Result | PASS; `g2_qualified=false` | PASS; `g2_qualified=false` |
| Source bytes, including pcapng | 1,776,812,087 | 1,716,896,265 |
| Matrix elapsed | 189.77 s | 135.07 s |
| User + system CPU | 186.93 s | 134.98 s |
| Maximum reported process RSS | 60,812 KiB | 61,180 KiB |
| Reporter arrival scan | 57.873 s | 53.126 s |
| Before/receiver scan | 97.884 s | 55.648 s |
| Host diagnostic scan | 29.026 s | 23.230 s |
| Reporter total | 185.293 s | 132.345 s |

Both runs use all million input identities and the same selection seed. Stress
updates all million exact Q-prefix states. Its checker reports raw FAIL with
161,157 missing identities and `complete=true`; the reporter reconciles those
with native intentional Q drops and independently checks every selected drop.
No receiver order or storage violation remains. The elapsed measurements are
single runs on a shared host with different surviving populations, not a
controlled claim that stress is faster than L0.

The fixed cell also passed under cProfile after the completeness corrections:
288.960 s, 59,760 KiB RSS, 4,012 selected offers. Profiling overhead is excluded
from the normal throughput estimate. Of 288.958 profiled seconds, 245.938 s
(85.1%) are inside `StreamReader.__next__`: full JSON decode, schema validation,
canonical record hashing and transport checks. These cumulative times overlap:
record validation 115.316 s, input read/decode 65.991 s, canonical serialization
47.769 s. Sampled reference consume is 12.879 s; it is not the main bottleneck.

The Q-only microbenchmark's 2.13–2.27 microseconds/packet excludes this input
pipeline. Adding NumPy only to Q state cannot remove the dominant parsing and
validation work. A future compact, validated arrival/sample projection must
preserve all source seals, counter coverage, clocks and selected evidence.

## Historical JSONL processing budget

Using the planning population **1.2e11 offered packets** and this fixed-cell
measurement:

```text
CPU estimate = 186.93 s / 1e6 * 1.2e11 / 3600 = 6,231 core-hours
16-core ideal wall estimate = 189.77 s / 1e6 * 1.2e11 / 16
                           = 395.35 h = 16.47 days
24-hour ideal core count = 6,231 / 24 = 260 cores (rounded up)
```

The stress measurement alone gives 4,499 core-hours, an ideal 16-core elapsed
estimate of 281.40 h (11.72 days), and a CPU-only 24-hour floor of 188 cores.
The observed range is therefore **4,499–6,231 core-hours**, before the excluded
costs below. It is not an upper bound on the complete M6 workload.

These are estimates, not measured parallel speedup. They exclude acquisition,
native-binary decoding, pcapng conversion, checker runtime, production host
event density, trace lookup and shared disk/cache contention. The new reporter
does **not** establish the one-day M6 processing budget. It replaces the earlier
Q-kernel-only estimate of roughly 73 core-hours for the complete pipeline.

Matrix `workers` parallelizes independent complete runs with isolated outputs.
No time shard starts Q or trace state from empty. Physical qualification and
full-scale parallel memory/storage throughput remain unmeasured. The existing
capture storage budget is not reduced: this implementation still reads sealed
normalized JSONL and raw capture evidence. It does not implement online source
discard or a compact binary projection.

## Direct-binary million-packet cell

The new opt-in input reads the existing HNGBIN1 file using bounded NumPy
`fromfile` buffers, validates CRC frames and tagged records, and exposes
structured arrays. HNGBIN1 entries have variable length, so mapping a fixed
record dtype over the file would be incorrect. Packet dictionaries are created
only for selected witnesses. Raw pcapng is also read in bounded arrays, retaining
capture health, profile, clock and source-digest checks. Tiny sealed JSONL
streams supply external capture counter endpoints; native counters come from
the binary. No expanded packet JSONL or native-counter prepass is required.

All stress arrivals still update Q serially. Integer ticks of 1/C ns preserve
the exact Fraction model with arbitrary-precision arithmetic; differential
tests cover variable lengths, non-integral service times and drain ties.
Trace L0 retains its complete clamp prefix. Different phase masks require an
additional verified native pass; the benchmark uses a common registered mask.

The complete stress cell below uses one million offers from the **same** native
binary, raw captures, C-checker result, calibration and counter evidence for
both routes. The legacy route is generated by the existing binary decoder,
NativeAdapter and capture adapter. Both routes PASS with 838,843 deliveries,
161,157 intentional Q drops, 4,012 selected decisions and 3,348 timed samples.
`verdict.json` (302 bytes), `results.csv` (649 bytes) and `summary.txt` (274 bytes)
are byte-identical. Input provenance and processing fields in `report.json`
are intentionally different. Smaller regressions additionally compare fixed
L0, trace clamp, seven-writer bidirectional evidence and Q-mismatch FAIL cases.

| Metric | Same-cell JSONL | Direct binary (`baea886`) |
|---|---:|---:|
| Matrix elapsed, including reads/hashes/child startup/output | 208.94 s | **8.84 s** |
| Elapsed per offered packet | 208.94 us | **8.84 us** |
| User + system CPU | 206.18 s | 8.82 s |
| Maximum reported process RSS | 70,308 KiB | 149,448 KiB |
| Reporter total | — | 8.014 s |
| Native/capture input, selection and exact Q | — | 7.485 s |

This is a single pinned-CPU run on a shared Linux host, AS 4 GiB/CPU 180 s for
binary (600 s for JSONL), warm ordinary filesystem cache, no tmpfs. Reading
and validation are included; fixture generation and the separately run C
checker are excluded. The C result and its complete raw-source bindings are
verified. There is no NIC, real clock or qualified capture disk. These offers
and Q-drop terminals omit full production dispatch/attempt diagnostics, so
this measurement is not a conservative production throughput guarantee.

## Current M6 processing estimate

For the same planning population of **1.2e11 offers**, the measured binary cell
gives:

```text
CPU estimate = 8.82 s / 1e6 * 1.2e11 / 3600 = 294 core-hours
16-core ideal wall estimate = 8.84 s / 1e6 * 1.2e11 / 16
                           = 18.42 hours
24-hour CPU-only floor = ceil(294 / 24) = 13 cores
```

This is an ideal extrapolation, not measured parallel speedup or a one-day M6
qualification. Complete independent runs/lines can run in separate matrix
workers; a time shard must preserve Q and trace-prefix state. Parallel workers,
cold storage, trace lookup, mixed masks and production event density remain
unmeasured. The C checker, acquisition and disk contention add work.

The measured compact inputs are 262,628,936 native bytes plus 294,215,176
pcapng bytes per million offers: **556.84 MB**, or about **66.82 TB** at the
planning population. Current integrity checks read native bytes three times
(matrix before/after hashes and validated parsing), and pcapng bytes four
times (those reads plus checker-source binding). That is about **1.965 GB**
read per million offers, **235.77 TB** extrapolated, requiring roughly
**2.73 GB/s aggregate reads** to finish in 24 hours, before excluded work.
These byte estimates use this stress survival ratio and event density.
The existing sustained 500 MB/s native-capture disk requirement alone does
not qualify simultaneous multiworker reporting. No source retention or
integrity check is removed to obtain these numbers.

## Reproduction and retained evidence

With the registered fixture in `CASE_DIR` and an unused report destination:

```sh
PYTHONPATH="$PWD" /usr/bin/time -v prlimit --as=4294967296 --cpu=600 --core=0 -- \
  taskset -c "$CPU" python3 -m tools.g2.matrix "$CASE_DIR/matrix.json" --out "$REPORT_DIR"
```

The worker retains its streaming generator, source manifests/captures,
checker JSON, plain resources, report artifacts and full `report.pstats` in the
private measurement archive. Million-packet inputs are not committed or made
part of routine CI. Unit tests cover full/sample agreement at selected
identities, Q boundaries, unsampled trace floor influence, changed hashes,
missing checker evidence, gate failures and deterministic parallel resume.

The complete matrix suite runs as its own mandatory Meson `g2-matrix` test.
Under four-CPU affinity, `g2-software` took 52.41 s and `g2-matrix` 20.99 s;
all 243 tests from the former combined target still execute (223 + 20).
This split preserves assertions, fixture populations and existing timeouts.
