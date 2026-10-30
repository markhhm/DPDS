<!-- SPDX-License-Identifier: Apache-2.0 -->

# Changelog

## Unreleased

- Check real pcap and net_ring PMDs against an independent finite-Q and stable
  ordering oracle over 32 synthetic seeds, including 10,027,008 ring inputs.
- Add disposable namespace/veth af_packet smoke checks with explicit capability
  skips, byte comparisons, and accounted background traffic.
- Register the complete software suite with Meson and CI. Map test lcores onto
  available CPUs, including small CI runners; these checks do not measure throughput.
- Adopt the existing M0 pre-push gate. Preserve the core submodule at e9dc335.
