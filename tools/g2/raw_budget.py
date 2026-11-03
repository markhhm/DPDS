# SPDX-License-Identifier: Apache-2.0
"""Register bounded raw-prefix budgets once per physical acquisition."""
from fractions import Fraction
from pathlib import PurePosixPath

DIRECTIONS={'forward':(0,), 'reverse':(1,), 'both':(0,1)}


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
