# SPDX-License-Identifier: Apache-2.0
"""Matrix CLI preserves the existing reporter's sealed-run verdicts."""
import cProfile
import hashlib
import json
import os
import signal
import time
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from tools.g2.tests.test_bundle import fixture
from tools.g2.tests.test_pcapng import setup as capture_pair, pcapng


class MatrixTests(unittest.TestCase):
    def test_invalid_worker_counts_are_rejected_before_output(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            matrix = root / 'matrix.json'
            controller = ('import os; os.sched_getaffinity=lambda _: {0, 1}; '
                          'from tools.g2.matrix import main; import sys; sys.exit(main())')
            for workers in (0, -1, True, 1.0, '2', 3):
                matrix.write_text(json.dumps(dict(schema='hng-m6-matrix-v1', scope='software-only',
                    plan={}, workers=workers, runs=[dict(id='one', manifest='missing.json')])))
                result = subprocess.run([sys.executable, '-c', controller, str(matrix),
                                         '--out', str(root / 'out')], capture_output=True, text=True)
                with self.subTest(workers=workers):
                    self.assertEqual(result.returncode, 2, result.stderr)
                    self.assertFalse((root / 'out').exists())

    def test_trace_report_loads_and_validates_each_profile_once(self):
        from tools.g2.matrix import completed_run, report_one
        from tools.g2.reference import Trace, load_config
        profile = dict(end_ns=10**9, loss_seed=0, outage_action='drop', terminal='error',
                       terminal_horizon_ns=None, rows=[dict(at_ns=0, t_ns=10, loss='0', outage=False)])
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fixture(root, trace=profile)
            profiler = cProfile.Profile()
            code = profiler.runcall(report_one, root/'manifest.json', root/'out', 'demo', 'software-only')
            self.assertEqual(code, 0)
            verdict, sealed_code = completed_run(root/'out', root/'manifest.json', 'demo', 'software-only', 'software')
            self.assertEqual((verdict['status'], sealed_code), ('PASS', 0))
            calls = {entry.code: entry.callcount for entry in profiler.getstats()}
            self.assertEqual(calls[load_config.__code__], 1, 'trace JSON must not be loaded twice per report')
            self.assertEqual(calls[Trace.__init__.__code__], 1, 'oracle and custody must share the validated trace')
            # A later report must validate changed bytes, even when their hashes are registered.
            trace = json.loads((root/'trace.json').read_text())
            trace['profile']['rows'][0]['loss'] = '1.1'
            raw = json.dumps(trace).encode()
            (root/'trace.json').write_bytes(raw)
            digest = hashlib.sha256(raw).hexdigest()
            config = json.loads((root/'engine.json').read_text())
            config['lines'][0]['trace']['sha256'] = digest
            raw = json.dumps(config).encode()
            (root/'engine.json').write_bytes(raw)
            manifest = json.loads((root/'manifest.json').read_text())
            manifest['config']['sha256'] = hashlib.sha256(raw).hexdigest()
            manifest['traces'][0]['sha256'] = digest
            (root/'manifest.json').write_text(json.dumps(manifest))
            self.assertEqual(report_one(root/'manifest.json', root/'invalid', 'demo', 'software-only'), 2)
            self.assertEqual(json.loads((root/'invalid/verdict.json').read_text())['status'], 'INVALID')

    def test_pcapng_mode_rejects_foreign_run_or_scope_before_conversion(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary); capture_pair(root,run='registered')
            for run,scope in [('other','software-only'),('registered','physical')]:
                matrix=root/'matrix.json'
                matrix.write_text(json.dumps(dict(schema='hng-m6-matrix-v1',mode='pcapng',scope=scope,plan={},
                    runs=[dict(id=run,manifest='pair.json')])))
                output=root/run
                result=subprocess.run([sys.executable,'-m','tools.g2.matrix',str(matrix),'--out',str(output)],
                                      capture_output=True,text=True)
                self.assertEqual(result.returncode,2,result.stderr)
                self.assertEqual(json.loads((output/'summary.json').read_text())['measurement_status'],'INCONCLUSIVE')
                self.assertFalse((output/run/'capture').exists())

    def test_explicit_software_mode_and_unknown_mode_before_output(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary); source=root/'source'; source.mkdir()
            fixture(source,run='software')
            matrix=root/'matrix.json'
            value=dict(schema='hng-m6-matrix-v1',scope='software-only',mode='unknown',plan={},
                       runs=[dict(id='software',manifest='source/manifest.json')])
            matrix.write_text(json.dumps(value))
            result=subprocess.run([sys.executable,'-m','tools.g2.matrix',str(matrix),'--out',str(root/'out')],
                                  capture_output=True,text=True)
            self.assertEqual(result.returncode,2,result.stderr)
            self.assertFalse((root/'out').exists())
            value['mode']='software'; matrix.write_text(json.dumps(value))
            result=subprocess.run([sys.executable,'-m','tools.g2.matrix',str(matrix),'--out',str(root/'out')],
                                  capture_output=True,text=True)
            self.assertEqual(result.returncode,0,result.stderr)
            self.assertEqual(json.loads((root/'out/summary.json').read_text())['measurement_status'],'PASS')

    def test_pcapng_mode_reports_three_cells_and_retains_capture_loss(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary); runs=[]
            for run,family in [('main-r0','main'),('dropped-r0','boundary'),('last-r0','trace')]:
                capture=root/run; capture.mkdir()
                expected=capture_pair(capture,run=run)
                path=capture/'source/manifest.json'
                manifest=json.loads(path.read_text()); manifest['condition_id']=run
                for cell in manifest['required_cells']: cell['condition_id']=run
                path.write_text(json.dumps(manifest))
                if run=='dropped-r0':
                    pcapng(capture/'receiver.pcapng',[row['release_ns']+3 for row in expected],drop=1)
                runs.append(dict(id=run,family=family,manifest=f'{run}/pair.json'))
            matrix=root/'matrix.json'
            matrix.write_text(json.dumps(dict(schema='hng-m6-matrix-v1',scope='software-only',
                mode='pcapng',plan={},runs=runs)))
            result=subprocess.run([sys.executable,'-m','tools.g2.matrix',str(matrix),'--out',str(root/'out')],
                                  capture_output=True,text=True)
            self.assertEqual(result.returncode,2,result.stderr)
            self.assertTrue((root/'out/summary.json').is_file(),result.stderr)
            summary=json.loads((root/'out/summary.json').read_text())
            self.assertEqual([run['measurement_status'] for run in summary['runs']],['INCONCLUSIVE']*3)
            self.assertEqual(summary['measurement_status'],'INCONCLUSIVE')
            self.assertEqual([run['condition_id'] for run in summary['runs']],['main-r0','dropped-r0','last-r0'])
            self.assertFalse(summary['g2_qualified'])
            for run in summary['runs']:
                report=json.loads((root/'out'/run['report']).read_text())
                self.assertEqual(run['measurement_status'],report['measurement_status'])
                self.assertEqual(run['rows'][0]['condition_id'],run['id'])
                self.assertTrue((root/'out'/run['id']/'results.csv').is_file())
                self.assertTrue((root/'out'/run['id']/'capture/conversion.json').is_file())
                conversion=json.loads((root/'out'/run['id']/'capture/conversion.json').read_text())
                self.assertEqual(conversion['captures']['receiver']['complete'], run['id']!='dropped-r0')
                self.assertEqual(report['observer_cost']['status'], 'INCONCLUSIVE')

    def test_matrix_metadata_must_match_registered_run_and_scope(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary)
            capture=root/'capture'; capture.mkdir()
            fixture(capture,run='registered')
            for run,scope in [('different','software-only'),('registered','physical')]:
                matrix=root/'matrix.json'
                matrix.write_text(json.dumps(dict(schema='hng-m6-matrix-v1',scope=scope,plan={},
                    runs=[dict(id=run,manifest='capture/manifest.json')])))
                result=subprocess.run([sys.executable,'-m','tools.g2.matrix',str(matrix),'--out',str(root/run)],
                                      capture_output=True,text=True)
                with self.subTest(run=run,scope=scope):
                    self.assertEqual(result.returncode,2,result.stderr)
                    summary=json.loads((root/run/'summary.json').read_text())
                    self.assertEqual(summary['measurement_status'],'INCONCLUSIVE')
                    self.assertFalse(summary['g2_qualified'])

    def test_missing_capture_is_retained_and_does_not_skip_the_next_run(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary)
            capture=root/'capture'; capture.mkdir()
            fixture(capture,run='present')
            matrix=root/'matrix.json'
            matrix.write_text(json.dumps(dict(schema='hng-m6-matrix-v1',scope='software-only',plan={},
                runs=[dict(id='missing',manifest='absent.json'),dict(id='present',manifest='capture/manifest.json')])) )
            result=subprocess.run([sys.executable,'-m','tools.g2.matrix',str(matrix),'--out',str(root/'out')],
                                  capture_output=True,text=True)
            self.assertEqual(result.returncode,2,result.stderr)
            summary=json.loads((root/'out/summary.json').read_text())
            self.assertEqual([run['measurement_status'] for run in summary['runs']],['INVALID','PASS'])
            self.assertEqual(summary['measurement_status'],'INCONCLUSIVE')
            self.assertFalse(summary['g2_qualified'])

    def test_invalid_inventory_is_rejected_before_output_and_existing_results_are_preserved(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary)
            for runs in ([],[dict(id='../escape',manifest='a.json')],
                         [dict(id='summary.json',manifest='a.json')],
                         [dict(id='a.stderr.log',manifest='a.json'),dict(id='a',manifest='b.json')],
                         [dict(id='same',manifest='a.json'),dict(id='same',manifest='b.json')],
                         [dict(id='one',manifest='a.json'),dict(id='two',manifest='./a.json')]):
                matrix=root/'matrix.json'
                matrix.write_text(json.dumps(dict(schema='hng-m6-matrix-v1',scope='software-only',plan={},runs=runs)))
                result=subprocess.run([sys.executable,'-m','tools.g2.matrix',str(matrix),'--out',str(root/'out')],
                                      capture_output=True,text=True)
                with self.subTest(runs=runs):
                    self.assertEqual(result.returncode,2,result.stderr)
                    self.assertFalse((root/'out').exists())
            matrix.write_text(json.dumps(dict(schema='hng-m6-matrix-v1',scope='software-only',plan={},
                runs=[dict(id='one',manifest='a.json')])))
            output=root/'out'; output.mkdir()
            (output/'sentinel').write_text('previous evidence')
            result=subprocess.run([sys.executable,'-m','tools.g2.matrix',str(matrix),'--out',str(output)],
                                  capture_output=True,text=True)
            self.assertEqual(result.returncode,2,result.stderr)
            self.assertEqual((output/'sentinel').read_text(),'previous evidence')
            self.assertEqual(sorted(p.name for p in output.iterdir()),['sentinel'])

    def test_all_registered_runs_keep_their_pass_fail_and_inconclusive_verdicts(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary)
            def fault(rows,registration):
                for row in rows['receiver']:
                    row['receive_ns']+=200_000
                    row['timestamp']['raw']+=200_000
            def incomplete(rows,registration):
                rows['receiver'][0]['timestamp']['mapping_id']='missing'
            runs=[]
            for run,mutate in [('pass',None),('fail',fault),('inconclusive',incomplete)]:
                capture=root/run; capture.mkdir()
                fixture(capture,run=run,mutate=mutate)
                runs.append(dict(id=run,manifest=f'{run}/manifest.json'))
            matrix=root/'matrix.json'
            matrix.write_text(json.dumps(dict(schema='hng-m6-matrix-v1',scope='software-only',plan={},runs=runs)))
            result=subprocess.run([sys.executable,'-m','tools.g2.matrix',str(matrix),'--out',str(root/'out')],
                                  capture_output=True,text=True)
            self.assertEqual(result.returncode,1,result.stderr)
            summary=json.loads((root/'out/summary.json').read_text())
            self.assertEqual([run['measurement_status'] for run in summary['runs']],['PASS','FAIL','INCONCLUSIVE'])
            self.assertEqual(summary['measurement_status'],'FAIL')
            self.assertFalse(summary['g2_qualified'])

    def test_registered_run_uses_existing_reporter_without_qualifying_software(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary)
            capture=root/'capture'; capture.mkdir()
            fixture(capture,run='main-r0')
            matrix=root/'matrix.json'
            matrix.write_text(json.dumps(dict(schema='hng-m6-matrix-v1',scope='software-only',
                plan={},runs=[dict(id='main-r0',manifest='capture/manifest.json')])) )
            result=subprocess.run([sys.executable,'-m','tools.g2.matrix',str(matrix),'--out',str(root/'out')],
                                  capture_output=True,text=True)
            self.assertEqual(result.returncode,0,result.stderr)
            summary=json.loads((root/'out/summary.json').read_text())
            report=json.loads((root/'out/main-r0/report.json').read_text())
            self.assertEqual(summary['measurement_status'],'PASS')
            self.assertFalse(summary['g2_qualified'])
            self.assertEqual(summary['runs'][0]['measurement_status'],report['measurement_status'])
            self.assertEqual(report['rows'][0]['timing']['main']['n'],400)
            self.assertTrue((root/'out/main-r0/results.csv').is_file())


class MatrixResumeTests(unittest.TestCase):
    def matrix_fixture(self, root):
        def late(rows, registration):
            for row in rows['receiver']:
                row['receive_ns'] += 200_000
                row['timestamp']['raw'] += 200_000

        def unavailable(rows, registration):
            rows['receiver'][0]['timestamp']['mapping_id'] = 'missing'

        runs = []
        for run, change in [('first', None), ('second', late), ('third', unavailable)]:
            capture = root / run
            capture.mkdir()
            fixture(capture, run=run, mutate=change)
            runs.append(dict(id=run, manifest=f'{run}/manifest.json'))
        matrix = root / 'matrix.json'
        matrix.write_text(json.dumps(dict(schema='hng-m6-matrix-v1', scope='software-only',
                                          plan={}, runs=runs)))
        return matrix

    def cli(self, matrix, output):
        return subprocess.run([sys.executable, '-m', 'tools.g2.matrix', str(matrix), '--out', str(output)],
                              capture_output=True, text=True, timeout=30)

    def saved_files(self, root):
        return {str(path.relative_to(root)): (path.stat().st_ino, path.stat().st_mtime_ns, path.read_bytes())
                for path in root.rglob('*') if path.is_file()}

    def test_two_workers_overlap_and_preserve_order_bytes_and_resume(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            matrix = self.matrix_fixture(root)
            baseline = root / 'baseline'
            clean = self.cli(matrix, baseline)
            self.assertEqual(clean.returncode, 1, clean.stderr)
            value = json.loads(matrix.read_text())
            value['workers'] = 1
            matrix.write_text(json.dumps(value))
            serial = self.cli(matrix, root / 'serial')
            self.assertEqual(serial.returncode, 1, serial.stderr)
            value['workers'] = 2
            matrix.write_text(json.dumps(value))
            worker = (
                'from pathlib import Path; import sys,time; from tools.g2.matrix import report_one; '
                f'root=Path({str(root)!r}); (root/("ready-"+sys.argv[3])).touch(); '
                'deadline=time.monotonic()+5\n'
                'while sys.argv[3] in ("first", "second") and not all('
                '(root/("ready-"+run)).exists() for run in ("first", "second")):\n'
                ' if time.monotonic()>deadline: raise RuntimeError("workers did not overlap")\n'
                ' time.sleep(0.01)\n'
                'while sys.argv[3]=="first" and not (root/"done-second").exists():\n'
                ' if time.monotonic()>deadline: raise RuntimeError("second worker did not finish")\n'
                ' time.sleep(0.01)\n'
                'code=report_one(*sys.argv[1:]); (root/("done-"+sys.argv[3])).touch(); sys.exit(code)'
            )
            controller = ('import os; os.sched_getaffinity=lambda _: {0, 1}; '
                          'import tools.g2.matrix as m; import sys; '
                          f'm.WORKER={worker!r}; sys.exit(m.main())')
            output = root / 'parallel'
            parallel = subprocess.run([sys.executable, '-c', controller, str(matrix), '--out', str(output)],
                                      capture_output=True, text=True, timeout=30)
            self.assertEqual(parallel.returncode, 1, parallel.stderr)
            expected = (baseline / 'summary.json').read_bytes()
            self.assertEqual((output / 'summary.json').read_bytes(), expected)
            self.assertEqual((root / 'serial/summary.json').read_bytes(), expected)
            for run in ('first', 'second', 'third'):
                for name in ('report.json', 'results.csv', 'summary.txt', 'verdict.json'):
                    self.assertEqual((output / run / name).read_bytes(),
                                     (baseline / run / name).read_bytes(), (run, name))
                    self.assertEqual((root / 'serial' / run / name).read_bytes(),
                                     (baseline / run / name).read_bytes(), (run, name))
            saved = {run: self.saved_files(output / run) for run in ('first', 'second', 'third')}
            resumed = subprocess.run([sys.executable, '-c', controller, str(matrix), '--out', str(output)],
                                     capture_output=True, text=True, timeout=30)
            self.assertEqual(resumed.returncode, 1, resumed.stderr)
            for run, snapshot in saved.items():
                self.assertEqual(self.saved_files(output / run), snapshot, run)

    def test_resume_hashes_include_seqcheck_json_and_both_capture_inputs(self):
        from tools.g2.matrix import completed_run, file_hash, report_one, source_hashes
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / 'source'
            source.mkdir()
            fixture(source)
            output = root / 'out'
            self.assertEqual(report_one(source / 'manifest.json', output, 'demo', 'software-only'), 0)
            manifest = json.loads((source / 'manifest.json').read_text())
            dependencies = ('seqcheck.json', 'ingress.pcapng', 'receiver.pcapng')
            for name in dependencies:
                (source / name).write_bytes(b'registered evidence')
            manifest['verification'] = dict(mode='sampled', seqchecks=[dict(
                path='seqcheck.json', sha256=file_hash(source / 'seqcheck.json'),
                ingress_port=0, egress_port=1, ingress_path='ingress.pcapng',
                receiver_path='receiver.pcapng')])
            (source / 'manifest.json').write_text(json.dumps(manifest))
            seal = json.loads((output / '.complete.json').read_text())
            seal['sources'][str(source / 'manifest.json')] = file_hash(source / 'manifest.json')
            for name in dependencies:
                seal['sources'][str(source / name)] = file_hash(source / name)
            (output / '.complete.json').write_text(json.dumps(seal))
            self.assertEqual(source_hashes(source / 'manifest.json', 'software'), seal['sources'])
            self.assertIsNotNone(completed_run(output, source / 'manifest.json',
                                             'demo', 'software-only', 'software'))
            for name in dependencies:
                path = source / name
                path.write_bytes(b'changed evidence')
                with self.subTest(source=name):
                    self.assertIsNone(completed_run(output, source / 'manifest.json',
                                                   'demo', 'software-only', 'software'))
                path.write_bytes(b'registered evidence')

    def test_killed_matrix_resumes_completed_and_partial_runs_like_uninterrupted_run(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            matrix = self.matrix_fixture(root)
            baseline, output = root / 'baseline', root / 'resumed'
            clean = self.cli(matrix, baseline)
            self.assertEqual(clean.returncode, 1, clean.stderr)
            self.assertEqual([r['measurement_status'] for r in
                              json.loads((baseline / 'summary.json').read_text())['runs']],
                             ['PASS', 'FAIL', 'INCONCLUSIVE'])
            ready = root / 'worker-ready'
            # Block only after the real first reporter has completed. The second
            # cell leaves partial output, as a killed reporter/converter would.
            worker = (
                'from pathlib import Path; import signal,sys; '
                'from tools.g2.matrix import report_one; '
                f'ready=Path({str(ready)!r}); '
                '\nif sys.argv[3]=="second":\n'
                ' path=Path(sys.argv[2]); path.mkdir(); '
                '(path/"partial.bin").write_bytes(b"interrupted evidence"); '
                'ready.touch(); signal.pause()\n'
                'sys.exit(report_one(*sys.argv[1:]))'
            )
            controller = ('import tools.g2.matrix as m; import sys; '
                          f'm.WORKER={worker!r}; sys.exit(m.main())')
            process = subprocess.Popen([sys.executable, '-c', controller, str(matrix), '--out', str(output)],
                                       stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
                                       start_new_session=True)
            try:
                deadline = time.monotonic() + 20
                while not ready.exists():
                    self.assertIsNone(process.poll(), 'matrix exited before the interruption checkpoint')
                    self.assertLess(time.monotonic(), deadline, 'reporter did not reach checkpoint')
                    time.sleep(0.01)
                saved = self.saved_files(output / 'first')
                self.assertTrue(saved)
                os.killpg(process.pid, signal.SIGKILL)
                process.communicate(timeout=5)
                self.assertEqual(process.returncode, -signal.SIGKILL)
            finally:
                if process.poll() is None:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.communicate(timeout=5)
                if process.stderr is not None:
                    process.stderr.close()
            resumed = self.cli(matrix, output)
            self.assertEqual(resumed.returncode, 1, resumed.stderr)
            self.assertEqual(self.saved_files(output / 'first'), saved, 'completed run was rewritten')
            self.assertEqual(json.loads((output / 'summary.json').read_text()),
                             json.loads((baseline / 'summary.json').read_text()))
            for run in ('first', 'second', 'third'):
                for name in ('report.json', 'results.csv', 'summary.txt', 'verdict.json'):
                    self.assertEqual((output / run / name).read_bytes(),
                                     (baseline / run / name).read_bytes(), (run, name))
            partials = list(root.rglob('partial.bin'))
            self.assertEqual(len(partials), 1, 'partial evidence was deleted or duplicated')
            self.assertNotEqual(partials[0].parent, output / 'second')
            self.assertEqual(partials[0].read_bytes(), b'interrupted evidence')

    def test_changed_report_is_archived_and_only_affected_run_is_recomputed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            matrix = self.matrix_fixture(root)
            output = root / 'out'
            first = self.cli(matrix, output)
            self.assertEqual(first.returncode, 1, first.stderr)
            original = (output / 'first' / 'report.json').read_bytes()
            saved = {run: self.saved_files(output / run) for run in ('second', 'third')}
            corrupted = json.loads(original)
            corrupted['rows'][0]['counts']['received'] = 999
            corrupt_bytes = json.dumps(corrupted).encode()
            (output / 'first' / 'report.json').write_bytes(corrupt_bytes)
            resumed = self.cli(matrix, output)
            self.assertEqual(resumed.returncode, 1, resumed.stderr)
            self.assertEqual((output / 'first' / 'report.json').read_bytes(), original)
            for run, snapshot in saved.items():
                self.assertEqual(self.saved_files(output / run), snapshot, run)
            archived = [p for p in root.rglob('report.json') if p.read_bytes() == corrupt_bytes]
            self.assertEqual(len(archived), 1, 'changed evidence was not preserved')
            self.assertNotEqual(archived[0].parent, output / 'first')

    def test_changed_sealed_capture_does_not_reuse_an_old_pass(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            matrix = self.matrix_fixture(root)
            output = root / 'out'
            first = self.cli(matrix, output)
            self.assertEqual(first.returncode, 1, first.stderr)
            saved = {run: self.saved_files(output / run) for run in ('second', 'third')}
            receiver = root / 'first' / 'receiver.jsonl'
            with receiver.open('ab') as stream:
                stream.write(b'changed after sealing\n')
            resumed = self.cli(matrix, output)
            self.assertEqual(resumed.returncode, 1, resumed.stderr)
            summary = json.loads((output / 'summary.json').read_text())
            self.assertEqual([run['id'] for run in summary['runs']], ['first', 'second', 'third'])
            self.assertEqual(summary['runs'][0]['status'], 'INVALID')
            self.assertFalse(summary['runs'][0]['g2_qualified'])
            for run, snapshot in saved.items():
                self.assertEqual(self.saved_files(output / run), snapshot, run)
            archived = [json.loads(p.read_text()) for p in root.rglob('verdict.json')
                        if p.parent != output / 'first' and p.parent not in
                        (output / 'second', output / 'third')]
            self.assertTrue(any(v['run']=='first' and v['status']=='PASS' for v in archived),
                            'old PASS evidence was deleted')

    def test_pcapng_resume_keeps_conversion_and_rechecks_changed_capture(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / 'source'
            source.mkdir()
            expected = capture_pair(source, run='pcap')
            matrix = root / 'matrix.json'
            matrix.write_text(json.dumps(dict(schema='hng-m6-matrix-v1', mode='pcapng',
                scope='software-only', plan={}, runs=[dict(id='pcap', manifest='source/pair.json')])))
            output = root / 'out'
            initial = self.cli(matrix, output)
            self.assertEqual(initial.returncode, 2, initial.stderr)
            saved = self.saved_files(output / 'pcap')
            resumed = self.cli(matrix, output)
            self.assertEqual(resumed.returncode, 2, resumed.stderr)
            self.assertEqual(self.saved_files(output / 'pcap'), saved)
            pcapng(source / 'receiver.pcapng', [r['release_ns']+3 for r in expected], drop=1)
            changed = self.cli(matrix, output)
            self.assertEqual(changed.returncode, 2, changed.stderr)
            report = json.loads((output / 'pcap/report.json').read_text())
            self.assertEqual(report['measurement_status'], 'INCONCLUSIVE')
            conversion = json.loads((output / 'pcap/capture/conversion.json').read_text())
            self.assertIn('PCAPNG_CAPTURE_DROP', conversion['captures']['receiver']['reason_codes'])
            self.assertFalse(report['g2_qualified'])
            conversions = list(root.rglob('conversion.json'))
            self.assertEqual(len(conversions), 2, 'prior converted evidence was not preserved')

    def test_summary_temporary_filename_does_not_collide_with_a_valid_run_id(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / 'source'
            source.mkdir()
            fixture(source, run='summary.json.tmp')
            matrix = root / 'matrix.json'
            matrix.write_text(json.dumps(dict(schema='hng-m6-matrix-v1', scope='software-only',
                plan={}, runs=[dict(id='summary.json.tmp', manifest='source/manifest.json')])))
            result = self.cli(matrix, root / 'out')
            self.assertEqual(result.returncode, 0, result.stderr)
            summary = json.loads((root / 'out/summary.json').read_text())
            self.assertEqual(summary['runs'][0]['status'], 'PASS')

    def test_resume_hashes_include_the_trace_actually_loaded_from_config(self):
        from tools.g2.matrix import source_hashes
        from tools.g2.validate import preflight
        import hashlib
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fixture(root)
            trace = (Path(__file__).resolve().parents[3] / 'dpds-core/tests/fixtures/trace.json').read_bytes()
            for name in ('actual-trace.json', 'registered-trace.json'):
                (root / name).write_bytes(trace)
            digest = hashlib.sha256(trace).hexdigest()
            config = json.loads((root / 'engine.json').read_text())
            config['lines'][0]['trace'] = dict(path='actual-trace.json', sha256=digest)
            config['lines'][0]['model'].update(t_ns=0, delta_ns=0, p='0')
            raw = json.dumps(config).encode()
            (root / 'engine.json').write_bytes(raw)
            manifest = json.loads((root / 'manifest.json').read_text())
            manifest['config']['sha256'] = hashlib.sha256(raw).hexdigest()
            manifest['traces'] = [dict(line='cable', path='registered-trace.json', sha256=digest,
                                      provenance='synthetic fixture')]
            (root / 'manifest.json').write_text(json.dumps(manifest))
            preflight(root / 'manifest.json')
            before = source_hashes(root / 'manifest.json', 'software')
            with (root / 'actual-trace.json').open('ab') as stream:
                stream.write(b' ')
            self.assertNotEqual(source_hashes(root / 'manifest.json', 'software'), before)

    def test_missing_converted_source_symlink_is_archived_and_rebuilt(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            capture_pair(root, run='pcap')
            matrix = root / 'matrix.json'
            matrix.write_text(json.dumps(dict(schema='hng-m6-matrix-v1', mode='pcapng',
                scope='software-only', plan={}, runs=[dict(id='pcap', manifest='pair.json')])))
            output = root / 'out'
            first = self.cli(matrix, output)
            self.assertEqual(first.returncode, 2, first.stderr)
            source = output / 'pcap/capture/bundle/source'
            source.unlink()
            resumed = self.cli(matrix, output)
            self.assertEqual(resumed.returncode, 2, resumed.stderr)
            self.assertTrue(source.is_dir(), 'resume reused an unusable converted bundle')
            self.assertEqual(len(list(root.rglob('conversion.json'))), 2)

    def test_pcapng_manifest_symlink_uses_the_resolved_native_directory(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            capture_pair(root, run='pcap')
            (root / 'linked.json').symlink_to(root / 'source/manifest.json')
            pair = json.loads((root / 'pair.json').read_text())
            pair['manifest'] = 'linked.json'
            (root / 'pair.json').write_text(json.dumps(pair))
            matrix = root / 'matrix.json'
            matrix.write_text(json.dumps(dict(schema='hng-m6-matrix-v1', mode='pcapng',
                scope='software-only', plan={}, runs=[dict(id='pcap', manifest='pair.json')])))
            result = self.cli(matrix, root / 'out')
            self.assertEqual(result.returncode, 2, result.stderr)
