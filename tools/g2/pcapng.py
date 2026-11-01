# SPDX-License-Identifier: Apache-2.0
"""Stream a registered same-clock nanosecond pcapng pair into G2 observations."""
import argparse
import hashlib
import json
from pathlib import Path
import struct
import sys

from .calibration import Calibration
from .counters import OBSERVER_FIELDS
from .matrix import run_matrix, EXIT
from .probe import decode_probe
from .protocol import StreamError, StreamReader, StreamWriter, shape, identifier
from .reference import strict_json, uint
from .validate import preflight


MAX_BLOCK = 1024*1024
SHB = b'\x0a\x0d\x0d\x0a'


def fail(code, message):
    raise StreamError(code, message, 'INCONCLUSIVE')


def options(data, order):
    result = {}
    while data:
        if len(data) < 4:
            fail('PCAPNG_OPTIONS', 'truncated option header')
        code, size = struct.unpack_from(order+'HH', data)
        if code == 0:
            if size or any(data[4:]):
                fail('PCAPNG_OPTIONS', 'invalid end of options')
            return result
        padded = (size+3) & ~3
        if len(data) < 4+padded:
            fail('PCAPNG_OPTIONS', 'truncated option value')
        result.setdefault(code, []).append(data[4:4+size])
        data = data[4+padded:]
    return result


def option(values, code, size, default=None):
    if code not in values:
        return default
    if len(values[code]) != 1 or len(values[code][0]) != size:
        fail('PCAPNG_OPTIONS', 'duplicate or incorrectly sized option')
    return values[code][0]


