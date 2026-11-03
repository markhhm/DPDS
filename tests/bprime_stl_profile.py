# SPDX-License-Identifier: Apache-2.0
"""Offline STL contract: packet templates and VM instructions, no server or NIC."""
import importlib.util
from ipaddress import IPv4Address
import json
import os
from pathlib import Path
import struct
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools.g2.probe import decode_probe
from tools.g2.pcapng import observation


class Packet:
    def __init__(self, kind, **fields):
        self.layers = [(kind, fields)]

    def __truediv__(self, other):
        result = Packet.__new__(Packet)
        result.layers = self.layers + other.layers
        return result

    def __bytes__(self):
        fields = dict(self.layers)
        payload = fields['Raw']['load']
        udp = fields['UDP']
        udp_bytes = struct.pack('!HHHH', udp['sport'], udp['dport'], 8+len(payload), udp['chksum'])
        ip = fields['IP']
        ip_bytes = struct.pack('!BBHHHBBHII', 0x45, 0, 28+len(payload), 0, 0, 64, 17, 0,
                               int(IPv4Address(ip['src'])), int(IPv4Address(ip['dst'])))
        ether = fields['Ether']
        mac = lambda value: bytes.fromhex(value.replace(':', ''))
        return (mac(ether['dst'])+mac(ether['src'])+b'\x81\x00'+
                struct.pack('!HH', fields['Dot1Q']['vlan'], 0x0800)+ip_bytes+udp_bytes+payload)

    def __len__(self):
        return len(bytes(self))


class FlowVar:
    def __init__(self, name, *, size, op, init_value=None, min_value=0, max_value=255,
                 step=1, split_to_cores=True):
        self.name, self.size, self.op = name, size, op
        self.initial = min_value if init_value is None else init_value
        self.minimum, self.maximum, self.step, self.split = min_value, max_value, step, split_to_cores


class WriteVar:
    def __init__(self, fv_name, pkt_offset, is_big=True):
        self.name, self.offset, self.big = fv_name, pkt_offset, is_big


class VM:
    def __init__(self, commands, cache_size=0):
        self.commands, self.cache_size = commands, cache_size


class Builder:
    def __init__(self, pkt, vm):
        self.pkt, self.vm = pkt, vm


class Rate:
    def __init__(self, *, pps):
        self.pps = pps


class Stream:
    def __init__(self, *, name, packet, mode, core_id=-1):
        self.name, self.packet, self.mode, self.core_id = name, packet, mode, core_id

    def packets(self, count):
        """Execute the two supported VM instructions; independent of profile code."""
        variables = {v.name: v.initial for v in self.packet.vm.commands if isinstance(v, FlowVar)}
        for _ in range(count):
            raw = bytearray(bytes(self.packet.pkt))
            for write in self.packet.vm.commands:
                if isinstance(write, WriteVar):
                    var = next(v for v in self.packet.vm.commands if isinstance(v, FlowVar) and v.name==write.name)
                    raw[write.offset:write.offset+var.size] = variables[var.name].to_bytes(var.size, 'big' if write.big else 'little')
            yield bytes(raw)
            for var in self.packet.vm.commands:
                if isinstance(var, FlowVar):
                    value = variables[var.name]+var.step
                    variables[var.name] = var.minimum if value>var.maximum else value


def stub_api():
    api = types.ModuleType('trex.stl.api')
    for kind in ('Ether', 'Dot1Q', 'IP', 'UDP'):
        setattr(api, kind, lambda _kind=kind, **fields: Packet(_kind, **fields))
    api.Raw = lambda load: Packet('Raw', load=load)
    for name, cls in [('STLPktBuilder', Builder), ('STLScVmRaw', VM), ('STLStream', Stream),
                      ('STLTXCont', Rate), ('STLVmFlowVar', FlowVar), ('STLVmWrFlowVar', WriteVar)]:
        setattr(api, name, cls)
    return {'trex':types.ModuleType('trex'), 'trex.stl':types.ModuleType('trex.stl'), 'trex.stl.api':api}


