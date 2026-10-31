# SPDX-License-Identifier: Apache-2.0
"""Concurrent unittest groups must retain every result and propagate failures."""
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


class RunnerTests(unittest.TestCase):
    def run_groups(self, first_passes):
        runner = Path(__file__).resolve().parents[3] / "scripts/run-unittest-groups.py"
        with tempfile.TemporaryDirectory() as directory:
            for name, passes in (("first", first_passes), ("second", True)):
                peer = "second" if name == "first" else "first"
                (Path(directory) / (name + ".py")).write_text(
                    "import pathlib, time, unittest\nclass Fixture(unittest.TestCase):\n"
                    f"    def test_runs(self):\n        pathlib.Path({name!r} + '-started').touch()\n"
                    "        deadline = time.monotonic() + 3\n"
                    f"        while not pathlib.Path({peer!r} + '-started').exists():\n"
                    "            self.assertLess(time.monotonic(), deadline, 'groups ran serially')\n"
                    "            time.sleep(0.005)\n"
                    f"        pathlib.Path({name!r} + '-ran').write_text('complete')\n"
                    f"        self.assertTrue({passes!r})\n")
            result = subprocess.run([sys.executable, str(runner), "first", "--", "second"],
                                    cwd=directory, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0 if first_passes else 1, result.stderr)
            for name in ("first", "second"):
                self.assertEqual((Path(directory) / (name + "-ran")).read_text(), "complete")

    def test_all_passing_groups_succeed(self):
        self.run_groups(True)

    def test_failing_group_fails_runner_and_other_group_finishes(self):
        self.run_groups(False)