class Capture:
    def __init__(self, path, interfaces, start, end):
        self.path, self.interfaces, self.start, self.end = path, interfaces, start, end
        self.descriptions = {}
        self.interface_count = 0
        self.drops = 0
        self.records = 0

    def packets(self):
        order = None
        section_end = None
        ordinal = 0
        with self.path.open('rb') as source:
            while prefix := source.read(12):
                if len(prefix) != 12:
                    fail('PCAPNG_TRUNCATED', 'truncated block header')
                if prefix[:4] == SHB:
                    if order is not None:
                        fail('PCAPNG_SECTION', 'one capture section is required')
                    order = {b'\x4d\x3c\x2b\x1a': '<', b'\x1a\x2b\x3c\x4d': '>'}.get(prefix[8:])
                if order is None:
                    fail('PCAPNG_SECTION', 'missing/invalid section byte order')
                kind, size = struct.unpack(order+'II', prefix[:8])
                if size < 12 or size % 4 or size > MAX_BLOCK:
                    fail('PCAPNG_BLOCK_LENGTH', 'invalid or unsupported block length')
                rest = source.read(size-12)
                if len(rest) != size-12:
                    fail('PCAPNG_TRUNCATED', 'truncated block body')
                body = prefix[8:] + rest
                if struct.unpack(order+'I', body[-4:])[0] != size:
                    fail('PCAPNG_BLOCK_LENGTH', 'block trailer length mismatch')
                body = body[:-4]
                ordinal += 1
                if kind == 0x0a0d0d0a:
                    if len(body) < 16 or struct.unpack_from(order+'HH', body, 4) != (1, 0):
                        fail('PCAPNG_SECTION', 'unsupported section version')
                    length = struct.unpack_from(order+'q', body, 8)[0]
                    if length < -1:
                        fail('PCAPNG_SECTION', 'invalid section length')
                    section_end = None if length == -1 else source.tell()+length
                    options(body[16:], order)
                elif kind == 1:
                    if len(body) < 8:
                        fail('PCAPNG_INTERFACE', 'truncated interface description')
                    link, _, snaplen = struct.unpack_from(order+'HHI', body)
                    opts = options(body[8:], order)
                    if (link != 1 or option(opts, 9, 1, b'\x06') != b'\x09'
                            or option(opts, 13, 1, b'\0') != b'\0'):
                        fail('PCAPNG_INTERFACE', 'Ethernet, explicit nanoseconds and stripped FCS are required')
                    offset = struct.unpack(order+'q', option(opts, 14, 8, bytes(8)))[0]*10**9
                    if self.interface_count in self.interfaces:
                        self.descriptions[self.interface_count] = dict(offset=offset, snaplen=snaplen, packets=0,
                                                                      first=None, last=None, packet_block=0, stats=None)
                    self.interface_count += 1
                elif kind in (5, 6):
                    minimum = 20 if kind == 6 else 12
                    if len(body) < minimum:
                        fail('PCAPNG_TRUNCATED', 'truncated packet/statistics block')
                    interface, high, low = struct.unpack_from(order+'III', body)
                    if interface not in self.descriptions:
                        fail('PCAPNG_INTERFACE', 'unregistered capture interface')
                    info = self.descriptions[interface]
                    raw = (high << 32 | low) + info['offset']
                    uint(raw, 'capture timestamp')
                    if kind == 6:
                        caplen, wirelen = struct.unpack_from(order+'II', body, 12)
                        padded = (caplen+3) & ~3
                        if (caplen != wirelen or len(body) < 20+padded
                                or info['snaplen'] and caplen > info['snaplen']):
                            fail('PCAPNG_TRUNCATED', 'packet bytes were truncated')
                        opts = options(body[20+padded:], order)
                        dropped = struct.unpack(order+'Q', option(opts, 4, 8, bytes(8)))[0]
                        if dropped == (1 << 64)-1:
                            fail('PCAPNG_HEALTH_UNKNOWN', 'packet drop count is unavailable')
                        self.drops += dropped
                        if dropped:
                            fail('PCAPNG_CAPTURE_DROP', 'enhanced packet block reports capture loss')
                        flags = struct.unpack(order+'I', option(opts, 2, 4, bytes(4)))[0]
                        if flags >> 5 & 15:
                            fail('PCAPNG_FCS', 'per-packet FCS is unsupported')
                        if info['last'] is not None and raw < info['last']:
                            fail('CLOCK_BACKWARD', 'capture timestamp moved backward')
                        if info['first'] is None: info['first'] = raw
                        info.update(last=raw, packet_block=ordinal, packets=info['packets']+1)
                        self.records += 1
                        yield interface, raw, body[20:20+caplen]
                    else:
                        opts = options(body[12:], order)
                        required = [option(opts, code, 8) for code in (2, 3, 5, 7, 8)]
                        if any(value is None for value in required):
                            fail('PCAPNG_HEALTH_UNKNOWN', 'capture statistics need time bounds, ifdrop, osdrop and usrdeliv')
                        start, end = [(struct.unpack(order+'II', value)[0] << 32 |
                                       struct.unpack(order+'II', value)[1]) + info['offset'] for value in required[:2]]
                        ifdrop, osdrop, delivered = [struct.unpack(order+'Q', value)[0] for value in required[2:]]
                        if max(ifdrop, osdrop, delivered) == (1 << 64)-1 or ifdrop+osdrop >= 1 << 64:
                            fail('PCAPNG_HEALTH_UNKNOWN', 'capture counter is unavailable or overflowed')
                        self.drops = max(self.drops, ifdrop+osdrop)
                        if ifdrop or osdrop:
                            fail('PCAPNG_CAPTURE_DROP', 'interface statistics report capture loss')
                        prior = info['stats']
                        if prior and (start != prior['start'] or end < prior['end']
                                      or delivered < prior['delivered'] or raw < prior['at']):
                            fail('PCAPNG_HEALTH_UNKNOWN', 'capture statistics reset or moved backward')
                        info['stats'] = dict(start=start, end=end, delivered=delivered, block=ordinal, at=raw)
                elif kind in (2, 3):
                    fail('PCAPNG_PACKET_BLOCK', 'packet blocks without the required timestamp profile are unsupported')
                if section_end is not None and source.tell() > section_end:
                    fail('PCAPNG_SECTION', 'section length exceeded')
            if order is None or section_end is not None and source.tell() != section_end:
                fail('PCAPNG_TRUNCATED', 'empty or incomplete capture section')
        for interface in self.interfaces:
            if interface not in self.descriptions:
                fail('PCAPNG_INTERFACE', 'registered interface was not described')
            info = self.descriptions[interface]
            stats = info['stats']
            if (stats is None or stats['block'] < info['packet_block'] or stats['delivered'] != info['packets']
                    or not stats['start'] <= self.start < self.end <= stats['end'] <= stats['at']
                    or info['first'] is not None and info['first'] < stats['start']
                    or info['last'] is not None and info['last'] > stats['end']):
                fail('PCAPNG_HEALTH_UNKNOWN', 'final capture statistics do not certify the registered window/packet count')


