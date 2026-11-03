# SPDX-License-Identifier: Apache-2.0
"""Bounded pcapng identity projection for seqcheck; never an independent capture."""
import struct

import numpy as np

EPB = np.dtype([('kind','<u4'),('size','<u4'),('interface','<u4'),('high','<u4'),
    ('low','<u4'),('captured','<u4'),('length','<u4'),('data','u1',(128,)),('end','<u4')])


def _option(code, data):
    return struct.pack('<HH',code,len(data))+data+bytes(-len(data)%4)


def _block(kind, data):
    size=len(data)+12
    return struct.pack('<II',kind,size)+data+struct.pack('<I',size)


class NativeIdentityWriter:
    """Only the selected RX direction; no packet bytes/times claimed as wire evidence."""
    def __init__(self, path):
        self.stream=path.open('xb');self.count=0;self.last=0
        comment=_option(1,b'native RX identity projection; not an independent capture')+bytes(4)
        self.stream.write(_block(0x0a0d0d0a,struct.pack('<IHHq',0x1a2b3c4d,1,0,-1)+comment))
        self.stream.write(_block(1,struct.pack('<HHI',1,0,128)+_option(9,b'\x09')+bytes(4)))

    def write(self, items):
        if not len(items):return
        if (np.any(items['identity_kind']!=1) or np.any(items['selected_late']>1)
                or np.any(items['length']<146)):
            raise ValueError('native identity projection requires complete stage1 probes (at least 146 bytes)')
        records=np.zeros(len(items),dtype=EPB)
        records['kind']=6;records['size']=records['end']=EPB.itemsize
        records['high']=items['arrival_ns']>>32;records['low']=items['arrival_ns']&0xffffffff
        records['captured']=128;records['length']=items['length']
        packet=records['data']
        prefix=bytes.fromhex('ffffffffffff020000000001810000000800450000800000000040110000')
        packet[:,:len(prefix)]=np.frombuffer(prefix,dtype='u1')
        def field(start,name,dtype):
            values=items[name].astype(dtype)
            packet[:,start:start+values.dtype.itemsize]=values.view('u1').reshape(len(items),-1)
        field(14,'vlan_id','>u2');field(30,'source','>u4');field(34,'destination','>u4')
        field(38,'source_port','>u2');field(40,'destination_port','>u2')
        packet[:,42:44]=[0,108]
        field(46,'identity','>u8');packet[:,54:58]=[0x4c,0x30,0x50,0x4b]
        packet[:,58]=items['selected_late']
        records.tofile(self.stream)
        self.count+=len(items);self.last=max(self.last,int(items['arrival_ns'].max()))

    def finish(self, end_ns):
        end_ns=max(end_ns,self.last+1)
        stats=((2,bytes(8)),(3,struct.pack('<II',end_ns>>32,end_ns&0xffffffff)),
               (5,bytes(8)),(7,bytes(8)),(8,struct.pack('<Q',self.count)))
        options=b''.join(_option(code,data) for code,data in stats)+bytes(4)
        self.stream.write(_block(5,struct.pack('<III',0,end_ns>>32,end_ns&0xffffffff)+options))
        self.stream.close()

    def close(self):
        self.stream.close()
