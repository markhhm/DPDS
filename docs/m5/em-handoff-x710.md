<!-- SPDX-License-Identifier: Apache-2.0 -->

# X710 operator handoff

Deployment-specific values come from the operator's private configuration. Keep that
configuration, inventories, captures and resulting logs outside this repository.
Do not run any traffic command until the measurement owner authorizes its start.
All three sections execute **on `DUT_HOST`, as `root`**, using the system manager
for `systemd-run`, never `--user`.

| Variables | Private value or constraint |
| --- | --- |
| `DUT_HOST`, `PRIVATE_ENV`, `DPDS_SRC` | Execution target, approved shell environment file and checked-out source directory |
| `PORT0_BDF`, `PORT1_BDF`, `RX_MAC` | Two X710 functions and the receiver function's MAC; both functions already use `vfio-pci` |
| `ARTIFACT_DIR` | Private location of the `dpdk-26.03-testpmd-static` bundle obtained from the approved shared artifact storage |
| `TESTPMD`, `HUGEDIR`, `OUT_DIR` | `TESTPMD=/root/dpdk/dpdk-testpmd`; private initially unmounted empty hugetlbfs mountpoint and new output directory |
| `DUT_NODE`, `NODE1_CORE_SET`, `CORES`, `MAIN_CORE` | Approved memory node, complete allowed CPU list, seven distinct allowed CPU IDs and one of those IDs for main/filler; comma-separated lists/ranges |
| `TX_CORES`, `TX_MAIN_CORE`, `RX_CORES`, `RX_MAIN_CORE` | Two disjoint pairs of allowed CPU IDs and each pair's main CPU, for independent TX/RX instances |
| `NODE1_MEMORY_MIB`, `PROBE_SOCKET_MEM`, `LOAD_SOCKET_MEM` | Approved memory budget and EAL MiB vectors: zero preallocation on every other node; two probe allocations together fit the budget, load vector registers the maximum concurrent allocation |
| `HUGEPAGES_REQUIRED`, `HUGEPAGE_MOUNT` | Approved free 1 GiB page requirement and mount; set `HUGEPAGE_MOUNT` to `HUGEDIR` |
| `EXPECTED_PCIE_GTS`, `EXPECTED_PCIE_WIDTH`, `VERSION_LOCK` | Approved negotiated link and exact version lock; `pmd=net_i40e`, `driver=vfio-pci` |
| `HOST_REV`, `CORE_REV`, `CONFIG_FILE`, `RUN_ID` | Frozen source revisions, approved engine configuration and unique attempt ID |
| `VLAN_ID` | Empty for isolated untagged access ports; otherwise an approved tag, without changing the total 1518-byte frame size |
| `VFIO_GROUP0`, `VFIO_GROUP1` | Existing device paths for the two registered VFIO groups, for ownership checks |
| `M5_MATRIX`, `M5_OUT`, `P1_MANIFEST`, `QUALIFIED_MANIFEST` | Later closed-capture qualification inputs and new output paths; not required for the initial rate check |

Derived CPU strings below only change commas to spaces for systemd. Validate
actual CPU/node/core topology, page availability and effective cpusets against
the private inventory. Never include another workload's reserved CPUs. Verify
that the two independent probe instances can own their VFIO groups separately;
if the backend cannot split a shared group, stop and resolve that topology.
Do not use an unsafe IOMMU setting to work around it.

## 1. Bring-up checks and a full 60-second line-rate check

**Execute on `DUT_HOST` as `root`; system manager only.** No driver binding,
page reservation changes or traffic generation are part of the initial checker.
Use the supplied generic static DPDK 26.03 executable; no DPDK installation on
the target is required. Before the checker, the operator verifies and installs
the bundle and mounts the existing 1 GiB page pool as shown below. The binary
requires x86-64 with its generic CPU baseline and glibc >=2.38; its only dynamic
dependencies are glibc components. Consult the bundle build record for scope
and software-smoke limits, including the bounded no-huge mbuf fixture.
Load the approved private configuration, create a new output directory and make
`dpds.slice` a sibling of the normal system/user slices:

