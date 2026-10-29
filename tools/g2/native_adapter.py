# SPDX-License-Identifier: Apache-2.0
"""Normalize the software-only native pipe into sealed oracle input streams.

All destinations are open text streams. The adapter forwards every input to the
oracle; retention and its default 1/1024 sample belong to the downstream consumer.
No packet file is created. Native producer loss is never repaired by re-sequencing.
"""
import argparse
from collections import Counter
import hashlib
import json
import os
import stat
import sys

from .protocol import StreamWriter
from .reference import U64, fields, load_config, strict_json, uint

# hng-native-v1 item-vector order. Raw indices are adapter-private, not interchange.
ITEM_FIELDS = ('identity', 'ingress_seq', 'selection_seq', 'arrival_ns', 'handoff_seq', 'gate_ns',
               'source', 'destination', 'length', 'source_port', 'destination_port', 'ingress_port',
               'line', 'vlan_id', 'segments', 'handoff_queue', 'identity_kind', 'selected_late',
               'input_error', 'classified', 'stripped', 'rx_flags')
ROW_FIELDS = ('producer', 'producer_seq', 'order', 'kind', 'at_ns', 'gate_ns', 'submit_before_ns',
              'submit_after_ns', 'actual_selection_seq', 'attempt_id', 'consumed_dispatch',
              'first_handoff_seq', 'handoff_queue', 'port', 'accepted_count', 'reason', 'snapshot',
              'items', 'resources')
REASONS = ('q_drop', 'trace_loss_drop', 'trace_outage_drop', 'delay_storage_drop', 'invalid_input_drop',
           'handoff_drop', 'terminal_tx_drop', 'missed_deadline_outage_drop', 'shutdown_drop')
HEALTH_FIELDS = ('attempted', 'emitted', 'dropped', 'capture_drops', 'high_water', 'identity_errors', 'malformed_records')
PRODUCERS = 5


