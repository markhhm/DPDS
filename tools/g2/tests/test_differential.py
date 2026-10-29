# SPDX-License-Identifier: Apache-2.0
import os
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from tools.g2.compare import compare


class DifferentialTests(unittest.TestCase):
    def test_optimized_python_cannot_disable_mismatch_detection(self):
        with tempfile.TemporaryDirectory() as directory:
            bad = Path(directory) / "bad-core"
            bad.write_text('#!/bin/sh\ncat >/dev/null\nprintf \'%s\\n\' \'{"kind":"admission","ingress_seq":0,"outcome":"q_drop","q_bytes":0}\'\n')
            bad.chmod(0o755)
            command = [sys.executable, "-O", "-c",
                       "import sys; from tools.g2.compare import compare; compare(sys.argv[1], total=32, seeds=32)", str(bad)]
            result = subprocess.run(command, capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("mismatch", result.stderr)

    def test_million_ingress_events_match_production_core(self):
        default = Path(__file__).resolve().parents[3] / "dpds-core/target/release/examples/m2_replay"
        executable = Path(os.environ.get("G2_CORE_REPLAY", default))
        result = compare(executable, total=1 << 20, seeds=32)
        self.assertEqual(result["events"], 1048576)
        self.assertEqual(result["cases"], 32)
        self.assertEqual(result["mismatches"], 0)
        print(json.dumps(result, sort_keys=True))
