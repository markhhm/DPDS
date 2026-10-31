#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Run two independent unittest groups concurrently; wait for both results."""
import subprocess
import sys

separator = sys.argv.index("--")
groups = (sys.argv[1:separator], sys.argv[separator + 1:])
if not all(groups):
    raise ValueError("two nonempty unittest groups are required")
processes = [subprocess.Popen([sys.executable, "-m", "unittest", *group]) for group in groups]
results = [process.wait() for process in processes]
sys.exit(int(any(results)))
