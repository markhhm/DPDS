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

    def test_real_bypass_loss_fails_even_when_too_few_timing_pairs_survive(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);matrix=fixture(root)
            pcapng(root/'high/receiver.pcapng',[100+i*1000+500 for i in range(360)])
            out=root/'qualification';process=self.invoke(matrix,out)
            self.assertEqual(process.returncode,1,process.stderr)
            report=json.loads((out/'qualification.json').read_text())
            self.assertEqual(report['status'],'FAIL')
            high=next(row for row in report['rows'] if row['run']=='high')
            self.assertEqual(high['missing'],40)
            self.assertIn('M5_BYPASS_ANOMALY',high['reason_codes'])

    def test_loads_on_different_observation_devices_do_not_qualify_either_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);matrix=fixture(root)
            path=root/'high/source/calibration.json';cal=json.loads(path.read_text())
            next(clock for clock in cal['clocks'] if clock['clock_id']=='capture')['device_token']='other-synthetic-observer'
            path.write_text(json.dumps(cal))
            out=root/'qualification';process=self.invoke(matrix,out)
            self.assertEqual(process.returncode,2,process.stderr)
            report=json.loads((out/'qualification.json').read_text())
            self.assertEqual(report['status'],'INCONCLUSIVE')
            self.assertTrue(all('M5_LOAD_COVERAGE' in row['reason_codes'] for row in report['rows']))

    def test_capture_loss_and_truncation_are_inconclusive(self):
        for kind in ('drop','truncated'):
            with self.subTest(kind=kind),tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp);matrix=fixture(root);path=root/'high/receiver.pcapng'
                if kind=='drop':pcapng(path,[600+i*1000 for i in range(400)],drop=1)
                else:path.write_bytes(path.read_bytes()[:-5])
                out=root/'qualification';process=self.invoke(matrix,out)
                self.assertEqual(process.returncode,2,process.stderr)
                self.assertEqual(json.loads((out/'qualification.json').read_text())['status'],'INCONCLUSIVE')

    def test_one_observed_load_is_not_enough_to_bound_load_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);matrix=fixture(root);doc=json.loads(matrix.read_text())
            doc['runs']=doc['runs'][:1];matrix.write_text(json.dumps(doc))
            out=root/'qualification';process=self.invoke(matrix,out)
            self.assertEqual(process.returncode,2,process.stderr)
            report=json.loads((out/'qualification.json').read_text())
            self.assertEqual(report['status'],'INCONCLUSIVE')
            self.assertEqual(report['rows'][0]['reason_codes'],['M5_LOAD_COVERAGE'])

    def test_registered_offset_does_not_hide_raw_load_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);matrix=fixture(root,errors=(500,50500))
            path=root/'high/source/calibration.json';cal=json.loads(path.read_text())
            cal['pairs'][0]['offset_ns']=50500;path.write_text(json.dumps(cal))
            out=root/'qualification';process=self.invoke(matrix,out)
            self.assertEqual(process.returncode,1,process.stderr)
            report=json.loads((out/'qualification.json').read_text())
            self.assertEqual(report['clock_uncertainty_ns'],50500)
            self.assertTrue(all(row['load_error_ns']==50000 for row in report['rows']))

    def test_ten_microsecond_threshold_is_inclusive(self):
        for error,exit_code in ((10000,0),(10001,1)):
            with self.subTest(error=error),tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp);matrix=fixture(root,errors=(error,error));out=root/'qualification'
                process=self.invoke(matrix,out)
                self.assertEqual(process.returncode,exit_code,process.stderr)
                self.assertEqual(json.loads((out/'qualification.json').read_text())['clock_uncertainty_ns'],error)

    def test_nonzero_delay_configuration_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);matrix=fixture(root)
            path=root/'high/source/engine.json';config=json.loads(path.read_text())
            config['lines'][0]['model']['t_ns']=1;path.write_text(json.dumps(config))
            manifest_path=root/'high/source/manifest.json';manifest=json.loads(manifest_path.read_text())
            manifest['config']['sha256']=hashlib.sha256(path.read_bytes()).hexdigest()
            manifest_path.write_text(json.dumps(manifest))
            process=self.invoke(matrix,root/'qualification')
            self.assertEqual(process.returncode,2)
            self.assertIn('t=delta=p=0',process.stderr)

    def test_zero_probability_decimal_format_is_accepted(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);matrix=fixture(root)
            path=root/'high/source/engine.json';config=json.loads(path.read_text())
            config['lines'][0]['model']['p']='0.000';path.write_text(json.dumps(config))
            manifest_path=root/'high/source/manifest.json';manifest=json.loads(manifest_path.read_text())
            manifest['config']['sha256']=hashlib.sha256(path.read_bytes()).hexdigest()
            manifest_path.write_text(json.dumps(manifest))
            process=self.invoke(matrix,root/'qualification')
            self.assertEqual(process.returncode,0,process.stderr)


if __name__=='__main__':unittest.main()
