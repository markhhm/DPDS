<!-- SPDX-License-Identifier: Apache-2.0 -->

# Changelog

All notable changes to this fork are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/). No version of the HNG
fork has been released yet.

## [Unreleased]

### Added

- HNG host: VLAN-line classification onto four independent C/Q states, RX
  frontier, release-ordered TX with suffix retention, bounded pools and
  conservation accounting at shutdown, on top of the `dpds-core` HNG engine.
- Null-PMD process tests and C unit tests for worker, TX, lifecycle, reorder,
  trace TX, ledger, rings and recovery.
- Real pcap and net_ring PMD checks against an independent finite-Q and stable
  ordering oracle over 32 synthetic seeds, including 10,027,008 ring inputs.
- `io-ring-stepped`: the same ring check with a stepped fixture clock, so
  departures interleave with arrivals, line heads are partially due, and the
  source and RX handoff rings fill until their producers block.
- Disposable namespace/veth af_packet smoke checks with explicit capability
  skips, byte comparisons and accounted background traffic.
- `scripts/check.sh`, the single check suite run by the pre-push hook and CI:
  policy scan, pinned-core checks, Meson suite, and visible SKIP accounting
  (`scripts/check-skips.py`).
- Versioned pre-push hook and installer.
- GitHub Actions CI running `scripts/check.sh` on virtual PMDs without hugepages.
- Zero-NIC quickstart in the README.
- 7-lcore layout: one RX, scheduler and TX lcore per direction, with the
  `split` equivalence test, `io-ring-io-split` and `io-ring-stepped-io-split`
  oracle runs, and `ring_rate.py --io-split`.

### Changed

- Test lcores map onto available CPUs, including small CI runners; these checks
  do not measure throughput.
- The core submodule is pinned to the integration revision that provides the
  shared policy scan and check script.
- Added SPDX headers to all files introduced by this fork.
