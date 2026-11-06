#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Independent accounting, no production-sized allocations or engine integration."""
import argparse
from fractions import Fraction
import json
from math import ceil
from pathlib import Path
import subprocess
GIB = 2**30


def rounded(value, page):
    return (value+page-1)//page*page


def plan(c_bps, q_bytes, layout, hold_ns=1_000_000_000, extend_slots=False, node_gib=None, page_bytes=4096):
    delay = 462_000_000
    # Core's original hard slot ceiling remains unless the caller explicitly budgets more.
    records = q_bytes//64+16+ceil(Fraction(c_bps*(delay+(hold_ns if extend_slots else 0)), 8_000_000_000*84))
    unit = layout['cell_bytes']+layout['link_bytes']
    def cost(l2):
        return unit*((l2-4+layout['cell_bytes']-1)//layout['cell_bytes'])
    rw, lw = max((Fraction(cost(l2), l2+20), l2) for l2 in range(64,1523))
    rq, lq = max((Fraction(cost(l2), l2), l2) for l2 in range(64,1523))
    cells = ceil((Fraction(c_bps*(delay+hold_ns),8_000_000_000)*rw+q_bytes*rq+16*cost(1522))/unit)
    arena = (layout['control_allocation']+records*layout['descriptor_bytes']+
             cells*layout['cell_bytes']+rounded(cells*layout['link_bytes'],64))
    # Prototype client keeps generation-bearing handles; do not hide their RAM.
    handles = records*layout['handle_bytes']
    ordinary_arena = 2*rounded(arena+4096,page_bytes) # One allocation, 4KiB allocator/alignment allowance.
    ordinary_handles = 2*rounded(handles+4096,4096)
    core = 2*records*layout['core_slot_bytes']
    # Existing #67 fixed transport reserve; no delay population in these pools.
    reserve = 16384+1024+1024+7*32+4*1024+4*16+1024
    pool = rounded(reserve*2624,GIB)+8*(1<<reserve.bit_length())+384+1056960
    transport_eal = ceil(Fraction(2*pool+39104,GIB))+1
    # 256MiB ordinary allowance and 1GiB observer budget retained from the design spike.
    total = ordinary_arena+ordinary_handles+core+transport_eal*GIB+GIB//4+GIB
    allowed = min(64,node_gib/2) if node_gib is not None else 64
    gate = 'FAIL' if total/GIB > allowed else ('INCONCLUSIVE' if node_gib is None else 'PASS_BUDGET_ONLY')
    return dict(c_bps=c_bps,q_bytes=q_bytes,delay_ns=delay,extra_residence_ns=hold_ns,
                extend_slots=extend_slots,records_per_direction=records,cells_per_direction=cells,
                worst_wire_ratio=str(rw),worst_wire_L2=lw,worst_Q_ratio=str(rq),worst_Q_L2=lq,
                allocator_bytes_per_direction=arena,allocator_page_bytes=page_bytes,
                two_direction_arena_bytes=ordinary_arena,client_handle_bytes=ordinary_handles,
                core_metadata_bytes=core,transport_EAL_gib=transport_eal,
                observer_allowance_bytes=GIB,ordinary_allowance_bytes=GIB//4,
                descriptor_free_list='intrusive; included in descriptor',cell_cache_bytes=0,
                node_budget_bytes=total,node_budget_gib=total/GIB,allowed_gib=allowed,capacity_gate=gate)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('binary',type=Path,help='hng-storage-proto --describe supplies actual layout')
    parser.add_argument('--node-gib',type=float)
    parser.add_argument('--out',type=Path)
    args=parser.parse_args()
    if args.node_gib is not None and not 0 < args.node_gib < 1e9:
        parser.error('node-gib must be positive and finite')
    layout=json.loads(subprocess.check_output([str(args.binary.resolve()),'--describe'],text=True))
    rows=[]
    for c in (80_000_000_000,90_000_000_000):
        q=c*7_456_541//8_000_000_000
        for hold,extended in ((0,False),(1_000_000_000,False),(1_000_000_000,True)):
            for pages in (4096,GIB):
                row=plan(c,q,layout,hold,extended,args.node_gib,pages)
                rows.append(row)
                print(f'C={c} hold_ns={hold} extended_slots={extended} page={pages} '
                      f'node_GiB={row["node_budget_gib"]:.3f} gate={row["capacity_gate"]}')
    if args.out:
        args.out.write_text(json.dumps(dict(layout=layout,plans=rows),indent=2)+'\n')
    return 0


if __name__=='__main__':
    raise SystemExit(main())