class NativeAdapter:
    def __init__(self, source, ingress, host, counters, *, run, lines, clock_id='engine', producer_commit='software'):
        self.source, self.lines, self.clock = source, lines, clock_id
        self.writers = [StreamWriter(target, run, f'native-{role}', role, clock_id,
                        producer='native-adapter', producer_commit=producer_commit)
                        for role, target in zip(('ingress', 'host', 'counters'), (ingress, host, counters))]
        self.seen = [0]*PRODUCERS
        self.counts = [0]*PRODUCERS
        self.order, self.at_ns = 0, 0
        self.errors = Counter()
        self.raw_hash = hashlib.sha256()
        self.custody = {}
        self.native_missing = 0
        self.normalization_drops = 0
        self.health = None
        self.header = None
        self.ends = set()
        self.input_ports = {line['ingress_port'] for line in lines}

    def _fault(self, reason, count=1):
        self.errors[reason] += count

    def _read(self):
        line = self.source.readline(65537)
        if not line:
            return None
        raw = line.encode('utf-8') if isinstance(line, str) else line
        self.raw_hash.update(raw)
        if len(raw) > 65536 or not raw.endswith(b'\n'):
            raise ValueError('raw row is oversized or lacks LF')
        return strict_json(raw)

    def _counter(self, scope, scope_id, phase, at_ns, values, *, source='native-observer', semantics='overlapping_diagnostic', units=None):
        descriptors = {}
        for name, value in values.items():
            descriptors[name] = dict(unit=(units or {}).get(name, 'events'), semantics=semantics, source=source,
                                     status='measured' if value is not None else 'missing')
            if name in ('queue_high_water', 'queue_capacity'):
                descriptors[name]['semantics'] = 'gauge'
            if value is None:
                descriptors[name]['reason'] = 'native-resource-counter-unavailable'
        self.writers[2].emit('counter', scope=scope, scope_id=scope_id, phase=phase, at_ns=at_ns,
            clock_id=self.clock, reset_generation=0, values=values, descriptors=descriptors)

    def _observer_health(self, phase):
        h = self.health
        values = dict(records_attempted=sum(p['attempted'] for p in h['producers']) if h else 0,
            records_emitted=sum(p['emitted'] for p in h['producers']) if h else 0,
            records_dropped=sum(p['dropped'] for p in h['producers']) if h else 0,
            capture_drops=sum(p['capture_drops'] for p in h['producers']) if h else 0,
            queue_high_water=sum(p['high_water'] for p in h['producers']) if h else 0,
            queue_capacity=self.header['capacity']*PRODUCERS,
            identity_errors=sum(p['identity_errors'] for p in h['producers']) if h else 0,
            malformed_records=self.errors['malformed_native_record']+(sum(p['malformed_records'] for p in h['producers']) if h else 0), timestamp_missing=0, clock_invalid=0)
        if not h and phase == 'end':
            values['capture_drops'] = None
        self._counter('observer', 'native', phase, self.at_ns if phase == 'end' else 0, values)

    def _item(self, raw):
        if not isinstance(raw, list) or len(raw) != len(ITEM_FIELDS):
            raise ValueError('invalid raw item width')
        item = dict(zip(ITEM_FIELDS, raw))
        for name, value in item.items():
            if name in ('classified', 'stripped'):
                if type(value) is not bool:
                    raise ValueError('invalid raw boolean')
            else:
                uint(value, name)
        for name in ('source', 'destination', 'length', 'segments'):
            uint(item[name], name, (1 << 32)-1)
        for name in ('source_port', 'destination_port', 'ingress_port'):
            uint(item[name], name, 65535)
        uint(item['identity_kind'], 'identity kind', 2)
        uint(item['selected_late'], 'payload flag', 2)
        uint(item['handoff_queue'], 'handoff queue', 1)
        if item['classified']:
            uint(item['line'], 'line index', len(self.lines)-1)
        return item

    def _identity(self, item):
        if item['identity_kind'] == 2:
            flow = f"software-l2-{item['ingress_port']}"
        elif item['identity_kind'] == 1:
            flow = f"{item['source']:08x}:{item['destination']:08x}:{item['source_port']}:{item['destination_port']}"
        else:
            return None, None
        return f"{flow}:{item['identity']}", flow

    def _key(self, item):
        identity, _ = self._identity(item)
        if identity is None or not item['classified']:
            raise ValueError('terminal/handoff identity or line is unrepresentable')
        return dict(ingress_port=item['ingress_port'], ingress_seq=item['ingress_seq'],
                    external_identity=identity, line=self.lines[item['line']]['name'])

    def _snapshot(self, raw):
        if not isinstance(raw, list) or len(raw) > 2:
            raise ValueError('invalid raw snapshot')
        seen, frontiers = {}, {}
        for entry in raw:
            if not isinstance(entry, list) or len(entry) != 4:
                raise ValueError('invalid raw port snapshot')
            port, next_seq, lower, ended = entry
            uint(port, 'port', 65535); uint(next_seq, 'seen prefix'); uint(lower, 'lower bound')
            if type(ended) is not bool or str(port) in seen or (lower == U64 and not ended):
                raise ValueError('invalid raw closed frontier')
            seen[str(port)] = next_seq
            frontiers[str(port)] = dict(through_ns=lower-1 if lower and lower != U64 else None, input_complete=ended)
        return dict(seen_next_ingress_seq=seen, frontiers=frontiers)

    def _resources(self, row):
        phase, total, free_count, valid, *nic = row['resources']
        uint(phase, 'resource phase', 1); uint(valid, 'NIC validity', 1)
        if free_count > total:
            raise ValueError('pool free count exceeds size')
        values = dict(zip(('nic_ipackets', 'nic_opackets', 'nic_imissed', 'nic_ierrors', 'nic_oerrors', 'nic_rx_nombuf'), nic))
        if not valid:
            values = dict.fromkeys(values)
        self._counter('port', str(row['port']), 'end' if phase else 'start', row['at_ns'], values,
                      source='software-pmd', units=dict.fromkeys(values, 'packets'))
        self._counter('pool', str(row['port']), 'end' if phase else 'start', row['at_ns'],
                      dict(pool_total=total, pool_free=free_count, pool_owned=total-free_count),
                      semantics='gauge', units=dict.fromkeys(('pool_total', 'pool_free', 'pool_owned'), 'mbufs'))

    def _event(self, row):
        fields(row, ROW_FIELDS)
        for name in ROW_FIELDS:
            if name not in ('consumed_dispatch', 'snapshot', 'items', 'resources'):
                uint(row[name], name)
        p = uint(row['producer'], 'producer', PRODUCERS-1)
        uint(row['kind'], 'kind', 7)
        if row['producer_seq'] != self.seen[p]+1:
            self._fault('native_producer_sequence_gap')
            self.native_missing += max(0, row['producer_seq']-self.seen[p]-1)
            self.writers[0].emit('gap', first_missing_seq=self.seen[p]+1,
                last_missing_seq=max(self.seen[p]+1, row['producer_seq']-1), reason='native-producer-sequence-gap')
        if row['order'] != self.order:
            self._fault('native_global_order_gap')
        self.order = row['order']+1
        self.seen[p] = row['producer_seq']; self.counts[p] += 1
        if not isinstance(row['consumed_dispatch'], list) or len(row['consumed_dispatch']) != 2:
            raise ValueError('invalid dispatch prefix vector')
        if not isinstance(row['resources'], list) or len(row['resources']) != 10:
            raise ValueError('invalid resource width')
        for value in row['consumed_dispatch']+row['resources']:
            uint(value, 'raw counter/prefix')
        if not isinstance(row['items'], list) or len(row['items']) > 16:
            raise ValueError('invalid raw batch size')
        items = [self._item(item) for item in row['items']]
        kind = row['kind']
        if len(items) != (1 if kind in (0, 1, 2) else len(items)):
            raise ValueError('missing packet item')
        self.at_ns = max(self.at_ns, row['at_ns'], row['submit_after_ns'], *(i['arrival_ns'] for i in items))
        if kind == 0:
            x = items[0]; identity, flow = self._identity(x)
            error = None if not x['input_error'] else ('invalid-input-profile' if x['input_error'] == 1 else 'unsupported-packet-occurrence-identity')
            vlan = x['vlan_id']
            if vlan > 4094 or x['stripped']:
                error = 'unsupported-raw-vlan-profile'; self._fault('unrepresentable_raw_vlan')
            self.writers[0].emit('packet', ingress_port=x['ingress_port'], ingress_seq=x['ingress_seq'],
                line=self.lines[x['line']]['name'] if x['classified'] else None,
                selection_seq=x['selection_seq'] if x['classified'] else None, external_identity=identity,
                flow_id=flow, arrival_ns=x['arrival_ns'], length=x['length'], vlan_id=vlan if vlan <= 4094 else 0,
                segments=x['segments'], length_flags=dict(fcs_present=False, vlan_retained=True), app=None,
                selected_late=x['selected_late'] if x['selected_late'] < 2 else None, input_error=error,
                identity_error='unsupported-packet-occurrence-identity' if identity is None else None,
                extensions=dict(raw_vlan_id=vlan, raw_vlan_stripped=x['stripped'], rx_flags=x['rx_flags']))
            self.custody.setdefault(x['ingress_port'], Counter())['rx_returned'] += 1
        elif kind in (3, 4):
            self.writers[0].emit('frontier' if kind == 3 else 'end', ingress_port=row['port'],
                next_ingress_seq=row['actual_selection_seq'], **(dict(through_ns=row['at_ns']) if kind == 3 else {}))
            if kind == 4:
                self.ends.add(row['port'])
        elif kind in (1, 2):
            x = items[0]
            try:
                key = self._key(x)
            except ValueError:
                self.normalization_drops += 1
                self._fault('unrepresentable_native_terminal_identity')
                self.writers[1].emit('gap', first_missing_seq=row['producer_seq'],
                    last_missing_seq=row['producer_seq'], reason='unrepresentable-native-terminal-identity')
                return
            if kind == 1:
                self.writers[1].emit('offer', **key, at_ns=row['at_ns'], actual_selection_seq=row['actual_selection_seq'])
            else:
                reason = REASONS[uint(row['reason'], 'terminal reason', len(REASONS)-1)]
                self.writers[1].emit('terminal', **key, at_ns=row['at_ns'], reason=reason)
                self.custody.setdefault(x['ingress_port'], Counter())[reason] += 1
        elif kind == 5:
            if items:
                self.writers[1].emit('dispatch', egress_port=row['port'], at_ns=row['at_ns'],
                    handoff_queue_id=f"handoff-{uint(row['handoff_queue'], 'handoff queue', 1)}",
                    first_handoff_seq=row['first_handoff_seq'], identities=[self._key(x) for x in items],
                    **self._snapshot(row['snapshot']))
        elif kind == 6:
            offered = [dict(self._key(x), gate_ns=x['gate_ns'], handoff_queue_id=f"handoff-{x['handoff_queue']}",
                            handoff_seq=x['handoff_seq']) for x in items]
            self.writers[1].emit('attempt', egress_port=row['port'], queue_id=0, attempt_id=row['attempt_id'],
                gate_ns=row['gate_ns'], submit_before_ns=row['submit_before_ns'], submit_after_ns=row['submit_after_ns'],
                consumed_dispatch={f'handoff-{i}': n for i, n in enumerate(row['consumed_dispatch'])}, offered=offered,
                accepted_count=row['accepted_count'], **self._snapshot(row['snapshot']))
            for x in items[:row['accepted_count']]:
                self.custody.setdefault(x['ingress_port'], Counter())['tx_accepted'] += 1
        else:
            self._resources(row)

    def process(self):
        try:
            self.header = self._read()
            fields(self.header, ('schema', 'identity_policy', 'probe_port', 'capacity', 'record_bytes'))
            if self.header['schema'] != 'hng-native-v1':
                raise ValueError('unknown native schema')
            uint(self.header['identity_policy'], 'identity policy', 1)
            uint(self.header['probe_port'], 'probe port', 65535)
            if not uint(self.header['capacity'], 'queue capacity', 65536) or not uint(self.header['record_bytes'], 'record size', 65536):
                raise ValueError('native capacity/record width must be positive')
            self._observer_health('start')
            while True:
                row = self._read()
                if row is None:
                    self._fault('missing_native_health'); break
                if row.get('kind') == 'health':
                    fields(row, ('kind', 'complete', 'producers'))
                    if type(row['complete']) is not bool or not isinstance(row['producers'], list) or len(row['producers']) != PRODUCERS:
                        raise ValueError('invalid native health')
                    for p, health in enumerate(row['producers']):
                        fields(health, HEALTH_FIELDS)
                        for name in HEALTH_FIELDS:
                            uint(health[name], name)
                        if (health['emitted'] != self.counts[p] or health['attempted'] != health['emitted']+health['dropped']+health['capture_drops']
                                or health['high_water'] > self.header['capacity'] or health['attempted'] < self.seen[p]):
                            self._fault('native_health_mismatch')
                        if health['dropped'] or health['capture_drops'] or health['identity_errors'] or health['malformed_records']:
                            self._fault('native_capture_loss', health['dropped']+health['capture_drops']+health['identity_errors']+health['malformed_records'])
                    self.health = row
                    if not row['complete']:
                        self._fault('native_incomplete')
                    if self._read() is not None:
                        self._fault('data_after_native_health')
                    break
                self._event(row)
        except (ValueError, TypeError, KeyError, IndexError, UnicodeError, OSError):
            self._fault('malformed_native_record')
            self.normalization_drops += 1
        if self.header and self.header.get('schema') == 'hng-native-v1' and type(self.header.get('capacity')) is int:
            self._observer_health('end')
        if self.input_ports - self.ends:
            self._fault('missing_native_input_end')
        for port, counts in self.custody.items():
            values = {name: counts[name] for name in ('rx_returned', 'tx_accepted')+REASONS}
            values['still_owned'] = max(0, values['rx_returned']-values['tx_accepted']-sum(values[r] for r in REASONS))
            self._counter('port', f'ingress-custody-{port}', 'start', 0, dict.fromkeys(values, 0),
                          source='native-ingress-custody', semantics='terminal_disjoint', units=dict.fromkeys(values, 'packets'))
            self._counter('port', f'ingress-custody-{port}', 'end', self.at_ns, values,
                          source='native-ingress-custody', semantics='terminal_disjoint', units=dict.fromkeys(values, 'packets'))
        complete = not self.errors and self.health is not None
        dropped = self.normalization_drops + (sum(p['dropped']+p['capture_drops'] for p in self.health['producers'])
                                               if self.health else self.native_missing)
        for writer in self.writers:
            writer.finish(complete=complete, dropped_records=dropped)
        return dict(complete=complete, errors=dict(self.errors), raw_consumed_sha256=self.raw_hash.hexdigest(),
                    raw_producer_health=self.health, scope='software-only', g2_qualified=False)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('config')
    parser.add_argument('run')
    parser.add_argument('--input-fd', type=int, default=0)
    parser.add_argument('--ingress-fd', type=int, required=True)
    parser.add_argument('--host-fd', type=int, required=True)
    parser.add_argument('--counter-fd', type=int, required=True)
    parser.add_argument('--producer-commit', required=True)
    args = parser.parse_args()
    fds = (args.input_fd, args.ingress_fd, args.host_fd, args.counter_fd)
    for fd in fds:
        mode = os.fstat(fd).st_mode
        if not (stat.S_ISFIFO(mode) or stat.S_ISSOCK(mode)):
            parser.error('native adapter descriptors must be pipes/sockets')
    config, _ = load_config(args.config)
    handles = [os.fdopen(os.dup(fd), 'r' if i == 0 else 'w', encoding='utf-8') for i, fd in enumerate(fds)]
    try:
        result = NativeAdapter(*handles, run=args.run, lines=config['lines'], producer_commit=args.producer_commit).process()
    finally:
        for handle in handles:
            handle.close()
    print(json.dumps(result, sort_keys=True), file=sys.stderr)
    return 0 if result['complete'] else 2


if __name__ == '__main__':
    sys.exit(main())
