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
        first = direction << 63
        # Share the existing encoder; only its uint64 sequence is changed by the VM.
        payload = encode_probe(first, False)
        packet = (Ether(src=os.environ[prefix + 'SRC_MAC'], dst=os.environ[prefix + 'DST_MAC'])
                  / Dot1Q(vlan=int(os.environ['M7_VLAN']))
                  / IP(src=os.environ[prefix + 'SRC_IP'], dst=os.environ[prefix + 'DST_IP'])
                  / UDP(sport=12000 + direction, dport=9000, chksum=0)
                  / Raw(payload + bytes(1472 - len(payload))))
        assert len(packet) == 1518
        vm = STLScVmRaw([
            STLVmFlowVar(name='sequence', size=8, op='inc', step=1,
                         min_value=first, max_value=first + (1 << 63) - 1,
                         split_to_cores=True),
            STLVmWrFlowVar(fv_name='sequence', pkt_offset=46, is_big=True),
        ])
        return [STLStream(name=f'm7-probe-{direction}',
                          packet=STLPktBuilder(pkt=packet, vm=vm),
                          mode=STLTXCont(bps_L1=100_000_000_000))]


def register():
    return M7Probe()
