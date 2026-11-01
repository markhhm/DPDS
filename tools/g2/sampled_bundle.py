# SPDX-License-Identifier: Apache-2.0
"""Sampled timing/Q verification; external full-capture order/drop evidence."""
from collections import Counter
import hashlib
import json
from pathlib import Path
import time

from .calibration import Calibration
from .counters import normalize_counters
from .online import OnlineCells, sample
from .protocol import StreamError, StreamReader
from .reference import canonical
from .registration import validate_registration
from .sampled_reference import SampledReference
from .seqcheck import load_checks


def build_sampled_report(config,traces,manifest,sources,calibration,retention_dir=None,registration_root=None):
    validate_registration(manifest,config)
    started=time.perf_counter()
    cells=OnlineCells(manifest)
    clock=Calibration(calibration,manifest['scope'],manifest['run'])
    reference=SampledReference(config,manifest['run'],traces,manifest['timing_mode'])
    selected={};readers={};issues=set();failures=Counter();diagnostics=Counter();timings={}
    limits={};inputs=Counter();sample_counts=Counter();observed={'before':Counter(),'receiver':Counter()};all_reasons=set()
    runtime={}
    cap=manifest['capacity']['timing_sample_capacity']
    lines={line['name']:line for line in config['lines']}

    def issue(code,row=None,fail=False):
        all_reasons.add(code)
        if fail:failures[code]+=1
        if row is None and not fail:issues.add(code)
        if row is not None:cells.violation(row,code,'FAIL' if fail else 'INCONCLUSIVE')

    def bound(port,value):
        if port is None:return
        token=str(port)
        low,high=runtime.get(token,(value,value))
        runtime[token]=(min(low,value),max(high,value))

    def rows(role):
        for registration in manifest['streams']:
            if registration['role']!=role:continue
            reader=StreamReader(sources[registration['stream_id']],role,manifest['run'])
            readers[registration['stream_id']]=reader
            try:
                if reader.header['stream_id']!=registration['stream_id']:
                    raise StreamError('STREAM_REGISTRATION','stream differs from registration')
                if role in ('ingress','host') and reader.header['clock_id']!=manifest['clocks']['engine']:
                    raise StreamError('CLOCK_RUNTIME_DOMAIN','native stream is not in engine time','INCONCLUSIVE')
                for row in reader:yield row
                if reader.integrity['file_sha256']!=registration['file_sha256']:
                    raise StreamError('SOURCE_HASH_MISMATCH','stream differs from registered hash')
            finally:reader._source.close()

    def key(row):return row['ingress_port'],row['external_identity']

    counters=normalize_counters(rows('counters'),manifest)
    issues.update(counters['reason_codes'])
    cost=counters.get('observer_cost',dict(status='INCONCLUSIVE',warnings=[],end_backlog={}))
    for warning in cost.get('warnings',[]):issue(warning['code'])
    mark=time.perf_counter()
    for event in rows('ingress'):
        wanted=False
        cell=None
        if event['kind']=='packet':
            inputs[str(event['ingress_port'])]+=1
            bound(event['ingress_port'],event['arrival_ns'])
            cell=cells._cell(event)
            if cell is not None:
                mask=cell['registration'].get('mask_bits',manifest['sampling']['mask_bits'])
                wanted=sample(event,manifest['sampling']['seed'],mask)
        row=reference.consume(event,wanted)
        if row is None:continue
        token=key(row)
        if token in selected:raise StreamError('DUPLICATE_SAMPLE','sample identity repeats')
        tag=(str(row['ingress_port']),row['line'],cell['registration']['phase'])
        limits[tag]=limits.get(tag,0)+1
        if limits[tag]>cap:raise StreamError('SAMPLE_CAPACITY','selected sample bound exhausted','INCONCLUSIVE')
        selected[token]=dict(expected=row,before=None,receive=None,offer=None,terminal=None)
        sample_counts[str(row['ingress_port'])]+=1
        cells.expect(row)
    summary=reference.finish()
    timings['arrival_prefix_s']=time.perf_counter()-mark
    mark=time.perf_counter()
    for role in ('before','receiver'):
        for observation in rows(role):
            observed[role][str(observation['ingress_port'])]+=1
            state=selected.get(key(observation))
            if state is None:continue
            row=state['expected']
            field='before' if role=='before' else 'receive'
            if state[field] is not None:
                issue('duplicate_sample_'+field,row);continue
            for name in ('egress_port','line','length','vlan_id','flow_id','app'):
                if observation[name]!=row[name]:issue('sample_identity_mapping',row)
            if observation['selected_late'] is not None and observation['selected_late']!=int(row['late']):
                issue('probe_flag_mismatch',row,True)
            state[field]=observation
    for role,field,ordinal,timekey,portkey in (('before','before','before_seq','ingress_ts_ns','ingress_port'),
                                              ('receiver','receive','receive_seq','receive_ns','egress_port')):
        observations=[item[field] for item in selected.values() if item[field] is not None]
        seen=set()
        for observation in sorted(observations,key=lambda row:(row[portkey],row[ordinal])):
            token=(observation[portkey],observation[ordinal])
            if token in seen:issue('sample_observation_ordinal_duplicate')
            seen.add(token)
            try:clock.map_timestamp(observation['timestamp'],observation[timekey],order_key=(role,observation[portkey]))
            except StreamError as error:issue(error.code)
    timings['sample_pair_scan_s']=time.perf_counter()-mark
    mark=time.perf_counter()
    for event in rows('host'):
        diagnostics[event['kind']]+=1
        port=event.get('ingress_port')
        if port is None:
            port=next((p['ingress_port'] for p in manifest['ports'] if p['egress_port']==event.get('egress_port')),None)
        for name in ('at_ns','gate_ns','submit_after_ns'):
            if name in event:bound(port,event[name])
        if event['kind'] not in ('offer','terminal'):continue
        state=selected.get(key(event))
        if state is None:continue
        row=state['expected']
        if event['ingress_seq']!=row['ingress_seq'] or event['line']!=row['line']:
            issue('sample_host_identity_mismatch',row)
        if event['kind']=='offer':
            if state['offer'] is not None:issue('duplicate_sample_offer',row)
            state['offer']=event
            if event['actual_selection_seq']!=row['selection_seq']:
                issue('sample_q_decision_mismatch',row,True)
        else:
            if state['terminal'] is not None:issue('duplicate_sample_terminal',row)
            state['terminal']=event
    timings['host_diagnostic_scan_s']=time.perf_counter()-mark
    expected_reasons=dict(q_drop='q_drop',trace_loss='trace_loss_drop',trace_outage='trace_outage_drop')
    for state in selected.values():
        row,before,received=state['expected'],state['before'],state['receive']
        terminal=state['terminal']
        actual=terminal['reason'] if terminal else None
        expected=expected_reasons.get(row['outcome'])
        if state['offer'] is None:issue('sample_q_decision_unavailable',row)
        if expected is not None and actual!=expected or expected is None and actual in expected_reasons.values():
            issue('sample_q_decision_mismatch',row,True)
        if before is None:issue('missing_sample_before',row)
        if row['outcome']!='accepted':
            if received is not None:cells.delivery(row,0,0)
            continue
        if received is None:
            cells.loss(row,'sample_missing_delivery',False)
            issue('missing_sample_receiver',row)
            continue
        if before is None:continue
        try:
            pair=clock.pair(before,received)
            if pair['target_clock_id']!=manifest['clocks']['observation'][str(row['ingress_port'])]:
                raise StreamError('CLOCK_PAIR_DOMAIN','sample clock differs from registration','INCONCLUSIVE')
            target=None
            if manifest['timing_mode']=='calibration':
                line=lines[row['line']]
                target=row['release_ns']-row['arrival_ns'] if line['trace'] else line['model']['t_ns']+row['late']*line['model']['delta_ns']
            cells.delivery(row,pair['owd_ns'],pair['uncertainty_ns'],target,pair['raw_owd_ns'])
        except StreamError as error:issue(error.code,row)
    for port in (str(p['ingress_port']) for p in manifest['ports']):
        values=counters.get('ports',{}).get(port,{})
        if inputs[port]!=values.get('rx_returned'):issue('counter_ingress_mismatch')
        endpoint=counters.get('endpoints',{}).get('port:'+port,{})
        if port in runtime:
            low,high=runtime[port]
            if (endpoint.get('start_clock_id')!=manifest['clocks']['engine'] or
                    endpoint.get('end_clock_id')!=manifest['clocks']['engine'] or
                    endpoint.get('start_ns') is None or endpoint.get('start_ns')>low or
                    endpoint.get('end_ns') is None or endpoint.get('end_ns')<high):
                issue('counter_window_incomplete')
    for cell in cells.cells.values():
        if cell['registration']['repeat']=='pooled':cell['incomplete'].add('pooled_repeat_evidence_unavailable')
    checks=load_checks(manifest['verification']['seqchecks'],registration_root,manifest,counters)
    for port,check in checks.items():
        if check['totals'] is not None:
            if inputs[port]!=check['totals']['ingress']:issue('seqcheck_ingress_prefix_mismatch')
            for role in ('before','receiver'):
                expected=check['totals']['ingress' if role=='before' else 'receiver']
                if observed[role][port]!=expected:issue('seqcheck_stream_count_mismatch')
    for token,reader in readers.items():
        if reader.header['role']=='counters':continue
        claimed=counters.get('observer',{}).get(token,{})
        if claimed.get('records_emitted')!=reader.integrity.get('record_count'):
            issue('stream_counter_mismatch')
    known={port:{(flow['flow_id'],flow['vlan_id']) for flow in check['flows']} for port,check in checks.items()}
    for item in selected.values():
        row=item['expected'];port=str(row['ingress_port'])
        if checks[port]['totals'] is not None and (row['flow_id'],row['vlan_id']) not in known[port]:
            issue('seqcheck_sample_flow_mismatch',row)
    result=cells.finish()
    reasons=set(issues)|set(failures)|all_reasons
    for row in result['rows']:
        check=checks[row['direction']]
        row['gates'].update(order=check['order'],storage=check['storage'])
        row['full_capture_evidence']=dict(source='hng-seqcheck-v1',scope=check['coverage_scope'])
        row['counts_scope']='sampled-offers'
        row['reason_codes']=sorted(set(row['reason_codes'])|issues|set(check['reason_codes']))
        for name in ('late_extra',*(key for key in row['timing'] if key.startswith('app_extra:'))):
            row['timing'][name]['status']='not_checked'
        incomplete=issues or any(gate['status']=='INCONCLUSIVE' for gate in row['gates'].values()) or row['measurement_status']=='INCONCLUSIVE'
        failed=row['measurement_status']=='FAIL' or any(gate['status']=='FAIL' for gate in row['gates'].values())
        state='INCONCLUSIVE' if incomplete else 'FAIL' if failed else 'PASS'
        row.update(measurement_status=state,status='INVALID' if state=='INCONCLUSIVE' else state,
                   g2_pass=state=='PASS',g2_qualified=False,
                   error_median_ms=None if row['timing']['main']['median_abs_error_ns'] is None else row['timing']['main']['median_abs_error_ns']/1e6,
                   error_p99_ms=None if row['timing']['main']['p99_abs_error_ns'] is None else row['timing']['main']['p99_abs_error_ns']/1e6,
                   delay_storage_drops=check['storage']['value'],capture_drops=None,
                   timing_samples=row['timing']['main']['n'])
        reasons.update(row['reason_codes'])
    required=[row for row in result['rows'] if row['required']]
    state='FAIL' if any(row['measurement_status']=='FAIL' for row in required) else 'INCONCLUSIVE' if any(row['measurement_status']!='PASS' for row in required) else 'PASS'
    if retention_dir is not None:
        with (Path(retention_dir)/'samples.jsonl').open('x') as target:
            for state_row in selected.values():target.write(json.dumps(state_row,separators=(',',':'))+'\n')
    timings['total_s']=time.perf_counter()-started
    return dict(result,schema='hng-g2-report-v1',run=manifest['run'],condition_id=manifest['condition_id'],
        status='INVALID' if state=='INCONCLUSIVE' else state,measurement_status=state,g2_qualified=False,
        reason_codes=sorted(reasons),verification_mode='sampled',sampling=dict(seed=manifest['sampling']['seed'],algorithm=manifest['sampling']['algorithm'],cells=[dict(direction=c['direction'],line=c['line'],phase=c['phase'],mask_bits=c['mask_bits']) for c in manifest['required_cells']]),
        coverage=dict(order='unavailable' if any(c['order']['status']=='INCONCLUSIVE' for c in checks.values()) else 'full-packet via seqcheck',
            storage='unavailable' if any(c['storage']['status']=='INCONCLUSIVE' for c in checks.values()) else 'full-packet via seqcheck + intentional counters',
            timing='sampled',q='sampled',custody='diagnostic',gate_scope='input-run',
            unverified=['unsampled Q decisions','unsampled latency','full custody','hardware qualification']),
        sampled_reference=summary,selected_offers=len(selected),input_packets=dict(inputs),
        custody=dict(mode='diagnostic',event_counts=dict(diagnostics),full_audit_performed=False),
        seqcheck=checks,processing=timings,stream_integrity=[reader.integrity for reader in readers.values()],
        observer_cost=cost,counters=counters,sources=dict(config_sha256=hashlib.sha256(canonical(config)).hexdigest(),
            calibration_document_sha256=hashlib.sha256(canonical(calibration)).hexdigest()),
        registration=manifest,stage1_source_commit=manifest['stage1_source_commit'],stage1_source_review=manifest['stage1_source_review'],
        mismatches=dict(cap=cells.retention.cap,records=[],counts=dict(failures)),
        unavailable=['hardware_qualification','full_custody_audit'])
