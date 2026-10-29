#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Exercise process startup, all stage layouts, signal shutdown, and mbuf reclamation."""
import os
import signal
import subprocess
import sys
import time
from eal_args import eal_arguments

binary = os.path.abspath(sys.argv[1])
for cores in (2, 3, 4, 5):
    for no_rx in (True, False):
        suffix = ",no-rx=1" if no_rx else ""
        command = [binary]+eal_arguments(cores)+["--no-pci", "--no-huge",
                   "--no-telemetry", f"--file-prefix=hng-null-{os.getpid()}-{cores}-{no_rx}",
                   f"--vdev=net_null0{suffix}", f"--vdev=net_null1{suffix}"]
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        time.sleep(0.25)
        process.send_signal(signal.SIGINT)
        output, _ = process.communicate(timeout=12)
        assert process.returncode == 0, output
        assert f"layout={cores}-lcore" in output, output
        assert "conservation=PASS byte_conservation=PASS status=DRAINED" in output, output
        assert output.count("owned=0") == 2, output
        for port in (0, 1):
            assert output.index(f"descriptors port={port} ") < output.index("pool port=0 "), output
        print(f"null PMD {cores} lcores no_rx={no_rx}: PASS")
