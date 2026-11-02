<!-- SPDX-License-Identifier: Apache-2.0 -->

# M5 hardware bring-up

Use this procedure to prepare the hardware path before accuracy qualification.
The equipment operator executes it during the allocated hardware session.
Software PMD checks do not qualify a physical path.

## Decisions required before hardware use

1. **Version combination:** select and lock the kernel, RDMA stack, adapter
   firmware, DPDK, compiler, Rust, generator and collector versions in
   `VERSION_LOCK`. Confirm their compatibility for the selected adapter.
   A software CI result does not qualify that combination.

2. **Single-port loop observation:** select the physical port/VLAN mapping,
   packet identities, timestamp points and observation adapter. Both modeled
   directions share the physical path's capacity. One port permits 2/3/4
   lcores in the current host. Five and seven lcores require two ports.
   Select the [native binary path](NATIVE-BINARY.md) for physical evidence.
   Resolve physical observation before traffic.

3. **Switch clock schema:** select a versioned representation for the switch
   clock and its timestamp points. Register width, tick scale, reset/wrap
   rules, mappings and uncertainty bounds. The current
   [calibration verifier](../tools/g2/calibration.py) has no switch clock kind.
   Do not relabel physical switch timestamps as fake clocks.

An unresolved decision makes hardware bring-up **BLOCKED**.
Resolve these decisions before device changes or qualification traffic.

## Private deployment manifest

Keep actual machine values and raw inventories in a private manifest.
Set these variables from that manifest. Do not use implicit machine defaults.

| Group | Variables |
|---|---|
| Sources and inputs | `DPDS_SRC`, `HOST_REV`, `CORE_REV`, `VERSION_LOCK`, `CONFIG_FILE`, `TOPOLOGY_FILE` |
| Device | `DUT_PCI`, `DUT_IF`, `DUT_NODE`, `EXPECTED_PCIE_GTS`, `EXPECTED_PCIE_WIDTH` |
| Resources | `CORE_SET`, `LCORE_MAP`, `HUGEPAGE_KB`, `HUGEPAGE_MOUNT`, `HUGEPAGES_REQUIRED`, `SOCKET_MEM`, `ORDINARY_RAM_BYTES` |
| Run | `RUN_ID`, `OUT_DIR`, `FRAME_PROFILE`, `OFFERED_BPS`, `DURATION_S`, `REPEATS` |
| Observation | `OBSERVER_REV`, `CALIBRATION_FILE`, `OBSERVER_COST_REPORT`, `EVIDENCE_COMMIT` |

Register generator and collector parameters separately.
Include the operator's traffic, drain and restoration commands.
Use unique run identifiers and output directories.

## Preflight

1. Build the host and do the normal software checks in [Readme.md](../Readme.md).
   Confirm the private version combination before hardware use.

2. Set the deployment variables above.
   Also set `P1_0_MANIFEST` to a new private JSON path.
   Put `lspci`, `findmnt`, `git`, `uname`, Python and `dpdk-testpmd` on `PATH`.
   Set `DPDK_TESTPMD` if the testpmd executable has a different path.

3. Run the checker during the allocated hardware session:

```sh
cd "$DPDS_SRC"
scripts/bringup-check.sh --out "$P1_0_MANIFEST"
```

The command prints a PASS/FAIL table and writes `hng-p1-0-v1` JSON.
Exit status 0 means all environment checks passed. Exit status 1 means failure.

An existing output file causes failure before any probe. Preserve each failed attempt.
The checker creates the manifest with owner-only permissions (0600).
Repeat the command with a new output path for each registered PCI function.

The checker requires these environment variables:

| Variables | Meaning |
|---|---|
| `DUT_PCI`, `DUT_NODE` | PCI domain:bus:device.function and expected NUMA node |
| `EXPECTED_PCIE_GTS`, `EXPECTED_PCIE_WIDTH` | Exact negotiated PCIe rate and width |
| `CORE_SET` | At least two allowed CPU IDs, as a list or ranges |
| `HUGEPAGE_KB`, `HUGEPAGES_REQUIRED`, `HUGEPAGE_MOUNT` | Page size in KiB, minimum free NIC-local pages, hugetlbfs path |
| `SOCKET_MEM` | EAL MiB vector by NUMA node, with positive NIC-local memory |
| `VERSION_LOCK`, `CONFIG_FILE`, `RUN_ID` | Exact version lock, engine configuration and attempt ID |

