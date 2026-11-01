<!-- SPDX-License-Identifier: Apache-2.0 -->
# Sampled M6 reporter: software measurements

This report covers `verification.mode=sampled`, based on host revision
`2814711` plus the sampled reporter changes. It is software-only. It does not
qualify a NIC, clock, capture disk or DUT timing. See [runner usage](../M6-RUNNER.md#sampled-timing-and-q-verification).

## Verification scope

| Evidence | Check |
|---|---|
| Complete ingress/receiver captures | External `hng-seqcheck-v1`: every ordinary identity's relative order, duplicate/unexpected identities, matching flags and missing identities |
| Complete native counters | Missing reconciled with intentional Q/trace drops; zero delay-storage drops |
| Every registered timing sample, selected before Q admission | Independent expected Q decision and OWD, calibrated timestamps and existing statistical gates |
| All stress arrivals and trace prefixes | Exact Fraction Q/drain state and trace clamp/loss/outage prefix; only samples emit expected records |
| Large-run host custody | Diagnostic event counts; full custody audit unverified |

The default full mode remains available. No seed, sample-count rule, timeout or
physical calibration requirement is weakened. Saturated observer queues,
absolute end backlog, missing input population, invalid capture/clock evidence
and incomplete counter endpoints cannot yield a sampled PASS. Gate coverage is
the complete supplied input run, not independent attribution to each phase.

The external checker is [DPDS PR #32](https://github.com/markhhm/DPDS/pull/32),
revision `13aa2ba`. Its frozen contract and real missing=1 fixture were checked.
The checker reports complete pairs/s (N/t), distinct from EPBs/s (2N/t).
Its worker reports median **5.277 M pairs/s** at 128-byte snaplen against the
PM's **4.9 M pairs/s** requirement. That is the checker's separate synthetic
measurement, not reporter throughput or a physical capture result.

## Complete million-packet cell

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

## Updated processing budget

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
