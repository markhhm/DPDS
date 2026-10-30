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
# Numeric options: strict parsing. 100e9 is an integer written with an exponent and must equal 100000000000.
def gib(args):
    out = subprocess.run([binary]+args, capture_output=True, text=True, check=True).stdout
    return re.search(r"per_numa_node envelope .* mempool_gib=([\d.]+)", out).group(1)
assert gib(["--c-bps", "100e9"]) == gib(["--c-bps", "100000000000"]) == node.group(3)
assert gib(["--t-ms", "1.42e2"]) == gib(["--t-ms", "142"])
for option, bad in [("--c-bps", ["", "abc", "100G", "12x", "-5", " 7", "1.5e9", "1e30", "100e", "e9", "99999999999999999999"]),
                    ("--t-ms", ["", "abc", "12ms", "-1", "nan", "inf", "1e400", "1e30"]),
                    ("--delta-ms", ["x", "-0.5"]), ("--q-ms", ["", "1,5"]),
                    ("--p", ["", "abc", "1.5", "-0.1", "nan", "0.1x"]),
                    ("--min-frame", ["", "6x4", "-64"]), ("--max-frame", ["abc"]),
                    ("--burst", ["", "0x10", "-1"]), ("--lines-per-port", ["", "2x", "-1", "4294967297"]),
                    ("--lcores", ["", "7x", "4294967303"]), ("--rx-desc", ["", "q"]), ("--tx-desc", ["-1"]),
                    ("--page-bytes", ["", "2M"]), ("--trace-max-delay-ms", ["x"]), ("--trace-max-hold-ms", ["-1"])]:
    for value in bad:
        run = subprocess.run([binary, option, value], capture_output=True, text=True)
        assert run.returncode != 0 and run.stderr and not run.stdout, (option, value, run.returncode, run.stdout[:80])
run = subprocess.run([binary]+sum([["--p", "0.1"]]*9, []), capture_output=True, text=True)
assert run.returncode != 0, "nine --p values must be rejected, not truncated"
# Trace mode sizes storage for the maximum trace delay plus the longest outage hold.
def slots(args):
    out = subprocess.run([binary]+args, capture_output=True, text=True, check=True).stdout
    return int(re.search(r"slots_per_line=(\d+)", out).group(1)), out
trace, out = slots(["--trace-max-delay-ms", "600", "--trace-max-hold-ms", "40"])
fixed, _ = slots(["--t-ms", "640", "--delta-ms", "0"])
assert trace == fixed and "mode=trace" in out and "mean_occupancy" not in out, out
# A small configuration that fits in the default --no-huge memory.
command = [binary, "-l", "0", "--no-pci", "--no-huge", "--no-telemetry", "-m", "512",
           "--in-memory", f"--file-prefix=hng-pool-{os.getpid()}", "--vdev=net_null0", "--vdev=net_null1", "--",
           "--c-bps", "1000000000", "--t-ms", "20", "--delta-ms", "40", "--q-ms", "5",
           "--burst", "256", "--lines-per-port", "2", "--verify"]
run = subprocess.run(command, capture_output=True, text=True)
print(run.stdout, end="")
assert run.returncode == 0 and "gate_5pct=PASS" in run.stdout, run.stdout + run.stderr
# Object counts are checked against the real mempool built by the production pool function.
assert run.stdout.count("objects_match=PASS") == 2 and "objects_mempool=" in run.stdout, run.stdout
