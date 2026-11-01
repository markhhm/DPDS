<!-- SPDX-License-Identifier: Apache-2.0 -->
# M7 traffic examples — configuration only

No TRex server, simulator, NIC or traffic was started. These examples describe
one two-port pair and simultaneous data in both directions; hardware capacity,
TCP windows and 100G performance remain unverified.
Publish only the two profiles, two templates and this page; `sources/` and
verification receipts are audit artifacts.

| File | Purpose |
| --- | --- |
| `stl_probe.py` + `trex-stl.yaml.in` | MTU-1500 numbered UDP, 100G L1 per direction |
| `astf_long_rtt.py` + `trex-astf.yaml.in` | 64 long-lived full-duplex TCP flows |

Set machine-specific variables before rendering: `M7_PCI_0/1`,
`M7_P0_SRC_MAC/DST_MAC`, `M7_P1_SRC_MAC/DST_MAC`, `M7_PORT_GB`,
`M7_MEMORY_MB`, `M7_MASTER_CORE`, `M7_LATENCY_CORE`, `M7_NUMA_NODE`, and
`M7_DATA_CORES` (a comma-separated list of distinct data-core IDs). Reserve
master/latency/data cores without overlap. `M7_VLAN` must match a registered
emulator line. Set `RUN_DIR` to an external configuration directory.

```sh
envsubst < trex-stl.yaml.in > "$RUN_DIR/trex-stl.yaml"
envsubst < trex-astf.yaml.in > "$RUN_DIR/trex-astf.yaml"
```

TRex does not expand these template variables itself. Keep rendered local
configs out of the repository. Load one mode at a time after apparatus approval.

**STL:** set `M7_P0_SRC_IP/DST_IP` and `M7_P1_SRC_IP/DST_IP` to the two
directional endpoint pairs. Add the DPDS checkout to `PYTHONPATH` so the profile
can import `tools.g2.probe.encode_probe`; add the installed TRex Python API as
usual. Load on ports 0 and 1; TRex passes direction `port_id % 2`. The packet is
1518 bytes without FCS: one VLAN, IPv4 MTU 1500, 1472 UDP payload bytes; wire
accounting is 1542 bytes, about 8.106 Mpps/direction at 100G. Payload starts at
byte 46 and preserves the probe's `!QIB` header and padding. The selected-late
flag is zero; model/reference selection determines emulator lateness.

The VM increments the big-endian uint64 ID, splits values across data cores,
and reserves the high bit for direction. IDs reset on a new run: record a new
run ID. Use no packet cache or duplicate copy of a stream. Multi-core IDs are
identities; actual ingress observation supplies wire order. IPv4 UDP checksum
is zero, so payload ID writes need no checksum offload. STL already carries
its VLAN; use its matching platform template to retain one tag.

**ASTF:** set disjoint `M7_CLIENT_START/END` and `M7_SERVER_START/END`
ranges, with at least 64 client addresses; set `M7_IP_OFFSET` for this port
pair and optionally `M7_TCP_PORT` (default 9000). Client port 0 and server port 1
both send bulk data concurrently. Both endpoints request 64 MiB TX/RX buffers,
MSS 1460 and RFC1323 support. `cps=64`, `limit=64`, `cont=True` request and
maintain 64 active flows; verify the observed active-flow count. CPS is a
connection rate, not a bit rate. The platform template supplies the VLAN.

The 284 ms RTT comes from the registered path/emulator, with no application
delay inserted. At 100G the BDP is 3.55 GB, so
`ceil(100e9 * 0.284 / (8 * 64 * 2**20)) = 53` flows; 64 provides margin.
Buffer caps do not prove negotiated rwnd/cwnd or throughput. Provision generator
RAM separately and confirm steady traffic after connection warmup. ACKs,
retransmissions and headers count toward L1; record L1/L2/L7 separately.

API checks use TRex v3.08 source `27e0153b5ff833c51d48f1625ace979a2868d8a0`:
[STL VM](https://github.com/cisco-system-traffic-generator/trex-core/blob/27e0153b5ff833c51d48f1625ace979a2868d8a0/scripts/automation/trex_control_plane/interactive/trex/stl/trex_stl_packet_builder_scapy.py),
[ASTF profile API](https://github.com/cisco-system-traffic-generator/trex-core/blob/27e0153b5ff833c51d48f1625ace979a2868d8a0/scripts/automation/trex_control_plane/interactive/trex/astf/trex_astf_profile.py).
These are example configurations, not physical G2 results.
