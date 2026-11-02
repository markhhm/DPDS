#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Exercise bring-up decisions with hardware-boundary fixtures; never open a NIC."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
ENTRY = ROOT / 'scripts/bringup-check.sh'
PCI = '0000:01:00.0'


class BringupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.sys = self.base / 'sys'
        self.bin = self.base / 'bin'
        self.bin.mkdir()
        self.device = self.sys / 'bus/pci/devices' / PCI
        self.write('bus/pci/devices/' + PCI + '/numa_node', '0')
        self.write('bus/pci/devices/' + PCI + '/class', '0x020000')
        self.write('module/vfio_pci/version', '0.2')
        self.bind('vfio-pci')
        self.cpus = sorted(os.sched_getaffinity(0))[:2]
        for cpu in self.cpus:
            (self.sys / f'devices/system/cpu/cpu{cpu}/node0').mkdir(parents=True)
        self.pages = 'devices/system/node/node0/hugepages/hugepages-2048kB/'
        self.write(self.pages + 'nr_hugepages', '1024')
        self.write(self.pages + 'free_hugepages', '1024')
        self.command('lspci', 'printf "LnkCap: Port #0, Speed 16GT/s, Width x16\\nLnkSta: Speed 8GT/s, Width x8\\n"')
        self.command('findmnt', 'echo \'{"filesystems":[{"target":"/pages","fstype":"hugetlbfs","options":"rw,pagesize=2M"}]}\'')
        self.command('uname', 'echo test-kernel')
        self.probe()
        self.lock = self.base / 'versions.json'
        self.versions = dict(dpdk='26.03.0', pmd='net_ixgbe', firmware='1.2.3',
                             driver='vfio-pci', driver_version='0.2', kernel='test-kernel')
        self.lock.write_text(json.dumps(self.versions))
        self.output = self.base / 'p1-0.json'
        self.env = {**os.environ, 'PATH': str(self.bin) + ':' + os.environ['PATH'],
                    'DPDK_TESTPMD': str(self.bin / 'dpdk-testpmd'),
                    'HOST_REV': '', 'CORE_REV': '',
                    'BRINGUP_SYSFS_ROOT': str(self.sys), 'VERSION_LOCK': str(self.lock),
                    'CONFIG_FILE': str(ROOT / 'config.json'), 'DUT_PCI': PCI,
                    'DUT_NODE': '0', 'EXPECTED_PCIE_GTS': '8', 'EXPECTED_PCIE_WIDTH': '8',
                    'CORE_SET': ','.join(map(str, self.cpus)), 'SOCKET_MEM': '128',
                    'HUGEPAGE_KB': '2048', 'HUGEPAGES_REQUIRED': '64',
                    'HUGEPAGE_MOUNT': '/pages', 'RUN_ID': 'bringup-fixture'}

    def write(self, name, content):
        p = self.sys / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)

    def bind(self, driver):
        target = self.sys / 'bus/pci/drivers' / driver
        target.mkdir(parents=True, exist_ok=True)
        link = self.device / 'driver'
        link.unlink(missing_ok=True)
        link.symlink_to(target)

    def command(self, name, body):
        path = self.bin / name
        path.write_text('#!/bin/sh\n' + body + '\n')
        path.chmod(0o755)

    def probe(self, pci=PCI, pmd='net_ixgbe', firmware='1.2.3'):
        # Wrong allowlist/start flags must not pass just because fixture text matches.
        text = f'DPDK 26.03.0\n********************* Infos for port 0 *********************\nDevice name: {pci}\nDriver name: {pmd}\nFirmware-version: {firmware}\nConnect to socket: 0\n'
        self.command('dpdk-testpmd',
                     'case " $* " in *" --in-memory "*) ;; *) exit 31;; esac\n'
                     'case " $* " in *" -a ' + PCI + ' "*) ;; *) exit 32;; esac\n'
                     'case " $* " in *" --disable-device-start "*) ;; *) exit 33;; esac\n'
                     'case " $* " in *" --no-huge "*|*" --no-pci "*|*" --vdev"*) exit 34;; esac\n'
                     'read -r line\n[ "$line" = "show port info all" ] || exit 35\n'
                     'read -r line\n[ "$line" = "quit" ] || exit 36\n'
                     "printf '%s\\n' '" + text + "'")

    def run_check(self):
        self.assertTrue(ENTRY.is_file(), 'bring-up command is missing')
        result = subprocess.run(['sh', str(ENTRY), '--out', str(self.output)],
                                env=self.env, text=True, capture_output=True, timeout=20)
        self.assertNotIn('Traceback', result.stderr)
        doc = json.loads(self.output.read_text())
        return result, doc

    def test_vfio_pass_manifest_has_model_and_unqualified_clock(self):
        result, doc = self.run_check()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.output.stat().st_mode & 0o777, 0o600)
        self.assertEqual((doc['schema'], doc['scope'], doc['status']),
                         ('hng-p1-0-v1', 'software-only', 'PASS'))
        self.assertIsNone(doc['clock_uncertainty_ns'])
        self.assertIsNone(doc['clock_qualification'])
        self.assertFalse(doc['g2_qualified'])
        self.assertEqual(doc['versions']['observed'], self.versions)
        self.assertEqual((doc['lines'][0]['model']['c_bps'], doc['lines'][0]['model']['q_bytes'],
                          doc['lines'][0]['model']['seed']), (8000000, 6400, 1))
        self.assertEqual(doc['byte_accounting']['q_l2_bytes'], 'max(pkt_len + 4, 64)')
        self.assertEqual(doc['byte_accounting']['c_wire_bytes'], 'q_l2_bytes + 20')
        self.assertEqual(doc['numa']['nic_node'], 0)
        self.assertEqual(doc['numa']['hugepages']['free'], 1024)
        self.assertIn('PASS', result.stdout)
        self.assertTrue(all(c['status'] == 'PASS' for c in doc['checks']))

    def test_mlx5_bifurcated_pass(self):
        self.bind('mlx5_core')
        self.write('module/mlx5_core/version', '5.0')
        self.versions.update(driver='mlx5_core', driver_version='5.0', pmd='net_mlx5')
        self.lock.write_text(json.dumps(self.versions))
        self.probe(pmd='net_mlx5')
        result, doc = self.run_check()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(doc['versions']['observed']['driver'], 'mlx5_core')

    def test_missing_device_fails_without_probing(self):
        self.env['DUT_PCI'] = '0000:ff:00.0'
        self.command('dpdk-testpmd', 'touch "' + str(self.base / 'probed') + '"')
        result, doc = self.run_check()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(doc['status'], 'FAIL')
        self.assertFalse((self.base / 'probed').exists())

    def test_unknown_or_degraded_environment_fails(self):
        for name, change in [
            ('negotiated PCIe', lambda: self.env.update(EXPECTED_PCIE_WIDTH='16')),
            ('NUMA', lambda: self.write('bus/pci/devices/' + PCI + '/numa_node', '-1')),
            ('free pages', lambda: self.write(self.pages + 'free_hugepages', '1')),
            ('firmware lock', lambda: self.versions.update(firmware='wrong')),
        ]:
            with self.subTest(name=name):
                self.setUp()
                change()
                self.lock.write_text(json.dumps(self.versions))
                result, doc = self.run_check()
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(doc['status'], 'FAIL')

    def test_wrong_device_no_port_or_unknown_firmware_cannot_pass(self):
        for pci, firmware in [('0000:02:00.0', '1.2.3'), (PCI, 'not available')]:
            with self.subTest(pci=pci, firmware=firmware):
                self.probe(pci=pci, firmware=firmware)
                self.output.unlink(missing_ok=True)
                result, doc = self.run_check()
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(doc['status'], 'FAIL')
        self.command('dpdk-testpmd', 'echo "No probed ethernet devices"')
        self.output.unlink(missing_ok=True)
        result, doc = self.run_check()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(doc['status'], 'FAIL')

    def test_missing_settings_and_malformed_inputs_produce_failed_manifest(self):
        cases = ('missing setting', 'bad lock', 'bad config', 'wrong page size', 'wrong PMD')
        for case in cases:
            with self.subTest(case=case):
                self.setUp()
                if case == 'missing setting':
                    self.env.pop('DUT_NODE')
                elif case == 'bad lock':
                    self.lock.write_text('{')
                elif case == 'bad config':
                    config = self.base / 'bad-config.json'
                    config.write_text('{}')
                    self.env['CONFIG_FILE'] = str(config)
                elif case == 'wrong page size':
                    self.env['HUGEPAGE_KB'] = '1048576'
                else:
                    self.probe(pmd='net_null')
                result, doc = self.run_check()
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(doc['status'], 'FAIL')
                self.assertTrue(any(c['status'] == 'FAIL' for c in doc['checks']))

    def test_manifest_records_actual_core_even_in_git_hook_environment(self):
        git_env = {k: v for k, v in os.environ.items()
                   if k not in ('GIT_DIR', 'GIT_WORK_TREE', 'GIT_INDEX_FILE')}
        core = subprocess.check_output(['git', '-C', str(ROOT / 'dpds-core'),
                                        'rev-parse', 'HEAD'], text=True, env=git_env).strip()
        git_dir = subprocess.check_output(['git', '-C', str(ROOT), 'rev-parse',
                                           '--absolute-git-dir'], text=True, env=git_env).strip()
        self.env.update(GIT_DIR=git_dir, GIT_WORK_TREE=str(ROOT))
        result, doc = self.run_check()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(doc['sources']['core']['revision'], core)
        self.assertEqual(doc['sources']['config_sha256'],
                         hashlib.sha256((ROOT / 'config.json').read_bytes()).hexdigest())
        self.assertEqual(doc['sources']['version_lock_sha256'],
                         hashlib.sha256(self.lock.read_bytes()).hexdigest())

    def test_missing_mount_evidence_fails_without_traceback(self):
        for value in ('{"filesystems":[]}', '{"filesystems":[null]}', '{"filesystems":null}', 'null'):
            with self.subTest(findmnt=value):
                self.command('findmnt', "printf '%s\\n' '" + value + "'")
                self.output.unlink(missing_ok=True)
                result, doc = self.run_check()
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(doc['status'], 'FAIL')
                self.assertIn('FAIL', result.stdout)

    def test_version_mismatch_or_tool_failure_cannot_pass(self):
        for name in self.versions:
            with self.subTest(version=name):
                old = self.versions[name]
                self.versions[name] = 'different'
                self.lock.write_text(json.dumps(self.versions))
                self.output.unlink(missing_ok=True)
                result, doc = self.run_check()
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(doc['status'], 'FAIL')
                self.versions[name] = old
        self.lock.write_text(json.dumps(self.versions))
        self.command('lspci', 'echo "permission denied" >&2; exit 1')
        self.output.unlink(missing_ok=True)
        result, doc = self.run_check()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(doc['status'], 'FAIL')

    def test_pcie_status_annotations_and_legacy_format(self):
        for annotation in (' (ok)', ' (downgraded)', ''):
            with self.subTest(annotation=annotation):
                self.command('lspci', 'echo "LnkCap: Speed 16GT/s, Width x16\nLnkSta: Speed 8GT/s' + annotation + ', Width x8 (ok)"')
                self.output.unlink(missing_ok=True)
                result, doc = self.run_check()
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(doc['pcie'], dict(negotiated_gts=8.0, negotiated_width=8))

    def test_nonfinite_pcie_output_is_a_failed_check(self):
        self.command('lspci', 'echo "LnkSta: Speed ' + '9' * 400 + 'GT/s, Width x8"')
        result, doc = self.run_check()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(doc['status'], 'FAIL')

    def test_prior_manifest_is_not_overwritten(self):
        self.output.write_text('prior evidence\n')
        self.assertTrue(ENTRY.is_file(), 'bring-up command is missing')
        result = subprocess.run(['sh', str(ENTRY), '--out', str(self.output)], env=self.env,
                                text=True, capture_output=True, timeout=20)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.output.read_text(), 'prior evidence\n')


if __name__ == '__main__':
    unittest.main()
