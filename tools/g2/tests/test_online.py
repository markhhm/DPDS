# SPDX-License-Identifier: Apache-2.0
"""Online cell and retention checks use mixed hash-selected populations."""
import copy
import hashlib
import json
import unittest
from unittest.mock import patch

from tools.g2.online import Histogram, OnlineCells, ReceiveOrder, Retention, sample
from tools.g2.report import timing_sample


def manifest():
    return dict(run="demo",condition_id="fixture",scope="software-only",
        sampling=dict(seed=17,mask_bits=10,oversample_apps=["app-a","app-b"]),
        retention=dict(mode="stream",debug_windows=[],mismatch_cap=10),
        capacity=dict(max_unresolved=16,timing_sample_capacity=10000),
        windows=[dict(phase="steady",start_ns=0,end_ns=10**12,clock_id="engine")],
        required_cells=[dict(condition_id="fixture",repeat=0,direction="0",line=line,phase="steady",mask_bits=10)
                        for line in ("cable","leo")])


def expected(seq,line="cable",**changes):
    row=dict(kind="expected",run="demo",ingress_port=0,egress_port=1,line=line,
             external_identity=f"f:{seq}",flow_id="f",ingress_seq=seq,selection_seq=seq,
             arrival_ns=seq*100,service_ns=seq*100+100,deadline_ns=seq*100+1000,
             release_ns=seq*100+1000,outcome="accepted",late=False,length=76,
             l2_bytes=80,wire_bytes=100,q_bytes=80,q_drain_ns=100,app="app-a",selected_late=None)
    row.update(changes)
    return row


