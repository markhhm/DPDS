# SPDX-License-Identifier: Apache-2.0
"""Array reader agrees with the independent streaming adapter on all packets."""
import hashlib
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch

import numpy as np

from tools.g2.pcapng import Capture, observation
from tools.g2.tests.test_pcapng import pcapng


class CaptureArraysTests(unittest.TestCase):
    def reader(self, path, start=0, end=10**9, **kwargs):
        from tools.g2.pcapng_arrays import CaptureArrays
        return CaptureArrays(path, {0: 3}, start, end, **kwargs)

    def test_array_records_match_streaming_observations_and_health(self):
        manifest = dict(lines=[dict(name='line', ingress_port=3, egress_port=4, vlan_id=100)],
                        identity_profile=dict(probe_port=9000))
        for endian, retained, ip_options, offset in [('<',128,0,0), ('>',59,0,-1), ('<',99,40,1)]:
            with self.subTest(endian=endian, retained=retained), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp)/'capture.pcapng'
                epoch = 2**54
                pcapng(path, [epoch+i for i in range(7)], endian=endian, caplen=retained,
                       snaplen=retained, wirelen=1518, ip_options=ip_options, offset=offset,
                       start=epoch-1, end=epoch+100)
                start, end = epoch-1+offset*10**9, epoch+100+offset*10**9
                reader = self.reader(path, start, end, batch_size=3)
                batches = list(reader.batches())
                self.assertEqual([len(batch) for batch in batches], [3,3,1])
                rows = np.concatenate(batches)
                reference = Capture(path, {0:3}, start, end)
                for ordinal, ((interface, raw, frame, length), row) in enumerate(zip(reference.packets(), rows)):
                    expected = observation(frame, length, 3, 'before', manifest)
                    flow = f'{int(row["source"]):08x}:{int(row["destination"]):08x}:{row["source_port"]}:{row["destination_port"]}'
                    self.assertEqual(flow+f':{row["sequence"]}', expected['external_identity'])
                    self.assertEqual(tuple(int(row[key]) for key in ('interface','port','raw','ordinal','length','vlan_id','selected_late')),
                                     (interface,3,raw,ordinal,length,100,0))
                self.assertEqual(reader.descriptions, reference.descriptions)
                self.assertEqual((reader.records, reader.drops), (7,0))
                self.assertEqual(reader.integrity['file_sha256'], hashlib.sha256(path.read_bytes()).hexdigest())
                self.assertTrue(reader.integrity['complete'])

    def test_all_packet_health_and_identity_faults_reject_capture(self):
        cases = [dict(times=[2,1]), dict(drop=1), dict(os_drop=1), dict(epb_drop=1),
                 dict(epb_drop=2**64-1), dict(resolution=6), dict(caplen=58,snaplen=58),
                 dict(caplen=128,snaplen=127), dict(wirelen=145), dict(start=3), dict(end=5)]
        for kwargs in cases:
            with self.subTest(kwargs=kwargs), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp)/'capture.pcapng'
                pcapng(path, **(dict(times=[1,2]) | kwargs))
                reader = self.reader(path, batch_size=1)
                with self.assertRaises(ValueError):
                    list(reader.batches())
                self.assertFalse(reader.integrity['complete'])

    def test_corrupt_late_packet_and_final_statistics_are_not_skipped(self):
        for fault in ('probe','vlan','fragment','port','trailer','missing_isb','delivered','epb_option','timestamp_range'):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp)/'capture.pcapng'; pcapng(path, [1,2])
                data = bytearray(path.read_bytes())
                positions=[]; cursor=0
                while cursor < len(data):
                    kind,size=struct.unpack_from('<II',data,cursor)
                    if kind == 6: positions.append(cursor)
                    cursor += size
                packet = positions[-1]+28
                if fault == 'probe': data[packet+54] ^= 1
                elif fault == 'vlan': data[packet+12] = 0
                elif fault == 'fragment': data[packet+24] = 0x20
                elif fault == 'port': data[packet+40] ^= 1
                elif fault == 'trailer': data[-4:] = struct.pack('<I',12)
                elif fault == 'missing_isb': data = data[:-struct.unpack_from('<I',data,len(data)-4)[0]]
                elif fault == 'delivered':
                    pos=data.rfind(struct.pack('<HHQ',8,8,2)); data[pos+4:pos+12]=struct.pack('<Q',3)
                elif fault == 'epb_option':
                    size=struct.unpack_from('<I',data,positions[-1]+4)[0]
                    data[positions[-1]+size-8:positions[-1]+size-4]=b'\0\0\1\0'
                elif fault == 'timestamp_range':
                    # A negative IDB second offset must never wrap to uint64.
                    marker=struct.pack('<HHq',14,8,0); pos=data.find(marker)
                    data[pos+4:pos+12]=struct.pack('<q',-1)
                path.write_bytes(data)
                reader=self.reader(path,batch_size=1)
                with self.assertRaises(ValueError): list(reader.batches())
                self.assertFalse(reader.integrity['complete'])

    def test_empty_capture_can_be_certified_and_missing_capture_cannot(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'capture.pcapng';pcapng(path,[])
            reader=self.reader(path)
            self.assertEqual(list(reader.batches()),[])
            self.assertEqual(reader.records,0)
            self.assertTrue(reader.integrity['complete'])
            path.unlink()
            with self.assertRaises(OSError): list(self.reader(path).batches())

    def test_chunk_boundaries_and_option_free_packets_preserve_every_record(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'capture.pcapng';pcapng(path,list(range(100)))
            original=path.read_bytes(); blocks=[]; cursor=0
            while cursor < len(original):
                kind,size=struct.unpack_from('<II',original,cursor)
                block=original[cursor:cursor+size]
                if kind == 6:
                    # Omit optional EPB drop count and end marker, as native captures do.
                    block=block[:4]+struct.pack('<I',size-16)+block[8:-20]+struct.pack('<I',size-16)
                blocks.append(block);cursor+=size
            path.write_bytes(b''.join(blocks))
            with patch('tools.g2.pcapng_arrays.CHUNK_BYTES', 257):
                reader=self.reader(path,batch_size=7)
                rows=np.concatenate(list(reader.batches()))
            np.testing.assert_array_equal(rows['sequence'],np.arange(100))
            np.testing.assert_array_equal(rows['ordinal'],np.arange(100))
            self.assertEqual(reader.records,100)
            self.assertTrue(reader.integrity['complete'])

    def test_probe_uint64_sequence_and_selected_late_flag_are_exact(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'capture.pcapng';pcapng(path,[1])
            data=path.read_bytes()
            probe=struct.pack('!QIB',0,0x4c30504b,0)
            replacement=struct.pack('!QIB',2**64-1,0x4c30504b,1)
            self.assertIn(probe,data)
            path.write_bytes(data.replace(probe,replacement))
            rows=np.concatenate(list(self.reader(path).batches()))
            self.assertEqual(int(rows['sequence'][0]),2**64-1)
            self.assertEqual(int(rows['selected_late'][0]),1)

    def test_interleaved_interfaces_have_independent_port_ordinals(self):
        from tools.g2.pcapng_arrays import CaptureArrays
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'capture.pcapng';pcapng(path,[1,2,3,4])
            data=path.read_bytes();blocks=[];cursor=sequence=0
            while cursor < len(data):
                kind,size=struct.unpack_from('<II',data,cursor)
                block=bytearray(data[cursor:cursor+size]);cursor+=size
                if kind == 1: blocks.append(bytes(block))
                if kind == 6:
                    struct.pack_into('<I',block,8,sequence%2);sequence+=1
                if kind == 5:
                    pos=block.find(struct.pack('<HHQ',8,8,4))
                    struct.pack_into('<Q',block,pos+4,2)
                    blocks.append(bytes(block));struct.pack_into('<I',block,8,1)
                blocks.append(bytes(block))
            path.write_bytes(b''.join(blocks))
            reader=CaptureArrays(path,{0:3,1:4},0,10**9,batch_size=3)
            rows=np.concatenate(list(reader.batches()))
            np.testing.assert_array_equal(rows['port'],[3,4,3,4])
            np.testing.assert_array_equal(rows['ordinal'],[0,0,1,1])
            reference=Capture(path,{0:3,1:4},0,10**9);list(reference.packets())
            self.assertEqual(reader.descriptions,reference.descriptions)

    def test_mutation_after_batch_prevents_source_certification(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'capture.pcapng';pcapng(path,[1,2])
            reader=self.reader(path,batch_size=1);iterator=reader.batches()
            next(iterator)
            changed=bytearray(path.read_bytes());changed[100] ^= 1
            path.write_bytes(changed)
            with self.assertRaisesRegex(ValueError,'changed'):
                list(iterator)
            self.assertFalse(reader.integrity['complete'])


if __name__ == '__main__':
    unittest.main()
