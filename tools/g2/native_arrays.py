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


def _entry_offsets(payload):
    if len(payload) < 2:
        raise ValueError('truncated native binary entry length')
    size, = struct.unpack_from('<H', payload)
    stride = size+2
    if size and len(payload) % stride == 0:
        positions = np.arange(0, len(payload), stride, dtype=np.int64)
        sizes = payload[positions[:, None]+np.arange(2)].view('<u2').reshape(-1)
        if np.all(sizes == size):
            return positions+2, positions+stride
    starts, ends, offset = [], [], 0
    while offset < len(payload):
        if offset+2 > len(payload):
            raise ValueError('truncated native binary entry length')
        size, = struct.unpack_from('<H', payload, offset)
        end = offset+2+size
        if not size or end > len(payload):
            raise ValueError('truncated or empty native binary event')
        starts.append(offset+2)
        ends.append(end)
        offset = end
    return np.asarray(starts, dtype=np.int64), np.asarray(ends, dtype=np.int64)


class _Columns:
    """Advance independent row cursors while decoding a column at a time."""
    def __init__(self, data, starts, ends):
        self.data = data
        self.cursor = np.asarray(starts, dtype=np.int64)
        self.ends = np.asarray(ends, dtype=np.int64)

    def take(self, rows, size):
        # Decoder row selections are unique; a full selection is the whole column.
        full = len(rows) == len(self.cursor)
        positions = self.cursor.copy() if full else self.cursor[rows]
        ends = positions+size
        if np.any(ends > (self.ends if full else self.ends[rows])):
            raise ValueError('truncated native binary event')
        if full:
            self.cursor[:] = ends
        else:
            self.cursor[rows] = ends
        return positions

    def byte(self, rows):
        return self.data[self.take(rows, 1)]

    def integer(self, rows, maximum=(1 << 64)-1):
        tags = self.byte(rows)
        if tags.size and tags[0] >= 128 and np.all(tags == tags[0]):
            width = int(tags[0])-0x7f
            if width > 8:
                raise ValueError('unknown native binary integer tag')
            positions = self.take(rows, width)
            values = self.data[positions[:, None]+np.arange(8)].view('<u8').reshape(-1)
            values &= np.uint64((1 << 64)-1) >> np.uint64((8-width)*8)
            if np.any(values < 128) or (width > 1 and np.any(self.data[positions+width-1] == 0)):
                raise ValueError('noncanonical native binary integer')
        else:
            values = tags.astype(np.uint64)
            wide = np.flatnonzero(tags >= 128)
            if wide.size:
                if np.any(tags[wide] > 0x87):
                    raise ValueError('unknown native binary integer tag')
                widths = tags[wide].astype(np.int64)-0x7f
                positions = self.take(rows[wide], widths)
                decoded = self.data[positions[:, None]+np.arange(8)].view('<u8').reshape(-1)
                decoded &= np.uint64((1 << 64)-1) >> ((8-widths).astype(np.uint64)*np.uint64(8))
                if np.any(decoded < 128) or np.any((widths > 1) & (self.data[positions+widths-1] == 0)):
                    raise ValueError('noncanonical native binary integer')
                values[wide] = decoded
        if maximum != (1 << 64)-1 and np.any(values > maximum):
            raise ValueError('overflowing native binary integer')
        return values

    def sparse(self, rows, maxima, columns, destinations):
        positions = self.take(rows, 4)
        masks = self.data[positions[:, None]+np.arange(4)].copy().view('<u4').reshape(-1)
        any_mask = int(np.bitwise_or.reduce(masks, initial=0))
        all_mask = int(np.bitwise_and.reduce(masks, initial=(1 << len(maxima))-1))
        if any_mask >> len(maxima):
            raise ValueError('unknown native binary presence bit')
        for bit, maximum in enumerate(maxima):
            if not any_mask & (1 << bit):
                continue
            if all_mask & (1 << bit):
                selected = slice(None)
            else:
                selected = np.flatnonzero(masks & (1 << bit))
            column = self.integer(rows[selected], maximum)
            if np.any(column == 0):
                raise ValueError('native binary present value is zero')
            target = columns[bit]
            if isinstance(selected, slice) and len(destinations) == len(target):
                target[:] = column
            else:
                target[destinations[selected]] = column


def _decode(data, starts, ends):
    wire = _Columns(data, starts, ends)
    rows = np.arange(len(starts))
    events = np.zeros(len(rows), dtype=EVENT_DTYPE)
    for name in ('producer', 'kind', 'count', 'reason'):
        events[name] = wire.byte(rows)
    if np.any(events['producer'] > 6) or np.any(events['kind'] > 7) or np.any(events['count'] > 16):
        raise ValueError('native binary producer, kind or item count is out of range')
    events['producer_seq'], events['order'] = wire.integer(rows), wire.integer(rows)
    common_columns = ([events[name] for name in ('at_ns', 'gate_ns', 'submit_before_ns', 'submit_after_ns')]
        + [events['resources'][:, index] for index in range(10)]
        + [events[name] for name in ('actual_selection_seq', 'attempt_id')]
        + [events['consumed_dispatch'][:, index] for index in range(2)]
        + [events[name] for name in ('first_handoff_seq', 'handoff_queue', 'port', 'accepted_count')])
    wire.sparse(rows, COMMON_MAX, common_columns, rows)
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
    item_columns = [items[name] for name in ITEM_FIELDS]
    bases = np.cumsum(counts, dtype=np.int64)-counts
    for index in range(int(counts.max(initial=0))):
        selected = rows[counts > index]
        positions = bases[selected]+index
        wire.sparse(selected, ITEM_MAX, item_columns, positions)
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
        # Eight-byte gathers may cross fields; take() still enforces each entry end.
        data = np.concatenate(buffers+[np.zeros(7, dtype=np.uint8)])
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
                buffers, starts, ends, buffered, pending = [], [], [], 0, 0
                while True:
                    kind, payload = _frame(source, expected, digest)
                    expected += 1
                    if kind == 2:
                        entry_starts, entry_ends = _entry_offsets(payload)
                        offset = 0
                        while offset < len(entry_starts):
                            take = min(self.chunk_records-pending, len(entry_starts)-offset)
                            starts.append(entry_starts[offset:offset+take]+buffered)
                            ends.append(entry_ends[offset:offset+take]+buffered)
                            buffers.append(payload)
                            buffered += len(payload)
                            pending += take
                            offset += take
                            if pending == self.chunk_records:
                                yield self._batch(buffers, np.concatenate(starts), np.concatenate(ends))
                                self._unchanged(source)
                                buffers, starts, ends, buffered, pending = [], [], [], 0, 0
                    elif kind == 3:
                        if pending:
                            yield self._batch(buffers, np.concatenate(starts), np.concatenate(ends))
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
