<!-- SPDX-License-Identifier: Apache-2.0 -->

# B-prime registered DPDS inputs

Use these inputs with the [real-engine handoff](em-handoff-x710-bprime.md).
They answer source, configuration/memory, generator control, timing and telemetry.
The measurement owner still supplies generator/capture deployment, the rendered
10G X profile, port reservations, clocks and the selected trunk VLAN.
These inputs do not authorize a hardware run.

## 1. Source ref and immutable pin

The integration ref is **`hng/core`**, not `main`.
The calculation baseline is DPDS `7e9cfee14b506440d90196b5e5cbc4249f30fe0f`,
with Core `f3d255dc734b2e4dc3ed9877dcd1e691ce6a20e3`.
The integration owner sends the approved post-merge full SHA to the measurement owner.
Set `DPDS_REV` to that SHA and `DPDS_SRC` to its local source checkout.
Require an immutable 40-hex pin with this input document and the control tool.
An unmerged feature head or a floating branch does not meet that requirement.

```sh
# Source checkout only; no device or generator operation.
set -eu
: "${DPDS_REV:?approved full post-merge hng/core SHA required}"
test "${#DPDS_REV}" -eq 40
case "$DPDS_REV" in *[!0-9a-f]*) exit 2 ;; esac
cd "$DPDS_SRC"
git fetch origin hng/core
git merge-base --is-ancestor "$DPDS_REV" origin/hng/core
git cat-file -e "$DPDS_REV:docs/m5/bprime-inputs.md"
git checkout --detach "$DPDS_REV"
git submodule update --init --recursive
test -f scripts/trex_control.py
git rev-parse HEAD
git -C dpds-core rev-parse HEAD
```

Retain both printed SHAs with the attempt. Keep the executable's separate build
provenance below. A source checkout is not proof of its executable's revision.

## 2. Expanded CONFIG_FILE and node memory

Condition: **`fixed-E3-142-320-1-both-L9-X`** from
[the registered matrix](../../examples/m6-x710.json).
Use the committed engine file for the selected `ACTIVE_VLAN`, one new attempt per tag:

| ACTIVE_VLAN | CONFIG_FILE relative to DPDS_SRC | SHA256 |
| --- | --- | --- |
| 710 | `examples/m5/bprime-710.json` | `277514b6be923c358c015542399b7952b9b1dfbfa762f7ac15215ba503a26002` |
| 711 | `examples/m5/bprime-711.json` | `5319dab5509b212eefc7bfe58522aa2b5c2139d83f0bd810dc57f237f09b8125` |

Both contain two lines, 0 to 1 and 1 to 0, on the same selected VLAN.
Each direction has C=9,000,000,000 bit/s and Q=8,388,608 L2 bytes.
Set t=142,000,000 ns, delta=320,000,000 ns, p=`"1"`, seed=1, and no trace.

The registered X offer remains 10,000,000,000 bit/s per direction with 64 flows.
The Q charges are 64/512/1522 bytes. Stored lengths are 60/508/1518 without FCS.
The envelope is min=64, max=1522, burst=16 packets.
These values come from the matrix and [handoff](em-handoff-x710-bprime.md).

Regenerate either file with the [existing expander](../../tools/g2/expand_m6_x710.py):

```sh
cd "$DPDS_SRC"
python3 -m tools.g2.expand_m6_x710 \
  --condition fixed-E3-142-320-1-both-L9-X --active-vlan "$ACTIVE_VLAN" --out "$CONFIG_FILE"
sha256sum "$CONFIG_FILE"
```

The [Core envelope check](../../dpds-core/src/hng.rs) and independent
[reference](../../tools/g2/reference.py) require, per direction:

```text
slots = floor(8388608 / 64) + 16
      + ceil(9000000000 * 462000000 / (8000000000 * 84))
      = 6318588 packets
frame-byte capacity = slots * (1522 - 4) = 9591616584 bytes
```

Do not add frame-byte capacity again to the mbuf allocation: mbufs already hold the frames.
Use the [pool calculator](../../perf/pool_calc.c) without EAL or full allocation:

