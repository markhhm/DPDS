# SPDX-License-Identifier: Apache-2.0
"""Matrix CLI preserves the existing reporter's sealed-run verdicts."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from tools.g2.tests.test_bundle import fixture
from tools.g2.tests.test_pcapng import setup as capture_pair, pcapng


class MatrixTests(unittest.TestCase):
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
            self.assertEqual([run['measurement_status'] for run in summary['runs']],['PASS','INCONCLUSIVE','PASS'])
            self.assertEqual(summary['measurement_status'],'INCONCLUSIVE')
            self.assertEqual([run['condition_id'] for run in summary['runs']],['main-r0','dropped-r0','last-r0'])
            self.assertFalse(summary['g2_qualified'])
            for run in summary['runs']:
                report=json.loads((root/'out'/run['report']).read_text())
                self.assertEqual(run['measurement_status'],report['measurement_status'])
                self.assertEqual(run['rows'][0]['condition_id'],run['id'])
                self.assertTrue((root/'out'/run['id']/'results.csv').is_file())
                self.assertTrue((root/'out'/run['id']/'capture/conversion.json').is_file())

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
