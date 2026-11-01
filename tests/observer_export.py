#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Native exporter over software PCAP PMDs, including disabled-path equivalence."""
import io
import json
import os
from pathlib import Path
import signal
import struct
import subprocess
import sys
import tempfile
import threading
import time
from eal_args import eal_arguments
from startup import wait_for_output
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools.g2.native_adapter import NativeAdapter
from tools.g2.native_binary import decode
from tools.g2.protocol import StreamReader
from tools.g2.reference import Reference
from tools.g2.counters import normalize_counters


def pcap(path, frames):
    with path.open('wb') as target:
        target.write(struct.pack('<IHHIIII', 0xa1b2c3d4, 2, 4, 0, 0, 65535, 1))
        for frame in frames:
            target.write(struct.pack('<IIII', 0, 0, len(frame), len(frame)))
            target.write(frame)


def frames(port):
    result = []
    for i in range(4):
        d = bytearray(76)
        d[:6] = b'\xff' * 6
        d[6:12] = bytes((2, 0, 0, 0, 0, port))
        d[12:18] = bytes((0x81, 0, 0, 100, 0x88, 0xb5))
        struct.pack_into('<QQQ', d, 18, port * 4 + i, i * 1000, i)
        result.append(bytes(d))
    return result


def output_frames(path):
    data = path.read_bytes()
    result = []
    offset = 24
    while offset < len(data):
        _, _, size, wire = struct.unpack_from('<IIII', data, offset)
        assert size == wire
        offset += 16
        result.append(data[offset:offset + size])
        offset += size
    return result


