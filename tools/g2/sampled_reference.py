# SPDX-License-Identifier: Apache-2.0
"""Exact arrival prefixes with expected records only for selected identities."""
from fractions import Fraction
from .exact_q import ExactQ
from .reference import Reference, U64, q_admit, selected, trace_decision, uint


class SampledReference:
    def __init__(self, config, run, traces=None, timing_mode='stress'):
        self.full = Reference(config,run,traces)
        self.mode = timing_mode
        self.checked = 0
        self.prefix = 0
        self._column_q = {}

    def consume_columns(self, items, wanted, make_event):
        """Validate arrival columns; retain the exact serial Q/trace recurrence."""
        import numpy as np
        ref=self.full;result=[]
        if len(items)!=len(wanted):raise ValueError('sample mask length differs from arrivals')
        for port_id in np.unique(items['ingress_port']):
            port=ref.ports[int(port_id)];rows=items[items['ingress_port']==port_id]
            arrivals=rows['arrival_ns'];count=len(rows)
            if (port['ended'] or port['next_seq']+count>U64 or
                    np.any(rows['ingress_seq']!=np.arange(count,dtype=np.uint64)+port['next_seq']) or
                    np.any(arrivals[1:]<arrivals[:-1]) or int(arrivals[0])<port['arrival'] or
                    int(arrivals[0])<=port['frontier']):
                raise ValueError('invalid sampled port arrival prefix')
            port.update(next_seq=port['next_seq']+count,arrival=int(arrivals[-1]))
        names=list(ref.lines)
        for line_id in np.unique(items['line']):
            index=int(line_id)
            if index>=len(names):raise ValueError('unregistered sampled line')
            state=ref.lines[names[index]];line=state['config'];model=line['model']
            indices=np.flatnonzero(items['line']==line_id);rows=items[indices];count=len(rows)
            ordinal=state['seq'];q=np.maximum(rows['length'].astype(np.uint64)+4,64)
            if (ordinal+count>U64 or np.any(rows['selection_seq']!=np.arange(count,dtype=np.uint64)+ordinal)
                    or np.any(rows['ingress_port']!=line['ingress_port']) or np.any(rows['vlan_id']!=line['vlan_id'])
                    or np.any(rows['segments']!=1) or np.any(rows['input_error']) or np.any(rows['length']==0)
                    or np.any(q<line['envelope']['min_q_bytes']) or np.any(q>line['envelope']['max_q_bytes'])):
                raise ValueError('invalid sampled line arrival prefix')
            prefix=self.mode=='stress' or state['trace'] is not None
            choices=wanted[indices]

            def record(local,finish,outcome,service=None,deadline=None,release=None,clamp=0):
                i=int(indices[local]);event=make_event(i)
                late=selected(state['chance'],model['seed'],ordinal+local)
                if finish is not None and state['trace'] is None:
                    service=-(-finish.numerator//finish.denominator)
                    release=deadline=service+model['t_ns']+late*model['delta_ns']
                drain=max(Fraction(0),(finish if finish is not None else state['tail'])-int(rows['arrival_ns'][local]))
                result.append((i,dict(event,kind='expected',egress_port=line['egress_port'],
                    l2_bytes=int(q[local]),wire_bytes=int(q[local])+20,outcome=outcome,late=late,
                    service_ns=service,deadline_ns=deadline,release_ns=release,clamp_extra_ns=clamp,
                    q_bytes=state['used'],q_drain_ns=-(-drain.numerator//drain.denominator))))

            if prefix:
                self.prefix+=count
                kernel=self._column_q.get(names[index])
                if kernel is None:
                    kernel=self._column_q[names[index]]=ExactQ(state)
                for local,(arrival,size,choice) in enumerate(zip(rows['arrival_ns'].tolist(),q.tolist(),choices.tolist())):
                    ticks=kernel.admit(arrival,size)
                    outcome='q_drop' if ticks is None else 'accepted'
                    service=deadline=release=None;clamp=0
                    if ticks is not None and state['trace'] is not None:
                        service=-(-ticks//kernel.C)
                        deadline,release,outcome,clamp=trace_decision(state,service,ordinal+local)
                    state['counts'][outcome]+=1
                    if choice:
                        kernel.publish()
                        record(local,None if ticks is None else Fraction(ticks,kernel.C),outcome,service,deadline,release,clamp)
                kernel.publish()
            else:
                services={int(value):Fraction(8*(int(value)+20)*10**9,model['c_bps']) for value in np.unique(q)}
                accepted=q<=model['q_bytes']
                state['counts']['accepted']+=int(accepted.sum());state['counts']['q_drop']+=count-int(accepted.sum())
                for local in np.flatnonzero(choices):
                    local=int(local);size=int(q[local]);state['used']=size if accepted[local] else 0
                    finish=Fraction(int(rows['arrival_ns'][local]))+services[size] if accepted[local] else None
                    record(local,finish,'accepted' if accepted[local] else 'q_drop')
                state['used']=int(q[-1]) if accepted[-1] else 0
            state['seq']+=count;state['counts']['offers']+=count
        ref.count+=len(items);self.checked+=int(np.count_nonzero(wanted))
        yield from (row for _,row in sorted(result,key=lambda pair:pair[0]))

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
        previous=self._column_q.pop(name,None)
        if previous is not None:previous.restore()
        ordinal = uint(event['selection_seq'],'line ordinal',U64-1)
        q = max(uint(event['length'],'length',2**32-1)+4,64)
        line, model = state['config'], state['config']['model']
        if (port['ended'] or seq != port['next_seq'] or arrival < port['arrival'] or arrival <= port['frontier']
                or ordinal != state['seq'] or line['ingress_port'] != event['ingress_port']
                or not line['envelope']['min_q_bytes'] <= q <= line['envelope']['max_q_bytes']
                or not event['length'] or event.get('input_error') or event.get('segments',1) != 1
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
