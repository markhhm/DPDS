# SPDX-License-Identifier: Apache-2.0
"""Register bounded raw-prefix budgets once per physical acquisition."""
from fractions import Fraction
from pathlib import PurePosixPath
from collections import Counter

DIRECTIONS={'forward':(0,), 'reverse':(1,), 'both':(0,1)}
# Largest artifacts among two software-only million-offer witnesses per class,
# at mask 10. See docs/reports/m6-quarter-rate.md for fit limits/provenance.
SAMPLE_BYTES_PER_MILLION={'fixed-U':1924179,'fixed-X':1873641,
    'trace-U':1909777,'trace-X':1932515,'trace-stress-U':1960268,'trace-stress-X':1905890}
REPORT_BYTES={'fixed-U':61147,'fixed-X':82545,'trace-U':58120,
    'trace-X':79348,'trace-stress-U':61647,'trace-stress-X':83097}


def _positive(value,name):
    if type(value) is not int or value<=0:
        raise ValueError(name+' must be a positive integer')
    return value


def _ceil(value):
    return -(-value.numerator//value.denominator)


def rates(condition,traffic,*,native_bytes_per_offer=316):
    """Bytes/s estimate: per-flow dense identities, native plus both raw points.

    Native density is a conservative measured scenario, not a guaranteed bound.
    The destructive stage separately enforces the actual retained byte budget.
    """
    _positive(native_bytes_per_offer,'native density')
    wire=traffic['wire_bytes'];charges=traffic['q_bytes']
    if not wire or len(wire)!=len(charges):raise ValueError('matching packet/wire profiles are required')
    for value in wire+charges:_positive(value,'packet size')
    if min(charges)<4:raise ValueError('packet charge must include FCS')
    ports=DIRECTIONS[condition['direction_mode']]
    offered=sum(_positive(line['offered_bps'],'offered rate') for line in condition['lines'])
    packets=Fraction(offered*len(ports)*len(wire),8*sum(wire))
    # FCS is stripped; EPB header/trailer is 32 B, packet data is padded to 4 B.
    pcap=Fraction(sum(32+((min(128,q-4)+3)//4)*4 for q in charges),len(charges))
    result=dict(native=_ceil(packets*native_bytes_per_offer),before=_ceil(packets*pcap),receiver=_ceil(packets*pcap))
    result['total']=sum(result.values())
    return result


def register_retention(matrix,*,limit_bytes=2_000_000_000_000,headroom_percent=10,native_bytes_per_offer=316):
    """Attach deterministic quotas/N to each member, counting shared raw once."""
    _positive(limit_bytes,'raw limit');_positive(native_bytes_per_offer,'native density')
    if type(headroom_percent) is not int or not 0<=headroom_percent<100:
        raise ValueError('headroom_percent must be in 0..99')
    plan=matrix['plan']
    if plan['generator']['sequence_policy']!='per-flow-dense':
        raise ValueError('retention budget requires per-flow-dense registration')
    conditions={row['id']:row for row in plan['conditions']};groups={}
    for run in matrix['runs']:
        meta=run['extensions']['m6'];groups.setdefault(meta['acquisition_id'],[]).append(run)
    if not groups:raise ValueError('retention requires acquisitions')
    planned=limit_bytes*(100-headroom_percent)//100;quota=planned//len(groups);reserve=65536
    if quota<=reserve:raise ValueError('raw quota cannot fit capture metadata')
    for aid,members in groups.items():
        first=members[0]['extensions']['m6'];condition=conditions[first['condition_id']]
        expected=set(DIRECTIONS[condition['direction_mode']])
        roots={str(PurePosixPath(run['manifest']).parent.parent) for run in members}
        if (len(members)!=len(expected) or {r['extensions']['m6']['ingress_port'] for r in members}!=expected
                or any(r['extensions']['m6']['condition_id']!=first['condition_id'] for r in members) or len(roots)!=1):
            raise ValueError('inconsistent acquisition members: '+aid)
        rate=rates(condition,plan['traffic'][condition['traffic']],native_bytes_per_offer=native_bytes_per_offer)
        duration=condition['observation_seconds']+plan['warmup_seconds']
        keep=min(duration*10**9,(quota-reserve)*10**9//rate['total'])
        if keep<=0:raise ValueError('raw budget cannot retain a positive time interval')
        estimate=(rate['total']*keep+10**9-1)//10**9+reserve
        entry=dict(keep_ns=keep,keep_seconds=f'{keep//10**9}.{keep%10**9:09d}',byte_budget=quota,
            estimated_bytes=estimate,estimated_bytes_per_second=rate,capture_directory=next(iter(roots)))
        for run in members:run['extensions']['m6']['raw_retention']=dict(entry)
    plan['raw_retention']=dict(schema='hng-m6-raw-retention-v1',limit_bytes=limit_bytes,headroom_percent=headroom_percent,
        planned_bytes=planned,acquisitions=len(groups),native_bytes_per_offer=native_bytes_per_offer,
        metadata_reserve_bytes=reserve,sequence_policy='per-flow-dense',default_action='dry-run',
        scope='diagnostic raw prefixes; complete sealed judgments are preserved')
    return matrix


def estimate_resources(matrix):
    """Planning only; no captures, source parsing, pruning or hardware access."""
    from .judge_shards import estimate_condition_ns
    plan=matrix['plan']
    if plan['timing_mask_bits']!=10:raise ValueError('sample size fit requires mask 10')
    unique=processed=samples=Fraction(0);reports=cpu=0;raw={str(d):0 for d in (188,252,316)}
    classes={}
    for c in plan['conditions']:
        ports=len(DIRECTIONS[c['direction_mode']]);count=c['repeats']*c['episodes_per_repeat']
        duration=c['observation_seconds']+plan['warmup_seconds'];traffic=plan['traffic'][c['traffic']]
        offers=Fraction(sum(l['offered_bps'] for l in c['lines'])*ports*duration*count*len(traffic['wire_bytes']),8*sum(traffic['wire_bytes']))
        work=offers*ports
        stress=c['input_kind']=='trace' and any(l['offered_bps']>l['c_bps'] for l in c['lines'])
        kind=('trace-stress' if stress else c['input_kind'])+'-'+c['traffic']
        unique+=offers;processed+=work;samples+=work*SAMPLE_BYTES_PER_MILLION[kind]/1_000_000
        reports+=ports*count*REPORT_BYTES[kind];cpu+=estimate_condition_ns(plan,c)
        row=classes.setdefault(kind,dict(unique_offers=Fraction(0),processed_offers=Fraction(0)))
        row['unique_offers']+=offers;row['processed_offers']+=work
        for density in raw:
            raw[density]+=rates(c,traffic,native_bytes_per_offer=int(density))['total']*duration*count
    # Source/native files are shared by the two directional reports.
    acquisitions={r['extensions']['m6']['acquisition_id']:r['extensions']['m6']['raw_retention'] for r in matrix['runs']}
    return dict(status='ESTIMATE',rate_divisor=plan['rate_scaling']['divisor'],
        conditions=len(plan['conditions']),acquisitions=len(acquisitions),directional_reports=len(matrix['runs']),
        acquisition_seconds=plan['budget']['acquisition_seconds'],total_with_reserve_seconds=plan['budget']['total_with_reserve_seconds'],
        judge_cpu_ns=cpu,judge_core_hours=cpu/3_600_000_000_000,
        unique_offers=_ceil(unique),processed_offers=_ceil(processed),
        populations={k:{field:_ceil(value) for field,value in row.items()} for k,row in sorted(classes.items())},
        expected_selected_offers=_ceil(processed/1024),estimated_sample_bytes=_ceil(samples),estimated_other_report_bytes=reports,
        sample_bytes_per_million_processed_offers=SAMPLE_BYTES_PER_MILLION.copy(),report_bytes_per_direction=REPORT_BYTES.copy(),
        raw_bytes_by_native_density=raw,retained_raw_bytes=sum(e['estimated_bytes'] for e in acquisitions.values()),
        retained_seconds_acquisitions=dict(sorted(Counter(e['keep_seconds'] for e in acquisitions.values()).items(),key=lambda item:Fraction(item[0]))))
