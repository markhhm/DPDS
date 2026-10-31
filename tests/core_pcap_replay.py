#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Run the pinned core's pcap tests and reject empty or skipped suites."""
from pathlib import Path
import unittest

root = Path(__file__).resolve().parents[1] / 'dpds-core'
suite = unittest.defaultTestLoader.discover(str(root / 'tests'), pattern='test_pcap_replay.py')
result = unittest.TextTestRunner(verbosity=2).run(suite)
print(f'Core pcap replay: run={result.testsRun} skipped={len(result.skipped)}', flush=True)
if not result.wasSuccessful() or not result.testsRun or result.skipped:
    raise SystemExit(1)
