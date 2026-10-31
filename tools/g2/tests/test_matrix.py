# SPDX-License-Identifier: Apache-2.0
"""Matrix CLI preserves the existing reporter's sealed-run verdicts."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from tools.g2.tests.test_bundle import fixture


class MatrixTests(unittest.TestCase):
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
