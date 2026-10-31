# SPDX-License-Identifier: Apache-2.0
"""Stage 1 probe payload at the Stage 1 harness de035a6 (not second-reviewed)."""
import struct

from .reference import uint

HEADER = struct.Struct("!QIB")
MAGIC = 0x4C30504B


def encode_probe(sequence, selected_late):
    uint(sequence, "probe sequence")
    if type(selected_late) not in (bool, int) or selected_late not in (0, 1):
        raise ValueError("selected-late flag must be 0/1")
    return HEADER.pack(sequence, MAGIC, int(selected_late)) + bytes(87)


def decode_probe(payload):
    if len(payload) < HEADER.size:
        raise ValueError("truncated probe payload")
    sequence, magic, flag = HEADER.unpack_from(payload)
    if magic != MAGIC or flag not in (0, 1):
        raise ValueError("invalid probe magic/selected-late flag")
    return sequence, bool(flag)
