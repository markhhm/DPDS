<!-- SPDX-License-Identifier: Apache-2.0 -->
# M7 storage integration performance

This comparison uses DPDS a9b92962 and Core 8a32ae58 as the baseline.
The optimized code uses Core 327ba72 and DPDS 308d232.
All changes affect the explicit M7 storage option. The default path uses the original API.
These short software runs estimate CPU cost. They do not establish hardware `R_valid` or NIC losslessness.

## Attribution method

A temporary link-wrapper probe sampled DPDK cycle counters once per 1024 calls in each region.
Each region used a different sample phase. Nested regions never measured the same call.
The analysis subtracted the measured overhead of an empty counter pair.
It converted cycles with the DPDK timer frequency, then multiplied by the actual region call count.
Each value below is the median across three trials and two scheduler owners, in ns per accepted packet.

The probe measured admission, accepted callback, backend admission, head selection/consume, materialize, pool operations, diagnostic publication and hold checks.
The hold estimate includes its helper, backlog queries and the extra clock read at that call site.
The pool row measures the actual pool dequeue operation. It excludes mbuf reset and payload copy.
The 1518-byte case retains its mbufs, so compact copy and output allocation costs are zero.

Inclusive rows contain their subset rows. Do not add them together.
The admission and callback-shell rows subtract nested costs before the median.
Separate row medians can therefore differ from the difference between two displayed medians.

Wrappers and the callback proxy add overhead. The profile gives estimates for attribution, not absolute CPU cost totals.
The throughput comparison below uses executables without the probe.

## Before and after attribution

### X stored bytes

| Measured region | Before ns/packet | After ns/packet |
| --- | ---: | ---: |
| Core admission excluding callback | 44.91 | 45.60 |
| Accepted callback, inclusive | 47.22 | 47.49 |
| Callback shell above backend | 4.48 | 6.31 |
| Backend admission (subset of callback) | 43.33 | 42.26 |
| Merged selection + consume | 68.03 | 22.82 |
| Materialize, inclusive | 36.90 | 35.97 |
| Compact copy-out (subset of materialize) | 30.50 | 31.69 |
| Pool dequeue ops (subset of materialize) | 4.06 | 4.87 |
| Storage diagnostic publication | 3.54 | 0.07 |
| Hold helper + backlog + extra clock | 1.77 | 1.95 |

### 64 stored bytes

| Measured region | Before ns/packet | After ns/packet |
| --- | ---: | ---: |
| Core admission excluding callback | 40.33 | 36.11 |
| Accepted callback, inclusive | 40.68 | 43.31 |
| Callback shell above backend | 4.67 | 5.45 |
| Backend admission (subset of callback) | 35.90 | 37.48 |
| Merged selection + consume | 58.51 | 15.76 |
| Materialize, inclusive | 43.06 | 42.30 |
| Compact copy-out (subset of materialize) | 39.56 | 37.29 |
| Pool dequeue ops (subset of materialize) | 3.77 | 7.49 |
| Storage diagnostic publication | 3.41 | 0.07 |
| Hold helper + backlog + extra clock | 1.19 | 1.60 |

### 512 stored bytes

| Measured region | Before ns/packet | After ns/packet |
| --- | ---: | ---: |
| Core admission excluding callback | 39.93 | 36.64 |
| Accepted callback, inclusive | 70.79 | 73.10 |
| Callback shell above backend | 6.85 | 4.27 |
| Backend admission (subset of callback) | 62.99 | 66.68 |
| Merged selection + consume | 64.26 | 20.62 |
| Materialize, inclusive | 60.78 | 58.13 |
| Compact copy-out (subset of materialize) | 53.21 | 53.00 |
| Pool dequeue ops (subset of materialize) | 5.40 | 7.59 |
| Storage diagnostic publication | 3.70 | 0.07 |
| Hold helper + backlog + extra clock | 1.43 | 1.86 |

### 1518 stored bytes

| Measured region | Before ns/packet | After ns/packet |
| --- | ---: | ---: |
| Core admission excluding callback | 39.45 | 38.50 |
| Accepted callback, inclusive | 13.16 | 10.47 |
| Callback shell above backend | 5.67 | 4.50 |
| Backend admission (subset of callback) | 7.75 | 7.08 |
| Merged selection + consume | 60.64 | 12.81 |
| Materialize, inclusive | 4.62 | 3.61 |
| Compact copy-out (subset of materialize) | 0.00 | 0.00 |
| Pool dequeue ops (subset of materialize) | 0.00 | 0.00 |
| Storage diagnostic publication | 3.42 | 0.08 |
| Hold helper + backlog + extra clock | 1.96 | 1.23 |

## Optimization scope