```sh
set -euo pipefail
set -a
. "$PRIVATE_ENV"
set +a
cd "$DPDS_SRC"
umask 077
mkdir -- "$OUT_DIR"
TESTPMD=/root/dpdk/dpdk-testpmd
export TESTPMD
(
  cd "$ARTIFACT_DIR"
  sha256sum --check SHA256SUMS
  printf '%s  dpdk-testpmd\n' \
    '1891b2a750df538cc67a1fa5a5d1c438127b0ebc4c39e079e9e8ced7299cc63f' | sha256sum --check -
)
install -d /root/dpdk
install -m 0755 "$ARTIFACT_DIR/dpdk-testpmd" "$TESTPMD"
printf '%s  %s\n' \
  '1891b2a750df538cc67a1fa5a5d1c438127b0ebc4c39e079e9e8ced7299cc63f' "$TESTPMD" | sha256sum --check -

# Create this run's mount only; never cover or replace another owner's mount.
mkdir -p -- "$HUGEDIR"
if mountpoint -q -- "$HUGEDIR"; then
  echo 'Stop: choose an unmounted private mountpoint.' >&2
  exit 1
fi
if [ -n "$(find "$HUGEDIR" -mindepth 1 -maxdepth 1 -print -quit)" ]; then
  echo 'Stop: mountpoint must be empty.' >&2
  exit 1
fi
mount -t hugetlbfs -o pagesize=1G none "$HUGEDIR"
printf '%s\n' "$HUGEDIR" > "$OUT_DIR/hugetlbfs-mounted.path"
findmnt -T "$HUGEDIR" -o TARGET,FSTYPE,OPTIONS
SLICE_CPUS=${NODE1_CORE_SET//,/ }
RUN_CPUS=${CORES//,/ }
TX_CPUS=${TX_CORES//,/ }
RX_CPUS=${RX_CORES//,/ }
TX_UNIT="p1-x710-tx-${RUN_ID}.service"
RX_UNIT="p1-x710-rx-${RUN_ID}.service"
B_UNIT="p1-x710-b-${RUN_ID}.service"
FILL_UNIT="p1-x710-b-fill-${RUN_ID}.service"

cat > /etc/systemd/system/dpds.slice <<SLICE
[Unit]
Description=DPDS isolated data plane
[Slice]
AllowedCPUs=$SLICE_CPUS
AllowedMemoryNodes=$DUT_NODE
CPUAccounting=yes
MemoryAccounting=yes
SLICE
systemctl daemon-reload
systemctl start dpds.slice
systemctl show dpds.slice -p Slice -p ControlGroup -p EffectiveCPUs -p EffectiveMemoryNodes
```

Require **`Slice=-.slice`**, the registered CPU set and only the registered memory
node. The slice name establishes the top-level parent; do not put the services
under a restricted system/user slice. Stop if the effective values differ.
Do not replace an existing active slice configuration without the owner's
handoff. `--socket-limit` zero means no EAL limit for that node, not a ban;
`AllowedMemoryNodes` and NUMA binding must exclude all other nodes.

Run the [environment example](../../examples/bringup-x710.env) and
[checker](../../scripts/bringup-check.sh) from the isolated service so its CPU
validation sees the approved affinity. The private environment must contain the
required variables above and the version-lock/configuration files described in
[hardware bring-up](../BRING-UP.md). Each function gets a new P1-0 manifest:

