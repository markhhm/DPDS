#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Repeated close must include the control writer's final resource publication."""
from pathlib import Path
import sys
import tempfile
from observer_export import run

with tempfile.TemporaryDirectory() as tmp:
    for iteration in range(20):
        path = Path(tmp)/str(iteration)
        path.mkdir()
        run(str(Path(sys.argv[1]).resolve()), 2, True, path, binary=True)
print('observer shutdown: 20 final-tail/stop interleavings PASS')