The retained case identified the merged head search as a large avoidable cost.
The new Core API selects a small head ticket once, then consumes that exact FIFO head.
It keeps the original merge key, readiness, watermark and Q rules.

A serialized scheduler owner holds the ticket across materialize. Other group mutations invalidate the ticket contract.
The API rejects stale or repeated consume operations without model mutation.
A defensive host failure path reclaims an already materialized mbuf and its empty tag exactly once.

M7 diagnostic publication now occurs at most every 64 active steps, or at the next idle step.
END, INVALID and final cleanup force publication. Unchanged idle steps do not read allocator statistics.
The model-drop credit remains visible every step. It does not use the diagnostic publication budget.

Live diagnostic values can lag within that step budget. A preempted owner has no wall-time freshness guarantee.
Final and INVALID values include all completed changes. The original fields and formats remain unchanged.

The callback shell costs roughly 4–7 ns per packet above the backend admission work.
A burst callback requires additional transaction and failure semantics, so this change keeps per-packet admission.
Scalar pool operations and hold checks are smaller costs than the merged head search.
This change defers burst callbacks, bulk allocation and hold-check changes.
It does not change storage layout, registered quotas, workload, composition, deadlines or failure gates.
The registered 90G X node estimate remains 28.045 GiB, with the same 24 GiB EAL reservation.

## Throughput comparison

Both executables used the same seven distinct allowed CPUs, input configuration and population ceiling of 2046.
Each case used 0.2 seconds of warmup and 1.5 seconds of measurement, with three trials per executable.
The test alternated baseline/optimized order between trials. It ran one process at a time, with a 4 GiB address-space limit.
All 24 trials drained, conserved packets and reclaimed both pools. Q drops and other drops were zero.

The table gives medians of the mean throughput of the two directions.
Actual populations can differ with bulk allocation and RX progress. The configured ceiling stays the same.

| Stored bytes | a9b92962 Mpps/dir | Optimized Mpps/dir | Change |
| --- | ---: | ---: | ---: |
| X | 4.853 | 6.398 | +31.8% |
| 64 | 4.750 | 6.201 | +30.5% |
| 512 | 4.010 | 4.911 | +22.5% |
| 1518 | 6.683 | 9.546 | +42.8% |

The fixture keeps four lines, C=400G, Q=1572864, t=2000 ns, delta=0, p=0, S=32768 and burst=64.
Its M7 registration uses offered_bps=400G. These are synthetic fixture values, not the production 100G registration.
X uses equal fresh-packet counts of stored 60/508/1518 bytes, with registered charged lengths 64/512/1522.
The fixed 64/512/1518 cases name stored bytes. Their charged lengths include four additional FCS bytes.

The perf loader enforces logical population credit and caps allocated storage to the fixture population.
The production loader has no such shortcut. No production-size allocation or testbed execution formed part of this comparison.

To reproduce the throughput comparison, prepare each M7 configuration as in [M7 storage](M7-STORAGE.md#software-rate-comparison).
Use separate baseline and optimized executables, each with the Core library from its revision. Alternate their order across three trials.
Set `RATE_CPUS` to seven distinct allowed CPU IDs. Set `RATE_SIZE` and `M7_RATE_CONFIG` for each case.

```sh
prlimit --as=4294967296 --core=0 -- python3 perf/ring_rate.py \
  --io-split --cpus "$RATE_CPUS" "$RATE_BINARY" "$M7_RATE_CONFIG" \
  -- -b "$RATE_SIZE" -n 2046 -w 0.2 -d 1.5
```

## Semantic checks

`m7-baseline-semantics` compares a canonical ledger from the merged baseline with the current worker and engine fixtures.
The baseline fixtures use the old production objects and Core library, with test-only ledger output.
Two baseline runs produced the same 1,339,552 bytes, with SHA-256:

```text
c64b71e25bbb118f340705fd18510c0b05ed0a11b047f41f2d7b1169bb415012
```

The ledger includes packet bytes, every declared packet/model field, worker counters, storage counters, final pools and INVALID causes.
It covers eight sizes, two Q limits, p=0/0.5/1, both directions, END and abort.
It also covers materialize and Core-capacity failures. ABI padding, addresses and real clock samples are not values in the ledger.

The original `m7-legacy-binary` fixture retains its b35a28b0 JSONL and binary SHA-256 references.
Core ticket tests compare every model counter after each consume with the original API.
They also cover watermarks, global limits, sealed Q drain and rejected stale or edited tickets.
Host tests cover the publication budget, idle/END/INVALID/final flush and a rejected consume after materialize.
A combined fixture passes compact and retained packets through materialize, PMD prefix acceptance, refused suffix and terminal timeout.
It checks payload/order/deadlines, strict timeout boundaries, packet conservation and full pool recovery.
