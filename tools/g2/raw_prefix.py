# SPDX-License-Identifier: Apache-2.0
"""Bounded diagnostic excerpts. Original event/packet identities are preserved."""
import argparse
import binascii
import hashlib
import json
import os
from pathlib import Path
import struct

import numpy as np
from .native_arrays import NativeArrays
from .native_binary import MAGIC, FRAME, _read, _event, _header, _health, _json
from .protocol import StreamError
from .reference import uint


def _line(value):
    return (json.dumps(value, separators=(',', ':'), allow_nan=False)+'\n').encode()


def _frame(destination, kind, payload, sequence):
    raw = FRAME.pack(kind, 0, 0, len(payload), sequence)
    destination.write(raw+payload+struct.pack('<I', binascii.crc32(payload, binascii.crc32(raw))))


def _identity(path):
    state = path.stat()
    return state.st_dev, state.st_ino, state.st_size, state.st_mtime_ns, state.st_ctime_ns


def _hash(path):
    with path.open('rb') as source:
        return hashlib.file_digest(source, 'sha256').hexdigest()


def _bounds(start, end):
    uint(start, 'prefix start'); uint(end, 'prefix end')
    if start >= end:
        raise ValueError('prefix requires start < end')


def native(source, destination, *, start_ns, end_ns, source_sha256, clock_id='engine'):
    """Filter ALL event batches: native records need not be ordered by time."""
    source, destination = Path(source), Path(destination)
    _bounds(start_ns, end_ns)
    original = _identity(source)
    reader = NativeArrays(source, keep_wire=True)
    created = False
    try:
        with destination.open('xb') as output:
            created = True
            output.write(MAGIC)
            header = dict(schema='hng-native-prefix-v1', complete=False, capture_header=reader.header,
                          source_sha256=source_sha256, source_bytes=original[2],
                          clock_id=clock_id, start_ns=start_ns, end_ns=end_ns)
            _frame(output, 1, _line(header), 0)
            sequence, payload, counts = 1, bytearray(), [0]*7
            for batch in reader.batches():
                events, wire = batch['events'], batch['wire']
                selected = np.flatnonzero((events['at_ns'] >= start_ns) & (events['at_ns'] < end_ns))
                for index in selected:
                    entry = wire['data'][int(wire['starts'][index])-2:int(wire['ends'][index])].tobytes()
                    if len(payload)+len(entry) > 65516:
                        _frame(output, 2, payload, sequence); sequence += 1; payload.clear()
                    payload.extend(entry)
                    counts[int(events['producer'][index])] += 1
            if reader.integrity['sha256'] != source_sha256 or _identity(source) != original:
                raise ValueError('original native hash/identity differs from sealed source')
            if payload:
                _frame(output, 2, payload, sequence); sequence += 1
            health = dict(kind='retained-health', complete=False, producers=counts,
                          source_health=reader.health)
            _frame(output, 3, _line(health), sequence)
            output.flush(); os.fsync(output.fileno())
        return dict(records=sum(counts), bytes=destination.stat().st_size, sha256=_hash(destination),
                    source_sha256=source_sha256, source_bytes=original[2], start_ns=start_ns, end_ns=end_ns)
    except BaseException:
        if created:
            destination.unlink(missing_ok=True)
        raise


def _prefix_header(payload):
    try:
        value=_json(payload)
        if value['schema']!='hng-native-prefix-v1' or value['complete'] is not False:
            raise ValueError('not a diagnostic native prefix')
        _bounds(value['start_ns'],value['end_ns'])
        if (not isinstance(value['source_sha256'],str) or len(value['source_sha256'])!=64
                or any(c not in '0123456789abcdef' for c in value['source_sha256'])
                or type(value['source_bytes']) is not int or value['source_bytes']<=0
                or not isinstance(value['clock_id'],str) or not value['clock_id']):
            raise ValueError('invalid native prefix provenance')
        _header(_line(value['capture_header']))
        return value
    except (KeyError,TypeError) as error:
        raise ValueError('invalid diagnostic prefix header') from error


def _prefix_health(payload,header,counts,seen):
    try:
        value=_json(payload)
        if value['kind']!='retained-health' or value['complete'] is not False or value['producers']!=counts:
            raise ValueError('invalid retained health counts')
        original=value['source_health']
        original_counts=[p['emitted'] for p in original['producers']]
        _health(_line(original),header,original_counts,seen)
        if any(counts[p]>original_counts[p] for p in range(7)):
            raise ValueError('retained counts exceed original health')
        return value
    except (KeyError,TypeError,IndexError) as error:
        raise ValueError('invalid diagnostic prefix health') from error


