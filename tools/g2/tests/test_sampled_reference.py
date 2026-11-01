# SPDX-License-Identifier: Apache-2.0
import copy
import unittest
from tools.g2.reference import Reference
from tools.g2.tests.test_reference import config, packet
try:
    from tools.g2.sampled_reference import SampledReference
except ImportError:
    SampledReference = None


class SampledReferenceTests(unittest.TestCase):
    def check(self, cfg, arrivals, traces=None):
        self.assertIsNotNone(SampledReference)
        full = Reference(cfg, 'fixture', traces)
        slim = SampledReference(cfg, 'fixture', traces, 'stress')
        for i,a in enumerate(arrivals):
            event = packet(i,a,length=60+(i%3)*16)
            expected = full.consume(event)
            actual = slim.consume(event, i % 3 == 2)
            if i % 3 == 2:
                self.assertEqual(actual, expected)
            else:
                self.assertIsNone(actual)
        end = dict(kind='end',run='fixture',ingress_port=0,next_ingress_seq=len(arrivals))
        slim.consume(end,False)
        self.assertTrue(slim.finish()['input_complete'])

    def test_unsampled_arrivals_determine_sampled_drop_and_equal_drain(self):
        self.check(config(), [base*1000+a for base in range(20) for a in [0,0,0,100,200,200,201,500]])

    def test_variable_lengths_and_fractional_service_do_not_round_tail(self):
        cfg=config();cfg['lines'][0]['model']['c_bps']=9_999_999_937
        self.check(cfg,[i*79 for i in range(300)])

    def test_trace_calibration_keeps_unsampled_clamp_prefix(self):
        self.assertIsNotNone(SampledReference)
        cfg=config();line=cfg['lines'][0]
        line['model'].update(t_ns=0,p='0',delta_ns=0,q_bytes=8192,storage_capacity_packets=1_000_000,storage_capacity_bytes=2_000_000_000)
        line['trace']=dict(path='trace.json',sha256='0'*64)
        profile=dict(end_ns=20_000_000,loss_seed=1,rows=[dict(at_ns=0,t_ns=10_000_000,loss='0',outage=False),
            dict(at_ns=1_000_000,t_ns=0,loss='0',outage=False)],outage_action='hold',terminal='error',terminal_horizon_ns=None)
        full=Reference(cfg,'fixture',{'cable':profile})
        slim=SampledReference(cfg,'fixture',{'cable':profile},'calibration')
        first=packet(0,0);second=packet(1,2_000_000)
        full.consume(first);self.assertIsNone(slim.consume(first,False))
        expected=full.consume(second);actual=slim.consume(second,True)
        self.assertEqual(actual,expected)
        self.assertGreater(actual['clamp_extra_ns'],7_000_000)

    def test_zero_length_is_not_an_unobserved_valid_arrival(self):
        ref=SampledReference(config(),'fixture')
        with self.assertRaises(ValueError):ref.consume(packet(0,0,length=0),False)

    def test_fixed_calibration_uses_original_ordinal_with_sparse_outputs(self):
        self.assertIsNotNone(SampledReference)
        cfg=config();cfg['lines'][0]['model']['q_bytes']=8192
        ref=SampledReference(cfg,'fixture',None,'calibration')
        for i in range(10):
            row=ref.consume(packet(i,i*1000),i==9)
        full=Reference(cfg,'fixture')
        for i in range(10):expected=full.consume(packet(i,i*1000))
        self.assertEqual(row,expected)

if __name__=='__main__':unittest.main()
