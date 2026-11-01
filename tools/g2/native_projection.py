# SPDX-License-Identifier: Apache-2.0
"""Validate complete native columns and materialize only selected observations."""
from collections import Counter
import io
import json

import numpy as np

from .native_adapter import ITEM_FIELDS, NativeAdapter, REASONS
from .native_arrays import EVENT_DTYPE, ITEM_DTYPE, SNAPSHOT_DTYPE
from .native_binary import _header, _health
from .protocol import StreamError, validate_record
from .reference import U64


def _require(condition, message):
    if not condition:
        raise ValueError(message)


def _in(values, choices):
    choices = tuple(choices)
    if not choices:
        return np.zeros(values.shape, dtype=bool)
    if len(choices) <= 3:
        result = values == choices[0]
        for choice in choices[1:]:
            result |= values == choice
        return result
    return np.isin(values, choices)


class NativeProjection:
    """All-record native validation, logical counts and sampled JSON projection.

    ``accept`` returns batch-local arrays of legacy logical stream ordinals;
    -1 denotes an event that does not emit into that role. Only ``packet`` and
    ``host`` construct packet dictionaries, and callers use them after sampling.
    Counter serialization reuses the legacy adapter's small endpoint helpers.
    """
    def __init__(self, header, manifest, config, producer_commit='software'):
        self._failed = self._finished = False
        try:
            self.header = _header((json.dumps(header)+'\n').encode())
            self.manifest, self.config = manifest, config
            self.lines = config['lines']
            self.input_ports = {line['ingress_port'] for line in self.lines}
            self.physical_ports = {line[key] for line in self.lines for key in ('ingress_port', 'egress_port')}
            _require(set(header['ports']) == self.physical_ports, 'native physical ports differ from configuration')
            self._line_ingress = np.asarray([line['ingress_port'] for line in self.lines], dtype=np.uint16)
            self._line_egress = np.asarray([line['egress_port'] for line in self.lines], dtype=np.uint16)
            self._line_vlan = np.asarray([line['vlan_id'] for line in self.lines], dtype=np.uint16)
            self._line_min = np.asarray([line['envelope']['min_q_bytes'] for line in self.lines], dtype=np.uint64)
            self._line_max = np.asarray([line['envelope']['max_q_bytes'] for line in self.lines], dtype=np.uint64)
            self._packet_selection = [0]*len(self.lines)
            self.port_state = {port:dict(next_seq=0, arrival=0, frontier=-1, ended=False) for port in self.input_ports}
            self._unused_frontiers, self._unused_ends = {}, set()
            self.logical_emitted, self.counts, self.seen = [0, 0], [0]*7, [0]*7
            self.diagnostics, self.runtime, self.custody = Counter(), {}, {}
            self.at_ns, self._order = 0, 0
            self._counter_output = io.StringIO()
            self._adapter = NativeAdapter(io.StringIO(), io.StringIO(), io.StringIO(), self._counter_output,
                run=manifest['run'], lines=self.lines, clock_id=manifest['clocks']['engine'], producer_commit=producer_commit)
            self._adapter.header, self._adapter.producers = self.header, 7
            self._adapter.provenance_key = 'native_observer_v2'
            self._adapter.counts, self._adapter.seen = [0]*7, [0]*7
            self.resources = self._adapter.resources
            self._adapter._observer_health('start')
            self._adapter._logical_health('start')
        except (ValueError, TypeError, KeyError, IndexError, OverflowError) as error:
            raise StreamError('NATIVE_SEMANTIC', str(error), 'INCONCLUSIVE') from error

    def _ingress(self, events, packet_items):
        kind = events['kind']
        packet_ports = np.full(len(events), -1, dtype=np.int64)
        packet_seq = np.zeros(len(events), dtype=np.uint64)
        arrival = np.zeros(len(events), dtype=np.uint64)
        indexes = packet_items['event_index']
        packet_ports[indexes], packet_seq[indexes], arrival[indexes] = (packet_items[name] for name in ('ingress_port', 'ingress_seq', 'arrival_ns'))
        for port, state in self.port_state.items():
            selected = ((kind == 0) & (packet_ports == port)) | (_in(kind, (3, 4)) & (events['port'] == port))
            rows = np.flatnonzero(selected)
            if not rows.size:
                continue
            _require(not state['ended'], 'ingress event follows port END')
            kinds = kind[rows]
            packets, frontiers, ends = kinds == 0, kinds == 3, kinds == 4
            expected = state['next_seq']+np.cumsum(packets, dtype=np.uint64)-packets
            actual = np.where(packets, packet_seq[rows], events['actual_selection_seq'][rows])
            _require(np.array_equal(actual, expected), 'ingress sequence or marker count is not contiguous')
            end_positions = np.flatnonzero(ends)
            _require(not end_positions.size or (len(end_positions) == 1 and end_positions[0] == len(rows)-1), 'input event follows port END')
            times = arrival[rows]
            old_arrival = np.maximum.accumulate(np.r_[np.uint64(state['arrival']), np.where(packets, times, 0)])[:-1]
            lower = np.where(frontiers, events['at_ns'][rows], 0)
            old_frontier = np.maximum.accumulate(np.r_[np.uint64(max(0, state['frontier'])), lower])[:-1]
            has_frontier = np.maximum.accumulate(np.r_[state['frontier'] >= 0, frontiers])[:-1]
            _require(not np.any(packets & ((times < old_arrival) | (has_frontier & (times <= old_frontier)))), 'ingress timestamp regressed or crossed a closed frontier')
            _require(not np.any(frontiers & ((events['at_ns'][rows] < old_arrival) | (events['at_ns'][rows] < old_frontier) | (events['at_ns'][rows] == U64))), 'invalid ingress closed frontier')
            state['next_seq'] += int(packets.sum())
            if np.any(packets):
                state['arrival'] = int(times[packets][-1])
            if np.any(frontiers):
                state['frontier'] = int(events['at_ns'][rows][frontiers][-1])
            state['ended'] = bool(end_positions.size)
        for port in self.physical_ports-self.input_ports:
            rows = np.flatnonzero(_in(kind, (3, 4)) & (events['port'] == port))
            if not rows.size:
                continue
            _require(port not in self._unused_ends and not np.any(events['actual_selection_seq'][rows]) and not np.any(events['count'][rows]), 'invalid unused ingress marker')
            times = events['at_ns'][rows]
            _require(times[0] >= self._unused_frontiers.get(port, 0) and not np.any(times[1:] < times[:-1]), 'unused ingress marker regressed')
            end_positions = np.flatnonzero(kind[rows] == 4)
            _require(not end_positions.size or (len(end_positions) == 1 and end_positions[0] == len(rows)-1), 'unused ingress marker follows END')
            self._unused_frontiers[port] = int(times[-1])
            if end_positions.size:
                self._unused_ends.add(port)
        for line in range(len(self.lines)):
            selected = packet_items['selection_seq'][packet_items['line'] == line]
            want = np.arange(self._packet_selection[line], self._packet_selection[line]+len(selected), dtype=np.uint64)
            _require(np.array_equal(selected, want), 'raw per-line selection sequence is not contiguous')
            self._packet_selection[line] += len(selected)

    def _actors(self, events, items, item_kinds):
        kind, producer = events['kind'], events['producer']
        _require(not np.any(producer[kind == 7] != 4), 'resources require the control actor')
        markers = (kind == 3) | (kind == 4)
        marker_ports = events['port'][markers]
        _require(np.all(_in(marker_ports, self.physical_ports)), 'marker on unregistered port')
        split, ports = self.header['layout'] == 'per-direction', self.header['ports']
        marker_actors = np.where(marker_ports == ports[-1], 5, 0) if split else 0
        _require(not np.any(producer[markers] != marker_actors), 'RX marker actor differs from port owner')
        for stage in (0, 1, 2, 5, 6):
            selected = np.flatnonzero(item_kinds == stage)
            if not selected.size:
                continue
            indexes = items['event_index'][selected]
            actors, ingress = producer[indexes], items['ingress_port'][selected]
            if split:
                _require(np.all(_in(ingress, ports)), 'packet actor refers to unregistered native port')
                owner = (ingress == ports[-1]).astype(np.uint8)
                rx, tx = np.where(owner, 5, 0), np.where(owner, 6, 3)
                worker = actors == 1+owner
            else:
                rx, tx = 0, 3
                worker = (actors == 1) | ((actors == 2) & (ingress == ports[-1]))
            if stage == 0:
                allowed = actors == rx
            elif stage in (1, 5):
                allowed = worker
            elif stage == 6:
                allowed = actors == tx
            else:
                reason = events['reason'][indexes]
                allowed = np.where(reason == 5, worker | (actors == rx),
                    np.where((reason == 6) | (reason == 7), actors == tx, worker))
                _require(np.all(reason < len(REASONS)), 'terminal actor differs from reason stage')
            _require(np.all(allowed), 'packet actor differs from port owner')
            if stage in (5, 6):
                _require(np.all(events['port'][indexes] == self._line_egress[items['line'][selected]]), 'native egress differs from registered line')
            if stage == 5:
                bases = np.cumsum(events['count'], dtype=np.int64)-events['count']
                ordinal = (selected-bases[indexes]).astype(np.uint64)
                first = events['first_handoff_seq'][indexes]
                _require(not np.any((items['handoff_queue'][selected] != events['handoff_queue'][indexes]) |
                    (items['handoff_seq'][selected] != first+ordinal) | (first > U64-ordinal-np.uint64(1))), 'dispatch item differs from queue prefix')
            elif stage == 6:
                _require(not np.any(items['handoff_seq'][selected] == U64), 'TX handoff ordinal uses uint64 sentinel')
                if split:
                    _require(np.all(items['handoff_queue'][selected] == owner), 'TX item belongs to another handoff owner')
                gates = items['gate_ns'][selected]
                _require(not np.any((gates < events['gate_ns'][indexes]) | (gates > events['submit_before_ns'][indexes])), 'item gate outside TX selection interval')
        dispatch = kind == 5
        _require(not np.any(producer[dispatch] != 1+events['handoff_queue'][dispatch].astype(np.uint64)), 'dispatch queue differs from scheduler actor')
        attempts = np.flatnonzero(kind == 6)
        allowed_tx = (3, 6) if split else (3,)
        _require(np.all(_in(producer[attempts], allowed_tx)) and
            not np.any(events['accepted_count'][attempts] > events['count'][attempts]), 'invalid TX actor or accepted prefix')
        if split:
            for actor, owner in ((3, 0), (6, 1)):
                rows = attempts[producer[attempts] == actor]
                egress = {line['egress_port'] for line in self.lines if line['ingress_port'] == ports[owner]}
                _require(not np.any(events['consumed_dispatch'][rows, 1-owner]) and np.all(_in(events['port'][rows], egress)), 'TX prefix or egress belongs to another owner')
        _require(not np.any((events['gate_ns'][attempts] > events['submit_before_ns'][attempts]) |
            (events['submit_before_ns'][attempts] > events['submit_after_ns'][attempts]) |
            (events['attempt_id'][attempts] == U64)), 'impossible TX call timestamp or ordinal')
        _require(not np.any(events['actual_selection_seq'][kind == 1] == U64), 'offer selection ordinal uses uint64 sentinel')

    def _snapshot(self, events, snapshots):
        emitted = ((events['kind'] == 5) & (events['count'] > 0)) | (events['kind'] == 6)
        snapshots = snapshots[emitted[snapshots['event_index']]]
        indexes = snapshots['event_index']
        _require(not np.any((snapshots['lower_bound'] == U64) & ~snapshots['ended']), 'open frontier has closed uint64 sentinel')
        _require(np.all(_in(snapshots['port'], self.physical_ports)), 'snapshot on unregistered port')
        _require(not np.any(~_in(snapshots['port'], self.input_ports) & (snapshots['seen'] != 0)), 'snapshot contains unregistered ingress traffic')
        if len(snapshots) > 1:
            _require(not np.any((indexes[1:] == indexes[:-1]) & (snapshots['port'][1:] == snapshots['port'][:-1])), 'duplicate snapshot port')

    def _bound(self, port, values):
        if not len(values):
            return
        low, high = int(values.min()), int(values.max())
        old = self.runtime.get(str(port), (low, high))
        self.runtime[str(port)] = min(low, old[0]), max(high, old[1])

    def accept(self, batch):
        try:
            _require(not self._failed and not self._finished, 'native projection is failed or finished')
            events, items, snapshots = (batch[name] for name in ('events', 'items', 'snapshots'))
            _require(events.dtype == EVENT_DTYPE and items.dtype == ITEM_DTYPE and snapshots.dtype == SNAPSHOT_DTYPE,
                     'native array schema mismatch')
            _require(events.ndim == items.ndim == snapshots.ndim == 1, 'native columns must be one-dimensional')
            _require(not np.any(events['producer'] > 6) and not np.any(events['kind'] > 7) and not np.any(events['count'] > 16), 'invalid native event domain')
            rows, kinds = np.arange(len(events)), events['kind']
            _require(np.array_equal(items['event_index'], np.repeat(rows, events['count'])), 'native item links differ from event counts')
            _require(not np.any(snapshots['event_index'] < 0) and not np.any(snapshots['event_index'] >= len(events)) and
                not np.any(snapshots['event_index'][1:] < snapshots['event_index'][:-1]), 'invalid snapshot event links')
            _require(not np.any(np.bincount(snapshots['event_index'], minlength=len(events)) > 2), 'too many snapshots')
            _require(not np.any(_in(kinds, (0, 1, 2)) & (events['count'] != 1)), 'invalid packet count for native kind')
            _require(np.array_equal(events['order'], np.arange(self._order, self._order+len(events), dtype=np.uint64)), 'native global order gap')
            for producer in range(7):
                selected = events['producer_seq'][events['producer'] == producer]
                want = np.arange(self.seen[producer]+1, self.seen[producer]+1+len(selected), dtype=np.uint64)
                _require(np.array_equal(selected, want), 'native producer sequence gap')
                if selected.size:
                    self.seen[producer], self.counts[producer] = int(selected[-1]), self.counts[producer]+len(selected)
            indexes = items['event_index']
            item_kinds = kinds[indexes]
            active = (item_kinds <= 2) | (item_kinds == 5) | (item_kinds == 6)
            _require(not np.any((items['identity_kind'] > 2) | (items['selected_late'] > 2) |
                (items['input_error'] > 2) | (items['handoff_queue'] > 1) |
                (items['classified'] & (items['line'] >= len(self.lines))) |
                ((items['identity_kind'] != 0) & (items['identity_kind'] != self.header['identity_policy']+1))), 'invalid native item identity, flags or line')
            _require(not np.any(active & (~items['classified'] | (items['identity_kind'] == 0))),
                     'unrepresentable native packet identity or classification')
            packets = items[item_kinds == 0]
            _require(not np.any((packets['ingress_port'] != self._line_ingress[packets['line']]) |
                (packets['vlan_id'] != self._line_vlan[packets['line']]) | (packets['vlan_id'] > 4094) | packets['stripped']), 'native line or VLAN differs from raw input')
            length = np.maximum(packets['length'].astype(np.uint64)+4, 64)
            _require(not np.any((packets['length'] == 0) | (packets['segments'] != 1) |
                (length < self._line_min[packets['line']]) | (length > self._line_max[packets['line']]) |
                (packets['selection_seq'] == U64)), 'native length, segments or ordinal is unrepresentable')
            _require(not np.any(active & (items['ingress_seq'] == U64)), 'native ingress ordinal uses uint64 sentinel')
            self._actors(events, items, item_kinds)
            self._ingress(events, packets)
            self._snapshot(events, snapshots)
            if len(events):
                self.at_ns = max(self.at_ns, int(events['at_ns'].max()), int(events['submit_after_ns'].max()), int(items['arrival_ns'].max(initial=0)))
            ingress_emits = (kinds == 0) | (_in(kinds, (3, 4)) & _in(events['port'], self.input_ports))
            host_emits = _in(kinds, (1, 2, 6)) | ((kinds == 5) & (events['count'] > 0))
            links = {}
            for role, mask, index in (('ingress', ingress_emits, 0), ('host', host_emits, 1)):
                links[role+'_seq'] = np.where(mask, self.logical_emitted[index]+np.cumsum(mask, dtype=np.int64), -1)
                self.logical_emitted[index] += int(mask.sum())
            for kind, name in ((1, 'offer'), (2, 'terminal'), (5, 'dispatch'), (6, 'attempt')):
                count = int(np.count_nonzero((kinds == kind) & host_emits))
                if count:
                    self.diagnostics[name] += count
            terminal_items = np.flatnonzero(item_kinds == 2)
            terminal_ports = items['ingress_port'][terminal_items]
            terminal_reasons = events['reason'][indexes[terminal_items]]
            attempted_items = np.flatnonzero(item_kinds == 6)
            if attempted_items.size:
                bases = np.cumsum(events['count'], dtype=np.int64)-events['count']
                attempted_events = indexes[attempted_items]
                ordinal = attempted_items-bases[attempted_events]
                accepted_items = attempted_items[ordinal < events['accepted_count'][attempted_events]]
            else:
                accepted_items = attempted_items
            accepted_ports = items['ingress_port'][accepted_items]
            packet_ports = packets['ingress_port']
            host_items = np.flatnonzero((item_kinds == 1) | (item_kinds == 2))
            host_ports = items['ingress_port'][host_items]
            ports = np.unique(np.r_[np.asarray(tuple(self.input_ports), dtype=np.uint16), items['ingress_port'][active]])
            for port_value in ports:
                port = int(port_value)
                received = int(np.count_nonzero(packet_ports == port))
                accepted = int(np.count_nonzero(accepted_ports == port))
                reasons = np.bincount(terminal_reasons[terminal_ports == port], minlength=len(REASONS))
                if port in self.input_ports or received or accepted or np.any(reasons):
                    custody = self.custody.setdefault(port, Counter())
                    custody['rx_returned'] += received
                    custody['tx_accepted'] += accepted
                    for reason in np.flatnonzero(reasons):
                        custody[REASONS[reason]] += int(reasons[reason])
                self._bound(port, packets['arrival_ns'][packet_ports == port])
                self._bound(port, events['at_ns'][indexes[host_items[host_ports == port]]])
            mapped = {}
            for port in self.manifest['ports']:
                mapped.setdefault(port['egress_port'], port['ingress_port'])
            for egress, ingress in mapped.items():
                self._bound(ingress, events['at_ns'][(kinds == 5) & host_emits & (events['port'] == egress)])
                attempts = (kinds == 6) & (events['port'] == egress)
                self._bound(ingress, events['gate_ns'][attempts])
                self._bound(ingress, events['submit_after_ns'][attempts])
            for index in np.flatnonzero(kinds == 7):
                event = events[index]
                row = {name:event[name].tolist() for name in EVENT_DTYPE.names if name != 'count'}
                row['items'] = [[item[name].item() for name in ITEM_FIELDS] for item in items[indexes == index]]
                row['snapshot'] = [[snapshot[name].item() for name in ('port', 'seen', 'lower_bound', 'ended')]
                                   for snapshot in snapshots[snapshots['event_index'] == index]]
                self._adapter.provenance = {name:row[name] for name in ('producer', 'producer_seq', 'order')}
                self._adapter._resources(row)
            self._order += len(events)
            return links
        except (ValueError, TypeError, KeyError, IndexError, OverflowError) as error:
            self._failed = True
            raise StreamError('NATIVE_SEMANTIC', str(error), 'INCONCLUSIVE') from error

    def _row(self, event, kind, stream_seq, role):
        return dict(kind=kind, run=self.manifest['run'], stream_id='native-'+role, seq=stream_seq,
            extensions=dict(native_observer_v2={name:int(event[name]) for name in ('producer', 'producer_seq', 'order')}))

    def packet(self, event, item, stream_seq):
        """Materialize one already validated, selected ingress observation."""
        raw = {name:item[name].item() for name in ITEM_FIELDS}
        identity, flow = self._adapter._identity(raw)
        row = self._row(event, 'packet', stream_seq, 'ingress')
        row.update(ingress_port=raw['ingress_port'], ingress_seq=raw['ingress_seq'], line=self.lines[raw['line']]['name'],
            selection_seq=raw['selection_seq'], external_identity=identity, flow_id=flow, arrival_ns=raw['arrival_ns'],
            length=raw['length'], vlan_id=raw['vlan_id'], segments=raw['segments'],
            length_flags=dict(fcs_present=False, vlan_retained=True), app=None,
            selected_late=raw['selected_late'] if raw['selected_late'] < 2 else None,
            input_error=None if not raw['input_error'] else ('invalid-input-profile' if raw['input_error'] == 1 else 'unsupported-packet-occurrence-identity'),
            identity_error=None)
        row['extensions'].update(raw_vlan_id=raw['vlan_id'], raw_vlan_stripped=raw['stripped'], rx_flags=raw['rx_flags'])
        validate_record(row, 'ingress')
        return row

    def host(self, event, item, stream_seq):
        """Materialize one already validated, selected offer or terminal."""
        _require(int(event['kind']) in (1, 2), 'host materialization requires offer or terminal')
        raw = {name:item[name].item() for name in ITEM_FIELDS}
        kind = 'offer' if event['kind'] == 1 else 'terminal'
        row = self._row(event, kind, stream_seq, 'host')
        row.update(self._adapter._key(raw), at_ns=int(event['at_ns']))
        if kind == 'offer':
            row['actual_selection_seq'] = int(event['actual_selection_seq'])
        else:
            row['reason'] = REASONS[int(event['reason'])]
        validate_record(row, 'host')
        return row

    def counter_rows(self, health, counts, seen):
        """Produce legacy endpoint rows after all native columns were accepted."""
        try:
            _require(not self._failed and not self._finished, 'native projection is failed or finished')
            _require(list(counts) == self.counts and list(seen) == self.seen, 'native health does not cover all projected records')
            health = _health((json.dumps(health)+'\n').encode(), self.header, self.counts, self.seen)
            _require(all(state['ended'] for state in self.port_state.values()), 'missing native input END')
            adapter = self._adapter
            adapter.at_ns, adapter.health = self.at_ns, health
            adapter.counts, adapter.seen = self.counts, self.seen
            adapter.custody = self.custody
            adapter.logical_emitted[:2] = self.logical_emitted
            adapter.provenance = None
            adapter._observer_health('end')
            adapter._logical_health('end')
            adapter._port_counters()
            self._finished = True
            return [json.loads(line) for line in self._counter_output.getvalue().splitlines()[1:]]
        except (ValueError, TypeError, KeyError, IndexError, OverflowError) as error:
            self._failed = True
            raise StreamError('NATIVE_SEMANTIC', str(error), 'INCONCLUSIVE') from error
