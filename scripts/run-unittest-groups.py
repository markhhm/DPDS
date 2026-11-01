#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Run independent unittest groups concurrently; wait for every result."""
import subprocess
import sys

groups = [[]]
for argument in sys.argv[1:]:
    if argument == "--":
        groups.append([])
    else:
        groups[-1].append(argument)
if len(groups) < 2 or not all(groups):
    raise ValueError("at least two nonempty unittest groups are required")
processes = [subprocess.Popen([sys.executable, "-m", "unittest", *group]) for group in groups]
results = [process.wait() for process in processes]
sys.exit(int(any(results)))
