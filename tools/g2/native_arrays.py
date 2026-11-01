# SPDX-License-Identifier: Apache-2.0
"""Bounded NumPy columns for the variable-length HNGBIN1 wire format.

Only entry boundaries are scanned in Python. Tagged values, sparse fields and
sequence checks operate on arrays; the JSONL decoder remains independent.
"""
import binascii
import hashlib
import os
from pathlib import Path
import stat
import struct

import numpy as np

from .native_adapter import ITEM_FIELDS
from .native_binary import COMMON_MAX, FRAME, ITEM_MAX, MAGIC, _header, _health
from .protocol import StreamError

EVENT_DTYPE = np.dtype([
    ('producer', 'u1'), ('kind', 'u1'), ('count', 'u1'), ('reason', 'u1'),
    ('producer_seq', '<u8'), ('order', '<u8'),
    ('at_ns', '<u8'), ('gate_ns', '<u8'), ('submit_before_ns', '<u8'), ('submit_after_ns', '<u8'),
    ('resources', '<u8', (10,)), ('actual_selection_seq', '<u8'), ('attempt_id', '<u8'),
    ('consumed_dispatch', '<u8', (2,)), ('first_handoff_seq', '<u8'),
    ('handoff_queue', '<u2'), ('port', '<u2'), ('accepted_count', '<u2')])
ITEM_DTYPE = np.dtype([('event_index', '<i8')] + list(zip(ITEM_FIELDS,
    ['<u8']*6 + ['<u4']*3 + ['<u2']*7 + ['u1']*3 + ['?', '?', '<u8'])))
SNAPSHOT_DTYPE = np.dtype([('event_index', '<i8'), ('port', '<u2'),
                           ('seen', '<u8'), ('lower_bound', '<u8'), ('ended', '?')])
ERRORS = (ValueError, OSError, UnicodeError, struct.error, RecursionError, OverflowError)


def _read(source, size):
    result = np.fromfile(source, dtype=np.uint8, count=size)
    if len(result) != size:
        raise ValueError('truncated native binary capture')
    return result


def _frame(source, expected, digest=None):
    raw = _read(source, FRAME.size)
    kind, flags, reserved, length, sequence = FRAME.unpack(raw)
    if flags or reserved or not 0 < length <= 65536-FRAME.size-4 or sequence != expected:
        raise ValueError('invalid native binary frame flags, length or sequence')
    body = _read(source, length+4)
    payload = body[:-4]
    checksum, = struct.unpack('<I', body[-4:])
    if checksum != binascii.crc32(payload, binascii.crc32(raw)):
        raise ValueError('native binary CRC mismatch')
    if digest is not None:
        digest.update(raw)
        digest.update(body)
    return kind, payload


class _Columns:
    """Advance independent row cursors while decoding a column at a time."""
    def __init__(self, data, starts, ends):
        self.data = data
        self.cursor = np.asarray(starts, dtype=np.int64)
        self.ends = np.asarray(ends, dtype=np.int64)

    def take(self, rows, size):
        positions = self.cursor[rows]
        if np.any(positions + size > self.ends[rows]):
            raise ValueError('truncated native binary event')
        self.cursor[rows] = positions + size
        return positions

    def byte(self, rows):
        return self.data[self.take(rows, 1)]

    def integer(self, rows, maximum=(1 << 64)-1):
        tags = self.byte(rows)
        values = tags.astype(np.uint64)
        wide = np.flatnonzero(tags >= 128)
        if wide.size:
            if np.any(tags[wide] > 0x87):
                raise ValueError('unknown native binary integer tag')
            widths = tags[wide].astype(np.int64)-0x7f
            positions = self.take(rows[wide], widths)
            decoded = np.zeros(len(wide), dtype=np.uint64)
            for byte in range(int(widths.max())):
                selected = np.flatnonzero(widths > byte)
                decoded[selected] |= self.data[positions[selected]+byte].astype(np.uint64) << np.uint64(byte*8)
            if np.any(decoded < 128) or np.any((widths > 1) & (self.data[positions+widths-1] == 0)):
                raise ValueError('noncanonical native binary integer')
            values[wide] = decoded
        if maximum != (1 << 64)-1 and np.any(values > maximum):
            raise ValueError('overflowing native binary integer')
        return values

    def sparse(self, rows, maxima):
        positions = self.take(rows, 4)
        masks = self.data[positions[:, None]+np.arange(4)].copy().view('<u4').reshape(-1)
        if np.any(masks >> len(maxima)):
            raise ValueError('unknown native binary presence bit')
        values = np.zeros((len(rows), len(maxima)), dtype=np.uint64)
        for bit, maximum in enumerate(maxima):
            selected = np.flatnonzero(masks & (1 << bit))
            if selected.size:
                column = self.integer(rows[selected], maximum)
                if np.any(column == 0):
                    raise ValueError('native binary present value is zero')
                values[selected, bit] = column
        return values


