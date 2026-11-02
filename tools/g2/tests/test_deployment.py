# SPDX-License-Identifier: Apache-2.0
"""Explicit deployment registration; synthetic identifiers, no hardware access."""
import copy
import json
from pathlib import Path
import tempfile
import unittest

from tools.g2.registration import validate_registration
from tools.g2.tests.test_reference import config
from tools.g2.tests.test_registration import manifest


def deployment():
    return dict(nic=dict(model='X710',pmd='i40e',ports=[0,1]),dut_host='dut',
        vlans=dict(cable=100,leo=200),
        generator=dict(host='generator',interfaces={'0':'gen0','1':'gen1'}),
        capture=dict(host='capture',interfaces={'0':'cap0','1':'cap1'},
                     clock_id='capture',mapping_id='ns',clock_source='synthetic-capture'))


def register(registration,family='cable'):
    registration['deployment']=deployment()
    for line in registration['lines']:line['family']=family
    return registration


class DeploymentTests(unittest.TestCase):
    def test_physical_registration_requires_explicit_deployment(self):
        reg=manifest();reg['scope']='physical'
        with self.assertRaisesRegex(ValueError,'deployment'):
            validate_registration(reg,config())

    def test_explicit_software_deployment_uses_the_same_validation(self):
        validate_registration(register(manifest()),config())

    def test_required_deployment_fields_are_never_defaulted(self):
        paths=[('nic',),('dut_host',),('vlans',),('generator',),('capture',),
               ('nic','model'),('nic','pmd'),('nic','ports'),('vlans','cable'),
               ('generator','host'),('generator','interfaces'),('capture','host'),
               ('capture','interfaces'),('capture','clock_source'),('capture','clock_id'),('capture','mapping_id')]
        for path in paths:
            reg=register(manifest());value=reg['deployment']
            for name in path[:-1]:value=value[name]
            del value[path[-1]]
            with self.subTest(path=path),self.assertRaises(ValueError):validate_registration(reg,config())
        reg=register(manifest());del reg['lines'][0]['family']
        with self.assertRaises(ValueError):validate_registration(reg,config())

    def test_declared_vlan_ports_and_external_clock_values_must_be_valid(self):
        mutations=[lambda d:d['nic'].update(pmd='other'),lambda d:d['nic'].update(ports=[0,0]),
            lambda d:d['vlans'].update(cable=300),lambda d:d['vlans'].update(leo=100),
            lambda d:d['vlans'].update(cable=0),lambda d:d['vlans'].update(cable='100'),
            lambda d:d['capture'].update(host='dut'),lambda d:d['generator'].update(host='dut'),
            lambda d:d['capture']['interfaces'].pop('1'),
            lambda d:d['capture'].update(clock_source='${CAPTURE_CLOCK_SOURCE}')]
        for mutate in mutations:
            reg=register(manifest());mutate(reg['deployment'])
            with self.subTest(mutate=mutate),self.assertRaises(ValueError):validate_registration(reg,config())

    def test_preflight_requires_the_declared_capture_source_and_mapping(self):
        from tools.g2.tests.test_pcapng import setup
        from tools.g2.validate import preflight
        for field in ('clock_id','mapping_id','clock_source'):
            with self.subTest(field=field),tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp);setup(root);path=root/'source/manifest.json'
                reg=register(json.loads(path.read_text()));reg['deployment']['capture'][field]='other'
                path.write_text(json.dumps(reg))
                with self.assertRaisesRegex(ValueError,'deployment'):preflight(path)

    def test_capture_pair_cannot_choose_an_undeclared_mapping(self):
        from tools.g2.tests.test_pcapng import setup
        from tools.g2.pcapng import prepare_pair
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);setup(root)
            path=root/'source/manifest.json';path.write_text(json.dumps(register(json.loads(path.read_text()))))
            path=root/'source/calibration.json';cal=json.loads(path.read_text())
            cal['clocks'].append(dict(next(c for c in cal['clocks'] if c['clock_id']=='capture'),clock_id='alias'))
            cal['mappings'].append(dict(cal['mappings'][0],mapping_id='other',raw_clock_id='alias'))
            path.write_text(json.dumps(cal))
            path=root/'pair.json';pair=json.loads(path.read_text());pair.update(clock_id='alias',mapping_id='other')
            path.write_text(json.dumps(pair))
            with self.assertRaisesRegex(ValueError,'deployment'):prepare_pair(path,root/'converted')


if __name__=='__main__':unittest.main()
