<!-- SPDX-License-Identifier: Apache-2.0 -->

# CX-4 100G operator handoff

The equipment operator executes this procedure during the authorized M7 session.
Run DUT commands locally as root. Do not start traffic before owner approval.
Keep deployment values, inventories, captures and logs outside this public repository.
Use a new `RUN_ID` and output directory for every attempt.

| Variables | Private value or constraint |
| --- | --- |
| `PRIVATE_ENV`, `DPDS_SRC`, `OUT_DIR`, `RUN_ID` | Approved shell environment, source checkout, new evidence directory and attempt ID |
| `HOST_REV`, `CORE_REV`, `TESTPMD`, `DPDS_BIN` | Frozen revisions and absolute approved executables with mlx5 support |
| `PORT0_BDF`, `PORT1_BDF`, `PORT0_IF`, `PORT1_IF` | One Ethernet PF on each of two distinct ConnectX-4 cards and their kernel interfaces |
| `PORT0_NODE`, `PORT1_NODE`, `PORT0_CORES`, `PORT1_CORES` | Each card's NUMA node and at least two allowed CPU IDs local to that card |
| `PORT0_MAIN`, `PORT1_MAIN`, `RX_MAC` | Each probe instance's main CPU and the receiving port's MAC |
| `PORT0_SOCKET_MEM`, `PORT1_SOCKET_MEM` | Each probe's EAL MiB vector, with positive local allocation and zero on other nodes |
| `HUGEPAGE_KB`, `HUGEPAGE_MOUNT`, `PORT0_PAGES`, `PORT1_PAGES` | Approved page size, existing matching hugetlbfs mount and required free local page counts |
| `DUT_SLICE` | Existing owner-approved system slice with access to all registered CPUs and memory nodes |
| `PORT0_VERSION_LOCK`, `PORT1_VERSION_LOCK` | Each card's exact six-field checker version lock, including its firmware |
| `RDMA_CORE_VERSION`, `DOCA_VERSION` | Approved rdma-core/provider release and DOCA release, or explicit `not-used` for DOCA |
| `RDMA_VERSION_COMMAND`, `DOCA_VERSION_COMMAND` | Approved read-only inventory commands for the installed stack and package versions |
| `CONFIG_FILE`, `ZERO_CONFIG`, `LCORE_SET`, `MAIN_CORE`, `DUT_SOCKET_MEM`, `DUT_MEMORY_NODES` | Registered M7 model, zero-delay model, seven distinct pipeline CPUs, main CPU, full EAL MiB budget and approved memory nodes |
| `OBSERVER_CAPACITY`, `PROBE_PORT`, `DRAIN_WAIT_S`, `STOP_TIMEOUT_S` | Approved observer queue capacity (1..65536), probe UDP port and drain/stop bounds |
| `M5_MATRIX`, `M5_REPORT_DIR`, `P1_0_MANIFEST`, `P1_0_QUALIFIED` | Rendered qualification matrix, new report directory and original/new P1-0 paths |
| `GENERATOR_HOST`, `CAPTURE_HOST`, `CAPTURE_CLOCK_ID`, `CAPTURE_MAPPING_ID`, `CAPTURE_CLOCK_SOURCE` | External generator/capture roles and one common capture clock's registration |

## 1. Bring-up checks and 100G between the two cards

### Preserve the mlx5 stack and check each card

