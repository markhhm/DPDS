# SPDX-License-Identifier: Apache-2.0
"""Bulk native-item selection using the exact online.sample SHA-256 key.

Only flow groups enter Python; sequence formatting and SHA-256 rounds operate on
NumPy arrays. Work is chunked independently of the size of the capture.
"""
from functools import lru_cache

import numpy as np

from .reference import canonical, uint


_CHUNK_ROWS = 8192
_INITIAL = np.asarray((
    0x6a09e667, 0xbb67ae85, 0x3c6ef372, 0xa54ff53a,
    0x510e527f, 0x9b05688c, 0x1f83d9ab, 0x5be0cd19), dtype=np.uint32)
_K = np.asarray((
    0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5,
    0x3956c25b, 0x59f111f1, 0x923f82a4, 0xab1c5ed5,
    0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3,
    0x72be5d74, 0x80deb1fe, 0x9bdc06a7, 0xc19bf174,
    0xe49b69c1, 0xefbe4786, 0x0fc19dc6, 0x240ca1cc,
    0x2de92c6f, 0x4a7484aa, 0x5cb0a9dc, 0x76f988da,
    0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7,
    0xc6e00bf3, 0xd5a79147, 0x06ca6351, 0x14292967,
    0x27b70a85, 0x2e1b2138, 0x4d2c6dfc, 0x53380d13,
    0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85,
    0xa2bfe8a1, 0xa81a664b, 0xc24b8b70, 0xc76c51a3,
    0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070,
    0x19a4c116, 0x1e376c08, 0x2748774c, 0x34b0bcb5,
    0x391c0cb3, 0x4ed8aa4a, 0x5b9cca4f, 0x682e6ff3,
    0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208,
    0x90befffa, 0xa4506ceb, 0xbef9a3f7, 0xc67178f2), dtype=np.uint32)


def _ror(word, bits):
    return (word >> bits) | (word << (32 - bits))


def _compress(state, block, schedule=None):
    """Compress one 64-byte block per state column with uint32 arithmetic."""
    if schedule is None:
        schedule = np.empty((64, block.shape[0]), dtype=np.uint32)
    schedule[:16] = block.copy().view('>u4').T
    for index in range(16, 64):
        x, y = schedule[index - 15], schedule[index - 2]
        sigma0 = _ror(x, 7) ^ _ror(x, 18) ^ (x >> 3)
        sigma1 = _ror(y, 17) ^ _ror(y, 19) ^ (y >> 10)
        schedule[index] = schedule[index - 16] + sigma0 + schedule[index - 7] + sigma1
    a, b, c, d, e, f, g, h = state.copy()
    for index in range(64):
        sigma1 = _ror(e, 6) ^ _ror(e, 11) ^ _ror(e, 25)
        choice = (e & f) ^ (~e & g)
        temp1 = h + sigma1 + choice + _K[index] + schedule[index]
        sigma0 = _ror(a, 2) ^ _ror(a, 13) ^ _ror(a, 22)
        majority = (a & b) ^ (a & c) ^ (b & c)
        h, g, f, e, d, c, b, a = g, f, e, d + temp1, c, b, a, temp1 + sigma0 + majority
    return state + np.asarray((a, b, c, d, e, f, g, h))


@lru_cache(maxsize=256)
def _shared_compress(state, block):
    """Cache immutable midstates only for complete common leading data blocks."""
    words = np.asarray(state, dtype=np.uint32)[:, None]
    data = np.frombuffer(block, dtype=np.uint8)[None, :]
    return tuple(_compress(words, data)[:, 0].tolist())


