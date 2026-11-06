# SPDX-License-Identifier: Apache-2.0
import copy
import json
from pathlib import Path
import unittest
from tools.g2.reference import Reference
from tools.g2.tests.test_reference import packet

class M7ReferenceTests(unittest.TestCase):
    def test_registered_backend_keeps_model_decisions(self):
        cfg=json.loads(Path('tests/m7-engine.json').read_text())
        cfg['lines'][0].update(name='cable', trace=None)
        old=copy.deepcopy(cfg);old['lines'][0].pop('m7_storage')
        a,b=Reference(old,'fixture'),Reference(cfg,'fixture')
        self.assertEqual([a.consume(packet(i,length=60)) for i in range(3)],
                         [b.consume(packet(i,length=60)) for i in range(3)])

    def test_smaller_registered_core_and_strict_configuration(self):
        cfg=json.loads(Path('tests/m7-engine.json').read_text())
        line=cfg['lines'][0];line.update(name='cable',trace=None)
        line['model'].update(c_bps=90_000_000_000,q_bytes=83_886_086,t_ns=142_000_000,
            delta_ns=320_000_000,storage_capacity_packets=9_377_250,storage_capacity_bytes=9_377_250*1518)
        Reference(cfg,'fixture')
        for change in ({'mode':'unknown'},{'window_ns':1},{'hold_ns':0},{'unknown':1},
                       {'composition':[{'l2_bytes':64,'weight':0}]}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                bad=copy.deepcopy(cfg);bad['lines'][0]['m7_storage'].update(change);Reference(bad,'fixture')
        bad=copy.deepcopy(cfg);bad['lines'][0]['m7_storage']=None
        with self.assertRaises(ValueError):Reference(bad,'fixture')

    def test_unknown_class_rejected(self):
        cfg=json.loads(Path('tests/m7-engine.json').read_text());cfg['lines'][0].update(name='cable',trace=None)
        ref=Reference(cfg,'fixture')
        with self.assertRaises(ValueError):ref.consume(packet(0,length=76))

    def test_sampled_prefix_rejects_unknown_lengths_even_without_samples(self):
        import numpy as np
        from tools.g2.sampled_reference import SampledReference
        from tools.g2.tests.test_sampled_columns import columns
        cfg=json.loads(Path('tests/m7-engine.json').read_text());cfg['lines'][0].update(name='cable',trace=None)
        for mode in ('stress','calibration'):
            event=packet(0,length=76)
            with self.subTest(mode=mode,path='event'),self.assertRaises(ValueError):
                SampledReference(cfg,'fixture',timing_mode=mode).consume(event,False)
            with self.subTest(mode=mode,path='columns'),self.assertRaises(ValueError):
                list(SampledReference(cfg,'fixture',timing_mode=mode).consume_columns(columns([event]),np.array([False]),lambda i:event))

    def test_checked_in_m7_conditions_keep_models_and_exact_quotas(self):
        import hashlib
        from tools.g2.m7_storage import plan
        base=Path('examples/m7')
        hashes=dict(line.split()[::-1] for line in (base/'compact.sha256').read_text().splitlines() if not line.startswith('#'))
        for c in (80,90):
            for mix in ('x','u'):
                name=f'c{c}-{mix}-compact.json';raw=(base/name).read_bytes()
                self.assertEqual(hashlib.sha256(raw).hexdigest(),hashes[name])
                cfg=json.loads(raw);old=json.loads((base/f'c{c}-{mix}.json').read_text())
                Reference(cfg,'fixture')
                for line,original in zip(cfg['lines'],old['lines']):
                    sizes=plan(line['m7_storage'],line['model'],line['envelope'])
                    self.assertEqual(line['model']['storage_capacity_packets'],sizes['slots'])
                    copy_config=copy.deepcopy(line)
                    copy_config.pop('m7_storage')
                    for key in ('storage_capacity_packets','storage_capacity_bytes'):
                        copy_config['model'][key]=original['model'][key]
                    self.assertEqual(copy_config,original)
