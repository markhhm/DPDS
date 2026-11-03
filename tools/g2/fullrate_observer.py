# SPDX-License-Identifier: Apache-2.0
"""Judge supplemental full-rate native timing and one externally captured egress.

This is deliberately separate from the independent external-timestamp M6 runner.
The existing G2 registration supplies models, windows and sampling only.
"""
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys

import numpy as np

from .array_sampling import sample_mask
from .counters import normalize_counters
from .matrix import EXIT, SEQCHECK, file_hash
from .native_arrays import NativeArrays
from .native_identity import NativeIdentityWriter
from .native_projection import NativeProjection
from .online import OnlineCells
from .protocol import StreamError, digest, shape
from .reference import load_config, strict_json, uint
from .registration import validate_registration
from .sampled_reference import SampledReference
from .seqcheck import JSON_LIMIT, load_checks


def _json(path):return strict_json(path.read_bytes())
def _key(item):return int(item['ingress_port']),int(item['ingress_seq'])


def _source(root, entry):
    shape(entry,('path','sha256'))
    if not isinstance(entry['path'],str) or not entry['path']:raise ValueError('source path required')
    digest(entry['sha256'])
    path=(root/entry['path']).resolve()
    if file_hash(path)!=entry['sha256']:raise ValueError('registered source hash mismatch')
    return path


def _wanted(items,registration,config):
    result=np.zeros(len(items),dtype=bool)
    names={line['name']:index for index,line in enumerate(config['lines'])}
    windows={window['phase']:window for window in registration['windows']}
    for cell in registration['required_cells']:
        window=windows[cell['phase']]
        indices=np.flatnonzero((items['line']==names[cell['line']])&
            (items['ingress_port']==int(cell['direction']))&
            (items['arrival_ns']>=window['start_ns'])&(items['arrival_ns']<window['end_ns']))
        result[indices]=sample_mask(registration['run'],registration['sampling']['seed'],
            cell.get('mask_bits',registration['sampling']['mask_bits']),items[indices])
    return result


