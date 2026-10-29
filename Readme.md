# DPDS HNG host

This fork of [DPDS](https://github.com/uni-tue-kn/DPDS) uses DPDK I/O and the
[Rust HNG engine](dpds-core/README.md). It preserves the Apache-2.0 license.
The legacy Rust API remains available in the core. This host accepts only `hng-v1`.

Each configured `(ingress_port, VLAN)` selects one line and direction.
Every flow on that line shares one C/Q state. Cable and LEO in both directions
use four independent states. The configuration gives the egress port explicitly.
Sparse DPDK port IDs are valid.

## Build and software checks

Requirements: DPDK 26.03 with the null PMD, stable Rust, a C compiler, Meson,
Ninja, and Python 3 for the process tests.

```sh
git submodule update --init --recursive
meson setup build
meson compile -C build
meson test -C build --print-errorlogs
```

The Meson tests use real mbufs, an injected TX callback, and null PMDs.
They need no PCI devices, hugepages, or root privileges. The test EAL uses
logical CPU 0. The process tests also use CPUs 1–3.

The tests cover four independent queues, VLAN classification, sparse ports,
the RX frontier, delayed ordinary packets, partial TX, and ownership at shutdown.
Null-PMD checks exercise all layouts with zero RX and zero-filled RX packets.
Those packets exercise classifier rejection. Direct mbuf fixtures cover valid traffic.

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
physical outage boundaries, or full receiver/oracle evidence. Pcap replay and
hardware checks remain separate acceptance work.

## License

Licensed under the [Apache License, Version 2.0](LICENSE).

## Required pre-push checks

After cloning (and initializing the host submodule), install the versioned hook:

```sh
./scripts/install-hooks.sh
```

The hook runs `meson compile -C build` and `meson test -C build --print-errorlogs`
(initializing `build` when absent, including the pinned core build).
It checks a clean checkout of the exact commit being pushed. Check out each
commit before pushing another branch. Deletions need no build. A check failure
blocks the push; fix the failure or report missing tools to the lead. Never
bypass hooks. The installer refuses to replace a different existing hook.
Keep the build environment (Cargo, Meson, DPDK pkg-config and runtime library
paths) available in the shell that performs the push.