def observation(frame, port, role, manifest):
    if len(frame) < 18 or frame[12:14] != b'\x81\0' or frame[16:18] != b'\x08\0':
        fail('PCAPNG_IDENTITY', 'retained single-VLAN IPv4 UDP probes are required')
    vlan = struct.unpack_from('!H', frame, 14)[0] & 4095
    side = 'ingress_port' if role == 'before' else 'egress_port'
    lines = [line for line in manifest['lines'] if line[side] == port and line['vlan_id'] == vlan]
    if len(lines) != 1:
        fail('PCAPNG_IDENTITY', 'packet has no unique registered line')
    ip = 18
    ihl = (frame[ip] & 15)*4
    if len(frame) < ip+20 or frame[ip] >> 4 != 4 or ihl < 20 or len(frame) < ip+ihl+8:
        fail('PCAPNG_IDENTITY', 'truncated or unsupported IPv4 header')
    total, fragments = struct.unpack_from('!HH', frame, ip+2)[0], struct.unpack_from('!H', frame, ip+6)[0]
    udp = ip+ihl
    sport, dport, size = struct.unpack_from('!HHH', frame, udp)
    if (frame[ip+9] != 17 or fragments & 0x3fff or dport != manifest['identity_profile']['probe_port']
            or total != ihl+size or size < 8 or ip+total > len(frame)):
        fail('PCAPNG_IDENTITY', 'unsupported or truncated probe packet')
    sequence, late = decode_probe(frame[udp+8:udp+size])
    source, destination = struct.unpack_from('!II', frame, ip+12)
    flow = f'{source:08x}:{destination:08x}:{sport}:{dport}'
    line = lines[0]
    return dict(ingress_port=line['ingress_port'], egress_port=line['egress_port'], line=line['name'],
                external_identity=f'{flow}:{sequence}', flow_id=flow, length=len(frame), vlan_id=vlan,
                app=None, selected_late=int(late))


def file_hash(path):
    with path.open('rb') as source:
        return hashlib.file_digest(source, 'sha256').hexdigest()


def file_signature(path):
    state = path.stat()
    return state.st_dev, state.st_ino, state.st_size, state.st_mtime_ns


def coverage_window(clock, manifest, start, end, target, uncertainty):
    engine = manifest['clocks']['engine']
    if clock.clocks[engine]['unit'] != 'ns':
        fail('CLOCK_COVERAGE_MISSING', 'engine endpoint clock must use nanoseconds')
    bounds = [min(start, *(window['start_ns'] for window in manifest['windows'])),
              max(end, *(window['end_ns'] for window in manifest['windows']))]
    result = []
    for raw, sign in zip(bounds, (-1, 1)):
        candidates = [value for value in clock.mappings.values() if value['raw_clock_id'] == engine
            and value['target_clock_id'] == target and value['valid_from_raw'] <= raw < value['valid_to_raw']]
        if len(candidates) != 1:
            fail('CLOCK_COVERAGE_MISSING', 'registered engine-to-observation clock anchor is missing')
        mapping = candidates[0]
        mapped = clock._mapped(mapping, raw) + sign*(mapping['residual_bound_ns']+uncertainty)
        if not 0 <= mapped < 1 << 64:
            fail('CLOCK_COVERAGE_RANGE', 'measurement coverage is outside the capture clock range')
        result.append(mapped)
    return result