```sh
"$POOL_CALC" --c-bps 9000000000 --t-ms 142 --delta-ms 320 \
  --q-ms 7.456541 --p 1 --min-frame 64 --max-frame 1522 --burst 16 \
  --lines-per-port 1 --lcores 7 --rx-desc 256 --tx-desc 1024 --page-bytes 1073741824
```

The calculator converts 7.456541 ms to 7,456,541 ns, then floors C times ns / 8e9.
This returns exactly Q=8,388,608 bytes. Truncation to 7,456,540 ns loses one Q byte and one slot.
The engine JSON uses Q bytes directly.

The following sizes use DPDK 26.03 x86-64 and the bundle's static build configuration.
The allocator size functions returned these values without EAL or NIC access.
Sources: [pool calculator](../../perf/pool_calc.c),
[host_pool_objects](../../common/common.c), [constants](../../common/config.h),
[ring creation](../../dpds.c), and Core `slot_metadata_bytes()`.

| Allocation | Actual algorithm and bytes |
| --- | --- |
| Objects in each ingress pool | 6318588 + 256 RX descriptors + 1024 RX handoff + 1024 TX handoff + 7*32 cache + 4*1024 pending + 4*16 burst + 1024 TX descriptors = **6326300** |
| Mbuf object / stride | 2456 / **2624** bytes from `rte_mempool_calc_obj_size()` |
| Objects in each 1 GiB page | floor(1073741824/2624) = **409200** |
| Object pages in each pool | ceil(6326300/409200) = **16**, or 17179869184 bytes |
| Pool free-object ring | `rte_ring_get_memsize(8388608)` = **67109248** bytes |
| Pool header and caches | `RTE_MEMPOOL_HEADER_SIZE(NULL,32)` = **1056960** bytes |
| Each pool | **17248035392** bytes, or 16.0634847283 GiB |
| Two pools | **34496070784** bytes |
| Four handoff rings | 4 * `rte_ring_get_memsize(1024)` = 4 * 8576 = **34304** bytes |
| Pipeline statistics | `sizeof(struct pipeline_stats)` = **4800** bytes |
| EAL base subtotal | **34496109888** bytes = 32.1270058751 GiB |
| Core metadata, ordinary heap | 2 * 6318588 * 144 = **1819753344** bytes = 1.6947773695 GiB |

Register **`LOAD_SOCKET_MEM=0,33792`** for the approved `DUT_NODE=1`:
33 GiB of EAL memory on node1, zero preallocation on node0.
Use the same vector for `--socket-limit`, plus the existing CPU/node bind.
The EAL reserve above the subtotal is **937370304 bytes**, or 0.8729941249 GiB.
It covers allocator/memzone overhead, driver queues and page alignment beyond the base model.

Require the printed descriptors to be RX=256 and TX=1024. Recalculate if the PMD adjusts them.

Within the 36 GiB accounting budget:

```text
33 GiB EAL + 1819753344 Core bytes + 256 MiB ordinary-heap/stack/library reserve
= 37521668992 bytes = 34.9447773695 GiB
36 GiB - that total = 1133036672 bytes = 1.0552226305 GiB headroom
```

The 256 MiB reserve is a registered allowance, not a measured hardware limit.
EAL uses 33 of the 36 available 1 GiB hugepages and leaves three hugepages unused.
Core metadata uses ordinary memory, not those unused reserved hugepages.

Before launch, require at least **2088188800 bytes** of ordinary node1 memory
for Core plus the 256 MiB reserve. Do not count reserved hugepages as that memory.
Stop if either budget or the full pool allocation fails. Do not shrink this condition.

The calculator's `both_nodes` label means two pools here: both pools belong on node1.

## 4. Non-interactive generator control and reply contract

Use [scripts/trex_control.py](../../scripts/trex_control.py) with the installed
TRex STL API that matches the server. `GEN_HOST`, `GEN_SERVER`, `GEN_WORKDIR`,
`GEN_PYTHON`, `GEN_CONTROL`, `GEN_STL_PROFILE` and reserved port IDs are private owner inputs.
The profile must already contain the registered 10G X mix and per-flow dense IDs.
The unmodified MTU/100G example does not meet that input.
The client loads a local profile, not a path on the remote server.
Configure `GEN_PYTHON` and private `PYTHONPATH` for both the STL API and the profile's imports.

