# SPDX-License-Identifier: Apache-2.0
"""Exact arrival prefixes with expected records only for selected identities."""
from fractions import Fraction
from .reference import Reference, U64, q_admit, selected, trace_decision, uint


class SampledReference:
    def __init__(self, config, run, traces=None, timing_mode='stress'):
        self.full = Reference(config,run,traces)
        self.mode = timing_mode
        self.checked = 0
        self.prefix = 0

    def consume(self, event, wanted=False):
        ref = self.full
        if event['kind'] != 'packet':
            return ref.consume(event)
        if event['run'] != ref.run:
            raise ValueError('mixed run')
        port = ref.ports[uint(event['ingress_port'],'port',65535)]
        arrival = uint(event['arrival_ns'],'arrival')
        seq = uint(event['ingress_seq'],'ingress sequence',U64-1)
        name = event['line']
        state = ref.lines[name]
        ordinal = uint(event['selection_seq'],'line ordinal',U64-1)
        q = max(uint(event['length'],'length',2**32-1)+4,64)
        line, model = state['config'], state['config']['model']
        if (port['ended'] or seq != port['next_seq'] or arrival < port['arrival'] or arrival <= port['frontier']
                or ordinal != state['seq'] or line['ingress_port'] != event['ingress_port']
                or not line['envelope']['min_q_bytes'] <= q <= line['envelope']['max_q_bytes']
                or event.get('input_error') or event.get('segments',1) != 1
                or event.get('vlan_id',line['vlan_id']) != line['vlan_id']):
            raise ValueError('invalid sampled arrival prefix')
        port.update(next_seq=seq+1,arrival=arrival)
        state['seq'] += 1
        ref.count += 1
        prefix = self.mode == 'stress' or state['trace'] is not None
        if prefix:
            self.prefix += 1
            finish = q_admit(state,arrival,q)
        else:
            # Registered fixed L0 assumes no queue; this is not a full Q audit.
            finish = Fraction(arrival)+Fraction(8*(q+20)*10**9,model['c_bps']) if q <= model['q_bytes'] else None
            state['used'] = q if finish is not None else 0
        late = selected(state['chance'],model['seed'],ordinal) if wanted else False
        outcome, release, service, deadline, clamp = 'q_drop', None, None, None, 0
        if finish is not None:
            outcome = 'accepted'
            if wanted or state['trace']:
                service = -(-finish.numerator//finish.denominator)
                deadline = service+model['t_ns']+late*model['delta_ns']
                release = deadline
                if state['trace']:
                    deadline,release,outcome,clamp = trace_decision(state,service,ordinal)
        state['counts']['offers'] += 1
        state['counts'][outcome] += 1
        if not wanted:
            return None
        self.checked += 1
        drain = max(Fraction(0),finish-arrival) if finish is not None else max(Fraction(0),state['tail']-arrival)
        return dict(event,kind='expected',egress_port=line['egress_port'],l2_bytes=q,wire_bytes=q+20,
            outcome=outcome,late=late,service_ns=service,deadline_ns=deadline,release_ns=release,
            clamp_extra_ns=clamp,q_bytes=state['used'],q_drain_ns=-(-drain.numerator//drain.denominator))

    def finish(self):
        if not all(port['ended'] for port in self.full.ports.values()):
            raise ValueError('missing input end')
        return dict(input_complete=True,event_count=self.full.count,sample_checked=self.checked,
                    prefix_events=self.prefix,line_counts={name:dict(state['counts']) for name,state in self.full.lines.items()})
