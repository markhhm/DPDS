# SPDX-License-Identifier: Apache-2.0
"""Recompute diagnostic short-run samples from authenticated retained raw."""
import argparse
from collections import Counter
import json
from pathlib import Path
import shutil
import subprocess
import sys

import numpy as np

from .array_sampling import sample_mask
from .calibration import Calibration
from .matrix import EXIT, SEQCHECK, completed_run, file_hash, read_matrix
from .native_projection import NativeProjection
from .pcapng_arrays import CaptureArrays
from .protocol import StreamError
from .raw_prefix import NativePrefixArrays
from .raw_prune import Retention
from .reference import strict_json
from .report import error_report, timing_gate, timing_stats, write_outputs
from .sampled_reference import SampledReference
from .seqcheck import JSON_LIMIT, ORDER
from .validate import load_registration


def _load(path):return strict_json(path.read_bytes())
def _key(row):return row['ingress_port'],row['external_identity']


def _seqchecks(pairs,raw_manifest,output):
    expected={item['path']:item for item in raw_manifest['files']};results=[]
    for index,(path,pair) in enumerate(pairs.items()):
        paths=[(Path(path).parent/pair[role]['path']).resolve() for role in ('before','receiver')]
        destination=output/f'seqcheck-{index}.json'
        with destination.open('xb') as stream:
            process=subprocess.run([shutil.which('seqcheck') or str(SEQCHECK),'--ingress',str(paths[0]),
                '--receiver',str(paths[1]),'--details','8'],stdout=stream,stderr=subprocess.PIPE)
        if destination.stat().st_size>JSON_LIMIT:raise ValueError('prefix seqcheck exceeds JSON bound')
        value=_load(destination)
        if (value.get('schema')!='hng-seqcheck-v1' or value.get('complete') is not True
                or value.get('status') not in ('PASS','FAIL') or process.returncode!=EXIT[value['status']]):
            raise StreamError('PREFIX_SEQCHECK_INVALID',process.stderr.decode(errors='replace')[-4000:] or
                              str(value.get('error')),'INCONCLUSIVE')
        for name,path in zip(('ingress','receiver'),paths):
            source=value['inputs'][name]
            if source['sha256']!=file_hash(path) or source['byte_size']!=path.stat().st_size or str(path) not in expected:
                raise ValueError('prefix checker did not authenticate the registered captures')
        results.append(value)
    return results


def _capture_samples(pairs,window,manifest,clock,selected):
    counts=Counter();seen={'before':set(),'receiver':set()};uncertainty=0
    for name,pair in pairs.items():
        uncertainty=max(uncertainty,pair['uncertainty_ns'])
        for role in ('before','receiver'):
            interfaces={int(k):v for k,v in pair[role]['interfaces'].items()}
            path=(Path(name).parent/pair[role]['path']).resolve()
            if path in seen[role]:raise ValueError('duplicate prefix capture registration')
            seen[role].add(path)
            reader=CaptureArrays(path,interfaces,window['start_ns'],window['end_ns'],manifest['identity_profile']['probe_port'])
            side='ingress_port' if role=='before' else 'egress_port'
            for batch in reader.batches():
                counts[role]+=len(batch)
                for row in batch:
                    lines=[line for line in manifest['lines'] if line[side]==int(row['port']) and line['vlan_id']==int(row['vlan_id'])]
                    if len(lines)!=1:raise ValueError('ambiguous prefix capture line')
                    line=lines[0]
                    flow=f"{int(row['source']):08x}:{int(row['destination']):08x}:{int(row['source_port'])}:{int(row['destination_port'])}"
                    key=(line['ingress_port'],f"{flow}:{int(row['sequence'])}")
                    if key not in selected:continue
                    stamp=int(row['raw']);ordinal=int(row['ordinal'])
                    item=dict(ingress_port=line['ingress_port'],egress_port=line['egress_port'],line=line['name'],
                        external_identity=key[1],flow_id=flow,length=int(row['length']),vlan_id=int(row['vlan_id']),app=None,
                        selected_late=int(row['selected_late']),timestamp=dict(raw=stamp,clock_id=pair['clock_id'],
                        mapping_id=pair['mapping_id'],point='external_rx',uncertainty_ns=pair['uncertainty_ns']))
                    item.update(dict(before_seq=ordinal,ingress_ts_ns=stamp) if role=='before' else dict(receive_seq=ordinal,receive_ns=stamp))
                    clock.map_timestamp(item['timestamp'],stamp,order_key=(role,line[side]))
                    field='before' if role=='before' else 'receive'
                    if selected[key][field] is not None:raise ValueError('duplicate selected prefix observation')
                    state=selected[key]
                    if any(item[name]!=state['expected'][name] for name in ('egress_port','line','length','vlan_id','flow_id','app')):
                        state.setdefault('metadata_mismatches',[]).append(role)
                    state[field]=item
    return counts,uncertainty


