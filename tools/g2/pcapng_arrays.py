# SPDX-License-Identifier: Apache-2.0
"""Bounded, vectorized decoding of the registered G2 pcapng probe profile.

Consumers must exhaust batches before trusting selected records: final ISBs and
the source signature/hash certify the complete capture, including omitted rows.
"""
import hashlib
from pathlib import Path
import struct

import numpy as np

from .pcapng import MAX_BLOCK, SHB, fail, file_signature, option, options
from .probe import HEADER, MAGIC
from .reference import uint


RECORD = np.dtype([(name, dtype) for name, dtype in (
    ('interface','u4'), ('port','u4'), ('raw','u8'), ('ordinal','u8'),
    ('source','u4'), ('destination','u4'), ('source_port','u2'),
    ('destination_port','u2'), ('sequence','u8'), ('selected_late','u1'),
    ('length','u4'), ('vlan_id','u2'))])
CHUNK_BYTES = 8*1024*1024


def _byte_rows(data, positions, width):
    if (len(positions) and positions.dtype.kind == 'i' and data.ndim == 1
            and data.dtype == np.uint8 and data.flags.c_contiguous):
        first = int(positions[0])
        stride = int(positions[1])-first if len(positions) > 1 else 0
        # Python integers keep the buffer-bound proof exact even for hostile offsets.
        if (first >= 0 and stride >= 0 and first+(len(positions)-1)*stride+width <= len(data)
                and np.all(np.diff(positions) == stride)):
            return np.ndarray((len(positions),width),dtype=np.uint8,buffer=data,
                              offset=first,strides=(stride,1))
    return data[positions[:,None]+np.arange(width)]


def _words(data, positions, dtype):
    dtype = np.dtype(dtype)
    return _byte_rows(data,positions,dtype.itemsize).view(dtype).reshape(-1)


