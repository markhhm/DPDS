#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Run the production cleanup, including late INVALID metadata, against retained mbufs."""
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
from eal_args import eal_arguments
from startup import wait_for_output

binary = str(Path(sys.argv[1]).resolve())
for invalid, startup in ((False, False), (True, False), (True, True)):
    with tempfile.TemporaryDirectory(prefix="hng-pool-") as directory:
        stats = Path(directory) / "stats.txt"
        env = dict(os.environ, RUNTIME_DIRECTORY=directory)
        if invalid:
            env["HNG_TEST_START_INVALID" if startup else "HNG_TEST_CLOSE_INVALID"] = "1"
        command = [binary, *eal_arguments(7), "--no-pci", "--no-huge", "--in-memory", "--no-telemetry",
                   "-m", "256", "--file-prefix=hng-pool", "--vdev=net_null0,no-rx=1", "--vdev=net_null1,no-rx=1",
                   "--", "-o", str(stats)]
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env)
        output = ""
        try:
            output = wait_for_output(process, b"layout=7-lcore")
            process.send_signal(signal.SIGINT)
            tail, _ = process.communicate(timeout=12)
            output += tail.decode()
            assert process.returncode == int(invalid), output
            assert output.count("owned=0") == 2, output
            report = stats.read_text()
            assert f"status={'INVALID' if invalid else 'DRAINED'}" in report, report
            assert f"first_invalid_recorded={int(invalid)}" in report, report
            if startup:
                assert "first_invalid_reason=fixture runtime invalid" in report, report
                fields = dict(line.split("=", 1) for line in report.splitlines() if "=" in line and " " not in line)
                anchor = int(fields["native_observer_epoch_monotonic_ns"])
                gap = int(fields["native_observer_epoch_read_gap_ns"])
                first_mono = int(fields["first_invalid_mono_ns"])
                first_native = int(fields["first_invalid_at_ns"])
                for owner in (0, 1):
                    stopped = int(fields[f"rx{owner}_stopped_mono_ns"])
                    assert anchor <= first_mono <= stopped, report
                    # An upper-bracket anchor overestimates native-zero by at most gap.
                    assert anchor + first_native - gap <= stopped, report
                    for port in (0, 1):
                        closed_native = int(fields[f"port{port}_close_ns"])
                        closed_mono = int(fields[f"port{port}_close_monotonic_ns"])
                        close_gap = int(fields[f"port{port}_close_read_gap_ns"])
                        assert first_native <= closed_native, report
                        assert stopped <= closed_mono, report
                        assert stopped <= anchor + closed_native + close_gap, report
            elif invalid:
                assert "first_invalid_reason=fixture port cleanup failure" in report, report
                assert "first_invalid_owner=main:0" in report, report
            print(f"cross-port TX retention={986}, invalid={invalid}, startup={startup}: all ports close before pool free: PASS")
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate(timeout=2)
