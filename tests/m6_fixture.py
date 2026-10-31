# SPDX-License-Identifier: Apache-2.0
"""Small PCAP evidence fixtures; timestamp pairs share the DUT software clock."""
from contextlib import ExitStack
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import struct
import subprocess
import sys
import threading
import time

from eal_args import eal_arguments
from observer_export import output_frames, pcap
from startup import wait_for_output
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools.g2.native_adapter import NativeAdapter
from tools.g2.protocol import StreamReader, StreamWriter
from tools.g2.tests.test_calibration import document
from tools.g2.tests.test_counters import OBSERVER_FIELDS, snapshot
from tools.g2.tests.test_registration import manifest


COUNT = 400


def emit(writer, row):
    writer.emit(row['kind'], **{key: value for key, value in row.items()
                               if key not in ('kind', 'run', 'stream_id', 'seq')})


def capture(binary, family, root):
    config = dict(schema='hng-v1', lines=[])
    for port in (0, 1):
        config['lines'].append(dict(name=f'line-{port}', ingress_port=port, egress_port=1-port,
            vlan_id=100, model=dict(c_bps=9_000_000_000, q_bytes=8192,
                t_ns=65_000 if family == 'main' else 0,
                delta_ns=5_000 if family == 'boundary' else 0,
                p='0.1' if family == 'boundary' else '0', seed=17,
                storage_capacity_packets=1024, storage_capacity_bytes=2_000_000),
            envelope=dict(min_q_bytes=64, max_q_bytes=1522, burst_bound_packets=16), trace=None))
    if family == 'trace':
        profile = dict(end_ns=2_000_000_000, terminal='error', terminal_horizon_ns=None,
            outage_action='hold', loss_seed=17,
            rows=[dict(at_ns=0, t_ns=5_000, loss='0', outage=False),
                  dict(at_ns=10_000_000, t_ns=1_000, loss='0', outage=False)])
        trace = dict(schema='hng-trace-v1', source_id='synthetic-m6-smoke', direction='both',
            units='ns_owd', conversion='exact-ns', source_sha256=hashlib.sha256(json.dumps(profile).encode()).hexdigest(),
            splice_policy='natural_hold', trace_epoch_ns=0, time_scale=1, profile=profile)
        raw = json.dumps(trace).encode()
        (root / 'trace.json').write_bytes(raw)
        for line in config['lines']:
            line['trace'] = dict(path='trace.json', sha256=hashlib.sha256(raw).hexdigest())
    config_path = root / 'engine.json'
    config_path.write_text(json.dumps(config))
    frames = {}
    for port in (0, 1):
        packets = []
        for seq in range(COUNT):
            frame = bytearray(76)
            frame[:6] = b'\xff'*6
            frame[6:12] = bytes((2, 0, 0, 0, 0, port))
            frame[12:18] = bytes((0x81, 0, 0, 100, 0x88, 0xb5))
            struct.pack_into('<QQQ', frame, 18, port*COUNT+seq, seq, seq)
            frames[port, f'software-l2-{port}:{port*COUNT+seq}'] = bytes(frame)
            packets.append(frame)
        pcap(root / f'in-{port}.pcap', packets)
    reader, writer = os.pipe()
    def spool():
        with os.fdopen(reader, 'rb') as source, (root / 'native.jsonl').open('wb') as target:
            shutil.copyfileobj(source, target, 65536)
    thread = threading.Thread(target=spool)
    thread.start()
    command = [binary] + eal_arguments(2) + ['--no-pci', '--no-huge', '--no-telemetry',
        '-m', '256', '--in-memory', f'--file-prefix=hng-m6-{os.getpid()}-{family}']
    command += [f'--vdev=net_pcap{p},rx_pcap={root}/in-{p}.pcap,tx_pcap={root}/out-{p}.pcap' for p in (0, 1)]
    command += ['--', '-c', str(config_path), '--observe-fd', str(writer),
                '--observe-software', '--observe-identity', 'software-l2']
    process = subprocess.Popen(command, pass_fds=(writer,), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                               text=True)
    os.close(writer)
    try:
        prefix = wait_for_output(process, b'layout=2-lcore')
        time.sleep(0.25)
        process.send_signal(signal.SIGINT)
        output, _ = process.communicate(timeout=10)
        output = prefix + output
        (root / 'dut.log').write_text(output)
        assert process.returncode == 0, output
        assert 'status=DRAINED' in output and f'tx_accepted={2*COUNT} ' in output, output
        assert 'conservation=PASS byte_conservation=PASS' in output and output.count('owned=0') == 2, output
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate()
        thread.join(timeout=3)
    assert not thread.is_alive(), 'native descriptor remained open'
    with ExitStack() as stack:
        source = stack.enter_context((root / 'native.jsonl').open())
        targets = [stack.enter_context((root / f'{role}.jsonl').open('w')) for role in ('ingress', 'host', 'adapter-counters')]
        result = NativeAdapter(source, *targets, run=family, lines=config['lines']).process()
    assert result['complete'], result
    with (root / 'expected.jsonl').open('w') as target:
        reference = subprocess.run([sys.executable, '-m', 'tools.g2.reference', str(config_path), family,
                                    str(root / 'ingress.jsonl')], stdout=target, stderr=subprocess.PIPE, text=True)
    assert reference.returncode == 0, reference.stderr
    expected = [row for row in StreamReader(root / 'expected.jsonl', role='expected') if row['kind'] == 'expected']
    assert len(expected) == 2*COUNT and all(row['outcome'] == 'accepted' for row in expected), expected[-1:]
    by_identity = {(row['ingress_port'], row['external_identity']): row for row in expected}
    accepted = {}
    for row in StreamReader(root / 'host.jsonl', role='host'):
        if row['kind'] == 'attempt':
            for item in row['offered'][:row['accepted_count']]:
                key = item['ingress_port'], item['external_identity']
                assert key not in accepted, key
                accepted[key] = row['submit_after_ns']
    assert set(accepted) == set(by_identity), 'actual accepted identities differ from Fraction oracle'
    writers = {role: StreamWriter(root / f'{role}.jsonl', family, role, role, 'obs')
               for role in ('before', 'receiver')}
    before_seq = {0: 0, 1: 0}
    for row in expected:
        port = row['ingress_port']
        common = {key: row[key] for key in ('ingress_port', 'egress_port', 'line', 'external_identity',
                                           'flow_id', 'length', 'vlan_id', 'app', 'selected_late')}
        writers['before'].emit('before', **common, before_seq=before_seq[port], ingress_ts_ns=row['arrival_ns'],
            timestamp=dict(raw=row['arrival_ns'], clock_id='before', mapping_id='b', point='fake', uncertainty_ns=0))
        before_seq[port] += 1
    received = set()
    for port in (0, 1):
        packets = output_frames(root / f'out-{port}.pcap')
        assert len(packets) == COUNT, (family, port, len(packets))
        for seq, frame in enumerate(packets):
            identity = struct.unpack_from('<Q', frame, 18)[0]
            key = 1-port, f'software-l2-{1-port}:{identity}'
            assert key not in received and frame == frames[key], 'duplicate or changed output frame'
            received.add(key)
            row = by_identity[key]
            common = {name: row[name] for name in ('ingress_port', 'egress_port', 'line', 'external_identity',
                                                  'flow_id', 'length', 'vlan_id', 'app', 'selected_late')}
            # These are same-engine fixtures, not independent receiver timestamps.
            at = accepted[key]
            writers['receiver'].emit('receive', **common, receive_seq=seq, receive_ns=at,
                timestamp=dict(raw=at, clock_id='after', mapping_id='a', point='fake', uncertainty_ns=0))
    assert received == set(by_identity)
    for writer in writers.values():
        writer.finish()
    counters = list(StreamReader(root / 'adapter-counters.jsonl', role='counters'))
    last = max(row['at_ns'] for row in counters)
    for role, writer in writers.items():
        for phase in ('start', 'end'):
            values = dict.fromkeys(OBSERVER_FIELDS, 0)
            values['queue_capacity'] = 4096
            if phase == 'end':
                values.update(records_attempted=2*COUNT, records_emitted=2*COUNT)
            counters.append(snapshot('observer', role, phase, values, at_ns=0 if phase == 'start' else last))
    counter_writer = StreamWriter(root / 'counters.jsonl', family, 'native-counters', 'counters', 'engine')
    for row in counters:
        emit(counter_writer, row)
    counter_writer.finish()
    registration = manifest()
    registration.update(run=family, condition_id=f'm6-{family}',
        ports=[dict(registration['ports'][0], ingress_port=p, egress_port=1-p, link_bps=10_000_000_000) for p in (0, 1)],
        lines=[dict(name=f'line-{p}', ingress_port=p, egress_port=1-p, vlan_id=100, apps=[]) for p in (0, 1)],
        clocks=dict(engine='engine', observation={'0': 'obs', '1': 'obs'}),
        required_cells=[dict(condition_id=f'm6-{family}', repeat=0, direction=str(p), line=f'line-{p}',
            phase='steady', mask_bits=0, planned_survivors=COUNT) for p in (0, 1)])
    registration['sampling']['oversample_apps'] = []
    registration['capacity'].update(identity_index_capacity=2*COUNT+10, max_unresolved=2*COUNT+10,
        handoff_queues=[dict(handoff_queue_id=f'handoff-{p}', producer=f'worker-{p}', consumer='tx', capacity=4096)
                        for p in (0, 1)])
    registration['identity_profile'] = dict(kind='packet_occurrence', occurrence_policy='software-l2-identity',
                                           policy_sha256=hashlib.sha256(b'software-l2-identity').hexdigest())
    registration['config']['sha256'] = hashlib.sha256(config_path.read_bytes()).hexdigest()
    registration['traces'] = [dict(line=line['name'], **line['trace'], provenance='synthetic-m6-smoke')
                              for line in config['lines'] if line['trace']]
    for stream in registration['streams']:
        path = root / stream['path']
        stream['file_sha256'] = hashlib.sha256(path.read_bytes()).hexdigest()
        with path.open() as source:
            stream['stream_id'] = json.loads(source.readline())['stream_id']
    calibration = document()
    calibration['run'] = family
    calibration['clocks'].append(dict(clock_id='engine', kind='fake', unit='ns', width_bits=64,
                                      device_token='same-engine', mode='synthetic'))
    for mapping in calibration['mappings']:
        mapping.update(scale_num=1, scale_den=1, target_origin_ns=0, valid_to_raw=10**12, residual_bound_ns=0)
    for key in ('offset_ns', 'offset_bound_ns', 'drift_bound_ns', 'quantization_bound_ns', 'load_bound_ns',
                'mapping_bound_ns', 'uncertainty_ns'):
        calibration['pairs'][0][key] = 0
    (root / 'calibration.json').write_text(json.dumps(calibration))
    (root / 'manifest.json').write_text(json.dumps(registration))
