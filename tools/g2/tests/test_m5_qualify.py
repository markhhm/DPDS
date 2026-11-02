# SPDX-License-Identifier: Apache-2.0
"""Synthetic captures exercise the public M5 CLI and P1-0 export contract."""
import csv
import hashlib
import json
import os
from pathlib import Path
import shutil
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


def physical_metadata(matrix):
    """Exercise physical schema logic using synthetic bytes, never hardware evidence."""
    root=matrix.parent;doc=json.loads(matrix.read_text());doc['scope']='physical'
    matrix.write_text(json.dumps(doc))
    for run in doc['runs']:
        folder=root/run['id']/'source'
        path=folder/'manifest.json';reg=json.loads(path.read_text());reg['scope']='physical'
        path.write_text(json.dumps(reg))
        path=folder/'calibration.json';cal=json.loads(path.read_text())
        for clock in cal['clocks']:
            clock.update(kind='engine_monotonic' if clock['clock_id']=='engine' else 'nic_rx',mode='hardware')
        cal['qualification'].update(scope='physical',offered_bps=136000000)
        for key in ('source_versions','firmware_versions','driver_versions','timestamp_points'):cal[key]={'fixture':'test-only'}
        path.write_text(json.dumps(cal))


def p1_manifest(root,scope='software-only'):
    path=root/'p1-0.json'
    path.write_text(json.dumps(dict(schema='hng-p1-0-v1',scope=scope,status='FAIL',
        versions={'observed':{'fixture':'version'}},byte_accounting={'fixture':'preserve'},
        clock_uncertainty_ns=123,clock_qualification={'status':'old'},checks=['preserve'])))
    return path


