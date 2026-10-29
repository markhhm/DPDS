<!-- SPDX-License-Identifier: Apache-2.0 -->

# DPDS HNG host

This fork of [DPDS](https://github.com/uni-tue-kn/DPDS) uses DPDK I/O and the
[Rust HNG engine](dpds-core/README.md). It preserves the Apache-2.0 license.
The legacy Rust API remains available in the core. This host accepts only `hng-v1`.

Each configured `(ingress_port, VLAN)` selects one line and direction.
Every flow on that line shares one C/Q state. Cable and LEO in both directions
use four independent states. The configuration gives the egress port explicitly.
Sparse DPDK port IDs are valid.

## Build and software checks

Requirements: DPDK 26.03 with the null, pcap, ring, and af_packet PMDs;
libpcap development files when building DPDK; stable Rust; a C compiler; Meson;
Ninja, and Python 3 for the process tests.

```sh
git submodule update --init --recursive
meson setup build
meson compile -C build
meson test -C build --print-errorlogs
```

The Meson tests use real mbufs, an injected TX callback, null PMDs, and actual
pcap/ring/af_packet PMD RX/TX.
They need no PCI devices, hugepages, or root privileges. Test lcores map to CPUs
in the process affinity mask. Runners with fewer than four CPUs share them
between lcores for integrity checks; this is unsuitable for performance measurements.

The tests cover four independent queues, VLAN classification, sparse ports,
the RX frontier, delayed ordinary packets, partial TX, and ownership at shutdown.
Null-PMD checks exercise all layouts with zero RX and zero-filled RX packets.
Those packets exercise classifier rejection. Direct mbuf fixtures cover valid traffic.

## Zero-NIC integrity quickstart

No physical NIC, PCI access, hugepages, or host network changes are needed.
Clone with the core submodule, then install DPDK in any user-writable prefix
with these drivers enabled:

```sh
git clone --recursive https://github.com/markhhm/DPDS.git dpds && cd dpds
curl -fsSLO https://fast.dpdk.org/rel/dpdk-26.03.tar.xz && tar -xf dpdk-26.03.tar.xz
meson setup dpdk-build dpdk-26.03 --prefix="$PWD/dpdk-local" --libdir=lib \
  -Dexamples= -Dtests=false \
  -Denable_drivers=net/null,net/pcap,net/ring,net/af_packet,mempool/ring,bus/vdev
meson compile -C dpdk-build
meson install -C dpdk-build
export PKG_CONFIG_PATH="$PWD/dpdk-local/lib/pkgconfig${PKG_CONFIG_PATH:+:$PKG_CONFIG_PATH}"
export LD_LIBRARY_PATH="$PWD/dpdk-local/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
meson setup build
meson compile -C build
meson test -C build io-oracle io-pcap io-ring io-ring-stepped io-af-packet --print-errorlogs
```

The first emulator run on two `net_null` software ports is shown under
[Run with software ports](#run-with-software-ports).

The `dpdk-26.03` source directory must be extracted from the official DPDK
release archive. DPDK's build also needs its standard development prerequisites,
including pkg-config, Python pyelftools, and the libpcap headers/library. Run `pkg-config --exists --print-errors libpcap`
before configuring DPDK and `pkg-config --exists --print-errors libdpdk` before
configuring this host to diagnose missing development dependencies.
The CI workflow builds this configuration and runs the same Meson suite.
While the core submodule is private, configure the optional Actions secret
`DPDS_READ_TOKEN` with read-only contents access to both repositories. The default
workflow token suffices once the submodule is public. No credential is stored in fixtures.

Public test commands, also available individually:

```sh
./build/io-oracle-test
python3 tests/io_pmd.py pcap ./build/io-pmd-test
python3 tests/io_pmd.py ring ./build/io-pmd-test
python3 tests/io_pmd.py ring-stepped ./build/io-pmd-test
python3 tests/io_pmd.py af_packet ./build/dpds
```

`pcap` checks 32 seeds, each with two 1024-packet directional captures and both
VLAN 100 (cable) and VLAN 101 (LEO). Captures and output paths are unique to each
run. Capture timestamps deliberately do not encode arrival pacing. The RX
harness injects arrival time and physical-port sequence from synthetic payloads.

`ring` runs one process on four lcores. Main supplies and checks traffic through
companion net_ring ports. Three other lcores own RX, the production scheduler,
and production TX. Each of 32 seeds runs 153 independent 2048-packet epochs:
**10,027,008 numbered input packets** total. One engine and scheduler owner persist
through all 153 epochs of each seed. Physical-port sequence, arrival clock, and
per-line selector draw advance throughout the seed. The one-millisecond epoch
gaps empty payload storage and bound memory. Only the final epoch seals input
and drains the service ledger before resetting for the next seed. IDs stay
unique across all epochs. This is an integrity check, not a sustained-load benchmark.

In `ring`, each epoch admits all input before the fixture clock advances, so every
packet is due at once. `ring-stepped` runs the same seeds, epochs and oracle with
the clock trailing the newest consumed arrival by 0-4 us: departures interleave
with arrivals, the scheduler chooses among partially due line heads under the
input watermark, and 64-entry source and RX handoff rings are filled until their
producers block before each epoch drains. Its PASS lines add per-seed counts of
interleaved and partially due steps and of full-ring events.

The independent C oracle uses a finite L2-byte service ledger, exact rational
wire serialization, deterministic selector, and a global stable sort by release
time and physical ingress sequence. It never calls Rust. Every received output
ID, egress mapping, VLAN, and full frame must match. Golden tests cover Q fit,
release at equality, dropped selection draws, tied deadlines, allowed inversion,
fractional service, and selector vectors. Corruption checks reject changed IDs,
VLANs, lengths, payload bytes, and swapped output order.

PASS lines include seed, epoch count, cumulative input/output/Q-drop counts,
`every_ID_bytes_order=PASS`, `conservation=PASS`, and `drained=PASS`. Checks also
reconcile RX, handoff, core per-line packet/byte counters, TX, receiver, and pool
reclamation. Any assertion or timeout fails the test; deliberate Q drops are
reported separately from infrastructure loss.

`af_packet` requires Linux `unshare`, `ip`, unprivileged user/network namespaces,
and namespace-local CAP_NET_ADMIN/CAP_NET_RAW. Its probe creates a disposable
veth pair. If that actual probe fails, the test exits 77 with the failure reason
and Meson records SKIP. A successful probe runs a 64-packet bidirectional smoke
through two af_packet ports and production application RX/worker/TX, checks all
output frames, and checks shutdown conservation. The observer reconstructs any
kernel-stripped VLAN from PACKET_AUXDATA before comparing bytes. Namespace
smoke sends the next frame only after both directional receivers see the current
frame, so legal cable/LEO overtakes under CPU contention cannot invalidate its
simple physical sequence check. The pcap/ring oracle checks model-permitted reordering.
Namespace
background frames are rejected by the classifier and explicitly counted as
`background_rejected`; they are included in software conservation.
All interfaces disappear when
the namespace exits. PMD, payload, or application failures are test failures.

## Run with software ports

```sh
cd build
./dpds -l 0-3 --no-pci --no-huge --no-telemetry \
  --vdev=net_null0,no-rx=1 --vdev=net_null1,no-rx=1 \
  -- -c config.json -o stats.txt
```

Use `Ctrl-C` to stop RX and drain the pipeline. The optional output file must
not exist. The host prints final text counters and pool ownership counts.
It exits with failure if a resource failure, terminal TX failure, or conservation
failure makes the run invalid.

Classifier rejection has a separate counter.
`DRAINED` identifies software custody completion. It does not identify a G2 pass.

| lcores | Layout |
| --- | --- |
| 2 | RX, worker, and TX share one worker core |
| 3 | RX has one core; worker and TX share another core |
| 4 | RX, worker, and TX each have one core |

The main lcore waits for completion. It reads counters after the worker cores exit.

## Configuration and packet contract

Use [config.json](config.json) for a small software example. Use
[HNG-DESIGN.md](dpds-core/docs/HNG-DESIGN.md) for the full contract.
The Rust constructor validates the model and storage envelope before RX starts.
The host also validates port availability before port initialization.
Legacy JSON fields do not select HNG parameters.

The baseline accepts one retained 802.1Q tag and one mbuf segment, with an MTU
of 1500 bytes. It rejects unknown VLAN IDs, untagged frames, stacked tags,
stripped tags, and unsupported lengths. It preserves VLAN bytes.
RX and TX use one queue per port, with offloads disabled.
Q charges `max(pkt_len + 4, 64)` L2 bytes. C charges another 20 wire bytes.

RX captures an integer timestamp relative to the run epoch and a physical-port
sequence before handoff. Each line has a separate selection sequence in Rust.
The worker uses a producer frontier only after it consumes all earlier handoff
records. An empty RX poll can advance the software frontier. This clock describes
software RX observation. It cannot establish hardware wire time.

The worker merges line heads by `(release_ns, ingress_seq)` on each egress.
TX preserves an unsent suffix and checks the actual-time trace gate on every
attempt. A held LEO line permits safe cable output on the same port.
The host records actual submit lateness against the original model deadline.
A missed-deadline outage discard is a non-model failure.

## Resource and shutdown bounds

Each ingress port has a pool on its NIC NUMA node, or the current node for a
software port. Its object count includes all configured storage slots on that
port, plus ring, cache, and burst reserves. Descriptor preflight runs before
pool allocation. The pool includes the adjusted RX count and adjusted TX counts
for each distinct mapped egress. Port setup uses those same descriptor counts.

Startup prints the actual object footprint and NUMA node. It does not reserve a fixed 32 GiB.
Cross-NUMA egress DMA remains possible.

The RX producer waits for handoff space before another burst. An unexpected
short ring enqueue invalidates the run and counts its discarded suffix.
The TX backlog permits 256 packets per line. An exhausted backlog invalidates
the run.

The one-second TX retry bound starts after the first unsuccessful admission.
Actual-time TX outage holds and scheduler shutdown have a five-second bound.
The pending TX bound is separate from modeled outage storage in Rust.
These bounds are software defaults, not model Q loss.

Shutdown closes RX first, then drains RX handoffs, the core, TX handoffs, and
TX suffixes. A timeout counts every reclaimed packet under a terminal reason.
Port cleanup precedes the final pool ownership check. The host reports packet
and byte conservation, per-line core counters, and raw NIC counters separately.
A TX-accepted count does not prove receiver delivery.

Software checks do not qualify 10G/100G throughput, G2 timing, NIC timestamps,
physical outage boundaries, or hardware receiver evidence. The software PMD
checks below establish software integrity only.

## License

Independent M2 reference/reporting software and its limitations are documented
in [tools/g2/README.md](tools/g2/README.md). Meson includes its million-event
core comparison and injected-fault checks. All results remain software-only.

Licensed under the [Apache License, Version 2.0](LICENSE).

## Checks, pre-push hook and CI

`scripts/check.sh` is the complete check suite; the pre-push hook and GitHub
Actions (`.github/workflows/ci.yml`) run this same script. It runs the core's
open-source policy scan on this repository (SPDX headers; no lab hostnames,
personal paths or private IP addresses; upstream exemptions in
`scripts/spdx-exempt.txt`, accepted hits in `scripts/env-allow.txt`), then the
pinned core's own `scripts/check.sh` (policy, fmt, clippy, tests), then the
complete Meson suite. `scripts/check-skips.py` prints every Meson SKIP and fails
unless that test is listed in `DPDS_ALLOW_SKIP`; CI allows only `io-af-packet`,
which skips on runners without unprivileged user namespaces, and reports such a
skip as a warning, never as a pass. The script fails if a check modified the
working tree. It locates cargo from `CARGO`, then `PATH`, then the rustup default.

Install the versioned hook after cloning with the submodule:

```sh
./scripts/install-hooks.sh
```

The hook checks a clean checkout of the exact commit being pushed. Check out each
commit before pushing another branch. Deletions need no build. A check failure
blocks the push; fix the failure or the missing tool. Never bypass hooks. The
installer refuses to replace a different existing hook. Keep the DPDK pkg-config
and runtime library paths available in the shell that performs the push.
