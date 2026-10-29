# SPDX-License-Identifier: Apache-2.0
"""Independent, disk-backed replay of host offers, scheduler and TX custody.

Inputs are validated DATA records. Explicit ingress and handoff prefixes, rather
than a timestamp merge, establish scheduler and TX availability.
"""
from collections import Counter
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import tempfile

from .reference import Trace, canonical, uint


MODEL_REASONS = {"q_drop": "q_drop", "trace_loss": "trace_loss_drop",
                 "trace_outage": "trace_outage_drop", "invalid_input": "invalid_input_drop"}
TERMINAL_REASONS = set(MODEL_REASONS.values()) | {
    "delay_storage_drop", "handoff_drop", "terminal_tx_drop",
    "missed_deadline_outage_drop", "shutdown_drop"}


def _number(value):
    """Lexically sortable uint64, including values beyond SQLite's signed range."""
    return f"{uint(value, 'host ordinal/time'):020d}"


class HostAudit:
    def __init__(self, database, max_diagnostics):
        self.database = Path(database)
        self.issues = []
        self.counts = Counter()
        self.status_counts = Counter()
        self.max_diagnostics = max_diagnostics

    @property
    def measurement_status(self):
        return next((status for status in ("INVALID", "INCONCLUSIVE", "FAIL")
                     if self.status_counts[status]), "PASS")

    def issue(self, code, status, row=None):
        self.counts[code] += 1
        self.status_counts[status] += 1
        if len(self.issues) < self.max_diagnostics:
            item = dict(code=code, measurement_status=status)
            if row:
                item.update((key, row[key]) for key in
                            ("line", "ingress_port", "external_identity") if row.get(key) is not None)
            self.issues.append(item)

    def terminal_reason(self, port, identity):
        with closing(sqlite3.connect(self.database)) as db, db:
            row = db.execute("SELECT reason FROM terminals WHERE ingress_port=? AND external_identity=?",
                             (port, identity)).fetchone()
        return row[0] if row else None

    def was_accepted(self, port, identity):
        with closing(sqlite3.connect(self.database)) as db, db:
            return db.execute("SELECT 1 FROM accepted WHERE ingress_port=? AND external_identity=?",
                              (port, identity)).fetchone() is not None