def _native(path,expected_hash,registration,config,traces,port,output):
    reader=NativeArrays(path)
    if registration['scope']=='physical' and reader.header.get('capture_scope')!='physical':
        raise ValueError('physical supplemental evidence requires a physical native capture')
    if (registration['identity_profile'].get('kind')!='probe' or reader.header['identity_policy']!=0
            or reader.header['probe_port']!=registration['identity_profile']['probe_port']):
        raise ValueError('supplemental evidence requires the registered stage1 probe policy')
    projection=NativeProjection(reader.header,registration,config)
    reference=SampledReference(config,registration['run'],traces,registration['timing_mode'])
    cells=OnlineCells(registration);selected={};inputs=Counter()
    capacity=registration['capacity']['timing_sample_capacity']*len(registration['required_cells'])
    writer=NativeIdentityWriter(output/'native-ingress-identities.pcapng')
    try:
        for batch in reader.batches():
            links=projection.accept(batch);events,items=batch['events'],batch['items']
            packets=items[events['kind'][items['event_index']]==0]
            for ingress in np.unique(packets['ingress_port']):
                inputs[str(ingress)]+=int(np.count_nonzero(packets['ingress_port']==ingress))
            writer.write(packets[packets['ingress_port']==port])
            def materialize(index):
                item=packets[index];event=int(item['event_index'])
                return projection.packet(events[event],item,int(links['ingress_seq'][event]))
            mask=_wanted(packets,registration,config)
            rows=list(reference.consume_columns(packets,mask,materialize))
            for row,item in zip(rows,packets[mask]):
                key=_key(item)
                if key in selected:raise ValueError('duplicate selected native identity')
                selected[key]=dict(expected=row,raw=item.copy(),offer=None,terminal=None,submit=None)
                cells.expect(row)
            if len(selected)>capacity:raise ValueError('native sample capacity exhausted')
            for ingress,state in projection.port_state.items():reference.full.ports[ingress].update(state)
        if reader.integrity['sha256']!=expected_hash:raise ValueError('native source hash changed')
        if not reader.health['complete']:raise ValueError('native source is incomplete')
        summary=reference.finish()
        counters=normalize_counters(projection.counter_rows(reader.health,reader.counts,reader.seen),registration)
        end=max((row.get('end_ns') or 0 for row in counters['endpoints'].values()),default=0)
        writer.finish(end)
    finally:writer.close()
    # A second bounded pass resolves evidence that can precede a sampled RX record
    # in the consumer's cross-producer export order. No all-packet Python join.
    second=NativeArrays(path)
    for batch in second.batches():
        events,items=batch['events'],batch['items']
        kinds=events['kind'][items['event_index']]
        starts=np.r_[0,np.cumsum(events['count'],dtype=np.int64)[:-1]]
        positions=np.arange(len(items))-starts[items['event_index']]
        accepted=(kinds==6)&(positions<events['accepted_count'][items['event_index']])
        candidates=items[(kinds==1)|(kinds==2)|accepted]
        for item in candidates[_wanted(candidates,registration,config)]:
            state=selected.get(_key(item))
            if state is None:raise ValueError('selected host evidence lacks native RX identity')
            event=events[int(item['event_index'])];kind=int(event['kind'])
            for name in item.dtype.names:
                if name not in ('event_index','handoff_seq','gate_ns') and item[name]!=state['raw'][name]:
                    raise ValueError('selected host metadata differs from native RX')
            field='offer' if kind==1 else 'terminal' if kind==2 else 'submit'
            if state[field] is not None:raise ValueError('duplicate selected host decision or accepted sample')
            if kind==6:
                before,after=int(event['submit_before_ns']),int(event['submit_after_ns'])
                if before>after or before<int(item['arrival_ns']):raise ValueError('invalid native submit bracket')
                state['submit']=(before,after)
            else:state[field]=projection.host(event,item,int(item['ingress_seq'])+1)
    if second.integrity['sha256']!=expected_hash:raise ValueError('native changed between passes')
    return selected,cells,summary,counters,inputs,reader,projection


def _clock(root,entry,registration,projection):
    if entry is None:return None
    clock=_json(_source(root,entry))
    shape(clock,('schema','run','start_ns','end_ns','pair_error_ns'))
    if clock['schema']!='hng-native-clock-bound-v1' or clock['run']!=registration['run']:
        raise ValueError('clock bound schema/run mismatch')
    for field in ('start_ns','end_ns','pair_error_ns'):uint(clock[field],field)
    low=min((r[0] for r in projection.runtime.values()),default=0)
    high=max((r[1] for r in projection.runtime.values()),default=0)
    if not clock['start_ns']<=low<=high<clock['end_ns']:raise ValueError('clock bound does not cover native runtime')
    return clock['pair_error_ns']


