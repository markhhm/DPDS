#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Known envelope examples and the explicit one-second backpressure budget."""
import unittest
from storage_capacity import plan
LAYOUT = dict(descriptor_bytes=192, cell_bytes=64, link_bytes=4,
              control_bytes=144, control_allocation=192, handle_bytes=16,
              core_slot_bytes=144, cache_bytes=0)
class CapacityTests(unittest.TestCase):
    def test_original_envelope_and_rounding_boundary(self):
        p = plan(90_000_000_000, 83_886_086, LAYOUT, hold_ns=0)
        self.assertEqual(p['records_per_direction'], 63_185_736)
        self.assertEqual(p['cells_per_direction'], 119_229_618)
        self.assertEqual(p['worst_wire_L2'], 69)
        self.assertEqual(p['worst_Q_L2'], 69)
    def test_full_terminal_residence_with_original_slot_limit(self):
        p = plan(90_000_000_000, 83_886_086, LAYOUT, hold_ns=1_000_000_000)
        self.assertEqual(p['records_per_direction'], 63_185_736)
        self.assertEqual(p['cells_per_direction'], 372_038_607)
        self.assertEqual(p['capacity_gate'], 'FAIL')
        self.assertGreater(p['node_budget_gib'], 64)
    def test_slot_extension_is_never_implicit(self):
        p = plan(90_000_000_000, 83_886_086, LAYOUT, extend_slots=True)
        self.assertEqual(p['records_per_direction'], 197_114_308)
        self.assertGreater(p['core_metadata_bytes'], 2*63_185_736*144)
    def test_node_half_is_stricter_than_the_64_gib_cap(self):
        p = plan(90_000_000_000, 83_886_086, LAYOUT, hold_ns=0, node_gib=96)
        self.assertEqual(p['allowed_gib'], 48)
        self.assertEqual(p['capacity_gate'], 'FAIL')
if __name__ == '__main__':
    unittest.main()
