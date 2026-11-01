# SPDX-License-Identifier: Apache-2.0
"""Column batches keep the independent exact arrival model and chosen identities."""
import unittest
import numpy as np

from tools.g2.reference import Reference
from tools.g2.sampled_reference import SampledReference
from tools.g2.tests.test_reference import config, packet


def columns(events):
    dtype=[(name,'<u8') for name in ('ingress_port','ingress_seq','selection_seq','arrival_ns','length','line','vlan_id','segments','input_error')]
    rows=np.zeros(len(events),dtype=dtype)
    for i,event in enumerate(events):
        for name in rows.dtype.names:
            rows[name][i]=0 if name=='line' else event.get(name,1 if name=='segments' else 100 if name=='vlan_id' else 0)
    return rows


class SampledColumnsTests(unittest.TestCase):
    def compare(self,cfg,events,mode='stress',traces=None):
        full=Reference(cfg,'fixture',traces);slim=SampledReference(cfg,'fixture',traces,mode)
        expected=[full.consume(event) for event in events]
        for start in range(0,len(events),13):
            batch=events[start:start+13]
            wanted=np.array([(start+i)%3==2 for i in range(len(batch))])
            self.assertTrue(callable(getattr(slim,'consume_columns',None)),'column arrival entry point is missing')
            actual=list(slim.consume_columns(columns(batch),wanted,lambda i:batch[i]))
            self.assertEqual(actual,[expected[start+i] for i in np.flatnonzero(wanted)])
        end=dict(kind='end',run='fixture',ingress_port=0,next_ingress_seq=len(events))
        slim.consume(end)
        self.assertEqual(slim.finish()['line_counts'],{name:dict(state['counts']) for name,state in full.lines.items()})

    def test_stress_keeps_unsampled_q_and_fraction_boundaries(self):
        cfg=config();cfg['lines'][0]['model']['c_bps']=9_999_999_937
        events=[packet(i,(i//6)*1000,length=60+(i%3)*16) for i in range(150)]
        self.compare(cfg,events)

    def test_fixed_l0_only_computes_selected_expected_records(self):
        cfg=config();cfg['lines'][0]['model']['q_bytes']=8192
        self.compare(cfg,[packet(i,i*1000,length=60) for i in range(80)],'calibration')

    def test_trace_l0_keeps_entire_unsampled_clamp_prefix(self):
        cfg=config();line=cfg['lines'][0]
        line['model'].update(t_ns=0,p='0',delta_ns=0,q_bytes=8192,storage_capacity_packets=1_000_000,storage_capacity_bytes=2_000_000_000)
        line['trace']=dict(path='trace.json',sha256='0'*64)
        profile=dict(end_ns=100_000_000,loss_seed=1,rows=[dict(at_ns=0,t_ns=10_000_000,loss='0',outage=False),
            dict(at_ns=1_000_000,t_ns=0,loss='0',outage=False)],outage_action='hold',terminal='error',terminal_horizon_ns=None)
        self.compare(cfg,[packet(i,i*400_000,length=60) for i in range(80)],'calibration',{'cable':profile})

    def test_column_gap_and_backward_arrival_do_not_pass(self):
        for name,value in (('ingress_seq',2),('selection_seq',2),('arrival_ns',0),('length',0),('vlan_id',200)):
            slim=SampledReference(config(),'fixture')
            events=[packet(0,100),packet(1,200)]
            values=columns(events);values[name][1]=value
            self.assertTrue(callable(getattr(slim,'consume_columns',None)),'column arrival entry point is missing')
            with self.assertRaises(ValueError):list(slim.consume_columns(values,np.zeros(2,dtype=bool),lambda i:events[i]))


if __name__=='__main__':unittest.main()
