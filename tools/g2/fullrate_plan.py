# SPDX-License-Identifier: Apache-2.0
"""Register a small full-rate supplement; never acquire traffic or qualify hardware."""
import argparse
import copy
import hashlib
import json
from pathlib import Path

from .raw_budget import rates

SELECTION=(
    ('fixed-M-CA-65-0-0-both-L9-U','fixed cable baseline; simultaneous bidirectional load'),
    ('fixed-E-CB-120-0-0-forward-L9-U','longer fixed delay; one active direction'),
    ('fixed-E1-142-320-0.10-both-L9-U','rare late selection and long residence; shared host pressure'),
    ('fixed-E3-142-320-1-reverse-L9-U','all-late endpoint and reverse direction'),
    ('trace-single-A-forward-L9-U','registered corridor A; exact trace/clamp prefix'),
    ('trace-single-B-both-L9-U','registered corridor B; asymmetric bidirectional trace prefixes'))
DIRECTIONS={'forward':[0],'reverse':[1],'both':[0,1]}


def build_plan(matrix):
    source={row['id']:row for row in matrix['plan']['conditions']}
    selected=[dict(copy.deepcopy(source[key]),selection_reason=reason) for key,reason in SELECTION]
    root=Path(__file__).resolve().parents[2]
    for action,mode in (('hold','forward'),('drop','reverse')):
        profile=root/f'dpds-core/tests/fixtures/outage-{action}.json'
        selected.append(dict(id=f'outage-{action}-{mode}-U',input_kind='outage',traffic='U',direction_mode=mode,
            selection_reason='registered synthetic outage companion; no outage exists in the 360-condition inventory',
            outage_action=action,source_profile=dict(path='${CORE_ROOT}/tests/fixtures/'+profile.name,
                sha256=hashlib.sha256(profile.read_bytes()).hexdigest()),
            lines=[dict(family='leo',vlan_id=711,t_ns=0,delta_ns=0,p='0',c_bps=9_000_000_000,
                        offered_bps=10_000_000_000,q_bytes=8_388_608,seed=1)]))
    conditions=[];acquisitions=[];windows={};on_seconds=0;native_bytes=0;receiver_bytes=0;proxy_bytes=0
    max_acquisition_raw=0;max_projection=0
    for condition in selected:
        condition_id=condition['id'];lines=condition['lines']
        offered=sum(line['offered_bps'] for line in lines)
        if offered not in (10_000_000_000,2_500_000_000):raise ValueError('unrecognized full/quarter-rate source')
        scale=10_000_000_000//offered
        for line in lines:
            for field in ('c_bps','q_bytes','offered_bps'):line[field]*=scale
            if line['c_bps']!=9_000_000_000 or line['q_bytes']!=8_388_608:
                raise ValueError('supplement requires the original full-rate L9 envelope')
            for key in line.get('trace_windows',{}).values():windows[key]=copy.deepcopy(matrix['plan']['trace_windows'][key])
        trace=condition['input_kind']=='trace';outage=condition['input_kind']=='outage'
        count=10 if outage else 36 if trace else 7;exposure=1 if outage else 9 if trace else 60
        warmup=0 if outage else 2
        condition.update(repeats=count,episodes_per_repeat=1,observation_seconds=exposure,main_mask_bits=8)
        conditions.append(condition)
        # Native density matches the conservative per-flow raw-retention estimate.
        estimate=rates(condition,matrix['plan']['traffic']['U'])
        ports=DIRECTIONS[condition['direction_mode']]
        pcap_ingress_rate=estimate['before']//len(ports)
        receiver_rate=(pcap_ingress_rate*9+9)//10
        for repeat in range(1,count+1):
            ingress=ports[(repeat-1)%len(ports)];duration=warmup+exposure+1+5
            on_seconds+=duration
            native_size=estimate['native']*(warmup+exposure+1)
            receiver_size=receiver_rate*(warmup+exposure)+pcap_ingress_rate
            proxy_size=pcap_ingress_rate*(warmup+exposure)
            native_bytes+=native_size;receiver_bytes+=receiver_size;proxy_bytes+=proxy_size
            max_acquisition_raw=max(max_acquisition_raw,native_size+receiver_size)
            max_projection=max(max_projection,proxy_size)
            for enabled in (True,False):
                aid=f"{condition_id}-c{repeat:02}-{'on' if enabled else 'off'}"
                acquisitions.append(dict(id=aid,condition_id=condition_id,repeat=repeat,observer_enabled=enabled,
                    warmup_seconds=warmup,observation_seconds=exposure,drain_seconds=1,reset_seconds=5,
                    native_directions=ports if enabled else [],mirrored_egress_ports=[1-ingress],
                    captured_ingress_port=ingress,source='${CAPTURE_ROOT}/'+aid+'/fullrate.json' if enabled else None))
    return dict(schema='hng-fullrate-observer-plan-v1',scope='supplemental-native-timing',
        status='ESTIMATE',conditions=conditions,acquisitions=acquisitions,
        traffic=copy.deepcopy(matrix['plan']['traffic']['U']),trace_windows=windows,
        coverage=dict(os4_independent_owd=False,outage='registered synthetic hold and drop companions; outside the 360 conditions',
            native='all RX and accepted-PMD-submit records; sampled timing and Q verdicts',
            external='one egress per acquisition; alternate captured side across bidirectional repeats',
            exclusions=['mixed small-frame profile','full 360-condition full-rate correctness','independent wire OWD']),
        budget=dict(status='ESTIMATE',observer_on_acquisitions=len(acquisitions)//2,
            observer_off_acquisitions=len(acquisitions)//2,observer_on_seconds=on_seconds,
            observer_off_extra_seconds=on_seconds,on_off_seconds=2*on_seconds,reserve_seconds=600,
            on_off_with_reserve_seconds=2*on_seconds+600,
            native_bytes_per_offer=316,pcapng_bytes_per_packet=160,
            native_raw_bytes=native_bytes,receiver_raw_bytes_per_arm=receiver_bytes,
            on_off_raw_bytes=native_bytes+2*receiver_bytes,
            offline_identity_projection_bytes=proxy_bytes,
            largest_acquisition_raw_bytes=max_acquisition_raw,largest_identity_projection_bytes=max_projection,
            shared_storage_peak_bytes_without_pruning=native_bytes+2*receiver_bytes+proxy_bytes,
            capture_disk_minimum_bytes_per_second=800_000_000,
            capture_disk_recommended_bytes_per_second=1_000_000_000,
            model_capacity_bps_per_direction=9_000_000_000,offered_bps_per_direction=10_000_000_000,
            mirrored_physical_peak_bps=10_000_000_000,mirror_peak_headroom_bps=0,
            simultaneous_directions_count_once=True,offline_reporting_included=False))


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--matrix',default='examples/m6-x710.json')
    parser.add_argument('--out',required=True)
    args=parser.parse_args()
    plan=build_plan(json.loads(Path(args.matrix).read_text()))
    rows=plan.pop('conditions');acquisitions=plan.pop('acquisitions')
    text=json.dumps(dict(plan,conditions=[],acquisitions=[]),allow_nan=False,indent=2)
    for name,values in (('conditions',rows),('acquisitions',acquisitions)):
        text=text.replace('"'+name+'": []','"'+name+'": [\n'+',\n'.join(
            '    '+json.dumps(row,allow_nan=False,separators=(',',':')) for row in values)+'\n  ]')
    Path(args.out).write_text(text+'\n')


if __name__=='__main__':main()
