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
   The [native exporter](../NATIVE-OBSERVER.md) supports software PMDs only.
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

1. Verify source revisions and the version lock. Build and run the normal
   software checks as described in [Readme.md](../Readme.md).

2. Inspect the device and resources with the commands below.
   Repeat applicable checks for the generator.

3. Compare negotiated PCIe rate/width, link mode, MTU and FEC with the manifest.
   Stop on a degraded link or unknown NUMA placement.

4. Confirm physical cores, free node-local hugepages and ordinary RAM against
   the registered envelope. Include EAL, mempool and observer overhead.
   Use the pool calculator's envelope, not mean occupancy.

```sh
test "$(git -C "$DPDS_SRC" rev-parse HEAD)" = "$HOST_REV"
test "$(git -C "$DPDS_SRC/dpds-core" rev-parse HEAD)" = "$CORE_REV"
lspci -D -vv -s "$DUT_PCI"
ethtool -i "$DUT_IF"
ethtool "$DUT_IF"
ethtool --show-fec "$DUT_IF"
cat "/sys/bus/pci/devices/$DUT_PCI/numa_node"
numactl --hardware
lscpu -e=CPU,NODE,SOCKET,CORE,ONLINE
grep -E 'MemAvailable|Huge' /proc/meminfo
cat "/sys/devices/system/node/node$DUT_NODE/hugepages/hugepages-${HUGEPAGE_KB}kB/free_hugepages"
findmnt --target "$HUGEPAGE_MOUNT" -o TARGET,FSTYPE,OPTIONS
```

Keep `--in-memory` in the registered EAL arguments.
Do not substitute software timestamps for physical observations.
Use the selected driver's device procedure for any bind, reset or firmware change.
Do not change unrelated devices or memory reservations during preflight.

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

Require all three conditions before M6:

- Require measured `records_dropped=0` and `capture_drops=0` for every observer.
- Require measured `queue_capacity>0` and `queue_high_water<queue_capacity` for every observer queue.
- The reporter's `observer_cost` assessment has no warnings. Missing or
  inconclusive cost evidence does not pass.

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
