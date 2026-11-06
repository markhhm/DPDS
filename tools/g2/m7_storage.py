# SPDX-License-Identifier: Apache-2.0
"""Independent exact registered-storage sizing; never allocates a production pool."""
from fractions import Fraction as F
from math import ceil


def plan(reg, model, envelope):
    if not isinstance(reg, dict) or set(reg)-{
        'mode','threshold_l2','hold_ns','window_ns','offered_bps',
        'margin_numerator','margin_denominator','composition'} or not {'mode','composition'} <= reg.keys():
        raise ValueError('invalid M7 storage registration fields')
    p=dict(threshold_l2=1024,hold_ns=10_000_000,window_ns=10_000_000,
           offered_bps=100_000_000_000,margin_numerator=5,margin_denominator=4)
    p.update(reg)
    for name in ('threshold_l2','hold_ns','window_ns','offered_bps','margin_numerator','margin_denominator'):
        if type(p[name]) is not int or not 0 < p[name] < 2**64:
            raise ValueError('invalid M7 unsigned parameter')
    if (p['mode']!='m7-hybrid-v1' or not 64<=p['threshold_l2']<=1522
            or p['window_ns']!=10_000_000 or (p['margin_numerator'],p['margin_denominator'])!=(5,4)
            or model['c_bps']>p['offered_bps']):
        raise ValueError('unsupported M7 mode, threshold, window, margin or offered rate')
    mix=p['composition']
    if not isinstance(mix,list) or not 1<=len(mix)<=16:
        raise ValueError('invalid M7 composition')
    lengths=[];weights=[]
    for entry in mix:
        if not isinstance(entry,dict) or set(entry)!={'l2_bytes','weight'}:
            raise ValueError('invalid M7 class fields')
        l,w=entry['l2_bytes'],entry['weight']
        if (type(l) is not int or type(w) is not int or not 64<=l<=1522 or w<=0
                or l in lengths or not envelope['min_q_bytes']<=l<=envelope['max_q_bytes']):
            raise ValueError('invalid M7 class length/weight')
        lengths.append(l);weights.append(w)
    if sum(weights)>65535:raise ValueError('M7 weights exceed 65535')
    residence=model['t_ns']+model['delta_ns']+p['hold_ns']
    if residence>=2**64:raise ValueError('M7 residence overflow')
    wire=F(model['c_bps']*residence,8_000_000_000);q=model['q_bytes']
    episode=F(p['offered_bps']*p['window_ns'],8_000_000_000)
    bd,bq=min(wire,episode),min(q,episode)
    sl=sum(l*w for l,w in zip(lengths,weights));sw=sum((l+20)*w for l,w in zip(lengths,weights))
    resources=([1 for l in lengths],[int(l<=p['threshold_l2']) for l in lengths],
               [(l-4+63)//64 if l<=p['threshold_l2'] else 0 for l in lengths],
               [int(l>p['threshold_l2']) for l in lengths])
    out={}
    for name,cost in zip(('slot','record','cell','normal'),resources):
        sr=sum(r*w for r,w in zip(cost,weights));aw,al=F(sr,sw),F(sr,sl)
        burst=envelope['burst_bound_packets']*max(cost)
        physical=ceil(F(5,4)*(wire*aw+q*al)+burst)
        window=ceil((wire-bd)*aw+bd*max(F(r,l+20) for r,l in zip(cost,lengths))+
                    (q-bq)*al+bq*max(F(r,l) for r,l in zip(cost,lengths))+burst)
        if physical>=2**32-1:raise ValueError('M7 resource index overflow')
        out[name+'s' if name!='normal' else 'normal_mbufs']=physical
        out[name+'_limit']=min(physical,window)
    return out