def _judge(selected,cells,config,bound):
    reasons=set();accepted=0;max_bracket=0
    drop=dict(q_drop='q_drop',trace_loss='trace_loss_drop',trace_outage='trace_outage_drop')
    lines={line['name']:line for line in config['lines']}
    for state in selected.values():
        expected=state['expected'];offer,terminal,submit=(state[field] for field in ('offer','terminal','submit'))
        if offer is None:
            cells.violation(expected,'native_q_decision_missing','INCONCLUSIVE');reasons.add('native_q_decision_missing')
        elif offer['actual_selection_seq']!=expected['selection_seq']:
            cells.violation(expected,'native_q_mismatch');reasons.add('native_q_mismatch')
        actual=terminal['reason'] if terminal else None;want=drop.get(expected['outcome'])
        if want!=actual:
            cells.violation(expected,'native_terminal_mismatch');reasons.add('native_terminal_mismatch')
        if expected['selected_late']!=int(expected['late']):
            cells.violation(expected,'native_late_flag_mismatch');reasons.add('native_late_flag_mismatch')
        if expected['outcome']!='accepted':
            if submit is not None:cells.delivery(expected,0,0);reasons.add('native_delivered_intended_drop')
            continue
        if submit is None:
            cells.loss(expected,'native_missing_submit',True);reasons.add('native_missing_submit');continue
        if actual is not None:cells.violation(expected,'native_accepted_terminal_conflict')
        accepted+=1;before,after=submit;max_bracket=max(max_bracket,after-before)
        if bound is None:
            cells.violation(expected,'native_clock_bound_missing','INCONCLUSIVE');continue
        midpoint=(before+after)//2;uncertainty=(after-before+1)//2+bound
        line=lines[expected['line']]
        target=expected['release_ns']-expected['arrival_ns']
        if cells.manifest['timing_mode']=='calibration' and line['trace'] is None:
            target=line['model']['t_ns']+expected['late']*line['model']['delta_ns']
        cells.delivery(expected,midpoint-expected['arrival_ns'],uncertainty,target)
    if bound is None:reasons.add('native_clock_bound_missing')
    return reasons,accepted,max_bracket