class NativePrefixArrays(NativeArrays):
    """Explicit bounded diagnostic columns. Full NativeArrays remains strict."""
    def __init__(self,*args,**kwargs):
        self.prefix_header=None
        self._previous_order=-1
        super().__init__(*args,**kwargs)

    def _capture_header(self,payload):
        value=_prefix_header(payload)
        if self.prefix_header is not None and value!=self.prefix_header:
            raise ValueError('native prefix metadata changed')
        self.prefix_header=value
        return value['capture_header']

    def _capture_health(self,payload):
        return _prefix_health(payload,self.header,self.counts,self.seen)

    def _validate_order(self,events):
        if len(events) and (int(events['order'][0])<=self._previous_order
                or np.any(events['order'][1:]<=events['order'][:-1])
                or np.any(events['at_ns']<self.prefix_header['start_ns'])
                or np.any(events['at_ns']>=self.prefix_header['end_ns'])):
            raise ValueError('invalid diagnostic event order/time range')
        if len(events):self._previous_order=int(events['order'][-1])


def decode_native(source, destination):
    """Explicit diagnostic reader; never returns a complete-capture health seal."""
    try:
        if _read(source, len(MAGIC)) != MAGIC:
            raise ValueError('invalid prefix magic')
        header, sequence, last = None, 0, -1
        counts, seen = [0]*7, [0]*7
        while True:
            raw = _read(source, FRAME.size)
            kind, flags, reserved, length, seq = FRAME.unpack(raw)
            if flags or reserved or seq != sequence or not 0 < length <= 65516:
                raise ValueError('invalid prefix frame')
            payload = _read(source, length)
            if struct.unpack('<I', _read(source, 4))[0] != binascii.crc32(payload, binascii.crc32(raw)):
                raise ValueError('prefix CRC mismatch')
            sequence += 1
            if header is None:
                header = _prefix_header(payload)
                if kind != 1 or header.get('schema') != 'hng-native-prefix-v1' or header.get('complete') is not False:
                    raise ValueError('not a diagnostic native prefix')
                _header(_line(header['capture_header']))
                _bounds(header['start_ns'], header['end_ns'])
                destination.write(payload.decode())
            elif kind == 2:
                offset = 0
                while offset < len(payload):
                    size = struct.unpack_from('<H', payload, offset)[0]; offset += 2
                    row = _event(payload[offset:offset+size]); offset += size
                    producer = row['producer']
                    if (offset > len(payload) or row['order'] <= last or row['producer_seq'] <= seen[producer]
                            or not header['start_ns'] <= row['at_ns'] < header['end_ns']):
                        raise ValueError('invalid retained event range/order')
                    last, seen[producer] = row['order'], row['producer_seq']; counts[producer] += 1
                    destination.write(_line(row).decode())
            elif kind == 3:
                value = _prefix_health(payload,header['capture_header'],counts,seen)
                if (value.get('kind') != 'retained-health' or value.get('complete') is not False
                        or value.get('producers') != counts or source.read(1)):
                    raise ValueError('invalid retained health/EOF')
                original = value['source_health']['producers']
                if len(original) != 7 or any(counts[p] > original[p]['emitted'] or seen[p] > original[p]['attempted'] for p in range(7)):
                    raise ValueError('retained counts exceed original health')
                destination.write(payload.decode()); return value
            else:
                raise ValueError('unknown diagnostic frame')
    except (ValueError, KeyError, TypeError, UnicodeError, struct.error) as error:
        raise StreamError('NATIVE_PREFIX_FORMAT', str(error), 'INCONCLUSIVE') from error


