# DPDS

**DPDS** is a high-performance software **network-link emulator**. It sits
between two network interfaces and reproduces the three effects a real link
imposes on traffic — **delay**, **rate limiting**, and **packet loss** — on a
live packet stream at line rate.

The data plane is built on [DPDK](https://www.dpdk.org/): packets are received,
passed through the emulation pipeline, and transmitted again with zero copies
and no kernel involvement on the fast path. The emulation logic itself lives in
[`dpds-core`](dpds-core/README.md), a Rust library linked in over a small C FFI.
This repository is the DPDK host application that owns the NICs and drives that
library.

## How it works

```
        ┌────────┐   rx_ring   ┌────────┐   tx_ring   ┌──────────┐
NIC ──► │   RX   │ ──────────► │ Worker │ ──────────► │    TX    │ ──► NIC
        └────────┘             └───┬────┘             └──────────┘
                                   │
                            ┌──────▼───────┐
                            │  dpds-core   │  delay · rate-limit · loss
                            │  (Buffer)    │
                            └──────────────┘
```

The pipeline has three stages, each able to run on its own isolated CPU core:

| Stage      | Source            | Responsibility                                                                                    |
| ---------- | ----------------- | ------------------------------------------------------------------------------------------------- |
| **RX**     | `core/receive.c`  | Poll the NIC for incoming bursts and hand them to the worker.                                      |
| **Worker** | `core/worker.c`   | Push each packet into the `dpds-core` buffer, then drain packets whose computed send time is due.  |
| **TX**     | `core/transmit.c` | Transmit ready packets on the outgoing port.                                                       |

The worker delegates *when* (or *whether*) each packet is forwarded to
`dpds-core`; see its [README](dpds-core/README.md) for the delay distributions,
rate-limiter modes (Spacer / Token Bucket Filter), and loss models
(Random / Gilbert–Elliott).

### Core layouts

DPDS adapts to the number of lcores it is given (1 main/monitor core + 1–3
worker cores). Fewer cores fuse stages onto a single thread to save context;
more cores isolate each stage for maximum throughput:

| lcores | Layout                                          |
| ------ | ----------------------------------------------- |
| 2      | RX + Worker + TX combined on one core           |
| 3      | RX on one core; Worker + TX combined on another |
| 4      | RX, Worker, and TX each on a dedicated core      |

### Topology

- **Two NICs** — packets received on one port are emulated and sent out the
  other (the worker flips the port). This is the typical inline / bump-in-the-wire setup.
- **One NIC** — single-port loopback; packets are emulated and sent back out the
  same port.

## Requirements

- **DPDK 26.03** (and a DPDK-bound NIC; tested on Mellanox ConnectX)
- **Rust** (stable toolchain) — builds `dpds-core`
- **Meson** + **Ninja**
- A C compiler with `-march=native` support
- Linux with hugepages configured

A ready-to-use [dev container](.devcontainer/) is included that installs DPDK,
Rust, and the build tooling. Building the app works anywhere; *running* it
requires a Linux host with hugepages and a bound NIC (see the commented
`runArgs` / hugepage mount in `.devcontainer/devcontainer.json`).

## Building

Clone with the `dpds-core` submodule:

```bash
git clone --recurse-submodules <repo-url>
cd dpds
# (or, if already cloned)  git submodule update --init --recursive
```

Configure and build with Meson:

```bash
meson setup build
meson compile -C build
```

This builds `dpds-core` (via Cargo) and the `dpds` executable into `build/`, and
copies `config.json` alongside it.

## Running

DPDS uses DPDK's standard EAL command line. Arguments before `--` configure the
EAL (cores, NIC PCI addresses); arguments after `--` are application options.

```bash
# From the build directory (config.json is read from the working directory)
cd build

# 4-core example, two NICs, writing a CSV stats summary on exit:
sudo ./dpds -l 0-3 -a 0000:17:00.0 -a 0000:17:00.1 -- -o stats.csv
```

- `-l 0-3` — lcores to use (here cores 0–3 → 4-core layout).
- `-a <PCI>` — NIC PCI address(es); pass one for loopback, two for inline.
- `-o <file>` — *(optional)* write a final CSV stats summary on shutdown.

While running, DPDS prints a live per-second dashboard (RX/TX packet rates, burst
histograms, emulator/ring drops, and per-NIC hardware counters). Stop it with
`Ctrl-C`; it drains the buffer and prints cumulative statistics.

## Configuration

The emulation behaviour is controlled by `config.json`, read from the working
directory at startup. The full schema — delay distributions, limiter modes, and
loss models — is documented in
[`dpds-core/README.md`](dpds-core/README.md#configuration), with a complete
example in [`dpds-core/config.example.json`](dpds-core/config.example.json). The
`config.json` in this repository is a working default.

## Performance tuning

For line-rate operation, isolate the emulator's cores from the OS and tune the
NIC. These steps are host-specific and re-applied per boot.

### CPU isolation

Cores 1–4 are isolated from the OS scheduler and used exclusively by the
emulator. Isolation is configured via the kernel boot parameter:

```
isolcpus=1-4
```

Because these cores are isolated, `irqbalance` cannot migrate interrupts onto
them — stopping `irqbalance` is therefore not required.

### NIC tuning (one-time, per host)

The following improve throughput on Mellanox ConnectX NICs and must be
re-applied after a reboot.

#### PCIe MaxReadReq → 1024 B

Increasing the PCIe Maximum Read Request Size reduces the number of round-trips
the NIC needs to fetch TX packet data from system memory.

```bash
# Read the current value (requires root; returns 4 hex digits ABCD)
sudo setpci -s <PCI_ADDRESS> 68.w

# Set to 1024 B: replace the first digit with 3, keep the rest (BCD)
sudo setpci -s <PCI_ADDRESS> 68.w=3BCD

# Verify
sudo setpci -s <PCI_ADDRESS> 68.w   # should now start with 3
```

Example — address `0000:17:00.0` with original value `2934`:

```bash
sudo setpci -s 0000:17:00.0 68.w=3934
```

## Repository layout

```
dpds.c            Application entry point: EAL init, port setup, pipeline launch, stats
core/             Pipeline stages (receive, worker, transmit, combined fused modes)
common/           Shared port init, signal handling, config constants, stats structs
dpds-core/        Rust emulation library (git submodule, C FFI)
config.json       Default emulation configuration
meson.build       Build definition (also drives the Cargo build of dpds-core)
.devcontainer/    Dev container with DPDK + Rust preinstalled
```

## License

Licensed under the [Apache License, Version 2.0](LICENSE).