class _Replay:
    def __init__(self, db, result, config, traces, manifest):
        self.db, self.result = db, result
        self.lines = {row["name"]: row for row in config["lines"]}
        self.traces = {name: profile if isinstance(profile, Trace) else Trace(profile)
                       for name, profile in traces.items()}
        self.ports = {}
        for row in manifest["ports"]:
            port, queue = row["egress_port"], row["queue_id"]
            if port in self.ports and self.ports[port] != queue:
                result.issue("port_mapping", "INVALID")
            self.ports[port] = queue
        for line in self.lines.values():
            if not any(row["ingress_port"] == line["ingress_port"] and
                       row["egress_port"] == line["egress_port"] for row in manifest["ports"]):
                result.issue("port_mapping", "INVALID", line)
        self.inputs = {row["ingress_port"] for row in self.lines.values()}
        self.queues = {}
        for row in manifest["capacity"]["handoff_queues"]:
            name = row["handoff_queue_id"]
            if name in self.queues or not row["producer"] or not row["consumer"]:
                result.issue("handoff_mapping", "INVALID")
            self.queues[name] = dict(config=row, next=0, consumed=0, producer=None,
                                     consumer=None, egress=None)
        self.attempts = {}
        self.snapshots = {}
        self.live = 0
        self.limit = manifest["capacity"]["max_unresolved"]
        self.db.executescript("""
            CREATE TABLE expected (ingress_port INTEGER, external_identity TEXT,
                ingress_seq TEXT, line TEXT, selection_seq TEXT, arrival_ns TEXT,
                egress_port INTEGER, outcome TEXT, release_ns TEXT, body TEXT,
                PRIMARY KEY(ingress_port,external_identity), UNIQUE(ingress_port,ingress_seq));
            CREATE INDEX expected_line ON expected(line,release_ns,ingress_seq);
            CREATE TABLE events (event_id INTEGER PRIMARY KEY, stream_id TEXT, seq TEXT,
                at_ns TEXT, kind TEXT, body TEXT, done INTEGER DEFAULT 0,
                UNIQUE(stream_id,seq));
            CREATE INDEX event_clock ON events(at_ns,stream_id,seq);
            CREATE TABLE offers (ingress_port INTEGER, external_identity TEXT,
                actual_selection_seq TEXT, at_ns TEXT, stream_id TEXT, seq TEXT,
                line TEXT, ingress_seq TEXT, PRIMARY KEY(ingress_port,external_identity));
            CREATE INDEX offer_line ON offers(line,actual_selection_seq);
            CREATE INDEX offer_clock ON offers(at_ns,stream_id,seq);
            CREATE TABLE terminals (ingress_port INTEGER, external_identity TEXT,
                reason TEXT, at_ns TEXT, stream_id TEXT, seq TEXT,
                PRIMARY KEY(ingress_port,external_identity));
            CREATE TABLE accepted (ingress_port INTEGER, external_identity TEXT,
                egress_port INTEGER, attempt_id TEXT, accepted_index INTEGER,
                at_ns TEXT, PRIMARY KEY(ingress_port,external_identity));
            CREATE TABLE dispatch_items (handoff_queue_id TEXT, handoff_seq TEXT,
                ingress_port INTEGER, external_identity TEXT, egress_port INTEGER,
                state TEXT, selected INTEGER, at_ns TEXT, stream_id TEXT, event_seq TEXT,
                PRIMARY KEY(handoff_queue_id,handoff_seq),
                UNIQUE(ingress_port,external_identity));
            CREATE INDEX dispatch_state ON dispatch_items(state,ingress_port,external_identity);
        """)

    def issue(self, code, status, row=None):
        self.result.issue(code, status, row)

    def expected(self, row):
        found = self.db.execute("SELECT * FROM expected WHERE ingress_port=? AND external_identity=?",
                                (row["ingress_port"], row["external_identity"])).fetchone()
        if found is None:
            self.issue("unknown_identity", "INVALID", row)
        elif (found["line"] != row["line"] or
              int(found["ingress_seq"]) != row["ingress_seq"]):
            self.issue("identity_mapping", "INVALID", row)
            return None
        return found

    def load(self, expected_rows, host_rows):
        for row in expected_rows:
            if row["kind"] != "expected":
                continue
            try:
                self.db.execute("INSERT INTO expected VALUES(?,?,?,?,?,?,?,?,?,?)", (
                    row["ingress_port"], row["external_identity"], _number(row["ingress_seq"]),
                    row["line"], _number(row["selection_seq"]) if row["selection_seq"] is not None else None,
                    _number(row["arrival_ns"]), row["egress_port"], row["outcome"],
                    _number(row["release_ns"]) if row["release_ns"] is not None else None,
                    canonical(row).decode()))
            except sqlite3.IntegrityError:
                self.issue("duplicate_input", "INVALID", row)
        self.totals = {port: self.db.execute("SELECT COUNT(*) FROM expected WHERE ingress_port=?",
                                           (port,)).fetchone()[0] for port in self.inputs}
        for row in host_rows:
            kind = row["kind"]
            if kind not in {"offer", "terminal", "dispatch", "attempt"}:
                self.issue("host_event_kind", "INVALID", row)
                continue
            at = row["gate_ns"] if kind == "attempt" else row["at_ns"]
            try:
                self.db.execute("INSERT INTO events(stream_id,seq,at_ns,kind,body) VALUES(?,?,?,?,?)",
                                (row["stream_id"], _number(row["seq"]), _number(at), kind,
                                 canonical(row).decode()))
            except sqlite3.IntegrityError:
                self.issue("host_sequence", "INVALID", row)
                continue
            if kind not in {"offer", "terminal"}:
                continue
            expected = self.expected(row)
            if expected is None:
                continue
            if _number(at) < expected["arrival_ns"]:
                self.issue("host_time", "INVALID", row)
            try:
                if kind == "offer":
                    self.db.execute("INSERT INTO offers VALUES(?,?,?,?,?,?,?,?)", (
                        row["ingress_port"], row["external_identity"], str(row["actual_selection_seq"]),
                        _number(at), row["stream_id"], _number(row["seq"]), row["line"],
                        _number(row["ingress_seq"])))
                else:
                    if row["reason"] not in TERMINAL_REASONS:
                        self.issue("terminal_reason", "INVALID", row)
                    self.db.execute("INSERT INTO terminals VALUES(?,?,?,?,?,?)", (
                        row["ingress_port"], row["external_identity"], row["reason"],
                        _number(at), row["stream_id"], _number(row["seq"])))
            except sqlite3.IntegrityError:
                self.issue("duplicate_offer" if kind == "offer" else "double_terminal", "INVALID", row)
        self.db.commit()
        # Per-producer order is authoritative, even when producer files were concatenated.
        for stream, in self.db.execute("SELECT DISTINCT stream_id FROM events"):
            previous = None
            for event in self.db.execute("SELECT * FROM events WHERE stream_id=? ORDER BY seq", (stream,)):
                if previous is not None and event["at_ns"] < previous:
                    self.issue("host_time", "INVALID", json.loads(event["body"]))
                previous = event["at_ns"]
        self.check_offers()

    def check_offers(self):
        previous = {}
        actual = Counter()
        for offer in self.db.execute("SELECT * FROM offers ORDER BY at_ns,stream_id,seq"):
            row = dict(offer)
            port, line = row["ingress_port"], row["line"]
            if port in previous and row["ingress_seq"] <= previous[port]:
                self.issue("offer_order", "FAIL", row)
            previous[port] = row["ingress_seq"]
            if int(row["actual_selection_seq"]) != actual[line]:
                self.issue("actual_selection_sequence", "INVALID", row)
            actual[line] += 1
            expected = self.db.execute("SELECT selection_seq FROM expected WHERE ingress_port=? AND external_identity=?",
                                       (port, row["external_identity"])).fetchone()
            if expected[0] is None:
                self.issue("invalid_input_offered", "FAIL", row)
            elif int(row["actual_selection_seq"]) != int(expected[0]):
                self.issue("selection_divergence", "FAIL", row)

    def before(self, other, event):
        """None means two distinct producers recorded an ambiguous clock tie."""
        if other["stream_id"] == event["stream_id"]:
            return other["seq"] < _number(event["seq"])
        at = event.get("at_ns", event.get("gate_ns"))
        if other["at_ns"] == _number(at):
            return None
        return other["at_ns"] < _number(at)

    def snapshot(self, event):
        seen, frontiers = event["seen_next_ingress_seq"], event["frontiers"]
        if set(seen) != {str(p) for p in self.inputs} or set(frontiers) != set(seen):
            self.issue("ingress_prefix_missing", "INCONCLUSIVE", event)
            return False
        valid = True
        for port in self.inputs:
            key, count = str(port), seen[str(port)]
            frontier = frontiers[key]
            previous = self.snapshots.get((event["stream_id"], port))
            if count > self.totals[port] or (previous and count < previous[0]):
                self.issue("ingress_prefix", "INVALID", event)
                valid = False
            through = frontier["through_ns"]
            if frontier["input_complete"] and count != self.totals[port]:
                self.issue("ingress_prefix", "INVALID", event)
                valid = False
            if previous and (previous[2] and not frontier["input_complete"] or
                             previous[1] is not None and
                             (through is None or through < previous[1])):
                self.issue("backward_frontier", "INVALID", event)
                valid = False
            if through is not None:
                unseen = self.db.execute("SELECT 1 FROM expected WHERE ingress_port=? AND ingress_seq>=? AND arrival_ns<=? LIMIT 1",
                                         (port, _number(count), _number(through))).fetchone()
                if unseen:
                    self.issue("frontier_unconsumed_input", "INVALID", event)
                    valid = False
            self.snapshots[event["stream_id"], port] = (count, through, frontier["input_complete"])
        return valid

    def safe(self, row, event):
        relevant = {line["ingress_port"] for line in self.lines.values()
                    if line["egress_port"] == row["egress_port"]}
        return all(event["frontiers"][str(port)]["input_complete"] or
                   event["frontiers"][str(port)]["through_ns"] is not None and
                   event["frontiers"][str(port)]["through_ns"] >= int(row["release_ns"])
                   for port in relevant)

    def scheduler_head(self, event, selected):
        heads = []  # At most the four configured lines, never the packet history.
        for name, line in self.lines.items():
            if line["egress_port"] != event["egress_port"]:
                continue
            for row in self.db.execute("""SELECT e.* FROM expected e
                JOIN offers o USING(ingress_port,external_identity)
                LEFT JOIN dispatch_items d USING(ingress_port,external_identity)
                WHERE e.line=? AND e.outcome='accepted' AND e.ingress_seq<?
                AND e.release_ns<=? AND d.external_identity IS NULL
                ORDER BY e.release_ns,e.ingress_seq""", (
                    name, _number(event["seen_next_ingress_seq"][str(line["ingress_port"])]),
                    _number(event["at_ns"]))):
                key = row["ingress_port"], row["external_identity"]
                terminal = self.db.execute("SELECT * FROM terminals WHERE ingress_port=? AND external_identity=?", key).fetchone()
                if terminal:
                    before = self.before(terminal, event)
                    if before is None:
                        self.issue("ambiguous_clock_order", "INCONCLUSIVE", dict(row))
                    if before is not False:
                        continue
                offer = self.db.execute("SELECT * FROM offers WHERE ingress_port=? AND external_identity=?", key).fetchone()
                before = self.before(offer, event)
                if before is None and key != selected:
                    self.issue("ambiguous_clock_order", "INCONCLUSIVE", dict(row))
                    continue
                if before is False:
                    continue
                heads.append(row)
                break
        return min(heads, key=lambda row: (row["release_ns"], row["ingress_seq"])) if heads else None

    def dispatch(self, event):
        queue = self.queues.get(event["handoff_queue_id"])
        if queue is None:
            self.issue("handoff_mapping", "INVALID", event)
            return
        if queue["producer"] not in {None, event["stream_id"]}:
            self.issue("handoff_multiple_producers", "INCONCLUSIVE", event)
        queue["producer"] = event["stream_id"]
        if queue["egress"] not in {None, event["egress_port"]}:
            self.issue("port_mapping", "INVALID", event)
        queue["egress"] = event["egress_port"]
        if event["first_handoff_seq"] != queue["next"]:
            self.issue("dispatch_sequence", "INCONCLUSIVE", event)
        valid_snapshot = self.snapshot(event)
        for index, item in enumerate(event["identities"]):
            row = self.expected(item)
            if row is None:
                continue
            key = item["ingress_port"], item["external_identity"]
            if row["egress_port"] != event["egress_port"]:
                self.issue("port_mapping", "INVALID", item)
            if row["outcome"] != "accepted":
                self.issue("model_drop_dispatched", "FAIL", item)
            elif row["release_ns"] > _number(event["at_ns"]):
                self.issue("dispatch_before_release", "FAIL", item)
            if valid_snapshot and row["outcome"] == "accepted":
                if item["ingress_seq"] >= event["seen_next_ingress_seq"][str(item["ingress_port"])]:
                    self.issue("dispatch_unconsumed_input", "INVALID", item)
                head = self.scheduler_head(event, key)
                if not self.safe(row, event):
                    self.issue("unsafe_dispatch_frontier", "INCONCLUSIVE", item)
                elif head and (head["ingress_port"], head["external_identity"]) != key:
                    self.issue("dispatch_wrong_head", "FAIL", item)
            offer = self.db.execute("SELECT * FROM offers WHERE ingress_port=? AND external_identity=?", key).fetchone()
            if offer is not None and self.before(offer, event) is False:
                self.issue("dispatch_before_offer", "INVALID", item)
            try:
                self.db.execute("INSERT INTO dispatch_items VALUES(?,?,?,?,?,'dispatched',NULL,?,?,?)", (
                    event["handoff_queue_id"], _number(event["first_handoff_seq"] + index),
                    *key, event["egress_port"], _number(event["at_ns"]), event["stream_id"],
                    _number(event["seq"])))
            except sqlite3.IntegrityError:
                self.issue("duplicate_dispatch", "INVALID", item)
        queue["next"] = max(queue["next"], event["first_handoff_seq"] + len(event["identities"]))
        waiting = self.db.execute("SELECT COUNT(*) FROM dispatch_items WHERE handoff_queue_id=? AND state='dispatched'",
                                  (event["handoff_queue_id"],)).fetchone()[0]
        if waiting > queue["config"]["capacity"]:
            self.issue("handoff_capacity", "INCONCLUSIVE", event)

    def consume(self, event, prefixes):
        if set(prefixes) != set(self.queues):
            self.issue("consumed_dispatch_missing", "INCONCLUSIVE", event)
        for name, queue in self.queues.items():
            if name not in prefixes:
                continue
            end, start = prefixes[name], queue["consumed"]
            if end < start:
                self.issue("consumed_dispatch_regression", "INCONCLUSIVE", event)
                continue
            if end == start:
                continue
            if queue["consumer"] not in {None, event["stream_id"]}:
                self.issue("handoff_multiple_consumers", "INCONCLUSIVE", event)
            queue["consumer"] = event["stream_id"]
            if queue["egress"] is not None and queue["egress"] != event["egress_port"]:
                self.issue("port_mapping", "INVALID", event)
                continue
            found = self.db.execute("SELECT COUNT(*) FROM dispatch_items WHERE handoff_queue_id=? AND handoff_seq>=? AND handoff_seq<?",
                                    (name, _number(start), _number(end))).fetchone()[0]
            if end > queue["next"] or found != end - start:
                self.issue("missing_dispatch", "INCONCLUSIVE", event)
                continue
            self.db.execute("UPDATE dispatch_items SET state='pending' WHERE handoff_queue_id=? AND handoff_seq>=? AND handoff_seq<? AND state='dispatched'",
                            (name, _number(start), _number(end)))
            queue["consumed"] = end

    def gate_open(self, line, at, row):
        trace = self.traces.get(line)
        if trace is None:
            return True
        try:
            return not trace.rows[trace.index(at)]["outage"]
        except ValueError:
            self.issue("trace_gate_coverage", "INCONCLUSIVE", row)
            return None

    def pending_head(self, event, at):
        heads = []
        for name, line in self.lines.items():
            if line["egress_port"] != event["egress_port"] or not self.gate_open(name, at, event):
                continue
            row = self.db.execute("""SELECT e.* FROM expected e
                JOIN dispatch_items d USING(ingress_port,external_identity)
                WHERE e.line=? AND d.state='pending' AND d.selected IS NULL
                AND e.release_ns<=? ORDER BY e.release_ns,e.ingress_seq LIMIT 1""",
                                  (name, _number(at))).fetchone()
            if row:
                heads.append(row)
        return min(heads, key=lambda row: (row["release_ns"], row["ingress_seq"])) if heads else None

    def attempt(self, event, event_id):
        port = event["egress_port"]
        previous = self.attempts.get(port, (0, 0, None))
        if event["attempt_id"] != previous[0]:
            self.issue("attempt_sequence", "INVALID", event)
        if (not event["gate_ns"] <= event["submit_before_ns"] <= event["submit_after_ns"] or
                event["gate_ns"] < previous[1]):
            self.issue("attempt_time", "INVALID", event)
        if port not in self.ports or event["queue_id"] != self.ports[port]:
            self.issue("port_mapping", "INVALID", event)
        if previous[2] not in {None, event["stream_id"]}:
            self.issue("multiple_tx_owners", "INCONCLUSIVE", event)
        self.attempts[port] = (event["attempt_id"] + 1, event["submit_after_ns"], event["stream_id"])
        self.snapshot(event)
        self.consume(event, event["consumed_dispatch"])
        if not 0 <= event["accepted_count"] <= len(event["offered"]):
            self.issue("accepted_prefix", "INVALID", event)
            return
        last_gate = event["gate_ns"]
        for index, item in enumerate(event["offered"]):
            at = item["gate_ns"]
            if not last_gate <= at <= event["submit_before_ns"]:
                self.issue("attempt_time", "INVALID", item)
            last_gate = at
            if "consumed_dispatch" in item:
                self.consume(event, item["consumed_dispatch"])
            row = self.expected(item)
            if row is None:
                continue
            key = item["ingress_port"], item["external_identity"]
            dispatch = self.db.execute("SELECT * FROM dispatch_items WHERE handoff_queue_id=? AND handoff_seq=?",
                                       (item["handoff_queue_id"], _number(item["handoff_seq"]))).fetchone()
            usable = dispatch is not None
            if dispatch is None:
                self.issue("missing_dispatch", "INCONCLUSIVE", item)
            elif (dispatch["ingress_port"], dispatch["external_identity"]) != key:
                self.issue("handoff_identity", "INVALID", item)
                usable = False
            elif dispatch["state"] != "pending" or dispatch["selected"] is not None:
                self.issue("tx_without_pending_custody", "INVALID", item)
                usable = False
            if row["egress_port"] != port:
                self.issue("port_mapping", "INVALID", item)
            if row["outcome"] != "accepted":
                self.issue("model_drop_transmitted", "FAIL", item)
            elif row["release_ns"] > _number(at):
                self.issue("tx_before_release", "FAIL", item)
            gate = self.gate_open(item["line"], at, item)
            if gate is False:
                self.issue("tx_gate_closed", "FAIL", item)
            head = self.pending_head(event, at)
            if usable and gate and head and (head["ingress_port"], head["external_identity"]) != key:
                self.issue("tx_wrong_head", "FAIL", item)
            if usable:
                self.db.execute("UPDATE dispatch_items SET selected=? WHERE ingress_port=? AND external_identity=?",
                                (event_id, *key))
            if index < event["accepted_count"]:
                terminal = self.db.execute("SELECT 1 FROM terminals WHERE ingress_port=? AND external_identity=?", key).fetchone()
                if terminal:
                    self.issue("double_terminal", "INVALID", item)
                try:
                    self.db.execute("INSERT INTO accepted VALUES(?,?,?,?,?,?)", (
                        *key, port, str(event["attempt_id"]), index, _number(event["submit_after_ns"])))
                    self.live -= 1
                except sqlite3.IntegrityError:
                    self.issue("double_terminal", "INVALID", item)
                if usable:
                    self.db.execute("UPDATE dispatch_items SET state='accepted' WHERE ingress_port=? AND external_identity=?", key)
        self.db.execute("UPDATE dispatch_items SET selected=NULL WHERE selected=?", (event_id,))

    def replay(self):
        # Timestamp order is used only to schedule replay. Explicit prefixes above
        # determine availability; same-clock dependency ties are retried below.
        remaining = self.db.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        while remaining:
            progressed = 0
            for event in self.db.execute("SELECT * FROM events WHERE done=0 ORDER BY at_ns,stream_id,seq"):
                row = json.loads(event["body"])
                if row["kind"] == "attempt":
                    # A recorded enqueue in another file at the same clock value
                    # may precede this consume; its ordinal supplies the ordering.
                    blocked = self.db.execute("SELECT 1 FROM events WHERE done=0 AND kind='dispatch' AND at_ns<=? LIMIT 1",
                                              (event["at_ns"],)).fetchone()
                    if blocked:
                        continue
                    self.attempt(row, event["event_id"])
                elif row["kind"] == "dispatch":
                    self.dispatch(row)
                elif row["kind"] == "offer":
                    self.live += 1
                    if self.live > self.limit:
                        self.issue("unresolved_capacity", "INCONCLUSIVE", row)
                elif row["kind"] == "terminal":
                    key = row["ingress_port"], row["external_identity"]
                    offer = self.db.execute("SELECT * FROM offers WHERE ingress_port=? AND external_identity=?", key).fetchone()
                    if offer is not None and self.before(offer, row):
                        self.live -= 1
                    self.db.execute("UPDATE dispatch_items SET state='terminal' WHERE ingress_port=? AND external_identity=? AND state!='accepted'", key)
                self.db.execute("UPDATE events SET done=1 WHERE event_id=?", (event["event_id"],))
                progressed += 1
            if not progressed:
                self.issue("host_order_unresolved", "INCONCLUSIVE")
                break
            remaining -= progressed
        self.finish()
        self.db.commit()

    def finish(self):
        for expected in self.db.execute("""SELECT e.*, o.external_identity AS offered,
            t.reason, a.external_identity AS accepted FROM expected e
            LEFT JOIN offers o USING(ingress_port,external_identity)
            LEFT JOIN terminals t USING(ingress_port,external_identity)
            LEFT JOIN accepted a USING(ingress_port,external_identity)"""):
            row = dict(expected)
            reason = row["reason"]
            model_reason = MODEL_REASONS.get(row["outcome"])
            if reason and reason != model_reason and (model_reason is None or reason not in MODEL_REASONS.values()):
                self.issue("non_model_drop" if reason not in MODEL_REASONS.values()
                           or row["outcome"] == "accepted" else "model_terminal_mismatch", "FAIL", row)
            if model_reason and reason != model_reason:
                if reason or row["accepted"]:
                    self.issue("model_terminal_mismatch", "FAIL", row)
                else:
                    self.issue("missing_terminal", "INCONCLUSIVE", row)
            if row["offered"] is None and row["outcome"] != "invalid_input":
                if reason and reason not in {"q_drop", "trace_loss_drop", "trace_outage_drop"}:
                    if reason == model_reason:
                        self.issue("non_model_drop", "FAIL", row)
                else:
                    self.issue("missing_offer", "INCONCLUSIVE", row)
            if row["outcome"] == "accepted" and not reason and row["accepted"] is None:
                self.issue("missing_tx_evidence", "INCONCLUSIVE", row)