```sh
systemd-run --wait --unit="p1-x710-check-${RUN_ID}" --slice=dpds.slice \
  --property="AllowedCPUs=$SLICE_CPUS" --property="CPUAffinity=$SLICE_CPUS" \
  --property="AllowedMemoryNodes=$DUT_NODE" --property=NUMAPolicy=bind \
  --property="NUMAMask=$DUT_NODE" --property=LimitMEMLOCK=infinity \
  /bin/bash -euc '
    set -a; . "$1"; set +a
    cd "$DPDS_SRC"
    export TESTPMD=/root/dpdk/dpdk-testpmd
    export DUT_PCI_PORT0="$PORT0_BDF" DUT_PCI_PORT1="$PORT1_BDF"
    export HUGEPAGE_MOUNT="$HUGEDIR" DPDK_TESTPMD="$TESTPMD"
    . examples/bringup-x710.env
    DUT_PCI="$PORT0_BDF" scripts/bringup-check.sh --out "$OUT_DIR/port0-p1.json"
    DUT_PCI="$PORT1_BDF" scripts/bringup-check.sh --out "$OUT_DIR/port1-p1.json"
  ' _ "$PRIVATE_ENV"
```

The mount must report hugetlbfs with 1 GiB pages. Mounting does not reserve
new pages; the checker still requires the approved free page count on the
registered node. Keep the mount receipt for cleanup. The bundle also contains
the upstream `dpdk-telemetry.py`; its checksum is included in `SHA256SUMS`.

Both checker invocations must exit zero and report environment PASS. That is
not rate or clock qualification. Preserve failures and use new attempt paths.

Next, after authorization, use **two independent primary instances**: one
TX-only instance for `PORT0_BDF`, one RX-only instance for `PORT1_BDF`.
Each has local port ID **0**; the receiver's local 0 is the second physical
function. Check the device name, `net_i40e`, firmware and 10G full-duplex link
in the startup output. A single global `txonly` forwarding mode cannot also
receive on the other port.

```sh
printf 'show port info all\nshow config fwd\nset promisc all on\n' > "$OUT_DIR/rx.cmd"
printf 'show port info all\nshow config fwd\n' > "$OUT_DIR/tx.cmd"
TXPKTS=1514
if [ -n "${VLAN_ID:-}" ]; then
  TXPKTS=1510
  printf 'port stop all\nport config all tx_offload vlan_insert on\ntx_vlan set 0 %s\nport start all\n' \
    "$VLAN_ID" >> "$OUT_DIR/tx.cmd"
fi
PROBE_PORT_NUMA="(0,$DUT_NODE)"
PROBE_RING_NUMA="(0,3,$DUT_NODE)"
date --iso-8601=ns > "$OUT_DIR/bringup-start-request.time"
systemd-run --unit="$RX_UNIT" --slice=dpds.slice \
  --property="AllowedCPUs=$RX_CPUS" --property="CPUAffinity=$RX_CPUS" \
  --property="AllowedMemoryNodes=$DUT_NODE" --property=NUMAPolicy=bind \
  --property="NUMAMask=$DUT_NODE" --property=LimitMEMLOCK=infinity \
  --property=KillMode=control-group --property=KillSignal=SIGTERM \
  --property=SendSIGKILL=no --property=TimeoutStopSec=30s \
  --property=StandardOutput=journal --property=StandardError=journal \
  /usr/bin/stdbuf -oL -eL "$TESTPMD" -l "$RX_CORES" --main-lcore="$RX_MAIN_CORE" \
  --in-memory --file-prefix="${RUN_ID}-rx" -a "$PORT1_BDF" \
  --huge-dir="$HUGEDIR" --socket-mem="$PROBE_SOCKET_MEM" --socket-limit="$PROBE_SOCKET_MEM" -- \
  --nb-cores=1 --nb-ports=1 --portlist=0 --port-topology=loop \
  --socket-num="$DUT_NODE" --port-numa-config="$PROBE_PORT_NUMA" --ring-numa-config="$PROBE_RING_NUMA" \
  --rxq=1 --txq=1 --rxd=1024 --txd=1024 --burst=32 --total-num-mbufs=65536 \
  --forward-mode=rxonly --stats-period=10 --cmdline-file="$OUT_DIR/rx.cmd"

# Inspect RX readiness before starting TX.
systemd-run --unit="$TX_UNIT" --slice=dpds.slice \
  --property="AllowedCPUs=$TX_CPUS" --property="CPUAffinity=$TX_CPUS" \
  --property="AllowedMemoryNodes=$DUT_NODE" --property=NUMAPolicy=bind \
  --property="NUMAMask=$DUT_NODE" --property=LimitMEMLOCK=infinity \
  --property=KillMode=control-group --property=KillSignal=SIGTERM \
  --property=SendSIGKILL=no --property=TimeoutStopSec=30s \
  --property=StandardOutput=journal --property=StandardError=journal \
  /usr/bin/stdbuf -oL -eL "$TESTPMD" -l "$TX_CORES" --main-lcore="$TX_MAIN_CORE" \
  --in-memory --file-prefix="${RUN_ID}-tx" -a "$PORT0_BDF" \
  --huge-dir="$HUGEDIR" --socket-mem="$PROBE_SOCKET_MEM" --socket-limit="$PROBE_SOCKET_MEM" -- \
  --nb-cores=1 --nb-ports=1 --portlist=0 --port-topology=loop \
  --socket-num="$DUT_NODE" --port-numa-config="$PROBE_PORT_NUMA" --ring-numa-config="$PROBE_RING_NUMA" \
  --rxq=1 --txq=1 --rxd=1024 --txd=1024 --burst=32 --total-num-mbufs=65536 \
  --forward-mode=txonly --txpkts="$TXPKTS" --eth-peer="0,$RX_MAC" \
  --stats-period=10 --cmdline-file="$OUT_DIR/tx.cmd"
```

