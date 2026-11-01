# SPDX-License-Identifier: Apache-2.0
"""Host selection reuses only a proven identical captured ingress key."""
import unittest
import numpy as np

from tools.g2.array_sampling import sample_mask
from tools.g2.native_arrays import ITEM_DTYPE
from tools.g2.sampled_native import NativeInput
from tools.g2.pcapng_arrays import RECORD


class SampleReuseTests(unittest.TestCase):
    def test_capture_membership_keeps_full_uint64_and_direction(self):
        source=NativeInput.__new__(NativeInput)
        source.config=dict(lines=[dict(name='a',ingress_port=0),dict(name='b',ingress_port=1)])
        source.names={'a':0,'b':1};source._match_groups=None
        source.chosen=np.zeros(4,dtype=ITEM_DTYPE)
        source.chosen['identity']=[0,2**64-1,2**53+3,7]
        source.chosen['line']=[0,0,1,1]
        source.chosen['source']=123
        rows=np.zeros(7,dtype=RECORD)
        rows['source']=123;rows['sequence']=[2**64-1,2**53+3,0,7,2**64-2,2**53+3,0]
        ports=np.array([0,0,0,1,0,1,1]);rows['source'][-1]=999
        expected=np.array([True,False,True,True,False,True,False])
        for _ in range(2):self.assertTrue(np.array_equal(source._matches(rows,ports,rows['sequence']),expected))

    def test_cached_current_previous_and_unknown_host_keys_match_scalar_contract(self):
        items=np.zeros(1024,dtype=ITEM_DTYPE)
        for name,value in (('source',0xc0000201),('destination',0xc6336402),('source_port',1111),('destination_port',9000),('identity_kind',1)):
            items[name]=value
        items['identity']=items['ingress_seq']=np.arange(len(items))
        wanted=sample_mask('reuse',17,4,items)
        source=NativeInput.__new__(NativeInput)
        source.manifest=dict(run='reuse',sampling=dict(seed=17));source.uniform=4;source._packet_cache={}
        self.assertTrue(callable(getattr(source,'_host_selection',None)),'host sample reuse entry point is missing')
        for begin,end in ((0,512),(512,1024),(1024,1024)):
            host=items[[0,1,31,400,511,512,700,1023]].copy()
            host['ingress_seq'][1]=999
            host['source'][2]^=123
            host['identity'][3]=2**64-1
            actual=source._host_selection(items[begin:end],wanted[begin:end],host)
            self.assertTrue(np.array_equal(actual,sample_mask('reuse',17,4,host)))


if __name__=='__main__':unittest.main()
