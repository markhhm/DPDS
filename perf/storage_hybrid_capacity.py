#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Joint LIVE-byte bound and separately charged fixed prototype allocations.

The joint bound is not an implemented shared backing allocator. Do not use
it as proof that two independently reserved pools fit in physical RAM.
"""
import argparse
from functools import lru_cache
from fractions import Fraction as F
import json
from math import ceil
from pathlib import Path
import subprocess
from storage_capacity import rounded
GIB=2**30


@lru_cache(maxsize=8)
def ratios(descriptor,cell,link,item,mbuf):
    rows={};cw=cq=sw=sq=None;maximum=cellmax=0
    for l2 in range(64,1523):
        cells=(l2-4+cell-1)//cell
        payload=(cell+link)*cells;cost=descriptor+payload+item
        cw=max(cw or (F(0),0),(F(cost,l2+20),l2));cq=max(cq or (F(0),0),(F(cost,l2),l2))
        sw=max(sw or (F(0),0),(F(payload,l2+20),l2));sq=max(sq or (F(0),0),(F(payload,l2),l2))
        maximum=max(maximum,cost);cellmax=max(cellmax,payload)
        normal=(F(mbuf+item,l2+21),l2+1) if l2<1522 else (F(0),0)
        normalq=(F(mbuf+item,l2+1),l2+1) if l2<1522 else (F(0),0)
        rows[l2]=(max(cw,normal),max(cq,normalq),max(maximum,mbuf+item if l2<1522 else 0),sw,sq,cellmax)
    return rows


def plan(c,q,layout,t,hold_ns=10_000_000,page_bytes=GIB,node_gib=None):
    if not 64<=t<=1522 or not 0<hold_ns<2**64:raise ValueError('T=64..1522, positive uint64 H required')
    duration=462_000_000+hold_ns;wire=F(c*duration,8_000_000_000)
    records=q//64+16+ceil(wire/84)
    w,z,maximum,sw,sq,cellmax=ratios(layout['descriptor_bytes'],layout['cell_bytes'],layout['link_bytes'],layout['item_bytes'],layout['mbuf_stride'])[t]
    live=ceil(wire*w[0]+q*z[0]+16*maximum)
    reserve=23840
    normal_records=(q//(t+1)+16+ceil(wire/(t+21))) if t<1522 else 0
    def ring(n):return 8*(1<<n.bit_length())+384
    def pool(n):return ceil(F(n,GIB//layout['mbuf_stride']))*GIB+ring(n)+layout['mempool_header_bytes']
    transport_eal=ceil(F(2*pool(reserve)+39104,GIB))+1
    ring_extra=2*(ring(normal_records+reserve)-ring(reserve))
    core=2*records*layout['core_slot_bytes']
    # Ideal joint storage includes the active tagged item in each packet cost.
    joint=2*rounded(live+layout['control_allocation']+layout['hybrid_control_bytes']+4096,page_bytes)+ring_extra+core+(transport_eal+1)*GIB+GIB//4
    # Actual independent pools reserve every component's maximum concurrently.
    cells=ceil((wire*sw[0]+q*sq[0]+16*cellmax)/(layout['cell_bytes']+layout['link_bytes']))
    compact=layout['control_allocation']+records*layout['descriptor_bytes']+cells*layout['cell_bytes']+rounded(cells*layout['link_bytes'],64)
    fixed_eal=ceil(F(2*pool(normal_records+reserve)+39104,GIB))+1
    fixed=2*rounded(compact+4096,page_bytes)+2*rounded(records*layout['item_bytes']+4096,4096)+core+(fixed_eal+1)*GIB+GIB//4+2*rounded(layout['hybrid_control_bytes'],4096)
    limit=min(64,node_gib/2) if node_gib is not None else 64
    return dict(threshold_l2=t,hold_ns=hold_ns,c_bps=c,q_bytes=q,records_per_direction=records,
                worst_wire_L2=w[1],worst_wire_ratio=str(w[0]),worst_Q_L2=z[1],worst_Q_ratio=str(z[0]),
                joint_live_bytes_per_direction=live,joint_node_gib=joint/GIB,joint_fits_limit=joint<=limit*GIB,
                shared_backing_implemented=False,normal_records_per_direction=normal_records,
                fixed_compact_cells_per_direction=cells,fixed_compact_bytes_per_direction=compact,
                fixed_normal_EAL_gib=fixed_eal,fixed_node_gib=fixed/GIB,physical_fits_limit=fixed<=limit*GIB,
                allowed_gib=limit,node_known=node_gib is not None,page_bytes=page_bytes,
                fixed_core_bytes=core,extra_normal_free_ring_bytes=ring_extra)


def scan(c,q,layout,hold_ns=10_000_000,node_gib=None):
    rows=[plan(c,q,layout,t,hold_ns,node_gib=node_gib) for t in range(64,1523)]
    knee=next(t for t,r in zip(range(64,1523),rows) if r['worst_wire_L2']<=t and r['worst_Q_L2']<=t)
    best=min(rows,key=lambda r:(r['joint_node_gib'],r['threshold_l2']))
    feasible=[r for r in rows if r['joint_fits_limit']]
    return dict(knee_threshold=knee,best_rounded_joint_threshold=best['threshold_l2'],
                first_joint_feasible_threshold=feasible[0]['threshold_l2'] if feasible else None,all_rows=rows)


def selected_thresholds(scans):
    scan90=scans['90000000000']
    values={128,256,512,1024,1521,scan90['first_joint_feasible_threshold'],
            scan90['knee_threshold'],scan90['best_rounded_joint_threshold']}
    return sorted(t for t in values if t is not None)


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('binary',type=Path)
    p.add_argument('--hold-ns',type=int,default=10_000_000);p.add_argument('--node-gib',type=float);p.add_argument('--out',type=Path)
    a=p.parse_args()
    if a.node_gib is not None and not 0<a.node_gib<1e9:p.error('positive finite node size required')
    layout=json.loads(subprocess.check_output([a.binary.resolve(),'--describe'],text=True))
    scans={str(c):scan(c,c*7_456_541//8_000_000_000,layout,a.hold_ns,a.node_gib) for c in [80_000_000_000,90_000_000_000]}
    chosen=selected_thresholds(scans)
    plans=[]
    for c in [80_000_000_000,90_000_000_000]:
        for t in chosen:
            row=plan(c,c*7_456_541//8_000_000_000,layout,t,a.hold_ns,node_gib=a.node_gib);plans.append(row)
            print(f'C={c} T={t} worst_wire_L2={row["worst_wire_L2"]} joint_GiB={row["joint_node_gib"]:.3f} fixed_GiB={row["fixed_node_gib"]:.3f}')
    if a.out:a.out.write_text(json.dumps(dict(layout=layout,registered_hold_ns=a.hold_ns,plans=plans,scans=scans),indent=2)+'\n')
    return 0
if __name__=='__main__':raise SystemExit(main())
