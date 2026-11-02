# SPDX-License-Identifier: Apache-2.0
"""Qualify closed zero-delay pcapng cells with the existing M6 runner."""
import argparse
import csv
from fractions import Fraction
import hashlib
import json
import os
from pathlib import Path
import sys

from .calibration import Calibration
from .matrix import completed_run, file_hash, run_matrix
from .protocol import StreamError, StreamReader
from .reference import strict_json
from .validate import load_registration


LIMIT_NS = 10000
MIN_SAMPLES = 368


def _require(condition,code,message):
    if not condition:raise StreamError(code,message,'INCONCLUSIVE')


def _ceil(value):return -(-value.numerator//value.denominator)


def _zero_delay(path):
    _,config,_=load_registration(path)
    for line in config['lines']:
        model=line['model']
        _require(model['t_ns']==model['delta_ns']==0 and Fraction(model['p'])==0 and line['trace'] is None,
                 'M5_ZERO_DELAY_REQUIRED','M5 requires t=delta=p=0 without trace')
    manifest=strict_json(Path(path).read_bytes())
    _require(manifest['timing_mode']=='calibration' and manifest.get('verification',{}).get('mode')=='sampled',
             'M5_CALIBRATION_REQUIRED','register sampled calibration cells')
    return manifest


def _rows(folder,run,scope):
    report=strict_json((folder/'report.json').read_bytes())
    _require(report.get('verification_mode')=='sampled','M5_REPORT_INCOMPLETE','sampled capture report is unavailable')
    _require(report['stream_integrity'] and all(row['complete'] for row in report['stream_integrity']),
             'M5_STREAM_INCOMPLETE','an input stream is incomplete')
    # M5 measures clocks and bypass; the separate M6 observer-cost gate is unchanged.
    unexpected=[reason for reason in report['reason_codes'] if not reason.startswith('observer_cost_')]
    root=folder/'capture/bundle';manifest=_zero_delay(root/'manifest.json')
    calibration=Calibration(strict_json((root/manifest['calibration_path']).read_bytes()),scope,run)
    loads={}
    for stream in manifest['streams']:
        if stream['role']!='before':continue
        reader=StreamReader(root/stream['path'],'before',run)
        try:
            for packet in reader:
                key=(str(packet['ingress_port']),packet['line'])
                state=loads.setdefault(key,dict(n=0,first=packet['ingress_ts_ns'],last=0,bits=0))
                state['n']+=1;state['last']=packet['ingress_ts_ns']
                if state['n']>1:state['bits']+=8*(max(packet['length']+4,64)+20)
            _require(reader.integrity['complete'] and reader.integrity['file_sha256']==stream['file_sha256'],
                     'M5_STREAM_INCOMPLETE','before stream differs from registration')
        finally:reader._source.close()
    states={}
    with (folder/'samples.jsonl').open() as samples:
        for raw in samples:
            sample=strict_json(raw);expected=sample['expected'];before=sample['before'];receiver=sample['receive']
            if before is None or receiver is None:continue
            pair=calibration.pair(before,receiver)
            key=(str(expected['ingress_port']),expected['line'])
            state=states.setdefault(key,dict(n=0,first=before['ingress_ts_ns'],last=0,
                sx=0,sy=0,sxx=0,sxy=0,max_abs=0,extra=0,
                clock_path=calibration.clocks[before['timestamp']['clock_id']]))
            x=before['ingress_ts_ns']-state['first'];y=pair['raw_owd_ns']
            state['n']+=1;state['last']=max(state['last'],before['ingress_ts_ns'])
            state['sx']+=x;state['sy']+=y;state['sxx']+=x*x;state['sxy']+=x*y
            state['max_abs']=max(state['max_abs'],abs(y))
            bound=calibration.pairs[pair['pair_id']]
            state['extra']=max(state['extra'],bound['quantization_bound_ns']+bound['mapping_bound_ns'])
    rows=[]
    for port,line in sorted({(str(row['ingress_port']),row['name']) for row in manifest['lines']}):
        check=report['seqcheck'][port];totals=check['totals']
        _require(totals is not None and check['order']['status'] in ('PASS','FAIL') and
                 all(source['health']['complete'] and source['health']['capture_drops']==0
                 for source in check['inputs'].values()),'M5_CAPTURE_INCOMPLETE','full capture comparison is incomplete')
        load=loads.get((port,line),{});state=states.get((port,line),{})
        n=state.get('n',0);span=load.get('last',0)-load.get('first',0)
        denominator=n*state.get('sxx',0)-state.get('sx',0)**2
        anomalies=sum(totals[k] for k in ('ordinary_reorder','missing','duplicate_ingress',
            'duplicate_receiver','unexpected','flag_mismatch'))
        if unexpected or n<MIN_SAMPLES or denominator<=0 or span<=0:
            reasons=['M5_REPORT_INCOMPLETE' if unexpected else 'M5_TIMING_COVERAGE']
            if anomalies:reasons.append('M5_BYPASS_ANOMALY')
            rows.append(dict(run=run,direction=port,line=line,timing_samples=n,missing=totals['missing'],
                seqcheck_anomalies=anomalies,status='FAIL' if anomalies else 'INCONCLUSIVE',
                reason_codes=reasons,clock_uncertainty_ns=None))
            continue
        slope=Fraction(n*state['sxy']-state['sx']*state['sy'],denominator)
        offset=Fraction(state['sy'],n)-slope*Fraction(state['sx'],n)
        drift=_ceil(abs(slope)*(state['last']-state['first']))
        row=dict(run=run,direction=port,line=line,timing_samples=n,
            offered_bps=round(Fraction(load['bits']*10**9,span)),offset_ns=round(offset),
            offset_fraction_ns=dict(numerator=offset.numerator,denominator=offset.denominator),
            drift_ppb=float(slope*10**9),drift_span_ns=drift,max_abs_error_ns=state['max_abs'],
            external_bound_ns=state['extra'],clock_uncertainty_ns=max(state['max_abs'],drift)+state['extra'],
            missing=totals['missing'],seqcheck_anomalies=anomalies,clock_path=state['clock_path'],
            first_ns=state['first'],last_ns=state['last'])
        rows.append(row)
    return rows


def qualify(matrix_path,output,*,p1_manifest=None,manifest_out=None):
    path=Path(matrix_path).resolve();destination=Path(output)
    raw_matrix=path.read_bytes();matrix=strict_json(raw_matrix)
    sources={str(path):hashlib.sha256(raw_matrix).hexdigest()};deployment=None
    _require((p1_manifest is None)==(manifest_out is None),'M5_MANIFEST_ARGUMENTS',
             '--p1-manifest and --manifest-out must be supplied together')
    if p1_manifest is not None:
        p1_source=Path(p1_manifest).resolve();target=Path(manifest_out)
        raw=p1_source.read_bytes();deployment=strict_json(raw)
        _require(deployment['schema']=='hng-p1-0-v1' and deployment['scope']==matrix['scope'],
                 'M5_MANIFEST_SCOPE','P1-0 schema/scope differs from qualification')
        _require(not target.exists() and not target.is_symlink(),'M5_OUTPUT_EXISTS','manifest output already exists')
        report_root=destination.resolve();output_path=target.resolve()
        _require(output_path!=report_root and not output_path.is_relative_to(report_root/'matrix') and
                 output_path not in {report_root/name for name in ('qualification.json','results.csv','summary.txt')},
                 'M5_OUTPUT_COLLISION','manifest output collides with qualification evidence')
        sources[str(p1_source)]=hashlib.sha256(raw).hexdigest()
    _require(matrix.get('mode')=='pcapng','M5_PCAPNG_REQUIRED','M5 requires directional pcapng cells')
    for run in matrix['runs']:
        pair_path=path.parent/run['manifest'];pair=strict_json(pair_path.read_bytes())
        _zero_delay(pair_path.parent/pair['manifest'])
    destination.mkdir(parents=True,exist_ok=False,mode=0o700)
    scope=matrix['scope'];rows=[]
    run_matrix(path,destination/'matrix')
    for run in matrix['runs']:
        folder=destination/'matrix'/run['id'];source=path.parent/run['manifest']
        try:
            _require(completed_run(folder,source,run['id'],scope,'pcapng') is not None,
                     'M5_REPORT_INCOMPLETE','M6 completion receipt is unavailable or changed')
            receipt=strict_json((folder/'.complete.json').read_bytes());sources.update(receipt['sources'])
            selected=_rows(folder,run['id'],scope)
            _require(completed_run(folder,source,run['id'],scope,'pcapng') is not None,
                     'M5_SOURCE_CHANGED','inputs or reports changed during qualification')
            rows.extend(selected)
        except (ValueError,KeyError,TypeError,OSError) as error:
            rows.append(dict(run=run['id'],status='INCONCLUSIVE',reason_codes=[getattr(error,'code','M5_INPUT_INVALID')],
                             reason=str(error),clock_uncertainty_ns=None))
    groups={}
    for row in rows:
        if row['clock_uncertainty_ns'] is not None:
            key=(row['direction'],row['line'],json.dumps(row['clock_path'],sort_keys=True))
            groups.setdefault(key,[]).append(row)
    for group in groups.values():
        offsets=[Fraction(row['offset_fraction_ns']['numerator'],row['offset_fraction_ns']['denominator']) for row in group]
        load_error=_ceil(max(offsets)-min(offsets))
        covered=len({row['offered_bps'] for row in group})>=2
        for row in group:
            row['load_error_ns']=load_error
            row['clock_uncertainty_ns']=max(row['clock_uncertainty_ns'],load_error+row['external_bound_ns'])
            row['status']='FAIL' if row['seqcheck_anomalies'] or row['clock_uncertainty_ns']>LIMIT_NS else 'PASS' if covered else 'INCONCLUSIVE'
            row['reason_codes']=[]
            if row['seqcheck_anomalies']:row['reason_codes'].append('M5_BYPASS_ANOMALY')
            if row['clock_uncertainty_ns']>LIMIT_NS:row['reason_codes'].append('M5_CLOCK_UNCERTAINTY')
            if not covered:row['reason_codes'].append('M5_LOAD_COVERAGE')
    status='FAIL' if any(row['status']=='FAIL' for row in rows) else 'INCONCLUSIVE' if not rows or any(row['status']!='PASS' for row in rows) else 'PASS'
    bound=None if any(row['clock_uncertainty_ns'] is None for row in rows) else max(row['clock_uncertainty_ns'] for row in rows)
    result=dict(schema='hng-m5-qualification-v1',scope=scope,status=status,
        physical_qualified=scope=='physical' and status=='PASS',threshold_ns=LIMIT_NS,
        clock_uncertainty_ns=bound,rows=rows,sources=sources,
        coverage='registered main timing samples; full identities via seqcheck; observed loads/windows only; no fitted correction',
        m6_report=str(destination/'matrix/summary.json'))
    _require(file_hash(path)==sources[str(path)],'M5_SOURCE_CHANGED','matrix changed during qualification')
    if deployment is not None:
        _require(file_hash(p1_source)==sources[str(p1_source)],'M5_SOURCE_CHANGED','P1-0 manifest changed during qualification')
        deployment.update(clock_qualification=result,
            clock_uncertainty_ns=bound if result['physical_qualified'] else None)
        with os.fdopen(os.open(target,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600),'w') as stream:
            json.dump(deployment,stream,allow_nan=False,indent=2);stream.write('\n')
    (destination/'qualification.json').write_text(json.dumps(result,allow_nan=False,indent=2)+'\n')
    columns=('run','direction','line','status','offered_bps','timing_samples','offset_ns','drift_ppb',
             'drift_span_ns','load_error_ns','max_abs_error_ns','clock_uncertainty_ns','missing','reason_codes')
    with (destination/'results.csv').open('x',newline='') as stream:
        writer=csv.DictWriter(stream,fieldnames=columns,extrasaction='ignore');writer.writeheader();writer.writerows(rows)
    (destination/'summary.txt').write_text(f"{scope}: {status}\nClock uncertainty: {bound} ns; limit {LIMIT_NS} ns\n"+
        '\n'.join(f"{row['run']}: {row['status']}" for row in rows)+'\n')
    return result


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('matrix');parser.add_argument('--out',required=True)
    parser.add_argument('--p1-manifest',help='original private hng-p1-0-v1 manifest')
    parser.add_argument('--manifest-out',help='new private manifest; existing files are refused')
    args=parser.parse_args()
    try:result=qualify(args.matrix,args.out,p1_manifest=args.p1_manifest,manifest_out=args.manifest_out)
    except (ValueError,KeyError,TypeError,OSError) as error:
        print(str(error),file=sys.stderr);return 2
    print(json.dumps(result,allow_nan=False))
    return {'PASS':0,'FAIL':1,'INCONCLUSIVE':2}[result['status']]


if __name__=='__main__':sys.exit(main())
