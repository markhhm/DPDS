# SPDX-License-Identifier: Apache-2.0
"""Partition complete M6 conditions; select immutable jobs without reading captures."""
import argparse
from collections import Counter
import copy
from fractions import Fraction
import hashlib
import json
import os
from pathlib import Path
import re
import sys

# Software-only warm-cache pipeline fits, rounded to integer ns per processed offer.
SLOPES_NS={'fixed-U':6573,'fixed-X':22680,'trace-U':10530,'trace-X':25440,
           'trace-stress-U':8710,'trace-stress-X':25110}
FIXED_START_NS={'U':260000000,'X':340000000}
TRACE_START_NS=9170000000


def estimate_condition_ns(plan,condition):
    ports={'forward':1,'reverse':1,'both':2}[condition['direction_mode']]
    trace=condition['input_kind']=='trace';mix=condition['traffic']
    lines=condition['lines']
    stress=any(line['offered_bps']>line['c_bps'] for line in lines)
    category=('trace-stress' if trace and stress else 'trace' if trace else 'fixed')+'-'+mix
    sizes=plan['traffic'][mix]['wire_bytes']
    wire=Fraction(sum(sizes),len(sizes))
    duration=Fraction(str(condition['observation_seconds']))+plan['warmup_seconds']
    offers=Fraction(sum(line['offered_bps'] for line in lines))*duration/(8*wire)
    # Each directional report currently reads the entire shared native capture.
    startup=TRACE_START_NS*ports if trace else FIXED_START_NS[mix]
    cost=(offers*ports*SLOPES_NS[category]+startup)*ports*condition['repeats']*condition['episodes_per_repeat']
    if cost<=0:raise ValueError('condition judge cost must be positive')
    return -(-cost.numerator//cost.denominator)


def assign_shards(plan):
    conditions=plan['conditions'];count=len(conditions)
    if not count or count%2:raise ValueError('judge shards require an even positive condition count')
    costs={}
    for condition in conditions:
        name=condition['id']
        if not isinstance(name,str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}',name) or name in costs:
            raise ValueError('judge condition IDs must be safe and unique')
        costs[name]=estimate_condition_ns(plan,condition)
    shards={name:dict(condition_ids=[],estimated_cpu_ns=0) for name in ('a','b')}
    for condition,cost in sorted(costs.items(),key=lambda item:(-item[1],item[0])):
        name=min((name for name in shards if len(shards[name]['condition_ids'])<count//2),
                 key=lambda name:(shards[name]['estimated_cpu_ns'],len(shards[name]['condition_ids']),name))
        shards[name]['condition_ids'].append(condition);shards[name]['estimated_cpu_ns']+=cost
    totals=[shards[name]['estimated_cpu_ns'] for name in ('a','b')]
    if 200*abs(totals[0]-totals[1])>5*sum(totals):
        raise ValueError('estimated judge cost difference exceeds 5% of the mean shard cost')
    for shard in shards.values():shard['condition_ids'].sort()
    return dict(schema='hng-m6-judge-shards-v1',algorithm='cost-descending-count-balanced-v1',
        cost_scope='software-only planning estimate; no platform speed scaling',
        slope_ns_per_offer=SLOPES_NS.copy(),fixed_start_ns=FIXED_START_NS.copy(),
        trace_start_ns_per_profile=TRACE_START_NS,shards=shards)


def select_job(matrix,shard,source_sha256,condition=None):
    plan=matrix['plan'];assignment=assign_shards(plan)
    if plan.get('judge_shards')!=assignment:
        raise ValueError('judge shard registration differs; regenerate the master matrix')
    expected=Counter((c['id'],repeat,episode,port,1-port) for c in plan['conditions']
        for repeat in range(1,c['repeats']+1) for episode in range(1,c['episodes_per_repeat']+1)
        for port in {'forward':[0],'reverse':[1],'both':[0,1]}[c['direction_mode']])
    actual=Counter(tuple(run['extensions']['m6'][key]
        for key in ('condition_id','repeat','episode','ingress_port','egress_port')) for run in matrix['runs'])
    if actual!=expected:
        raise ValueError('run inventory must contain every registered condition/repeat/episode/direction exactly once')
    chosen=set(assignment['shards'][shard]['condition_ids'])
    if condition is not None:
        if condition not in chosen:raise ValueError('condition does not belong to this judge shard')
        chosen={condition}
    job=copy.deepcopy(matrix);job['workers']=1
    selected=[c for c in job['plan']['conditions'] if c['id'] in chosen]
    job['runs']=[r for r in job['runs'] if r['extensions']['m6']['condition_id'] in chosen]
    if not job['runs']:raise ValueError('judge selection has no registered runs')
    job['plan']['conditions']=selected
    for key in ('main','boundary','trace','budget','judge_shards'):job['plan'].pop(key,None)
    job['plan']['inventory']=f'judge shard {shard}: {len(chosen)} conditions; {len(job["runs"])} directional reports'
    job['plan']['judge_selection']=dict(shard=shard,condition_ids=sorted(chosen),
        source_matrix_sha256=source_sha256,source_condition_count=len(plan['conditions']),
        estimated_cpu_ns=sum(estimate_condition_ns(plan,c) for c in selected))
    return job


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('matrix',type=Path);parser.add_argument('--shard',choices=('a','b'),required=True)
    parser.add_argument('--workers',type=int,default=1,help='approved reporter process count for this judge')
    parser.add_argument('--condition',help='one fully closed condition, including all repeats/episodes/directions')
    parser.add_argument('--out',required=True,type=Path)
    args=parser.parse_args()
    try:
        from .matrix import read_matrix
        cpus=len(os.sched_getaffinity(0)) if hasattr(os,'sched_getaffinity') else os.cpu_count() or 1
        if not 1<=args.workers<=cpus:raise ValueError('workers must be positive and within available CPUs')
        raw=args.matrix.read_bytes();matrix=read_matrix(args.matrix)
        if args.matrix.read_bytes()!=raw:raise ValueError('source matrix changed during job selection')
        job=select_job(matrix,args.shard,hashlib.sha256(raw).hexdigest(),args.condition)
        job['workers']=args.workers
        for run in job['runs']:run['manifest']=str((args.matrix.parent/run['manifest']).resolve())
        with args.out.open('x') as out:json.dump(job,out,allow_nan=False,indent=2);out.write('\n')
    except (OSError,ValueError,KeyError,TypeError,ZeroDivisionError) as error:
        print(str(error),file=sys.stderr);return 2
    print(json.dumps(job['plan']['judge_selection'],allow_nan=False));return 0


if __name__=='__main__':sys.exit(main())