Set `HOST_REV` and `CORE_REV` for an exact source revision comparison.
The manifest records actual revisions and dirty state in all cases.
Register `LCORE_MAP` and owner roles separately with the topology.

Use this JSON shape for `VERSION_LOCK`.
Replace every placeholder with the approved exact observed value:

```json
{
  "dpdk": "<runtime DPDK version>",
  "pmd": "<DPDK port driver name>",
  "firmware": "<NIC firmware version>",
  "driver": "<vfio-pci or mlx5_core>",
  "driver_version": "<module version or kernel:release>",
  "kernel": "<uname -r>"
}
```

The checker compares these six fields exactly. Missing or unknown values fail.
It reads the loaded module version from sysfs.
Without a module version, it records `kernel:` followed by the kernel release.
Freeze RDMA, compiler, Rust, generator and collector versions separately in the private deployment record.

Confirm adapter compatibility separately. Equality with a version lock does not prove compatibility.

The checker reads negotiated `LnkSta` values from `lspci -D -vv`.
It rejects absent devices, unsupported drivers and unknown NIC NUMA nodes.
It records CPU NUMA nodes and cross-NUMA placement.
It checks free pages on each requested memory node, including the `SOCKET_MEM` allocation, and the mount page size.

After all prerequisites pass, testpmd probes only `DUT_PCI`.
It uses `--in-memory`, the registered hugepage path and `SOCKET_MEM`.
It requests port information, then quits.