Discard initialization samples, wait for stable rates, then record the start
of a **complete 60-second steady observation window**. Keep timestamped journal
output; the first stats report can have zero pps. In a separate terminal use
`journalctl -f -u "$TX_UNIT" -u "$RX_UNIT" -o short-iso-precise --no-pager`
redirected to a new private log file. After the window, stop TX first, drain,
then stop RX:

```sh
date --iso-8601=ns > "$OUT_DIR/steady-start.time"
sleep 60
date --iso-8601=ns > "$OUT_DIR/steady-end.time"
# Keep forwarding until the first stats report at/after steady-end.time is saved.
# The owner checks this bracketing report before continuing.
systemctl stop "$TX_UNIT"
sleep 1
systemctl stop "$RX_UNIT"
journalctl -u "$TX_UNIT" -u "$RX_UNIT" \
  --since="$(cat "$OUT_DIR/bringup-start-request.time")" -o short-iso-precise --no-pager \
  > "$OUT_DIR/bringup-final.log"
```

**Rate-check PASS:** for the entire 60-second steady window, every complete
10-second interval has physical port 0 TX **>=812,744 pps** and physical port 1
RX **>=812,744 pps**, correct 10G links/placement and no added NIC errors,
RX-missed or RX-nombuf. After drain, transmitted/received counts reconcile;
keep reports bracketing both endpoints; missing coverage or unverified drain is not PASS. Do not accept one peak or an
aggregate across ports. For a 1518-byte frame including FCS, wire accounting is
`10^10 / (8 * (1518 + 20))`, approximately 812,744 pps; NIC byte-rate statistics
exclude some wire overhead. This DPDK version's txonly length excludes FCS,
whereas flowgen subtracts four bytes from its configured length; preserve total
frame size when adding a hardware VLAN tag.

With no capture machine, stop here: this is only environment/rate evidence.
Once closed directional captures, native evidence, physical clock calibration
and a registered zero-delay matrix exist, run the separate
[M5 qualification](../../tools/g2/m5_qualify.py) on `DUT_HOST`, as `root`:

```sh
python3 -m tools.g2.m5_qualify "$M5_MATRIX" --out "$M5_OUT" \
  --p1-manifest "$P1_MANIFEST" --manifest-out "$QUALIFIED_MANIFEST"
```

Keep outputs separate for each registered function/manifest. Read
`qualification.json`; only physical PASS qualifies the measured clock path.
Do not infer timing qualification from testpmd port statistics.

## 2. Authorized A/B/A B phase: seven busy cores and bidirectional 10G