```sh
# On the approved generator-control host, through its approved system-manager wrapper.
"$GEN_PYTHON" "$GEN_CONTROL" start --server "$GEN_SERVER" \
  --ports "$GEN_PORT0" "$GEN_PORT1" --profile "$GEN_STL_PROFILE"
"$GEN_PYTHON" "$GEN_CONTROL" status --server "$GEN_SERVER" \
  --ports "$GEN_PORT0" "$GEN_PORT1" --profile "$GEN_STL_PROFILE"
"$GEN_PYTHON" "$GEN_CONTROL" stop --server "$GEN_SERVER" \
  --ports "$GEN_PORT0" "$GEN_PORT1" --profile "$GEN_STL_PROFILE" --timeout 5 --poll-interval 0.25
```

Run only the requested subcommand at its authorized phase boundary.
`start` loads directions 0/1 onto the two argv ports, starts both with multiplier 1,
no total-rate split and no automatic duration. It refuses an active pair.

All commands acquire only the two reserved ports with `force=False`.
They release their ports and disconnect with `stop_traffic=False`.
Thus a successful short-lived start/status leaves traffic active.
No command requests a hardware counter reset or force-acquires another session's ports.

Every operation writes exactly one JSON line to stdout. Diagnostics go to stderr.
`--help` prints usage text and is outside this operation reply contract.
The fixed fields are:

```json
{"schema":"hng-trex-control-v1","command":"stop","status":"STOPPED","server":"${GEN_SERVER}","ports":[0,1],"profile":"${GEN_STL_PROFILE}","opackets":[123,456],"tx_active":[false,false],"stable_samples":2,"error":null}
```

The numeric ports/counters above illustrate the schema, not deployment values.
`ports`, `opackets` and `tx_active` use argv order.
`command` is start/stop/status. Allowed `status` values: RUNNING, IDLE, STOPPED, ERROR.

The SDK counts `opackets` from each connection's baseline.
Compare counters only within one invocation, never across start/status/stop commands.

Unknown snapshots use null. Unparsed arguments can leave command/server/profile null and ports empty.

Errors return exit 2 and a non-null error string. Success returns exit 0.

`status` reports RUNNING if either port is active. IDLE alone is not TX-stop proof.

`stop` first stops both TX streams. It then reads fresh per-connection `opackets`
for both ports and requires two consecutive unchanged intervals, with both ports inactive.
This needs at least three snapshots. The default interval is 0.25 s and settle timeout is 5 s.

Any increase restarts the unchanged count. Counter reset, missing/bad counters,
RPC failure, ownership failure or timeout returns ERROR, never STOPPED.
The settle timeout starts after the stop RPC. Connection/RPC calls retain the SDK's own timeouts.

Accept "TX stopped" only with exit 0, matching server/ports/profile, STOPPED,
two false `tx_active` values, `stable_samples=2`, and error=null.
Preserve stdout, stderr and exit status. Keep external capture active.

## 5. Warm-up, drain and stop bounds

Use the registered constants above and [TX limits](../../common/config.h).
The maximum wire/Q ratio is 84/64 for the smallest X frame.
The largest frame occupies 1542 wire bytes.
For one direction, the following conservative bounds include the active packet:

```text
Q_drain = 8 * 8388608 * (84/64) / 9000000000 = 0.009786709333 s
D_model = 0.142 + 0.320 + Q_drain = 0.471786709333 s
Q_fill  = 8 * 8388608 * (84/64) / (10000000000-9000000000) = 0.088080384 s
I/O packets = 256 RX + 1023 RX ring + 1023 TX ring + 1024 TX pending
            + 1024 TX descriptors + 4*16 burst = 4414
D_io = 4414 * 1542 * 8 / 9000000000 = 0.006050122667 s
TX tolerance = TERMINAL_TIMEOUT_NS / 1e9 = 1 s
WARMUP_S = ceil(D_model + Q_fill + D_io + TX tolerance) = ceil(1.565917216) = 2
DRAIN_WAIT_S = ceil(D_model + D_io + TX tolerance) = ceil(1.477836832) = 2
STOP_TIMEOUT_S = round that 1.477836832 s allowance up to 10 s
               = 10 s, with 8.522163168 s for control/port/EAL teardown
```