class M5QualifyTests(unittest.TestCase):
    def invoke(self,matrix,out,*args,env=None):
        return subprocess.run([sys.executable,'-m','tools.g2.m5_qualify',str(matrix),
            '--out',str(out),*map(str,args)],capture_output=True,text=True,timeout=45,env=env)

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
            with (out/'results.csv').open() as stream:table=list(csv.DictReader(stream))
            self.assertEqual(len(table),2)
            self.assertTrue(all(row['clock_uncertainty_ns']=='500' and row['missing']=='0' for row in table))
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

    def test_manifest_export_preserves_environment_status_and_synthetic_scope(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);matrix=fixture(root);source=p1_manifest(root);original=source.read_bytes()
            out=root/'qualification';target=root/'qualified.json'
            process=self.invoke(matrix,out,'--p1-manifest',source,'--manifest-out',target)
            self.assertEqual(process.returncode,0,process.stderr)
            updated=json.loads(target.read_text());report=json.loads((out/'qualification.json').read_text())
            expected=json.loads(original);expected.update(clock_uncertainty_ns=None,clock_qualification=report)
            self.assertEqual(updated,expected)
            self.assertEqual(report['sources'][str(source.resolve())],hashlib.sha256(original).hexdigest())
            self.assertEqual(source.read_bytes(),original)

    def test_physical_metadata_exports_only_passed_clock_bounds(self):
        for error,status,bound in ((500,'PASS',500),(50000,'FAIL',None)):
            with self.subTest(error=error),tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp);matrix=fixture(root,errors=(error,error));physical_metadata(matrix)
                source=p1_manifest(root,'physical');target=root/'qualified.json'
                process=self.invoke(matrix,root/'qualification','--p1-manifest',source,'--manifest-out',target)
                self.assertEqual(process.returncode,0 if status=='PASS' else 1,process.stderr)
                updated=json.loads(target.read_text())
                self.assertEqual(updated['status'],'FAIL')
                self.assertEqual(updated['clock_uncertainty_ns'],bound)
                self.assertEqual(updated['clock_qualification']['status'],status)
                self.assertEqual(updated['clock_qualification']['physical_qualified'],status=='PASS')

    def test_export_refuses_scope_mismatch_and_existing_outputs(self):
        for kind in ('scope','exists','same-file','missing-argument'):
            with self.subTest(kind=kind),tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp);matrix=fixture(root);source=p1_manifest(root,'physical' if kind=='scope' else 'software-only')
                target=source if kind=='same-file' else root/'qualified.json'
                if kind=='exists':target.write_text('keep')
                originals={p:p.read_bytes() for p in (source,target) if p.exists()}
                args=['--p1-manifest',source]
                if kind!='missing-argument':args+=['--manifest-out',target]
                process=self.invoke(matrix,root/'qualification',*args)
                self.assertEqual(process.returncode,2,process.stderr)
                for p,data in originals.items():self.assertEqual(p.read_bytes(),data)
                if kind in ('scope','missing-argument'):self.assertFalse(target.exists())

    def test_manifest_output_cannot_replace_reports_or_change_sealed_matrix_outputs(self):
        for name in ('qualification.json','results.csv','summary.txt','matrix/high/extra.json'):
            with self.subTest(name=name),tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp);matrix=fixture(root);source=p1_manifest(root);out=root/'qualification'
                process=self.invoke(matrix,out,'--p1-manifest',source,'--manifest-out',out/name)
                self.assertEqual(process.returncode,2,process.stderr)
                self.assertFalse((out/name).exists())

    def test_contradictory_native_drop_counters_do_not_pass_a_complete_capture(self):
        from tools.g2.protocol import StreamReader,StreamWriter
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);matrix=fixture(root);folder=root/'high/source'
            path=folder/'counters.jsonl';reader=StreamReader(path,'counters','high')
            rows=list(reader);header=reader.header;reader._source.close();path.unlink()
            writer=StreamWriter(path,'high',header['stream_id'],'counters',header['clock_id'])
            for row in rows:
                if row['scope']=='port' and row['phase']=='end':row['values']['q_drop']=1
                writer.emit('counter',**{k:v for k,v in row.items() if k not in ('kind','run','stream_id','seq')})
            writer.finish()
            manifest_path=folder/'manifest.json';manifest=json.loads(manifest_path.read_text())
            next(s for s in manifest['streams'] if s['role']=='counters')['file_sha256']=hashlib.sha256(path.read_bytes()).hexdigest()
            manifest_path.write_text(json.dumps(manifest))
            out=root/'qualification';process=self.invoke(matrix,out)
            self.assertEqual(process.returncode,2,process.stderr)
            self.assertEqual(json.loads((out/'qualification.json').read_text())['status'],'INCONCLUSIVE')

    def test_load_bound_uses_exact_fits_instead_of_rounded_display_offsets(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);matrix=fixture(root)
            for name,spacing,sign in (('low',10000,-1),('high',1000,1)):
                before=[100000+i*spacing for i in range(400)]
                receiver=[time+sign*(5000+(i%3==0)) for i,time in enumerate(before)]
                pcapng(root/name/'before.pcapng',before);pcapng(root/name/'receiver.pcapng',receiver)
                path=root/name/'source/calibration.json';cal=json.loads(path.read_text())
                cal['pairs'][0]['uncertainty_ns']=10000;path.write_text(json.dumps(cal))
            out=root/'qualification';process=self.invoke(matrix,out)
            self.assertEqual(process.returncode,1,process.stderr)
            report=json.loads((out/'qualification.json').read_text())
            self.assertEqual(report['clock_uncertainty_ns'],10001)
            self.assertTrue(all(row['load_error_ns']==10001 for row in report['rows']))

    def test_external_quantization_and_mapping_bounds_are_not_discarded(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);matrix=fixture(root)
            for name in ('low','high'):
                path=root/name/'source/calibration.json';cal=json.loads(path.read_text())
                cal['pairs'][0].update(quantization_bound_ns=5001,mapping_bound_ns=5000,uncertainty_ns=10001)
                path.write_text(json.dumps(cal))
            out=root/'qualification';process=self.invoke(matrix,out)
            self.assertEqual(process.returncode,1,process.stderr)
            self.assertEqual(json.loads((out/'qualification.json').read_text())['clock_uncertainty_ns'],10501)

    def test_zero_delay_is_checked_on_the_measured_config_not_only_before_the_matrix(self):
        from tools.g2.matrix import SEQCHECK
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);matrix=fixture(root);binary=root/'bin';binary.mkdir()
            proxy=binary/'seqcheck'
            # The first real checker call changes a later cell before its source snapshot.
            proxy.write_text('#!'+sys.executable+'\n'+'''import hashlib,json,os,sys
from pathlib import Path
root=Path(os.environ['M5_TEST_ROOT'])
config=root/'high/source/engine.json';doc=json.loads(config.read_text())
doc['lines'][0]['model']['t_ns']=1;config.write_text(json.dumps(doc))
path=root/'high/source/manifest.json';manifest=json.loads(path.read_text())
manifest['config']['sha256']=hashlib.sha256(config.read_bytes()).hexdigest()
path.write_text(json.dumps(manifest))
os.execv(os.environ['M5_REAL_SEQCHECK'],[os.environ['M5_REAL_SEQCHECK'],*sys.argv[1:]])
''')
            proxy.chmod(0o755)
            env=dict(os.environ,PATH=str(binary)+os.pathsep+os.environ['PATH'],M5_TEST_ROOT=str(root),
                     M5_REAL_SEQCHECK=str(Path(shutil.which('seqcheck') or SEQCHECK).resolve()))
            out=root/'qualification';process=self.invoke(matrix,out,env=env)
            self.assertEqual(process.returncode,2,process.stderr)
            report=json.loads((out/'qualification.json').read_text())
            high=next(row for row in report['rows'] if row['run']=='high')
            self.assertEqual(high['reason_codes'],['M5_ZERO_DELAY_REQUIRED'])
            self.assertEqual(report['status'],'INCONCLUSIVE')


if __name__=='__main__':unittest.main()