**Execute on `DUT_HOST` as `root`, through the system manager.** Start only on
the measurement owner's notification; stop only on its stop notification.
Keep the maximum load running for the **whole** B measurement phase, without a
60-second timer, automatic restart or rate reduction. The two probe services
must already be stopped. Use the same isolated top-level `dpds.slice`
(`Slice=-.slice`) and private CPU/memory limits.

This is a **testpmd load surrogate for DPDS**: the registered core count, NIC,
PCIe path and wire rate match the planned concurrent load. It is **not DPDS's
actual memory-access pattern**, delay-store, C/Q model or cache working set.
The owner must confirm that this scope represents the intended maximum load;
a later larger working set/core count/rate is not qualified by this run.

```sh
printf 'show port info all\nshow config fwd\nset promisc all on\n' > "$OUT_DIR/flowgen.cmd"
FLOWGEN_PKTS=1518
if [ -n "${VLAN_ID:-}" ]; then
  FLOWGEN_PKTS=1514
  printf 'port stop all\nport config all tx_offload vlan_insert on\ntx_vlan set 0 %s\ntx_vlan set 1 %s\nport start all\n' \
    "$VLAN_ID" "$VLAN_ID" >> "$OUT_DIR/flowgen.cmd"
fi
LOAD_PORT_NUMA="(0,$DUT_NODE),(1,$DUT_NODE)"
LOAD_RING_NUMA="(0,3,$DUT_NODE),(1,3,$DUT_NODE)"
date --iso-8601=ns > "$OUT_DIR/B-start-request.time"
systemd-run --unit="$B_UNIT" --slice=dpds.slice \
  --property="AllowedCPUs=$RUN_CPUS" --property="CPUAffinity=$RUN_CPUS" \
  --property="AllowedMemoryNodes=$DUT_NODE" --property=NUMAPolicy=bind \
  --property="NUMAMask=$DUT_NODE" --property=LimitMEMLOCK=infinity \
  --property=KillMode=control-group --property=KillSignal=SIGTERM \
  --property=SendSIGKILL=no --property=TimeoutStopSec=30s \
  --property=StandardOutput=journal --property=StandardError=journal \
  /usr/bin/stdbuf -oL -eL "$TESTPMD" -l "$CORES" --main-lcore="$MAIN_CORE" \
  --in-memory --file-prefix="${RUN_ID}-b" -a "$PORT0_BDF" -a "$PORT1_BDF" \
  --huge-dir="$HUGEDIR" --socket-mem="$LOAD_SOCKET_MEM" --socket-limit="$LOAD_SOCKET_MEM" -- \
  --nb-cores=6 --nb-ports=2 --portlist=0,1 --port-topology=loop \
  --socket-num="$DUT_NODE" --port-numa-config="$LOAD_PORT_NUMA" --ring-numa-config="$LOAD_RING_NUMA" \
  --rxq=4 --txq=4 --rxd=1024 --txd=1024 --burst=32 --total-num-mbufs=262144 \
  --forward-mode=flowgen --flowgen-flows=64 --txpkts="$FLOWGEN_PKTS" \
  --stats-period=10 --cmdline-file="$OUT_DIR/flowgen.cmd"
systemctl is-active --quiet "$B_UNIT"
systemd-run --unit="$FILL_UNIT" --slice=dpds.slice \
  --property="AllowedCPUs=$MAIN_CORE" --property="CPUAffinity=$MAIN_CORE" \
  --property="AllowedMemoryNodes=$DUT_NODE" --property=NUMAPolicy=bind \
  --property="NUMAMask=$DUT_NODE" \
  --property="BindsTo=$B_UNIT" --property="After=$B_UNIT" \
  --property=KillMode=control-group --property=KillSignal=SIGTERM \
  --property=SendSIGKILL=no --property=TimeoutStopSec=30s \
  /bin/sh -c 'while :; do :; done'
```

