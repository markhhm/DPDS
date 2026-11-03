# SPDX-License-Identifier: Apache-2.0
"""Expand the approved G-short inventory; register future captures, never acquire them."""
import argparse
from collections import Counter
from fractions import Fraction
import hashlib
import itertools
import json
from pathlib import Path

from .raw_budget import register_retention
from .judge_shards import assign_shards

EXAMPLES=Path(__file__).resolve().parents[2]/'examples'
DIRECTIONS={'forward':[0],'reverse':[1],'both':[0,1]}
LOADS={'L0':(10_000_000_000,9_000_000_000),
       'L8':(8_000_000_000,10_000_000_000),'L9':(9_000_000_000,10_000_000_000)}


def exposure(rate,probability):
    # Empty ordinary/late strata use main; Q losses constrain the surviving rate.
    p=Fraction(probability);fraction=min(p,1-p) if 0<p<1 else Fraction(1)
    need=Fraction(431*1024*8*1542,rate)/fraction
    return max(30,-(-need.numerator//need.denominator))


def deployment(rate_divisor=4):
    capture_interfaces=({'0':'${CAPTURE_IF}','1':'${CAPTURE_IF}'} if rate_divisor==4 else
                        {'0':'${CAPTURE_PORT0_IF}','1':'${CAPTURE_PORT1_IF}'})
    return dict(nic=dict(model='X710',pmd='i40e',ports=[0,1]),dut_host='${DUT_HOST}',
        vlans=dict(cable=710,leo=711),
        generator=dict(host='${GENERATOR_HOST}',interfaces={'0':'${GENERATOR_PORT0_IF}','1':'${GENERATOR_PORT1_IF}'}),
        capture=dict(host='${CAPTURE_HOST}',interfaces=capture_interfaces,
            clock_id='${CAPTURE_CLOCK_ID}',mapping_id='${CAPTURE_MAPPING_ID}',clock_source='${CAPTURE_CLOCK_SOURCE}'))


def line(family,t,delta,p,capacity,offered):
    return dict(family=family,vlan_id=710 if family=='cable' else 711,
        t_ns=t,delta_ns=delta,p=p,c_bps=capacity,offered_bps=offered,q_bytes=8_388_608,seed=1)


def fixed_conditions():
    rows=[]
    families=[('M-CA','main','cable',[65],[0],['0'],list(LOADS),['U','X']),
        ('M-CB','main','cable',[100],[0],['0'],list(LOADS),['U','X']),
        ('E0','boundary','leo',[65,142],[0],['0'],['L0','L9'],['U','X']),
        ('E1','boundary','leo',[65,142],[5,320],['0.01','0.10'],['L0','L9'],['U','X']),
        ('E2','boundary','leo',[65,142],[0],['1'],['L0','L9'],['U','X']),
        ('E3','boundary','leo',[65,142],[320],['1'],['L0','L9'],['U','X']),
        ('E-mid','boundary','leo',[65,142],[10,20,40,80,160],['0.01','0.10'],['L0'],['U']),
        ('E-CB','boundary','cable',[80,120],[0],['0'],list(LOADS),['U','X'])]
    for name,family,role,delays,deltas,ps,loads,traffic in families:
        for t,delta,p,mode,load,mix in itertools.product(delays,deltas,ps,DIRECTIONS,loads,traffic):
            capacity,offered=LOADS[load]
            rows.append(dict(id=f'fixed-{name}-{t}-{delta}-{p}-{mode}-{load}-{mix}',family=family,
                input_kind='fixed',subfamily=name,direction_mode=mode,traffic=mix,load=load,
                lines=[line(role,t*1_000_000,delta*1_000_000,p,capacity,offered)],
                repeats=3 if family=='main' else 1,episodes_per_repeat=1,
                observation_seconds=exposure(min(capacity,offered),p)))
    return rows


def generator_registration(plan):
    """Bound one directional capture, including all VLAN groups and warmup offers."""
    profiles={}
    conditions=plan['conditions'] if 'conditions' in plan else plan['trace_conditions']
    for condition in conditions:
        traffic=plan['traffic'][condition['traffic']];flows=traffic['flows']
        wire=Fraction(sum(traffic['wire_bytes']),len(traffic['wire_bytes']))
        duration=Fraction(str(condition.get('observation_seconds',condition.get('steady_seconds'))))+plan['warmup_seconds']
        lines=condition['lines'] if 'lines' in condition else [
            dict(offered_bps=condition['offered_bps_per_line'])]*(2 if condition['trace_kind']=='pair' else 1)
        for item in lines:
            offers=Fraction(item['offered_bps'])*duration/(8*wire)
            packets=-(-offers.numerator//offers.denominator)
            bound=profiles.setdefault(condition['traffic'],dict(flows=0,max_sequence_per_flow=0))
            bound['flows']=max(bound['flows'],flows*len(lines))
            bound['max_sequence_per_flow']=max(bound['max_sequence_per_flow'],(packets+flows-1)//flows-1)
    return dict(profile='examples/m7-traffic/stl_probe.py',sequence_policy='per-flow-dense',
        sequence_start=0,sequence_step=1,seqcheck_memory_limit_bytes=4294967296,
        seqcheck_profiles=profiles)


def mirror_budget(condition):
    """Steady L1 envelope only: model service does not pace physical TX bursts."""
    ports=len(DIRECTIONS[condition['direction_mode']])
    ingress=ports*sum(line['offered_bps'] for line in condition['lines'])
    egress=ports*sum(min(line['offered_bps'],line['c_bps']) for line in condition['lines'])
    return dict(ingress_bps=ingress,egress_bps=egress,total_bps=ingress+egress,
                headroom_bps=10_000_000_000-ingress-egress)


def build_matrix(rate_divisor=4):
    if type(rate_divisor) is not int or rate_divisor not in (1,4):raise ValueError('rate divisor must be 1 or 4')
    base=json.loads((EXAMPLES/'m6-short.json').read_text())
    raw=(EXAMPLES/'m6-trace-conditions.json').read_bytes();source=json.loads(raw)
    plan=base['plan'];plan.update(inventory='360 required conditions: 300 fixed plus 60 trace; captures not acquired',
        deployment=deployment(rate_divisor),conditions=fixed_conditions(),
        rate_scaling=dict(divisor=rate_divisor,scaled_fields=['offered_bps','c_bps','q_bytes'],
            fixed_exposure='recompute with unchanged sampling threshold',trace_exposure='unchanged'),
        repeat_policy=dict(fixed_main=3,fixed_boundary=1,trace=2),
        fixed_exposure_minimum_seconds=30,fixed_exposure_maximum_seconds=70,
        fixed_exposure_formula='max(30,ceil(431*1024*8*1542/(min(C,R)*p_late))); p=0/1 uses main',
        trace_registration=dict(repository_path='examples/m6-trace-conditions.json',sha256=hashlib.sha256(raw).hexdigest()),
        traffic=source['plan']['traffic'],trace_acceptance=source['plan']['acceptance'])
    del plan['fixed_exposure_seconds']
    windows={}
    for key,original in source['plan']['windows'].items():
        windows[key]=dict(original)
        for field in ('profile_path','profile_manifest_path'):
            windows[key][field]='${TRACE_PROFILE_ROOT}/'+Path(original[field]).name
    plan['trace_windows']=windows
    for original in source['plan']['trace_conditions']:
        paired=original['trace_kind']=='pair';mode=original['direction_mode']
        capacity=original['c_bps_per_line'];offered=original['offered_bps_per_line']
        leo=line('leo',0,0,'0',capacity,offered)
        leo['trace_windows']={str(port):original['directions']['ul' if port==0 else 'dl']['window_key']
                              for port in DIRECTIONS[mode]}
        lines=([line('cable',original['cable_t_ns'],0,'0',capacity,offered)] if paired else [])+[leo]
        plan['conditions'].append(dict(id=original['id'],family='boundary' if paired else 'main',
            input_kind='trace',subfamily='E-pair' if paired else 'M-L'+original['corridor'],
            direction_mode=mode,traffic=original['traffic'],lines=lines,
            source_condition_id=original['id'],repeats=original['repeats'],
            episodes_per_repeat=original['episodes_per_repeat'],observation_seconds=9))
    for condition in plan['conditions']:
        for item in condition['lines']:
            for key in ('offered_bps','c_bps','q_bytes'):
                if item[key]%rate_divisor:raise ValueError('rate scaling must be exact')
                item[key]//=rate_divisor
        if condition['input_kind']=='fixed':
            condition['observation_seconds']=max(exposure(min(l['c_bps'],l['offered_bps']),l['p']) for l in condition['lines'])
        # Full-rate conditions retain the exact pre-quarter registration shape.
        if rate_divisor==4:condition['mirror_estimate']=mirror_budget(condition)
    maximum=max(mirror_budget(c)['total_bps'] for c in plan['conditions'])
    if rate_divisor==4 and maximum>10_000_000_000:raise ValueError('quarter-rate steady mirror budget exceeds 10G')
    plan['capture_mirror']=dict(port_bps=10_000_000_000,snaplen_bytes=128,
        sources=['port0-rx','port0-tx','port1-rx','port1-tx'],source_labels_required=True,
        rate_scope='steady offered plus min(offered,service); physical bursts require separate qualification',
        maximum_estimated_bps=maximum,minimum_headroom_bps=10_000_000_000-maximum)
    plan['fixed_exposure_maximum_seconds']=max(70,max(c['observation_seconds'] for c in plan['conditions'] if c['input_kind']=='fixed'))
    runs=[];acquisitions=Counter();seconds=Counter()
    for c in plan['conditions']:
        for repeat,episode in itertools.product(range(1,c['repeats']+1),range(1,c['episodes_per_repeat']+1)):
            aid=f"{c['id']}-r{repeat:02}"+(f'-e{episode:02}' if c['input_kind']=='trace' else '')
            acquisitions[c['input_kind']]+=1;seconds[c['input_kind']]+=c['observation_seconds']+8
            for port in DIRECTIONS[c['direction_mode']]:
                meta=dict(condition_id=c['id'],acquisition_id=aid,repeat=repeat,episode=episode,
                          ingress_port=port,egress_port=1-port)
                runs.append(dict(id=f'{aid}-d{port}',family='trace' if c['input_kind']=='trace' else c['family'],
                    manifest='${CAPTURE_ROOT}/'+aid+f'/d{port}/pair.json',extensions=dict(m6=meta)))
    plan['generator']=generator_registration(plan)
    plan['judge_shards']=assign_shards(plan)
    total=sum(seconds.values())
    plan['budget']=dict(status='ESTIMATE',acquisition_runs=sum(acquisitions.values()),
        fixed_acquisition_runs=acquisitions['fixed'],trace_episode_runs=acquisitions['trace'],
        directional_report_inputs=len(runs),fixed_seconds=seconds['fixed'],trace_seconds=seconds['trace'],
        acquisition_seconds=total,setup_calibration_rerun_reserve_seconds=4*3600,
        total_with_reserve_seconds=total+4*3600,
        acquisition_seconds_if_all_fixed_maximum=acquisitions['fixed']*(plan['fixed_exposure_maximum_seconds']+8)+seconds['trace'],
        simultaneous_directions_count_once=True,offline_reporting_included=False,
        observer_cost_1_in_256_shortening_applied=False)
    base.update(scope='physical',mode='pcapng',workers=1,runs=runs)
    return register_retention(base)


def write_matrix(path,matrix):
    # Keep generated tables reviewable: one explicit condition/run per line.
    plan=dict(matrix['plan']);conditions=plan.pop('conditions')
    doc=dict(matrix,plan=dict(plan,conditions=[]),runs=[])
    raw=json.dumps(doc,indent=2,allow_nan=False)
    raw=raw.replace('    "conditions": []','    "conditions": [\n'+',\n'.join(
        '      '+json.dumps(c,allow_nan=False,separators=(',',':')) for c in conditions)+'\n    ]')
    raw=raw.replace('  "runs": []','  "runs": [\n'+',\n'.join(
        '    '+json.dumps(r,allow_nan=False,separators=(',',':')) for r in matrix['runs'])+'\n  ]')
    Path(path).write_text(raw+'\n')


def engine_config(condition_id,active_vlan,rate_divisor=1):
    """Expand one fixed, single-line condition to both registered port directions."""
    if type(active_vlan) is not int or not 1<=active_vlan<=4094:raise ValueError('active VLAN must be 1..4094')
    # Existing B-prime engine exports remain full-rate unless explicitly scaled.
    matrix=build_matrix(rate_divisor);plan=matrix['plan']
    condition=next((c for c in plan['conditions'] if c['id']==condition_id),None)
    if condition is None or condition['input_kind']!='fixed' or len(condition['lines'])!=1:
        raise ValueError('engine expansion requires a registered fixed single-line condition')
    source=condition['lines'][0];sizes=plan['traffic'][condition['traffic']]['q_bytes']
    low,high=min(sizes),max(sizes);burst=16
    numerator=source['c_bps']*(source['t_ns']+source['delta_ns']);denominator=8000000000*(low+20)
    slots=source['q_bytes']//low+burst+(numerator+denominator-1)//denominator
    model={key:source[key] for key in ('c_bps','q_bytes','t_ns','delta_ns','p','seed')}
    model.update(storage_capacity_packets=slots,storage_capacity_bytes=slots*(high-4))
    return dict(schema='hng-v1',lines=[dict(name=f'fixed-d{port}',ingress_port=port,egress_port=1-port,
        vlan_id=active_vlan,model=model.copy(),envelope=dict(min_q_bytes=low,max_q_bytes=high,
        burst_bound_packets=burst),trace=None) for port in DIRECTIONS[condition['direction_mode']]])


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out',required=True)
    parser.add_argument('--condition',help='expand a fixed single-line condition into an engine config')
    parser.add_argument('--active-vlan',type=int,help='one approved VLAN for the engine config')
    parser.add_argument('--rate-divisor',type=int,choices=(1,4),help='default: matrix 4, engine config 1 (full-rate B-prime)')
    args=parser.parse_args()
    if (args.condition is None)!=(args.active_vlan is None):parser.error('--condition and --active-vlan must be used together')
    divisor=args.rate_divisor if args.rate_divisor is not None else (4 if args.condition is None else 1)
    try:result=build_matrix(divisor) if args.condition is None else engine_config(args.condition,args.active_vlan,divisor)
    except ValueError as error:parser.error(str(error))
    if args.condition is None:write_matrix(args.out,result)
    else:Path(args.out).write_text(json.dumps(result,allow_nan=False,indent=2)+'\n')


if __name__=='__main__':main()