def _decode(data, starts, ends):
    wire = _Columns(data, starts, ends)
    rows = np.arange(len(starts))
    events = np.zeros(len(rows), dtype=EVENT_DTYPE)
    for name in ('producer', 'kind', 'count', 'reason'):
        events[name] = wire.byte(rows)
    if np.any(events['producer'] > 6) or np.any(events['kind'] > 7) or np.any(events['count'] > 16):
        raise ValueError('native binary producer, kind or item count is out of range')
    events['producer_seq'], events['order'] = wire.integer(rows), wire.integer(rows)
    common = wire.sparse(rows, COMMON_MAX)
    for bit, name in enumerate(('at_ns', 'gate_ns', 'submit_before_ns', 'submit_after_ns')):
        events[name] = common[:, bit]
    events['resources'] = common[:, 4:14]
    events['actual_selection_seq'], events['attempt_id'] = common[:, 14], common[:, 15]
    events['consumed_dispatch'] = common[:, 16:18]
    for bit, name in enumerate(('first_handoff_seq', 'handoff_queue', 'port', 'accepted_count'), 18):
        events[name] = common[:, bit]
    snapshot_counts = wire.byte(rows)
    if np.any(snapshot_counts > 2):
        raise ValueError('native binary snapshot count exceeds two')
    snapshots = np.zeros(int(snapshot_counts.sum()), dtype=SNAPSHOT_DTYPE)
    snapshots['event_index'] = np.repeat(rows, snapshot_counts)
    bases = np.cumsum(snapshot_counts, dtype=np.int64)-snapshot_counts
    for index in range(int(snapshot_counts.max(initial=0))):
        selected = rows[snapshot_counts > index]
        positions = bases[selected]+index
        snapshots['port'][positions] = wire.integer(selected, 65535)
        snapshots['seen'][positions] = wire.integer(selected)
        snapshots['lower_bound'][positions] = wire.integer(selected)
        ended = wire.byte(selected)
        if np.any(ended > 1):
            raise ValueError('native binary snapshot ended is not boolean')
        snapshots['ended'][positions] = ended
    counts = events['count']
    items = np.zeros(int(counts.sum()), dtype=ITEM_DTYPE)
    items['event_index'] = np.repeat(rows, counts)
    bases = np.cumsum(counts, dtype=np.int64)-counts
    for index in range(int(counts.max(initial=0))):
        selected = rows[counts > index]
        positions = bases[selected]+index
        values = wire.sparse(selected, ITEM_MAX)
        for bit, name in enumerate(ITEM_FIELDS):
            items[name][positions] = values[:, bit]
    if np.any(wire.cursor != wire.ends):
        raise ValueError('trailing bytes in native binary event')
    return dict(events=events, items=items, snapshots=snapshots)


def _identity(info):
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