def _build(source_path,output,checker):
    source=_json(source_path);shape(source,('schema','registration','native','receiver','clock_bound'))
    if source['schema']!='hng-fullrate-observer-v1':raise ValueError('unsupported fullrate source schema')
    root=source_path.parent
    path=_source(root,source['registration']);registration=_json(path)
    config_path=path.parent/registration['config']['path']
    config_raw=config_path.read_bytes()
    if hashlib.sha256(config_raw).hexdigest()!=registration['config']['sha256']:raise ValueError('engine config hash mismatch')
    config,traces=load_config(config_path,contents=config_raw);validate_registration(registration,config)
    # Reference validates source profiles; no external clock qualification is
    # inferred from the old G2 calibration_path or capture-pair registration.
    reference=SampledReference(config,registration['run'],traces,registration['timing_mode'])
    traces={name:state['trace'] for name,state in reference.full.lines.items() if state['trace'] is not None}
    native=_source(root,source['native'])
    shape(source['receiver'],('path','sha256','ingress_port'))
    port=uint(source['receiver']['ingress_port'],'captured ingress port',65535)
    pairs={row['ingress_port']:row['egress_port'] for row in registration['ports']}
    if port not in pairs:raise ValueError('unknown captured direction')
    receiver=_source(root,{key:source['receiver'][key] for key in ('path','sha256')})
    selected,cells,summary,counters,inputs,reader,projection=_native(native,source['native']['sha256'],registration,config,traces,port,output)
    bound=_clock(root,source['clock_bound'],registration,projection)
    reasons,accepted,max_bracket=_judge(selected,cells,config,bound)
    proxy=output/'native-ingress-identities.pcapng';check_path=output/'seqcheck.json'
    with check_path.open('xb') as stream:
        process=subprocess.run([str(checker),'--ingress',str(proxy),'--receiver',str(receiver),'--details','8'],
            stdout=stream,stderr=subprocess.PIPE)
    if check_path.stat().st_size>JSON_LIMIT:raise ValueError('seqcheck output exceeds JSON limit')
    check=_json(check_path)
    if process.returncode!=EXIT.get(check.get('status')):raise ValueError('seqcheck exit/status mismatch: '+process.stderr.decode(errors='replace')[-4000:])
    if check.get('inputs',{}).get('receiver',{}).get('sha256')!=source['receiver']['sha256']:
        raise ValueError('seqcheck receiver differs from registered capture')
    registration_check=dict(path='seqcheck.json',sha256=file_hash(check_path),ingress_port=port,egress_port=pairs[port],
        ingress_path=proxy.name,receiver_path=str(receiver))
    checks=load_checks([registration_check],output,registration,counters)
    result=cells.finish();cost=counters['observer_cost']
    reasons.update(counters['reason_codes'])
    if cost['status']!='PASS' or cost['warnings']:reasons.add('observer_cost_unavailable')
    for row in result['rows']:
        # These are software statistical gates; no row can qualify M6 timing.
        row['g2_qualified']=False;row['g2_pass']=False
        row['timing_source']='native-rx-to-pmd-submit';row['external_egress_checked']=row['direction']==str(port)
        if row['external_egress_checked']:
            row['gates'].update(order=checks[str(port)]['order'],storage=checks[str(port)]['storage'])
        else:
            row['gates'].update(order=dict(status='NOT_CHECKED'),storage=dict(status='NOT_CHECKED'))
        for name in ('late_extra',*(key for key in row['timing'] if key.startswith('app_extra:'))):
            row['timing'][name]['status']='not_checked'
        states=[row['measurement_status'],*(gate['status'] for gate in row['gates'].values())]
        row['status']=row['measurement_status']='FAIL' if 'FAIL' in states else 'INCONCLUSIVE' if 'INCONCLUSIVE' in states else 'PASS'
    reasons.update(checks[str(port)]['reason_codes'])
    rows=result['rows'];states=[row['status'] for row in rows if row['required']]
    status='FAIL' if 'FAIL' in states else 'INCONCLUSIVE' if reasons or 'INCONCLUSIVE' in states or not states else 'PASS'
    with (output/'samples.jsonl').open('x') as stream:
        for state in selected.values():
            expected=state['expected'];submit=state['submit']
            stream.write(json.dumps(dict(expected=expected,offer=state['offer'],terminal=state['terminal'],
                submit_before_ns=submit[0] if submit else None,submit_after_ns=submit[1] if submit else None),allow_nan=False)+'\n')
    return dict(schema='hng-fullrate-observer-report-v1',run=registration['run'],condition_id=registration['condition_id'],
        scope=registration['scope'],status=status,g2_qualified=False,reason_codes=sorted(reasons),rows=rows,
        coverage=dict(os4_independent_owd=False,timing='sampled native RX to PMD submit',q='sampled decisions; all-arrival exact state',
            ingress_reference='native-rx; not an independent capture',external_egress_ports=[pairs[port]],
            external_order_drop_scope='full identities relative to native RX; intentional drop counts reconciled',
            custody='diagnostic',noninterference='separate paired ON/OFF gate required',pre_rx_loss='not observable here'),
        timing_bound=dict(pair_error_ns=bound,max_submit_bracket_ns=max_bracket,accepted_sample_packets=accepted,
            wire_owd_error_bound_ns=None),sampled_reference=summary,observer_cost=cost,counters=counters,
        input_packets=dict(inputs),seqcheck=checks,registration=registration,sources=source,
        native_integrity=reader.integrity)


def run(source,output,*,seqcheck=None):
    source,output=Path(source).resolve(),Path(output).resolve()
    output.mkdir(parents=True,exist_ok=False)
    try:
        result=_build(source,output,seqcheck or shutil.which('seqcheck') or SEQCHECK)
    except (ValueError,KeyError,TypeError,OSError,OverflowError,IndexError) as error:
        result=dict(schema='hng-fullrate-observer-report-v1',status='INCONCLUSIVE',g2_qualified=False,
            reason_codes=[getattr(error,'code','FULLRATE_INPUT_INVALID')],reason=str(error),rows=[])
    (output/'report.json').write_text(json.dumps(result,allow_nan=False,indent=2)+'\n')
    (output/'summary.txt').write_text(f"Supplemental DUT-timed result: {result['status']}\nIndependent OWD: false; G2 qualified: false\n")
    return result


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source');parser.add_argument('--out',required=True);parser.add_argument('--seqcheck')
    args=parser.parse_args();result=run(args.source,args.out,seqcheck=args.seqcheck)
    print(json.dumps(dict(status=result['status'],g2_qualified=False,reason_codes=result['reason_codes'])))
    return EXIT[result['status']]


if __name__=='__main__':sys.exit(main())
