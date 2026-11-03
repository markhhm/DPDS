# SPDX-License-Identifier: Apache-2.0
"""Acquisition-scoped raw retention after complete sealed M6 judgments.

Receipts live outside immutable scientific results. They authorize only named
raw hash substitutions during resume, never a new judgment from partial inputs.
"""
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
from threading import Lock
import uuid

from . import raw_prefix
from .calibration import Calibration
from .reference import strict_json


def _load(path):
    return strict_json(path.read_bytes())


def _hash(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream,'sha256').hexdigest()


def _utc():
    return datetime.now(timezone.utc).isoformat()


def _sync_directory(path):
    descriptor=os.open(path,os.O_RDONLY|os.O_DIRECTORY)
    try:os.fsync(descriptor)
    finally:os.close(descriptor)


def _write(path,value):
    temporary=path.with_name('.'+path.name+'.'+uuid.uuid4().hex)
    try:
        with temporary.open('x') as stream:
            json.dump(value,stream,indent=2,allow_nan=False);stream.write('\n');stream.flush();os.fsync(stream.fileno())
        os.replace(temporary,path);_sync_directory(path.parent)
    finally:temporary.unlink(missing_ok=True)


def replace_prefix(stage,target):
    """Replace a directory entry; never truncate hardlinked backup content."""
    os.replace(stage,target)
    _sync_directory(target.parent)


def _digest(value):
    return isinstance(value,str) and re.fullmatch('[0-9a-f]{64}',value) is not None


def _positive(value):
    return type(value) is int and value>0