def run(executable, cores, enabled, directory, binary=False):
    inputs = [directory / f'in-{p}.pcap' for p in (0, 1)]
    outputs = [directory / f'out-{cores}-{enabled}-{binary}-{p}.pcap' for p in (0, 1)]
    for p in (0, 1):
        pcap(inputs[p], frames(p))
    config = directory / 'engine.json'
    config.write_text(json.dumps(dict(schema='hng-v1', lines=[dict(
        name=f'line-{p}', ingress_port=p, egress_port=1-p, vlan_id=100,
        model=dict(c_bps=1000000000, q_bytes=512, t_ns=1000, delta_ns=0, p='0', seed=1,
                   storage_capacity_packets=32, storage_capacity_bytes=64000),
        envelope=dict(min_q_bytes=64, max_q_bytes=1522, burst_bound_packets=4), trace=None) for p in (0, 1)])))
    reader, writer = os.pipe()
    rows = []
    def consume():
        with os.fdopen(reader, 'r') as source:
            rows.extend(json.loads(line) for line in source)
    thread = threading.Thread(target=consume)
    thread.start()
    capture = directory / f'native-{cores}-{binary}.bin'
    command = [executable] + eal_arguments(cores) + ['--no-pci', '--no-huge', '--no-telemetry', '-m', '256', '--in-memory',
        f'--file-prefix=hng-observe-{os.getpid()}-{cores}-{enabled}']
    command += [f'--vdev=net_pcap{p},rx_pcap={inputs[p]},tx_pcap={outputs[p]}' for p in (0, 1)]
    command += ['--', '-c', str(config)]
    if enabled:
        command += (['--observe-binary', str(capture)] if binary else ['--observe-fd', str(writer)])
        command += ['--observe-software', '--observe-identity', 'software-l2']
    process = subprocess.Popen(command, pass_fds=(writer,), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    os.close(writer)
    try:
        prefix = wait_for_output(process, f'layout={cores}-lcore'.encode())
        time.sleep(0.05)
        process.send_signal(signal.SIGINT)
        output, _ = process.communicate(timeout=10)
        output = prefix + output
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate()
    thread.join(timeout=3)
    assert not thread.is_alive(), 'export descriptor was not closed'
    assert process.returncode == 0, output
    assert 'rx=8 ' in output and 'tx_accepted=8 ' in output, output
    assert 'status=DRAINED' in output and output.count('owned=0') == 2, output
    for p in (0, 1):
        assert output_frames(outputs[p]) == frames(1-p), output
    if enabled:
        if binary:
            decoded = io.StringIO()
            with capture.open('rb') as source:
                health = decode(source, decoded)
            assert health['complete'], health
            rows = [json.loads(line) for line in decoded.getvalue().splitlines()]
        assert rows[0]['schema'] == 'hng-native-v2'
        assert rows[-1]['kind'] == 'health' and rows[-1]['complete'], rows[-1]
        events = rows[1:-1]
        assert [r['order'] for r in events] == list(range(len(events)))
        assert sum(r['kind'] == 0 for r in events) == 8
        assert sum(r['kind'] == 1 for r in events) == 8
        assert sum(r['kind'] == 5 for r in events) == 8
        assert sum(r['kind'] == 4 for r in events) == 2
        attempts = [r for r in events if r['kind'] == 6]
        assert sum(r['accepted_count'] for r in attempts) == 8
        assert rows[0]['ports'] == [0, 1] and len(rows[-1]['producers']) == 7
        assert rows[0]['layout'] == ('per-direction' if cores == 7 else 'shared')
        if cores == 7:
            for owner, rx, tx in ((0, 0, 3), (1, 5, 6)):
                assert sum(r['producer'] == rx and r['kind'] == 0 for r in events) == 4
                assert any(r['producer'] == rx and r['kind'] == 4 and r['port'] == owner for r in events)
                owned = [r for r in attempts if r['producer'] == tx]
                assert owned and sum(r['accepted_count'] for r in owned) == 4
                assert all(r['port'] == 1-owner and r['consumed_dispatch'][1-owner] == 0 for r in owned)
                assert max(r['consumed_dispatch'][owner] for r in owned) == 4
        assert all(r['gate_ns'] <= r['submit_before_ns'] <= r['submit_after_ns'] for r in attempts)
        queues = {r['handoff_queue'] for r in events if r['kind'] == 5}
        assert queues == ({0, 1} if cores in (5, 7) else {0}), queues
        streams = [io.StringIO() for _ in range(3)]
        config_value = json.loads(config.read_text())
        result = NativeAdapter(io.StringIO(''.join(json.dumps(r)+'\n' for r in rows)), *streams,
                               run='software-pmd', lines=config_value['lines']).process()
        assert result['complete'], result
        validated = []
        for role, stream in zip(('ingress', 'host', 'counters'), streams):
            path = directory / f'{role}.jsonl'
            path.write_text(stream.getvalue())
            reader = StreamReader(path, role=role)
            validated.append(list(reader))
            assert reader.integrity['complete'], reader.integrity
        oracle = Reference(config_value, 'software-pmd')
        expected = [decision for event in validated[0] if (decision := oracle.consume(event)) is not None]
        assert len(expected) == 8 and all(x['outcome'] == 'accepted' for x in expected)
        assert oracle.finish()['input_complete']
        manifest = dict(run='software-pmd', scope='software-only',
            capacity=dict(max_unresolved=64), ports=[dict(ingress_port=p) for p in (0, 1)],
            streams=[dict(stream_id=f'native-{role}', role=role) for role in ('ingress', 'host', 'counters')])
        counter_result = normalize_counters(validated[2], manifest)
        assert counter_result['measurement_status'] == 'PASS', counter_result
        assert all('native_observer_v2' in r.get('extensions', {}) for r in validated[1])
        print(f'native export {cores} lcores binary={binary} complete custody and preserved frame bytes: PASS')
    else:
        assert rows == []


def reject_binary_options(executable, directory):
    capture = directory / 'rejected.bin'
    config = directory / 'engine.json'
    base = [executable] + eal_arguments(2) + ['--no-pci', '--no-huge', '--no-telemetry', '-m', '256', '--in-memory',
        f'--file-prefix=hng-observe-reject-{os.getpid()}', '--vdev=net_null0', '--vdev=net_null1', '--', '-c', str(config)]
    options = ['--observe-binary', str(capture), '--observe-software', '--observe-identity', 'software-l2']
    cases = [
        (options + ['--observe-fd', '999999'], 'mutually exclusive'),
        (['--observe-fd', '1'] + options, 'mutually exclusive'),
        (['--observe-binary', str(capture), '--observe-identity', 'software-l2'], 'requires --observe-software'),
        (['--observe-binary', str(capture), '--observe-software'], 'requires --observe-software'),
        (options + ['--observe-capacity', '0'], 'Invalid observer capacity/probe port'),
        (options + ['--observe-capacity', '65537'], 'Invalid observer capacity/probe port'),
        (options + ['--observe-probe-port', '0'], 'Invalid observer capacity/probe port'),
        (options + ['--observe-probe-port', '65536'], 'Invalid observer capacity/probe port'),
        (options + ['-c', str(directory / 'missing.json')], 'configuration rejected before RX'),
    ]
    for args, message in cases:
        process = subprocess.run(base + args, capture_output=True, text=True, timeout=10)
        output = process.stdout + process.stderr
        assert process.returncode != 0 and message in output, output
        assert not capture.exists(), output
    capture.write_bytes(b'previous capture must survive')
    process = subprocess.run(base + options, capture_output=True, text=True, timeout=10)
    output = process.stdout + process.stderr
    assert process.returncode != 0 and 'Cannot exclusively create observer binary file' in output, output
    assert capture.read_bytes() == b'previous capture must survive', output
    capture.unlink()
    existing = directory / 'existing.bin'
    existing.write_bytes(b'symlink target must survive')
    capture.symlink_to(existing)
    process = subprocess.run(base + options, capture_output=True, text=True, timeout=10)
    output = process.stdout + process.stderr
    assert process.returncode != 0 and 'Cannot exclusively create observer binary file' in output, output
    assert capture.is_symlink() and existing.read_bytes() == b'symlink target must survive', output
    print('native binary exclusive output and observer preflight: PASS')


if __name__ == '__main__':
    binary = str(Path(sys.argv[1]).resolve())
    with tempfile.TemporaryDirectory() as tmp:
        for cores in (2, 3, 4, 5, 7):
            for enabled in (False, True):
                run(binary, cores, enabled, Path(tmp))
        for cores in (2, 3, 4, 5, 7):
            run(binary, cores, True, Path(tmp), binary=True)
        reject_binary_options(binary, Path(tmp))
