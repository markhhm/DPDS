#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Record M5 environment checks; never bind devices, start ports or forward traffic."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools.g2.reference import Reference, load_config, strict_json


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def cpus(text):
    result = set()
    for part in text.split(','):
        if not re.fullmatch(r'\d+(-\d+)?', part):
            raise ValueError('CORE_SET requires CPU numbers/ranges')
        bounds = list(map(int, part.split('-')))
        lo, hi = bounds[0], bounds[-1]
        if not 0 <= lo <= hi <= 65535:
            raise ValueError('invalid CPU range')
        result.update(range(lo, hi + 1))
    if len(result) < 2 or not result <= os.sched_getaffinity(0):
        raise ValueError('CORE_SET requires at least two allowed CPUs')
    return sorted(result)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', required=True, type=Path, help='new private P1-0 manifest')
    args = parser.parse_args()
    # Reserve the output before any probe; never overwrite a prior attempt.
    try:
        output = os.fdopen(os.open(args.out, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'w')
    except OSError as exc:
        print('FAIL output:', exc, file=sys.stderr)
        return 1
    sysfs = Path(os.environ.get('BRINGUP_SYSFS_ROOT', '/sys'))
    manifest = dict(schema='hng-p1-0-v1', stage='bringup-preflight',
                    scope='software-only' if 'BRINGUP_SYSFS_ROOT' in os.environ else 'physical',
                    status='FAIL', g2_qualified=False, run=os.environ.get('RUN_ID'),
                    clock_uncertainty_ns=None, clock_qualification=None,
                    versions=dict(observed={}, locked={}), sources={}, numa={}, lines=[],
                    byte_accounting=dict(q_l2_bytes='max(pkt_len + 4, 64)',
                                         c_wire_bytes='q_l2_bytes + 20',
                                         q_includes_in_service=True,
                                         pkt_len_excludes_fcs=True,
                                         service_rounding='ceil(wire_bytes * 8 * 1000000000 / C_bps)'),
                    checks=[], commands=[], started_utc=datetime.now(timezone.utc).isoformat())
    observed = manifest['versions']['observed']

    def check(name, action):
        try:
            value = action()
            manifest['checks'].append(dict(check=name, status='PASS',
                                           detail=value if isinstance(value, str) else 'verified'))
            return value
        except (OSError, ValueError, KeyError, TypeError, OverflowError, subprocess.SubprocessError) as exc:
            manifest['checks'].append(dict(check=name, status='FAIL', detail=str(exc)))
            return None

    def run(argv, data=None):
        entry = dict(argv=list(map(str, argv)))
        manifest['commands'].append(entry)
        env = {**os.environ, 'LC_ALL': 'C'}
        if entry['argv'][0] == 'git':
            for key in ('GIT_DIR', 'GIT_WORK_TREE', 'GIT_INDEX_FILE'):
                env.pop(key, None)
        try:
            result = subprocess.run(entry['argv'], input=data, capture_output=True,
                                    text=True, timeout=30, env=env)
        except (OSError, subprocess.SubprocessError) as exc:
            entry['error'] = str(exc)
            raise
        entry.update(exit_status=result.returncode, stdout=result.stdout, stderr=result.stderr)
        if result.returncode:
            raise ValueError(f'{argv[0]} exited {result.returncode}: {result.stderr.strip()}')
        return result.stdout + result.stderr

    def require(condition, message):
        if not condition:
            raise ValueError(message)

    def settings():
        names = ('DUT_PCI', 'DUT_NODE', 'EXPECTED_PCIE_GTS', 'EXPECTED_PCIE_WIDTH',
                 'CORE_SET', 'SOCKET_MEM', 'HUGEPAGE_KB', 'HUGEPAGES_REQUIRED',
                 'HUGEPAGE_MOUNT', 'VERSION_LOCK', 'CONFIG_FILE', 'RUN_ID')
        values = {key: os.environ.get(key, '') for key in names}
        require(all(values.values()), 'missing environment: ' + ', '.join(k for k, v in values.items() if not v))
        require(re.fullmatch(r'[0-9a-fA-F]{4}:[0-9a-fA-F]{2}:[0-9a-fA-F]{2}\.[0-7]', values['DUT_PCI']), 'invalid DUT_PCI')
        values['DUT_PCI'] = values['DUT_PCI'].lower()
        for key in ('DUT_NODE', 'EXPECTED_PCIE_WIDTH', 'HUGEPAGE_KB', 'HUGEPAGES_REQUIRED'):
            values[key] = int(values[key])
            require(values[key] >= (0 if key == 'DUT_NODE' else 1), f'invalid {key}')
        require(re.fullmatch(r'\d+(\.\d+)?', values['EXPECTED_PCIE_GTS']), 'invalid EXPECTED_PCIE_GTS')
        speed = float(values['EXPECTED_PCIE_GTS'])
        require(math.isfinite(speed) and speed > 0, 'invalid EXPECTED_PCIE_GTS')
        require(re.fullmatch(r'\d+(,\d+)*', values['SOCKET_MEM']), 'invalid SOCKET_MEM MiB vector')
        values['memory_mib'] = list(map(int, values['SOCKET_MEM'].split(',')))
        node = values['DUT_NODE']
        require(node < len(values['memory_mib']) and values['memory_mib'][node] > 0, 'SOCKET_MEM has no NIC-local allocation')
        values['cpus'] = cpus(values['CORE_SET'])
        manifest['deployment'] = values
        return values

    cfg = check('deployment', settings)

    def need_settings():
        require(cfg is not None, 'deployment settings unavailable')
        return sysfs / 'bus/pci/devices' / cfg['DUT_PCI']

    def source_model():
        need_settings()
        config, traces = load_config(cfg['CONFIG_FILE'])
        Reference(config, cfg['RUN_ID'], traces)
        manifest['lines'] = config['lines']
        manifest['sources'].update(config_sha256=sha(cfg['CONFIG_FILE']),
                                   cargo_lock_sha256=sha(ROOT / 'dpds-core/Cargo.lock'))
        for name, path in (('host', ROOT), ('core', ROOT / 'dpds-core')):
            revision = run(['git', '-C', path, 'rev-parse', 'HEAD']).strip()
            dirty = bool(run(['git', '-C', path, 'status', '--porcelain']).strip())
            manifest['sources'][name] = dict(revision=revision, dirty=dirty)
            expected = os.environ.get(name.upper() + '_REV')
            require(not expected or revision == expected, f'{name} revision differs from registered revision')

    check('model/source', source_model)

    def lock_versions():
        need_settings()
        locked = strict_json(Path(cfg['VERSION_LOCK']).read_bytes())
        keys = {'dpdk', 'pmd', 'firmware', 'driver', 'driver_version', 'kernel'}
        require(isinstance(locked, dict) and set(locked) == keys, 'version lock requires exactly: ' + ', '.join(sorted(keys)))
        require(all(isinstance(v, str) and v.strip() and v not in ('unknown', 'not available', 'not-applicable') for v in locked.values()), 'unknown or empty locked version')
        manifest['versions']['locked'] = locked
        manifest['sources']['version_lock_sha256'] = sha(cfg['VERSION_LOCK'])
        return locked

    locked = check('version lock', lock_versions)

    def device():
        path = need_settings()
        require(path.is_dir(), 'PCI device is absent: ' + cfg['DUT_PCI'])
        require(int((path / 'class').read_text().strip(), 16) >> 16 == 2, 'selected PCI device is not a network controller')
        driver = (path / 'driver').resolve(strict=True).name
        require(driver in ('vfio-pci', 'mlx5_core'), 'expected vfio-pci or mlx5_core bifurcated binding, observed ' + driver)
        observed['driver'] = driver
        kernel = run(['uname', '-r']).strip()
        require(bool(kernel), 'kernel version unavailable')
        observed['kernel'] = kernel
        version = sysfs / 'module' / driver.replace('-', '_') / 'version'
        observed['driver_version'] = version.read_text().strip() if version.exists() else 'kernel:' + kernel
        require(bool(observed['driver_version']), 'driver version unavailable')
        return path

    dev = check('NIC/binding', device)

    def pcie():
        require(dev is not None, 'NIC unavailable')
        text = run(['lspci', '-D', '-vv', '-s', cfg['DUT_PCI']])
        sta = re.search(r'\bLnkSta:\s*Speed\s*([\d.]+)GT/s(?:\s*\([^)]*\))?,\s*Width\s*x(\d+)', text)
        require(sta is not None, 'negotiated PCIe LnkSta unavailable (permissions/capability)')
        speed, width = float(sta[1]), int(sta[2])
        require(math.isfinite(speed) and speed > 0 and width > 0, 'invalid negotiated PCIe rate/width')
        manifest['pcie'] = dict(negotiated_gts=speed, negotiated_width=width)
        require(speed == float(cfg['EXPECTED_PCIE_GTS']) and width == cfg['EXPECTED_PCIE_WIDTH'],
                f'negotiated {speed:g} GT/s x{width} differs from expected ' + cfg['EXPECTED_PCIE_GTS'] + ' GT/s x' + str(cfg['EXPECTED_PCIE_WIDTH']))
        return f'{speed:g} GT/s x{width}'

    check('PCIe negotiated', pcie)

    def numa():
        require(dev is not None, 'NIC unavailable')
        node = int((dev / 'numa_node').read_text())
        require(node >= 0 and node == cfg['DUT_NODE'], 'unknown or unexpected NIC NUMA node')
        placement = {}
        for cpu in cfg['cpus']:
            nodes = list((sysfs / f'devices/system/cpu/cpu{cpu}').glob('node[0-9]*'))
            require(len(nodes) == 1, f'CPU {cpu} NUMA node unavailable')
            placement[str(cpu)] = int(nodes[0].name[4:])
        manifest['numa'].update(nic_node=node, cpu_nodes=placement,
                                cross_numa=any(n != node for n in placement.values()),
                                socket_mem_mib=cfg['memory_mib'])
        return f'NIC node{node}; CPU nodes {placement}'

    check('NUMA placement', numa)

    def hugepages():
        need_settings()
        counts = {}
        for node, mib in enumerate(cfg['memory_mib']):
            if not mib:
                continue
            pool = sysfs / f'devices/system/node/node{node}/hugepages/hugepages-{cfg["HUGEPAGE_KB"]}kB'
            total, free = (int((pool / f).read_text()) for f in ('nr_hugepages', 'free_hugepages'))
            needed = max(cfg['HUGEPAGES_REQUIRED'] if node == cfg['DUT_NODE'] else 0,
                         (mib * 1024 + cfg['HUGEPAGE_KB'] - 1) // cfg['HUGEPAGE_KB'])
            counts[str(node)] = dict(total=total, free=free, required=needed)
            require(0 <= needed <= free <= total, f'node{node} has insufficient free hugepages')
        mounts = strict_json(run(['findmnt', '-J', '-T', cfg['HUGEPAGE_MOUNT'], '-o', 'TARGET,FSTYPE,OPTIONS']))
        require(isinstance(mounts, dict) and isinstance(mounts.get('filesystems'), list)
                and len(mounts['filesystems']) == 1 and isinstance(mounts['filesystems'][0], dict),
                'expected one mount record from findmnt')
        mount = mounts['filesystems'][0]
        require(mount['fstype'] == 'hugetlbfs', 'hugepage mount is not hugetlbfs')
        match = re.search(r'(?:^|,)pagesize=(\d+)([KMG])(?:B)?(?:,|$)', mount['options'], re.I)
        if match:
            kb = int(match[1]) * {'K': 1, 'M': 1024, 'G': 1048576}[match[2].upper()]
        else:
            meminfo = Path('/proc/meminfo').read_text()
            kb = int(re.search(r'^Hugepagesize:\s+(\d+) kB', meminfo, re.M)[1])
        require(kb == cfg['HUGEPAGE_KB'], 'hugetlbfs page size differs from registered size')
        manifest['numa']['hugepages'] = dict(size_kb=kb, mount=mount,
                                            nodes=counts, **counts[str(cfg['DUT_NODE'])])
        return f'{kb} KiB; per-node counts {counts}'

    check('hugepages', hugepages)
    # Probe only after binding, locality, lock and resource prerequisites passed.
    prerequisites = all(row['status'] == 'PASS' for row in manifest['checks'])

    def probe():
        require(prerequisites, 'probe withheld: prerequisite checks failed')
        text = run([os.environ.get('DPDK_TESTPMD', 'dpdk-testpmd'), '-v', '-l', cfg['CORE_SET'],
                    '-a', cfg['DUT_PCI'], '--in-memory', '--no-telemetry',
                    '--file-prefix=hng-bringup-' + str(os.getpid()),
                    '--huge-dir=' + cfg['HUGEPAGE_MOUNT'], '--socket-mem=' + cfg['SOCKET_MEM'],
                    '--', '-i', '--disable-device-start', '--no-mlockall', '--nb-cores=1'],
                   'show port info all\nquit\n')
        version = re.search(r'\bDPDK\s+(\d+\.\d+(?:\.\d+)?[^\s]*)', text)
        require(version is not None, 'runtime DPDK version unavailable')
        observed['dpdk'] = version[1]
        ports = []
        for block in re.split(r'\*+ Infos for port\s+\d+ \*+', text)[1:]:
            info = dict(re.findall(r'^(Device name|Driver name|Firmware-version):\s*(.+)$', block, re.M))
            name = info.get('Device name', '')
            if re.fullmatch(re.escape(cfg['DUT_PCI']) + r'(?:_representor_\S+)?', name):
                ports.append(info)
        require(bool(ports), 'DPDK exposed no port for the selected PCI device')
        require(len({(p.get('Driver name'), p.get('Firmware-version')) for p in ports}) == 1,
                'inconsistent driver/firmware across selected ports')
        observed['pmd'] = ports[0].get('Driver name', '')
        observed['firmware'] = ports[0].get('Firmware-version', '')
        require(observed['firmware'] not in ('', 'not available', 'unknown'), 'NIC firmware unavailable')
        require(observed['driver'] != 'mlx5_core' or observed['pmd'] in ('mlx5_pci', 'net_mlx5'),
                'mlx5 bifurcated device was not probed by an mlx5 PCI PMD')
        manifest['ports'] = ports
        return f"{cfg['DUT_PCI']}: {len(ports)} port(s), {observed['pmd']}"

    check('DPDK port probe', probe)

    def version_match():
        require(locked is not None, 'version lock unavailable')
        for key, value in locked.items():
            require(observed.get(key) == value, f'{key}: expected {value!r}, observed {observed.get(key)!r}')
        return ', '.join(f'{key}={value}' for key, value in observed.items())

    check('fixed versions', version_match)
    manifest['status'] = 'PASS' if all(r['status'] == 'PASS' for r in manifest['checks']) else 'FAIL'
    manifest['finished_utc'] = datetime.now(timezone.utc).isoformat()
    with output:
        json.dump(manifest, output, indent=2, sort_keys=True, allow_nan=False)
        output.write('\n')
    print('CHECK                     STATUS  DETAIL')
    for row in manifest['checks']:
        print(f'{row["check"]:25} {row["status"]:6}  {row["detail"]}')
    print(f'{manifest["status"]} bringup-preflight ({manifest["scope"]}); manifest: {args.out}')
    return 0 if manifest['status'] == 'PASS' else 1


if __name__ == '__main__':
    sys.exit(main())
