# SPDX-License-Identifier: Apache-2.0
"""64 persistent full-duplex TCP flows, 64 MiB endpoint buffers; configuration only."""
import os

from trex.astf.api import (
    ASTFAssociationRule, ASTFGlobalInfo, ASTFIPGen, ASTFIPGenDist,
    ASTFIPGenGlobal, ASTFProfile, ASTFProgram, ASTFTCPClientTemplate,
    ASTFTCPServerTemplate, ASTFTemplate,
)

FLOWS = 64
WINDOW_BYTES = 64 * 1024 * 1024


class M7LongRtt:
    def get_profile(self, tunables, **kwargs):
        client = ASTFProgram()
        server = ASTFProgram(side='s')
        client.connect()
        server.accept()
        for program in (client, server):
            program.set_send_blocking(False)
            program.set_var('chunks', 4096)
            program.set_label('bulk')
            program.send('M7', size=WINDOW_BYTES, fill='M7')
            program.jmp_nz('chunks', 'bulk')
            program.recv(0xFFFFFFFF)
        ip_gen = ASTFIPGen(
            glob=ASTFIPGenGlobal(ip_offset=os.environ['M7_IP_OFFSET']),
            dist_client=ASTFIPGenDist(ip_range=[os.environ['M7_CLIENT_START'],
                                               os.environ['M7_CLIENT_END']], distribution='seq'),
            dist_server=ASTFIPGenDist(ip_range=[os.environ['M7_SERVER_START'],
                                               os.environ['M7_SERVER_END']], distribution='seq'))
        tcp = ASTFGlobalInfo()
        tcp.tcp.mss = 1460
        tcp.tcp.rxbufsize = WINDOW_BYTES
        tcp.tcp.txbufsize = WINDOW_BYTES
        tcp.tcp.do_rfc1323 = 1
        port = int(os.environ.get('M7_TCP_PORT', '9000'))
        template = ASTFTemplate(
            client_template=ASTFTCPClientTemplate(program=client, ip_gen=ip_gen,
                port=port, cps=FLOWS, limit=FLOWS, cont=True),
            server_template=ASTFTCPServerTemplate(program=server,
                assoc=ASTFAssociationRule(port=port)),
            tg_name='m7-long-rtt-full-duplex')
        return ASTFProfile(default_ip_gen=ip_gen, default_c_glob_info=tcp,
                           default_s_glob_info=tcp, templates=[template])


def register():
    return M7LongRtt()
