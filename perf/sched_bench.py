#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Run hng-sched-bench with main and two owner lcores on separate allowed CPUs.

Usage: sched_bench.py BINARY [--cpus A,B,C] [--memory MB] -- [hng-sched-bench options].
Without --cpus it uses the first three allowed CPUs (shared if fewer exist).
"""
import os
import subprocess
import sys

args = sys.argv[1:]
binary = os.path.abspath(args.pop(0))
cpus, memory = None, "512"
while args and args[0] != "--":
    flag, value = args.pop(0), args.pop(0)
    if flag == "--cpus":
        cpus = [int(c) for c in value.split(",")]
    elif flag == "--memory":
        memory = value
    else:
        sys.exit(__doc__)
extra = args[1:] if args else []
if cpus is None:
    allowed = sorted(os.sched_getaffinity(0))
    cpus = [allowed[i % len(allowed)] for i in range(3)]
command = [binary, "--lcores=" + ",".join(f"{i}@{c}" for i, c in enumerate(cpus)), "--no-pci", "--no-huge",
           "--no-telemetry", "-m", memory, f"--file-prefix=hng-sched-{os.getpid()}", "--"] + extra
sys.exit(subprocess.run(command).returncode)