def _build(registration,config,traces,calibration,reader,pairs,raw_manifest,output):
    clock=Calibration(calibration,registration['scope'],registration['run'])
    reference=SampledReference(config,registration['run'],traces,registration['timing_mode'])
    projection=NativeProjection(reader.header,registration,config)
    masks={i:min((cell.get('mask_bits',registration['sampling']['mask_bits']) for cell in registration['required_cells']
                  if cell['line']==line['name'] and cell['direction']==str(line['ingress_port'])),
                 default=registration['sampling']['mask_bits']) for i,line in enumerate(config['lines'])}
    def wanted(items):
        result=np.zeros(len(items),dtype=bool)
        for index,mask in masks.items():
            indices=np.flatnonzero(items['line']==index)
            result[indices]=sample_mask(registration['run'],registration['sampling']['seed'],mask,items[indices])
        return result
    cap=registration['capacity']['timing_sample_capacity']*max(1,len(registration['required_cells']))
    selected={};host=[];packets_count=0
    for batch in reader.batches():
        projection.validate_prefix_batch(batch)
        events,items=batch['events'],batch['items'];kinds=events['kind'][items['event_index']]
        packets=items[kinds==0];packets_count+=len(packets)
        def make_event(index):
            item=packets[index]
            return projection.packet(events[int(item['event_index'])],item,int(item['ingress_seq'])+1)
        try:
            for expected in reference.consume_columns(packets,wanted(packets),make_event):
                key=_key(expected)
                if key in selected:raise ValueError('duplicate selected prefix identity')
                selected[key]=dict(expected=expected,before=None,receive=None,offer=None,terminal=None)
        except (ValueError,KeyError,IndexError) as error:
            raise StreamError('PREFIX_INPUT_NOT_CONTIGUOUS',str(error),'INCONCLUSIVE') from error
        terminals=items[(kinds==1)|(kinds==2)]
        for item in terminals[wanted(terminals)]:
            event=events[int(item['event_index'])]
            host.append(projection.host(event,item,int(item['ingress_seq'])+1))
        if len(selected)>cap or len(host)>2*cap:
            raise StreamError('PREFIX_SAMPLE_CAPACITY','registered sample capacity exhausted','INCONCLUSIVE')
    if reader.health['source_health']['complete'] is not True:
        raise StreamError('PREFIX_SOURCE_INCOMPLETE','original native health was incomplete','INCONCLUSIVE')
    for row in host:
        state=selected.get(_key(row))
        if state is None:continue
        field='offer' if row['kind']=='offer' else 'terminal'
        if state[field] is not None:raise ValueError('duplicate selected prefix host decision')
        if any(row[name]!=state['expected'][name] for name in ('ingress_seq','line')):
            state.setdefault('metadata_mismatches',[]).append('host')
        state[field]=row
    seqchecks=_seqchecks(pairs,raw_manifest,output)
    observed,capture_uncertainty=_capture_samples(pairs,raw_manifest['observation_range'],registration,clock,selected)
    end=raw_manifest['observation_range']['end_ns'];target=raw_manifest['observation_range']['clock_id']
    mappings=[m for m in clock.mappings.values() if m['raw_clock_id']==registration['clocks']['engine'] and m['target_clock_id']==target]
    def pending(expected):
        release=expected['release_ns']
        fits=[m for m in mappings if m['valid_from_raw']<=release<m['valid_to_raw']]
        return len(fits)!=1 or Calibration._mapped(fits[0],release)+fits[0]['residual_bound_ns']+capture_uncertainty>=end
    rows={line['name']:dict(condition_id=registration['condition_id'],repeat=next((c['repeat'] for c in registration['required_cells'] if c['line']==line['name']
        and c['direction']==str(line['ingress_port'])),'diagnostic'),
        direction=str(line['ingress_port']),line=line['name'],phase='retained-prefix',required=False,
        g2_pass=False,g2_qualified=False,timing_samples=0,q_samples=0,q_mismatches=0,pending_q=0,
        pending_receiver=0,unobserved_due_samples=0,unmatched_before=0,metadata_mismatches=0,max_clamp_ns=0,reason_codes=[],errors=[],uncertainty=0)
        for line in config['lines']}
    errors_map=dict(q_drop='q_drop',trace_loss='trace_loss_drop',trace_outage='trace_outage_drop')
    line_models={line['name']:line for line in config['lines']}
    samples=[]
    for state in selected.values():
        expected=state['expected'];row=rows[expected['line']]
        row['max_clamp_ns']=max(row['max_clamp_ns'],expected['clamp_extra_ns'])
        metadata=state.get('metadata_mismatches',[])
        row['metadata_mismatches']+=len(metadata)
        actual=state['terminal']['reason'] if state['terminal'] and 'host' not in metadata else None
        expect_drop=errors_map.get(expected['outcome']);reason=None;error=None
        if state['offer'] is None or 'host' in metadata:
            row['pending_q']+=1
        else:
            row['q_samples']+=1
            mismatch=state['offer']['actual_selection_seq']!=expected['selection_seq']
            if mismatch:row['q_mismatches']+=1
            if expect_drop and actual is None:row['pending_q']+=1
        if actual is not None and actual!=expect_drop:row['q_mismatches']+=1
        before=state['before'] if 'before' not in metadata else None
        receive=state['receive'] if 'receiver' not in metadata else None
        if any(item is not None and item['selected_late']!=int(expected['late']) for item in (before,receive)):
            row['reason_codes'].append('prefix_probe_flag_mismatch')
        if before is None:row['unmatched_before']+=1
        if expect_drop:
            if receive is not None:row['q_mismatches']+=1
        elif receive is None:
            row['pending_receiver']+=1
            if pending(expected):reason='pending_at_cutoff'
            else:row['unobserved_due_samples']+=1;reason='right_censored_due_sample'
        elif before is not None:
            pair=clock.pair(before,receive)
            target_owd=expected['release_ns']-expected['arrival_ns']
            line=line_models[expected['line']]
            if registration['timing_mode']=='calibration' and line['trace'] is None:
                target_owd=line['model']['t_ns']+expected['late']*line['model']['delta_ns']
            error=pair['owd_ns']-target_owd
            row['errors'].append(abs(error));row['uncertainty']=max(row['uncertainty'],pair['uncertainty_ns'])
        samples.append(dict(expected=expected,actual=state,error_ns=error,boundary_reason=reason))
    order_failed=set()
    for pair,check in zip(pairs.values(),seqchecks):
        ports=set(pair['before']['interfaces'].values())
        if len(ports)!=1:raise ValueError('prefix seqcheck requires a directional capture pair')
        ingress=next(iter(ports))
        for flow in check['flows']:
            if not any(flow[name] for name in ORDER):continue
            names=[line['name'] for line in config['lines'] if line['ingress_port']==ingress and line['vlan_id']==flow['vlan_id']]
            if len(names)!=1:raise ValueError('prefix order failure has no unique registered line')
            order_failed.add((str(ingress),names[0]))
    for row in rows.values():
        stats=timing_stats(row.pop('errors'));uncertainty=row.pop('uncertainty');row['timing_samples']=stats['n']
        row['error_median_ms']=None if stats['median_abs_error_ns'] is None else stats['median_abs_error_ns']/1e6
        row['error_p99_ms']=None if stats['p99_abs_error_ns'] is None else stats['p99_abs_error_ns']/1e6
        row['reason_codes']=sorted(set(row['reason_codes']))
        row['timing']=dict(stats=stats,median=timing_gate(stats,'median',100000,uncertainty),p99=timing_gate(stats,'p99',500000,uncertainty))
        if row['q_mismatches']:row['reason_codes'].append('prefix_sample_q_mismatch')
        if (row['direction'],row['line']) in order_failed:row['reason_codes'].append('prefix_order_failure')
        failed=bool(row['reason_codes']) or any(row['timing'][name]['status']=='FAIL' for name in ('median','p99'))
        row['status']=row['measurement_status']='FAIL' if failed else 'INCONCLUSIVE' if (
            row['metadata_mismatches'] or row['unobserved_due_samples'] or row['unmatched_before'] or row['pending_q'] or not row['q_samples'] or
            any(row['timing'][name]['status']!='PASS' for name in ('median','p99'))) else 'PASS'
        if row['metadata_mismatches']:row['reason_codes'].append('prefix_sample_metadata_mismatch')
        if row['unobserved_due_samples']:row['reason_codes'].append('prefix_right_censored_due_sample')
        if row['pending_q']:row['reason_codes'].append('prefix_host_decision_unverified')
        if row['unmatched_before']:row['reason_codes'].append('prefix_before_sample_unavailable')
        for name in ('median','p99'):
            state=row['timing'][name]['status']
            if state!='PASS':row['reason_codes'].append('prefix_timing_'+name+'_'+state.lower())
    status='FAIL' if any(r['status']=='FAIL' for r in rows.values()) else 'INCONCLUSIVE' if any(r['status']!='PASS' for r in rows.values()) else 'PASS'
    report=dict(schema='hng-g2-prefix-report-v1',scope='diagnostic-prefix',registered_scope=registration['scope'],
        verification_mode='prefix-diagnostic',run=registration['run'],status=status,measurement_status=status,
        g2_qualified=False,diagnostic_only=True,reason_codes=['PREFIX_DIAGNOSTIC_ONLY']+
        sorted({code for row in rows.values() for code in row['reason_codes']}),rows=list(rows.values()),
        observation_range=raw_manifest['observation_range'],
        inputs=dict(native_packets=packets_count,**observed),seqchecks=seqchecks,
        reference=dict(prefix_events=reference.prefix,sample_checked=reference.checked,
                       line_counts={name:dict(state['counts']) for name,state in reference.full.lines.items()}),
        sampling=dict(run_domain=registration['run'],seed=registration['sampling']['seed'],
                      selection_policy='densest registered mask per line across the retained prefix',
                      mask_bits_by_line={line['name']:masks[i] for i,line in enumerate(config['lines'])}),
        coverage=dict(order='complete retained captures',storage='unverified without drain; visible terminal drops diagnosed',
                      timing='matched prefix samples',q='sampled decisions with exact retained arrival state',
                      counters='unverified',custody='unverified',full_run='not recomputed'))
    with (output/'samples.jsonl').open('x') as stream:
        for row in samples:stream.write(json.dumps(row,allow_nan=False)+'\n')
    return report


