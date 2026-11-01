# SPDX-License-Identifier: Apache-2.0
"""Decode bounded, CRC-protected HNGBIN1 captures to the native JSONL contract."""
import argparse
import binascii
import json
import struct
import sys
from typing import BinaryIO, TextIO

from .protocol import StreamError
from .reference import U64, fields, strict_json, uint

MAGIC = b'HNGBIN1\n'
FRAME = struct.Struct('<BBHIQ')
COMMON_MAX = (U64,)*19 + (65535,)*3
ITEM_MAX = (U64,)*6 + (2**32-1,)*3 + (65535,)*7 + (255,)*3 + (1, 1, U64)
HEALTH_FIELDS = ('attempted', 'emitted', 'dropped', 'capture_drops', 'high_water', 'identity_errors', 'malformed_records')


def _read(source, size):
    chunks = bytearray()
    while len(chunks) < size:
        chunk = source.read(size-len(chunks))
        if not chunk:
            raise ValueError('truncated native binary capture')
        chunks.extend(chunk)
    return bytes(chunks)


class _Entry:
    def __init__(self, data):
        self.data, self.offset = data, 0

    def take(self, size):
        end = self.offset + size
        if end > len(self.data):
            raise ValueError('truncated native binary event')
        result = self.data[self.offset:end]
        self.offset = end
        return result

    def byte(self):
        return self.take(1)[0]

    def integer(self, maximum=U64):
        tag = self.byte()
        if tag < 128:
            value = tag
        else:
            if tag > 0x87:
                raise ValueError('unknown native binary integer tag')
            width = tag - 0x7f
            raw = self.take(width)
            value = int.from_bytes(raw, 'little')
            if value < 128 or (width > 1 and not raw[-1]):
                raise ValueError('noncanonical native binary integer')
        if value > maximum:
            raise ValueError('overflowing native binary integer')
        return value

    def sparse(self, maxima):
        mask, = struct.unpack('<I', self.take(4))
        if mask >> len(maxima):
            raise ValueError('unknown native binary presence bit')
        values = []
        for bit, maximum in enumerate(maxima):
            value = self.integer(maximum) if mask & (1 << bit) else 0
            if mask & (1 << bit) and not value:
                raise ValueError('native binary present value is zero')
            values.append(value)
        return values


def _event(data):
    wire = _Entry(data)
    producer, kind, count, reason = wire.take(4)
    if producer > 6 or kind > 7 or count > 16:
        raise ValueError('native binary producer, kind or item count is out of range')
    seq, order = wire.integer(), wire.integer()
    common = wire.sparse(COMMON_MAX)
    snapshot_count = wire.byte()
    if snapshot_count > 2:
        raise ValueError('native binary snapshot count exceeds two')
    snapshot = []
    for _ in range(snapshot_count):
        port, seen, lower, ended = wire.integer(65535), wire.integer(), wire.integer(), wire.byte()
        if ended > 1:
            raise ValueError('native binary snapshot ended is not boolean')
        snapshot.append([port, seen, lower, bool(ended)])
    items = []
    for _ in range(count):
        item = wire.sparse(ITEM_MAX)
        item[19], item[20] = bool(item[19]), bool(item[20])
        items.append(item)
    if wire.offset != len(data):
        raise ValueError('trailing bytes in native binary event')
    return dict(producer=producer, producer_seq=seq, order=order, kind=kind,
        at_ns=common[0], gate_ns=common[1], submit_before_ns=common[2], submit_after_ns=common[3],
        actual_selection_seq=common[14], attempt_id=common[15], consumed_dispatch=common[16:18],
        first_handoff_seq=common[18], handoff_queue=common[19], port=common[20], accepted_count=common[21],
        reason=reason, snapshot=snapshot, resources=common[4:14], items=items)


def _json(payload):
    if not payload.endswith(b'\n') or payload.count(b'\n') != 1:
        raise ValueError('native binary metadata must be one JSONL line')
    return strict_json(payload.decode('utf-8'))