class OnlineTests(unittest.TestCase):
    def test_online_sampling_and_histograms_have_bounded_per_record_cost(self):
        cells=OnlineCells(manifest())
        with patch("tools.g2.online.sample", wraps=sample) as sampled, \
                patch("tools.g2.online.Histogram", wraps=Histogram) as histogram:
            for seq in range(16):
                row=expected(seq)
                cells.expect(row)
                cells.delivery(row,1000,0)
            with self.subTest(cost="histogram allocation"):
                self.assertEqual(histogram.call_count,4*sum(len(cell["groups"]) for cell in cells.cells.values()))
            with self.subTest(cost="sampling hash"):
                self.assertEqual(sampled.call_count,32)
        row=cells.finish()["rows"][0]
        self.assertEqual(row["counts"]["received"],16)
        self.assertEqual(sum(group.get("population",0) for name,group in row["timing"].items()
                             if name in ("main","app_extra:app-a")),16)

    def test_line_gates_cannot_be_masked_and_retained_records_are_only_main(self):
        cells=OnlineCells(manifest())
        n=500_000
        for line in ("cable","leo"):
            for seq in range(n):
                row=expected(seq,line,external_identity=f"{line}:{seq}")
                cells.expect(row)
                cells.delivery(row,1000+(600_000 if line=="leo" and seq%20==0 else 0),0)
        report=cells.finish()
        rows={row["line"]:row for row in report["rows"]}
        self.assertEqual(rows["cable"]["status"],"PASS")
        self.assertEqual(rows["leo"]["gates"]["p99"]["status"],"FAIL")
        self.assertEqual(report["status"],"FAIL")
        self.assertFalse(report["g2_qualified"])
        for row in rows.values():
            self.assertGreaterEqual(row["timing"]["main"]["n"],368)
            self.assertLess(row["timing"]["main"]["n"],1000)
            self.assertEqual(row["timing"]["app_extra:app-a"]["n"],n-row["timing"]["main"]["n"])
            self.assertEqual(row["counts"]["probe_sent"],0)

    def test_receive_seq_is_authoritative_and_gaps_are_inconclusive(self):
        ordered=ReceiveOrder(4)
        self.assertEqual(ordered.push(dict(egress_port=1,receive_seq=1,external_identity="second")),[])
        ready=ordered.push(dict(egress_port=1,receive_seq=0,external_identity="first"))
        self.assertEqual([row["external_identity"] for row in ready],["first","second"])
        self.assertEqual(ordered.finish(),[])
        ordered.push(dict(egress_port=1,receive_seq=3,external_identity="later"))
        self.assertEqual(ordered.finish(),["receive_sequence_gap"])

    def test_retention_defaults_to_sampling_and_debug_has_explicit_duration_bound(self):
        registration=manifest()
        retain=Retention(registration)
        selected=0
        for seq in range(10000):
            row=expected(seq)
            selected+=retain.keep(row)
        independent=sum(timing_sample(expected(seq),17) for seq in range(10000))
        self.assertEqual(selected,independent)
        self.assertGreater(selected,0)
        self.assertLess(selected,100)
        self.assertEqual(retain.mode,"stream")
        physical=copy.deepcopy(registration)
        physical["scope"]="physical"
        physical["retention"].update(mode="full")
        with self.assertRaises(ValueError):
            Retention(physical)
        registration["retention"]["debug_windows"]=[dict(phase="steady",start_ns=0,end_ns=2_000_000_001)]
        with self.assertRaises(ValueError):
            Retention(registration)

    def test_proved_loss_and_observer_gap_have_distinct_verdicts(self):
        for complete,want in [(True,"FAIL"),(False,"INVALID")]:
            cells=OnlineCells(manifest())
            row=expected(0)
            cells.expect(row)
            cells.loss(row,"delay_storage_drop",complete=complete)
            result=cells.finish()
            self.assertEqual(result["rows"][0]["status"],want)
            if not complete:
                self.assertEqual(result["rows"][0]["measurement_status"],"INCONCLUSIVE")

    def test_phase_assignment_uses_ingress_and_not_late_output_time(self):
        registration=manifest()
        registration["windows"]=[dict(phase="steady",start_ns=0,end_ns=100,clock_id="engine"),
                                 dict(phase="drain",start_ns=100,end_ns=1000,clock_id="engine")]
        cells=OnlineCells(registration)
        row=expected(0,arrival_ns=99,release_ns=500)
        cells.expect(row)
        cells.delivery(row,401,0)
        self.assertEqual(cells.finish()["rows"][0]["counts"]["ingress"],1)

    def test_mismatch_cap_counts_all_errors_without_persisting_them_all(self):
        retain=Retention(manifest())
        for seq in range(11):
            retain.keep(expected(seq),mismatch=True)
        self.assertEqual(retain.mismatch_count,11)
        self.assertEqual(retain.issues,["mismatch_cap_exhausted"])

    def test_main_clock_bound_and_small_sample_remain_inconclusive(self):
        cells=OnlineCells(manifest())
        row=next(expected(seq) for seq in range(10000) if timing_sample(expected(seq),17))
        cells.expect(row)
        cells.delivery(row,1000,0)
        first=cells.finish()["rows"][0]
        self.assertEqual(first["measurement_status"],"INCONCLUSIVE")
        self.assertIn("insufficient_main_samples",first["reason_codes"])

    def test_delivery_of_intentionally_dropped_packet_is_a_datapath_failure(self):
        cells=OnlineCells(manifest())
        row=expected(0,outcome="q_drop",service_ns=None,deadline_ns=None,release_ns=None)
        cells.expect(row)
        cells.delivery(row,1000,0)
        self.assertEqual(cells.finish()["rows"][0]["status"],"FAIL")

    def test_histograms_count_nonzero_errors_exactly_inside_zero_microsecond_bin(self):
        cells=OnlineCells(manifest())
        row=next(expected(seq) for seq in range(10000) if timing_sample(expected(seq),17))
        cells.expect(row)
        cells.delivery(row,1001,0)
        histogram=cells.finish()["rows"][0]["timing"]["main"]["signed_error"]
        self.assertEqual(histogram["nonzero"],1)
        self.assertEqual(histogram["max_ns"],1)

    def test_mixed_run_cannot_change_hash_selection_or_rescue_cell(self):
        cells=OnlineCells(manifest())
        with self.assertRaises(ValueError):
            cells.expect(expected(0,run="other"))

    def test_phase_specific_mask_is_registered_and_does_not_change_default_sample(self):
        registration=manifest()
        registration["windows"]=[dict(phase="steady",start_ns=0,end_ns=100,clock_id="engine"),
                                 dict(phase="transition",start_ns=100,end_ns=1000,clock_id="engine")]
        registration["required_cells"].append(dict(condition_id="fixture",repeat=0,direction="0",line="cable",phase="transition",mask_bits=0))
        retain=Retention(registration)
        row=next(expected(seq,arrival_ns=200) for seq in range(10000) if not timing_sample(expected(seq),17))
        self.assertTrue(retain.keep(row))
        self.assertFalse(retain.keep(dict(row,arrival_ns=0)))

    def test_three_timing_faults_have_mixed_population_and_unchanged_limits(self):
        registrations=[manifest() for _ in range(3)]
        for registration in registrations:
            registration["required_cells"]=registration["required_cells"][:1]
        cells=[OnlineCells(registration) for registration in registrations]
        selected=0
        affected=[0,0,0]
        for seq in range(440_433):
            row=expected(seq)
            key=json.dumps(["demo",0,f"f:{seq}"],separators=(",",":"),ensure_ascii=True).encode()
            main=int.from_bytes(hashlib.sha256(b"hng-g2-timing-v1\0"+(17).to_bytes(8,"big")+key).digest()[:8],"big")&1023==0
            selected+=main
            errors=[200_000 if seq%5<3 else 0,200_000 if seq%20==0 else 0,600_000 if seq%20==0 else 0]
            for index,cell in enumerate(cells):
                affected[index]+=main and errors[index]>0
                cell.expect(row)
                cell.delivery(row,1000+errors[index],0)
        self.assertGreaterEqual(selected,368)
        rows=[cell.finish()["rows"][0] for cell in cells]
        self.assertEqual([row["status"] for row in rows],["FAIL","PASS","FAIL"])
        self.assertEqual(rows[0]["timing"]["main"]["median_abs_error_ns"],200_000)
        self.assertEqual(rows[0]["gates"]["median"]["status"],"FAIL")
        self.assertEqual(rows[0]["gates"]["p99"]["status"],"PASS")
        for index,row in enumerate(rows):
            self.assertEqual(row["timing"]["main"]["n"],selected)
            self.assertEqual(row["timing"]["main"]["absolute_error"]["nonzero"],affected[index])
        self.assertEqual(rows[1]["timing"]["main"]["p99_abs_error_ns"],200_000)
        self.assertEqual(rows[1]["timing"]["main"]["signed_error"]["max_ns"],200_000)
        self.assertEqual(rows[2]["gates"]["p99"]["status"],"FAIL")

    def test_missing_extra_pair_cannot_be_silently_discarded(self):
        cells=OnlineCells(manifest())
        row=next(expected(seq,late=True) for seq in range(10000) if not timing_sample(expected(seq),17))
        cells.expect(row)
        result=cells.finish()["rows"][0]
        self.assertIn("missing_extra_pairs",result["reason_codes"])

    def test_unsampled_identity_needs_order_evidence_but_not_a_timestamp(self):
        registration=manifest()
        registration["sampling"]["oversample_apps"]=[]
        cells=OnlineCells(registration)
        row=next(expected(seq,app=None) for seq in range(10000) if not timing_sample(expected(seq),17))
        cells.expect(row)
        cells.delivery(row,None,0)
        report=cells.finish()["rows"][0]
        self.assertEqual(report["counts"]["received"],1)
        self.assertNotIn("missing_main_pairs",report["reason_codes"])

    def test_diagnostic_cells_cannot_change_the_required_cell_verdict(self):
        registration=manifest()
        registration["required_cells"][1]["required"]=False
        cells=OnlineCells(registration)
        cells.violation(expected(0,"leo"),"unexpected_order")
        report=cells.finish()
        self.assertEqual(report["status"],"PASS")
        self.assertFalse(report["rows"][1]["required"])

    def test_empty_strata_have_explicit_applicability(self):
        report=OnlineCells(manifest()).finish()
        timing=report["rows"][0]["timing"]
        for name in ("main","ordinary-main","late-main","late_extra","app_extra:app-a","app_extra:app-b"):
            self.assertEqual(timing[name]["status"],"not_applicable")
