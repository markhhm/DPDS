#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
import unittest
import storage_hybrid_capacity as capacity
from storage_hybrid_capacity import plan, scan
LAYOUT=dict(descriptor_bytes=192,cell_bytes=64,link_bytes=4,item_bytes=16,
            hybrid_control_bytes=80,control_allocation=192,core_slot_bytes=144,
            mbuf_stride=2624,mempool_header_bytes=1056960)
class JointTests(unittest.TestCase):
    def test_no_feasible_registered_budget_still_has_reportable_thresholds(self):
        scans={str(c):scan(c,c*7_456_541//8_000_000_000,LAYOUT,hold_ns=1_000_000_000,node_gib=32)
               for c in (80_000_000_000,90_000_000_000)}
        self.assertIsNone(scans['90000000000']['first_joint_feasible_threshold'])
        self.assertEqual(capacity.selected_thresholds(scans),[128,256,512,663,1024,1521,1522])
    def test_retained_branch_boundary(self):
        for t in (128,256,512):
            row=plan(90_000_000_000,83_886_086,LAYOUT,t)
            self.assertEqual(row['worst_wire_L2'],t+1)
            self.assertEqual(row['worst_Q_L2'],t+1)
    def test_compact_rounding_can_be_global_worst(self):
        row=plan(90_000_000_000,83_886_086,LAYOUT,1024)
        self.assertEqual(row['worst_wire_L2'],69)
        self.assertEqual(row['worst_wire_ratio'],'344/89')
        self.assertEqual(row['records_per_direction'],64_525_022)
    def test_actual_fixed_pool_is_not_the_joint_live_bound(self):
        row=plan(90_000_000_000,83_886_086,LAYOUT,663)
        self.assertLess(row['joint_node_gib'],64)
        self.assertGreater(row['fixed_node_gib'],64)
        self.assertFalse(row['shared_backing_implemented'])
    def test_registered_hold_changes_the_budget(self):
        short=plan(90_000_000_000,83_886_086,LAYOUT,663,hold_ns=10_000_000)
        long=plan(90_000_000_000,83_886_086,LAYOUT,663,hold_ns=1_000_000_000)
        self.assertGreater(long['joint_node_gib'],short['joint_node_gib'])
    def test_scan_tie_breaks_to_lowest_threshold(self):
        rows=scan(90_000_000_000,83_886_086,LAYOUT)
        self.assertEqual(rows['knee_threshold'],663)
if __name__=='__main__':unittest.main()