def _epb_run(data, cursor, size, order, limit):
    count = min((len(data)-cursor)//size,limit)
    headers = np.ndarray((count,2),dtype=order+'u4',buffer=data,offset=cursor,strides=(size,4))
    trailers = np.ndarray((count,),dtype=order+'u4',buffer=data,offset=cursor+size-4,strides=(size,))
    mismatches = np.flatnonzero((headers[:,0] != 6) | (headers[:,1] != size) | (trailers != size))
    return int(mismatches[0]) if len(mismatches) else count


class CaptureArrays:
    def __init__(self, path, interfaces, start_ns, end_ns, probe_port=9000, batch_size=65536):
        if type(batch_size) is not int or batch_size <= 0:
            raise ValueError('batch_size must be positive')
        self.path, self.interfaces = Path(path), interfaces
        self.start, self.end = start_ns, end_ns
        self.probe_port, self.batch_size = probe_port, batch_size
        self.descriptions = {}
        self.records = self.drops = self.interface_count = 0
        self.integrity = dict(complete=False, file_sha256=None)
        self._ordinals = {}

    def _decode(self, data, positions, blocks, order):
        positions = np.asarray(positions, dtype=np.int64)
        headers = _byte_rows(data,positions+8,20).view(order+'u4').reshape(-1,5)
        interfaces, high, low, caplen, wirelen = headers.T
        padded = (caplen.astype(np.uint64)+3) & ~np.uint64(3)
        sizes = _words(data, positions+4, order+'u4')
        if np.any(caplen > wirelen) or np.any(32+padded > sizes):
            fail('PCAPNG_TRUNCATED', 'packet bytes were truncated')
        rows = np.empty(len(positions), dtype=RECORD)
        rows['interface'], rows['length'] = interfaces, wirelen
        raw = (high.astype(np.uint64) << np.uint64(32)) | low
        for interface in np.unique(interfaces):
            key = int(interface)
            if key not in self.descriptions:
                fail('PCAPNG_INTERFACE', 'unregistered capture interface')
            info = self.descriptions[key]
            selected = interfaces == interface
            stamps = raw[selected]
            if info['snaplen'] and np.any(caplen[selected] > info['snaplen']):
                fail('PCAPNG_TRUNCATED', 'packet bytes were truncated')
            offset = info['offset']
            if int(stamps.min())+offset < 0 or int(stamps.max())+offset >= 1 << 64:
                uint(int(stamps.min())+offset if offset < 0 else int(stamps.max())+offset, 'capture timestamp')
            stamps = stamps-np.uint64(-offset) if offset < 0 else stamps+np.uint64(offset)
            if (info['last'] is not None and int(stamps[0]) < info['last']) or np.any(stamps[1:] < stamps[:-1]):
                fail('CLOCK_BACKWARD', 'capture timestamp moved backward')
            if info['first'] is None: info['first'] = int(stamps[0])
            info.update(last=int(stamps[-1]), packet_block=int(np.asarray(blocks)[selected][-1]),
                        packets=info['packets']+int(selected.sum()))
            rows['raw'][selected] = stamps
            rows['port'][selected] = self.interfaces[key]
        for port in np.unique(rows['port']):
            selected = rows['port'] == port
            count = int(selected.sum())
            ordinal = self._ordinals.get(int(port),0)
            rows['ordinal'][selected] = np.arange(ordinal,ordinal+count,dtype=np.uint64)
            self._ordinals[int(port)] = ordinal+count
        option_at = positions+28+padded.astype(np.int64)
        option_end = positions+sizes-4
        for index in np.flatnonzero(option_at != option_end):
            opts = options(data[int(option_at[index]):int(option_end[index])].tobytes(), order)
            dropped = struct.unpack(order+'Q', option(opts,4,8,bytes(8)))[0]
            if dropped == (1 << 64)-1:
                fail('PCAPNG_HEALTH_UNKNOWN', 'packet drop count is unavailable')
            self.drops += dropped
            if dropped: fail('PCAPNG_CAPTURE_DROP', 'enhanced packet block reports capture loss')
            if struct.unpack(order+'I',option(opts,2,4,bytes(4)))[0] >> 5 & 15:
                fail('PCAPNG_FCS', 'per-packet FCS is unsupported')
        self.records += len(rows)
        if np.any(caplen < 38):
            fail('PCAPNG_IDENTITY', 'truncated single-VLAN IPv4 probe')
        packet = positions+28
        fixed = _byte_rows(data,packet,38)
        if np.any(fixed[:,12] != 0x81) or np.any(fixed[:,13] != 0) or np.any(fixed[:,16] != 8) or np.any(fixed[:,17] != 0):
            fail('PCAPNG_IDENTITY', 'retained single-VLAN IPv4 UDP probes are required')
        ihl = (fixed[:,18] & 15).astype(np.int64)*4
        if np.any(fixed[:,18] >> 4 != 4) or np.any(ihl < 20) or np.any(caplen < 18+ihl+8+HEADER.size):
            fail('PCAPNG_IDENTITY', 'truncated or unsupported IPv4 header')
        udp = packet+18+ihl
        total = _words(data,packet+20,'>u2')
        fragments = _words(data,packet+24,'>u2')
        udp_size = _words(data,udp+4,'>u2')
        destination_port = _words(data,udp+2,'>u2')
        if (np.any(fixed[:,27] != 17) or np.any(fragments & 0x3fff)
                or np.any(destination_port != self.probe_port) or np.any(total != ihl+udp_size)
                or np.any(udp_size < 8+HEADER.size) or np.any(18+total.astype(np.uint64) > wirelen)):
            fail('PCAPNG_IDENTITY', 'unsupported or truncated probe packet')
        late = data[udp+20]
        if np.any(_words(data,udp+16,'>u4') != MAGIC) or np.any(late > 1):
            raise ValueError('invalid probe magic/selected-late flag')
        rows['source'], rows['destination'] = _words(data,packet+30,'>u4'), _words(data,packet+34,'>u4')
        rows['source_port'], rows['destination_port'] = _words(data,udp,'>u2'), destination_port
        rows['sequence'], rows['selected_late'] = _words(data,udp+8,'>u8'), late
        rows['vlan_id'] = _words(data,packet+14,'>u2') & 4095
        return rows

    def _metadata(self, kind, body, order, ordinal):
        if kind == 1:
            if len(body) < 8: fail('PCAPNG_INTERFACE', 'truncated interface description')
            link, _, snaplen = struct.unpack_from(order+'HHI',body)
            opts = options(body[8:],order)
            if (link != 1 or option(opts,9,1,b'\x06') != b'\x09' or option(opts,13,1,b'\0') != b'\0'):
                fail('PCAPNG_INTERFACE', 'Ethernet, explicit nanoseconds and stripped FCS are required')
            offset = struct.unpack(order+'q',option(opts,14,8,bytes(8)))[0]*10**9
            if self.interface_count in self.interfaces:
                self.descriptions[self.interface_count] = dict(offset=offset,snaplen=snaplen,packets=0,
                                                              first=None,last=None,packet_block=0,stats=None)
            self.interface_count += 1
        elif kind == 5:
            if len(body) < 12: fail('PCAPNG_TRUNCATED', 'truncated statistics block')
            interface, high, low = struct.unpack_from(order+'III',body)
            if interface not in self.descriptions: fail('PCAPNG_INTERFACE', 'unregistered capture interface')
            info = self.descriptions[interface]
            raw = (high << 32 | low)+info['offset']; uint(raw,'capture timestamp')
            opts = options(body[12:],order)
            required = [option(opts,code,8) for code in (2,3,5,7,8)]
            if any(value is None for value in required):
                fail('PCAPNG_HEALTH_UNKNOWN', 'capture statistics need time bounds, ifdrop, osdrop and usrdeliv')
            start,end = [(struct.unpack(order+'II',value)[0] << 32 | struct.unpack(order+'II',value)[1])+info['offset']
                         for value in required[:2]]
            ifdrop,osdrop,delivered = [struct.unpack(order+'Q',value)[0] for value in required[2:]]
            if max(ifdrop,osdrop,delivered) == (1 << 64)-1 or ifdrop+osdrop >= 1 << 64:
                fail('PCAPNG_HEALTH_UNKNOWN', 'capture counter is unavailable or overflowed')
            self.drops = max(self.drops,ifdrop+osdrop)
            if ifdrop or osdrop: fail('PCAPNG_CAPTURE_DROP', 'interface statistics report capture loss')
            prior = info['stats']
            if prior and (start != prior['start'] or end < prior['end'] or delivered < prior['delivered'] or raw < prior['at']):
                fail('PCAPNG_HEALTH_UNKNOWN', 'capture statistics reset or moved backward')
            info['stats'] = dict(start=start,end=end,delivered=delivered,block=ordinal,at=raw)
        elif kind in (2,3):
            fail('PCAPNG_PACKET_BLOCK', 'packet blocks without the required timestamp profile are unsupported')

    def batches(self):
        initial = file_signature(self.path)
        if not self.path.is_file(): fail('PCAPNG_SOURCE','a closed regular capture file is required')
        digest = hashlib.sha256()
        order = section_end = None
        ordinal = absolute = 0
        tail = np.empty(0,dtype=np.uint8)
        with self.path.open('rb') as source:
            while True:
                chunk = np.fromfile(source,dtype=np.uint8,count=CHUNK_BYTES)
                if not len(chunk): break
                digest.update(chunk)
                data = np.concatenate((tail,chunk)) if len(tail) else chunk
                cursor = 0
                positions, blocks = [], []
                while len(data)-cursor >= 12:
                    prefix = data[cursor:cursor+12]
                    if prefix[:4].tobytes() == SHB:
                        if order is not None: fail('PCAPNG_SECTION','one capture section is required')
                        order = {b'\x4d\x3c\x2b\x1a':'<',b'\x1a\x2b\x3c\x4d':'>'}.get(prefix[8:].tobytes())
                    if order is None: fail('PCAPNG_SECTION','missing/invalid section byte order')
                    kind,size = struct.unpack_from(order+'II',data,cursor)
                    if size < 12 or size % 4 or size > MAX_BLOCK:
                        fail('PCAPNG_BLOCK_LENGTH','invalid or unsupported block length')
                    if cursor+size > len(data): break
                    if struct.unpack_from(order+'I',data,cursor+size-4)[0] != size:
                        fail('PCAPNG_BLOCK_LENGTH','block trailer length mismatch')
                    ordinal += 1
                    if kind == 6:
                        if size < 32: fail('PCAPNG_TRUNCATED','truncated packet block')
                        # Each candidate's type, length and trailer is checked before advancing.
                        count = _epb_run(data,cursor,size,order,self.batch_size-len(positions))
                        positions.extend(range(cursor,cursor+count*size,size))
                        blocks.extend(range(ordinal,ordinal+count))
                        cursor += (count-1)*size
                        ordinal += count-1
                    else:
                        if positions:
                            yield self._decode(data,positions,blocks,order)
                            positions,blocks = [],[]
                        body = data[cursor+8:cursor+size-4].tobytes()
                        if kind == 0x0a0d0d0a:
                            if len(body) < 16 or struct.unpack_from(order+'HH',body,4) != (1,0):
                                fail('PCAPNG_SECTION','unsupported section version')
                            length = struct.unpack_from(order+'q',body,8)[0]
                            if length < -1: fail('PCAPNG_SECTION','invalid section length')
                            section_end = None if length == -1 else absolute+cursor+size+length
                            options(body[16:],order)
                        else: self._metadata(kind,body,order,ordinal)
                    cursor += size
                    if section_end is not None and absolute+cursor > section_end:
                        fail('PCAPNG_SECTION','section length exceeded')
                    if len(positions) >= self.batch_size:
                        yield self._decode(data,positions,blocks,order)
                        positions,blocks = [],[]
                if positions: yield self._decode(data,positions,blocks,order)
                tail = data[cursor:].copy()
                absolute += cursor
        if len(tail): fail('PCAPNG_TRUNCATED','truncated capture block')
        if order is None or section_end is not None and absolute != section_end:
            fail('PCAPNG_TRUNCATED','empty or incomplete capture section')
        for interface in self.interfaces:
            if interface not in self.descriptions: fail('PCAPNG_INTERFACE','registered interface was not described')
            info = self.descriptions[interface]; stats = info['stats']
            if (stats is None or stats['block'] < info['packet_block'] or stats['delivered'] != info['packets']
                    or not stats['start'] <= self.start < self.end <= stats['end'] <= stats['at']
                    or info['first'] is not None and info['first'] < stats['start']
                    or info['last'] is not None and info['last'] > stats['end']):
                fail('PCAPNG_HEALTH_UNKNOWN','final capture statistics do not certify the registered window/packet count')
        if initial != file_signature(self.path): fail('PCAPNG_SOURCE_CHANGED','capture file changed during conversion')
        self.integrity.update(complete=True,file_sha256=digest.hexdigest())
