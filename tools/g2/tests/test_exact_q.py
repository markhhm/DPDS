# SPDX-License-Identifier: Apache-2.0
"""Integer service ticks must preserve the Fraction Q recurrence exactly."""
from collections import deque
import copy
from fractions import Fraction
import unittest

from tools.g2.exact_q import ExactQ
from tools.g2.reference import Reference, q_admit
from tools.g2.tests.test_reference import config, packet


def state(c_bps=8_000_000_000, capacity=160):
    return dict(config=dict(model=dict(c_bps=c_bps, q_bytes=capacity)),
                tail=Fraction(0), ledger=deque(), used=0)


class ExactQTests(unittest.TestCase):
    def test_closed_form_drop_and_completion_equality(self):
        queue = ExactQ(state())
        expected = [(100, 80), (200, 160), (None, 160), (300, 160)]
        for arrival, (finish_ns, used) in zip((0, 0, 0, 100), expected):
            finish = queue.admit(arrival, 80)
            self.assertEqual(finish, None if finish_ns is None else finish_ns * queue.C)
            self.assertEqual(queue.used, used)
        self.assertEqual(queue.tail, 300 * queue.C)
        self.assertEqual(list(queue.ledger), [(200 * queue.C, 80), (300 * queue.C, 80)])

    def test_variable_lengths_and_drops_match_fraction_per_packet(self):
        for c_bps in (3_000_000_000, 9_999_999_937, 10_000_000_000, 2**64 - 1):
            with self.subTest(c_bps=c_bps):
                baseline = state(c_bps, 4096)
                queue = ExactQ(copy.deepcopy(baseline))
                events = [((index // 4) * 350, (64, 80, 1522, 96, 128)[index % 5])
                          for index in range(800)]
                events += [(2**53 - 1, 64), (2**53, 80), (2**53 + 1, 96),
                           (2**64 - 2, 1522), (2**64 - 1, 64), (2**64 - 1, 1522)]
                for arrival, size in events:
                    expected = q_admit(baseline, arrival, size)
                    actual = queue.admit(arrival, size)
                    self.assertEqual(None if actual is None else Fraction(actual, queue.C), expected)
                    self.assertEqual(queue.used, baseline['used'])
                    self.assertEqual(Fraction(queue.tail, queue.C), baseline['tail'])
                    ticks = max(0, queue.tail - arrival * queue.C)
                    drain = max(Fraction(0), baseline['tail'] - arrival)
                    self.assertEqual(-(-ticks // queue.C), -(-drain.numerator // drain.denominator))
                self.assertGreater(queue.tail, 2**64)

    def test_warm_initialization_publish_and_restore_keep_exact_ledger(self):
        target = state(9_999_999_937, 4096)
        for size in (64, 80, 1522):
            q_admit(target, 1, size)
        baseline = copy.deepcopy(target)
        original_ledger = target['ledger']
        queue = ExactQ(target)
        for start in range(0, 120, 17):
            for index in range(start, min(start + 17, 120)):
                arrival, size = index * 63 + 1, 64 + index % 128
                expected = q_admit(baseline, arrival, size)
                actual = queue.admit(arrival, size)
                self.assertEqual(None if actual is None else Fraction(actual, queue.C), expected)
            queue.publish()
            self.assertEqual(target['used'], baseline['used'])
            self.assertEqual(target['tail'], baseline['tail'])
            self.assertIs(target['ledger'], original_ledger)
        queue.restore()
        self.assertEqual(target, baseline)
        for arrival, size in ((9000, 1522), (9001, 64), (10000, 96)):
            self.assertEqual(q_admit(target, arrival, size), q_admit(baseline, arrival, size))
        self.assertEqual(target, baseline)
        resumed = ExactQ(target)
        self.assertEqual(Fraction(resumed.admit(11000, 80), resumed.C), q_admit(baseline, 11000, 80))

    def test_restored_state_can_resume_reference_consume(self):
        cfg = config()
        cfg['lines'][0]['model'].update(c_bps=9_999_999_937, q_bytes=4096)
        actual, baseline = Reference(cfg, 'fixture'), Reference(cfg, 'fixture')
        queue = ExactQ(actual.lines['cable'])
        for arrival, size in ((0, 80), (100, 1522), (101, 80)):
            self.assertEqual(Fraction(queue.admit(arrival, size), queue.C),
                             q_admit(baseline.lines['cable'], arrival, size))
        queue.restore()
        for sequence in range(80):
            event = packet(sequence, 200 + sequence * 350, length=60 + sequence % 80)
            self.assertEqual(actual.consume(event), baseline.consume(event))

    def test_rejects_nonrepresentable_fraction_state_without_rounding(self):
        for field in ('tail', 'ledger'):
            target = state(10)
            if field == 'tail':
                target['tail'] = Fraction(1, 3)
            else:
                target['ledger'].append((Fraction(1, 3), 80))
                target['used'] = 80
            with self.subTest(field=field), self.assertRaises(ValueError):
                ExactQ(target)


if __name__ == '__main__':
    unittest.main()
