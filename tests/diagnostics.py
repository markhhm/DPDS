#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Read live atomic diagnostics, stop RX, compare final statistics to telemetry keys."""
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tempfile
import time
from eal_args import eal_arguments
from startup import wait_for_output

binary = str(Path(sys.argv[1]).resolve())
for cores in (2, 3, 4, 5, 7):
    with tempfile.TemporaryDirectory(prefix="hng-diag-") as directory:
        root = Path(directory)
        command = [binary, *eal_arguments(cores), "--no-pci", "--no-huge", "--in-memory", "-m", "256",
                   "--telemetry", "--file-prefix=diag", "--vdev=net_null0,no-rx=1", "--vdev=net_null1,no-rx=1",
                   "--", "-o", str(root / "stats.txt")]
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                   env=dict(os.environ, RUNTIME_DIRECTORY=directory))
        output = ""
        try:
            output = wait_for_output(process, f"layout={cores}-lcore".encode())
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
            sock.settimeout(2)
            sock.connect(str(root / "dpdk/diag/dpdk_telemetry.v2"))
            json.loads(sock.recv(65536))
            live = None
            active = [f"{role}0_loop_gap_max_ns" for role in ("rx", "scheduler", "tx")]
            if cores == 5:
                active.append("scheduler1_loop_gap_max_ns")
            if cores == 7:
                active += [f"{role}1_loop_gap_max_ns" for role in ("rx", "scheduler", "tx")]
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                sock.sendall(b"/dpds/diagnostics")
                live = json.loads(sock.recv(65536))["/dpds/diagnostics"]
                assert isinstance(live, dict), live
                if all(live[key] > 0 for key in active):
                    break
                time.sleep(.001)
            assert live is not None and all(live[key] > 0 for key in active), live
            assert live["rx0_stopped_mono_ns"] == 0, live
            assert live["first_invalid_recorded"] == 0 and live["first_invalid_reason"] == "none", live
            # An unknown parameter must not silently return a seemingly valid snapshot.
            sock.sendall(b"/dpds/diagnostics,unexpected")
            assert json.loads(sock.recv(65536))["/dpds/diagnostics"] is None
            sock.close()
            before = time.monotonic_ns()
            process.send_signal(signal.SIGINT)
            tail, _ = process.communicate(timeout=12)
            output += tail.decode()
            after = time.monotonic_ns()
            assert process.returncode == 0, output
            stats = (root / "stats.txt").read_text()
            fields = dict(token.split("=", 1) for token in stats.split() if "=" in token)
            assert set(live) <= fields.keys(), (set(live)-fields.keys(), stats)
            assert before <= int(fields["rx0_stopped_mono_ns"]) <= after, stats
            assert fields["first_invalid_recorded"] == "0" and fields["first_invalid_reason"] == "none", stats
            assert "status=DRAINED" in stats and output.count("owned=0") == 2, output
            for key in live:
                if key.endswith("_max_ns") or key.endswith("_high_water"):
                    assert int(fields[key]) >= live[key], (key, live, stats)
            print(f"diagnostics {cores} lcores: live telemetry, RX stop time, final statistics: PASS")
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate(timeout=2)
