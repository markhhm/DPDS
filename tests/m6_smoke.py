#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Three real PCAP acquisitions followed by the sequential software reporter."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile

from m6_fixture import capture


def main(binary):
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        runs = []
        for family in ('main', 'boundary', 'trace'):
            directory = root / family
            directory.mkdir()
            capture(binary, family, directory)
            runs.append(dict(id=family, family=family, manifest=f'{family}/manifest.json'))
        matrix = json.loads((Path(__file__).resolve().parents[1] / 'examples/m6-short.json').read_text())
        matrix['runs'] = runs
        (root / 'matrix.json').write_text(json.dumps(matrix))
        process = subprocess.run([sys.executable, '-m', 'tools.g2.matrix', str(root / 'matrix.json'),
                                  '--out', str(root / 'reports')], capture_output=True, text=True)
        summary = json.loads((root / 'reports/summary.json').read_text())
        assert len(summary['runs']) == 3, summary
        assert not summary['g2_qualified'], summary
        for run in summary['runs']:
            report = json.loads((root / 'reports' / run['report']).read_text())
            assert run['measurement_status'] == report['measurement_status'], run
            assert report['measurement_status'] in ('PASS', 'FAIL', 'INCONCLUSIVE'), run
            assert run['rows'] and all(row['required'] for row in run['rows']), run
            assert report['scope'] == 'software-only' and not report['g2_qualified'], report
            assert {stream['role'] for stream in report['stream_integrity']} == {
                'ingress', 'before', 'receiver', 'host', 'counters', 'expected'}, run
            assert all(stream['complete'] and not stream['issues'] for stream in report['stream_integrity']), run
            assert not any(code.startswith('reference_') for code in report['reason_codes']), run
            print(f"M6 {run['id']}: real PCAP bytes/custody checked; reporter={run['measurement_status']}")
        expected = ('FAIL' if any(run['measurement_status'] == 'FAIL' for run in summary['runs']) else
                    'PASS' if all(run['measurement_status'] == 'PASS' for run in summary['runs']) else 'INCONCLUSIVE')
        assert summary['measurement_status'] == expected, summary
        assert process.returncode == {'PASS': 0, 'FAIL': 1, 'INVALID': 2}[summary['status']], process.stderr
        print('M6 three-cell sequential software-only smoke: PASS')


if __name__ == '__main__':
    main(str(Path(sys.argv[1]).resolve()))
