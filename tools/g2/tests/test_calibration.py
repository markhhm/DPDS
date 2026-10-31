# SPDX-License-Identifier: Apache-2.0
"""Raw clock fixtures are synthetic software evidence, never hardware acceptance."""
import copy
import unittest

from tools.g2.calibration import Calibration
from tools.g2.protocol import StreamError


def document():
    return dict(schema="hng-g2-calibration-v1", calibration_id="cal", run="demo",
        clocks=[dict(clock_id="before",kind="fake",unit="ticks",width_bits=64,
                     device_token="fake-before",mode="synthetic"),
                dict(clock_id="after",kind="fake",unit="ticks",width_bits=64,
                     device_token="fake-after",mode="synthetic"),
                dict(clock_id="obs",kind="fake",unit="ns",width_bits=64,
                     device_token="fake-target",mode="synthetic")],
        mappings=[dict(mapping_id="b",raw_clock_id="before",target_clock_id="obs",
                     raw_origin=0,target_origin_ns=10,scale_num=1,scale_den=2,
                     valid_from_raw=0,valid_to_raw=1000,residual_bound_ns=1),
                  dict(mapping_id="a",raw_clock_id="after",target_clock_id="obs",
                     raw_origin=0,target_origin_ns=10,scale_num=1,scale_den=1,
                     valid_from_raw=0,valid_to_raw=1000,residual_bound_ns=1)],
        pairs=[dict(pair_id="pair",ingress_clock_id="before",egress_clock_id="after",
                    target_clock_id="obs",offset_ns=3,offset_bound_ns=2,drift_bound_ns=2,
                    quantization_bound_ns=1,load_bound_ns=3,mapping_bound_ns=2,
                    uncertainty_ns=10,evidence_sha256="a"*64)],
        qualification=dict(scope="software-only",offered_bps=0,frame_profile_sha256="b"*64,
                    duration_ns=100,before_after_checked=True,counter_resets=0,observer_drops=0,
                    uncertainty_pass=True))


def timestamp(raw, clock="before", mapping="b", **changes):
    return dict(raw=raw,clock_id=clock,mapping_id=mapping,point="fake",uncertainty_ns=1,**changes)


