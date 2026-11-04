<!-- SPDX-License-Identifier: Apache-2.0 -->

# X710 A/B-prime/A operator handoff

Use this procedure before the first DPDS/measurement concurrency window.
B-prime runs the real `dpds` engine. The [testpmd B phase](em-handoff-x710.md)
qualifies a different workload and does not replace this confirmation.
The measurement owner controls A1, B-prime and A2 times and the final decision.
Keep the load active for the whole authorized B-prime window. Do not use an automatic duration or restart.
Use the [registered inputs](bprime-inputs.md) for source/config hashes, node1 memory,
non-interactive control, warm-up/drain bounds and telemetry paths.

All operational commands use **`root`, through the system manager**, never `--user`.
Each command block identifies `DUT_HOST` or `GEN_HOST`. Execute it locally on that host.
Actual host names, accounts, addresses, device IDs and paths belong in private inputs.

| Variables | Private input or constraint |
| --- | --- |
| `DUT_HOST`, `GEN_HOST`, `PRIVATE_ENV`, `GEN_PRIVATE_ENV`, `DPDS_SRC` | DUT, separate generator/capture host, their approved environment files and source checkout |
| `DPDS_BIN`, `CONFIG_FILE`, `OUT_DIR`, `RUN_ID` | Absolute real DPDS executable, registered engine JSON, new private directory and attempt ID |
| `PORT0_BDF`, `PORT1_BDF`, `VFIO_GROUP0`, `VFIO_GROUP1` | Two DUT X710 functions and their existing VFIO device paths |
| `VLAN_CABLE`, `VLAN_LEO`, `ACTIVE_VLAN` | Approved trunk tags, distinct and retained. Select one tag per run |
| `DUT_NODE`, `NODE1_CORE_SET`, `CORES`, `MAIN_CORE` | Approved node1 ID, allowed CPUs, seven distinct node1 CPU IDs and their main CPU |
| `HUGEDIR`, `LOAD_SOCKET_MEM`, `STOP_TIMEOUT_S`, `DRAIN_WAIT_S` | Verified 1 GiB hugetlbfs, EAL MiB vector with zero on other nodes, registered stop/drain bounds |
| `DEPLOY_BUNDLE`, `DPDK_RUNTIME_ROOT`, `TELEMETRY_CLIENT` | Local deployment bundle, private runtime directory and installed `dpdk-telemetry.py` |
| `GEN_PYTHON`, `GEN_CONTROL`, `GEN_SERVER`, `GEN_WORKDIR`, `GEN_PORTS`, `GEN_STL_PROFILE` | Approved STL API interpreter/control tool, server, working directory, two reserved ports and prepared mixed-frame profile |
| `GEN_CONTROL_UNIT` | Unique system-manager unit prefix for this attempt's control commands |

Preconditions: complete environment bring-up, authorized device handover and
an approved top-level `dpds.slice`. Both X710 ports belong to DPDS, not to a
local traffic generator. External generation/capture must cover both directions
through the switch trunk.

Keep both VLANs available, but run the selected heavy
condition separately on each VLAN. Do not use a self-feeding switch loop.
Register capture clocks, topology and generator MAC/IP parameters privately.

## 1. Start the real engine and external traffic

**On `DUT_HOST`, as `root`, system manager.** The source
[short matrix](../../examples/m6-short.json) contains representative manifest
paths, not a runnable engine configuration. Resolve `boundary-r0` to the
registered expanded condition **`fixed-E3-142-320-1-both-L9-X`**:

| Field | Required value |
| --- | --- |
| Directions and line mapping | Two lines, `0 → 1` and `1 → 0`, both on `ACTIVE_VLAN` |
| Offered rate R / modeled C | 10 Gbit/s / 9 Gbit/s per direction |
| Q / t / delta / p / seed | 8,388,608 L2 bytes / 142 ms / 320 ms / 1 / 1 |
| X frame mix | Equal packet counts at Q charges 64 / 512 / 1522 bytes, 64 flows |
| Stored frame lengths | 60 / 508 / 1518 bytes, without FCS, with one retained VLAN tag |
| Envelope | Minimum Q charge 64, maximum 1522, burst bound 16 |
| Storage per line | At least 6,318,588 packets and 9,591,616,584 frame bytes |

