# SPDX-License-Identifier: Apache-2.0
"""Synthetic software-only native/pcap sources shared by both sampled reporters."""
import copy
import hashlib
import json
from pathlib import Path
import struct
import subprocess
import tempfile

from tools.g2.calibration import Calibration
from tools.g2.native_adapter import NativeAdapter
from tools.g2.native_binary import decode
from tools.g2.pcapng import convert_stream
from tools.g2.probe import encode_probe
from tools.g2.protocol import StreamReader, StreamWriter
from tools.g2.reference import Reference, U64, canonical, uint
from tools.g2.registration import planning_target
from tools.g2.seqcheck import ISSUES, METRICS
from tools.g2.tests.test_calibration import document
from tools.g2.tests.test_counters import OBSERVER_FIELDS, snapshot
from tools.g2.tests.test_native_binary import MAGIC, entry, frame, health, line
from tools.g2.tests.test_registration import manifest


FLOW = 'c0000201:c6336402:1111:9000'
END = 10**12


def _json(path, value):
    with path.open('xb') as output:
        output.write(canonical(value) + b'\n')


def _hash(path):
    with path.open('rb') as source:
        return hashlib.file_digest(source, 'sha256').hexdigest()


def _pcapng(path, packets):
    """Write real MTU VLAN probes with 128 captured bytes and healthy ISB data."""
    def block(kind, body):
        size = len(body) + 12
        return struct.pack('<II', kind, size) + body + struct.pack('<I', size)

    def option(code, data):
        return struct.pack('<HH', code, len(data)) + data + bytes(-len(data) % 4)

    with path.open('xb') as output:
        output.write(block(0x0a0d0d0a, struct.pack('<IHHq', 0x1a2b3c4d, 1, 0, -1)))
        output.write(block(1, struct.pack('<HHI', 1, 0, 128) + option(9, b'\x09') + bytes(4)))
        ip = struct.pack('!BBHHHBBHII', 0x45, 0, 1500, 0, 0, 64, 17, 0, 0xc0000201, 0xc6336402)
        prefix = bytes.fromhex('ffffffffffff020000000001810000640800') + ip
        prefix += struct.pack('!HHHH', 1111, 9000, 1480, 0)
        for sequence, ns in packets:
            captured = (prefix + encode_probe(sequence, False))[:128]
            assert len(captured) == 128
            output.write(block(6, struct.pack('<IIIII', 0, ns >> 32, ns & 0xffffffff, 128, 1518) + captured))
        stats = ((2, bytes(8)), (3, struct.pack('<II', END >> 32, END & 0xffffffff)),
                 (5, bytes(8)), (7, bytes(8)), (8, struct.pack('<Q', len(packets))))
        options = b''.join(option(code, data) for code, data in stats) + bytes(4)
        output.write(block(5, struct.pack('<III', 0, END >> 32, END & 0xffffffff) + options))


