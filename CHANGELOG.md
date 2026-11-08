<!-- SPDX-License-Identifier: Apache-2.0 -->

# Changelog

No tagged HNG release exists yet. The first public version remains **Unreleased**.
This draft covers `hng/core` through DPDS `659a0d5f` and its pinned Core.
Software checks and capacity estimates do not establish physical accuracy or 100G qualification.

## [Unreleased]

### Added

- HNG host integration: per-line C/Q state, exact wire serialization, deterministic
  selection, registered storage bounds and independent software oracles.
  See the [packet contract](Readme.md#configuration-and-packet-contract).

- Virtual-PMD integrity checks and 2/3/4/5/7-lcore layouts, including separate RX,
  scheduler and TX owners per direction. The legacy Core API remains available.
  ([#4](https://github.com/markhhm/DPDS/pull/4), [#8](https://github.com/markhhm/DPDS/pull/8))

- Core trace replay, canonical input conversion and independent expected-delay
  checks, with required pcap replay in host CI.
  ([#8](https://github.com/markhhm/DPDS/pull/8), [Core #4](https://github.com/markhhm/DPDS-Core/pull/4))

- Native v1/v2 observation, certified frontiers, a C binary consumer and offline
  decode. The physical-PMD opt-in retains the observer evidence contract.
  ([#3](https://github.com/markhhm/DPDS/pull/3), [#10](https://github.com/markhhm/DPDS/pull/10), [#30](https://github.com/markhhm/DPDS/pull/30), [#49](https://github.com/markhhm/DPDS/pull/49))

- Nanosecond pcapng input with capture-health checks, per-flow probe sequences and
  the C `seqcheck` tool for full-capture order/identity checks.
  ([#13](https://github.com/markhhm/DPDS/pull/13), [#32](https://github.com/markhhm/DPDS/pull/32), [#53](https://github.com/markhhm/DPDS/pull/53))

- Sampled timing/Q reports with exact all-arrival model state and direct native
  binary input. Full-packet seqcheck evidence supplies order/storage gates.
  ([#33](https://github.com/markhhm/DPDS/pull/33), [#34](https://github.com/markhhm/DPDS/pull/34), [#35](https://github.com/markhhm/DPDS/pull/35))

- M6 matrix reports, registered pcapng mode, hash-verified resume, judge shards,
  and sealed-result retention with explicit dry-run/apply raw pruning.
  ([#7](https://github.com/markhhm/DPDS/pull/7), [#16](https://github.com/markhhm/DPDS/pull/16), [#20](https://github.com/markhhm/DPDS/pull/20), [#54](https://github.com/markhhm/DPDS/pull/54), [#55](https://github.com/markhhm/DPDS/pull/55))

- Quarter-rate M6 matrix inputs, a preserved full-rate baseline, and selected
  short full-rate conditions with observer-cost qualification inputs.
  ([#44](https://github.com/markhhm/DPDS/pull/44), [#60](https://github.com/markhhm/DPDS/pull/60), [#62](https://github.com/markhhm/DPDS/pull/62))

- M5 bring-up manifests, clock/bypass qualification tools, noninteractive traffic
  control, and operator input/runbooks for M5, M6 and M7.
  ([#36](https://github.com/markhhm/DPDS/pull/36), [#37](https://github.com/markhhm/DPDS/pull/37), [#66](https://github.com/markhhm/DPDS/pull/66), [#67](https://github.com/markhhm/DPDS/pull/67))

- Configuration-only M7 STL probe and long-RTT ASTF examples. Hardware generator
  capacity remains unverified.
  ([#15](https://github.com/markhhm/DPDS/pull/15), [traffic inputs](examples/m7-traffic/README.md))

- Explicit M7 hybrid storage: compact small frames, retained large mbufs,
  registered composition quotas and a bounded handoff hold. Classic defaults remain unchanged.
  ([#70](https://github.com/markhhm/DPDS/pull/70), [#71](https://github.com/markhhm/DPDS/pull/71))

### Changed

- Use model-aware backpressure before TX handoff. Unsent packets retain their
  original model order and deadlines. Add loop-gap, ring-peak and failure diagnostics,
  clock epoch anchors and per-port close timestamps.
  ([#68](https://github.com/markhhm/DPDS/pull/68), [#69](https://github.com/markhhm/DPDS/pull/69))

- Request 4096 RX descriptors and retain 1024 TX descriptors, with accepted PMD
  counts in pool planning. Larger 100G descriptor budgets remain preparation estimates.
  ([#57](https://github.com/markhhm/DPDS/pull/57), [M7 inputs](docs/m7/inputs.md))

- Reduce measured M7 overhead with one selected-head ticket and periodic
  diagnostic publication. Idle, END, INVALID and final cleanup force exact pending counts.
  ([#72](https://github.com/markhhm/DPDS/pull/72), [Core #13](https://github.com/markhhm/DPDS-Core/pull/13))

### Fixed

- Prevent expired TX retries from rejoining fresh bursts and repair observer
  snapshot races with independent writer ownership and mailbox publication.
  ([#1](https://github.com/markhhm/DPDS/pull/1), [#9](https://github.com/markhhm/DPDS/pull/9))

- Handle single-direction trace END, trace L0 expected delay, and missing
  frontier/dispatch evidence in M6 reports.
  ([#17](https://github.com/markhhm/DPDS/pull/17), [#18](https://github.com/markhhm/DPDS/pull/18), [#19](https://github.com/markhhm/DPDS/pull/19), [#21](https://github.com/markhhm/DPDS/pull/21))

- Repair pcapng/native pairing and mixed-capture scans, and preserve both
  directions in direct-binary reports. Improve software reporter runtime.
  ([#23](https://github.com/markhhm/DPDS/pull/23), [#28](https://github.com/markhhm/DPDS/pull/28), [#50](https://github.com/markhhm/DPDS/pull/50), [#51](https://github.com/markhhm/DPDS/pull/51), [#52](https://github.com/markhhm/DPDS/pull/52))

- Correct process RSS accounting and wait behavior for offline judging on macOS.
  ([#59](https://github.com/markhhm/DPDS/pull/59), [#63](https://github.com/markhhm/DPDS/pull/63))