The ring capacities come from a 1024-entry DPDK ring with one reserved slot.
The pending bound uses the one active line per direction.
The 1 s TX allowance comes from `TERMINAL_TIMEOUT_NS`, not a permission to drop.
The teardown allowance is a registered margin, not a measured stop guarantee.

`SHUTDOWN_TIMEOUT_NS` concerns trace outage holds. It is not a global engine-stop deadline.
This fixed condition has no trace outage.

Wait `WARMUP_S=2` after both streams start. Then retain two complete 10 s stable
telemetry intervals under the existing rate/error gates before B-prime readiness.
The earliest ready time is therefore 2 + 2*10 = **22 s**, with no missing interval.

After verified generator STOPPED, wait `DRAIN_WAIT_S=2` and retain two later
unchanged packet-counter snapshots. Only then stop the monitor and engine.
Keep `STOP_TIMEOUT_S=10`, `SendSIGKILL=no`, and the final DRAINED/conservation/owned=0 gates.
A timeout, residual backlog, missing evidence or any non-model drop blocks completion.

## 6. Runtime and telemetry deployment

Register **`DPDK_RUNTIME_ROOT=/root/dpdk/run`** and
**`TELEMETRY_CLIENT=/root/dpdk/dpdk-telemetry.py`**.
Install the client from the approved bundle at that path before the window:

```sh
install -m 0755 "$DEPLOY_BUNDLE/dpds/dpdk-telemetry.py" "$TELEMETRY_CLIENT"
sha256sum "$TELEMETRY_CLIENT"
```

Require client SHA256 `6439d11394eeb98ee2f4183edf2ba31fcd3e335ef963b2d37eae34815c212410`.
Set `RUNTIME_DIRECTORY=$DPDK_RUNTIME_ROOT` for engine and client.
Use an ASCII `RUN_ID` of 1 to 32 letters, digits, underscores or hyphens.
With `FILE_PREFIX=hng-bprime-$RUN_ID`, the telemetry path length is at most
14 + 6 + 11 + 32 + 18 = **81 bytes**, below the Linux limit of 107.
The path is `$DPDK_RUNTIME_ROOT/dpdk/$FILE_PREFIX/dpdk_telemetry.v2`.
If any private override changes it, validate the actual encoded byte count:

```sh
python3 - "$RUN_ID" "$DPDK_RUNTIME_ROOT" <<'PY'
import os,re,sys
run,root=sys.argv[1:]
assert re.fullmatch(r'[A-Za-z0-9_-]{1,32}',run)
path=f'{root}/dpdk/hng-bprime-{run}/dpdk_telemetry.v2'
assert len(os.fsencode(path))<=107
print(path,len(os.fsencode(path)))
PY
```

Require the exact socket's handshake PID to match MainPID and `/ethdev/list` to be `[0,1]`.
An 81-byte name does not prove that a socket exists or that the PID matches.

## Existing executable provenance

Keep the separately supplied static binary and its build record together:
DPDS `b59fe424ba5146529614d8174f6c1c811184c897`,
Core `817624f2dc1a24938af47e51f3feee4bb6d56931`, DPDK 26.03.
Binary SHA256 is `6112faf68de5722166feee691304736bd83887b0a51ccccb3f0a4c4a6745e679`.
The new source baseline changes physical-observer setup/header paths only.
Core runtime source is unchanged. This B-prime command enables no observer.

Those changes do not require a B-prime binary rebuild. Do not describe the old binary as a new-source build.

The current bundle passed a fresh `ldd`/symbol check: only glibc components remain dynamic,
with highest required symbol GLIBC_2.39, below the 2.41 ceiling.
Its existing null/ring telemetry smokes are zero-packet software evidence.
No full 36 GiB allocation, physical NIC, rate or concurrency test ran for this input package.
