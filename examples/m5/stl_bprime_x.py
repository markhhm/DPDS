# SPDX-License-Identifier: Apache-2.0
"""Registered B-prime X offer; loading the profile never starts traffic."""
from collections import Counter
from fractions import Fraction
import json
from math import ceil
import os
from pathlib import Path

from trex.stl.api import (
    Ether, Dot1Q, IP, UDP, Raw, STLPktBuilder, STLScVmRaw, STLStream,
    STLTXCont, STLVmFlowVar, STLVmWrFlowVar,
)
from tools.g2.probe import HEADER, encode_probe


ROOT = Path(__file__).resolve().parents[2]
CONDITION = 'fixed-E3-142-320-1-both-L9-X'


class BprimeX:
    def get_streams(self, direction=0, **kwargs):
        if type(direction) is not int or direction not in (0, 1):
            raise ValueError('B-prime requires profile direction 0 or 1')
        matrix = Path(os.environ.get('BPRIME_MATRIX_FILE', ROOT/'examples/m6-x710-fullrate.json'))
        plan = json.loads(matrix.read_text())['plan']
        condition = next(row for row in plan['conditions'] if row['id'] == CONDITION)
        mix = plan['traffic'][condition['traffic']]
        sizes, wires, flows = mix['q_bytes'], mix['wire_bytes'], mix['flows']
        if (mix['equal_packet_counts'] is not True or not sizes or
                any(type(size) is not int or size-4 < 46+HEADER.size for size in sizes) or
                len(set(sizes)) != len(sizes) or wires != [size+20 for size in sizes] or
                type(flows) is not int or not len(sizes) <= flows <= (65536-12000)//2):
            raise ValueError('registered X mix requires supported sizes, equal packet counts and distinct flows')
        vlan, cores = int(os.environ['BPRIME_VLAN']), int(os.environ['BPRIME_CORES'])
        if not 1 <= vlan <= 4094 or not 1 <= cores <= flows:
            raise ValueError('valid VLAN and 1..flow-count TRex cores required')
        config = Path(os.environ.get('BPRIME_CONFIG_FILE', ROOT/f'examples/m5/bprime-{vlan}.json'))
        engine = json.loads(config.read_text())
        lines = [row for row in engine['lines'] if row['ingress_port'] == direction]
        if engine['schema'] != 'hng-v1' or len(lines) != 1:
            raise ValueError('selected engine config must contain the requested direction')
        line = lines[0]
        if (line['egress_port'] != 1-direction or line['vlan_id'] != vlan or
                line['envelope']['min_q_bytes'] != min(sizes) or
                line['envelope']['max_q_bytes'] != max(sizes) or
                Fraction(line['model']['p']) != 1 or line['trace'] is not None):
            raise ValueError('selected VLAN/envelope/all-late fixed condition differs from registered X offer')
        registered = condition['lines'][0]['offered_bps']
        rate = Fraction(os.environ.get('BPRIME_RATE_BPS', str(registered)))
        if not 0 < rate <= registered:
            raise ValueError('offer must be positive and no greater than the registered L1 rate')
        # Equal packet counts, not equal bit rates. Divide each class across its flows.
        class_pps = rate / (8*sum(wires))
        assigned = [sizes[flow % len(sizes)] for flow in range(flows)]
        counts = Counter(assigned)
        duration = Fraction(os.environ.get('BPRIME_DURATION_S', ''))
        if duration <= 0 or any(ceil(class_pps*duration/count) >= (1 << 32)
                                for count in counts.values()):
            raise ValueError('positive BPRIME_DURATION_S with fewer than 2^32 packets per flow required')
        prefix = f'BPRIME_P{direction}_'
        streams = []
        for flow, size in enumerate(assigned):
            stored = size-4  # One VLAN tag included; NIC supplies the FCS.
            payload = encode_probe(0, True)
            payload = payload[:stored-46] + bytes(max(0, stored-46-len(payload)))
            packet = (Ether(src=os.environ[prefix+'SRC_MAC'], dst=os.environ[prefix+'DST_MAC'])
                      / Dot1Q(vlan=vlan)
                      / IP(src=os.environ[prefix+'SRC_IP'], dst=os.environ[prefix+'DST_IP'])
                      / UDP(sport=12000+direction*flows+flow, dport=9000, chksum=0)
                      / Raw(payload))
            if len(packet) != stored:
                raise ValueError('unexpected single-tag IPv4/UDP template length')
            vm = STLScVmRaw([
                # Keep the u64 high word zero; a full u64 VM range overflows TRex.
                STLVmFlowVar(name='sequence', size=4, op='inc', init_value=0,
                             min_value=0, max_value=(1 << 32)-1, step=1, split_to_cores=False),
                STLVmWrFlowVar(fv_name='sequence', pkt_offset=50, is_big=True),
            ], cache_size=0)
            streams.append(STLStream(name=f'bprime-d{direction}-f{flow}',
                packet=STLPktBuilder(pkt=packet, vm=vm), core_id=flow % cores,
                mode=STLTXCont(pps=float(class_pps/counts[size]))))
        return streams


def register():
    return BprimeX()
