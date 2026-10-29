#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Exercise process startup, all stage layouts, signal shutdown, and mbuf reclamation.

SIGINT may arrive at any point of startup, including before ports and pools
exist; the run must still shut down cleanly and print its counters. Delays of
0 to 0.25 s cover signals during EAL init, engine load and port setup.
"""
import os
import signal
import subprocess
import sys
import time
from eal_args import eal_arguments
from startup import wait_for_output

binary = os.path.abspath(sys.argv[1])
cases = [(cores, no_rx, 0.25) for cores in (2, 3, 4, 5) for no_rx in (True, False)]
# Regression: early signals used to kill dpds before its handler was installed.
# Those delays count from the first EAL line, so main() is already running.
cases += [(cores, True, -delay) for cores in (2, 5) for delay in (0.001, 0.01, 0.05, 0.1)]
for cores, no_rx, delay in cases:
    suffix = ",no-rx=1" if no_rx else ""
    command = [binary]+eal_arguments(cores)+["--no-pci", "--no-huge",
               "--no-telemetry", "-m", "256", f"--file-prefix=hng-null-{os.getpid()}-{cores}-{no_rx}-{delay}",
               f"--vdev=net_null0{suffix}", f"--vdev=net_null1{suffix}"]
    if cores == 2 and no_rx and delay > 0:
        # Readiness must also tolerate a delayed exec of the real DUT.
        command = [sys.executable, "-c", "import os,sys,time\ntime.sleep(0.4)\nos.execv(sys.argv[1],sys.argv[1:])"]+command
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        prefix = wait_for_output(process, b"EAL:" if delay < 0 else f"layout={cores}-lcore".encode())
        time.sleep(abs(delay))
        process.send_signal(signal.SIGINT)
        output, _ = process.communicate(timeout=12)
        output = prefix+output
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=2)
    assert process.returncode == 0, f"dpds returned {process.returncode} after SIGINT at {delay} s (negative: after the first EAL line):\n{output}"
    assert f"layout={cores}-lcore" in output, output
    assert "conservation=PASS byte_conservation=PASS status=DRAINED" in output, output
    assert output.count("owned=0") == 2, output
    for port in (0, 1):
        assert output.index(f"descriptors port={port} ") < output.index("pool port=0 "), output
    print(f"null PMD {cores} lcores no_rx={no_rx} sigint_after={delay}s: PASS")
