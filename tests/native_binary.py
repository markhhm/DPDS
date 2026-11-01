#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""One deterministic native capture must survive binary transport byte for byte."""
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile

from eal_args import eal_arguments
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools.g2.native_adapter import NativeAdapter
from tools.g2.reference import Reference
from tools.g2.protocol import StreamReader
from tools.g2.protocol import StreamError


def main(binary):
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        result = subprocess.run([str(Path(binary).resolve()), *eal_arguments(1), '--no-pci', '--no-huge',
            '--no-telemetry', '--in-memory', '--file-prefix=hng-binary', '--', str(root)],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=20)
        assert result.returncode == 0, result.stdout
        from tools.g2.native_binary import decode
        decoded = io.StringIO()
        with (root / 'native.hngb').open('rb') as source:
            health = decode(source, decoded)
        golden = (root / 'native.jsonl').read_text()
        assert decoded.getvalue() == golden, 'binary transport changed legacy JSONL bytes'
        assert health['complete'], health
        events = [json.loads(line) for line in golden.splitlines()[1:-1]]
        assert {row['kind'] for row in events} == set(range(8))
        assert any(row['items'] and row['items'][0][0] == 2**64-1 for row in events)
        assert any(row['at_ns'] > 2**53 for row in events)
        assert {row['accepted_count'] for row in events if row['kind'] == 6} == {0,1}
        config = json.loads(Path('config.json').read_text())
        bundles = []
        for text in (golden, decoded.getvalue()):
            streams = [io.StringIO() for _ in range(3)]
            state = NativeAdapter(io.StringIO(text), *streams, run='binary-roundtrip', lines=config['lines']).process()
            assert state['complete'], state
            blobs = [s.getvalue() for s in streams]
            ingress = root / 'ingress.jsonl'; ingress.write_text(blobs[0])
            oracle = Reference(config, 'binary-roundtrip')
            expected = [value for row in StreamReader(ingress, 'ingress') if (value := oracle.consume(row)) is not None]
            assert len(expected) == 4 and sum(row['outcome'] == 'accepted' for row in expected) == 2
            assert oracle.finish()['input_complete']
            blobs.append(json.dumps(expected, separators=(',', ':')) + '\n')
            bundles.append(blobs)
        assert bundles[0] == bundles[1], 'binary transport changed normalized bundle/reference bytes'
        incomplete = io.StringIO()
        with (root / 'queue-loss.hngb').open('rb') as source:
            loss = decode(source, incomplete)
        assert not loss['complete'] and loss['producers'][0]['dropped'] == 1
        broken = io.StringIO()
        with (root / 'write-failure.hngb').open('rb') as source:
            try:
                decode(source, broken)
            except StreamError:
                pass
            else:
                raise AssertionError('partial binary write produced a success seal')
        assert '"kind":"health"' not in broken.getvalue()
        print('native binary: raw JSONL and normalized bundle/reference byte-exact PASS')


if __name__ == '__main__':
    main(sys.argv[1])