[Interactive mode and `--disable-device-start`](https://doc.dpdk.org/guides-26.03/testpmd_app_ug/run_app.html)
prevent automatic port start and packet forwarding.
The checker requires a port for the selected device and observed DPDK, PMD and firmware versions.
Probe failure, missing tools and command timeouts produce FAIL.
The checker does not bind devices, change page reservations or generate traffic.

### P1-0 output and remaining checks

The private manifest contains:

- `scope`, environment `status`, per-check results, timestamps and raw command outputs.
- `versions.observed` and `versions.locked`, source revisions, dirty state, configuration and version-lock SHA-256 hashes.
- Engine `lines`, including C, Q, model seeds and the frame envelope.
- `byte_accounting`: Q uses `max(pkt_len + 4, 64)` L2 bytes, including the in-service frame.
  C uses another 20 wire bytes. Packet length excludes FCS. Service time rounds upward.
- `numa`: NIC node, CPU nodes, cross-NUMA placement, requested MiB and observed hugepage counts and size.
- `clock_uncertainty_ns: null`, `clock_qualification: null` and `g2_qualified: false`.

Environment PASS does not qualify physical timing or throughput.
Keep both clock fields null until `m5_qualify` supplies qualification evidence.
That tool reads `--p1-manifest` and writes a new `--manifest-out` path.
It preserves environment `status` and records the original manifest SHA-256 in `sources`.
Its full result belongs in `clock_qualification`.

Only a physical qualification PASS can supply top-level `clock_uncertainty_ns`.
Software-only evidence keeps that field null.

For a hardware-free check, run `python3 tests/bringup_check.py`.
Its environment supplies a synthetic sysfs tree and command fixtures.
The `BRINGUP_SYSFS_ROOT` override always marks a manifest `scope=software-only`.
Do not use that override for physical evidence.

Continue the manual link, MTU and FEC checks:

```sh
ethtool -i "$DUT_IF"
ethtool "$DUT_IF"
ethtool --show-fec "$DUT_IF"
numactl --hardware
lscpu -e=CPU,NODE,SOCKET,CORE,ONLINE
grep -E 'MemAvailable|Huge' /proc/meminfo
```

Confirm physical cores, ordinary RAM and the complete pool envelope separately.
Include EAL, PMD, mempool and observer overhead.
The probe alone does not prove the full emulator allocation or its queue ownership.
Keep `--in-memory` in the registered EAL arguments.
Use the selected driver's procedure for a bind, reset or firmware change.
Do not change unrelated devices or memory reservations during preflight.

## X710 environment example

Use [bringup-x710.env](../examples/bringup-x710.env) for the X710 VFIO path.
Set its private deployment inputs before you source it.
The version lock must specify `pmd=net_i40e` and `driver=vfio-pci`.
The example selects node1 CPUs, node1 memory and 1 GiB hugepages.
It leaves node0 memory at zero. Register actual CPU IDs and resource quantities privately.

```sh
. examples/bringup-x710.env
DUT_PCI="$DUT_PCI_PORT0" scripts/bringup-check.sh --out "$OUT_DIR/p1-0-port0.json"
DUT_PCI="$DUT_PCI_PORT1" scripts/bringup-check.sh --out "$OUT_DIR/p1-0-port1.json"
```

Each invocation checks one function. Preserve both manifests.
A PASS for i40e does not qualify a different PMD.
Register switch/VLAN topology and external capture separately before traffic.
The example does not select VLAN tags or resolve line classification.

## CX-4 environment example

Use [bringup-cx4.env](../examples/bringup-cx4.env) for two distinct ConnectX-4 cards.
Keep the mlx5 bifurcated stack and `mlx5_core` binding. Do not bind these cards to vfio.
Check each card's negotiated Gen3 x16 link, local CPUs, pages and exact version lock separately.
Record the approved RDMA/DOCA versions in the private deployment inventory.
Follow the [100G operator handoff](m7/em-handoff-cx4.md) for commands, external-clock qualification and restoration.
Its hardware steps require owner authorization. A simulated sysfs PASS remains software-only.

## Bypass and clock checks

1. Freeze the topology and frame profile. Start with the registered lower
   offered rate, then the target rate. Keep the same observation path.

2. Bypass the emulator. Record complete packet identities and before/after
   generator, switch, receiver, NIC and observer counters.
   Require the target delivered rate and zero non-model loss.

3. Calibrate the two timestamp points with one observation clock.
   Bound offset, drift, quantization, load and mapping errors across the
   registered loads and maximum residence time. Preserve raw and corrected OWD.

4. Require pair uncertainty at most 10,000 ns for physical timing evidence.
   Do not subtract DUT residence or its load tail as a calibration offset.
   Retain pre/post anchors, mapping intervals and evidence hashes in
   `CALIBRATION_FILE`. Repeat the bypass check after DUT execution.

Missing observation records, unknown counter losses or unbounded clock error
make the result **INCONCLUSIVE**. Confirmed non-model packet loss is **FAIL**.
See the [observation tools](../tools/g2/README.md) for current software limits.

## Observer cost gate before M6

1. Register one physical pilot cell with `OFFERED_BPS=10000000000` and a unique
   `RUN_ID`. Use 1/256 main sampling: set each selected required cell's
   `mask_bits=8`. Keep the v1 `sampling` header. Run registration preflight.
   Stop if registration preflight rejects the cell.

2. Run the cell through the selected physical observation path. Preserve all
   observer counters, the reporter's `observer_cost` object and associated
   reason codes in `OBSERVER_COST_REPORT`.

Require the reporter's [`observer_cost` assessment](../tools/g2/README.md#observer-cost-assessment)
to have `status=PASS` and an empty `warnings` list before M6.
It requires zero observer loss, strict queue headroom and zero backlog at capture end.
Missing or inconclusive cost evidence does not pass.

Record the gate decision, `EVIDENCE_COMMIT`, `RUN_ID`, `HOST_REV`, `CORE_REV` and
`OBSERVER_REV` with the report. Preserve configuration and calibration hashes.
If all three conditions pass, register M6 fixed cells at 1/256 main sampling
and 30 s exposure. Retain the required samples, repeats and acceptance gates.
Run registration preflight before M6 traffic.
Otherwise, retain the previous registered sampling and exposure plan.

## Handoff

Preserve the manifest, source/configuration hashes, command lines, exit status,
resource inventories, counters, packet identities and calibration evidence in
`OUT_DIR`. Record the qualified topology and highest valid offered rate.
Stop ingress, drain the owned process and restore the allocated resources.
Keep failed and incomplete attempts. Claim only the rate and clock path that passed.
