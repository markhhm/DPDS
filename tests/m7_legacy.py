#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Default data-path bytes match the deterministic pre-M7 binary capture."""
import hashlib
from pathlib import Path
import subprocess
import sys
import tempfile
from eal_args import eal_arguments

# Captured twice with b35a28b0 data-path sources and their f3d255dc Core pin.
# No timestamps, fields or padding bytes are stripped from the output files.
GOLDEN = {
    'native.jsonl': '12ea24f5590e62d45ceac1bc8bdc00dec0b4a146b6f5063f4ee0634e9acab13e',
    'native.hngb': '4a34086f0f0253fdc036b2368ec3e03eb17eec648a17308b181d378128e4588f',
}


def main(binary, config=None):
    with tempfile.TemporaryDirectory() as directory:
        result = subprocess.run([str(Path(binary).resolve()), *eal_arguments(1), '--no-pci',
            '--no-huge', '--no-telemetry', '--in-memory', '--file-prefix=hng-m7-legacy',
            '--', directory, *([config] if config else [])], capture_output=True, text=True, timeout=25)
        assert result.returncode == 0, result.stdout + result.stderr
        for name, expected in GOLDEN.items():
            actual = hashlib.sha256((Path(directory) / name).read_bytes()).hexdigest()
            assert actual == expected, f'default legacy bytes changed: {name}: {actual} != {expected}'
    print('default native JSONL/binary bytes equal pre-M7 capture: PASS')


if __name__ == '__main__':
    main(*sys.argv[1:])