Keep `mlx5_core`, `mlx5_ib` and `ib_uverbs` loaded. Keep both PFs bound to
`mlx5_core`. This is the [mlx5 bifurcated path](https://doc.dpdk.org/guides-26.03/platform/mlx5.html#linux-prerequisites).
Do not bind either card to `vfio-pci`. Do not reset, flash or change link type here.
ConnectX-4 Lx is not the 100G ConnectX-4 in this procedure.

Use builds with the mlx5 PMD, PCI bus and matching `libibverbs`/`libmlx5` providers.
The X710-only static bundle does not include this stack. Do not reuse it.
Lock DPDK, firmware, kernel and module versions before the session.
Record the installed RDMA/DOCA combination separately. A version match does not prove compatibility.

```sh
# DUT: Bash, root. Load approved private values before any device access.
set -euo pipefail
set -a
. "$PRIVATE_ENV"
set +a
cd "$DPDS_SRC"
umask 077
mkdir -- "$OUT_DIR"
set -C
[[ "$PORT0_BDF" != "$PORT1_BDF" ]]
[[ $(git rev-parse HEAD) == "$HOST_REV" ]]
[[ $(git -C dpds-core rev-parse HEAD) == "$CORE_REV" ]]
sha256sum "$TESTPMD" "$DPDS_BIN" "$CONFIG_FILE" "$ZERO_CONFIG" > "$OUT_DIR/inputs.sha256"
lsmod > "$OUT_DIR/modules-before.txt"
sh -ec "$RDMA_VERSION_COMMAND" > "$OUT_DIR/rdma-version.txt"
sh -ec "$DOCA_VERSION_COMMAND" > "$OUT_DIR/doca-version.txt"
printf 'rdma-core/provider=%s\ndoca=%s\n' "$RDMA_CORE_VERSION" "$DOCA_VERSION" > "$OUT_DIR/stack-lock.txt"
ip -details -statistics link show dev "$PORT0_IF" > "$OUT_DIR/port0-before.txt"
ip -details -statistics link show dev "$PORT1_IF" > "$OUT_DIR/port1-before.txt"
ethtool -i "$PORT0_IF" > "$OUT_DIR/port0-driver-before.txt"
ethtool -i "$PORT1_IF" > "$OUT_DIR/port1-driver-before.txt"

export DUT_PCI_PORT0="$PORT0_BDF" DUT_PCI_PORT1="$PORT1_BDF"
export DPDK_TESTPMD="$TESTPMD"
(
  export DUT_NODE="$PORT0_NODE" CORE_SET="$PORT0_CORES" SOCKET_MEM="$PORT0_SOCKET_MEM"
  export HUGEPAGES_REQUIRED="$PORT0_PAGES" VERSION_LOCK="$PORT0_VERSION_LOCK"
  export CONFIG_FILE="$ZERO_CONFIG"
  . examples/bringup-cx4.env
  DUT_PCI="$PORT0_BDF" scripts/bringup-check.sh --out "$OUT_DIR/port0-p1.json"
)
(
  export DUT_NODE="$PORT1_NODE" CORE_SET="$PORT1_CORES" SOCKET_MEM="$PORT1_SOCKET_MEM"
  export HUGEPAGES_REQUIRED="$PORT1_PAGES" VERSION_LOCK="$PORT1_VERSION_LOCK"
  export CONFIG_FILE="$ZERO_CONFIG"
  . examples/bringup-cx4.env
  DUT_PCI="$PORT1_BDF" scripts/bringup-check.sh --out "$OUT_DIR/port1-p1.json"
)
```

Use the [CX-4 environment](../../examples/bringup-cx4.env) and
[checker contract](../BRING-UP.md#preflight). Require both tables and manifests to report PASS.
Each card must negotiate **8 GT/s x16**, independently of its advertised `LnkCap`.
Require `mlx5_core` binding and DPDK's `mlx5_pci` driver for these PCI PFs.
The checker validates six exact version fields. It does not compare RDMA/DOCA package versions.

Compare the saved package inventory with the approved stack lock before traffic.
If DOCA is unused, record that fact rather than an invented version.

Confirm CPU locality from each manifest. Cross-NUMA placement requires explicit owner approval.
The seven DUT CPUs and observer threads need their own approved CPU budget.

Concurrent probes must fit the combined free pages, especially when both cards share one node.
Do not reserve pages, remount a shared filesystem or change CPU isolation from these commands.
Environment PASS leaves clock uncertainty null. It is not timing or rate qualification.

Confirm `DUT_SLICE` and its effective CPU/memory sets before launch, as in the [X710 handoff](../m5/em-handoff-x710.md).
Do not replace an active slice configuration or inherit a parent that excludes registered resources.

### Run one directional line-rate check, then reverse

Use an approved isolated 100G connection between the cards for this check.
Stop all DPDS instances first. Select disjoint CPU sets and unique service names.
Each testpmd process owns one card and sees local port ID 0.

The receiver runs `rxonly`. The sender runs `txonly`.
Do not use one global `txonly` mode and assume the other port receives.

The commands below send from card 0 to card 1. Set `RX_MAC` to card 1's MAC.
Use exactly two CPUs per instance here: one main and one forwarding worker.
Confirm the private NUMA/CPU isolation before launch.

```sh
# DUT: same Bash environment. Create only this attempt's transient units.
TX_UNIT="p1-cx4-tx-${RUN_ID}"
RX_UNIT="p1-cx4-rx-${RUN_ID}"
DUT_UNIT="p1-cx4-dut-${RUN_ID}"
printf 'show port info all\nshow config fwd\nset promisc all on\n' > "$OUT_DIR/rx.cmd"
printf 'show port info all\nshow config fwd\n' > "$OUT_DIR/tx.cmd"
date --iso-8601=ns > "$OUT_DIR/rate-start-request.time"
systemd-run --unit="$RX_UNIT" --slice="$DUT_SLICE" --property=LimitMEMLOCK=infinity \
  --property="AllowedCPUs=${PORT1_CORES//,/ }" --property="CPUAffinity=${PORT1_CORES//,/ }" \
  --property="AllowedMemoryNodes=$PORT1_NODE" --property=NUMAPolicy=bind --property="NUMAMask=$PORT1_NODE" \
  --property=KillMode=control-group --property=KillSignal=SIGTERM --property=SendSIGKILL=no \
  --property="TimeoutStopSec=${STOP_TIMEOUT_S}s" --property=Restart=no \
  /usr/bin/stdbuf -oL -eL "$TESTPMD" -l "$PORT1_CORES" --main-lcore="$PORT1_MAIN" \
  --in-memory --file-prefix="${RUN_ID}-rx" -a "$PORT1_BDF" \
  --huge-dir="$HUGEPAGE_MOUNT" --socket-mem="$PORT1_SOCKET_MEM" -- \
  --nb-cores=1 --nb-ports=1 --portlist=0 --port-topology=loop \
  --socket-num="$PORT1_NODE" --rxq=1 --txq=1 --rxd=1024 --txd=1024 \
  --burst=32 --total-num-mbufs=65536 --forward-mode=rxonly \
  --stats-period=10 --cmdline-file="$OUT_DIR/rx.cmd"
# Inspect RX startup, PMD, 100G link and readiness before TX.
journalctl -u "$RX_UNIT" --since="$(cat "$OUT_DIR/rate-start-request.time")" --no-pager

systemd-run --unit="$TX_UNIT" --slice="$DUT_SLICE" --property=LimitMEMLOCK=infinity \
  --property="AllowedCPUs=${PORT0_CORES//,/ }" --property="CPUAffinity=${PORT0_CORES//,/ }" \
  --property="AllowedMemoryNodes=$PORT0_NODE" --property=NUMAPolicy=bind --property="NUMAMask=$PORT0_NODE" \
  --property=KillMode=control-group --property=KillSignal=SIGTERM --property=SendSIGKILL=no \
  --property="TimeoutStopSec=${STOP_TIMEOUT_S}s" --property=Restart=no \
  /usr/bin/stdbuf -oL -eL "$TESTPMD" -l "$PORT0_CORES" --main-lcore="$PORT0_MAIN" \
  --in-memory --file-prefix="${RUN_ID}-tx" -a "$PORT0_BDF" \
  --huge-dir="$HUGEPAGE_MOUNT" --socket-mem="$PORT0_SOCKET_MEM" -- \
  --nb-cores=1 --nb-ports=1 --portlist=0 --port-topology=loop \
  --socket-num="$PORT0_NODE" --rxq=1 --txq=1 --rxd=1024 --txd=1024 \
  --burst=32 --total-num-mbufs=65536 --forward-mode=txonly --txpkts=1514 \
  --eth-peer="0,$RX_MAC" --stats-period=10 --cmdline-file="$OUT_DIR/tx.cmd"
```

Check the service cpusets and memory nodes. An inherited restriction must not exclude registered resources.
After rates stabilize, record a full 60-second steady window.
Retain timestamped statistics before and after both window boundaries.
Wait for the first statistics report after the end timestamp before the stop commands.

```sh
# DUT: after stable rates and owner confirmation.
date --iso-8601=ns > "$OUT_DIR/steady-start.time"
sleep 60
date --iso-8601=ns > "$OUT_DIR/steady-end.time"
# Continue only after the bracketing end report is visible.
systemctl stop "$TX_UNIT"
sleep "$DRAIN_WAIT_S"
systemctl stop "$RX_UNIT"
journalctl -u "$TX_UNIT" -u "$RX_UNIT" \
  --since="$(cat "$OUT_DIR/rate-start-request.time")" -o short-iso-precise --no-pager \
  > "$OUT_DIR/rate-final.log"
```

Require 100G full-duplex links. Each complete 10-second steady interval needs TX and RX **>=8,127,438 pps**.
The target is `100000000000 / (8 * (1518 + 20))`, approximately 8,127,438 pps.
Here 1518 bytes includes FCS. `txonly --txpkts=1514` supplies that size without FCS.

This initial direct rate check uses no VLAN insertion.
Require no added errors, RX-missed or RX-nombuf. Counts must reconcile after drain.
Do not accept a peak, a sum across ports or missing interval coverage.
Repeat with new outputs, swapped card/node/CPU roles and the other receiver MAC.
Two directional passes do not prove simultaneous bidirectional throughput.

The [M7 STL profile](../../examples/m7-traffic/README.md) instead emits 1518 bytes **without** FCS, with its VLAN.
Its 1522-byte frame has a different rate, about 8.106 Mpps at 100G.
For the 1518-byte target, prepare a private 1514-byte tagged profile and preserve probe identities.
Do not add another VLAN tag or compare those two frame sizes as equal rates.

## 2. Qualification with external captures, then M7 observation

Restore the approved measurement topology after the direct cable check.
Use an external generator and an external capture machine. Both are separate from the DUT.
Register the before and receiver feeds through the real DUT path for each direction.
Both feeds must use **one common capture clock**, with one registered device token and mapping.

Two synchronized clocks or two interfaces on one host do not prove a common clock.
See the [physical deployment contract](../M6-RUNNER.md#x710-m6-cable-and-leo-on-separate-vlans).
Use `nic.model="ConnectX-4"`, `nic.pmd="mlx5"` and the actual two-port/family/VLAN declarations.
Do not copy an X710 identity or its 10G rate into the registration.

For M5, prepare four closed pairs: low/100G forward and low/100G reverse.
Use the [qualification matrix shape](../../examples/m5-qualify-x710.json), with CX-4 metadata and private paths.
Render every placeholder before use. Neither the matrix runner nor qualification expands shell variables.
Set `scope="physical"`, `mode="pcapng"`, `workers=1`.

For each native manifest, set sampled verification and `timing_mode="calibration"`.
Use `ZERO_CONFIG`: `t_ns=0`, `delta_ns=0`, `p="0"`, no trace and `c_bps=100000000000` per direction.
Register a distinct low rate and `TARGET_OFFERED_BPS=100000000000` at the external generator.

Preserve C/Q byte accounting and seed in the P1-0 manifest.
Provision the complete pools for the registered frame range and 100G envelope.
Do not lower the model to fit memory. Use the [pool calculator](../../Readme.md#performance-tools).

Retain at least 368 paired main samples per run/line over a nonzero interval.
Keep the capture clock and device stable across loads.
Require complete full-capture identities and healthy native/capture seals.
Keep one VLAN tag and the supported IPv4/UDP probe header in each capture.
Set snaplen from the [pcapng limits](../PCAPNG.md), not from the rate-test traffic.

Use the seven-lcore capture command below for acquisition, with a separate attempt for each load/direction.
Then run this offline command on the analysis host, after acquisition and clean drain:

```sh
cd "$DPDS_SRC"
prlimit --as=4294967296 -- python3 -m tools.g2.m5_qualify "$M5_MATRIX" \
  --out "$M5_REPORT_DIR" --p1-manifest "$P1_0_MANIFEST" --manifest-out "$P1_0_QUALIFIED"
```

Use one analysis process at a time. Stop and retain the error if a full run exceeds its approved memory limit.
Require physical PASS, `physical_qualified=true`, zero identity anomalies and clock uncertainty **<=10,000 ns**.
Also require the original P1-0 environment status PASS for both cards.

Keep per-card manifests and associate each with the unchanged qualified path.
The tool preserves the original environment status. It does not turn an environment FAIL into PASS.
Observer-cost and final G2 decisions remain separate gates.

### Seven-lcore pipeline and physical binary evidence

[#10](https://github.com/markhhm/DPDS/pull/10) supplies per-direction RX/scheduler/TX ownership.
[#30](https://github.com/markhhm/DPDS/pull/30) supplies binary capture.
[#31](https://github.com/markhhm/DPDS/pull/31) supplies the explicit mlx5 physical capture gate.
**Prerequisite:** the deployed host and pinned Core must include all three changes.
At this document's introduction, #31 is on `hng/native-binary`, outside `hng/core`.
Do not run the following command until the measurement owner confirms its integrated revision.
Do not bypass the driver gate or use `--observe-software` on hardware.

Stop testpmd before DPDS. Start external captures before the authorized generator.
Use a fresh binary path on real local storage and seven distinct pipeline CPUs.
Reserve CPU time for the encoder and I/O threads outside the six busy data-plane owners.
Seven EAL lcores do not mean seven total OS threads.

The consumer inherits main-thread affinity. Register and verify that placement with the owner.

```sh
# DUT: only with integrated #31, approved resources and the common-clock capture active.
systemd-run --unit="$DUT_UNIT" --slice="$DUT_SLICE" --property=LimitMEMLOCK=infinity \
  --property="AllowedCPUs=${LCORE_SET//,/ }" --property="CPUAffinity=${LCORE_SET//,/ }" \
  --property="AllowedMemoryNodes=$DUT_MEMORY_NODES" --property=NUMAPolicy=bind --property="NUMAMask=$DUT_MEMORY_NODES" \
  --property=KillMode=control-group --property=KillSignal=SIGTERM --property=SendSIGKILL=no \
  --property="TimeoutStopSec=${STOP_TIMEOUT_S}s" --property=Restart=no \
  --property="StandardOutput=append:$OUT_DIR/dpds.log" --property="StandardError=append:$OUT_DIR/dpds.log" \
  "$DPDS_BIN" -l "$LCORE_SET" --main-lcore="$MAIN_CORE" --in-memory \
  --file-prefix="${RUN_ID}-dut" -a "$PORT0_BDF" -a "$PORT1_BDF" \
  --huge-dir="$HUGEPAGE_MOUNT" --socket-mem="$DUT_SOCKET_MEM" -- \
  -c "$ZERO_CONFIG" -o "$OUT_DIR/final-stats.txt" \
  --observe-binary "$OUT_DIR/native.hngb" --observe-physical \
  --observe-identity stage1 --observe-probe-port "$PROBE_PORT" --observe-capacity "$OBSERVER_CAPACITY"
```

Require `layout=7-lcore rx_scheduler_tx=per-direction` and the expected port order before traffic.
Use the zero-delay configuration for M5. Use a new attempt and registered `CONFIG_FILE` for later M7 loads.
Record the physical raw header, producer loss counters, queue peaks and final drain.
Require zero observer loss, each queue peak below capacity and zero end backlog.
Apply the [observer-cost gate](../BRING-UP.md#observer-cost-gate-before-m6).

The old 0.85 Mpps/direction discard result does not qualify 8.127 Mpps or an actual disk.
Size and measure storage at 100G before the session. Do not extrapolate the 500 MB/s 10G floor as sufficient.
Retain native plus external pcapng spools until offline verification and retention succeed.

Stop the generator, retain external capture through drain, then stop DPDS before offline decode:

```sh
# DUT: after generator stop and the registered drain interval.
sleep "$DRAIN_WAIT_S"
systemctl stop "$DUT_UNIT"
# Analysis host: closed capture copied with its hashes, new output path only.
prlimit --as=4294967296 -- python3 -m tools.g2.native_binary "$OUT_DIR/native.hngb" \
  --out "$OUT_DIR/native.jsonl"
```

Use the [adapter contract](../../NATIVE-OBSERVER.md) for normalized native inputs and sealed manifests.
The decoder checks CRC, sequences and final health. A complete seal alone does not qualify physical clocks.
Do not use testpmd's anonymous traffic for identity qualification or claim a software fixture as M7 PASS.

## 3. Restore the approved initial state

Stop the external generator first. Retain capture until the registered drain completes.
Stop only this attempt's units. Do not kill processes with wildcard selection or force-reset a busy card.

```sh
# DUT: same private environment. Skip units that this attempt never created.
for unit in "$TX_UNIT" "$RX_UNIT" "$DUT_UNIT"; do
  if [[ $(systemctl show "$unit" -p LoadState --value) != not-found ]]; then
    systemctl stop "$unit"
    systemctl show "$unit" -p ActiveState -p SubState -p MainPID -p Result \
      > "$OUT_DIR/$unit-final-state.txt"
  fi
done
ip -details -statistics link show dev "$PORT0_IF" > "$OUT_DIR/port0-after.txt"
ip -details -statistics link show dev "$PORT1_IF" > "$OUT_DIR/port1-after.txt"
ethtool -i "$PORT0_IF" > "$OUT_DIR/port0-driver-after.txt"
ethtool -i "$PORT1_IF" > "$OUT_DIR/port1-driver-after.txt"
lsof -nP +D "$HUGEPAGE_MOUNT"
lsof -nP +L1
```

Inspect commands individually. A permission/tool error is not evidence of resource release.
Require inactive services, no live worker/observer threads and no owned hugepage mappings.
Compare recorded driver, interface and link state with the initial inventory.

Both cards remain on `mlx5_core`. No vfio rebind is part of restoration.
Restore only owner-approved cable, interface or isolation changes from the private change log.
This procedure creates no page reservation or mount. Leave shared hugepages and mounts unchanged.
Keep all failed attempts, raw captures, manifests, version records and hashes for owner review.
