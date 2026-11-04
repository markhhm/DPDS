<!-- SPDX-License-Identifier: Apache-2.0 -->

# CX-4 100G operator handoff

Use the [DPDS input sizing and formula-derived engine files](inputs.md) before allocating M7 pools.

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
| `M7_XSTATS` | Verified DPDK xstat names for periodic reports: the six packet-class counters below and supported PHY/error counters |
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
| `TREX_BIN`, `TREX_CFG`, `M7_TREX_CORES`, `M7_TRAFFIC_MATRIX`, `ACTIVE_VLAN` | Approved generator executable/configuration, qualified data-plane worker count, immutable 100G traffic registration and model VLAN |

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

### RX descriptors and the shortest-frame budget

The engine requests **4096 RX descriptors per port** and one RX queue per port.
It calls `rte_eth_dev_adjust_nb_rx_tx_desc()` and prints `descriptors port=PORT rx=RX tx=TX` with actual values.
Record those accepted values and successful queue setup. Testpmd below also requests 4096, replacing the earlier 1024 request.
This is an experiment setting, not a universal mlx5 requirement or maximum.
The [26.03 PMD](https://github.com/DPDK/dpdk/blob/v26.03/drivers/net/mlx5/mlx5_ethdev.c#L317-L345) derives descriptor limits from device capabilities.

For a 64-byte frame **including FCS**, add 8 bytes preamble/SFD and 12 bytes IFG:

```text
packet_rate = 100,000,000,000 / (8 * (64 + 20)) = 148,809,523.81 packets/s
empty_ring_time_us = accepted_packet_slots / packet_rate * 1,000,000
```

| Nominal packet slots in one RX queue | Time at 100G, 64B |
| ---: | ---: |
| 1024 | 6.88128 us |
| 4096 | 27.52512 us |
| 8192 | 55.05024 us |
| 16384 | 110.10048 us |

These are estimates for an empty, fully replenished queue with one slot per frame.
Occupancy, burst replenishment and mbuf availability reduce the usable margin.
CQE compression adds no packet buffers.

With [MPRQ](https://github.com/DPDK/dpdk/blob/v26.03/drivers/net/mlx5/mlx5_rxq.c#L1848-L1851), the PMD divides descriptors by strides per WQE.
A request of 4096 with 16 strides means nominally 256 WQEs, not 65,536 packet slots.
Record the actual RX path, scatter, stride and CQE settings. Do not assume RSS divides load across queues in this one-queue engine.

**4096 cannot absorb a 100 us RX-poll pause** at this worst-case rate.
If the approved pause budget is 100 us, propose **16384 descriptors** plus margin checks, subject to device limits and a separate engine change.
Recalculate mbufs, pools and NUMA memory before that change. This document does not change the engine's 4096 default or add an option.

Keep storage IRQs and storage workers off the registered RX poller CPUs.
Require measured poll-gap margin and zero RX loss. A deeper ring cannot fix a sustained receive-rate deficit.

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
printf 'show port info all\nshow config fwd\nshow config rxtx\nshow rxq info 0 0\nshow port xstats all\nset promisc all on\n' > "$OUT_DIR/rx.cmd"
printf 'show port info all\nshow config fwd\nshow config rxtx\nshow rxq info 0 0\nshow port xstats all\n' > "$OUT_DIR/tx.cmd"
date --iso-8601=ns > "$OUT_DIR/rate-hw-before-begin.time"
ethtool -S "$PORT0_IF" > "$OUT_DIR/port0-rate-hw-before.txt"
ethtool -S "$PORT1_IF" > "$OUT_DIR/port1-rate-hw-before.txt"
date --iso-8601=ns > "$OUT_DIR/rate-hw-before-end.time"
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
  --socket-num="$PORT1_NODE" --rxq=1 --txq=1 --rxd=4096 --txd=1024 \
  --burst=32 --total-num-mbufs=65536 --forward-mode=rxonly \
  --stats-period=10 --display-xstats="$M7_XSTATS" --cmdline-file="$OUT_DIR/rx.cmd"
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
  --socket-num="$PORT0_NODE" --rxq=1 --txq=1 --rxd=4096 --txd=1024 \
  --burst=32 --total-num-mbufs=65536 --forward-mode=txonly --txpkts=1514 \
  --eth-peer="0,$RX_MAC" --stats-period=10 --display-xstats="$M7_XSTATS" --cmdline-file="$OUT_DIR/tx.cmd"
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
date --iso-8601=ns > "$OUT_DIR/rate-pre-stop-begin.time"
ethtool -S "$PORT0_IF" > "$OUT_DIR/port0-rate-pre-stop.txt"
ethtool -S "$PORT1_IF" > "$OUT_DIR/port1-rate-pre-stop.txt"
date --iso-8601=ns > "$OUT_DIR/rate-pre-stop-end.time"
date --iso-8601=ns > "$OUT_DIR/tx-stop-request.time"
systemctl stop "$TX_UNIT"
sleep "$DRAIN_WAIT_S"
date --iso-8601=ns > "$OUT_DIR/rate-drained-begin.time"
ethtool -S "$PORT0_IF" > "$OUT_DIR/port0-rate-drained.txt"
ethtool -S "$PORT1_IF" > "$OUT_DIR/port1-rate-drained.txt"
date --iso-8601=ns > "$OUT_DIR/rate-drained-end.time"
date --iso-8601=ns > "$OUT_DIR/rx-stop-request.time"
systemctl stop "$RX_UNIT"
date --iso-8601=ns > "$OUT_DIR/rate-hw-after-begin.time"
ethtool -S "$PORT0_IF" > "$OUT_DIR/port0-rate-hw-after.txt"
ethtool -S "$PORT1_IF" > "$OUT_DIR/port1-rate-hw-after.txt"
date --iso-8601=ns > "$OUT_DIR/rate-hw-after-end.time"
journalctl -u "$TX_UNIT" -u "$RX_UNIT" \
  --since="$(cat "$OUT_DIR/rate-start-request.time")" -o short-iso-precise --no-pager \
  > "$OUT_DIR/rate-final.log"
```

Require 100G full-duplex links. Each complete 10-second steady interval needs TX and RX **>=8,127,438 pps**.
The target is `100000000000 / (8 * (1518 + 20))`, approximately 8,127,438 pps.
Here 1518 bytes includes FCS. `txonly --txpkts=1514` supplies that size without FCS.
Compute the interval pps from unicast deltas, not an RX/TX total that includes broadcast or multicast.

This initial direct rate check uses no VLAN insertion.
Require no added errors, RX-missed or RX-nombuf within the load window. Apply the counter rules below.
Do not accept a peak, a sum across ports or missing interval coverage.
Repeat with new outputs, swapped card/node/CPU roles and the other receiver MAC.
Two directional passes do not prove simultaneous bidirectional throughput.

The [M7 STL profile](../../examples/m7-traffic/README.md) instead emits 1518 bytes **without** FCS, with its VLAN.
Its 1522-byte frame has a different rate, about 8.106 Mpps at 100G.
For the 1518-byte target, prepare a private 1514-byte tagged profile and preserve probe identities.
Do not add another VLAN tag or compare those two frame sizes as equal rates.

### mlx5 conservation and the error window

Use named xstats, not fixed numeric IDs or an inclusive RX/TX packet total.
The [DPDK 26.03 source mapping](https://github.com/DPDK/dpdk/blob/v26.03/drivers/net/mlx5/linux/mlx5_ethdev_os.c#L1489-L1536) defines:

| DPDK xstat name | Underlying `ethtool -S` name | Use |
| --- | --- | --- |
| `rx_unicast_packets` | `rx_vport_unicast_packets` | Unicast received |
| `tx_unicast_packets` | `tx_vport_unicast_packets` | Unicast sent |
| `rx_multicast_packets` | `rx_vport_multicast_packets` | Separate background account |
| `tx_multicast_packets` | `tx_vport_multicast_packets` | Separate background account |
| `rx_broadcast_packets` | `rx_vport_broadcast_packets` | Separate background account |
| `tx_broadcast_packets` | `tx_vport_broadcast_packets` | Separate background account |

Require all six names in each port's startup inventory and periodic `M7_XSTATS` reports.
Every configured `M7_XSTATS` name must exist on that card. Freeze the verified list before the load window.
An absent counter is unavailable, not zero. Stop if the required evidence is absent.
Record standard `imissed`, `ierrors`, `oerrors` and `rx_nombuf` alongside them.

Include supported `rx_out_of_buffer`, `rx_phy_crc_errors`, `rx_phy_discard_packets` and `tx_phy_discard_packets` in `M7_XSTATS`.
The [source](https://github.com/DPDK/dpdk/blob/v26.03/drivers/net/mlx5/linux/mlx5_ethdev_os.c#L1538-L1591) maps DPDK `rx_phy_discard_packets` to Linux `rx_discards_phy`.
Do not confuse these two namespaces.

Use before/after deltas under one unchanged counter epoch. Do not reset counters or restart ports within an attempt.
Testpmd's noninteractive SIGTERM exit prints basic statistics, not a final xstats report.
The timestamped `ethtool -S` snapshots above supply raw vPort/PHY baselines, pre-stop values, settled drain values and the final stop tail.
Subtract Linux counters only from Linux counters. Do not subtract a DPDK reset-relative value from a Linux raw value.

Reject a failed snapshot, counter decrease or changed counter set. None is evidence of zero errors.

For the direct test, compare sender TX unicast with receiver RX unicast at matching boundaries.
Retain the in-flight/fill/drain residual and final settled snapshots. Register any boundary tolerance before the run.

For the real engine, reconcile generator TX with DUT RX, and DUT TX with receiver RX.
Reconcile engine ingress separately with delivered packets, registered model drops and final owned=0.
Do not require DUT RX=TX when Q drop is intentional.

The [bifurcated mlx5 statistics](https://doc.dpdk.org/guides-26.03/nics/mlx5.html#statistics) can include kernel traffic in hardware vPort counters.
Keep unrelated unicast absent or independently accounted for. Unicast totals alone cannot identify test flows.

List broadcast and multicast deltas separately. Never add them to the probe denominator to make conservation pass.
Keep PHY, vPort, software-queue and engine counters in their own scopes.
Matching totals do not replace sequence, drop or order checks.

For **testpmd rate checks only**, freeze the load window before the first stop request.
Require timestamped reports that bracket both boundaries and zero added NIC errors throughout that window.
Keep all stop/drain reports and their error deltas in a separate tail account.
A clearly post-stop error does not retroactively fail this load-window rate check.
Unknown error timing, missing coverage or a load-window error cannot use this exception.
Do not describe such a result as a full-run lossless test.

For the **real DPDS engine**, require zero new NIC errors over the entire attempt, through the final drain and stop.
Apply no testpmd tail exception. Reject the attempt if any NIC error counter increases, even outside the steady window.
Registered Q/late drops are model outcomes, not NIC errors.
Keep the existing observer-loss, queue-headroom, conservation and owned=0 gates unchanged.

### Reuse the STL X profile at 100G

The existing [B-prime X profile](../../examples/m5/stl_bprime_x.py) can materialize a registered 100G offer without code changes.
Setting `BPRIME_RATE_BPS=100000000000` alone fails against the default 10G full-rate matrix.
Use `BPRIME_MATRIX_FILE` for an approved immutable M7 matrix with `offered_bps=100000000000` per direction.
Retain its selected ID, `fixed-E3-142-320-1-both-L9-X`, or the current profile cannot select the condition.
The X mix remains equal packet counts at 64/512/1522-byte Q charges, with 20 extra wire bytes per frame.

Set `BPRIME_CONFIG_FILE` to the matching M7 engine JSON, not the X710 engine file.
Register C, Q, t, delta, p, seed, VLAN and the full storage envelope before use.
For the same overload ratio, C is 90G with a 100G offer.

Exact scaling of the old Q/C residence time gives Q=83,886,080 bytes at C=90G.
The [derived M7 inputs](inputs.md#stress-q-and-storage-envelope) use the calculator-compatible
7,456,541 ns allowance and Q=83,886,086 bytes instead, with the sub-nanosecond rounding stated explicitly.
Use that same integer Q in the matching traffic registration and engine JSON.
Keeping the original 8,388,608-byte Q would reduce Q/C tenfold; it is not the derived M7 input.
Do not scale only a rate label or reuse the 10G memory allocation.
Recalculate duration, dense-sequence bounds and capture/judge storage budgets. The old 10G bounds do not describe 100G.

This profile requires fixed, all-late `p="1"` traffic and emits the corresponding selected-late flag.
It is not the zero-delay `p="0"` M5 qualification profile or a general trace generator.
Use the [M7 probe recipe](../../examples/m7-traffic/README.md) for the matching zero-delay case.
Keep generator addresses, one VLAN tag and UDP probe identity settings consistent with each registered model.

```sh
# Generator control interpreter: approved files and rendered server configuration.
export BPRIME_MATRIX_FILE="$M7_TRAFFIC_MATRIX" BPRIME_CONFIG_FILE="$CONFIG_FILE"
export BPRIME_VLAN="$ACTIVE_VLAN" BPRIME_CORES="$M7_TREX_CORES"
unset BPRIME_RATE_BPS  # Derive 100G from the registered matrix.
sha256sum "$BPRIME_MATRIX_FILE" "$BPRIME_CONFIG_FILE" "$DPDS_SRC/examples/m5/stl_bprime_x.py"
# Server launch during the authorized session, after private resource checks:
"$TREX_BIN" -i --cfg "$TREX_CFG" -c "$M7_TREX_CORES"
```

Use multiplier 1 without `total` rate sharing. Each direction independently offers 100G L1.
Keep per-flow sequences dense from zero, one stream/core owner per flow, and `cache_size=0`.
Do not enable a VM packet cache or replicate a flow across workers to obtain a higher rate.

The [TRex manual](https://github.com/cisco-system-traffic-generator/trex-core/blob/master/doc/trex_book.asciidoc) defines `-c` per interface pair, plus two internal threads.
For one pair, **eight data-plane workers plus master/RX CPU allocations** is the initial X-mix planning budget, not a qualified minimum.
Set `BPRIME_CORES` to the actual selectable worker count, not the DUT's seven lcores or Linux CPU IDs.
Keep both directions on the default all-worker mask. A per-port mask change also requires compatible stream pins.

| Both directions at 100G each | Total offer | Average work at 4 DP workers | Average work at 8 DP workers |
| --- | ---: | ---: | ---: |
| Equal-packet X mix | 34.7544 Mpps | 8.6886 Mpps/worker | 4.3443 Mpps/worker |
| All frames 64B including FCS | 297.6190 Mpps | 74.4048 Mpps/worker | 37.2024 Mpps/worker |

Measure the same uncached profile on the actual generator before fixing the worker count.
For measured sustainable aggregate `P_core` packets/s per worker, estimate `ceil(total_pps / (0.8 * P_core))` workers for 20% headroom.
This is a planning model. Shared PCIe, NUMA, queues and memory can prevent linear scaling.
For the X mix, eight workers need at least 5.431 Mpps/worker in that same profile to retain the planned headroom.

The upstream [STL performance range](https://trex-tgn.cisco.com/trex/doc/trex_stateless.html#_high_level_functionality) does not qualify this VM or hardware.
Do not infer all-64B line rate from an X-mix pass. The current 64-flow profile can use at most 64 selectable workers.

Qualify both directions together over the full load window, with stable per-port rate, generator CPU margin and zero unexplained generator/NIC loss.
Insufficient cores or TX capacity blocks the 100G claim. Do not increase a multiplier to hide a generator deficit.
Offline stub tests verify the 100G matrix override, 4/8-worker stream pins, class rates and dense identities only:

```sh
python3 tests/bprime_stl_profile.py
```

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
**Source dependency satisfied:** [#49](https://github.com/markhhm/DPDS/pull/49) integrates all three changes into `hng/core` at `c1629e8`, with Core pin `f3d255d`.
Deploy that integrated revision or a descendant. Confirm `HOST_REV` and `CORE_REV` before hardware use.
Do not bypass the driver gate or use `--observe-software` on hardware.

Stop testpmd before DPDS. Start external captures before the authorized generator.
Use a fresh binary path on real local storage and seven distinct pipeline CPUs.
Reserve CPU time for the encoder and I/O threads outside the six busy data-plane owners.
Seven EAL lcores do not mean seven total OS threads.

The consumer inherits main-thread affinity. Register and verify that placement with the owner.

```sh
# DUT: approved integrated revision and resources, with the common-clock capture active.
date --iso-8601=ns > "$OUT_DIR/engine-hw-before-begin.time"
ethtool -S "$PORT0_IF" > "$OUT_DIR/port0-engine-hw-before.txt"
ethtool -S "$PORT1_IF" > "$OUT_DIR/port1-engine-hw-before.txt"
date --iso-8601=ns > "$OUT_DIR/engine-hw-before-end.time"
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
date --iso-8601=ns > "$OUT_DIR/engine-stop-request.time"
systemctl stop "$DUT_UNIT"
date --iso-8601=ns > "$OUT_DIR/engine-hw-after-begin.time"
ethtool -S "$PORT0_IF" > "$OUT_DIR/port0-engine-hw-after.txt"
ethtool -S "$PORT1_IF" > "$OUT_DIR/port1-engine-hw-after.txt"
date --iso-8601=ns > "$OUT_DIR/engine-hw-after-end.time"
# Analysis host: closed capture copied with its hashes, new output path only.
prlimit --as=4294967296 -- python3 -m tools.g2.native_binary "$OUT_DIR/native.hngb" \
  --out "$OUT_DIR/native.jsonl"
```

Use the [adapter contract](../../NATIVE-OBSERVER.md) for normalized native inputs and sealed manifests.
Apply the full-attempt NIC gate to final engine statistics and both ports' Linux hardware-counter deltas.
Keep one raw-counter epoch across those snapshots. An unavailable snapshot or unexplained reset blocks PASS.

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
