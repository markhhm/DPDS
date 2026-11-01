<!-- SPDX-License-Identifier: Apache-2.0 -->
# G-short trace-condition registration

The matrix registers 60 trace conditions: 36 single-LEO conditions and 24
cable-plus-LEO conditions. Each has two repeats and a computed episode count.
This matrix is a plan. No physical capture or hardware acceptance is included.

The repository includes the [generator](../tools/g2/register_m6_trace.py),
[trace-only registration](../examples/m6-trace-conditions.json) and this document.
**Do not commit `examples/trace-profiles/`, conversion logs, or private receipts.**
Measured-source profiles remain external. The matrix contains only provenance,
hashes, aggregate counts and acquisition metadata.

## Generate

Use a Python environment with NumPy and the adopted Core converter. Set RAW to
the paired original Garcia ZIP directory and M3_EVIDENCE to the external M3
evidence directory containing `pool-11s.json` and `tolerance-sweep/10000ns/`.
OUTPUT must be an external directory with no existing `examples` subdirectory.

```sh
OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 \
  prlimit --as=4294967296 -- python3 -B tools/g2/register_m6_trace.py \
  --core "$CORE" --raw "$RAW" --m3-evidence "$M3_EVIDENCE" --out "$OUTPUT"
```

Profiles are generated one at a time, each under the same 4 GiB address-space
limit. The four profiles use the registered paired source
`2025-04-16-09-26-16`, [38,49) seconds, both directions and corridors A/B.
Both directions passed canonical M3 fidelity. Intervals retain the 0.01 ms
converter tolerance and 1,000 ns boundary/latency quantization.

The original canonical profiles registered only 1 ms queue drain. M6 uses an
8 MiB Q, so these generated profiles register 20 ms instead. This covers the
conservative Q drain at C=4.5 Gbit/s with q_min=64 bytes (19,573,419 ns), and
therefore every faster registered line. The generator checks that every profile
byte other than the conversion recipe and terminal horizon matches the original
canonical profile. It neither splices nor extends the natural 11 s exposure.

## Conditions and budget

- Single LEO: corridors A/B × forward/reverse/both × L0/L8/L9 × U/X = 36.

- Cable plus LEO: corridors A/B × forward/reverse/both × calibration/stress
  × U/X = 24.

- Forward uses UL. Reverse uses DL. Port mapping remains a capture configuration
  responsibility. Both directions reuse the same paired source window/phase.

- L0: C=10G, R=9G. L8: C=8G, R=10G. L9: C=9G, R=10G, per active line/direction.

- Pair calibration applies the G-short 9G physical offer as R=4.5G per line,
  with C=10G per line. Pair stress retains R=5G/C=4.5G per line. Aggregate offer
  stays at 9G or 10G per physical direction.

  Cable delay is 65 ms for A and
  100 ms for B. The zero t/delta/p fields describe the LEO model.
  Its trace supplies delay. The paired cable uses its separately declared fixed t.

- L0/calibration trace lines use the registered per-packet reference OWD
  (`release_ns - arrival_ns`), including modeled hold/clamp, as in stress mode.
  Fixed lines keep `t + I*delta`. Timing thresholds and calibration checks are unchanged.

- U uses q=1522/wire=1542 bytes. X uses equal packet counts at q=64/512/1522
  with 64 flows. Budgeting conservatively uses wire=1542 bytes for both profiles.

- Assumed surviving rate is min(offered rate, C), per line/direction. The main
  sample rate is surviving_bps / (8 × 1542 × 1024).

- f is the full 11 s source late share, using the full-source, first-send-anchored
  15 s median baseline and strict excess >20 ms. It is 2130/41110 for UL and
  366/41087 for DL. The exact episode count is
  min(20, ceil(431 / (main_rate × 9 × f))). Both-direction conditions use the
  larger direction requirement. Repeats remain separate.

- Each episode has 2 s warmup, 9 s steady exposure, 1 s drain and 5 s reset.
  Repeating this window does not add independent source diversity.

At 8G and f=0.5%, the exact formula with 1542 wire bytes and target 431 gives
**16 episodes**.

## Per-episode acceptance (adopted 2026-11-01)

The maximum remains 20 episodes. Every registered episode must pass separately.
Do not combine episodes or repeats to manufacture a passing timing population.
Timing median and p99 use that episode's measured main samples, with the existing
368-pair minimum and confidence-interval gates. Order and storage gates retain
their full-identity/conservation checks, including unsampled packets.

Each 9 s episode has an estimated 3,206 main pairs at 4.5G or 5,700 at 8G.
At 9G the estimate is 6,412. These figures apply to each active line/direction
with the conservative MTU wire size.
These estimates exceed 431. Actual measured counts, calibration, capture
integrity and all four gates still determine each episode's verdict.

Late subsets are diagnostics. A late subset below 368 pairs cannot establish a bounded conditional p99.
Report diagnostic insufficiency. This does not make an otherwise valid
main-population episode inconclusive or failed.

The source steady counts (UL 1280/33660, DL 62/33766) and the old late-budget estimates
remain explicit diagnostic metadata. They do not require 37-74 episodes or a
new aggregation implementation. The original 2-16 episode counts, window,
warmup, sampling and profiles are unchanged.

## Existing matrix reader

`examples/m6-trace-conditions.json` is the committed trace-only snapshot.
The generator writes the same matrix to the external `examples/m6-short.json`.
Both use the existing `hng-m6-matrix-v1` schema. All
condition/window/budget metadata is inside `plan`. Each `runs` entry contains
only the supported `id`, `family`, and `manifest` fields. It is a trace-only
inventory, not a replacement for the fixed main/boundary inventory.

For an actual report, acquire each registered run and replace/materialize its
relative `captures/<id>/manifest.json` path with sealed evidence. The manifest's
run and scope must match. The generated software-only scope is not permission
to label evidence physical. Use the appropriate registered capture scope.

```sh
prlimit --as=4294967296 -- python3 -m tools.g2.matrix \
  "$OUTPUT/examples/m6-short.json" --out "$REPORT_OUTPUT"
```

The reader evaluates existing captures. It does not acquire traffic, allocate
episode durations or pool episode samples. Missing captures remain invalid or
INCONCLUSIVE. Each capture must satisfy its own main-population gates and the
matrix requires all episodes to pass. This generator fabricates no capture manifests or physical PASS results.
