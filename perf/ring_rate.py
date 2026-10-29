#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Run hng-ring-rate and require a conserved, drained run.

Usage: ring_rate.py [--split|--io-split] BINARY [CONFIG] [-- extra hng-ring-rate options].
--split uses five lcores: one scheduler per ingress port.
--io-split uses seven lcores: one RX, scheduler and TX lcore per direction.
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
binary = os.path.abspath(sys.argv[1])
config = os.path.abspath(sys.argv[2]) if len(sys.argv) > 2 and sys.argv[2] != "--" else \
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "ring_rate.json")
extra = sys.argv[sys.argv.index("--")+1:] if "--" in sys.argv else ["-d", "0.2", "-w", "0.05"]
command = [binary]+eal_arguments(lcores)+["--no-pci", "--no-huge", "--no-telemetry", "-m", "512",
           f"--file-prefix=hng-rate-{os.getpid()}", "--", "-c", config]+extra
result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=600)
print(result.stdout, end="")
assert result.returncode == 0, "hng-ring-rate failed"
assert "conservation=PASS status=DRAINED" in result.stdout, "run was not conserved and drained"
assert "total_pps=" in result.stdout and " total_pps=0 " not in result.stdout, "no packets measured"
