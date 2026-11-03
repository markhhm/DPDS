# SPDX-License-Identifier: Apache-2.0
"""Estimate the LP64 C sequence index from registered per-flow bounds; allocate nothing."""


def index_bytes(flows, max_sequence_per_flow):
    for name,value in (('flows',flows),('max_sequence_per_flow',max_sequence_per_flow)):
        if type(value) is not int or not 0 <= value < 2**64:
            raise ValueError(name+' must be a uint64')
    if not flows:
        raise ValueError('flows must be positive')
    pages=max_sequence_per_flow//4096+1
    # The C reader can grow a half-full table again on receiver lookup.
    page_slots=max(16,1 << (2*pages).bit_length())
    flow_slots=max(64,1 << (2*flows).bit_length())
    # seqcheck.c Page is 34312 B; include allocation headers and hash growth space.
    return flows*(pages*(34312+16)+8*page_slots+160)+24*flow_slots+4*page_slots
