# SPDX-License-Identifier: Apache-2.0
"""Retained data is readable diagnostic evidence, never a repaired full run."""
import hashlib
import io
import json
from pathlib import Path
import struct
import tempfile
import unittest

import numpy as np
from tools.g2.native_arrays import NativeArrays
from tools.g2.native_binary import decode
from tools.g2.pcapng_arrays import CaptureArrays
from tools.g2.protocol import StreamError
from tools.g2.tests.test_native_binary import MAGIC, HEADER, entry, frame, health, line
from tools.g2.tests.test_pcapng import pcapng
try:
    from tools.g2 import raw_prefix
except ImportError:
    raw_prefix=None


def sha(path):return hashlib.sha256(path.read_bytes()).hexdigest()


class RawPrefixTests(unittest.TestCase):
    def module(self):
        self.assertIsNotNone(raw_prefix,'raw time-prefix codec is missing')
        return raw_prefix

    def source(self,path):
        rows=[]
        for order,at in enumerate((2,9,3,8)):
            common=[0]*22;common[0]=at
            rows.append(entry(kind=3,seq=order+1,order=order,common=common))
        path.write_bytes(MAGIC+frame(1,HEADER,0)+frame(2,b''.join(rows),1)+frame(3,line(health((4,0,0,0,0,0,0))),2))
        return rows

    def test_native_time_filter_keeps_original_gapped_records_and_never_claims_complete(self):
        module=self.module()
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);source=root/'full.hngb';original=self.source(source);before=source.read_bytes()
            result=module.native(source,root/'prefix.hngb',start_ns=2,end_ns=5,source_sha256=sha(source))
            self.assertEqual(result['records'],2);self.assertEqual(source.read_bytes(),before)
            output=io.StringIO()
            with (root/'prefix.hngb').open('rb') as stream:state=module.decode_native(stream,output)
            rows=[json.loads(x) for x in output.getvalue().splitlines()]
            self.assertEqual(rows[0]['schema'],'hng-native-prefix-v1');self.assertFalse(state['complete'])
            self.assertEqual([(r['order'],r['producer_seq'],r['at_ns']) for r in rows[1:-1]],[(0,1,2),(2,3,3)])
            with (root/'prefix.hngb').open('rb') as stream,self.assertRaises(StreamError):decode(stream,io.StringIO())
            self.assertEqual(result['sha256'],sha(root/'prefix.hngb'))

    def test_wire_views_preserve_actual_entries_when_a_frame_crosses_batch_boundaries(self):
        self.module()
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'source.hngb';expected=self.source(path)
            reader=NativeArrays(path,chunk_records=2,keep_wire=True);actual=[]
            for batch in reader.batches():
                wire=batch['wire']
                actual.extend(wire['data'][int(start)-2:int(end)].tobytes() for start,end in zip(wire['starts'],wire['ends']))
            self.assertEqual(actual,expected)

    def test_pcap_time_filter_matches_original_packet_bytes_and_derived_health(self):
        module=self.module()
        for endian,offset in (('<',0),('>',-1),('<',1)):
            with self.subTest(endian=endian,offset=offset),tempfile.TemporaryDirectory() as temp:
                root=Path(temp);source=root/'full.pcapng';epoch=2**54
                pcapng(source,[epoch+i for i in range(7)],endian=endian,offset=offset,start=epoch-1,end=epoch+10)
                base=epoch+offset*10**9
                result=module.pcapng(source,root/'prefix.pcapng',start_ns=base+2,end_ns=base+5,source_sha256=sha(source))
                reader=CaptureArrays(root/'prefix.pcapng',{0:0},base+2,base+5)
                rows=np.concatenate(list(reader.batches()))
                self.assertEqual(rows['raw'].tolist(),[base+2,base+3,base+4]);self.assertEqual(result['records'],3)
                def packets(path):
                    data=path.read_bytes();out=[];cursor=0
                    while cursor<len(data):
                        kind,size=struct.unpack_from(endian+'II',data,cursor)
                        if kind==6:out.append(data[cursor:cursor+size])
                        cursor+=size
                    return out
                self.assertEqual(packets(root/'prefix.pcapng'),packets(source)[2:5])
                self.assertTrue(reader.integrity['complete']);self.assertEqual(result['sha256'],sha(root/'prefix.pcapng'))

    def test_native_prefix_crc_and_empty_selection(self):
        module=self.module()
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);source=root/'full.hngb';self.source(source)
            target=root/'prefix.hngb'
            result=module.native(source,target,start_ns=20,end_ns=30,source_sha256=sha(source))
            self.assertEqual(result['records'],0)
            with target.open('rb') as stream:
                self.assertFalse(module.decode_native(stream,io.StringIO())['complete'])
            raw=bytearray(target.read_bytes());raw[-1]^=1
            with self.assertRaises(StreamError):module.decode_native(io.BytesIO(raw),io.StringIO())

    def test_wrong_full_hash_leaves_source_unchanged_and_no_usable_prefix(self):
        module=self.module()
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            for name,writer in [('native',lambda p:self.source(p)),('pcapng',lambda p:pcapng(p,[1,2,3]))]:
                source=root/name;writer(source);old=source.read_bytes();target=root/(name+'-prefix')
                with self.assertRaises(ValueError):getattr(module,name)(source,target,start_ns=0,end_ns=2,source_sha256='0'*64)
                self.assertEqual(source.read_bytes(),old);self.assertFalse(target.exists())


if __name__=='__main__':unittest.main()
