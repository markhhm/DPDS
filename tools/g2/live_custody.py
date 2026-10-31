# SPDX-License-Identifier: Apache-2.0
"""Bounded scheduler and TX custody checks over active independent decisions."""
from collections import Counter

from .custody import MODEL_REASONS
from .reference import Trace
from .protocol import actor


def key(row):
    return row["ingress_port"], row["external_identity"]


class LiveCustody:
    def __init__(self, config, traces, manifest, active, issue):
        self.active, self.issue = active, issue
        self.lines = {row["name"]:row for row in config["lines"]}
        self.traces = {name:value if isinstance(value, Trace) else Trace(value) for name,value in traces.items()}
        self.ports = {row["egress_port"]:row["queue_id"] for row in manifest["ports"]}
        self.queues = {row["handoff_queue_id"]:dict(next=0, consumed=0, items={},
            capacity=row["capacity"], producer=None, consumer=None, egress=None)
            for row in manifest["capacity"]["handoff_queues"]}
        self.attempts, self.snapshots = {}, {}
        self.offers, self.last_offer = Counter(), {}
        self.counts = Counter()
        self.port_counts = Counter()
        self.input_next = {row["ingress_port"]:0 for row in manifest["ports"]}
        self.input_end = set()
        self.input_frontier = {port:None for port in self.input_next}

    def state(self, item):
        state = self.active.get(key(item))
        if state is None:
            self.issue("host_join_unavailable", "INCONCLUSIVE", item)
            return None
        row = state["expected"]
        if any(item[name] != row[name] for name in ("ingress_seq", "line")):
            self.issue("identity_mapping", "INVALID", row)
            return None
        return state

    def snapshot(self, event):
        seen, frontiers = event["seen_next_ingress_seq"], event["frontiers"]
        if set(seen) != {str(port) for port in self.input_next} or set(frontiers) != set(seen):
            self.issue("ingress_prefix_missing", "INCONCLUSIVE", event)
            return False
        valid = True
        for port, total in self.input_next.items():
            count, frontier = seen[str(port)], frontiers[str(port)]
            through, ended = frontier["through_ns"], frontier["input_complete"]
            old = self.snapshots.get((actor(event), port))
            if count > total or ended and (port not in self.input_end or count != total):
                self.issue("ingress_prefix", "INCONCLUSIVE", event)
                valid = False
            if old and (count < old[0] or old[2] and not ended or not ended and old[1] is not None and
                        (through is None or through < old[1])):
                self.issue("backward_frontier", "INCONCLUSIVE", event)
                valid = False
            # The join driver consumes ingress through the declared closed frontier.
            if through is not None and self.input_frontier[port] is not None and through > self.input_frontier[port] and port not in self.input_end:
                self.issue("frontier_unverified", "INCONCLUSIVE", event)
                valid = False
            for state in self.active.values():
                row = state["expected"]
                if row["ingress_port"]==port and row["ingress_seq"]>=count and through is not None and row["arrival_ns"]<=through:
                    self.issue("frontier_unconsumed_input", "INVALID", row)
                    valid = False
            self.snapshots[actor(event), port] = count, through, ended
        return valid

    def gate(self, line, at, row):
        trace = self.traces.get(line)
        if trace is None:
            return True
        try:
            return not trace.rows[trace.index(at)]["outage"]
        except ValueError:
            self.issue("trace_gate_coverage", "INCONCLUSIVE", row)
            return False

    def _head(self, event, at, pending, selected=()):
        heads = []
        for state in self.active.values():
            row = state["expected"]
            if (row["outcome"]!="accepted" or row["egress_port"]!=event["egress_port"]
                    or state["terminal"] is not None or state["tx"] or key(row) in selected
                    or row["release_ns"]>at or not state["offer"]):
                continue
            if pending:
                if not state["pending"] or not self.gate(row["line"], at, row):
                    continue
            elif state["dispatch"] is not None or row["ingress_seq"]>=event["seen_next_ingress_seq"].get(str(row["ingress_port"]), 0):
                continue
            heads.append(row)
        return min(heads, key=lambda row:(row["release_ns"], row["ingress_seq"])) if heads else None

    def consume(self, event, prefixes):
        if set(prefixes)!=set(self.queues):
            self.issue("consumed_dispatch_missing", "INCONCLUSIVE", event)
        for name, queue in self.queues.items():
            if name not in prefixes:
                continue
            end, start = prefixes[name], queue["consumed"]
            if end<start or end>queue["next"]:
                self.issue("consumed_dispatch_prefix", "INCONCLUSIVE", event)
                continue
            if queue["consumer"] not in {None, actor(event)}:
                self.issue("handoff_multiple_consumers", "INCONCLUSIVE", event)
            queue["consumer"] = actor(event)
            for ordinal in range(start, end):
                identity = queue["items"].pop(ordinal, None)
                state = self.active.get(identity)
                if state is None:
                    self.issue("missing_dispatch", "INCONCLUSIVE", event)
                else:
                    state["pending"] = True
            queue["consumed"] = end

    def event(self, event):
        kind = event["kind"]
        self.counts[kind] += 1
        if kind in {"offer", "terminal"}:
            state = self.state(event)
            if state is None:
                return
            row = state["expected"]
            if event["at_ns"]<row["arrival_ns"]:
                self.issue("host_time", "INVALID", row)
            if kind=="offer":
                if state["offer"]:
                    self.issue("duplicate_offer", "INVALID", row)
                if row["ingress_seq"]<=self.last_offer.get(row["ingress_port"], -1):
                    self.issue("offer_order", "FAIL", row)
                self.last_offer[row["ingress_port"]] = row["ingress_seq"]
                if event["actual_selection_seq"]!=self.offers[row["line"]]:
                    self.issue("actual_selection_sequence", "INVALID", row)
                self.offers[row["line"]] += 1
                if event["actual_selection_seq"]!=row["selection_seq"]:
                    self.issue("selection_divergence", "FAIL", row)
                state["offer"] = event
            else:
                if state["terminal"] is not None or state["tx"]:
                    self.issue("double_terminal", "INVALID", row)
                state["terminal"] = event
                self.counts[event["reason"]] += 1
                self.port_counts[row["ingress_port"], event["reason"]] += 1
                model = MODEL_REASONS.get(row["outcome"])
                if event["reason"]!=model:
                    self.issue("model_terminal_mismatch" if event["reason"] in MODEL_REASONS.values() else event["reason"], "FAIL", row)
            return
        valid = self.snapshot(event)
        if kind=="dispatch":
            queue = self.queues.get(event["handoff_queue_id"])
            if queue is None:
                self.issue("handoff_mapping", "INVALID", event)
                return
            if queue["producer"] not in {None, actor(event)}:
                self.issue("handoff_multiple_producers", "INCONCLUSIVE", event)
            queue["producer"] = actor(event)
            if event["first_handoff_seq"]!=queue["next"]:
                self.issue("dispatch_sequence", "INCONCLUSIVE", event)
                return
            for index, item in enumerate(event["identities"]):
                state = self.state(item)
                if state is None:
                    continue
                row = state["expected"]
                head = self._head(event, event["at_ns"], False)
                if state["dispatch"] is not None:
                    self.issue("duplicate_dispatch", "INVALID", row)
                if row["outcome"]!="accepted":
                    self.issue("model_drop_dispatched", "FAIL", row)
                elif row["release_ns"]>event["at_ns"]:
                    self.issue("dispatch_before_release", "FAIL", row)
                elif valid and head is not None and key(head)!=key(row):
                    self.issue("dispatch_wrong_head", "FAIL", row)
                if row["egress_port"]!=event["egress_port"]:
                    self.issue("port_mapping", "INVALID", row)
                if not state["offer"]:
                    self.issue("dispatch_before_offer", "INCONCLUSIVE", row)
                if not all(front["input_complete"] or front["through_ns"] is not None and front["through_ns"]>=row["release_ns"] for front in event["frontiers"].values()):
                    self.issue("unsafe_dispatch_frontier", "INCONCLUSIVE", row)
                ordinal = event["first_handoff_seq"]+index
                if len(queue["items"])>=queue["capacity"]:
                    self.issue("handoff_capacity", "INCONCLUSIVE", row)
                else:
                    queue["items"][ordinal] = key(row)
                state["dispatch"] = event["handoff_queue_id"], ordinal
            queue["next"] += len(event["identities"])
            return
        port = event["egress_port"]
        previous = self.attempts.get(port, (0, 0, actor(event)))
        if event["attempt_id"]!=previous[0]:
            self.issue("attempt_sequence", "INCONCLUSIVE", event)
        if event["gate_ns"]<previous[1]:
            self.issue("attempt_time", "INVALID", event)
        if actor(event)!=previous[2]:
            self.issue("multiple_tx_owners", "INCONCLUSIVE", event)
        self.attempts[port] = event["attempt_id"]+1, event["submit_after_ns"], actor(event)
        if self.ports.get(port)!=event["queue_id"]:
            self.issue("port_mapping", "INVALID", event)
        self.consume(event, event["consumed_dispatch"])
        selected = set()
        last_gate = event["gate_ns"]
        for index, item in enumerate(event["offered"]):
            if "consumed_dispatch" in item:
                self.consume(event, item["consumed_dispatch"])
            state = self.state(item)
            if state is None:
                continue
            row, at = state["expected"], item["gate_ns"]
            if at<last_gate:
                self.issue("attempt_time", "INVALID", row)
            last_gate = at
            if not state["pending"] or state["tx"] or state["terminal"] is not None or key(row) in selected:
                self.issue("tx_without_pending_custody", "INVALID", row)
            if state["dispatch"]!=(item["handoff_queue_id"], item["handoff_seq"]):
                self.issue("handoff_identity", "INVALID", row)
            if row["egress_port"]!=port:
                self.issue("port_mapping", "INVALID", row)
            if row["outcome"]!="accepted":
                self.issue("model_drop_transmitted", "FAIL", row)
            elif row["release_ns"]>at:
                self.issue("tx_before_release", "FAIL", row)
            if not self.gate(row["line"], at, row):
                self.issue("tx_gate_closed", "FAIL", row)
            head = self._head(event, at, True, selected)
            if head is not None and key(head)!=key(row):
                self.issue("tx_wrong_head", "FAIL", row)
            selected.add(key(row))
            if index<event["accepted_count"]:
                state["tx"] = event["submit_after_ns"]
                state["pending"] = False
                self.counts["tx_accepted"] += 1
                self.port_counts[row["ingress_port"], "tx_accepted"] += 1
