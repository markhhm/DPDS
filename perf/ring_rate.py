#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Run hng-ring-rate and require a conserved, drained run.

Usage: ring_rate.py [--split|--io-split] [--cpus A,B,...] BINARY [CONFIG] [-- extra hng-ring-rate options].
--split uses five lcores: one scheduler per ingress port.
--io-split uses seven lcores: one RX, scheduler and TX lcore per direction.
--cpus pins lcore i to the i-th listed CPU (main lcore first); without it,
lcores map onto allowed CPUs in order, which may put two lcores on one core.
Without extra options it runs a short smoke check suitable for the test suite.
"""
import os
import subprocess
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "tests"))
from eal_args import eal_arguments  # noqa: E402

lcores = {"--split": 5, "--io-split": 7}.get(sys.argv[1], 4)
if lcores != 4:
    sys.argv.pop(1)
cpus = None
if sys.argv[1] == "--cpus":
    cpus = [int(c) for c in sys.argv[2].split(",")]
    del sys.argv[1:3]
    assert len(cpus) >= lcores, "--cpus needs one CPU per lcore"
binary = os.path.abspath(sys.argv[1])
config = os.path.abspath(sys.argv[2]) if len(sys.argv) > 2 and sys.argv[2] != "--" else \
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "ring_rate.json")
extra = sys.argv[sys.argv.index("--")+1:] if "--" in sys.argv else ["-d", "0.2", "-w", "0.05"]
command = [binary]+(["--lcores="+",".join(f"{i}@{c}" for i, c in enumerate(cpus[:lcores]))] if cpus else eal_arguments(lcores))+["--no-pci", "--no-huge", "--no-telemetry", "-m", "512",
           "--in-memory", f"--file-prefix=hng-rate-{os.getpid()}", "--", "-c", config]+extra
result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=600)
print(result.stdout, end="")
assert result.returncode == 0, "hng-ring-rate failed"
assert "conservation=PASS status=DRAINED" in result.stdout, "run was not conserved and drained"
# Q holds 1033 MTU frames per line, more than one direction's population, so a Q drop means a broken run.
assert " q_drop=0 " in result.stdout or not config.endswith("perf/ring_rate.json"), "the rate fixture must not drop in Q"
assert "tx_burst_hist=" in result.stdout, "no TX burst histogram"
assert "total_pps=" in result.stdout and " total_pps=0 " not in result.stdout, "no packets measured"
