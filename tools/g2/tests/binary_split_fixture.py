# SPDX-License-Identifier: Apache-2.0
"""Small, software-only bidirectional seven-writer native/pcap parity fixture."""
import copy
import hashlib
import json
from pathlib import Path
import struct
import tempfile

from tools.g2.calibration import Calibration
from tools.g2.matrix import report_one
from tools.g2.native_adapter import NativeAdapter
from tools.g2.native_binary import decode
from tools.g2.pcapng import convert_stream
from tools.g2.protocol import StreamReader, StreamWriter
from tools.g2.reference import U64, canonical, uint
from tools.g2.tests.binary_sample_fixture import END, _hash, _json, make_case as make_one_case
from tools.g2.tests.test_counters import OBSERVER_FIELDS, snapshot
from tools.g2.tests.test_native_binary import MAGIC, entry, frame, health, line


def _copy_rows(writer, path, role):
    for row in StreamReader(path,role=role,run='demo'):
        writer.emit(row['kind'],**{key:value for key,value in row.items()
                                  if key not in ('kind','run','stream_id','seq')})


def make_case(root, count=400):
    """Return the same document/source mapping as the single-direction fixture."""
    uint(count,'packets per direction')
    root=Path(root);root.mkdir(parents=True,exist_ok=True)
    cases=[make_one_case(root/name,count=count) for name in ('forward','reverse')]
    config=copy.deepcopy(cases[0]['config'])
    config['lines'].append(dict(copy.deepcopy(config['lines'][0]),name='reverse',ingress_port=1,egress_port=0))
    _json(root/'engine.json',config)
    registration=copy.deepcopy(cases[0]['legacy_manifest'])
    registration['config']['sha256']=_hash(root/'engine.json')
    registration['lines'].append(dict(registration['lines'][0],name='reverse',ingress_port=1,egress_port=0))
    registration['ports'].append(dict(registration['ports'][0],ingress_port=1,egress_port=0))
    registration['clocks']['observation']['1']='obs'
    registration['required_cells'].append(dict(registration['required_cells'][0],direction='1',line='reverse'))
    queues=registration['capacity']['handoff_queues']
    queues.append(dict(queues[0],handoff_queue_id='handoff-1'))
    for owner,queue in enumerate(queues):
        queue['extensions']=dict(native_actors=dict(
            producer=dict(stream_id='native-host',producer=1+owner),
            consumer=dict(stream_id='native-host',producer=3 if owner==0 else 6)))

    raw=[]
    for owner,name in enumerate(('forward','reverse')):
        records=[json.loads(value) for value in (root/name/'native.jsonl').read_text().splitlines()]
        direction=[]
        for row in records[1:-1]:
            if row['kind']==7: continue
            if owner:
                row['producer']={0:5,1:2,3:6}[row['producer']]
                row['port']=1 if row['kind']==4 else 0
                row['handoff_queue']=1
                for item in row['items']:
                    item[6],item[7]=item[7],item[6]
                    item[11],item[12],item[15]=1,1,1
                if row['kind']==6:
                    row['consumed_dispatch']=list(reversed(row['consumed_dispatch']))
            if row['kind']==6:
                row['snapshot']=[[0,count,U64,True],[1,count,U64,True]]
            direction.append(row)
        raw.append(direction)
    resources=[json.loads(value) for value in (root/'forward/native.jsonl').read_text().splitlines()[1:-1]
               if json.loads(value)['kind']==7]
    for row in resources:
        phase=row['resources'][0]
        row['resources'][4:6]=[count if phase else 0]*2
    # Both RX/scheduler streams and ENDs precede TX, so every snapshot prefix is closed.
    events=([row for row in resources if row['resources'][0]==0]
            +[row for direction in raw for row in direction if row['kind']!=6]
            +[row for direction in raw for row in direction if row['kind']==6]
            +[row for row in resources if row['resources'][0]==1])
    header=records[0];header['layout']='per-direction'
    counts=[0]*7;pending=bytearray();frame_seq=1
    with (root/'native.hngb').open('xb') as output:
        output.write(MAGIC+frame(1,line(header),0))
        for order,row in enumerate(events):
            producer=row['producer'];counts[producer]+=1
            common=[row['at_ns'],row['gate_ns'],row['submit_before_ns'],row['submit_after_ns'],
                    *row['resources'],row['actual_selection_seq'],row['attempt_id'],
                    *row['consumed_dispatch'],row['first_handoff_seq'],row['handoff_queue'],
                    row['port'],row['accepted_count']]
            encoded=entry(producer=producer,kind=row['kind'],reason=row['reason'],seq=counts[producer],
                          order=order,common=common,snapshot=row['snapshot'],items=row['items'])
            if len(pending)+len(encoded)>60000:
                output.write(frame(2,pending,frame_seq));frame_seq+=1;pending.clear()
            pending.extend(encoded)
        if pending: output.write(frame(2,pending,frame_seq));frame_seq+=1
        output.write(frame(3,line(health(tuple(counts))),frame_seq))
    with (root/'native.hngb').open('rb') as source,(root/'native.jsonl').open('x') as target:
        assert decode(source,target)['complete']
    with (root/'native.jsonl').open() as source:
        state=NativeAdapter(source,root/'ingress.jsonl',root/'host.jsonl',root/'native-counters.jsonl',
                            run='demo',lines=config['lines']).process()
        assert state['complete'],state

    # Give the reverse direction a distinct probe flow in both retained raw captures.
    for role in ('before','receiver'):
        path=root/'reverse'/f'{role}.pcapng';data=bytearray(path.read_bytes());cursor=0
        while cursor<len(data):
            kind,size=struct.unpack_from('<II',data,cursor)
            if kind==6:
                source=cursor+28+30;destination=source+4
                data[source:source+4],data[destination:destination+4]=data[destination:destination+4],data[source:source+4]
            cursor+=size
        path.write_bytes(data)
    calibration=copy.deepcopy(cases[0]['calibration'])
    calibration['pairs'][0]['evidence_sha256']=hashlib.sha256(canonical([
        _hash(root/name/f'{role}.pcapng') for name in ('forward','reverse') for role in ('before','receiver')])).hexdigest()
    _json(root/'calibration.json',calibration)
    checks=[]
    for owner,name in enumerate(('forward','reverse')):
        check=json.loads((root/name/'seqcheck.json').read_text())
        if owner: check['flows'][0]['flow_id']='c6336402:c0000201:1111:9000'
        for role,capture in (('ingress','before'),('receiver','receiver')):
            path=f'{name}/{capture}.pcapng'
            check['inputs'][role].update(path=path,sha256=_hash(root/path))
        _json(root/f'seqcheck-{name}.json',check)
        checks.append(dict(path=f'seqcheck-{name}.json',sha256=_hash(root/f'seqcheck-{name}.json'),
            ingress_port=owner,egress_port=1-owner,ingress_path=f'{name}/before.pcapng',
            receiver_path=f'{name}/receiver.pcapng'))
        pair=dict(schema='hng-pcapng-pair-v1',manifest='binary.json',clock_id='capture',mapping_id='ns',
                  uncertainty_ns=0,start_ns=0,end_ns=END,
                  before=dict(path=f'{name}/before.pcapng',interfaces={'0':owner}),
                  receiver=dict(path=f'{name}/receiver.pcapng',interfaces={'0':1-owner}))
        _json(root/f'pair-{name}.json',pair)
        for role in ('before','receiver'):
            stream=next(item for item in registration['streams'] if item['role']==role)
            clock=Calibration(calibration,'software-only','demo')
            state=convert_stream(pair,role,root,registration,stream,root/f'{name}-{role}.jsonl',clock,(0,END))
            assert state['complete'],state
    for role in ('before','receiver'):
        writer=StreamWriter(root/f'{role}.jsonl','demo',role,role,'obs',producer='synthetic-capture-fixture')
        for name in ('forward','reverse'): _copy_rows(writer,root/f'{name}-{role}.jsonl',role)
        writer.finish()
    counters=StreamWriter(root/'counters.jsonl','demo','native-counters','counters','engine',
                          producer='synthetic-capture-fixture')
    _copy_rows(counters,root/'native-counters.jsonl','counters')
    for role in ('before','receiver'):
        for phase in ('start','end'):
            values=dict.fromkeys(OBSERVER_FIELDS,0);values['queue_capacity']=65536
            if phase=='end': values.update(records_attempted=2*count,records_emitted=2*count,queue_high_water=1)
            row=snapshot('observer',role,phase,values,at_ns=END if phase=='end' else 0)
            for descriptor in row['descriptors'].values(): descriptor['source']='synthetic-software-capture-fixture'
            counters.emit('counter',**{key:value for key,value in row.items() if key not in ('kind','run','stream_id','seq')})
    counters.finish()
    registration['verification']=dict(mode='sampled',seqchecks=checks)
    sources={}
    for stream in registration['streams']:
        sources[stream['stream_id']]=root/stream['path']
        stream['file_sha256']=_hash(root/stream['path'])
    _json(root/'legacy.json',registration)
    binary=copy.deepcopy(registration)
    binary['streams']=[stream for stream in binary['streams'] if stream['role']=='counters']
    binary['verification'].update(native_binary=dict(path='native.hngb',sha256=_hash(root/'native.hngb')),
                                  capture_pairs=['pair-forward.json','pair-reverse.json'])
    _json(root/'binary.json',binary)
    return dict(config=config,legacy_manifest=registration,binary_manifest=binary,
                calibration=calibration,legacy_sources=sources)


def _selfcheck():
    assert callable(globals().get('make_case')), 'bidirectional binary fixture is missing'
    with tempfile.TemporaryDirectory() as directory:
        root=Path(directory);case=make_case(root)
        assert case['config']['lines'][1]['ingress_port'] == 1
        records=[json.loads(raw) for raw in (root/'native.jsonl').read_text().splitlines()]
        assert records[0]['layout'] == 'per-direction'
        assert all(producer['emitted'] for producer in records[-1]['producers'])
        assert len([row for row in records[1:-1] if row['kind']==7]) == 4
        for mode in ('legacy','binary'):
            status=report_one(root/f'{mode}.json',root/f'{mode}-report','demo','software-only')
            report=json.loads((root/f'{mode}-report/report.json').read_text())
            assert status == 0, (mode,status,report['reason_codes'])
            assert report['sampled_reference']['sample_checked'] == 800
            assert len(report['rows']) == 2
        for name in ('verdict.json','results.csv','summary.txt'):
            assert (root/'binary-report'/name).read_bytes() == (root/'legacy-report'/name).read_bytes(), name
        print('per-direction 7-writer, 400 packets/direction: PASS; verdict/CSV/summary byte-identical')


if __name__ == '__main__':
    _selfcheck()