class Retention:
    def __init__(self,matrix_path,matrix,output,*,enabled=False,apply=False,backup_confirmed=False):
        if apply and (not enabled or not backup_confirmed):
            raise ValueError('raw deletion requires --prune-raw --apply-raw-prune --backup-scope-confirmed')
        if backup_confirmed and not enabled:
            raise ValueError('backup confirmation requires --prune-raw')
        self.base=Path(matrix_path).resolve().parent
        self.output=Path(output).resolve()
        self.matrix=matrix
        self.enabled,self.apply=enabled,apply
        self.groups,self.by_run,self.done={},{},set()
        self.lock=Lock()
        for run in matrix['runs']:
            meta=run.get('extensions',{}).get('m6',{})
            if 'raw_retention' not in meta:continue
            aid=meta.get('acquisition_id');policy=meta['raw_retention']
            if not isinstance(aid,str) or not re.fullmatch('[A-Za-z0-9][A-Za-z0-9_.-]{0,127}',aid):
                raise ValueError('unsafe raw retention acquisition ID')
            if not isinstance(policy,dict) or not _positive(policy.get('keep_ns')) or not _positive(policy.get('byte_budget')):
                raise ValueError('raw retention needs positive keep_ns and byte_budget')
            root=policy.get('capture_directory')
            if not isinstance(root,str) or not root or '${' in root:
                raise ValueError('bind the raw retention capture_directory')
            root=(self.base/root).resolve()
            if root==self.base or root in self.base.parents or self.output==root or root in self.output.parents:
                raise ValueError('retention requires a distinct acquisition capture directory')
            group=self.groups.setdefault(aid,dict(policy=policy,root=root,members=[]))
            if group['policy']!=policy or group['root']!=root:
                raise ValueError('shared acquisition raw retention policies differ')
            group['members'].append(run);self.by_run[run['id']]=aid
        if enabled and (matrix.get('mode')!='pcapng' or len(self.by_run)!=len(matrix['runs'])):
            raise ValueError('raw retention requires a policy on every pcapng run')
        roots=[group['root'] for group in self.groups.values()]
        if any(a==b or a in b.parents or b in a.parents for i,a in enumerate(roots) for b in roots[i+1:]):
            raise ValueError('acquisition raw roots must not overlap')
        for aid in self.groups:
            directory=self.directory(aid)
            if directory.exists():
                self.manifest(aid)  # Missing/corrupt provenance must never trigger re-reporting.

    def directory(self,aid):return self.output/'.raw-retention'/aid

    def managed(self,run_id):return run_id in self.by_run

    def manifest(self,aid):
        group=self.groups[aid];path=self.directory(aid)/'manifest.json'
        try:
            value=_load(path)
            if (value['schema']!='hng-m6-raw-manifest-v1' or value['acquisition_id']!=aid
                    or value['policy']!=group['policy'] or not value['files']
                    or set(value['members'])!={r['id'] for r in group['members']}):
                raise ValueError('raw manifest differs from acquisition registration')
            expected=self.discover(aid)[0]
            if {item['path']:item['kind'] for item in value['files']}!=expected or len(value['files'])!=len(expected):
                raise ValueError('raw manifest file set differs from registered captures')
            for item in value['files']:
                if not _digest(item['original_sha256']) or not _positive(item['original_bytes']):
                    raise ValueError('raw manifest needs every original full SHA and size')
                if not 0<=item['start_ns']<item['end_ns']<2**64:
                    raise ValueError('invalid raw retention interval')
            for run in group['members']:
                if value['members'][run['id']]['seal_sha256']!=_hash(self.output/run['id']/'.complete.json'):
                    raise ValueError('complete seal changed after raw registration')
            return value
        except (OSError,KeyError,TypeError) as error:
            raise ValueError('missing or malformed raw retention manifest/seal') from error

    def discover(self,aid):
        """Only declared raw files within one acquisition; never recursively delete."""
        group=self.groups[aid];root=group['root'];files={};pairs={};registrations=[]
        def register(path,kind):
            path=path.resolve()
            if not path.is_relative_to(root) or path==root or not path.is_file():
                raise ValueError('raw path escapes declared acquisition directory or is not a regular file')
            if str(path) in files and files[str(path)]!=kind:raise ValueError('raw kind alias')
            files[str(path)]=kind
        for run in group['members']:
            pair_path=(self.base/run['manifest']).resolve();pair=_load(pair_path)
            registration_path=(pair_path.parent/pair['manifest']).resolve();registration=_load(registration_path)
            verification=registration.get('verification',{})
            if verification.get('mode')!='sampled' or 'native_binary' not in verification:
                raise ValueError('raw retention requires sampled native binary registration')
            register(registration_path.parent/verification['native_binary']['path'],'native')
            registrations.append((registration_path,registration))
            for name in verification['capture_pairs']:
                path=(registration_path.parent/name).resolve();document=_load(path);pairs[str(path)]=document
                for role in ('before','receiver'):register(path.parent/document[role]['path'],'pcapng')
        return files,pairs,registrations

    def replacements(self,run_id):
        """Validate logged substitutions; still hash every retained raw on resume."""
        if not self.managed(run_id):return {}
        aid=self.by_run[run_id];directory=self.directory(aid)
        if not directory.exists():return {}
        manifest=self.manifest(aid);manifest_sha=_hash(directory/'manifest.json');result={}
        for index,item in enumerate(manifest['files']):
            path=directory/f'{index}.json'
            if not path.exists():continue
            try:
                receipt=_load(path)
                if (receipt['schema']!='hng-m6-raw-prune-v1' or receipt['manifest_sha256']!=manifest_sha
                        or any(receipt[key]!=item[key] for key in item)
                        or receipt['state'] not in ('PREPARED','APPLIED')
                        or not _digest(receipt['retained_sha256']) or not _positive(receipt['retained_bytes'])
                        or not receipt['prepared_at'] or not receipt['backup_scope_confirmed']):
                    raise ValueError('invalid raw prune receipt')
                current=_hash(Path(item['path']))
                if current==receipt['retained_sha256'] and Path(item['path']).stat().st_size!=receipt['retained_bytes']:
                    raise ValueError('retained raw size mismatch')
                if receipt['state']=='APPLIED' and current!=receipt['retained_sha256']:
                    raise ValueError('retained raw SHA mismatch')
                if current not in (item['original_sha256'],receipt['retained_sha256']):
                    raise ValueError('raw SHA differs from both logged versions')
                result[item['path']]=(current,item['original_sha256'])
            except (OSError,KeyError,TypeError) as error:
                raise ValueError('missing or malformed raw prune receipt') from error
        return result

    def guard_existing(self,run_id,target):
        # A policy-bearing sealed result is immutable, even if its receipts vanish.
        if self.enabled and target.exists() and not (target/'.complete.json').is_file():
            raise ValueError('raw pruning requires an existing complete seal; incomplete output is preserved')

    def finished(self,run_id):
        if not self.enabled:return
        aid=self.by_run[run_id]
        with self.lock:
            self.done.add(run_id)
            ready=all(run['id'] in self.done for run in self.groups[aid]['members'])
        if ready:self.prune(aid)

    def _ranges(self,aid,pairs,registrations):
        from .pcapng import Capture
        beginnings=[];targets=set();ends=[]
        for name,pair in pairs.items():
            registration_path=(Path(name).parent/pair['manifest']).resolve()
            registration=_load(registration_path)
            calibration=_load(registration_path.parent/registration['calibration_path'])
            mapping=next(m for m in calibration['mappings'] if m['mapping_id']==pair['mapping_id'])
            if (mapping['scale_num']!=mapping['scale_den'] or mapping['target_origin_ns']!=mapping['raw_origin']
                    or not mapping['valid_from_raw']<=pair['start_ns']<pair['end_ns']<=mapping['valid_to_raw']):
                raise ValueError('raw retention requires registered same-clock ns capture pairs')
            targets.add(mapping['target_clock_id']);ends.append(pair['end_ns'])
            capture=Capture(Path(name).parent/pair['before']['path'],
                {int(k):v for k,v in pair['before']['interfaces'].items()},pair['start_ns'],pair['end_ns'])
            iterator=capture.packets()
            try:
                first=next(iterator,None)
                if first is not None:beginnings.append(first[1])
            finally:iterator.close()
        if not beginnings or len(targets)!=1:raise ValueError('raw retention needs ingress evidence on one observation clock')
        start=min(beginnings);end=min(start+self.groups[aid]['policy']['keep_ns'],*ends)
        if start>=end:raise ValueError('empty raw retention window')
        ranges={}
        for path,registration in registrations:
            calibration=_load(path.parent/registration['calibration_path'])
            mappings=[m for m in calibration['mappings'] if m['raw_clock_id']==registration['clocks']['engine']
                      and m['target_clock_id']==next(iter(targets))]
            def lower(mapping,target):
                low,high=mapping['valid_from_raw'],mapping['valid_to_raw']
                while low<high:
                    mid=(low+high)//2
                    if Calibration._mapped(mapping,mid)<target:low=mid+1
                    else:high=mid
                return low
            candidates=[]
            for mapping in mappings:
                if mapping['scale_num']<=0 or mapping['scale_den']<=0:continue
                a,b=lower(mapping,start),lower(mapping,end)
                if (a<b and Calibration._mapped(mapping,mapping['valid_from_raw'])<=start
                        and Calibration._mapped(mapping,mapping['valid_to_raw']-1)>=end-1):
                    candidates.append((a,b,registration['clocks']['engine']))
            if len(candidates)!=1:raise ValueError('unique engine clock mapping must cover retention window')
            native=str((path.parent/registration['verification']['native_binary']['path']).resolve())
            if native in ranges and ranges[native]!=candidates[0]:raise ValueError('shared native clock mappings differ')
            ranges[native]=candidates[0]
        return start,end,next(iter(targets)),ranges

    def _register(self,aid):
        from .matrix import completed_run
        group=self.groups[aid];files,pairs,registrations=self.discover(aid)
        originals={};members={}
        for run in group['members']:
            target=self.output/run['id'];source=(self.base/run['manifest']).resolve()
            if completed_run(target,source,run['id'],self.matrix['scope'],'pcapng',self.matrix['plan'].get('deployment')) is None:
                raise ValueError('raw pruning requires verified complete member seals and full source SHA')
            seal=_load(target/'.complete.json');members[run['id']]=dict(seal_sha256=_hash(target/'.complete.json'))
            for path in files:
                value=seal['sources'].get(path)
                if not _digest(value):raise ValueError('complete seal lacks a full raw SHA')
                if path in originals and originals[path]!=value:raise ValueError('shared raw full SHA disagrees')
                originals[path]=value
        start,end,clock,ranges=self._ranges(aid,pairs,registrations)
        entries=[]
        for path,kind in sorted(files.items()):
            a,b,c=ranges[path] if kind=='native' else (start,end,clock)
            entries.append(dict(path=path,kind=kind,original_sha256=originals[path],original_bytes=Path(path).stat().st_size,
                                start_ns=a,end_ns=b,clock_id=c))
        value=dict(schema='hng-m6-raw-manifest-v1',acquisition_id=aid,policy=group['policy'],members=members,
                   observation_range=dict(start_ns=start,end_ns=end,clock_id=clock),files=entries,
                   producer_versions=[r.get('producer_versions',{}) for _,r in registrations])
        directory=self.directory(aid);directory.mkdir(parents=True,exist_ok=True)
        _write(directory/'manifest.json',value)
        return value

    def prune(self,aid):
        from .matrix import completed_run
        directory=self.directory(aid)
        manifest=self.manifest(aid) if directory.exists() else self._register(aid)
        for run in self.groups[aid]['members']:
            if completed_run(self.output/run['id'],(self.base/run['manifest']).resolve(),run['id'],self.matrix['scope'],
                             'pcapng',self.matrix['plan'].get('deployment'),self.replacements(run['id'])) is None:
                raise ValueError('complete artifacts failed retention validation')
        if not self.apply:return
        manifest_sha=_hash(directory/'manifest.json');staged=[];total=0
        try:
            for index,item in enumerate(manifest['files']):
                target=Path(item['path']);receipt_path=directory/f'{index}.json'
                receipt=_load(receipt_path) if receipt_path.exists() else None
                current=_hash(target)
                if receipt and current==receipt['retained_sha256']:
                    total+=receipt['retained_bytes'];staged.append((None,target,receipt_path,receipt,None));continue
                if current!=item['original_sha256'] or target.stat().st_size!=item['original_bytes']:
                    raise ValueError('original raw changed before clipping')
                identity=raw_prefix._identity(target)
                stage=target.with_name('.'+target.name+'.prefix-'+uuid.uuid4().hex)
                options=dict(start_ns=item['start_ns'],end_ns=item['end_ns'],source_sha256=item['original_sha256'])
                if item['kind']=='native':options['clock_id']=item['clock_id']
                retained=getattr(raw_prefix,item['kind'])(target,stage,**options)
                receipt=dict(item,schema='hng-m6-raw-prune-v1',manifest_sha256=manifest_sha,
                             retained_bytes=retained['bytes'],retained_sha256=retained['sha256'],
                             retained_records=retained['records'],state='PREPARED',prepared_at=_utc(),
                             applied_at=None,backup_scope_confirmed=True)
                staged.append((stage,target,receipt_path,receipt,identity));total+=retained['bytes']
            if total>self.groups[aid]['policy']['byte_budget']:
                raise ValueError('actual retained raw exceeds acquisition byte budget; originals preserved')
            # Authenticate ALL original directory entries again before the first destructive replace.
            for stage,target,_,_,identity in staged:
                if stage is not None and raw_prefix._identity(target)!=identity:
                    raise ValueError('raw changed while staging prefixes')
            for stage,target,receipt_path,receipt,identity in staged:
                if receipt['state']=='APPLIED':continue
                if stage is not None:
                    _write(receipt_path,receipt)  # durable write-ahead deletion record
                    if raw_prefix._identity(target)!=identity:raise ValueError('raw changed before replacement')
                    replace_prefix(stage,target)
                receipt=dict(receipt,state='APPLIED',applied_at=_utc())
                _write(receipt_path,receipt)
        finally:
            for stage,_,_,_,_ in staged:
                if stage is not None:stage.unlink(missing_ok=True)
