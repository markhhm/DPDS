# SPDX-License-Identifier: Apache-2.0
"""Fresh synthetic data only: destructive retention gates and sealed resume."""
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tools.g2.matrix import run_matrix
from tools.g2.raw_prefix import decode_native
from tools.g2.tests.binary_sample_fixture import make_case


def digest(path):return hashlib.sha256(path.read_bytes()).hexdigest()
def load(path):return json.loads(path.read_bytes())
def write(path,value):path.write_text(json.dumps(value))


class RawPruneTests(unittest.TestCase):
    def setup_case(self,root):
        raw=root/'capture';make_case(raw)
        pair=load(raw/'pair.json');pair['manifest']='binary.json';write(raw/'pair.json',pair)
        manifest=load(raw/'binary.json');manifest['verification']['seqchecks']=[];write(raw/'binary.json',manifest)
        policy=dict(keep_ns=200000,keep_seconds='0.000200000',byte_budget=1000000,
                    estimated_bytes=100000,estimated_bytes_per_second={},capture_directory='capture')
        meta=dict(acquisition_id='acq',condition_id='condition',ingress_port=0,raw_retention=policy)
        matrix=dict(schema='hng-m6-matrix-v1',scope='software-only',mode='pcapng',plan={},
                    runs=[dict(id='demo',manifest='capture/pair.json',extensions=dict(m6=meta))])
        path=root/'matrix.json';write(path,matrix)
        self.assertEqual(run_matrix(path,root/'out')['status'],'PASS')
        return path,raw,root/'out'

    def invoke(self,path,out,**kwargs):
        return run_matrix(path,out,prune_raw=True,**kwargs)

    def test_dry_run_apply_and_resume_preserve_complete_artifacts(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);path,raw,out=self.setup_case(root)
            report={str(p.relative_to(out/'demo')):p.read_bytes() for p in (out/'demo').rglob('*') if p.is_file() and not p.is_symlink()}
            original={p.name:(digest(p),p.stat().st_size) for p in (raw/'native.hngb',raw/'before.pcapng',raw/'receiver.pcapng')}
            self.invoke(path,out)
            self.assertEqual({n:digest(raw/n) for n in original},{n:v[0] for n,v in original.items()})
            manifest=load(out/'.raw-retention/acq/manifest.json')
            self.assertEqual(len(manifest['files']),3)
            self.invoke(path,out,apply_raw_prune=True,backup_scope_confirmed=True)
            for index,item in enumerate(manifest['files']):
                receipt=load(out/f'.raw-retention/acq/{index}.json')
                self.assertEqual(receipt['state'],'APPLIED')
                self.assertEqual(receipt['original_sha256'],original[Path(item['path']).name][0])
                self.assertEqual(receipt['original_bytes'],original[Path(item['path']).name][1])
                self.assertEqual(receipt['retained_sha256'],digest(Path(item['path'])))
                self.assertTrue(receipt['prepared_at']);self.assertTrue(receipt['applied_at'])
                self.assertLess(receipt['start_ns'],receipt['end_ns'])
            with (raw/'native.hngb').open('rb') as source:
                self.assertFalse(decode_native(source,io.StringIO())['complete'])
            from tools.g2.pcapng_arrays import CaptureArrays
            for name in ('before.pcapng','receiver.pcapng'):
                reader=CaptureArrays(raw/name,{0:0 if name=='before.pcapng' else 1},0,200000)
                list(reader.batches());self.assertTrue(reader.integrity['complete'])
            from tools.g2.matrix import report_one
            self.assertEqual(report_one(path.parent/'capture/pair.json',root/'fresh-report','demo','software-only','pcapng'),2)
            before={p.name:p.read_bytes() for p in (out/'.raw-retention/acq').iterdir()}
            self.assertEqual(run_matrix(path,out)['status'],'PASS')
            self.invoke(path,out,apply_raw_prune=True,backup_scope_confirmed=True)
            self.assertEqual(before,{p.name:p.read_bytes() for p in (out/'.raw-retention/acq').iterdir()})
            self.assertEqual(report,{str(p.relative_to(out/'demo')):p.read_bytes() for p in (out/'demo').rglob('*') if p.is_file() and not p.is_symlink()})

    def test_no_seal_hash_or_backup_ack_cannot_delete(self):
        for missing in ('seal','hash','backup'):
            with self.subTest(missing=missing),tempfile.TemporaryDirectory() as temp:
                root=Path(temp);path,raw,out=self.setup_case(root)
                before={p.name:digest(p) for p in raw.iterdir() if p.is_file()}
                seal=out/'demo/.complete.json'
                if missing=='seal':seal.unlink()
                if missing=='hash':
                    value=load(seal);value['sources'].pop(str(raw/'native.hngb'));write(seal,value)
                with self.assertRaises(ValueError):
                    self.invoke(path,out,apply_raw_prune=True,backup_scope_confirmed=missing!='backup')
                self.assertEqual(before,{p.name:digest(p) for p in raw.iterdir() if p.is_file()})

    def test_quota_refusal_and_corrupted_manifest_leave_raw_intact(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);path,raw,out=self.setup_case(root)
            self.invoke(path,out)
            manifest_path=out/'.raw-retention/acq/manifest.json'
            value=load(manifest_path);value['files'][0].pop('original_sha256');write(manifest_path,value)
            before={p.name:digest(p) for p in raw.iterdir() if p.is_file()}
            with self.assertRaises(ValueError):self.invoke(path,out,apply_raw_prune=True,backup_scope_confirmed=True)
            self.assertEqual(before,{p.name:digest(p) for p in raw.iterdir() if p.is_file()})
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);raw=root/'capture';make_case(raw)
            # Register the tiny quota BEFORE evaluation: matrix cannot change on resume.
            pair=load(raw/'pair.json');pair['manifest']='binary.json';write(raw/'pair.json',pair)
            policy=dict(keep_ns=200000,byte_budget=1,capture_directory='capture')
            matrix=dict(schema='hng-m6-matrix-v1',scope='software-only',mode='pcapng',plan={},runs=[
                dict(id='demo',manifest='capture/pair.json',extensions=dict(m6=dict(acquisition_id='acq',raw_retention=policy)))])
            path=root/'matrix.json';write(path,matrix);out=root/'out';run_matrix(path,out)
            before={p.name:digest(p) for p in raw.iterdir() if p.is_file()}
            with self.assertRaisesRegex(ValueError,'budget'):
                self.invoke(path,out,apply_raw_prune=True,backup_scope_confirmed=True)
            self.assertEqual(before,{p.name:digest(p) for p in raw.iterdir() if p.is_file()})
            self.assertFalse(list(raw.glob('*.prefix-*')))

    def test_tampered_prefix_nonraw_or_report_never_archives_full_result(self):
        for item in ('native.hngb','engine.json','report'):
            with self.subTest(item=item),tempfile.TemporaryDirectory() as temp:
                root=Path(temp);path,raw,out=self.setup_case(root)
                self.invoke(path,out,apply_raw_prune=True,backup_scope_confirmed=True)
                seal=(out/'demo/.complete.json').read_bytes()
                target=out/'demo/report.json' if item=='report' else raw/item
                target.write_bytes(target.read_bytes()+b' ')
                with self.assertRaises(ValueError):run_matrix(path,out)
                self.assertEqual((out/'demo/.complete.json').read_bytes(),seal)
                self.assertTrue((out/'demo/report.json').exists())

    def test_failed_resume_preserves_global_summary_and_rejects_wrong_retained_size(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);path,raw,out=self.setup_case(root)
            self.invoke(path,out,apply_raw_prune=True,backup_scope_confirmed=True)
            summary=(out/'summary.json').read_bytes()
            (raw/'native.hngb').write_bytes((raw/'native.hngb').read_bytes()+b' ')
            with self.assertRaises(ValueError):run_matrix(path,out)
            self.assertEqual((out/'summary.json').read_bytes(),summary)
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);path,raw,out=self.setup_case(root)
            self.invoke(path,out,apply_raw_prune=True,backup_scope_confirmed=True)
            receipt=out/'.raw-retention/acq/0.json';value=load(receipt)
            value['retained_bytes']=1;write(receipt,value)
            with self.assertRaises(ValueError):run_matrix(path,out)

    def test_declared_scope_and_hardlinked_backup(self):
        import os
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);path,raw,out=self.setup_case(root)
            backup=root/'backup.hngb';os.link(raw/'native.hngb',backup);original=digest(backup)
            self.invoke(path,out,apply_raw_prune=True,backup_scope_confirmed=True)
            self.assertEqual(digest(backup),original)
            self.assertNotEqual(digest(raw/'native.hngb'),original)
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);path,raw,out=self.setup_case(root)
            original=raw/'native.hngb';outside=root/'outside.hngb';original.rename(outside);original.symlink_to(outside)
            with self.assertRaises(ValueError):
                self.invoke(path,out,apply_raw_prune=True,backup_scope_confirmed=True)
            self.assertTrue(original.is_symlink())

    def test_engine_clock_cut_and_shared_member_barrier(self):
        from tools.g2.raw_prune import Retention
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);path,raw,out=self.setup_case(root);matrix=load(path)
            matrix['runs'].append(dict(matrix['runs'][0],id='other'))
            manager=Retention(path,matrix,out,enabled=True)
            with patch.object(manager,'prune') as prune:
                manager.finished('demo');prune.assert_not_called()
                manager.finished('other');prune.assert_called_once_with('acq')
            cal=load(raw/'calibration.json')
            engine=next(m for m in cal['mappings'] if m['raw_clock_id']=='engine')
            engine.update(raw_origin=1000,target_origin_ns=0)
            write(raw/'calibration.json',cal)
            _,pairs,registrations=manager.discover('acq')
            start,end,clock,ranges=manager._ranges('acq',pairs,registrations)
            self.assertEqual(ranges[str(raw/'native.hngb')],(start+1000,end+1000,'engine'))

    def test_bidirectional_workers_prune_shared_native_once_after_both_seals(self):
        from tools.g2.tests.binary_split_fixture import make_case as make_split
        from tools.g2.protocol import StreamReader,StreamWriter
        from tools.g2 import raw_prune
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);raw=root/'capture';make_split(raw)
            other=load(raw/'binary.json');other['run']='other'
            calibration=load(raw/'calibration.json');calibration['run']='other'
            write(raw/'cal-other.json',calibration);other['calibration_path']='cal-other.json'
            writer=StreamWriter(raw/'counters-other.jsonl','other','native-counters','counters','engine')
            for row in StreamReader(raw/'counters.jsonl',role='counters'):
                writer.emit(row['kind'],**{k:v for k,v in row.items() if k not in ('kind','run','stream_id','seq')})
            writer.finish()
            other['streams'][0].update(path='counters-other.jsonl',file_sha256=digest(raw/'counters-other.jsonl'))
            write(raw/'binary-other.json',other)
            pair=load(raw/'pair-reverse.json');pair['manifest']='binary-other.json';write(raw/'pair-reverse.json',pair)
            policy=dict(keep_ns=200000,byte_budget=1000000,capture_directory='capture')
            matrix=dict(schema='hng-m6-matrix-v1',scope='software-only',mode='pcapng',workers=2,plan={},runs=[
                dict(id=name,manifest=f'capture/pair-{direction}.json',extensions=dict(m6=dict(
                     acquisition_id='acq',ingress_port=port,raw_retention=policy)))
                for name,direction,port in [('demo','forward',0),('other','reverse',1)]])
            path=root/'matrix.json';write(path,matrix);out=root/'out';codec=raw_prune.raw_prefix.native;calls=[]
            def checked(*args,**kwargs):
                self.assertTrue(all((out/name/'.complete.json').is_file() for name in ('demo','other')))
                calls.append(args[0]);return codec(*args,**kwargs)
            with patch.object(raw_prune.raw_prefix,'native',checked):
                result=self.invoke(path,out,apply_raw_prune=True,backup_scope_confirmed=True)
            self.assertEqual(result['status'],'PASS');self.assertEqual(calls,[raw/'native.hngb'])
            self.assertEqual(len(load(out/'.raw-retention/acq/manifest.json')['files']),5)
            self.assertEqual(run_matrix(path,out)['status'],'PASS')

    def test_interrupted_replace_is_recoverable_without_rejudging(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);path,raw,out=self.setup_case(root)
            from tools.g2 import raw_prune
            replace=raw_prune.replace_prefix
            calls=[]
            def interrupted(stage,target):
                replace(stage,target);calls.append(target)
                if len(calls)==1:raise OSError('injected interruption after replace')
            with patch.object(raw_prune,'replace_prefix',interrupted),self.assertRaises(OSError):
                self.invoke(path,out,apply_raw_prune=True,backup_scope_confirmed=True)
            self.assertEqual(run_matrix(path,out)['status'],'PASS')
            self.invoke(path,out,apply_raw_prune=True,backup_scope_confirmed=True)
            self.assertTrue(all(load(p)['state']=='APPLIED' for p in (out/'.raw-retention/acq').glob('[0-9]*.json')))


if __name__=='__main__':unittest.main()
