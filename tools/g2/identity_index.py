# SPDX-License-Identifier: Apache-2.0
"""Volatile exact identity/outcome tags, bounded separately from active joins."""
import platform
import sys

from .protocol import identifier
from .reference import uint

OUTCOMES = ("accepted", "q_drop", "trace_loss", "trace_outage", "invalid_input")


def _size(value):
    total = sys.getsizeof(value)
    if isinstance(value, dict):
        total += sum(_size(key)+_size(item) for key,item in value.items())
    elif isinstance(value, (tuple, list, set)):
        total += sum(_size(item) for item in value)
    return total


class IdentityIndex:
    def __init__(self, manifest):
        self.manifest = manifest
        self.run = identifier(manifest["run"], "run")
        self.capacity = uint(manifest["capacity"].get("identity_index_capacity"), "identity index capacity")
        if not self.capacity:
            raise ValueError("identity index capacity must be positive")
        self.cells = sorted({(str(row["direction"]), row["line"], row["phase"])
                             for row in manifest["required_cells"]})
        self.cell_ids = {key:i for i,key in enumerate(self.cells)}
        self.entries = {}
        self.ports = {uint(row["ingress_port"], "ingress port", 65535) for row in manifest["ports"]}
        self.closed = False
        self.peak_entries = 0
        self.overflows = [0]*len(self.cells)
        self.breakdown = dict(allocation=0, unknown_after_discard=0)
        self.tainted_ports = set()
        self.empty_table_bytes = sys.getsizeof({})
        self.table_slot_estimate = sys.getsizeof({0:None})-self.empty_table_bytes
        self.max_entry_bytes = _size((65535, "x"*128))+_size((4, False, max(0,len(self.cells)-1)))
        self.payload_bytes = 0
        self.peak_table_bytes = self.empty_table_bytes

    @property
    def live_entries(self):
        return len(self.entries)

    def __enter__(self):
        if self.closed:
            raise ValueError("identity index is closed")
        return self

    def __exit__(self, *exception):
        self.close()

    def close(self):
        self.entries.clear()
        self.closed = True

    def _key(self, row):
        if self.closed:
            raise ValueError("identity index is closed")
        if row["run"] != self.run:
            raise ValueError("mixed identity-index run")
        port = uint(row["ingress_port"], "ingress port", 65535)
        if port not in self.ports:
            raise ValueError("identity input port is not registered")
        return port, str(identifier(row["external_identity"], "external identity"))

    def _cell(self, row):
        arrival = uint(row["arrival_ns"], "arrival")
        phases = [window["phase"] for window in self.manifest["windows"]
                  if window["start_ns"] <= arrival < window["end_ns"]]
        if len(phases) != 1:
            raise ValueError("identity input has missing/overlapping phase")
        if row["line"] is None and row["outcome"] == "invalid_input":
            return None
        key = str(row["ingress_port"]), row["line"], phases[0]
        if key not in self.cell_ids:
            raise ValueError("identity input cell is not registered")
        return self.cell_ids[key]

    def _fact(self, label, port, state=None):
        cells = [state[2]] if state is not None and state[2] is not None else [
            i for i,key in enumerate(self.cells) if key[0] == str(port)]
        return {"class":label, "outcome":OUTCOMES[state[0]] if state is not None else None,
                "cells":[dict(direction=self.cells[i][0], line=self.cells[i][1], phase=self.cells[i][2]) for i in cells]}

    def _overflow(self, port, reason, state=None):
        self.tainted_ports.add(port)
        self.breakdown[reason] += 1
        fact = self._fact("join_overflow", port, state)
        for cell in fact["cells"]:
            self.overflows[self.cell_ids[cell["direction"], cell["line"], cell["phase"]]] += 1
        return fact

    def remember(self, row):
        key = self._key(row)
        if row["outcome"] not in OUTCOMES:
            raise ValueError("unknown model outcome")
        if key in self.entries:
            return self._fact("duplicate_ingress", key[0], self.entries[key])
        state = OUTCOMES.index(row["outcome"]), False, self._cell(row)
        if len(self.entries) >= self.capacity:
            return self._overflow(key[0], "allocation", state)
        self.entries[key] = state
        self.peak_entries = max(self.peak_entries, len(self.entries))
        size = _size(key)+_size(state)
        self.payload_bytes += size
        self.max_entry_bytes = max(self.max_entry_bytes, size)
        table_bytes = sys.getsizeof(self.entries)
        self.peak_table_bytes = max(self.peak_table_bytes, table_bytes)
        self.table_slot_estimate = max(self.table_slot_estimate,
            (table_bytes-self.empty_table_bytes+len(self.entries)-1)//len(self.entries))
        return self._fact("indexed", key[0], state)

    def receive(self, row, *, input_complete):
        if type(input_complete) is not bool:
            raise ValueError("input completeness must be boolean")
        key = self._key(row)
        state = self.entries.get(key)
        if state is None:
            if key[0] in self.tainted_ports:
                return self._overflow(key[0], "unknown_after_discard")
            return self._fact("receiver_unknown_identity" if input_complete else "pending", key[0])
        label = "duplicate_delivery" if state[1] else "delivery" if state[0] == 0 else "delivered_intended_drop"
        self.entries[key] = state[0], True, state[2]
        return self._fact(label, key[0], state)

    def finish(self):
        self.close()
        auxiliary = sys.getsizeof(self)+sys.getsizeof(self.__dict__)+sum(
            _size(key)+_size(value) for key,value in self.__dict__.items() if key not in {"entries", "manifest"})
        memory = dict(backend=f"{platform.python_implementation()} {platform.python_version()}",
            formula="auxiliary_bytes + peak_table_bytes + sum(key_state_bytes)",
            capacity_formula="auxiliary_bytes + empty_table_bytes + capacity * (table_slot_estimate_bytes + max_key_state_bytes)",
            auxiliary_bytes=auxiliary, empty_table_bytes=self.empty_table_bytes,
            peak_table_bytes=self.peak_table_bytes, table_slot_estimate_bytes=self.table_slot_estimate,
            max_key_state_bytes=self.max_entry_bytes, key_state_bytes=self.payload_bytes,
            peak_estimated_bytes=auxiliary+self.peak_table_bytes+self.payload_bytes,
            estimated_capacity_bytes=auxiliary+self.empty_table_bytes+self.capacity*(self.table_slot_estimate+self.max_entry_bytes),
            shared_objects_counted_conservatively=True,
            excluded=["shared run registration", "allocator arenas", "process RSS"])
        result = dict(schema="hng-g2-identity-index-v1", run=self.run, capacity=self.capacity,
                      peak_entries=self.peak_entries, join_overflow=sum(self.breakdown.values()),
                      breakdown=dict(self.breakdown), released=True, live_entries=self.live_entries, memory=memory,
                      cells=[dict(direction=key[0], line=key[1], phase=key[2], join_overflow=self.overflows[i])
                             for i,key in enumerate(self.cells)])
        return result
