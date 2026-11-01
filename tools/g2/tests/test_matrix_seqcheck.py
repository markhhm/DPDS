# SPDX-License-Identifier: Apache-2.0
"""Real C seqcheck is generated/reused before sampled pcapng reporting."""
from contextlib import redirect_stderr
import hashlib
import io
import json
import shutil
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from tools.g2.tests.test_pcapng import setup as capture_pair, pcapng


def swap_probe_identities(path):
    data = bytearray(path.read_bytes())
    offsets, at = [], 0
    while at < len(data):
        kind, size = struct.unpack_from('<II', data, at)
        if kind == 6:
            offsets.append(at + 28 + 46)
        at += size
    first, second = offsets[:2]
    data[first:first+8], data[second:second+8] = data[second:second+8], data[first:first+8]
    path.write_bytes(data)


class MatrixSeqcheckTests(unittest.TestCase):
    def fixture(self, root, cases=('reordered', 'dropped', 'normal')):
        runs = []
        for run in cases:
            folder = root / run
            folder.mkdir()
            expected = capture_pair(folder, run=run)
            path = folder / 'source/manifest.json'
            manifest = json.loads(path.read_text())
            manifest['verification'] = dict(mode='sampled', seqchecks=[])
            path.write_text(json.dumps(manifest))
            if run == 'reordered':
                swap_probe_identities(folder / 'receiver.pcapng')
            elif run == 'dropped':
                pcapng(folder / 'receiver.pcapng', [row['release_ns']+3 for row in expected], drop=1)
            runs.append(dict(id=run, manifest=f'{run}/pair.json'))
        matrix = root / 'matrix.json'
        matrix.write_text(json.dumps(dict(schema='hng-m6-matrix-v1', scope='software-only',
                                          mode='pcapng', plan={}, runs=runs)))
        return matrix

    def run_matrix(self, matrix, output):
        result = subprocess.run([sys.executable, '-m', 'tools.g2.matrix', str(matrix), '--out', str(output)],
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 2, result.stderr)
        return json.loads((output / 'summary.json').read_text())

    def registration(self, output, run):
        path = output / run / 'capture/bundle/manifest.json'
        manifest = json.loads(path.read_text())
        checks = manifest['verification']['seqchecks']
        self.assertEqual(len(checks), 1, 'runner must generate and register C seqcheck evidence')
        registration = checks[0]
        evidence = path.parent / registration['path']
        self.assertEqual(hashlib.sha256(evidence.read_bytes()).hexdigest(), registration['sha256'])
        self.assertEqual((registration['ingress_port'], registration['egress_port']), (0, 1))
        return registration, json.loads(evidence.read_text())

    def test_three_real_capture_cells_generate_independent_checker_results(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            matrix = self.fixture(root)
            originals = {p: p.read_bytes() for p in root.rglob('*') if p.is_file()}
            output = root / 'out'
            summary = self.run_matrix(matrix, output)
            self.assertEqual(len(summary['runs']), 3)
            checks = [self.registration(output, run)[1] for run in ('reordered', 'dropped', 'normal')]
            self.assertEqual([doc['status'] for doc in checks], ['FAIL', 'INVALID', 'PASS'])
            self.assertEqual(checks[0]['totals']['ordinary_reorder'], 1)
            self.assertFalse(checks[1]['complete'])
            self.assertEqual(checks[1]['inputs']['receiver']['health']['capture_drops'], 1)
            reports = {run: json.loads((output / run / 'report.json').read_text())
                       for run in ('reordered', 'dropped', 'normal')}
            self.assertEqual(reports['reordered']['rows'][0]['gates']['order']['status'], 'FAIL')
            self.assertEqual(reports['dropped']['measurement_status'], 'INCONCLUSIVE')
            for gate in ('order', 'storage'):
                self.assertEqual(reports['normal']['rows'][0]['gates'][gate]['status'], 'PASS')
            # The existing adapter cannot certify physical observer cost; never waive that gate.
            self.assertEqual(reports['normal']['measurement_status'], 'INCONCLUSIVE')
            for path, contents in originals.items():
                self.assertEqual(path.read_bytes(), contents, f'input changed: {path}')

    def test_completed_and_interrupted_reports_reuse_hashed_checker_evidence(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            matrix = self.fixture(root, ('normal',))
            output = root / 'out'
            self.run_matrix(matrix, output)
            self.registration(output, 'normal')
            cache = output / '.seqchecks/normal'
            self.assertTrue(cache.is_dir())
            saved = {p: (p.stat().st_ino, p.stat().st_mtime_ns, p.read_bytes())
                     for p in cache.iterdir() if p.is_file()}
            self.run_matrix(matrix, output)
            for path, values in saved.items():
                self.assertEqual((path.stat().st_ino, path.stat().st_mtime_ns, path.read_bytes()), values)
            (output / 'normal/.complete.json').unlink()
            self.run_matrix(matrix, output)
            for path, values in saved.items():
                self.assertEqual((path.stat().st_ino, path.stat().st_mtime_ns, path.read_bytes()), values)
            self.registration(output, 'normal')


    def test_changed_capture_and_corrupt_cache_are_recomputed(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            matrix = self.fixture(root, ('normal',))
            output = root / 'out'
            self.run_matrix(matrix, output)
            result = output / '.seqchecks/normal/0-1.json'
            old_inode = result.stat().st_ino
            result.write_bytes(result.read_bytes() + b'\n')
            (output / 'normal/.complete.json').unlink()
            self.run_matrix(matrix, output)
            self.assertNotEqual(result.stat().st_ino, old_inode)
            self.assertEqual(self.registration(output, 'normal')[1]['status'], 'PASS')
            old_inode = result.stat().st_ino
            swap_probe_identities(root / 'normal/receiver.pcapng')
            self.run_matrix(matrix, output)
            self.assertNotEqual(result.stat().st_ino, old_inode)
            self.assertEqual(self.registration(output, 'normal')[1]['status'], 'FAIL')
            report = json.loads((output / 'normal/report.json').read_text())
            self.assertEqual(report['rows'][0]['gates']['order']['status'], 'FAIL')

    def test_missing_registered_result_is_generated_without_changing_sources(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            matrix = self.fixture(root, ('normal',))
            path = root / 'normal/source/manifest.json'
            manifest = json.loads(path.read_text())
            manifest['verification']['seqchecks'] = [dict(path='pending.json', sha256='0'*64,
                ingress_port=0, egress_port=1, ingress_path='../before.pcapng', receiver_path='../receiver.pcapng')]
            path.write_text(json.dumps(manifest))
            original = path.read_bytes()
            output = root / 'out'
            self.run_matrix(matrix, output)
            self.assertEqual(self.registration(output, 'normal')[1]['status'], 'PASS')
            self.assertEqual(path.read_bytes(), original)
            self.assertFalse((path.parent / 'pending.json').exists())

    def test_valid_supplied_result_needs_no_checker_execution(self):
        from tools.g2.matrix import report_one, SEQCHECK
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.fixture(root, ('normal',))
            pair = root / 'normal'
            result = subprocess.run([shutil.which('seqcheck') or str(SEQCHECK), '--ingress', str(pair/'before.pcapng'),
                                     '--receiver', str(pair/'receiver.pcapng')], capture_output=True)
            self.assertEqual(result.returncode, 0)
            evidence = pair / 'source/registered.json'
            evidence.write_bytes(result.stdout)
            path = pair / 'source/manifest.json'
            manifest = json.loads(path.read_text())
            manifest['verification']['seqchecks'] = [dict(path=evidence.name,
                sha256=hashlib.sha256(result.stdout).hexdigest(), ingress_port=0, egress_port=1,
                ingress_path='../before.pcapng', receiver_path='../receiver.pcapng')]
            path.write_text(json.dumps(manifest))
            output = root / 'out'
            output.mkdir()
            with patch('tools.g2.matrix.subprocess.run', side_effect=AssertionError('checker reran')):
                self.assertEqual(report_one(pair/'pair.json', output/'normal', 'normal', 'software-only', 'pcapng'), 2)
            self.assertEqual(self.registration(output, 'normal')[1]['status'], 'PASS')

    def test_checker_start_failure_is_inconclusive_and_retryable(self):
        from tools.g2.matrix import report_one, archive
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.fixture(root, ('normal',))
            output = root / 'out'
            output.mkdir()
            target = output / 'normal'
            with redirect_stderr(io.StringIO()), patch('tools.g2.matrix.subprocess.run', side_effect=FileNotFoundError('checker unavailable')):
                self.assertEqual(report_one(root/'normal/pair.json', target, 'normal', 'software-only', 'pcapng'), 2)
            report = json.loads((target / 'report.json').read_text())
            self.assertEqual(report['measurement_status'], 'INCONCLUSIVE')
            self.assertIn('SEQCHECK_PROCESS', report['reason_codes'])
            self.assertFalse((target / '.complete.json').exists())
            archive(output, target)
            self.assertEqual(report_one(root/'normal/pair.json', target, 'normal', 'software-only', 'pcapng'), 2)
            self.assertTrue((target / '.complete.json').is_file())
            self.assertEqual(self.registration(output, 'normal')[1]['status'], 'PASS')


    def test_unhashed_invalid_cache_cannot_hide_changed_receiver(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            matrix = self.fixture(root, ('normal',))
            output = root / 'out'
            self.run_matrix(matrix, output)
            result = output / '.seqchecks/normal/0-1.json'
            doc = json.loads(result.read_text())
            doc.update(status='INVALID', complete=False)
            doc['inputs']['receiver'].update(sha256=None, error='read_error',
                health=dict(complete=False, capture_drops=None))
            result.write_text(json.dumps(doc))
            receipt = result.with_name('0-1.registration.json')
            registration = json.loads(receipt.read_text())
            registration['sha256'] = hashlib.sha256(result.read_bytes()).hexdigest()
            receipt.write_text(json.dumps(registration))
            swap_probe_identities(root / 'normal/receiver.pcapng')
            self.run_matrix(matrix, output)
            checked = self.registration(output, 'normal')[1]
            self.assertEqual(checked['status'], 'FAIL')
            self.assertEqual(checked['totals']['ordinary_reorder'], 1)

    def test_modified_derived_result_is_not_sealed(self):
        from tools.g2.matrix import report_one, build_bundle_report
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.fixture(root, ('normal',))
            output = root / 'out'
            output.mkdir()
            target = output / 'normal'
            def corrupt_after_report(*args, **kwargs):
                result = build_bundle_report(*args, **kwargs)
                evidence = target / 'capture/bundle/seqcheck-0-1.json'
                evidence.write_bytes(evidence.read_bytes() + b'\n')
                return result
            with patch('tools.g2.matrix.build_bundle_report', side_effect=corrupt_after_report):
                self.assertEqual(report_one(root/'normal/pair.json', target, 'normal', 'software-only', 'pcapng'), 2)
            report = json.loads((target/'report.json').read_text())
            self.assertIn('SEQCHECK_SOURCE_CHANGED', report['reason_codes'])
            self.assertFalse((target/'.complete.json').exists())


    def test_fresh_unbound_checker_result_is_not_cached_or_sealed(self):
        from tools.g2.matrix import report_one, SEQCHECK
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.fixture(root, ('normal',))
            pair = root / 'normal'
            process = subprocess.run([shutil.which('seqcheck') or str(SEQCHECK), '--ingress',
                str(pair/'before.pcapng'), '--receiver', str(pair/'receiver.pcapng')], capture_output=True)
            self.assertEqual(process.returncode, 0)
            doc = json.loads(process.stdout)
            doc.update(status='INVALID', complete=False)
            doc['inputs']['receiver'].update(sha256=None, error='read_error',
                health=dict(complete=False, capture_drops=None))
            output = root / 'out'
            output.mkdir()
            target = output / 'normal'
            def unreadable_capture(command, **kwargs):
                kwargs['stdout'].write(json.dumps(doc).encode())
                return subprocess.CompletedProcess(command, 2, stderr=b'')
            with redirect_stderr(io.StringIO()), patch('tools.g2.matrix.subprocess.run', side_effect=unreadable_capture):
                self.assertEqual(report_one(pair/'pair.json', target, 'normal', 'software-only', 'pcapng'), 2)
            report = json.loads((target/'report.json').read_text())
            self.assertEqual(report['measurement_status'], 'INCONCLUSIVE')
            self.assertIn('SEQCHECK_PROCESS', report['reason_codes'])
            self.assertFalse((target/'.complete.json').exists())
            self.assertFalse((output/'.seqchecks/normal/0-1.registration.json').exists())


if __name__ == '__main__':
    unittest.main()
