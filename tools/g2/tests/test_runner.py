# SPDX-License-Identifier: Apache-2.0
"""Concurrent unittest groups must retain every result and propagate failures."""
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


class RunnerTests(unittest.TestCase):
    def run_groups(self, passes, count=2, fail_index=0):
        runner = Path(__file__).resolve().parents[3] / "scripts/run-unittest-groups.py"
        with tempfile.TemporaryDirectory() as directory:
            names = ('first', 'second', 'third', 'fourth')[:count]
            for index, name in enumerate(names):
                succeeds = passes or index != fail_index
                (Path(directory) / (name + ".py")).write_text(
                    "import pathlib, time, unittest\nclass Fixture(unittest.TestCase):\n"
                    f"    def test_runs(self):\n        pathlib.Path({name!r} + '-started').touch()\n"
                    "        deadline = time.monotonic() + 3\n"
                    f"        while not all(pathlib.Path(n + '-started').exists() for n in {names!r}):\n"
                    "            self.assertLess(time.monotonic(), deadline, 'groups ran serially')\n"
                    "            time.sleep(0.005)\n"
                    f"        pathlib.Path({name!r} + '-ran').write_text('complete')\n"
                    f"        self.assertTrue({succeeds!r})\n")
            command = [sys.executable, str(runner), names[0]]
            for name in names[1:]:
                command += ['--', name]
            result = subprocess.run(command,
                                    cwd=directory, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0 if passes else 1, result.stderr)
            for name in names:
                self.assertEqual((Path(directory) / (name + "-ran")).read_text(), "complete")

    def test_all_passing_groups_succeed(self):
        self.run_groups(True)

    def test_failing_group_fails_runner_and_other_group_finishes(self):
        self.run_groups(False)

    def test_multiple_groups_run_concurrently_and_all_complete(self):
        for count in (3, 4):
            with self.subTest(count=count):
                self.run_groups(True, count=count)

    def test_last_group_failure_cannot_be_hidden(self):
        for count in (3, 4):
            with self.subTest(count=count):
                self.run_groups(False, count=count, fail_index=count-1)

    def test_empty_group_is_rejected_before_running_tests(self):
        runner = Path(__file__).resolve().parents[3] / 'scripts/run-unittest-groups.py'
        for arguments in ([], ['first'], ['first', '--'], ['--', 'second'], ['first', '--', '--', 'second']):
            with self.subTest(arguments=arguments):
                result = subprocess.run([sys.executable, str(runner), *arguments],
                                        capture_output=True, text=True)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('nonempty unittest groups', result.stderr)
