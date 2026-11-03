# SPDX-License-Identifier: Apache-2.0
"""MTU-1500 probe stream for each TRex port; configuration only."""
import os

from trex.stl.api import (
    Ether, Dot1Q, IP, UDP, Raw, STLPktBuilder, STLScVmRaw, STLStream,
    STLTXCont, STLVmFlowVar, STLVmWrFlowVar,
)
from tools.g2.probe import encode_probe


class M7Probe:
    def get_streams(self, direction=0, **kwargs):
        if direction not in (0, 1):
            raise ValueError('M7 example requires one port pair, directions 0 and 1')
        prefix = f'M7_P{direction}_'
        flows = int(os.environ.get('M7_FLOWS', '64'))
        rate = int(os.environ.get('M7_RATE_BPS', '100000000000'))
        cores = os.environ['M7_DATA_CORES'].split(',')
        if (not 1 <= flows <= (65536 - 12000)//2 or rate <= 0 or
                any(not cpu.isdecimal() for cpu in cores) or len(set(cores)) != len(cores)):
            raise ValueError('positive M7_RATE_BPS, distinct M7_DATA_CORES and valid M7_FLOWS are required')
        payload = encode_probe(0, False)
        streams = []
        for flow in range(flows):
            packet = (Ether(src=os.environ[prefix + 'SRC_MAC'], dst=os.environ[prefix + 'DST_MAC'])
                      / Dot1Q(vlan=int(os.environ['M7_VLAN']))
                      / IP(src=os.environ[prefix + 'SRC_IP'], dst=os.environ[prefix + 'DST_IP'])
                      / UDP(sport=12000 + 2*flow + direction, dport=9000, chksum=0)
                      / Raw(payload + bytes(1472 - len(payload))))
            assert len(packet) == 1518
            vm = STLScVmRaw([
                STLVmFlowVar(name='sequence', size=8, op='inc', step=1,
                             init_value=0, min_value=0, max_value=(1 << 64) - 1,
                             split_to_cores=False),
                STLVmWrFlowVar(fv_name='sequence', pkt_offset=46, is_big=True),
            ])
            # One stream/core owns each flow's counter; replicated VMs would duplicate IDs.
            streams.append(STLStream(name=f'm7-probe-{direction}-{flow}', core_id=flow % len(cores),
                packet=STLPktBuilder(pkt=packet, vm=vm), mode=STLTXCont(bps_L1=rate/flows)))
        return streams


def register():
    return M7Probe()
