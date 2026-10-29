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
assert " lcores=7 " in calc and "page_bytes=2097152 (unverified)" in calc, calc
# Each lcore adds one mempool cache (MBUF_CACHE_SIZE = 32): 7 lcores need 64 objects more than 5.
five, _ = (lambda o: (int(re.search(r"pool_objects=(\d+)", o).group(1)), o))(
    subprocess.run([binary, "--lcores", "5"], capture_output=True, text=True, check=True).stdout)
assert int(node.group(2)) - five == 2 * 32, (node.group(2), five)
# Trace mode sizes storage for the maximum trace delay plus the longest outage hold.
def slots(args):
    out = subprocess.run([binary]+args, capture_output=True, text=True, check=True).stdout
    return int(re.search(r"slots_per_line=(\d+)", out).group(1)), out
trace, out = slots(["--trace-max-delay-ms", "600", "--trace-max-hold-ms", "40"])
fixed, _ = slots(["--t-ms", "640", "--delta-ms", "0"])
assert trace == fixed and "mode=trace" in out and "mean_occupancy" not in out, out
# A small configuration that fits in the default --no-huge memory.
command = [binary, "-l", "0", "--no-pci", "--no-huge", "--no-telemetry", "-m", "512",
           f"--file-prefix=hng-pool-{os.getpid()}", "--vdev=net_null0", "--vdev=net_null1", "--",
           "--c-bps", "1000000000", "--t-ms", "20", "--delta-ms", "40", "--q-ms", "5",
           "--burst", "256", "--lines-per-port", "2", "--verify"]
run = subprocess.run(command, capture_output=True, text=True)
print(run.stdout, end="")
assert run.returncode == 0 and "gate_5pct=PASS" in run.stdout, run.stdout + run.stderr
# Object counts are checked against the real mempool built by the production pool function.
assert run.stdout.count("objects_match=PASS") == 2 and "objects_mempool=" in run.stdout, run.stdout
