# SPDX-License-Identifier: Apache-2.0
"""Independent exact link diagnostics; no serializer value changes a G2 gate."""
from fractions import Fraction
import heapq

from .online import Histogram


def _ceil(value):
    return -(-value.numerator//value.denominator)


def _merge(target, source):
    target.n += source.n
    target.nonzero += source.nonzero
    target.bins.update(source.bins)
    target.underflow += source.underflow
    target.overflow += source.overflow
    if source.minimum is not None:
        target.minimum = source.minimum if target.minimum is None else min(target.minimum, source.minimum)
        target.maximum = source.maximum if target.maximum is None else max(target.maximum, source.maximum)


def _period_cell():
    return dict(packets=0, waiting_packets=0, waiting_wire_bytes=0, wait=Histogram(), residual=Histogram())


class Serialization:
    def __init__(self, manifest):
        self.manifest = manifest
        self.limit = manifest["capacity"]["max_unresolved"]
        self.pending, self.ordinal = 0, 0
        self.ports = {row["egress_port"]:dict(ingress=row["ingress_port"], rate=row["link_bps"],
            heap=[], tail=Fraction(0), period=None, actual_tail=Fraction(0)) for row in manifest["ports"]}
        self.cells = {(str(cell["direction"]),cell["line"],cell["phase"]):dict(
            modeled=Histogram(), residual=Histogram(), actual=Histogram(), late=_period_cell())
            for cell in manifest["required_cells"]}

    def _cell(self, row):
        phases = [window["phase"] for window in self.manifest["windows"]
                  if window["start_ns"]<=row["arrival_ns"]<window["end_ns"]]
        if len(phases)!=1:
            raise ValueError("serializer ingress phase is ambiguous")
        return str(row["ingress_port"]), row["line"], phases[0]

    def enqueue(self, row):
        if self.pending>=self.limit:
            raise ValueError("serializer capacity exhausted")
        handle = dict(cell=self._cell(row), release=row["release_ns"], wire_bytes=row["wire_bytes"],
                      late=row["late"], wait=None, period=None, observed=False, error=None)
        port = self.ports[row["egress_port"]]
        heapq.heappush(port["heap"], (row["release_ns"], row["ingress_seq"], self.ordinal, handle))
        self.ordinal += 1
        self.pending += 1
        return handle

    def _complete(self, period):
        if not period["closed"] or period["pending"] or period["merged"]:
            return
        period["merged"] = True
        if period["late"]:
            for key, source in period["cells"].items():
                target = self.cells[key]["late"]
                for name in ("packets", "waiting_packets", "waiting_wire_bytes"):
                    target[name] += source[name]
                _merge(target["wait"], source["wait"])
                _merge(target["residual"], source["residual"])

    def _residual(self, handle):
        if handle["error"] is not None:
            value = handle["error"]-handle["wait"]
            self.cells[handle["cell"]]["residual"].add(value)
            handle["period"]["cells"][handle["cell"]]["residual"].add(value)

    def observe(self, handle, signed_error):
        if handle is None or handle["observed"]:
            return
        handle.update(observed=True, error=signed_error)
        period = handle["period"]
        if period is not None:
            period["pending"] -= 1
            self._residual(handle)
            self._complete(period)

    def advance(self, ingress_port, through_ns):
        for port in self.ports.values():
            if port["ingress"]!=ingress_port:
                continue
            while port["heap"] and port["heap"][0][0]<=through_ns:
                _, _, _, handle = heapq.heappop(port["heap"])
                self.pending -= 1
                release = handle["release"]
                if port["period"] is None or release>=port["tail"]:
                    if port["period"] is not None:
                        port["period"]["closed"] = True
                        self._complete(port["period"])
                    port["period"] = dict(late=False, closed=False, pending=0, merged=False, cells={})
                period = port["period"]
                period["late"] |= handle["late"]
                wait = _ceil(max(Fraction(release),port["tail"])-release)
                handle.update(wait=wait, period=period)
                target = period["cells"].setdefault(handle["cell"], _period_cell())
                target["packets"] += 1
                target["waiting_packets"] += wait>0
                target["waiting_wire_bytes"] += handle["wire_bytes"] if wait else 0
                target["wait"].add(wait)
                self.cells[handle["cell"]]["modeled"].add(wait)
                port["tail"] = max(Fraction(release),port["tail"])+Fraction(handle["wire_bytes"]*8*10**9,port["rate"])
                if handle["observed"]:
                    self._residual(handle)
                else:
                    period["pending"] += 1

    def attempt(self, row, gate_ns):
        port = self.ports[row["egress_port"]]
        start = max(Fraction(gate_ns), port["actual_tail"])
        self.cells[self._cell(row)]["actual"].add(_ceil(start-gate_ns))
        port["actual_tail"] = start+Fraction(row["wire_bytes"]*8*10**9,port["rate"])

    def finish(self):
        for port in self.ports.values():
            self.advance(port["ingress"], 2**64-1)
            if port["period"] is not None:
                port["period"]["closed"] = True
                self._complete(port["period"])
        results = {}
        for key, state in self.cells.items():
            port = next(port for port in self.ports.values() if str(port["ingress"])==key[0])
            late = state["late"]
            results[key] = dict(status="diagnostic", gate_uses_original_expected_release=True,
                timestamp_convention="egress first bit", service_arithmetic="exact rational; histogram waits rounded up to ns",
                modeled=dict(origin="independent modeled eligible release order", wait=state["modeled"].snapshot(),
                             signed_residual=state["residual"].snapshot()),
                late_involved_busy_period={**{name:late[name] for name in ("packets","waiting_packets","waiting_wire_bytes")},
                    "wait":late["wait"].snapshot(), "signed_residual":late["residual"].snapshot()},
                actual_attempt=dict(origin="verified actual attempt gate_ns", wait=state["actual"].snapshot(),
                                    interpretation="separate actual-order serializer; never removed from gate errors"),
                egress_tail_ns=dict(numerator=port["tail"].numerator,denominator=port["tail"].denominator))
        return results