def audit_host(config, traces, manifest, expected_rows, host_rows, work_dir):
    if manifest.get("scope")!="software-only" or manifest.get("retention",{}).get("mode")!="full":
        raise ValueError("history replay requires explicit full retention for a bounded software fixture")
    """Audit complete validated host records against independent expected rows.

    SQLite owns all packet/event history. Only configuration, per-producer heads,
    counters and capped diagnostics occupy Python memory. The returned database
    remains in work_dir for joins by the caller.
    """
    capacity = manifest["capacity"]
    directory = Path(work_dir)
    directory.mkdir(parents=True, exist_ok=True)
    handle, path = tempfile.mkstemp(prefix="custody-", suffix=".sqlite", dir=directory)
    os.close(handle)
    result = HostAudit(path, uint(capacity["max_diagnostics"], "max diagnostics"))
    with closing(sqlite3.connect(path)) as db, db:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=OFF")
        db.execute("PRAGMA synchronous=OFF")
        db.execute("PRAGMA cache_size=-1024")
        db.execute("PRAGMA temp_store=FILE")
        page_size = db.execute("PRAGMA page_size").fetchone()[0]
        pages = uint(capacity["join_disk_bytes"], "join disk bytes") // page_size
        if not pages:
            result.issue("join_disk_capacity", "INCONCLUSIVE")
            return result
        db.execute(f"PRAGMA max_page_count={pages}")
        try:
            replay = _Replay(db, result, config, traces, manifest)
            replay.load(expected_rows, host_rows)
            replay.replay()
        except sqlite3.DatabaseError as exc:
            if "full" in str(exc).lower():
                result.issue("join_disk_capacity", "INCONCLUSIVE")
            else:
                result.issue("host_database_error", "INCONCLUSIVE")
        except (KeyError, TypeError, ValueError, OverflowError):
            result.issue("host_schema", "INVALID")
    return result
