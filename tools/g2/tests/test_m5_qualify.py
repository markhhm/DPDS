# SPDX-License-Identifier: Apache-2.0
"""Synthetic captures exercise the public M5 CLI and P1-0 export contract."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from tools.g2.tests.test_pcapng import setup, pcapng


def fixture(root, *, drift_ns=0, errors=(500,500), count=400):
    runs=[]
    for name,spacing,error in zip(('low','high'),(10000,1000),errors):
        folder=root/name;folder.mkdir()
        setup(folder,run=name)
        path=folder/'source/engine.json';config=json.loads(path.read_text())
        config['lines'][0]['model'].update(t_ns=0,delta_ns=0,p='0')
        path.write_text(json.dumps(config))
        path=folder/'source/manifest.json';manifest=json.loads(path.read_text())
        manifest['config']['sha256']=hashlib.sha256((folder/'source/engine.json').read_bytes()).hexdigest()
        manifest['verification']=dict(mode='sampled',seqchecks=[])
        manifest['timing_mode']='calibration'
        path.write_text(json.dumps(manifest))
        before=[100+i*spacing for i in range(count)]
        receiver=[time+error+(drift_ns*i//(count-1)) for i,time in enumerate(before)]
        pcapng(folder/'before.pcapng',before)
        pcapng(folder/'receiver.pcapng',receiver)
        runs.append(dict(id=name,manifest=f'{name}/pair.json'))
    path=root/'matrix.json'
    path.write_text(json.dumps(dict(schema='hng-m6-matrix-v1',scope='software-only',
        mode='pcapng',plan={},runs=runs)))
    return path


class M5QualifyTests(unittest.TestCase):
    def invoke(self,matrix,out,*args):
        return subprocess.run([sys.executable,'-m','tools.g2.m5_qualify',str(matrix),
            '--out',str(out),*map(str,args)],capture_output=True,text=True,timeout=45)

    def test_clean_zero_delay_cells_pass_without_claiming_physical_qualification(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);matrix=fixture(root);out=root/'qualification'
            sources={p:p.read_bytes() for p in root.rglob('*') if p.is_file()}
            process=self.invoke(matrix,out)
            self.assertEqual(process.returncode,0,process.stderr)
            report=json.loads((out/'qualification.json').read_text())
            self.assertEqual(report['schema'],'hng-m5-qualification-v1')
            self.assertEqual(report['status'],'PASS')
            self.assertEqual(report['scope'],'software-only')
            self.assertFalse(report['physical_qualified'])
            self.assertEqual(report['clock_uncertainty_ns'],500)
            self.assertEqual(len(report['rows']),2)
            for row in report['rows']:
                self.assertEqual(row['timing_samples'],400)
                self.assertEqual(row['offset_ns'],500)
                self.assertEqual(row['drift_span_ns'],0)
                self.assertEqual(row['missing'],0)
            self.assertTrue((out/'results.csv').is_file())
            self.assertIn('software-only',(out/'summary.txt').read_text())
            for path,data in sources.items():self.assertEqual(path.read_bytes(),data,path)

    def test_fifty_microsecond_drift_fails_even_though_g2_tolerance_is_larger(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);matrix=fixture(root,drift_ns=50000);out=root/'qualification'
            process=self.invoke(matrix,out)
            self.assertEqual(process.returncode,1,process.stderr)
            report=json.loads((out/'qualification.json').read_text())
            self.assertEqual(report['status'],'FAIL')
            self.assertGreaterEqual(report['clock_uncertainty_ns'],50000)
            self.assertTrue(all(row['drift_span_ns']>=49999 for row in report['rows']))
            self.assertFalse(report['physical_qualified'])


if __name__=='__main__':unittest.main()
