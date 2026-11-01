# SPDX-License-Identifier: Apache-2.0
"""Exact serial Q service in integer ticks of 1/C nanoseconds."""
from collections import deque
from fractions import Fraction


class ExactQ:
    """Own a Reference line's Q recurrence until restore permits legacy writes.

    Arrivals and sizes are validated Python integers supplied by the caller.
    After legacy code changes the restored state, construct a new ExactQ.
    """
    def __init__(self, state):
        self.state = state
        self.C = state['config']['model']['c_bps']
        if type(self.C) is not int or self.C <= 0:
            raise ValueError('Q service rate must be a positive integer')
        self.capacity = state['config']['model']['q_bytes']
        self.tail = self._ticks(state['tail'])
        self.ledger = deque((self._ticks(finish), size) for finish, size in state['ledger'])
        self.used = state['used']

    def _ticks(self, value):
        value = Fraction(value)
        ticks, remainder = divmod(value.numerator * self.C, value.denominator)
        if remainder:
            raise ValueError('Q service state is not representable in 1/C nanosecond ticks')
        return ticks

    def admit(self, arrival, q):
        arrival_ticks = arrival * self.C
        ledger = self.ledger
        while ledger and ledger[0][0] <= arrival_ticks:
            self.used -= ledger.popleft()[1]
        if self.used + q > self.capacity:
            return None
        finish = max(arrival_ticks, self.tail) + 8 * (q + 20) * 10**9
        self.tail = finish
        ledger.append((finish, q))
        self.used += q
        return finish

    def publish(self):
        """Expose current used/tail in O(1); state.ledger remains stale."""
        self.state['used'] = self.used
        self.state['tail'] = Fraction(self.tail, self.C)

    def restore(self):
        """Rebuild the Fraction ledger before handing state to legacy consume."""
        self.publish()
        self.state['ledger'] = deque((Fraction(finish, self.C), size) for finish, size in self.ledger)