def pcapng(source, destination, *, start_ns, end_ns, source_sha256):
    """Copy exact EPBs from the supported sealed single-interface ns profile.

    Full source SHA authenticates the already-validated tail. The final ISB must
    certify the selected interval and zero capture loss. No packet is renumbered.
    """
    from .pcapng import MAX_BLOCK, SHB, options, option
    source, destination = Path(source), Path(destination)
    _bounds(start_ns, end_ns)
    original, created = _identity(source), False
    try:
        with source.open('rb') as input_stream, destination.open('xb') as output:
            created = True
            prefix = _read(input_stream, 12)
            order = {b'\x4d\x3c\x2b\x1a':'<', b'\x1a\x2b\x3c\x4d':'>'}.get(prefix[8:])
            if prefix[:4] != SHB or order is None:
                raise ValueError('invalid pcapng section')
            input_stream.seek(0)
            def block():
                raw = _read(input_stream, 8)
                kind, size = struct.unpack(order+'II', raw)
                if size < 12 or size % 4 or size > MAX_BLOCK:
                    raise ValueError('invalid pcapng block length')
                raw += _read(input_stream, size-8)
                if struct.unpack(order+'I', raw[-4:])[0] != size:
                    raise ValueError('invalid pcapng block trailer')
                return kind, raw[8:-4], raw
            def emit(kind, body):
                size = len(body)+12
                output.write(struct.pack(order+'II', kind, size)+body+struct.pack(order+'I', size))
            def opt(code, value):
                return struct.pack(order+'HH', code, len(value))+value+bytes((-len(value))%4)
            kind, body, _ = block()
            if kind != 0x0a0d0d0a or len(body) < 16 or struct.unpack_from(order+'HH', body, 4) != (1,0):
                raise ValueError('unsupported section')
            opts = options(body[16:], order)
            comment = _line(dict(diagnostic_prefix=True, source_sha256=source_sha256,
                                 start_ns=start_ns, end_ns=end_ns)).rstrip()
            emit(kind, body[:8]+struct.pack(order+'q', -1)+b''.join(opt(k,v) for k,values in opts.items() for v in values)+opt(1,comment)+bytes(4))
            kind, body, raw = block()
            if kind != 1 or len(body) < 8 or struct.unpack_from(order+'H',body)[0] != 1:
                raise ValueError('one Ethernet interface is required')
            opts = options(body[8:], order)
            if option(opts,9,1) != b'\x09' or option(opts,13,1,b'\0') != b'\0':
                raise ValueError('nanosecond timestamps and stripped FCS are required')
            offset = struct.unpack(order+'q', option(opts,14,8,bytes(8)))[0]*10**9
            output.write(raw)
            count, last = 0, -1
            while input_stream.tell() < original[2]:
                kind, body, raw = block()
                if kind == 5:
                    continue
                if kind != 6 or len(body) < 20:
                    raise ValueError('unsupported pcapng profile')
                interface, high, low, caplen, wirelen = struct.unpack_from(order+'IIIII',body)
                at = (high<<32|low)+offset
                if interface or at < last or caplen > wirelen or len(body) < 20+((caplen+3)&~3):
                    raise ValueError('invalid packet/interface/timestamp')
                last = at
                if at >= end_ns:
                    break
                if at >= start_ns:
                    output.write(raw); count += 1
            # Registered G2 capture ends with an ISB for its one interface.
            input_stream.seek(-4,2); size = struct.unpack(order+'I',_read(input_stream,4))[0]
            if not 24 <= size <= MAX_BLOCK or size > original[2]:
                raise ValueError('missing final capture statistics')
            input_stream.seek(-size,2); kind, body, _ = block()
            if kind != 5 or len(body) < 12 or struct.unpack_from(order+'I',body)[0]:
                raise ValueError('missing final capture statistics')
            opts = options(body[12:],order)
            values = [option(opts,k,8) for k in (2,3,5,7,8)]
            if any(v is None for v in values):
                raise ValueError('capture health is unknown')
            bounds = [(struct.unpack(order+'II',v)[0]<<32|struct.unpack(order+'II',v)[1])+offset for v in values[:2]]
            counters = [struct.unpack(order+'Q',v)[0] for v in values[2:]]
            if not bounds[0] <= start_ns < end_ns <= bounds[1] or any(counters[:2]) or counters[2] == 2**64-1:
                raise ValueError('capture health does not certify prefix')
            def stamp(at):
                raw = at-offset
                uint(raw,'raw pcapng timestamp')
                return struct.pack(order+'II',raw>>32,raw&0xffffffff)
            emit(5,struct.pack(order+'I',0)+stamp(end_ns)+opt(2,stamp(start_ns))+opt(3,stamp(end_ns))+
                 opt(5,struct.pack(order+'Q',0))+opt(7,struct.pack(order+'Q',0))+opt(8,struct.pack(order+'Q',count))+bytes(4))
            input_stream.seek(0)
            if hashlib.file_digest(input_stream,'sha256').hexdigest() != source_sha256 or _identity(source) != original:
                raise ValueError('original pcapng hash/identity differs from sealed source')
            output.flush(); os.fsync(output.fileno())
        return dict(records=count, bytes=destination.stat().st_size, sha256=_hash(destination),
                    source_sha256=source_sha256, source_bytes=original[2],start_ns=start_ns,end_ns=end_ns)
    except BaseException:
        if created:
            destination.unlink(missing_ok=True)
        raise


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('input'); parser.add_argument('--out',required=True)
    args=parser.parse_args()
    with open(args.input,'rb') as source,open(args.out,'x') as destination:
        decode_native(source,destination)


if __name__=='__main__':
    main()