def _sha256_words(messages, lengths):
    """Hash rows of bytes with individual lengths; return eight uint32 words."""
    count = len(lengths)
    if not count:
        return np.empty((0, 8), dtype=np.uint32)
    lengths = np.asarray(lengths, dtype=np.uint64)
    blocks = (lengths + 9 + 63) // 64
    padded = np.zeros((count, int(blocks.max()) * 64), dtype=np.uint8)
    width = int(lengths.max())
    positions = np.arange(width)
    padded[:, :width] = np.where(positions < lengths[:, None], messages[:, :width], 0)
    rows = np.arange(count)
    padded[rows, lengths] = 0x80
    bit_lengths = lengths * np.uint64(8)
    for index in range(8):
        padded[rows, blocks * 64 - 1 - index] = (bit_lengths >> (index * 8)) & 255
    state = _INITIAL[:, None]
    shared = 0
    for block in range(int(lengths.min()) // 64):
        data = padded[:, block * 64:(block + 1) * 64]
        if not np.all(data == data[:1]):
            break
        state = np.asarray(_shared_compress(tuple(state[:, 0].tolist()), data[0].tobytes()), dtype=np.uint32)[:, None]
        shared += 1
    state = np.broadcast_to(state, (8, count)).copy()
    schedule = np.empty((64, count), dtype=np.uint32)
    for block in range(shared, int(blocks.max())):
        next_state = _compress(state, padded[:, block * 64:(block + 1) * 64], schedule)
        state = np.where(block < blocks, next_state, state)
    return state.T


def _group_mask(prefix, sequences, mask_bits):
    """Append exact uint64 decimal identities, then select the digest low bits."""
    count = len(sequences)
    digits = np.empty((count, 20), dtype=np.uint8)
    remainder = sequences.astype(np.uint64, copy=True)
    widths = np.ones(count, dtype=np.uint64)
    for index in range(19, -1, -1):
        digits[:, index] = remainder % 10 + ord('0')
        remainder //= 10
        if index:
            widths += remainder != 0
    lengths = len(prefix) + widths + 2
    messages = np.zeros((count, len(prefix) + 22), dtype=np.uint8)
    messages[:, :len(prefix)] = np.frombuffer(prefix, dtype=np.uint8)
    rows = np.arange(count)
    for index in range(20):
        active = index < widths
        messages[rows[active], len(prefix) + index] = digits[active, 20 - widths[active] + index]
    messages[rows, len(prefix) + widths] = ord('"')
    messages[rows, len(prefix) + widths + 1] = ord(']')
    # The first eight digest bytes are words 0 and 1; mask_bits is at most 10.
    return (_sha256_words(messages, lengths)[:, 1] & ((1 << mask_bits) - 1)) == 0


def sample_mask(run, seed, mask_bits, items):
    """Return the online.sample mask for a one-dimensional native item array.

    Required integer fields: ingress_port, source, destination, source_port,
    destination_port, identity. Optional identity_kind defaults to Stage1 (1);
    software-l2 (2) uses the native adapter's software-l2-<port>:<identity> key.
    """
    uint(seed, 'sampling seed')
    uint(mask_bits, 'sampling mask', 10)
    if not isinstance(run, str):
        raise ValueError('run must be a string')
    if items.ndim != 1 or items.dtype.names is None:
        raise ValueError('items must be a one-dimensional structured array')
    fields = ('ingress_port', 'source', 'destination', 'source_port', 'destination_port')
    limits = (65535, 2**32 - 1, 2**32 - 1, 65535, 65535)
    for name, maximum in zip(fields + ('identity',), limits + (2**64 - 1,)):
        if name not in items.dtype.names or items[name].dtype.kind not in 'ui':
            raise ValueError(f'{name} must be an integer array')
        values = items[name]
        if len(values) and (values.min() < 0 or values.max() > maximum):
            raise ValueError(f'{name} is out of range')
    kinds = items['identity_kind'] if 'identity_kind' in items.dtype.names else None
    if kinds is not None and (kinds.dtype.kind not in 'ui'
                              or len(kinds) and (kinds.min() < 1 or kinds.max() > 2)):
        raise ValueError('identity_kind must be Stage1 (1) or software-l2 (2)')
    result = np.empty(len(items), dtype=bool)
    if not mask_bits:
        result.fill(True)
        return result
    prefix_run = b'hng-g2-timing-v1\0' + seed.to_bytes(8, 'big') + b'[' + canonical(run) + b','
    for start in range(0, len(items), _CHUNK_ROWS):
        chunk = items[start:start + _CHUNK_ROWS]
        chunk_kinds = np.ones(len(chunk), dtype=np.uint8) if kinds is None else kinds[start:start + _CHUNK_ROWS]
        columns = [chunk[name] for name in fields] + [chunk_kinds]
        keys = np.rec.fromarrays(columns, names=fields + ('identity_kind',))
        if all(np.all(values == values[0]) for values in columns):
            groups, inverse = keys[:1], None
        else:
            groups, inverse = np.unique(keys, return_inverse=True)
        for index, group in enumerate(groups):
            selected = np.arange(len(chunk)) if inverse is None else np.flatnonzero(inverse == index)
            port = int(group['ingress_port'])
            if group['identity_kind'] == 2:
                flow = f'software-l2-{port}'
            else:
                flow = (f"{int(group['source']):08x}:{int(group['destination']):08x}:"
                        f"{int(group['source_port'])}:{int(group['destination_port'])}")
            prefix = prefix_run + str(port).encode('ascii') + b',"' + flow.encode('ascii') + b':'
            # Bound the byte matrix as well as row count when run is unusually long.
            batch_size = max(1, min(_CHUNK_ROWS, 8 * 1024 * 1024 // (len(prefix) + 22)))
            for offset in range(0, len(selected), batch_size):
                positions = selected[offset:offset + batch_size]
                result[start + positions] = _group_mask(prefix, chunk['identity'][positions], mask_bits)
    return result