def make_case(root, count=400, stress=False, trace=False, mask_bits=0, *, seqcheck_binary=None,q_bytes=8*1024*1024):
    """Return parsed documents and stream-ID paths for matching reporter inputs.

    All evidence is synthetic software evidence. Native observer endpoints come
    from NativeAdapter; capture observer cost is explicitly synthetic. An optional
    seqcheck executable replaces the known-population seqcheck fixture.
    """
    uint(count, 'packet count')
    uint(mask_bits, 'sample mask', 10)
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    rate = 12_000_000_000 if stress else 9_000_000_000
    config = dict(schema='hng-v1', lines=[dict(name='cable', ingress_port=0, egress_port=1, vlan_id=100,
        model=dict(c_bps=10_000_000_000, q_bytes=q_bytes, t_ns=65_000_000,
                   delta_ns=0, p='0', seed=1, storage_capacity_packets=2_000_000,
                   storage_capacity_bytes=4_000_000_000),
        envelope=dict(min_q_bytes=64, max_q_bytes=1522, burst_bound_packets=64), trace=None)])
    registration = manifest()
    registration['producer_versions'].update(oracle='synthetic-independent-reference', capture='synthetic-pcapng')
    registration['ports'][0]['link_bps'] = 10_000_000_000
    registration['sampling']['oversample_apps'] = []
    registration['timing_mode'] = 'stress' if stress else 'calibration'
    registration['windows'][0]['end_ns'] = END
    registration['capacity'].update(identity_index_capacity=max(count + 10, 4096),
                                    timing_sample_capacity=max(count, 10000), observer_ring_records=65536)
    registration['capacity']['handoff_queues'][0].update(handoff_queue_id='handoff-0', capacity=65536)
    ids = dict(ingress='native-ingress', host='native-host', counters='native-counters',
               before='before', receiver='receiver')
    registration['streams'] = [dict(role=role, stream_id=ids[role], path=f'{role}.jsonl', file_sha256='0' * 64)
                               for role in ('ingress', 'before', 'receiver', 'host', 'counters')]
    traces = {}
    if trace:
        step = max(1, count // 2) * 1542 * 8 * 10**9 // rate
        profile = dict(end_ns=END, loss_seed=1, outage_action='hold', terminal='error', terminal_horizon_ns=None,
                       rows=[dict(at_ns=0, t_ns=65_000_000, loss='0', outage=False),
                             dict(at_ns=step, t_ns=40_000_000, loss='0', outage=False)])
        _json(root / 'trace-source.json', profile)
        normalized = dict(schema='hng-trace-v1', source_id='trace-source.json', direction='forward',
                          units='ns_owd', conversion='synthetic nanoseconds',
                          source_sha256=_hash(root / 'trace-source.json'), splice_policy='natural_hold',
                          trace_epoch_ns=0, time_scale=1, profile=profile)
        _json(root / 'trace.json', normalized)
        config['lines'][0]['trace'] = dict(path='trace.json', sha256=_hash(root / 'trace.json'))
        config['lines'][0]['model']['t_ns'] = 0
        registration['traces'] = [dict(line='cable', provenance='synthetic trace-source.json',
                                     **config['lines'][0]['trace'])]
        traces['cable'] = profile
    _json(root / 'engine.json', config)
    registration['config']['sha256'] = _hash(root / 'engine.json')

    before, receiver, accepted = [], [], []
    reference = Reference(config, 'demo', traces)
    header = dict(schema='hng-native-v2', identity_policy=0, probe_port=9000, capacity=65536,
                  record_bytes=2248, layout='shared', ports=[0, 1])
    with (root / 'native.hngb').open('xb') as output:
        output.write(MAGIC + frame(1, line(header), 0))
        pending, counts, order, frame_seq = bytearray(), [0] * 7, 0, 1

        def flush():
            nonlocal frame_seq
            if pending:
                output.write(frame(2, pending, frame_seq))
                pending.clear()
                frame_seq += 1

        def emit(producer, kind, *, common=None, snapshot=(), items=(), reason=0):
            nonlocal order
            counts[producer] += 1
            raw = entry(producer=producer, kind=kind, reason=reason, seq=counts[producer],
                        order=order, common=common, snapshot=snapshot, items=items)
            if len(pending) + len(raw) > 60000:
                flush()
            pending.extend(raw)
            order += 1

        def resources(phase, port):
            common = [0] * 22
            common[0], common[20] = END if phase else 0, port
            common[4:14] = [phase, 65536, 65536, 1,
                            count if phase and port == 0 else 0,
                            len(accepted) if phase and port == 1 else 0, 0, 0, 0, 0]
            emit(4, 7, common=common)

        resources(0, 0)
        resources(0, 1)
        for sequence in range(count):
            arrival = sequence * 1542 * 8 * 10**9 // rate
            event = dict(kind='packet', run='demo', ingress_port=0, ingress_seq=sequence,
                         selection_seq=sequence, line='cable', external_identity=f'{FLOW}:{sequence}',
                         flow_id=FLOW, arrival_ns=arrival, length=1518, vlan_id=100, segments=1,
                         length_flags=dict(fcs_present=False, vlan_retained=True), app=None,
                         selected_late=0, input_error=None)
            want = reference.consume(event)
            item = [sequence, sequence, sequence, arrival, 0, 0, 0xc0000201, 0xc6336402,
                    1518, 1111, 9000, 0, 0, 100, 1, 0, 1, 0, 0, 1, 0, 0]
            common = [0] * 22
            common[0], common[14] = arrival, sequence
            emit(0, 0, common=common, items=(item,))
            emit(1, 1, common=common, items=(item,))
            before.append((sequence, arrival))
            if want['outcome'] == 'accepted':
                accepted.append((item, want['release_ns']))
                receiver.append((sequence, want['release_ns'] + 3))
            else:
                reason = dict(q_drop=0, trace_loss=1, trace_outage=2)[want['outcome']]
                emit(1, 2, common=common, items=(item,), reason=reason)
        reference.consume(dict(kind='end', run='demo', ingress_port=0, next_ingress_seq=count))
        reference.finish()
        common = [0] * 22
        common[0], common[14] = before[-1][1] + 1 if before else 0, count
        emit(0, 4, common=common)
        for index, (item, release) in enumerate(accepted):
            sent = list(item)
            sent[4], sent[5] = index, release
            common = [0] * 22
            common[:4] = [release] * 4
            common[15], common[16], common[20], common[21] = index, index + 1, 1, 1
            emit(3, 6, common=common, items=(sent,), snapshot=((0, count, U64, 1), (1, 0, U64, 1)))
        resources(1, 0)
        resources(1, 1)
        flush()
        output.write(frame(3, line(health(tuple(counts))), frame_seq))

    with (root / 'native.hngb').open('rb') as source, (root / 'native.jsonl').open('x') as decoded:
        assert decode(source, decoded)['complete']
    with (root / 'native.jsonl').open() as source:
        adapter = NativeAdapter(source, root / 'ingress.jsonl', root / 'host.jsonl', root / 'native-counters.jsonl',
                                run='demo', lines=config['lines'])
        state = adapter.process()
        assert state['complete'], state
    _pcapng(root / 'before.pcapng', before)
    _pcapng(root / 'receiver.pcapng', receiver)

    calibration = document()
    calibration['clocks'] = [dict(clock_id=name, kind='fake', unit='ns', width_bits=64,
                                   device_token=f'synthetic-{name}', mode='synthetic')
                             for name in ('capture', 'engine', 'obs')]
    calibration['mappings'] = [dict(mapping_id=mapping, raw_clock_id=clock, target_clock_id='obs',
                                    raw_origin=0, target_origin_ns=0, scale_num=1, scale_den=1,
                                    valid_from_raw=0, valid_to_raw=10**15, residual_bound_ns=0)
                              for mapping, clock in (('ns', 'capture'), ('engine-ns', 'engine'))]
    pair_calibration = calibration['pairs'][0]
    pair_calibration.update(ingress_clock_id='capture', egress_clock_id='capture', offset_ns=0,
                            evidence_sha256=hashlib.sha256(canonical([_hash(root / 'before.pcapng'),
                                                                    _hash(root / 'receiver.pcapng')])).hexdigest())
    for key in ('offset_bound_ns', 'drift_bound_ns', 'quantization_bound_ns', 'load_bound_ns',
                'mapping_bound_ns', 'uncertainty_ns'):
        pair_calibration[key] = 0
    _json(root / 'frame-profile.json', dict(scope='software-only', offered_bps=rate,
        wire_length_without_fcs=1518, vlan_id=100, capture_snaplen=128, packets=count))
    calibration['qualification'].update(offered_bps=rate, duration_ns=END,
                                        frame_profile_sha256=_hash(root / 'frame-profile.json'))
    _json(root / 'calibration.json', calibration)
    pair = dict(schema='hng-pcapng-pair-v1', manifest='binary.json', clock_id='capture', mapping_id='ns',
                uncertainty_ns=0, start_ns=0, end_ns=END,
                before=dict(path='before.pcapng', interfaces={'0': 0}),
                receiver=dict(path='receiver.pcapng', interfaces={'0': 1}))
    _json(root / 'pair.json', pair)
    clock = Calibration(calibration, 'software-only', 'demo')
    capture_counts = {}
    for role in ('before', 'receiver'):
        stream = next(item for item in registration['streams'] if item['role'] == role)
        converted = convert_stream(pair, role, root, registration, stream, root / stream['path'], clock, (0, END))
        assert converted['complete'], converted
        capture_counts[role] = converted['records']

    counters = StreamWriter(root / 'counters.jsonl', 'demo', 'native-counters', 'counters', 'engine',
                            producer='synthetic-capture-fixture')
    for row in StreamReader(root / 'native-counters.jsonl', role='counters', run='demo'):
        counters.emit('counter', **{key: value for key, value in row.items()
                                   if key not in ('kind', 'run', 'stream_id', 'seq')})
    for role, records in capture_counts.items():
        for phase in ('start', 'end'):
            values = dict.fromkeys(OBSERVER_FIELDS, 0)
            values['queue_capacity'] = 65536
            if phase == 'end':
                values.update(records_attempted=records, records_emitted=records, queue_high_water=1)
            row = snapshot('observer', ids[role], phase, values, at_ns=END if phase == 'end' else 0)
            for descriptor in row['descriptors'].values():
                descriptor['source'] = 'synthetic-software-capture-fixture'
            counters.emit('counter', **{key: value for key, value in row.items()
                                       if key not in ('kind', 'run', 'stream_id', 'seq')})
    counters.finish()

    missing = count - len(receiver)
    if seqcheck_binary is None:
        totals = dict.fromkeys(METRICS, 0)
        totals.update(ingress=count, receiver=len(receiver), missing=missing)
        inputs = {}
        for role, path, packets in (('ingress', 'before.pcapng', count),
                                    ('receiver', 'receiver.pcapng', len(receiver))):
            inputs[role] = dict(path=path, error=None, sha256=_hash(root / path),
                                byte_size=(root / path).stat().st_size, packets=packets,
                                health=dict(complete=True, capture_drops=0))
        check = dict(schema='hng-seqcheck-v1', scope='supplied-captures', status='FAIL' if missing else 'PASS',
                     complete=True, error=None, inputs=inputs, totals=totals,
                     flows=[dict(totals, flow_id=FLOW, vlan_id=100)], details_limit=0,
                     details={key: [] for key in ISSUES}, details_truncated={key: bool(totals[key]) for key in ISSUES})
        _json(root / 'seqcheck.json', check)
    else:
        result = subprocess.run([str(Path(seqcheck_binary).resolve()), '--ingress', 'before.pcapng',
                                 '--receiver', 'receiver.pcapng', '--details', '8'], cwd=root,
                                capture_output=True, check=False)
        assert result.returncode in (0, 1), result.stderr.decode(errors='replace')
        check = json.loads(result.stdout)
        assert check['complete'] and check['totals']['missing'] == missing, check
        with (root / 'seqcheck.json').open('xb') as output:
            output.write(result.stdout)
    registration['verification'] = dict(mode='sampled', seqchecks=[dict(path='seqcheck.json',
        sha256=_hash(root / 'seqcheck.json'), ingress_port=0, egress_port=1,
        ingress_path='before.pcapng', receiver_path='receiver.pcapng')])
    registration['required_cells'][0].update(mask_bits=mask_bits,
        planned_survivors=max(len(receiver), planning_target(mask_bits)))
    sources = {}
    for stream in registration['streams']:
        sources[stream['stream_id']] = root / stream['path']
        stream['file_sha256'] = _hash(root / stream['path'])
    _json(root / 'legacy.json', registration)
    binary = copy.deepcopy(registration)
    binary['streams'] = [item for item in binary['streams'] if item['role'] == 'counters']
    binary['verification'].update(native_binary=dict(path='native.hngb', sha256=_hash(root / 'native.hngb')),
                                  capture_pairs=['pair.json'])
    _json(root / 'binary.json', binary)
    return dict(config=config, legacy_manifest=registration, binary_manifest=binary,
                calibration=calibration, legacy_sources=sources)


def _selfcheck():
    from tools.g2.bundle import build_bundle_report
    from tools.g2.protocol import StreamReader
    from tools.g2.reference import load_config
    from tools.g2.validate import load_registration

    assert callable(globals().get('make_case')), 'binary sampled fixture is missing'
    with tempfile.TemporaryDirectory() as directory:
        for name, options in (('fixed', {}), ('stress', dict(stress=True)), ('trace', dict(trace=True))):
            root = Path(directory) / name
            case = make_case(root, **options)
            _, config, traces = load_registration(root / 'legacy.json')
            assert load_registration(root / 'binary.json')[1] == config
            assert load_config(root / 'engine.json')[0] == config == case['config']
            report = build_bundle_report(config, traces, case['legacy_manifest'],
                                         case['legacy_sources'], case['calibration'], registration_root=root)
            assert report['measurement_status'] == 'PASS', report['reason_codes']
            assert report['observer_cost']['status'] == 'PASS', report['observer_cost']
            assert report['sampled_reference']['sample_checked'] == 400
            for stream in case['legacy_manifest']['streams']:
                reader = StreamReader(case['legacy_sources'][stream['stream_id']], role=stream['role'])
                records = list(reader)
                assert reader.integrity['complete']
                if name == 'trace' and stream['role'] == 'receiver':
                    assert len({row['receive_ns'] for row in records}) < len(records), 'trace must exercise clamp'
            assert (root / 'native.hngb').read_bytes().startswith(b'HNGBIN1\n')
            print(name, report['measurement_status'], report['sampled_reference']['sample_checked'])


if __name__ == '__main__':
    _selfcheck()
