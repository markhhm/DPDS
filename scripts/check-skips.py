#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Make Meson SKIP results visible and fail on any skip not explicitly allowed.

Usage: check-skips.py TESTLOG_JSON. DPDS_ALLOW_SKIP holds space-separated test
names that may skip (for example io-af-packet on runners without unprivileged
user namespaces). Allowed skips are still printed and reported to GitHub Actions.
"""
import json
import os
import sys

allowed = set(os.environ.get("DPDS_ALLOW_SKIP", "").split())
with open(sys.argv[1]) as log:
    results = [json.loads(line) for line in log if line.strip()]
# Meson names tests "project:test"; accept either form in DPDS_ALLOW_SKIP.
skipped = [r["name"] for r in results if r["result"] == "SKIP"]
allowed |= {n for n in skipped if n.split(":", 1)[-1] in allowed}
lines = [f"Meson tests: {len(results)} run, {len(skipped)} skipped"]
lines += [f"SKIPPED: {name}" + (" (allowed by DPDS_ALLOW_SKIP)" if name in allowed else " (NOT allowed)")
          for name in skipped]
print("\n".join(lines))
if os.environ.get("GITHUB_ACTIONS"):
    for name in skipped:
        print(f"::warning title=Skipped test::{name} was skipped; it did not pass")
    with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as summary:
        summary.write("\n".join(lines) + "\n")
sys.exit(1 if set(skipped) - allowed else 0)