This selects the largest `t + delta` and storage envelope among fixed stress
conditions. C=9G is the highest stress C below the 10G offer. `p=1` keeps every
accepted packet through the full 462 ms delay. The X mix also exercises small
packets. A non-stress L0 envelope can be larger, but its offer is only 9G.
This is fixed-delay pollution evidence on each tag, not a trace-fidelity result.

Before the window, set `DEPLOY_BUNDLE` to the approved local copy of the same
deployment bundle as the static testpmd.
It provides the upstream `dpdk-telemetry.py` client and a `dpds/` subdirectory
with `dpds`, `BUILD.md` and `SHA256SUMS`. Verify the parent and `dpds/` checksum
manifests separately. Install `dpds/dpds` as `/root/dpdk/dpds` and set
`TELEMETRY_CLIENT` to the supplied client. Neither tool needs a DPDK installation
on the DUT; the client uses Python 3's standard library.

The DPDS binary is built natively on Debian 13 / glibc 2.41, with
`-Dplatform=generic` and static DPDK, Core and NUMA libraries. Only glibc remains
dynamic. Retain its build record and checksum with the run artifacts.

Prepare the approved private `CONFIG_FILE` with this mapping and capacity.
Retain its SHA-256 and the expanded registration. Size both ingress pools and
core metadata for node1. The [registered memory calculation](bprime-inputs.md#2-expanded-config_file-and-node-memory)
gives `LOAD_SOCKET_MEM=0,33792` plus ordinary Core memory and explicit reserves.
Both pools occupy the same registered node. The calculator's `both_nodes` label does not change placement.

Stop if the approved hugepage/RAM budget cannot contain the full envelope.
Do not lower C, delay, packet range or storage to fit a hardware allocation.

```sh
# DUT_HOST / root / system manager. Use a new RUN_ID/OUT_DIR per VLAN attempt.
set -euo pipefail
set -a
. "$PRIVATE_ENV"
set +a
: "${DPDS_BIN:?approved deployed executable path required}"
cd "$DPDS_SRC"
umask 077
mkdir -- "$OUT_DIR"
SLICE_CPUS=${NODE1_CORE_SET//,/ }
RUN_CPUS=${CORES//,/ }
DUT_UNIT="p1-x710-bprime-${RUN_ID}.service"
MON_UNIT="p1-x710-bprime-monitor-${RUN_ID}.service"
FILE_PREFIX="hng-bprime-${RUN_ID}"
export FILE_PREFIX
systemctl start dpds.slice
systemctl show dpds.slice -p Slice -p ControlGroup -p EffectiveCPUs -p EffectiveMemoryNodes
sha256sum "$CONFIG_FILE" > "$OUT_DIR/config.sha256"

systemd-run --unit="$DUT_UNIT" --slice=dpds.slice \
  --property="AllowedCPUs=$RUN_CPUS" --property="CPUAffinity=$RUN_CPUS" \
  --property="AllowedMemoryNodes=$DUT_NODE" --property=NUMAPolicy=bind \
  --property="NUMAMask=$DUT_NODE" --property=LimitMEMLOCK=infinity \
  --property=KillMode=control-group --property=KillSignal=SIGTERM \
  --property=SendSIGKILL=no --property="TimeoutStopSec=${STOP_TIMEOUT_S}s" \
  --property=Restart=no --property="WorkingDirectory=$DPDS_SRC" \
  --property="StandardOutput=append:$OUT_DIR/dpds.log" \
  --property="StandardError=append:$OUT_DIR/dpds.log" \
  --setenv="RUNTIME_DIRECTORY=$DPDK_RUNTIME_ROOT" \
  "$DPDS_BIN" -l "$CORES" --main-lcore="$MAIN_CORE" \
  --in-memory --telemetry --file-prefix="$FILE_PREFIX" \
  -a "$PORT0_BDF" -a "$PORT1_BDF" --huge-dir="$HUGEDIR" \
  --socket-mem="$LOAD_SOCKET_MEM" --socket-limit="$LOAD_SOCKET_MEM" -- \
  -c "$CONFIG_FILE" -o "$OUT_DIR/final-stats.txt"
systemctl show "$DUT_UNIT" -p MainPID -p ActiveState -p ControlGroup -p CPUAffinity -p NUMAMask
```

Require `Slice=-.slice`, `ControlGroup=/dpds.slice` and only the approved node1
CPUs/memory. Do not replace an existing slice or bind/reset a device here.
A zero socket limit is not a node ban. Retain the cpuset and NUMA bind.

Require two expected i40e devices, correct port order, node1 pools and
`layout=7-lcore rx_scheduler_tx=per-direction` in `dpds.log` before traffic.

Seven lcores mean six data-plane owners plus the main control/wait thread.
Do not add a synthetic busy-loop filler to the real-engine workload.
Do not pass testpmd options such as `--forward-mode` or `--stats-period` to DPDS.

**On `GEN_HOST`, as `root`, system manager.** Reserve a separate pair of generator
ports and the external captures before B-prime. Prepare `GEN_STL_PROFILE` from
the [STL example](../../examples/m7-traffic/README.md), with the registered X
mix, 64 flows, preserved identities and `M7_VLAN=ACTIVE_VLAN` in its rendered
configuration. The unmodified example is fixed-MTU/100G and is not this profile.
Verify aggregate 10G L1 per direction in bypass first. Keep captures active.
Register approved 10G links and rates for both generator ports.

Use the non-interactive tool through a transient system-manager unit:

```sh
# GEN_HOST / root / system manager. Preserve the one-line JSON reply and exit status.
set -euo pipefail
set -a
. "$GEN_PRIVATE_ENV"
set +a
systemd-run --wait --pipe --collect --unit="${GEN_CONTROL_UNIT}-start" \
  --property="WorkingDirectory=$GEN_WORKDIR" \
  /bin/bash -euc '
    set -a; . "$1"; set +a
    export M7_VLAN="$ACTIVE_VLAN"
    read -r -a ports <<< "$GEN_PORTS"
    exec "$GEN_PYTHON" "$GEN_CONTROL" start --server "$GEN_SERVER" \
      --ports "${ports[@]}" --profile "$GEN_STL_PROFILE"
  ' _ "$GEN_PRIVATE_ENV"
```

Run `start` only after DUT readiness and owner permission.
Require exit 0 and a RUNNING reply with both `tx_active` values true.
The tool uses each profile's rate, no total-rate split and no automatic duration.

Acquire only the two reserved ports. Do not force acquisition of another session's ports.
The transient command releases ownership without stopping traffic. No console must stay open.

Repeat this three-section procedure with a new attempt for the other VLAN.

For software CLI verification on `VALIDATION_HOST`, as `VALIDATION_USER`, use
seven allowed CPUs and two PCAP PMDs instead of PCI allowlist arguments.
Set `DPDS_BIN` to a development build with PCAP support for this software check.
The deployment bundle contains i40e, null and ring PMDs.
Retain `--in-memory --telemetry`, the explicit prefix and DPDS `-c/-o` arguments.
Use `--no-pci --no-huge -m 256` and two `--vdev=net_pcap...` ports.

Use a clearly labeled small software configuration under
`prlimit --as=4294967296`. The full storage envelope exceeds this validation budget.
Require the seven-lcore marker, telemetry for both ports, clean SIGTERM drain,
packet/byte conservation and two `owned=0` pool reports. This proves CLI/layout
and shutdown behavior, not the full hardware allocation, 10G rate or concurrency.

```sh
# VALIDATION_HOST / VALIDATION_USER. Software ports only, direct execution.
# Supply new PCAP output paths and a small SOFTWARE_CONFIG_FILE.
RUNTIME_DIRECTORY="$DPDK_RUNTIME_ROOT" prlimit --as=4294967296 -- \
  "$DPDS_BIN" -l "$CORES" --main-lcore="$MAIN_CORE" \
  --in-memory --telemetry --file-prefix="$FILE_PREFIX" \
  --no-pci --no-huge -m 256 \
  --vdev="net_pcap0,rx_pcap=$PCAP0_IN,tx_pcap=$PCAP0_OUT" \
  --vdev="net_pcap1,rx_pcap=$PCAP1_IN,tx_pcap=$PCAP1_OUT" -- \
  -c "$SOFTWARE_CONFIG_FILE" -o "$SOFTWARE_STATS_FILE"
```

## 2. Check every 10 seconds and retain deficiencies

**On `DUT_HOST`, as `root`, system manager.** DPDS writes host/line counters to
stdout and `-o` only after shutdown/drain. Do not signal it for live statistics.
For live port statistics, read the running DPDS process's
[DPDK telemetry](https://doc.dpdk.org/guides-26.03/howto/telemetry.html), selected by
its unique file prefix. `/ethdev/stats,0` and `,1` expose cumulative NIC packets,
bytes, errors, missed packets and RX allocation failures. Confirm the socket
belongs to the recorded `MainPID`, and that `/ethdev/list` is `[0,1]`.

`--in-memory` does not disable telemetry. This procedure sets
`RUNTIME_DIRECTORY=$DPDK_RUNTIME_ROOT` for both DPDS and its monitor, so the
socket is `$DPDK_RUNTIME_ROOT/dpdk/$FILE_PREFIX/dpdk_telemetry.v2`.
Check that exact socket and use the same environment and prefix for the client:

```sh
# DUT_HOST / root / system manager. The DPDS process must already be running.
TELEMETRY_SOCKET="$DPDK_RUNTIME_ROOT/dpdk/$FILE_PREFIX/dpdk_telemetry.v2"
test -S "$TELEMETRY_SOCKET"
RUNTIME_DIRECTORY="$DPDK_RUNTIME_ROOT" python3 "$TELEMETRY_CLIENT" -f "$FILE_PREFIX"
# At the client prompt: /ethdev/list, /ethdev/stats,0, /ethdev/stats,1, then quit.
# Enter each command on its own line.
```

Without `RUNTIME_DIRECTORY`, root defaults to `/var/run/dpdk/$FILE_PREFIX/`;
other users use `${XDG_RUNTIME_DIR:-/tmp}/dpdk/$FILE_PREFIX/`.
Do not use those defaults to locate this procedure's explicitly selected socket.

The deployment binary passed separate null and ring software smokes with
`--in-memory --telemetry`: both ports' `/ethdev/stats` were readable, followed
by clean shutdown/drain. These were zero-packet checks, not physical qualification.

Use a short runtime path and prefix. The complete Linux socket path must fit within 107 bytes.
Missing commands, unsupported counters, resets or missing coverage do not pass.

Start the monitor before the measurement window. It occupies the existing main CPU,
inside the same node1 slice, and adds no eighth CPU. Preserve its log:

```sh
# DUT_HOST / root / system manager. OUT_DIR is new and private.
(set -C; cat > "$OUT_DIR/monitor.py" <<'PY'
import datetime, json, subprocess, sys, time
previous = {}
try:
    while True:
        began = time.monotonic()
        raw = subprocess.check_output([sys.executable, sys.argv[1], '-f', sys.argv[2]],
            input='/ethdev/stats,0\n/ethdev/stats,1\nquit\n', text=True, timeout=5)
        rows = [json.loads(line)['/ethdev/stats'] for line in raw.splitlines()]
        if len(rows) != 2:
            raise ValueError('two complete port responses required')
        now = time.monotonic()
        stamp = datetime.datetime.now(datetime.timezone.utc).isoformat()
        for port, values in enumerate(rows):
            row = dict(time=stamp, monotonic_s=now, port=port, counters=values, status='BASELINE')
            if port in previous:
                old_time, old = previous[port]
                dt = now - old_time
                delta = {key: values[key] - old[key] for key in
                    ('ipackets','opackets','ibytes','obytes','imissed','ierrors','oerrors','rx_nombuf')}
                if dt <= 0 or min(delta.values()) < 0:
                    raise ValueError('invalid interval or counter reset')
                rx = 8 * (delta['ibytes'] + 24 * delta['ipackets']) / dt
                tx = 8 * (delta['obytes'] + 24 * delta['opackets']) / dt
                clean = all(delta[key] == 0 for key in ('imissed','ierrors','oerrors','rx_nombuf'))
                row.update(interval_s=dt, rx_l1_bps=rx, tx_l1_bps=tx,
                    rx_pps=delta['ipackets']/dt, tx_pps=delta['opackets']/dt,
                    status='OK' if rx >= 9.9e9 and 8.91e9 <= tx <= 9.09e9 and clean else 'REPORT')
            print(json.dumps(row), flush=True)
            previous[port] = now, values
        time.sleep(max(0, 10 - (time.monotonic() - began)))
except KeyboardInterrupt:
    pass
except Exception as exc:
    print(json.dumps(dict(time=datetime.datetime.now(datetime.timezone.utc).isoformat(),
                         status='REPORT', error=str(exc))), flush=True)
    raise
PY
)
systemd-run --unit="$MON_UNIT" --slice=dpds.slice \
  --property="AllowedCPUs=$MAIN_CORE" --property="CPUAffinity=$MAIN_CORE" \
  --property="AllowedMemoryNodes=$DUT_NODE" --property=NUMAPolicy=bind \
  --property="NUMAMask=$DUT_NODE" --property=KillSignal=SIGINT \
  --property=SendSIGKILL=no --property=TimeoutStopSec=30s \
  --property="StandardOutput=append:$OUT_DIR/port-stats.jsonl" \
  --property="StandardError=append:$OUT_DIR/monitor-errors.log" \
  --setenv="RUNTIME_DIRECTORY=$DPDK_RUNTIME_ROOT" \
  python3 -u "$OUT_DIR/monitor.py" "$TELEMETRY_CLIENT" "$FILE_PREFIX"
```

DPDK byte counters exclude FCS. For these frames of at least 60 bytes, add
24 bytes per packet: four FCS bytes plus 20 preamble/inter-frame-gap bytes.
Use actual monotonic intervals, not an assumed exactly ten seconds.
The **99%-line-rate threshold of 804,617 pps** applies only to 1518-byte frames including
FCS. The selected mixed frame sizes need the L1 byte calculation above.

After the [registered 2 s residence/fill warm-up](bprime-inputs.md#5-warm-up-drain-and-stop-bounds), require each port's RX >=9.9G L1 and
TX within 1% of the modeled 9G. Q drops make TX<10G intentional.
Record `Bprime-ready.time` only after complete stable intervals, then let the
owner start its B-prime measurement window.
Keep every raw interval and both
endpoint-bracketing reports. A `REPORT`, missing interval or service/load gap
requires timestamped notification to the owner. Do not hide it in an average.

The monitor reports port behavior. It does not prove per-line Q correctness.
Final Q/storage/drop gates remain pending until the engine drains.
The owner decides the A1/B-prime/A2 pollution result.

## 3. Stop generation, drain and restore DPDS-off

**First on `GEN_HOST`, as `root`, system manager.** On the owner's stop request,
record the time and stop both streams with the non-interactive tool:

```sh
# GEN_HOST / root / system manager. Accept only the registered STOPPED reply and exit 0.
set -euo pipefail
set -a
. "$GEN_PRIVATE_ENV"
set +a
systemd-run --wait --pipe --collect --unit="${GEN_CONTROL_UNIT}-stop" \
  --property="WorkingDirectory=$GEN_WORKDIR" \
  /bin/bash -euc '
    set -a; . "$1"; set +a
    read -r -a ports <<< "$GEN_PORTS"
    exec "$GEN_PYTHON" "$GEN_CONTROL" stop --server "$GEN_SERVER" \
      --ports "${ports[@]}" --profile "$GEN_STL_PROFILE" --timeout 5 --poll-interval 0.25
  ' _ "$GEN_PRIVATE_ENV"
```

Require the [reply contract](bprime-inputs.md#4-non-interactive-generator-control-and-reply-contract):
both TX streams inactive and two unchanged counter intervals. Keep external capture active.
Do not stop the DUT while ingress still runs.

**Then on `DUT_HOST`, as `root`, system manager.** Keep telemetry through drain.
Bound the delay by `t + delta + Q_drain`, plus registered NIC/handoff/TX margin.
For the selected minimum frame, a conservative Q bound is
`8 * Q_bytes * (84/64) / C_bps`, about 9.79 ms. The model contribution is
therefore less than 0.472 s. Set `DRAIN_WAIT_S` above this plus the registered
margin. The [registered values](bprime-inputs.md#5-warm-up-drain-and-stop-bounds)
are `DRAIN_WAIT_S=2` and `STOP_TIMEOUT_S=10`. Neither replaces zero-backlog evidence.

```sh
# DUT_HOST / root / system manager. Generator stop must already be confirmed.
date --iso-8601=ns > "$OUT_DIR/ingress-stopped.time"
sleep "$DRAIN_WAIT_S"
# Require two subsequent telemetry snapshots with unchanged packet counters.
# Preserve the final bracketing report before stopping the monitor.
systemctl stop "$MON_UNIT"
systemctl stop "$DUT_UNIT"
systemctl show "$DUT_UNIT" "$MON_UNIT" -p ActiveState -p SubState -p MainPID \
  -p Result -p ExecMainCode -p ExecMainStatus > "$OUT_DIR/unit-state.txt"
```

Require a normal exit, `status=DRAINED`, packet and byte `conservation=PASS`,
and both `pool ... owned=0` reports in `dpds.log`. Read the final host fields
from `final-stats.txt`. Require nonzero `q_drop` after the steady R>C window
and `q_peak_l2` no greater than Q. Require all
`storage_drop`, `trace_loss`, `trace_outage`,
`missed_deadline_outage_drop` and `tx_invalid_drop` must all be zero.

Global `invalid_input`, `rx_handoff_drop`, `tx_handoff_drop`, `terminal_tx_drop`
and `shutdown_drop` must be zero. `software_drops` must equal the total Q drops.

Require zero final `q_current_l2_bytes`, `storage_current_packets` and `storage_current_bytes`.
Keep the per-line Q/storage peaks and all payload/L2 byte counts.
For this mix, do not assume Q drops are exactly 10% of packets.
Check accepted packets against per-line TX acceptance after drain.
Across both ports, require `RX = TX + Q_drop` in packets and wire bytes.
Q-drop wire bytes equal `q_drop_l2_bytes + 20 * q_drop`.

Missing host counters, non-model drops, stop timeouts or failed reclamation block completion.
Record and report their observation times. Retain failed attempts.

Finally, confirm no owned process or open resource remains:

```sh
# DUT_HOST / root / system manager. Inspection only; do not use fuser kill mode.
systemctl is-active "$DUT_UNIT" "$MON_UNIT"
systemctl list-units --all "$DUT_UNIT" "$MON_UNIT"
fuser -v -- "$VFIO_GROUP0" "$VFIO_GROUP1"
lsof -nP -- "$VFIO_GROUP0" "$VFIO_GROUP1"
lsof -nP +D "$HUGEDIR"
lsof -nP +L1
# After individual inspection confirms release:
systemctl stop dpds.slice
systemctl is-active dpds.slice
```

Run inspection commands individually: inactive/no-match can return nonzero.
A tool error or unknown device path is not proof of release.
Review deleted hugepage mappings and the slice cgroup for surviving processes.

Stop only this attempt's units. Do not kill another workload or the shared
traffic server. Generator control uses transient units that finish after each command.
Leave port bindings, hugepage reservations,
mounts and switch configuration exactly as the operator handed them over.
Do not start A2 until the measurement owner confirms DPDS-off.

## TX handoff and shutdown diagnostics

Read [TX handoff backpressure and owner diagnostics](../TX-HANDOFF.md) for the
unchanged 1024-slot ring budget, terminal gate, `/dpds/diagnostics` telemetry,
final-statistics fields and engine-exit interpretation. Keep the registered
load and packet composition unchanged when diagnosing a pause.
