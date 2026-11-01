# SPDX-License-Identifier: Apache-2.0
"""Verified native/pcap columns feeding the shared sampled verdict code."""
from collections import Counter
import json
from pathlib import Path
from types import SimpleNamespace

import numpy as np

from .array_sampling import sample_mask
from .counters import normalize_counters
from .native_arrays import NativeArrays
from .native_projection import NativeProjection
from .pcapng import coverage_window
from .pcapng_arrays import CaptureArrays
from .protocol import StreamError, shape
from .reference import strict_json, uint


def _require(condition,code,message):
    if not condition:raise StreamError(code,message,'INCONCLUSIVE')


def _counter_key(row):return row['scope'],row['scope_id'],row['phase']


class NativeInput:
    def __init__(self,config,manifest,root,reference,cells,clock,counter_rows):
        self.config,self.manifest,self.root=config,manifest,Path(root)
        self.reference,self.cells,self.clock=reference,cells,clock
        self.expected=[];self.host=[];self.observations={'before':[],'receiver':[]}
        self.observed={'before':Counter(),'receiver':Counter()};self.readers={}
        self.cap=manifest['capacity']['timing_sample_capacity']*len(manifest['required_cells'])
        self.names={line['name']:i for i,line in enumerate(config['lines'])}
        masks={cell['mask_bits'] for cell in manifest['required_cells']}
        self.uniform=next(iter(masks)) if len(masks)==1 else None
        registration=manifest['verification']['native_binary']
        path=self.root/registration['path'];reader=NativeArrays(path)
        projection=NativeProjection(reader.header,manifest,config)
        self.projection=projection
        self._packet_cache={}
        chosen=[]
        for batch in reader.batches():
            links=projection.accept(batch)
            events,items=batch['events'],batch['items']
            kinds=events['kind'][items['event_index']]
            packets=items[kinds==0]
            wanted=self._wanted(packets)
            def materialize(i):
                item=packets[i];event_index=int(item['event_index'])
                return projection.packet(events[event_index],item,int(links['ingress_seq'][event_index]))
            self.expected.extend(reference.consume_columns(packets,wanted,materialize))
            chosen.append(packets[wanted].copy())
            _require(len(self.expected)<=self.cap,'SAMPLE_CAPACITY','selected sample bound exhausted')
            for port,state in projection.port_state.items():reference.full.ports[port].update(state)
            if self.uniform is not None:
                host=items[(kinds==1)|(kinds==2)]
                selected=self._host_selection(packets,wanted,host)
                for item in host[selected]:
                    event_index=int(item['event_index'])
                    self.host.append(projection.host(events[event_index],item,int(links['host_seq'][event_index])))
                _require(len(self.host)<=2*self.cap,'SAMPLE_CAPACITY','selected host evidence bound exhausted')
        _require(reader.integrity['sha256']==registration['sha256'],'SOURCE_HASH_MISMATCH','native capture differs from registration')
        _require(reader.health['complete'],'NATIVE_INCOMPLETE','native health seal is incomplete')
        generated=projection.counter_rows(reader.health,reader.counts,reader.seen)
        actual={_counter_key(row):{k:v for k,v in row.items() if k not in ('run','stream_id','seq')} for row in counter_rows}
        for row in generated:
            expected={k:v for k,v in row.items() if k not in ('run','stream_id','seq')}
            key=_counter_key(row)
            if key in actual:
                _require(actual[key]==expected,'NATIVE_COUNTER_MISMATCH','native counters differ from complete binary evidence')
        missing=[row for row in generated if _counter_key(row) not in actual]
        self.counters=normalize_counters(missing+counter_rows,manifest)
        self.chosen=np.concatenate(chosen) if chosen else np.empty(0,dtype=items.dtype)
        self._match_groups=None
        if self.uniform is None:self._host_pass(path,projection)
        self.runtime=projection.runtime
        self.inputs=Counter({str(port):values['rx_returned'] for port,values in projection.custody.items()})
        self.diagnostics=projection.diagnostics
        for role,count in zip(('ingress','host'),projection.logical_emitted):
            self.readers['native-'+role]=SimpleNamespace(header=dict(role=role),integrity=dict(
                stream_id='native-'+role,role=role,format='hng-native-v2',complete=True,
                record_count=count,file_sha256=reader.integrity['sha256']))
        self._captures(clock,self.counters)

    def _wanted(self,items):
        if not len(items):return np.zeros(0,dtype=bool)
        manifest=self.manifest
        covered=np.zeros(len(items),dtype=bool)
        for window in manifest['windows']:
            covered|=(items['arrival_ns']>=window['start_ns'])&(items['arrival_ns']<window['end_ns'])
        _require(np.all(covered),'CELL_WINDOW','arrival lies outside registered windows')
        if self.uniform is not None:
            return sample_mask(manifest['run'],manifest['sampling']['seed'],self.uniform,items)
        result=np.zeros(len(items),dtype=bool)
        windows={window['phase']:window for window in manifest['windows']}
        for cell in manifest['required_cells']:
            window=windows[cell['phase']]
            indices=np.flatnonzero((items['line']==self.names[cell['line']])&
                (items['ingress_port']==int(cell['direction']))&(items['arrival_ns']>=window['start_ns'])&
                (items['arrival_ns']<window['end_ns']))
            result[indices]=sample_mask(manifest['run'],manifest['sampling']['seed'],cell['mask_bits'],items[indices])
        return result

    def _host_selection(self,packets,wanted,host):
        if self.uniform==0:return np.ones(len(host),dtype=bool)
        for port in np.unique(packets['ingress_port']):
            positions=np.flatnonzero(packets['ingress_port']==port)
            cache=self._packet_cache.setdefault(int(port),[])
            cache.append((packets[positions].copy(),wanted[positions].copy()))
            del cache[:-2]
        result=np.zeros(len(host),dtype=bool);known=np.zeros(len(host),dtype=bool)
        for port,history in self._packet_cache.items():
            for rows,mask in history:
                first=int(rows['ingress_seq'][0])
                positions=np.flatnonzero((host['ingress_port']==port)&(host['ingress_seq']>=first)&
                                         (host['ingress_seq']<first+len(rows)))
                indices=(host['ingress_seq'][positions]-first).astype(np.int64)
                same=np.ones(len(positions),dtype=bool)
                for name in ('identity_kind','source','destination','source_port','destination_port','identity'):
                    same&=host[name][positions]==rows[name][indices]
                known[positions[same]]=True;result[positions[same]]=mask[indices[same]]
        remaining=np.flatnonzero(~known)
        result[remaining]=sample_mask(self.manifest['run'],self.manifest['sampling']['seed'],self.uniform,host[remaining])
        return result

    def _matches(self,rows,port,identity):
        wanted=np.zeros(len(rows),dtype=bool)
        fields=('source','destination','source_port','destination_port')
        if self._match_groups is None:
            self._match_groups=[]
            for line in self.config['lines']:
                chosen=self.chosen[self.chosen['line']==self.names[line['name']]]
                for flow in np.unique(chosen[list(fields)]):
                    selected=np.ones(len(chosen),dtype=bool)
                    for name in fields:selected&=chosen[name]==flow[name]
                    self._match_groups.append((line['ingress_port'],flow,np.unique(chosen['identity'][selected])))
        for ingress,flow,keys in self._match_groups:
            selected=port==ingress
            for name in fields:selected&=rows[name]==flow[name]
            indices=np.flatnonzero(selected);positions=np.searchsorted(keys,identity[indices])
            present=positions<len(keys)
            wanted[indices[present]]|=keys[positions[present]]==identity[indices[present]]
        return wanted

    def _host_pass(self,path,projection):
        # Different phase masks require the true ingress selection, not host arrival metadata.
        reader=NativeArrays(path);ordinal=0
        for batch in reader.batches():
            events,items=batch['events'],batch['items']
            kinds=events['kind'];emits=(kinds==1)|(kinds==2)|(kinds==6)|((kinds==5)&(events['count']>0))
            ordinals=np.cumsum(emits,dtype=np.int64)+ordinal
            ordinal+=int(emits.sum())
            host=items[np.isin(kinds[items['event_index']],(1,2))]
            for item in host[self._matches(host,host['ingress_port'],host['identity'])]:
                index=int(item['event_index']);self.host.append(projection.host(events[index],item,int(ordinals[index])))
            _require(len(self.host)<=2*self.cap,'SAMPLE_CAPACITY','selected host evidence bound exhausted')
        _require(reader.integrity['sha256']==self.manifest['verification']['native_binary']['sha256'],
                 'SOURCE_HASH_MISMATCH','native capture changed between column passes')

    def _captures(self,clock,counters):
        manifest=self.manifest;parts={'before':[],'receiver':[]};seen={'before':set(),'receiver':set()}
        registered=manifest['verification']['seqchecks']
        for entry in manifest['verification']['capture_pairs']:
            pair_path=self.root/entry;pair=strict_json(pair_path.read_bytes())
            shape(pair,('schema','manifest','clock_id','mapping_id','uncertainty_ns','start_ns','end_ns','before','receiver'))
            _require(pair['schema']=='hng-pcapng-pair-v1' and uint(pair['end_ns'],'end')>uint(pair['start_ns'],'start'),
                     'PCAPNG_PAIR','invalid capture pair schema/window')
            uint(pair['uncertainty_ns'],'uncertainty')
            mapping=clock.mappings[pair['mapping_id']]
            _require(mapping['raw_clock_id']==pair['clock_id'] and clock.clocks[pair['clock_id']]['unit']=='ns'
                and mapping['scale_num']==mapping['scale_den'] and mapping['raw_origin']==mapping['target_origin_ns']
                and all(target==mapping['target_clock_id'] for target in manifest['clocks']['observation'].values()),
                'CLOCK_PAIR_DOMAIN','capture requires a registered identity nanosecond mapping')
            endpoints=[value for name,value in counters.get('endpoints',{}).items() if name.startswith('port:')]
            _require(endpoints and all(p.get('start_ns') is not None and p.get('end_ns') is not None for p in endpoints),
                     'CLOCK_COVERAGE_MISSING','native counter endpoints are missing')
            coverage=coverage_window(clock,manifest,min(p['start_ns'] for p in endpoints),max(p['end_ns'] for p in endpoints),
                                     mapping['target_clock_id'],pair['uncertainty_ns'])
            _require(pair['start_ns']<=coverage[0]<coverage[1]<=pair['end_ns'] and
                mapping['valid_from_raw']<=coverage[0]<coverage[1]<mapping['valid_to_raw'],
                'CLOCK_COVERAGE_WINDOW','capture/mapping does not cover measurement and drain')
            for role in ('before','receiver'):
                shape(pair[role],('path','interfaces'))
                interfaces={int(k):v for k,v in pair[role]['interfaces'].items()}
                side='ingress_port' if role=='before' else 'egress_port'
                allowed={line[side] for line in manifest['lines']}
                _require(interfaces and len(set(interfaces.values()))==len(interfaces) and
                    not seen[role].intersection(interfaces.values()) and all(type(p) is int and p in allowed for p in interfaces.values()),
                    'PCAPNG_INTERFACE','captures require unique registered physical ports')
                seen[role].update(interfaces.values())
                path=(pair_path.parent/pair[role]['path']).resolve()
                for check in registered:
                    port=check['ingress_port'] if role=='before' else check['egress_port']
                    if port in interfaces.values():
                        source=check['ingress_path'] if role=='before' else check['receiver_path']
                        _require((self.root/source).resolve()==path,'SEQCHECK_SOURCE_HASH','seqcheck differs from converted capture source')
                reader=CaptureArrays(path,interfaces,pair['start_ns'],pair['end_ns'],manifest['identity_profile']['probe_port'])
                for rows in reader.batches():
                    line_ids=np.full(len(rows),-1,dtype=np.int16)
                    ingress=np.zeros(len(rows),dtype=np.uint16)
                    for i,line in enumerate(manifest['lines']):
                        selected=(rows['port']==line[side])&(rows['vlan_id']==line['vlan_id'])
                        _require(not np.any(line_ids[selected]>=0),'PCAPNG_IDENTITY','capture has ambiguous registered line')
                        line_ids[selected]=i;ingress[selected]=line['ingress_port']
                    _require(np.all(line_ids>=0),'PCAPNG_IDENTITY','capture has unknown port/VLAN')
                    for port in np.unique(ingress):self.observed[role][str(port)]+=int(np.count_nonzero(ingress==port))
                    wanted=self._matches(rows,ingress,rows['sequence'])
                    for i in np.flatnonzero(wanted):
                        row=rows[i];line=manifest['lines'][int(line_ids[i])]
                        flow=f"{int(row['source']):08x}:{int(row['destination']):08x}:{int(row['source_port'])}:{int(row['destination_port'])}"
                        stamp=int(row['raw']);ordinal=int(row['ordinal'])
                        item=dict(ingress_port=line['ingress_port'],egress_port=line['egress_port'],line=line['name'],
                            external_identity=f"{flow}:{int(row['sequence'])}",flow_id=flow,length=int(row['length']),
                            vlan_id=int(row['vlan_id']),app=None,selected_late=int(row['selected_late']),
                            timestamp=dict(raw=stamp,clock_id=pair['clock_id'],mapping_id=pair['mapping_id'],point='external_rx',uncertainty_ns=pair['uncertainty_ns']))
                        item.update(dict(before_seq=ordinal,ingress_ts_ns=stamp) if role=='before' else dict(receive_seq=ordinal,receive_ns=stamp))
                        self.observations[role].append(item)
                        _require(len(self.observations[role])<=2*self.cap,'SAMPLE_CAPACITY','selected capture evidence bound exhausted')
                parts[role].append(reader.integrity)
        for role,inputs in parts.items():
            allowed={line['ingress_port' if role=='before' else 'egress_port'] for line in manifest['lines']}
            _require(seen[role]==allowed,'PCAPNG_INTERFACE','capture coverage omits registered direction')
            self.readers[role]=SimpleNamespace(header=dict(role=role),integrity=dict(stream_id=role,role=role,
                format='pcapng-array',complete=True,record_count=sum(self.observed[role].values()),inputs=inputs))

    def rows(self,role):
        return iter(self.host if role=='host' else self.observations[role])
