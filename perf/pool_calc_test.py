#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Check hng-pool-calc: roadmap output, p-independence, and a real --no-huge allocation."""
import os
import re
import subprocess
import sys

binary = os.path.abspath(sys.argv[1])
calc = subprocess.run([binary], capture_output=True, text=True, check=True).stdout
print(calc, end="")
node = re.search(r"per_numa_node envelope slots_per_line=(\d+) pool_objects=(\d+) mempool_gib=([\d.]+)", calc)
assert node and float(node.group(3)) > 0, calc
assert calc.count("mean_occupancy") == 2, calc
# A small configuration that fits in the default --no-huge memory.
command = [binary, "-l", "0", "--no-pci", "--no-huge", "--no-telemetry", "-m", "512",
           f"--file-prefix=hng-pool-{os.getpid()}", "--vdev=net_null0", "--vdev=net_null1", "--",
           "--c-bps", "1000000000", "--t-ms", "20", "--delta-ms", "40", "--q-ms", "5",
           "--burst", "256", "--lines-per-port", "2", "--verify"]
run = subprocess.run(command, capture_output=True, text=True)
print(run.stdout, end="")
assert run.returncode == 0 and "gate_5pct=PASS" in run.stdout, run.stdout + run.stderr