class NativeArrays:
    """Read a regular capture once, yielding bounded structured array batches.

    ``header`` is available immediately. ``health`` and ``integrity`` become
    available only after exhausting ``batches()`` and checking the final EOF.
    No yielded batch alone establishes complete evidence. Event links in item
    and snapshot arrays are local to that batch. Arrays retain native unsigned
    widths, including exact uint64 values above 2**53.
    """
    def __init__(self, path, chunk_records=65536):
        self.path = Path(path)
        self.header = self.health = self.integrity = None
        self.counts, self.seen = [0]*7, [0]*7
        self.chunk_records, self._started, self._order = chunk_records, False, 0
        if type(chunk_records) is not int or not 1 <= chunk_records <= 65536:
            raise ValueError('chunk_records must be in 1..65536')
        try:
            with self.path.open('rb') as source:
                info = os.fstat(source.fileno())
                if not stat.S_ISREG(info.st_mode):
                    raise ValueError('native binary source must be a regular file')
                self._identity = _identity(info)
                if _read(source, len(MAGIC)).tobytes() != MAGIC:
                    raise ValueError('unknown native binary magic or version')
                kind, payload = _frame(source, 0)
                if kind != 1:
                    raise ValueError('native binary header must be first')
                self.header = _header(payload.tobytes())
                self._unchanged(source)
        except ERRORS as error:
            raise StreamError('NATIVE_BINARY_FORMAT', str(error), 'INCONCLUSIVE') from error

    def _unchanged(self, source):
        if _identity(os.fstat(source.fileno())) != self._identity or _identity(self.path.stat()) != self._identity:
            raise ValueError('native binary source changed while reading')

    def _batch(self, buffers, starts, ends):
        data = np.concatenate(buffers) if len(buffers) > 1 else buffers[0]
        batch = _decode(data, starts, ends)
        events = batch['events']
        if np.any(events['order'] != np.arange(self._order, self._order+len(events), dtype=np.uint64)):
            raise ValueError('invalid native binary event order')
        for producer in range(7):
            sequences = events['producer_seq'][events['producer'] == producer]
            if sequences.size:
                if sequences[0] <= self.seen[producer] or np.any(sequences[1:] <= sequences[:-1]):
                    raise ValueError('invalid native binary producer sequence')
                self.counts[producer] += len(sequences)
                self.seen[producer] = int(sequences[-1])
        self._order += len(events)
        return batch

    def batches(self):
        """Yield columns and publish a source hash only after complete validation."""
        if self._started:
            raise StreamError('NATIVE_BINARY_FORMAT', 'native array reader is single-use', 'INCONCLUSIVE')
        self._started = True
        try:
            with self.path.open('rb') as source:
                self._unchanged(source)
                magic = _read(source, len(MAGIC))
                if magic.tobytes() != MAGIC:
                    raise ValueError('unknown native binary magic or version')
                digest = hashlib.sha256(magic)
                kind, payload = _frame(source, 0, digest)
                if kind != 1 or _header(payload.tobytes()) != self.header:
                    raise ValueError('native binary header changed')
                expected = 1
                buffers, starts, ends, buffered = [], [], [], 0
                while True:
                    kind, payload = _frame(source, expected, digest)
                    expected += 1
                    if kind == 2:
                        offset, attached = 0, False
                        while offset < len(payload):
                            if offset+2 > len(payload):
                                raise ValueError('truncated native binary entry length')
                            size, = struct.unpack_from('<H', payload, offset)
                            end = offset+2+size
                            if not size or end > len(payload):
                                raise ValueError('truncated or empty native binary event')
                            if not attached:
                                base = buffered
                                buffers.append(payload)
                                buffered += len(payload)
                                attached = True
                            starts.append(base+offset+2)
                            ends.append(base+end)
                            offset = end
                            if len(starts) == self.chunk_records:
                                yield self._batch(buffers, starts, ends)
                                self._unchanged(source)
                                buffers, starts, ends, buffered = [], [], [], 0
                                attached = False
                    elif kind == 3:
                        if starts:
                            yield self._batch(buffers, starts, ends)
                            self._unchanged(source)
                        health = _health(payload.tobytes(), self.header, self.counts, self.seen)
                        if source.read(1):
                            raise ValueError('data after native binary health seal')
                        self._unchanged(source)
                        self.health = health
                        self.integrity = dict(sha256=digest.hexdigest(), bytes=source.tell())
                        return
                    else:
                        raise ValueError('duplicate header or unknown native binary frame type')
        except ERRORS as error:
            self.health = self.integrity = None
            raise StreamError('NATIVE_BINARY_FORMAT', str(error), 'INCONCLUSIVE') from error