def convert_stream(pair, role, base, manifest, registration, destination, clock, coverage):
    entry = pair[role]
    interfaces = {int(key): value for key, value in entry['interfaces'].items()}
    capture = Capture(base/entry['path'], interfaces, pair['start_ns'], pair['end_ns'])
    writer = StreamWriter(destination, manifest['run'], registration['stream_id'], role,
                          clock.mappings[pair['mapping_id']]['target_clock_id'], producer='pcapng-adapter')
    health = dict(records=0, dropped=None, complete=True, reason_codes=[], source_sha256=None)
    ordinals = {}
    try:
        if isinstance(coverage, StreamError):
            raise coverage
        if not pair['start_ns'] <= coverage[0] < coverage[1] <= pair['end_ns']:
            fail('CLOCK_COVERAGE_WINDOW', 'declared capture window does not cover the registered measurement/drain')
        mapping = clock.mappings[pair['mapping_id']]
        if not mapping['valid_from_raw'] <= coverage[0] < coverage[1] < mapping['valid_to_raw']:
            fail('CLOCK_COVERAGE_EXTRAPOLATION', 'capture calibration does not cover the whole measurement/drain')
        initial = file_signature(capture.path)
        if not capture.path.is_file():
            fail('PCAPNG_SOURCE', 'a closed regular capture file is required')
        for interface, raw, packet in capture.packets():
            values = observation(packet, interfaces[interface], role, manifest)
            timestamp = dict(raw=raw, clock_id=pair['clock_id'], mapping_id=pair['mapping_id'],
                             point='external_rx', uncertainty_ns=pair['uncertainty_ns'])
            clock.map_timestamp(timestamp, raw, order_key=(role, interface))
            port = values['ingress_port'] if role == 'before' else values['egress_port']
            ordinal = ordinals.get(port, 0); ordinals[port] = ordinal+1
            writer.emit('before' if role == 'before' else 'receive', **values, timestamp=timestamp,
                        **({'before_seq': ordinal, 'ingress_ts_ns': raw} if role == 'before' else
                           {'receive_seq': ordinal, 'receive_ns': raw}))
            health['records'] += 1
        health['source_sha256'] = file_hash(capture.path)
        if initial != file_signature(capture.path):
            fail('PCAPNG_SOURCE_CHANGED', 'capture file changed during conversion')
    except (ValueError, OSError, KeyError, IndexError, struct.error) as error:
        health.update(complete=False, reason_codes=[getattr(error, 'code', 'PCAPNG_FORMAT')], reason=str(error))
    health['observed_drop_count'] = capture.drops
    health['dropped'] = capture.drops if health['complete'] else None
    writer.finish(complete=health['complete'], dropped_records=capture.drops)
    return health