def run_prefix(matrix_path,reports,run_id,output):
    matrix_path=Path(matrix_path).resolve();reports=Path(reports).resolve();output=Path(output).resolve()
    matrix=read_matrix(matrix_path);retention=Retention(matrix_path,matrix,reports)
    if run_id not in retention.by_run:raise ValueError('run has no registered raw prefix')
    aid=retention.by_run[run_id]
    protected=(reports,*(group['root'] for group in retention.groups.values()))
    if any(output==base or output in base.parents or base in output.parents for base in protected):
        raise ValueError('prefix report output must not overlap complete results or captures')
    raw_manifest=retention.manifest(aid);replacements=retention.replacements(run_id)
    if len(replacements)!=len(raw_manifest['files']):raise ValueError('prefix report requires a receipt for every retained raw')
    for index,item in enumerate(raw_manifest['files']):
        receipt=_load(retention.directory(aid)/f'{index}.json')
        if receipt['state']!='APPLIED' or replacements[item['path']][0]!=receipt['retained_sha256']:
            raise ValueError('prefix report requires fully applied raw retention')
    run=next(run for run in matrix['runs'] if run['id']==run_id)
    source=(matrix_path.parent/run['manifest']).resolve()
    if completed_run(reports/run_id,source,run_id,matrix['scope'],'pcapng',matrix['plan'].get('deployment'),replacements) is None:
        raise ValueError('complete parent evidence failed revalidation')
    pair=_load(source);registration_path=(source.parent/pair['manifest']).resolve();registration=_load(registration_path)
    checked,config,traces=load_registration(registration_path)
    calibration=_load(registration_path.parent/registration['calibration_path'])
    paths={(registration_path.parent/name).resolve() for name in registration['verification']['capture_pairs']}
    pairs={str(path):_load(path) for path in sorted(paths)}
    native=(registration_path.parent/registration['verification']['native_binary']['path']).resolve()
    output.mkdir(parents=True,exist_ok=False)
    try:
        result=_build(registration,config,traces,calibration,NativePrefixArrays(native),pairs,raw_manifest,output)
    except (ValueError,KeyError,TypeError,OSError,MemoryError) as error:
        result=error_report(error,registration)
        result.update(schema='hng-g2-prefix-report-v1',scope='diagnostic-prefix',diagnostic_only=True,verification_mode='prefix-diagnostic',g2_qualified=False)
    # Detect modifications during inspection; never overwrite the complete report.
    after=retention.replacements(run_id)
    if after!=replacements:raise ValueError('prefix evidence changed during inspection')
    if completed_run(reports/run_id,source,run_id,matrix['scope'],'pcapng',matrix['plan'].get('deployment'),after) is None:
        raise ValueError('parent evidence changed during prefix inspection')
    result['sources']=dict(checked['sources'],parent_seal_sha256=raw_manifest['members'][run_id]['seal_sha256'],
        raw_manifest_sha256=file_hash(retention.directory(aid)/'manifest.json'),
        prefix_sha256={name:values[0] for name,values in replacements.items()})
    write_outputs(result,output)
    (output/'prefix-manifest.json').write_text(json.dumps(dict(schema='hng-prefix-inspection-v1',
        parent_run=run_id,diagnostic_only=True,g2_qualified=False,observation_range=raw_manifest['observation_range'],
        sources=result['sources']),indent=2)+'\n')
    return result


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('matrix');parser.add_argument('--reports',required=True)
    parser.add_argument('--run',required=True);parser.add_argument('--out',required=True)
    args=parser.parse_args()
    try:
        result=run_prefix(args.matrix,args.reports,args.run,args.out)
        print(json.dumps(dict(status=result['status'],scope=result['scope'],g2_qualified=False)))
        return EXIT[result['status']]
    except (ValueError,KeyError,TypeError,OSError) as error:
        print(str(error),file=sys.stderr);return 2


if __name__=='__main__':sys.exit(main())
