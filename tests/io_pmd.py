#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Public test CLI: io_pmd.py {pcap|ring|ring-stepped|ring-split|ring-stepped-split|ring-io-split|ring-stepped-io-split|ring-io-split-lcores|af_packet} EXECUTABLE.

The -split modes run one scheduler owner per ingress port on five lcores.
The -io-split modes also run one RX and one TX owner per direction on seven lcores;
ring-io-split-lcores uses the production RX and TX lcore functions and shutdown chain.

All fixtures are synthetic. PASS lines report input, output and intentional Q
loss; every output identity, complete frame and physical egress is checked.
"""
import json
import os
from pathlib import Path
import signal
import re
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import time
from eal_args import eal_arguments
from startup import wait_for_output


def config(seed, smoke=False):
    lines = []
    for port in range(2):
        for vlan in (100, 101):
            lines.append(dict(name=f"synthetic-{port}-{vlan}", ingress_port=port,
                egress_port=1-port, vlan_id=vlan, model=dict(c_bps=8000000003,
                q_bytes=600, t_ns=1000+(3000 if vlan == 101 else 0),
                delta_ns=0 if smoke else 15000, p="0" if smoke else "0.5", seed=seed,
                storage_capacity_packets=128 if smoke else 4096, storage_capacity_bytes=262144 if smoke else 8388608),
                envelope=dict(min_q_bytes=64, max_q_bytes=1522, burst_bound_packets=32 if smoke else 1024), trace=None))
    return dict(schema="hng-v1", lines=lines)


def frame(seed, port, seq):
    identity = port*1024+seq
    arrival = (seq//8)*700
    vlan = 100+(seq+seed+port) % 2
    length = 76+(seq*17+seed*3+port*5) % 128
    data = bytearray((identity*13+i*7) % 256 for i in range(length))
    data[:6] = b"\xff"*6
    data[6:8] = bytes((2, port))
    data[12:18] = struct.pack("!HHH", 0x8100, vlan, 0x88b5)
    data[18:42] = struct.pack("<QQQ", identity, arrival, seq)
    return bytes(data)


def pcap(path, seed, port):
    with path.open("wb") as output:
        output.write(struct.pack("<IHHIIII", 0xa1b2c3d4, 2, 4, 0, 0, 65535, 1))
        for seq in range(1024):
            data = frame(seed, port, seq)
            # Deliberately unrelated timestamps: PMD capture timestamps do not pace input.
            output.write(struct.pack("<IIII", 123, 999999-seq, len(data), len(data)))
            output.write(data)


def eal(binary, lcores=4):
    return [binary]+eal_arguments(lcores)+["--no-pci", "--no-huge", "--no-telemetry",
            "--in-memory", f"--file-prefix=hng-io-{os.getpid()}"]


def smoke(binary, directory):
    for port in range(2):
        subprocess.run(["ip", "link", "add", f"hng{port}", "type", "veth", "peer", "name", f"peer{port}"], check=True)
        for interface in (f"hng{port}", f"peer{port}"):
            subprocess.run(["ip", "link", "set", interface, "up"], check=True)
    (directory/"config.json").write_text(json.dumps(config(0, smoke=True)))
    receivers = []
    for port in range(2):
        sock = socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.htons(3))
        sock.bind((f"peer{port}", 0)); sock.settimeout(0.1)
        sock.setsockopt(263, 8, 1)  # SOL_PACKET/PACKET_AUXDATA retains stripped VLAN evidence.
        receivers.append(sock)
    command = eal(binary)+[f"--vdev=net_af_packet{p},iface=hng{p}" for p in range(2)]
    process = subprocess.Popen(command, cwd=directory, env=dict(os.environ, RUNTIME_DIRECTORY=str(directory)),
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    observed = [[], []]
    expected = [[frame(0, 1-p, seq) for seq in range(32)] for p in range(2)]
    prefix = ""
    try:
        prefix = wait_for_output(process, b"layout=4-lcore")
        time.sleep(0.5)
        assert process.poll() is None, process.communicate()[0]
        seen = [[], []]
        for seq in range(32):
            for port in range(2):
                receivers[port].send(frame(0, port, seq))
            # One outstanding frame per direction avoids legal cross-line overtakes
            # when a busy CI runner batches several physical arrivals.
            deadline = time.monotonic()+5
            while time.monotonic()<deadline and any(len(x)<seq+1 for x in observed):
                for port, sock in enumerate(receivers):
                    try:
                        data, ancillary, flags, address = sock.recvmsg(2048, 1024)
                        assert not flags & (socket.MSG_TRUNC | socket.MSG_CTRUNC)
                        for level, kind, value in ancillary:
                            if level == 263 and kind == 8:
                                status, _, _, _, _, tci, tpid = struct.unpack("=IIIHHHH", value[:20])
                                if status & (1 << 4):
                                    data = data[:12]+struct.pack("!HH", tpid if status & (1 << 6) else 0x8100, tci)+data[12:]
                    except TimeoutError:
                        continue
                    if len(seen[port])<8:
                        seen[port].append((address[2], data[12:18].hex(), len(data)))
                    if address[2] != socket.PACKET_OUTGOING and data[12:14] == b"\x81\x00" and data[16:18] == b"\x88\xb5":
                        observed[port].append(data)
            assert all(len(x)==seq+1 for x in observed), f"af_packet missing frame {seq}: {seen}"
        assert observed == expected, f"af_packet output mismatch: observed counts {[len(x) for x in observed]}, seen={seen}"
    finally:
        process.send_signal(signal.SIGINT)
        output, _ = process.communicate(timeout=12)
        output = prefix+output
        if observed != expected:
            print(output, file=sys.stderr)
        for sock in receivers:
            sock.close()
    assert process.returncode == 0, output
    assert "conservation=PASS byte_conservation=PASS status=DRAINED" in output, output
    counters = re.search(r"rx=(\d+) rx_bytes=\d+ tx_accepted=(\d+) tx_bytes=\d+ software_drops=(\d+) drop_bytes=\d+ invalid_input=(\d+)", output)
    assert counters, output
    received, transmitted, dropped, rejected = map(int, counters.groups())
    assert transmitted == 64 and received == 64+rejected and dropped == rejected, output
    print(f"af_packet namespace/veth bidirectional input=64 output=64 background_rejected={rejected} every_ID_bytes_order=PASS conservation=PASS drained=PASS")


def main():
    mode, binary = sys.argv[1:3]
    binary = str(Path(binary).resolve())
    if mode == "namespace-child":
        smoke(binary, Path(sys.argv[3])); return
    if mode == "af_packet":
        if not all(shutil.which(tool) for tool in ("unshare", "ip")):
            print("SKIP af_packet: unshare and ip executables are required")
            sys.exit(77)
        probe = subprocess.run(["unshare", "--user", "--map-root-user", "--net", "sh", "-c",
            "ip link add probe0 type veth peer name probe1"], capture_output=True, text=True)
        if probe.returncode:
            print("SKIP af_packet: disposable user/network namespace veth probe failed: "+probe.stderr.strip())
            sys.exit(77)
        with tempfile.TemporaryDirectory(prefix="hng-af-packet-") as directory:
            subprocess.run(["unshare", "--user", "--map-root-user", "--net", sys.executable,
                str(Path(__file__).resolve()), "namespace-child", binary, directory], check=True)
        return
    with tempfile.TemporaryDirectory(prefix="hng-io-") as temporary:
        directory = Path(temporary)
        for seed in range(32):
            (directory/f"seed{seed}.json").write_text(json.dumps(config(seed)))
        environment = dict(os.environ, IO_DIR=str(directory), IO_MODE=mode)
        if mode in ("ring", "ring-stepped", "ring-split", "ring-stepped-split", "ring-io-split", "ring-stepped-io-split", "ring-io-split-lcores"):
            lcores = 7 if "-io-split" in mode else 5 if mode.endswith("-split") else 4
            subprocess.run(eal(binary, lcores), env=environment, check=True, timeout=240)
        elif mode == "pcap":
            for seed in range(32):
                # A fresh directory also prevents stale output from satisfying the checker.
                run = directory/f"run{seed}"; run.mkdir()
                (run/f"seed{seed}.json").write_text(json.dumps(config(seed)))
                for port in range(2):
                    pcap(run/f"in{port}.pcap", seed, port)
                command = eal(binary)+[f"--vdev=net_pcap{p},rx_pcap={run}/in{p}.pcap,tx_pcap={run}/out{p}.pcap" for p in range(2)]
                subprocess.run(command, env=dict(environment, IO_DIR=str(run), IO_SEED=str(seed)), check=True, timeout=20)
        else:
            raise ValueError(f"unknown mode: {mode}")


if __name__ == "__main__":
    main()