def _header(payload):
    value = _json(payload)
    fields(value, ('schema', 'identity_policy', 'probe_port', 'capacity', 'record_bytes', 'layout', 'ports'))
    if value['schema'] != 'hng-native-v2':
        raise ValueError('unknown native binary header schema')
    uint(value['identity_policy'], 'identity policy', 1)
    if not uint(value['probe_port'], 'probe port', 65535):
        raise ValueError('native probe port must be positive')
    if not uint(value['capacity'], 'queue capacity', 65536) or not uint(value['record_bytes'], 'record size', 65536):
        raise ValueError('native capacity and record size must be positive')
    ports = value['ports']
    if value['layout'] not in ('shared', 'per-direction') or not isinstance(ports, list) or not 1 <= len(ports) <= 2:
        raise ValueError('invalid native binary layout or ports')
    for port in ports:
        uint(port, 'native port', 65535)
    if len(set(ports)) != len(ports) or value['layout'] == 'per-direction' and len(ports) != 2:
        raise ValueError('invalid native binary layout or ports')
    return value


def _health(payload, header, counts, seen):
    value = _json(payload)
    fields(value, ('kind', 'complete', 'abort_reasons', 'producers'))
    if value['kind'] != 'health' or type(value['complete']) is not bool or not isinstance(value['producers'], list) or len(value['producers']) != 7:
        raise ValueError('invalid native binary health')
    loss = bool(uint(value['abort_reasons'], 'abort reasons', 15))
    for producer, health in enumerate(value['producers']):
        fields(health, HEALTH_FIELDS)
        for key in HEALTH_FIELDS:
            uint(health[key], key)
        if (health['emitted'] != counts[producer]
                or health['attempted'] != health['emitted'] + health['dropped'] + health['capture_drops']
                or health['attempted'] < seen[producer] or health['high_water'] > header['capacity']):
            raise ValueError('native binary health counters disagree with capture')
        loss |= any(health[name] for name in ('dropped', 'capture_drops', 'identity_errors', 'malformed_records'))
    if loss and value['complete']:
        raise ValueError('native binary health claims complete despite loss')
    return value


def decode(source: BinaryIO, destination: TextIO) -> dict:
    """Stream native JSONL; emit a health seal only after validating the entire file.

    Malformed input raises an INCONCLUSIVE StreamError. A valid capture declaring
    loss retains its incomplete native health and returns that health unchanged.
    """
    try:
        if _read(source, len(MAGIC)) != MAGIC:
            raise ValueError('unknown native binary magic or version')
        header, expected_frame, order = None, 0, 0
        counts, seen = [0]*7, [0]*7
        while True:
            raw = _read(source, FRAME.size)
            kind, flags, reserved, length, sequence = FRAME.unpack(raw)
            if flags or reserved or not 0 < length <= 65536-FRAME.size-4 or sequence != expected_frame:
                raise ValueError('invalid native binary frame flags, length or sequence')
            payload = _read(source, length)
            checksum, = struct.unpack('<I', _read(source, 4))
            if checksum != binascii.crc32(payload, binascii.crc32(raw)):
                raise ValueError('native binary CRC mismatch')
            expected_frame += 1
            if header is None:
                if kind != 1:
                    raise ValueError('native binary header must be first')
                header = _header(payload)
            elif kind == 2:
                events = _Entry(payload)
                while events.offset < len(payload):
                    size, = struct.unpack('<H', events.take(2))
                    row = _event(events.take(size))
                    producer = row['producer']
                    if row['order'] != order or row['producer_seq'] <= seen[producer]:
                        raise ValueError('invalid native binary event order or producer sequence')
                    counts[producer] += 1
                    seen[producer] = row['producer_seq']
                    order += 1
                    destination.write(json.dumps(row, separators=(',', ':'))+'\n')
                continue
            elif kind == 3:
                health = _health(payload, header, counts, seen)
                if source.read(1):
                    raise ValueError('data after native binary health seal')
                destination.write(payload.decode('utf-8'))
                return health
            else:
                raise ValueError('duplicate header or unknown native binary frame type')
            destination.write(payload.decode('utf-8'))
    except (ValueError, UnicodeError, struct.error, RecursionError) as error:
        raise StreamError('NATIVE_BINARY_FORMAT', str(error), 'INCONCLUSIVE') from error


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('input', help='native binary capture')
    parser.add_argument('--out', required=True, help='new native JSONL output file')
    args = parser.parse_args(argv)
    try:
        with open(args.input, 'rb') as source, open(args.out, 'x', encoding='utf-8', newline='\n') as destination:
            health = decode(source, destination)
        return 0 if health['complete'] else 2
    except (OSError, StreamError) as error:
        print(f'INCONCLUSIVE: {error}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())
