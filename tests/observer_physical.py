#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Physical opt-in gate over mocked device buses; no physical hardware is used."""
import io
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
from eal_args import eal_arguments
from startup import wait_for_output
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools.g2.native_binary import decode
from tools.g2.native_adapter import NativeAdapter


def main(host, fixture):
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        config = root/'engine.json'
        lines = [dict(name=f'line-{p}', ingress_port=p, egress_port=1-p, vlan_id=100,
            model=dict(c_bps=1000000000, q_bytes=512, t_ns=1000, delta_ns=0, p='0', seed=1,
                       storage_capacity_packets=32, storage_capacity_bytes=64000),
            envelope=dict(min_q_bytes=64, max_q_bytes=1522, burst_bound_packets=4), trace=None) for p in (0,1)]
        config.write_text(json.dumps(dict(schema='hng-v1', lines=lines)))
        capture = root/'native.bin'
        eal = eal_arguments(2) + ['--no-pci', '--no-huge', '--no-telemetry', '-m', '256', '--in-memory',
            f'--file-prefix=hng-physical-{os.getpid()}', '--vdev=net_null0,no-rx=1', '--vdev=net_null1,no-rx=1',
            '--', '-c', str(config)]
        physical = ['--observe-binary', str(capture), '--observe-physical', '--observe-identity', 'stage1']
        cases = [
            (host, physical+['--observe-software'], {}, 'mutually exclusive'),
            (host, ['--observe-fd', '999999', '--observe-physical', '--observe-identity', 'stage1'], {}, 'requires binary capture and stage1'),
            (host, physical+['--observe-identity', 'software-l2'], {}, 'requires binary capture and stage1'),
            (host, physical, {}, 'requires supported mlx5 PCI/auxiliary devices'),
            (fixture, physical, {'HNG_TEST_MIXED':'1'}, 'requires supported mlx5 PCI/auxiliary devices'),
            (fixture, physical, {'HNG_TEST_BUS':'virtual'}, 'requires supported mlx5 PCI/auxiliary devices'),
            (fixture, physical, {'HNG_TEST_DRIVER':'net_vmxnet3'}, 'requires supported mlx5 PCI/auxiliary devices'),
            (fixture, physical, {'HNG_TEST_DRIVER':'net_virtio'}, 'requires supported mlx5 PCI/auxiliary devices'),
            (fixture, physical, {'HNG_TEST_DRIVER':'mlx5_auxiliary'}, 'requires supported mlx5 PCI/auxiliary devices'),
            (fixture, physical, {'HNG_TEST_DRIVER':'unknown'}, 'requires supported mlx5 PCI/auxiliary devices'),
            (fixture, ['--observe-binary', str(capture), '--observe-software', '--observe-identity', 'stage1'], {}, 'supports only pcap/ring/null'),
        ]
        for executable, args, extra, reason in cases:
            result = subprocess.run([executable, *eal, *args], capture_output=True, text=True,
                env=dict(os.environ, **extra), timeout=10)
            output = result.stdout+result.stderr
            assert result.returncode != 0 and reason in output, output[-8000:]
            assert not capture.exists(), output[-8000:]
        for bus in ('pci', 'auxiliary'):
            process = subprocess.Popen([fixture, *eal, *physical], stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, env=dict(os.environ, HNG_TEST_BUS=bus))
            try:
                prefix = wait_for_output(process, b'layout=2-lcore')
                process.send_signal(signal.SIGINT)
                output, _ = process.communicate(timeout=10)
                assert process.returncode == 0, (prefix+output)[-8000:]
            finally:
                if process.poll() is None:
                    process.kill(); process.wait()
            text = io.StringIO()
            with capture.open('rb') as source:
                health = decode(source, text)
            assert health['complete'], health
            header = json.loads(text.getvalue().splitlines()[0])
            assert header['capture_scope'] == 'physical' and header['identity_policy'] == 0, header
            streams = [io.StringIO() for _ in range(3)]
            state = NativeAdapter(io.StringIO(text.getvalue()), *streams, run='mock-physical', lines=lines).process()
            assert state['complete'] and state['scope'] == 'physical' and not state['g2_qualified'], state
            capture.unlink()
        print('physical opt-in: explicit binary/stage1, all-port device gates, scope and zero-traffic drain PASS (mock buses only)')


if __name__ == '__main__':
    main(*map(lambda value: str(Path(value).resolve()), sys.argv[1:3]))
