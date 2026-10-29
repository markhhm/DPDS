#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Map test lcores onto allowed CPUs; oversubscription is for integrity only."""
import os
import sys


def eal_arguments(count):
    cpus = sorted(os.sched_getaffinity(0))
    return ["--lcores="+",".join(f"{i}@{cpus[i % len(cpus)]}" for i in range(count))]


if __name__ == "__main__":
    os.execv(sys.argv[1], [sys.argv[1]]+eal_arguments(1)+sys.argv[2:])