class BprimeProfileTests(unittest.TestCase):
    def setUp(self):
        self.env = {'BPRIME_VLAN':'710', 'BPRIME_CORES':'4'}
        for direction in (0, 1):
            prefix = f'BPRIME_P{direction}_'
            self.env.update({prefix+'SRC_MAC':f'02:00:00:00:00:{direction+1:02x}',
                             prefix+'DST_MAC':f'02:00:00:00:01:{direction+1:02x}',
                             prefix+'SRC_IP':str(IPv4Address(1+2*direction)),
                             prefix+'DST_IP':str(IPv4Address(2+2*direction))})
        self.addCleanup(patch.stopall)
        patch.dict(sys.modules, stub_api()).start()
        patch.dict(os.environ, self.env, clear=True).start()

    def profile(self):
        path = ROOT/'examples/m5/stl_bprime_x.py'
        self.assertTrue(path.is_file(), 'B-prime registered STL profile is missing')
        spec = importlib.util.spec_from_file_location('bprime_stl_under_test', path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module.register()

    def test_registered_equal_packet_mix_and_l1_per_direction(self):
        profile = self.profile()
        for direction in (0, 1):
            streams = profile.get_streams(direction=direction)
            self.assertEqual(len(streams), 64)
            rates = {60:0.0, 508:0.0, 1518:0.0}
            for stream in streams:
                rates[len(stream.packet.pkt)] += stream.mode.pps
            # Independent wire accounting: stored frame +4 FCS +20 preamble/IFG.
            self.assertAlmostEqual(sum(8*(length+24)*pps for length,pps in rates.items()), 10_000_000_000, delta=1)
            for pps in rates.values():
                self.assertAlmostEqual(pps, 10_000_000_000/(8*(84+532+1542)), delta=1e-6)

    def test_default_offer_remains_10g_with_quarter_rate_main_matrix(self):
        def offer(filename):
            matrix = json.loads((ROOT/'examples'/filename).read_text())
            return next(row for row in matrix['plan']['conditions']
                        if row['id']=='fixed-E3-142-320-1-both-L9-X')['lines'][0]['offered_bps']
        self.assertEqual(offer('m6-x710.json'), 2_500_000_000)
        self.assertEqual(offer('m6-x710-fullrate.json'), 10_000_000_000)
        # No BPRIME_MATRIX_FILE or BPRIME_RATE_BPS override: B-prime stays full rate.
        for direction in (0,1):
            streams = self.profile().get_streams(direction=direction)
            self.assertAlmostEqual(sum(s.mode.pps*8*(len(s.packet.pkt)+24) for s in streams),
                                   10_000_000_000, delta=1)

    def test_dense_sequences_and_single_core_ownership_for_each_flow(self):
        flows = set()
        for direction in (0, 1):
            for stream in self.profile().get_streams(direction=direction):
                self.assertIn(stream.core_id, range(4))
                self.assertEqual(stream.packet.vm.cache_size, 0)
                var = next(c for c in stream.packet.vm.commands if isinstance(c, FlowVar))
                self.assertEqual((var.initial,var.minimum,var.size,var.op,var.step,var.split), (0,0,8,'inc',1,False))
                packets = list(stream.packets(4))
                self.assertEqual([decode_probe(p[46:])[0] for p in packets], [0,1,2,3])
                identity = packets[0][30:38]+packets[0][38:42]
                self.assertNotIn(identity, flows)
                flows.add(identity)
        self.assertEqual(len(flows), 128)

    def test_vlan_lengths_and_existing_reporter_decoder(self):
        for vlan in (710, 711):
            os.environ['BPRIME_VLAN'] = str(vlan)
            config = json.loads((ROOT/f'examples/m5/bprime-{vlan}.json').read_text())
            manifest = dict(config, identity_profile=dict(probe_port=9000))
            for direction in (0, 1):
                for stream in self.profile().get_streams(direction=direction):
                    packet = next(stream.packets(1))
                    self.assertIn(len(packet), (60,508,1518))
                    self.assertEqual(packet[12:14], b'\x81\x00')
                    self.assertEqual(struct.unpack_from('!H',packet,14)[0], vlan)
                    row = observation(packet,len(packet),direction,'before',manifest)
                    self.assertEqual((row['ingress_port'],row['egress_port'],row['vlan_id']), (direction,1-direction,vlan))
                    self.assertEqual((decode_probe(packet[46:]), row['selected_late']), ((0,True),1))

    def test_matrix_sizes_flow_count_and_offer_are_read_not_repeated_constants(self):
        matrix = json.loads((ROOT/'examples/m6-x710-fullrate.json').read_text())
        matrix['plan']['traffic']['X'].update(q_bytes=[64,128,256], wire_bytes=[84,148,276], flows=7)
        condition = next(c for c in matrix['plan']['conditions'] if c['id']=='fixed-E3-142-320-1-both-L9-X')
        condition['lines'][0]['offered_bps'] = 2_000_000_000
        config = json.loads((ROOT/'examples/m5/bprime-710.json').read_text())
        for line in config['lines']:
            line['envelope']['max_q_bytes'] = 256
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp)
            (path/'matrix.json').write_text(json.dumps(matrix))
            (path/'config.json').write_text(json.dumps(config))
            os.environ.update(BPRIME_MATRIX_FILE=str(path/'matrix.json'),BPRIME_CONFIG_FILE=str(path/'config.json'))
            streams = self.profile().get_streams(direction=0)
            self.assertEqual(len(streams),7)
            rates = {60:0.0,124:0.0,252:0.0}
            for stream in streams:
                rates[len(stream.packet.pkt)] += stream.mode.pps
            for pps in rates.values():
                self.assertAlmostEqual(pps,2_000_000_000/(8*(84+148+276)),delta=1e-6)

    def test_operator_rate_and_network_values_are_used(self):
        os.environ['BPRIME_RATE_BPS'] = '5000000000'
        streams = self.profile().get_streams(direction=1)
        self.assertAlmostEqual(sum(s.mode.pps*8*(len(s.packet.pkt)+24) for s in streams),5_000_000_000,delta=1)
        packet = next(streams[0].packets(1))
        self.assertEqual(packet[:6],bytes.fromhex(self.env['BPRIME_P1_DST_MAC'].replace(':','')))
        self.assertEqual(packet[6:12],bytes.fromhex(self.env['BPRIME_P1_SRC_MAC'].replace(':','')))
        self.assertEqual(packet[30:34],IPv4Address(self.env['BPRIME_P1_SRC_IP']).packed)

    def test_opposite_directions_remain_distinct_with_identical_ip_addresses(self):
        os.environ['BPRIME_P1_SRC_IP'] = self.env['BPRIME_P0_SRC_IP']
        os.environ['BPRIME_P1_DST_IP'] = self.env['BPRIME_P0_DST_IP']
        identities = []
        for direction in (0,1):
            for stream in self.profile().get_streams(direction=direction):
                frame = next(stream.packets(1))
                identities.append(frame[30:42])
        self.assertEqual(len(set(identities)),128)

    def test_invalid_direction_vlan_rate_or_core_count_is_rejected(self):
        for direction in (-1,2,True,'0'):
            with self.subTest(direction=direction),self.assertRaises(ValueError):
                self.profile().get_streams(direction=direction)
        for name,value in [('BPRIME_VLAN','0'),('BPRIME_CORES','0'),('BPRIME_CORES','65'),
                           ('BPRIME_RATE_BPS','0'),('BPRIME_RATE_BPS','nan'),('BPRIME_RATE_BPS','10000000001')]:
            with self.subTest(name=name,value=value),patch.dict(os.environ,{name:value}),self.assertRaises(ValueError):
                self.profile().get_streams(direction=0)

    def test_config_vlan_mismatch_is_rejected(self):
        os.environ['BPRIME_CONFIG_FILE'] = str(ROOT/'examples/m5/bprime-711.json')
        with self.assertRaises(ValueError):
            self.profile().get_streams(direction=0)


if __name__ == '__main__':
    unittest.main()
