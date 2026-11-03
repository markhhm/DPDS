# SPDX-License-Identifier: Apache-2.0
"""B-prime engine configs retain the full registered stress envelope."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[3]
CONDITION='fixed-E3-142-320-1-both-L9-X'


class BprimeInputsTests(unittest.TestCase):
    def test_single_port_candidate_is_exact_forward_line_with_full_capacity(self):
        from tools.g2.expand_m6_x710 import engine_config
        from tools.g2.reference import Reference
        for vlan in (710,711):
            reduced=json.loads((ROOT/f'examples/m5/bprime-1port-{vlan}.json').read_text())
            expanded=engine_config('fixed-E3-142-320-1-forward-L9-X',vlan)
            full=engine_config(CONDITION,vlan)
            self.assertEqual(reduced,expanded)
            self.assertEqual(reduced,dict(schema='hng-v1',lines=full['lines'][:1]))
            Reference(reduced,'single-direction-candidate')

    def test_committed_configs_are_expanded_and_have_full_capacity(self):
        from tools.g2.expand_m6_x710 import engine_config
        from tools.g2.reference import Reference
        for vlan in (710,711):
            config=engine_config(CONDITION,vlan)
            Reference(config,'bprime')
            self.assertEqual(config,json.loads((ROOT/f'examples/m5/bprime-{vlan}.json').read_text()))
            self.assertEqual(config['schema'],'hng-v1');self.assertEqual(len(config['lines']),2)
            for port,line in enumerate(config['lines']):
                self.assertEqual((line['ingress_port'],line['egress_port'],line['vlan_id']),(port,1-port,vlan))
                self.assertIsNone(line['trace'])
                self.assertEqual(line['model'],dict(c_bps=9000000000,q_bytes=8388608,t_ns=142000000,
                    delta_ns=320000000,p='1',seed=1,storage_capacity_packets=6318588,storage_capacity_bytes=9591616584))
                self.assertEqual(line['envelope'],dict(min_q_bytes=64,max_q_bytes=1522,burst_bound_packets=16))

    def test_existing_expander_cli_produces_engine_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            out=Path(tmp)/'engine.json'
            result=subprocess.run([sys.executable,'-B','-m','tools.g2.expand_m6_x710','--condition',CONDITION,
                '--active-vlan','711','--out',str(out)],capture_output=True,text=True)
            self.assertEqual(result.returncode,0,result.stderr)
            self.assertEqual(json.loads(out.read_text()),json.loads((ROOT/'examples/m5/bprime-711.json').read_text()))

    def test_unsupported_trace_multi_line_or_vlan_is_rejected(self):
        from tools.g2.expand_m6_x710 import engine_config
        for condition,vlan in ((CONDITION,0),(CONDITION,4095),('unknown',710),
                               ('trace-single-A-forward-L0-U',710)):
            with self.subTest(condition=condition,vlan=vlan),self.assertRaises(ValueError):engine_config(condition,vlan)


if __name__=='__main__':unittest.main()