Six forwarding workers plus a filler on the otherwise sleeping main CPU keep
exactly the seven selected CPUs busy; no eighth CPU is added. Four RX/TX queues
per port provide enough streams for all six workers. Confirm the actual active
worker map and per-CPU utilization throughout B; a seven-entry EAL list alone
does not prove seven busy CPUs. Confirm both ports really receive as well as
transmit. This version's flowgen MAC/flooding behavior also needs isolation
review; do not change the switch from these instructions.

In a separate terminal, record each 10-second port-stats report with actual
journal timestamps into the private file (Ctrl-C stops this logger, not load):

```sh
journalctl -f -u "$B_UNIT" -u "$FILL_UNIT" \
  --since="$(cat "$OUT_DIR/B-start-request.time")" -o short-iso-precise --no-pager \
  > "$OUT_DIR/B-live.log"
```

After initialization, confirm seven busy CPUs and full bidirectional rates,
record `B-ready.time`, and let the owner begin its B measurements. For **every**
complete 10-second report in that phase, each port's **TX and RX** must be at
least **99% of 812,744 pps**, or **804,617 pps** after rounding the minimum up.
Never hide a deficient report in an average. Record its timestamp, port,
direction, observed pps and counters, and report it to the measurement owner.
Missing reports, service failures and CPU/load gaps are also recorded.
**The measurement owner decides the B-phase/A/B/A result**, not this rate monitor.

On the stop notification:

```sh
date --iso-8601=ns > "$OUT_DIR/B-stop-request.time"
systemctl stop "$B_UNIT" "$FILL_UNIT"
date --iso-8601=ns > "$OUT_DIR/B-stopped.time"
systemctl show "$B_UNIT" "$FILL_UNIT" -p ActiveState -p Result \
  -p ExecMainStartTimestamp -p ExecMainExitTimestamp -p CPUUsageNSec \
  > "$OUT_DIR/B-unit-state.txt"
journalctl -u "$B_UNIT" -u "$FILL_UNIT" \
  --since="$(cat "$OUT_DIR/B-start-request.time")" -o short-iso-precise --no-pager \
  > "$OUT_DIR/B-final.log"
```

## 3. Restore the DPDS-off state

**Execute on `DUT_HOST` as `root`, through the system manager.** Stop all units
in the reserved experiment namespace, including forwarding and filler units:

```sh
systemctl stop 'p1-x710-*.service'
systemctl list-units --all 'p1-x710-*.service'
systemctl show "$TX_UNIT" "$RX_UNIT" "$B_UNIT" "$FILL_UNIT" \
  -p ActiveState -p SubState -p MainPID -p Result
```

Require inactive units, no surviving worker/filler processes and no handles to
the registered hugepage files or VFIO groups. Check both named and deleted
hugepage mappings with the approved local tools:

```sh
fuser -v -- "$VFIO_GROUP0" "$VFIO_GROUP1"
lsof -nP -- "$VFIO_GROUP0" "$VFIO_GROUP1"
lsof -nP +D "$HUGEDIR"
lsof -nP +L1
```

Run these inspection commands individually. An empty match is useful only with
valid device/mount paths and no permission/tool errors; a command error is not
proof of release. Review deleted mappings against `HUGEDIR` and the recorded
processes. If any process still owns the resources, stop and report it; do not
use fuser's kill mode or rebind/reset a device to clear ownership. A stop timeout
also blocks completion. Only after release is confirmed:

```sh
systemctl stop dpds.slice
# Unmount only the mount created in section 1, after all handles are released.
test "$(cat "$OUT_DIR/hugetlbfs-mounted.path")" = "$HUGEDIR"
umount -- "$HUGEDIR"
systemctl is-active dpds.slice
```

An inactive result is expected; `is-active` then returns nonzero. A busy or
failed unmount blocks completion; do not use lazy/forced unmount or alter any
other mount. Preserve logs and times. **Leave the reserved hugepage counts unchanged; unmounting this run's
hugetlbfs does not deallocate that reservation. Leave each port's driver binding
exactly as it was handed over by the operator.** Do not
undo the private host placement, alter another workload or claim A2/DPDS-off
until the owner has confirmed the absence of this experiment's load.
