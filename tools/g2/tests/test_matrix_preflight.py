# SPDX-License-Identifier: Apache-2.0
"""Registration-only preflight shares the reporting reader without touching captures."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


class MatrixPreflightTests(unittest.TestCase):
    def document(self):
        return dict(schema='hng-m6-matrix-v1',scope='software-only',plan={},
                    runs=[dict(id='future',family='main',manifest='future/pair.json')])

    def cli(self,path):
        return subprocess.run([sys.executable,'-m','tools.g2.matrix',str(path),'--preflight'],
                              capture_output=True,text=True)

    def test_cli_checks_future_paths_without_capture_or_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);path=root/'matrix.json';path.write_text(json.dumps(self.document()))
            before=set(root.iterdir());result=self.cli(path)
            self.assertEqual(result.returncode,0,result.stderr)
            report=json.loads(result.stdout)
            self.assertEqual(report['scope'],'registration-only');self.assertFalse(report['g2_qualified'])
            self.assertEqual(report['run_count'],1);self.assertEqual(report['path_count'],1)
            self.assertEqual(report['families'],dict(main=1))
            self.assertEqual(set(root.iterdir()),before)

    def test_reader_rejects_invalid_schema_ids_and_paths_before_output(self):
        mutations=[lambda d:d.update(schema='unknown'),
                   lambda d:d['runs'][0].update(id='../escape'),
                   lambda d:d['runs'].append(dict(id='other',manifest='future/../future/pair.json')),
                   lambda d:d['runs'][0].update(manifest=''),
                   lambda d:d['runs'][0].update(manifest='${CAPTURE_ROOT}/pair.json'),
                   lambda d:d['runs'][0].update(manifest='future/\x00/pair.json')]
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'matrix.json'
            for mutate in mutations:
                doc=self.document();mutate(doc);path.write_text(json.dumps(doc))
                with self.subTest(mutate=mutate):self.assertEqual(self.cli(path).returncode,2)

    def generator(self):
        return dict(profile='examples/m7-traffic/stl_probe.py',
                    sequence_policy='per-flow-dense', sequence_start=0, sequence_step=1,
                    seqcheck_memory_limit_bytes=4294967296,
                    seqcheck_profiles=dict(X=dict(flows=64, max_sequence_per_flow=1710568)))

    def test_dense_flow_budget_passes_without_opening_captures(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'matrix.json';doc=self.document()
            doc['plan']['generator']=self.generator();path.write_text(json.dumps(doc))
            result=self.cli(path)
            self.assertEqual(result.returncode,0,result.stderr)
            budget=json.loads(result.stdout)['generator_index_bytes']['X']
            self.assertGreaterEqual(budget,917914624)
            self.assertLessEqual(budget,1000000000)
            self.assertEqual(list(path.parent.iterdir()),[path])

    def test_global_sparse_or_oversized_generator_is_invalid_before_reporting(self):
        from tools.g2.matrix import run_matrix
        mutations=[('global',lambda g:g.update(sequence_policy='global')),
                   ('nonzero start',lambda g:g.update(sequence_start=1)),
                   ('global stride',lambda g:g.update(sequence_step=64)),
                   ('index memory',lambda g:g['seqcheck_profiles']['X'].update(max_sequence_per_flow=109476367)),
                   ('many flows',lambda g:g['seqcheck_profiles']['X'].update(flows=1000000)),
                   ('unknown range',lambda g:g['seqcheck_profiles']['X'].pop('max_sequence_per_flow')),
                   ('invalid bound',lambda g:g['seqcheck_profiles']['X'].update(flows=True)),
                   ('low memory limit',lambda g:g.update(seqcheck_memory_limit_bytes=1000)),
                   ('table growth',lambda g:g.update(seqcheck_memory_limit_bytes=8800000,
                       seqcheck_profiles=dict(probe=dict(flows=32,max_sequence_per_flow=32767))))]
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);path=root/'matrix.json'
            for name,mutate in mutations:
                doc=self.document();generator=self.generator();mutate(generator)
                doc['plan']['generator']=generator;path.write_text(json.dumps(doc))
                with self.subTest(name=name):
                    result=self.cli(path)
                    self.assertEqual(result.returncode,2,result.stdout+result.stderr)
                    self.assertIn('INVALID generator',result.stderr)
                    with self.assertRaises(ValueError):run_matrix(path,root/'out')
                    self.assertFalse((root/'out').exists())

    def test_registered_traffic_requires_its_generator_policy(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'matrix.json';doc=self.document()
            doc['plan']['traffic']={'X':{'flows':64}}
            path.write_text(json.dumps(doc));result=self.cli(path)
            self.assertEqual(result.returncode,2)
            self.assertIn('INVALID generator',result.stderr)

    def test_reporting_and_preflight_use_the_same_reader(self):
        from tools.g2 import matrix
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'matrix.json'
            with patch.object(matrix,'read_matrix',side_effect=ValueError('sentinel')) as reader:
                with self.assertRaisesRegex(ValueError,'sentinel'):matrix.run_matrix(path,Path(tmp)/'out')
                reader.assert_called_once_with(path)
            self.assertFalse((Path(tmp)/'out').exists())


if __name__=='__main__':unittest.main()