def convert_pair(pair_path, output):
    pair_path = Path(pair_path)
    pair = strict_json(pair_path.read_bytes())
    shape(pair, ('schema','manifest','clock_id','mapping_id','uncertainty_ns','start_ns','end_ns','before','receiver'))
    if pair['schema'] != 'hng-pcapng-pair-v1' or not uint(pair['end_ns'], 'end') > uint(pair['start_ns'], 'start'):
        raise ValueError('invalid capture pair schema/window')
    uint(pair['uncertainty_ns'], 'timestamp uncertainty')
    path = (pair_path.parent/pair['manifest']).resolve()
    manifest = strict_json(path.read_bytes())
    preflight(path)
    if manifest['identity_profile']['kind'] != 'probe':
        raise ValueError('this adapter requires the registered UDP probe identity profile')
    clock = Calibration(strict_json((path.parent/manifest['calibration_path']).read_bytes()),
                        manifest['scope'], manifest['run'])
    mapping = clock.mappings[pair['mapping_id']]
    if (mapping['raw_clock_id'] != pair['clock_id'] or clock.clocks[pair['clock_id']]['unit'] != 'ns'
            or mapping['scale_num'] != mapping['scale_den']
            or mapping['raw_origin'] != mapping['target_origin_ns']
            or any(target != mapping['target_clock_id'] for target in manifest['clocks']['observation'].values())):
        raise ValueError('same-clock capture requires a registered identity nanosecond mapping')
    streams = {}
    for role in ('before','receiver','counters'):
        selected = [row for row in manifest['streams'] if row['role'] == role]
        if len(selected) != 1:
            raise ValueError('one registered before/receiver/counters stream is required')
        streams[role] = selected[0]
    for role in ('before','receiver'):
        shape(pair[role], ('path','interfaces'))
        identifier(pair[role]['path'], 'capture path')
        interfaces = pair[role]['interfaces']
        allowed = {line['ingress_port' if role == 'before' else 'egress_port'] for line in manifest['lines']}
        if (not isinstance(interfaces, dict) or not interfaces or len(set(interfaces.values())) != len(interfaces)
                or any(not key.isascii() or not key.isdecimal() or str(int(key)) != key or
                       type(value) is not int or value not in allowed for key,value in interfaces.items())):
            raise ValueError('capture interfaces require unique registered physical ports')
    before = pair_path.parent/pair['before']['path']
    receiver = pair_path.parent/pair['receiver']['path']
    if before.resolve() == receiver.resolve() or before.is_file() and receiver.is_file() and before.samefile(receiver):
        raise ValueError('before and receiver must be different capture files')
    destination = Path(output)
    destination.mkdir(parents=True, exist_ok=False)
    bundle = destination/'bundle'; bundle.mkdir()
    (bundle/'source').symlink_to(path.parent, target_is_directory=True)
    source_counters = path.parent/streams['counters']['path']
    counters = StreamReader(source_counters, role='counters', run=manifest['run'])
    if counters.header['stream_id'] != streams['counters']['stream_id']:
        raise ValueError('counter stream ID differs from registration')
    counter_writer = StreamWriter(bundle/'counters.jsonl',manifest['run'],streams['counters']['stream_id'],
                                  'counters',manifest['clocks']['engine'],producer='pcapng-adapter')
    start, end = None, None
    replaced = {streams[role]['stream_id'] for role in ('before','receiver')}
    for row in counters:
        if row['clock_id'] == manifest['clocks']['engine']:
            if row['phase'] == 'start': start = min(start, row['at_ns']) if start is not None else row['at_ns']
            if row['phase'] == 'end': end = max(end, row['at_ns']) if end is not None else row['at_ns']
        if row['scope'] == 'observer' and row['scope_id'] in replaced:
            continue
        counter_writer.emit('counter', **{key:value for key,value in row.items()
                                           if key not in ('kind','run','stream_id','seq')})
    if start is None or end is None:
        raise ValueError('native counters require start/end measurement endpoints')
    if counters.integrity['file_sha256'] != streams['counters']['file_sha256']:
        raise ValueError('source counter file hash differs from registration')
    try:
        coverage = coverage_window(clock, manifest, start, end, mapping['target_clock_id'], pair['uncertainty_ns'])
    except StreamError as error:
        coverage = error
    health = {role: convert_stream(pair, role, pair_path.parent, manifest, streams[role],
                                  bundle/f'{role}.jsonl', clock, coverage) for role in ('before','receiver')}
    for role, state in health.items():
        for phase in ('start','end'):
            values = dict.fromkeys(OBSERVER_FIELDS, 0)
            values['queue_capacity'] = 1
            if phase == 'end':
                values.update(records_attempted=None if state['dropped'] is None else state['records']+state['dropped'],
                    records_emitted=state['records'],
                    records_dropped=state['dropped'], capture_drops=state['dropped'], queue_high_water=int(state['records']>0),
                    malformed_records=int(not state['complete']),
                    clock_invalid=int(any(code.startswith('CLOCK_') for code in state['reason_codes'])))
            descriptors = {name:dict(unit='events', semantics='gauge' if name.startswith('queue_') else 'overlapping_diagnostic',
                source='pcapng-adapter',status='measured' if value is not None else 'missing',
                reason=None if value is not None else state['reason_codes'][0]) for name,value in values.items()}
            counter_writer.emit('counter',scope='observer',scope_id=streams[role]['stream_id'],phase=phase,
                at_ns=start if phase=='start' else end,clock_id=manifest['clocks']['engine'],reset_generation=0,
                values=values,descriptors=descriptors)
    counter_writer.finish()
    for field in (manifest['config'], *manifest['traces'], *manifest['streams']):
        if field in streams.values():
            role = field['role']; field['path'] = f'{role}.jsonl'; field['file_sha256'] = file_hash(bundle/field['path'])
        elif not Path(field['path']).is_absolute():
            field['path'] = 'source/'+field['path']
    if not Path(manifest['calibration_path']).is_absolute():
        manifest['calibration_path'] = 'source/'+manifest['calibration_path']
    (bundle/'manifest.json').write_text(json.dumps(manifest, allow_nan=False)+'\n')
    (destination/'conversion.json').write_text(json.dumps(dict(schema='hng-pcapng-conversion-v1',
        run=manifest['run'],scope=manifest['scope'],g2_qualified=False,captures=health,
        source_manifest_sha256=file_hash(path),source_counters_sha256=counters.integrity['file_sha256']),indent=2)+'\n')
    matrix = dict(schema='hng-m6-matrix-v1',scope=manifest['scope'],plan={},
                  runs=[dict(id=manifest['run'],manifest='bundle/manifest.json')])
    (destination/'matrix.json').write_text(json.dumps(matrix)+'\n')
    return run_matrix(destination/'matrix.json', destination/'reports')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('pair')
    parser.add_argument('--out', required=True)
    args = parser.parse_args()
    try:
        result = convert_pair(args.pair, args.out)
    except (ValueError, OSError, KeyError, TypeError) as error:
        print(str(error), file=sys.stderr)
        return 2
    print(json.dumps(result, allow_nan=False))
    return EXIT[result['status']]


if __name__ == '__main__':
    sys.exit(main())