class CalibrationTests(unittest.TestCase):
    def assert_error(self, action, status="INCONCLUSIVE"):
        with self.assertRaises(StreamError) as caught:
            action()
        self.assertEqual(caught.exception.measurement_status, status)
        self.assertTrue(caught.exception.code)

    def test_exact_half_even_mapping_above_float_integer_precision(self):
        calibration = Calibration(document(), "software-only", "demo")
        for raw, mapped in [(1,10),(3,12),(5,12),(7,14)]:
            with self.subTest(raw=raw):
                self.assertEqual(calibration.map_timestamp(timestamp(raw), mapped), mapped)
        doc = document()
        doc["mappings"][0].update(raw_origin=2**53,target_origin_ns=2**53,
                                  valid_from_raw=2**53,valid_to_raw=2**53+100)
        calibration = Calibration(doc, "software-only", "demo")
        self.assertEqual(calibration.map_timestamp(timestamp(2**53+3), 2**53+2), 2**53+2)

    def test_pair_preserves_raw_owd_and_known_offset_only(self):
        calibration = Calibration(document(), "software-only", "demo")
        before = dict(timestamp=timestamp(4), ingress_ts_ns=12)
        receive = dict(timestamp=timestamp(20,"after","a"),receive_ns=30)
        self.assertEqual(calibration.pair(before,receive),dict(raw_owd_ns=18,owd_ns=15,
                         uncertainty_ns=10,pair_id="pair",target_clock_id="obs"))
        self.assert_error(lambda: calibration.pair(before,dict(receive,receive_ns=31)), "INVALID")

    def test_missing_mapping_pair_extrapolation_backward_clock_and_width(self):
        calibration = Calibration(document(), "software-only", "demo")
        self.assert_error(lambda: calibration.map_timestamp(timestamp(0,mapping="missing"),10))
        self.assert_error(lambda: calibration.map_timestamp(timestamp(1000),510))
        self.assert_error(lambda: calibration.map_timestamp(timestamp(0,clock="after"),10))
        self.assert_error(lambda: calibration.map_timestamp(timestamp(2**64),10), "INVALID")
        calibration.map_timestamp(timestamp(9),14)
        self.assert_error(lambda: calibration.map_timestamp(timestamp(8),14))
        doc = document()
        doc["pairs"] = []
        no_pair = Calibration(doc,"software-only","demo")
        self.assert_error(lambda: no_pair.pair(dict(timestamp=timestamp(4),ingress_ts_ns=12),
                         dict(timestamp=timestamp(20,"after","a"),receive_ns=30)))

    def test_mapping_and_pair_validation_rejects_bad_types_unknown_fields_and_underbounds(self):
        mutations = [("mappings",0,"scale_num",0),("mappings",0,"scale_den",0),
                     ("mappings",0,"raw_origin",True),("mappings",0,"valid_to_raw",0),
                     ("mappings",0,"raw_clock_id","unknown"),("mappings",0,"typo",1),
                     ("clocks",0,"width_bits",65),("clocks",0,"unit","seconds"),
                     ("pairs",0,"offset_ns",2**63),("pairs",0,"uncertainty_ns",9),
                     ("pairs",0,"mapping_bound_ns",1),("pairs",0,"evidence_sha256","bad")]
        for section,index,key,value in mutations:
            doc = document()
            doc[section][index][key] = value
            with self.subTest(section=section,key=key):
                self.assert_error(lambda: Calibration(doc,"software-only","demo"))
        for doc in [dict(document(),run="other"),dict(document(),typo=1),
                    dict(document(),schema="old"),dict(document(),calibration_id="é")]:
            self.assert_error(lambda: Calibration(doc,"software-only","demo"))

    def test_duplicate_clocks_mappings_and_pairs_are_rejected(self):
        for section in ["clocks","mappings","pairs"]:
            doc = document()
            doc[section].append(copy.deepcopy(doc[section][0]))
            with self.subTest(section=section):
                self.assert_error(lambda: Calibration(doc,"software-only","demo"))

    def test_physical_scope_does_not_accept_fake_or_unqualified_evidence(self):
        self.assert_error(lambda: Calibration(document(),"physical","demo"))
        for changes in [dict(uncertainty_ns=10001),dict(drift_bound_ns=10001)]:
            doc = document()
            for clock in doc["clocks"]:
                clock.update(kind="nic_rx",mode="hardware")
            doc["qualification"]["scope"] = "physical"
            doc["pairs"][0].update(changes)
            self.assert_error(lambda: Calibration(doc,"physical","demo"))
        for key,value in [("counter_resets",1),("observer_drops",1),
                          ("before_after_checked",False),("uncertainty_pass",False)]:
            doc = document()
            doc["qualification"][key] = value
            with self.subTest(key=key):
                self.assert_error(lambda: Calibration(doc,"software-only","demo"))

    def test_pair_join_order_does_not_invent_a_backward_clock(self):
        calibration = Calibration(document(),"software-only","demo")
        first = dict(timestamp=timestamp(4),ingress_ts_ns=12)
        last = dict(timestamp=timestamp(20,"after","a"),receive_ns=30)
        self.assertEqual(calibration.pair(first,last)["owd_ns"],15)
        self.assertEqual(calibration.pair(dict(timestamp=timestamp(2),ingress_ts_ns=11),
                         dict(timestamp=timestamp(10,"after","a"),receive_ns=20))["owd_ns"],6)

    def test_mapping_anchors_cannot_hide_a_clock_step(self):
        for origin in [0,100]:
            doc = document()
            doc["mappings"][0]["valid_to_raw"] = 10
            doc["mappings"].append(dict(doc["mappings"][0],mapping_id="b-next",raw_origin=10,
                                       target_origin_ns=origin,valid_from_raw=10,valid_to_raw=20))
            with self.subTest(origin=origin):
                self.assert_error(lambda: Calibration(doc,"software-only","demo"))

    def test_mapping_validity_is_half_open_and_reported_integer_is_verified(self):
        calibration = Calibration(document(),"software-only","demo")
        self.assertEqual(calibration.map_timestamp(timestamp(999),510),510)
        self.assert_error(lambda: Calibration(document(),"software-only","demo").map_timestamp(
                          timestamp(3),11),"INVALID")
        self.assert_error(lambda: Calibration(document(),"software-only","demo").map_timestamp(
                          timestamp(3),True),"INVALID")
        self.assert_error(lambda: Calibration(document(),"software-only","demo").map_timestamp(
                          dict(timestamp(3),point="tx_submit"),12))
        self.assert_error(lambda: Calibration(document(),"software-only","demo").pair(
                          dict(timestamp=None,ingress_ts_ns=None),dict(timestamp=None,receive_ns=None)))


if __name__ == "__main__":
    unittest.main()
