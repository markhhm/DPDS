<!-- SPDX-License-Identifier: Apache-2.0 -->

# Changelog

No HNG release exists yet. All validation is software-only.
Accuracy at 100G remains unverified.

## [Unreleased]

### Added

- M1–M4: integrate virtual-PMD I/O, bounded pools, and the per-direction scheduler. Validation: software-only. ([#4](https://github.com/markhhm/DPDS/pull/4), [Core #2](https://github.com/markhhm/DPDS-Core/pull/2))
- M2: add native capture, counter checks, and the G2 reporter integration. Validation: software-only. ([#3](https://github.com/markhhm/DPDS/pull/3))
- M3: require Core pcap replay in host CI and use trace reference OWD for L0. Validation: software-only. ([#8](https://github.com/markhhm/DPDS/pull/8), [#21](https://github.com/markhhm/DPDS/pull/21), [Core #4](https://github.com/markhhm/DPDS-Core/pull/4))
- M6 runner: report registered matrices sequentially with pcapng mode and verified resume after interruption. Validation: software-only. ([#7](https://github.com/markhhm/DPDS/pull/7), [#16](https://github.com/markhhm/DPDS/pull/16), [#20](https://github.com/markhhm/DPDS/pull/20))
- 7-lcore observation: add native v2 capture and certified dispatch frontiers. Validation: software-only. ([#10](https://github.com/markhhm/DPDS/pull/10), [#18](https://github.com/markhhm/DPDS/pull/18), [#19](https://github.com/markhhm/DPDS/pull/19))
- pcapng: convert same-clock nanosecond captures into G2 event streams. Validation: software-only. ([#13](https://github.com/markhhm/DPDS/pull/13), [#16](https://github.com/markhhm/DPDS/pull/16))
