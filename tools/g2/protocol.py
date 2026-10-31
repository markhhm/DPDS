# SPDX-License-Identifier: Apache-2.0
"""Strict, streaming hng-observer-v1 interchange. No DUT or network imports."""
import hashlib
import argparse
import json
from pathlib import Path
import re
import sys

from .reference import canonical, fields, strict_json, uint, U64


class StreamError(ValueError):
    def __init__(self, code, message, measurement_status="INVALID"):
        super().__init__(message)
        self.code = code
        self.measurement_status = measurement_status


def identifier(value, name="identifier"):
    if not isinstance(value, str) or not re.fullmatch(r"[\x20-\x7e]{1,128}", value):
        raise ValueError(f"{name} must be 1..128 printable ASCII characters")
    return value


def signed(value, name):
    if type(value) is not int or not -(1 << 63) <= value < (1 << 63):
        raise ValueError(f"{name} must be int64")
    return value


def boolean(value, name):
    if type(value) is not bool:
        raise ValueError(f"{name} must be boolean")
    return value


def digest(value, name="sha256"):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise ValueError(f"{name} must be lowercase SHA-256")
    return value


def shape(value, required, optional=()):
    fields(value, required, tuple(optional) + ("extensions",))
    if "extensions" in value and not isinstance(value["extensions"], dict):
        raise ValueError("extensions must be an object")


def nullable(value, check, name):
    if value is not None:
        check(value, name)


def timestamp_fields(value):
    shape(value, ("raw", "clock_id", "mapping_id", "point", "uncertainty_ns"))
    uint(value["raw"], "raw timestamp")
    uint(value["uncertainty_ns"], "timestamp uncertainty")
    identifier(value["clock_id"], "clock_id")
    identifier(value["mapping_id"], "mapping_id")
    if value["point"] not in {"external_rx", "tg_rx", "software_rx", "tx_submit", "fake", "tg_tx_port"}:
        raise ValueError("unsupported timestamp point")


ENVELOPE = ("kind", "run", "stream_id", "seq")
PACKET_FIELDS = ("ingress_port", "ingress_seq", "line", "selection_seq", "external_identity",
                 "flow_id", "arrival_ns", "length", "vlan_id", "segments", "length_flags",
                 "app", "selected_late", "input_error")
IDENTITY_FIELDS = ("ingress_port", "egress_port", "line", "external_identity", "flow_id",
                   "timestamp", "selected_late", "length", "vlan_id", "app")
ROLES = {"ingress": {"packet", "frontier", "end"}, "receiver": {"receive"},
         "before": {"before"}, "host": {"offer", "terminal", "attempt", "dispatch"},
         "counters": {"counter"}, "expected": {"expected", "summary"}}
TERMINALS = {"q_drop", "trace_loss_drop", "trace_outage_drop", "delay_storage_drop",
             "invalid_input_drop", "handoff_drop", "terminal_tx_drop",
             "missed_deadline_outage_drop", "shutdown_drop"}


def _port_map(value, frontier=False):
    if not isinstance(value, dict):
        raise ValueError("port prefix/frontier must be an object")
    for key, item in value.items():
        if not isinstance(key, str) or not re.fullmatch(r"0|[1-9][0-9]*", key) or int(key) > 65535:
            raise ValueError("port prefix keys must be canonical uint16 strings")
        if frontier:
            shape(item, ("through_ns", "input_complete"))
            nullable(item["through_ns"], uint, "frontier")
            if item["through_ns"] == U64:
                raise ValueError("closed frontier cannot be converted to uint64 lower bound")
            boolean(item["input_complete"], "input_complete")
        else:
            uint(item, "consumed ingress prefix")


def _dispatch_map(value):
    if not isinstance(value, dict):
        raise ValueError("consumed_dispatch must be an object")
    for key, item in value.items():
        identifier(key, "handoff queue ID")
        uint(item, "consumed handoff prefix")


def _identity(value, required, optional=(), nullable_line=False):
    shape(value, required, optional)
    uint(value["ingress_port"], "ingress port", 65535)
    uint(value["ingress_seq"], "ingress sequence", U64-1)
    identifier(value["external_identity"], "external_identity")
    if nullable_line:
        nullable(value["line"], identifier, "line")
    else:
        identifier(value["line"], "line")


def _packet(value):
    uint(value["ingress_port"], "ingress port", 65535)
    uint(value["ingress_seq"], "ingress sequence", U64-1)
    nullable(value["line"], identifier, "line")
    if value["selection_seq"] is not None:
        uint(value["selection_seq"], "selection sequence", U64-1)
    nullable(value["external_identity"], identifier, "external_identity")
    nullable(value["flow_id"], identifier, "flow_id")
    uint(value["arrival_ns"], "arrival")
    uint(value["length"], "length", (1 << 32)-1)
    uint(value["vlan_id"], "VLAN", 4094)
    uint(value["segments"], "segments", (1 << 32)-1)
    shape(value["length_flags"], ("fcs_present", "vlan_retained"))
    if value["length_flags"]["fcs_present"] is not False or value["length_flags"]["vlan_retained"] is not True:
        raise ValueError("v1 requires retained VLAN and no FCS")
    nullable(value["app"], identifier, "app")
    if value["selected_late"] is not None:
        uint(value["selected_late"], "selected_late", 1)
    nullable(value["input_error"], identifier, "input_error")
    if any(value[k] is None for k in ("line", "selection_seq", "external_identity", "flow_id")) and not value["input_error"]:
        raise ValueError("missing classification/identity requires explicit input_error")


def _validate_record(row, role):
    if not isinstance(row, dict):
        raise ValueError("record must be an object")
    for key in ENVELOPE:
        if key not in row:
            raise ValueError(f"missing envelope {key}")
    identifier(row["run"], "run")
    identifier(row["stream_id"], "stream_id")
    uint(row["seq"], "stream sequence")
    extensions = row.get("extensions", {})
    if isinstance(extensions, dict):
        versions = [name for name in ("native_observer_v1", "native_observer_v2") if name in extensions]
        if len(versions) > 1:
            raise ValueError("conflicting native provenance")
        for version in versions:
            native = extensions[version]
            shape(native, ("producer", "producer_seq", "order"))
            uint(native["producer"], "native producer", 4 if version.endswith("v1") else 6)
            uint(native["producer_seq"], "native producer sequence")
            uint(native["order"], "native reservation order")
    kind = row["kind"]
    if not isinstance(kind, str) or kind not in ROLES[role] | {"gap"}:
        raise ValueError(f"unsupported {role} record kind {kind!r}")
    if kind in {"packet", "expected"}:
        extras = ("egress_port", "l2_bytes", "wire_bytes", "late", "outcome", "service_ns",
                  "deadline_ns", "release_ns", "clamp_extra_ns", "q_bytes", "q_drain_ns")
        shape(row, ENVELOPE + PACKET_FIELDS + (extras if kind == "expected" else ()), ("identity_error",))
        _packet(row)
        if "identity_error" in row:
            nullable(row["identity_error"], identifier, "identity_error")
        if kind == "expected":
            if row["egress_port"] is None and row["outcome"] != "invalid_input":
                raise ValueError("only invalid input may have unknown egress")
            if row["egress_port"] is not None:
                uint(row["egress_port"], "egress port", 65535)
            boolean(row["late"], "late")
            for key in ("l2_bytes", "wire_bytes", "clamp_extra_ns", "q_bytes", "q_drain_ns"):
                uint(row[key], key)
            for key in ("service_ns", "deadline_ns", "release_ns"):
                nullable(row[key], uint, key)
            if row["outcome"] not in {"accepted", "q_drop", "trace_loss", "trace_outage", "invalid_input"}:
                raise ValueError("unknown expected outcome")
    elif kind in {"frontier", "end"}:
        shape(row, ENVELOPE + ("ingress_port", "next_ingress_seq") + (("through_ns",) if kind == "frontier" else ()))
        uint(row["ingress_port"], "ingress port", 65535)
        uint(row["next_ingress_seq"], "next ingress sequence")
        if kind == "frontier":
            uint(row["through_ns"], "frontier", U64-1)
    elif kind in {"receive", "before"}:
        time_key, seq_key = ("receive_ns", "receive_seq") if kind == "receive" else ("ingress_ts_ns", "before_seq")
        shape(row, ENVELOPE + IDENTITY_FIELDS + (time_key, seq_key))
        for key in ("ingress_port", "egress_port"):
            uint(row[key], key, 65535)
        for key in ("line", "external_identity", "flow_id"):
            identifier(row[key], key)
        nullable(row["app"], identifier, "app")
        uint(row[seq_key], seq_key, U64-1)
        uint(row["length"], "length", (1 << 32)-1)
        uint(row["vlan_id"], "VLAN", 4094)
        if row["selected_late"] is not None:
            uint(row["selected_late"], "selected_late", 1)
        nullable(row[time_key], uint, time_key)
        if (row[time_key] is None) != (row["timestamp"] is None):
            raise ValueError("raw and mapped timestamps must both be present or null")
        if row["timestamp"] is not None:
            timestamp_fields(row["timestamp"])
            allowed = {"external_rx", "tg_tx_port", "fake"} if kind == "before" else {"external_rx", "tg_rx", "software_rx", "tx_submit", "fake"}
            if row["timestamp"]["point"] not in allowed:
                raise ValueError("timestamp point does not match observation boundary")
    elif kind in {"offer", "terminal"}:
        required = ENVELOPE + ("ingress_port", "ingress_seq", "external_identity", "line", "at_ns")
        required += ("actual_selection_seq",) if kind == "offer" else ("reason",)
        _identity(row, required, ("subreason", "bytes") if kind == "terminal" else (),
                  nullable_line=kind=="terminal" and row["reason"]=="invalid_input_drop")
        uint(row["at_ns"], "at_ns")
        if kind == "offer":
            uint(row["actual_selection_seq"], "actual selection sequence", U64-1)
        else:
            if row["reason"] not in TERMINALS:
                raise ValueError("unknown terminal reason")
            if "subreason" in row:
                identifier(row["subreason"], "subreason")
            if "bytes" in row:
                uint(row["bytes"], "terminal bytes")
    elif kind in {"attempt", "dispatch"}:
        common = ("egress_port", "seen_next_ingress_seq", "frontiers")
        extra = (("queue_id", "attempt_id", "gate_ns", "submit_before_ns", "submit_after_ns",
                  "consumed_dispatch", "offered", "accepted_count") if kind == "attempt" else
                 ("at_ns", "handoff_queue_id", "first_handoff_seq", "identities"))
        shape(row, ENVELOPE + common + extra)
        uint(row["egress_port"], "egress port", 65535)
        _port_map(row["seen_next_ingress_seq"])
        _port_map(row["frontiers"], frontier=True)
        if kind == "attempt":
            uint(row["queue_id"], "queue ID", 65535)
            uint(row["attempt_id"], "attempt ID", U64-1)
            for key in ("gate_ns", "submit_before_ns", "submit_after_ns"):
                uint(row[key], key)
            if not row["gate_ns"] <= row["submit_before_ns"] <= row["submit_after_ns"]:
                raise ValueError("TX call timestamp order is impossible")
            _dispatch_map(row["consumed_dispatch"])
            if not isinstance(row["offered"], list):
                raise ValueError("offered must be an array")
            uint(row["accepted_count"], "accepted_count", len(row["offered"]))
            for item in row["offered"]:
                _identity(item, ("ingress_port", "ingress_seq", "external_identity", "line", "gate_ns", "handoff_queue_id", "handoff_seq"), ("consumed_dispatch",))
                uint(item["gate_ns"], "selection gate")
                if not row["gate_ns"] <= item["gate_ns"] <= row["submit_before_ns"]:
                    raise ValueError("selection gate is outside TX selection interval")
                identifier(item["handoff_queue_id"], "handoff queue ID")
                uint(item["handoff_seq"], "handoff sequence", U64-1)
                if "consumed_dispatch" in item:
                    _dispatch_map(item["consumed_dispatch"])
        else:
            uint(row["at_ns"], "dispatch at_ns")
            identifier(row["handoff_queue_id"], "handoff queue ID")
            first = uint(row["first_handoff_seq"], "first handoff sequence")
            if not isinstance(row["identities"], list) or not row["identities"] or first + len(row["identities"]) > U64:
                raise ValueError("dispatch identities must be a nonempty bounded array")
            for item in row["identities"]:
                _identity(item, ("ingress_port", "ingress_seq", "external_identity", "line"))
    elif kind == "counter":
        shape(row, ENVELOPE + ("scope", "scope_id", "at_ns", "clock_id", "reset_generation", "values", "descriptors"), ("phase", "reasons"))
        if row["scope"] not in {"port", "line", "pool", "observer"}:
            raise ValueError("unknown counter scope")
        for key in ("scope_id", "clock_id"):
            identifier(row[key], key)
        for key in ("at_ns", "reset_generation"):
            uint(row[key], key)
        if not isinstance(row["values"], dict) or not isinstance(row["descriptors"], dict) or set(row["values"]) != set(row["descriptors"]):
            raise ValueError("counter values and descriptors must have matching keys")
        if "reasons" in row and not isinstance(row["reasons"], dict):
            raise ValueError("counter reasons must be an object")
        for name, value in row["values"].items():
            identifier(name, "counter name")
            nullable(value, uint, name)
            desc = row["descriptors"][name]
            shape(desc, ("unit", "semantics", "source"), ("status", "reason"))
            if desc["unit"] not in {"packets", "bytes", "events", "mbufs"} or desc["semantics"] not in {"terminal_disjoint", "overlapping_diagnostic", "gauge"}:
                raise ValueError("unknown counter unit/semantics")
            identifier(desc["source"], "counter source")
            if value is None and not desc.get("reason") and not row.get("reasons", {}).get(name):
                raise ValueError("unavailable counter requires a reason")
            if "status" in desc and desc["status"] not in {"measured", "not_applicable", "missing", "unsupported"}:
                raise ValueError("unknown counter measurement status")
            if "reason" in desc:
                nullable(desc["reason"], identifier, "reason")
        if "phase" in row and row["phase"] not in {"start", "periodic", "end"}:
            raise ValueError("unknown counter phase")
        if "reasons" in row:
            if not isinstance(row["reasons"], dict) or set(row["reasons"]) - set(row["values"]):
                raise ValueError("counter reasons must match counter names")
            for reason in row["reasons"].values():
                identifier(reason, "counter reason")
    elif kind == "summary":
        shape(row, ENVELOPE + ("schema", "input_complete", "event_count", "ingress_sha256", "expected_sha256", "config_sha256"),
              ("trace_sha256", "trace_hashes", "input_streams", "input_stream_hashes", "input_stream_counts", "port_end_counts", "end_counts", "per_port", "per_line", "line_counts", "oracle_version", "resource_peaks", "config_file_sha256"))
        if row["schema"] != "hng-g2-reference-v1":
            raise ValueError("unsupported reference summary schema")
        boolean(row["input_complete"], "input_complete")
        uint(row["event_count"], "event_count")
        for key in ("ingress_sha256", "expected_sha256", "config_sha256"):
            digest(row[key], key)
        if "trace_sha256" in row:
            if not isinstance(row["trace_sha256"], dict):
                raise ValueError("trace_sha256 must map line names to hashes")
            for line, value in row["trace_sha256"].items():
                identifier(line, "trace line")
                digest(value, "trace sha256")
        if "end_counts" in row:
            _port_map(row["end_counts"])
        if "line_counts" in row:
            if not isinstance(row["line_counts"], dict):
                raise ValueError("line_counts must be an object")
            for line, counts in row["line_counts"].items():
                identifier(line, "line")
                shape(counts, ("offers", "accepted", "q_drop", "trace_loss", "trace_outage", "invalid_input"))
                for key, value in counts.items():
                    if key != "extensions":
                        uint(value, key)
        if "oracle_version" in row:
            identifier(row["oracle_version"], "oracle_version")
        if "resource_peaks" in row:
            if not isinstance(row["resource_peaks"], dict):
                raise ValueError("resource_peaks must be an object")
            for key, value in row["resource_peaks"].items():
                identifier(key, "resource name")
                uint(value, key)
        if "input_streams" in row:
            if not isinstance(row["input_streams"], list):
                raise ValueError("input_streams must be an array")
            seen = set()
            for item in row["input_streams"]:
                shape(item, ("stream_id", "role", "file_sha256", "records_sha256", "record_count", "complete", "dropped_records"), ("issues",))
                identifier(item["stream_id"], "input stream ID")
                if item["stream_id"] in seen or item["role"] != "ingress":
                    raise ValueError("duplicate/non-ingress input stream")
                seen.add(item["stream_id"])
                digest(item["file_sha256"], "input file hash")
                digest(item["records_sha256"], "input records hash")
                uint(item["record_count"], "input record count")
                uint(item["dropped_records"], "input dropped records")
                boolean(item["complete"], "input complete")
                if "issues" in item:
                    if not isinstance(item["issues"], list):
                        raise ValueError("input stream issues must be an array")
                    for issue in item["issues"]:
                        identifier(issue, "input stream issue")
    else:
        shape(row, ENVELOPE + ("first_missing_seq", "last_missing_seq", "reason"))
        uint(row["first_missing_seq"], "first missing seq")
        uint(row["last_missing_seq"], "last missing seq")
        if row["first_missing_seq"] > row["last_missing_seq"]:
            raise ValueError("invalid gap range")
        identifier(row["reason"], "gap reason")


def validate_record(row, role):
    try:
        _validate_record(row, role)
    except (ValueError, TypeError, KeyError, OverflowError) as exc:
        if isinstance(exc, StreamError):
            raise
        raise StreamError("RECORD_SCHEMA", str(exc)) from exc


def handoff_actors(queue):
    """Validate the explicit v2 scheduler/TX binding, if one is registered."""
    value = queue.get("extensions", {}).get("native_actors")
    if value is None:
        return {}
    shape(value, ("producer", "consumer"))
    result = {}
    for role, allowed in (("producer", (1, 2)), ("consumer", (3, 6))):
        item = value[role]
        shape(item, ("stream_id", "producer"))
        identifier(item["stream_id"], "native actor stream")
        uint(item["producer"], "native actor", 6)
        if item["producer"] not in allowed:
            raise ValueError("native actor role mismatch")
        result[role] = item["stream_id"], item["producer"]
    if (queue["handoff_queue_id"] != f"handoff-{result['producer'][1]-1}" or
            result["consumer"][1] == 6 and result["producer"][1] != 2):
        raise ValueError("native queue ownership mismatch")
    return result


def actor(row):
    """A normalized logical stream may preserve several native SPSC actors."""
    extensions = row.get("extensions", {})
    provenance = extensions.get("native_observer_v2", extensions.get("native_observer_v1"))
    return (row["stream_id"], provenance["producer"]) if provenance is not None else row["stream_id"]


def _header(row, role=None, run=None):
    shape(row, ENVELOPE + ("schema", "role", "producer", "producer_commit", "clock_id"))
    if row["kind"] != "header" or row["schema"] != "hng-g2-events-v1" or row["role"] not in ROLES:
        raise ValueError("unsupported stream header schema/role")
    if uint(row["seq"], "header seq") != 0:
        raise ValueError("header sequence must be zero")
    for key in ("run", "stream_id", "producer", "producer_commit", "clock_id"):
        identifier(row[key], key)
    if (role is not None and row["role"] != role) or (run is not None and row["run"] != run):
        raise ValueError("stream role/run does not match registration")


class StreamReader:
    """Read once, validate lazily, and expose full integrity only after exhaustion."""
    def __init__(self, path, role=None, run=None):
        self.path = Path(path)
        self.header = self.seal = None
        self.integrity = {}
        self._source = self.path.open("rb")
        self._file_hash = hashlib.sha256()
        self._records_hash = hashlib.sha256()
        self._count = 0
        self._next = 1
        self._done = False
        self._ports = {}
        self._observation_seq = {}
        self._summary_seen = False
        try:
            self.header = self._read()
            if self.header is None:
                raise ValueError("empty stream")
            _header(self.header, role, run)
            self._records_hash.update(canonical(self.header) + b"\n")
        except Exception as exc:
            self._source.close()
            if isinstance(exc, StreamError):
                raise
            raise StreamError("HEADER_SCHEMA", str(exc)) from exc

    def _read(self):
        raw = self._source.readline()
        if not raw:
            return None
        self._file_hash.update(raw)
        if not raw.endswith(b"\n") or raw.endswith(b"\r\n"):
            raise StreamError("LINE_TERMINATION", "every JSONL record requires LF termination")
        try:
            row = strict_json(raw.decode("utf-8"))
            if not isinstance(row, dict):
                raise ValueError("JSONL record must be an object")
            return row
        except (ValueError, UnicodeError) as exc:
            raise StreamError("JSON_SYNTAX", str(exc)) from exc

    def __iter__(self):
        return self

    def _fail(self, code, message, status="INVALID"):
        self._done = True
        self.integrity = dict(stream_id=self.header["stream_id"], role=self.header["role"],
            file_sha256=self._file_hash.hexdigest(), records_sha256=self._records_hash.hexdigest(),
            record_count=self._count, complete=False,
            dropped_records=self.seal.get("dropped_records", 0) if self.seal else 0,
            issues=[code])
        self._source.close()
        raise StreamError(code, message, status)

    def _semantic(self, row):
        kind = row["kind"]
        if self.header["role"] == "ingress" and kind != "gap":
            port = self._ports.setdefault(row["ingress_port"], dict(next=0, arrival=0, frontier=-1, end=False))
            if port["end"]:
                self._fail("INPUT_AFTER_END", "input event follows port end")
            if kind == "packet":
                if row["ingress_seq"] != port["next"]:
                    self._fail("INGRESS_SEQUENCE", "ingress ordinal is not contiguous")
                if row["arrival_ns"] < port["arrival"] or row["arrival_ns"] <= port["frontier"]:
                    self._fail("INPUT_TIME", "input time regressed or violates closed frontier")
                port.update(next=port["next"]+1, arrival=row["arrival_ns"])
            else:
                if row["next_ingress_seq"] != port["next"]:
                    self._fail("INPUT_MARKER_COUNT", "input marker count mismatch")
                if kind == "end":
                    port["end"] = True
                else:
                    if row["through_ns"] < max(port["arrival"], port["frontier"]):
                        self._fail("INPUT_FRONTIER", "backward input frontier")
                    port["frontier"] = row["through_ns"]
        # Wire ordinals are checked by the receiver join, independently of export order.
        if self._summary_seen:
            self._fail("SUMMARY_ORDER", "reference summary must be last data record")
        self._summary_seen = kind == "summary"

    def __next__(self):
        if self._done:
            raise StopIteration
        try:
            row = self._read()
            if row is None:
                self._fail("MISSING_SEAL", "stream has no seal", "INCONCLUSIVE")
            if row.get("run") != self.header["run"] or row.get("stream_id") != self.header["stream_id"]:
                self._fail("STREAM_IDENTITY", "mixed stream/run identity")
            try:
                sequence = uint(row.get("seq"), "stream sequence")
            except ValueError as exc:
                self._fail("STREAM_SEQUENCE", str(exc))
            if sequence != self._next:
                self._fail("STREAM_SEQUENCE_GAP" if sequence > self._next else "STREAM_SEQUENCE",
                           "stream sequence is not contiguous", "INCONCLUSIVE" if sequence > self._next else "INVALID")
            if row.get("kind") == "seal":
                self.seal = row
                shape(row, ENVELOPE + ("record_count", "records_sha256", "complete", "dropped_records"))
                uint(row["record_count"], "record count")
                uint(row["dropped_records"], "dropped records")
                boolean(row["complete"], "complete")
                digest(row["records_sha256"])
                if row["record_count"] != self._count or row["records_sha256"] != self._records_hash.hexdigest():
                    self._fail("SEAL_MISMATCH", "seal record count/hash mismatch")
                if self._read() is not None:
                    self._fail("AFTER_SEAL", "seal must be final record")
                if not row["complete"] or row["dropped_records"]:
                    self._fail("CAPTURE_INCOMPLETE", "producer reports incomplete capture", "INCONCLUSIVE")
                if self.header["role"] == "ingress" and (not self._ports or not all(p["end"] for p in self._ports.values())):
                    self._fail("MISSING_INPUT_END", "missing per-port input end", "INCONCLUSIVE")
                if self.header["role"] == "expected" and not self._summary_seen:
                    self._fail("MISSING_SUMMARY", "missing terminal reference summary", "INCONCLUSIVE")
                self.integrity = dict(stream_id=self.header["stream_id"], role=self.header["role"],
                    file_sha256=self._file_hash.hexdigest(), records_sha256=self._records_hash.hexdigest(),
                    record_count=self._count, complete=True, dropped_records=0, issues=[])
                self._done = True
                self._source.close()
                raise StopIteration
            if self._next == U64:
                self._fail("STREAM_SEQUENCE_OVERFLOW", "data record leaves no uint64 seal sequence")
            validate_record(row, self.header["role"])
            self._semantic(row)
            self._records_hash.update(canonical(row)+b"\n")
            self._count += 1
            self._next += 1
            if row["kind"] == "gap":
                self._fail("CAPTURE_GAP", "producer records a capture gap", "INCONCLUSIVE")
            return row
        except StreamError as exc:
            if not self._done:
                self._fail(exc.code, str(exc), exc.measurement_status)
            raise
        except (ValueError, TypeError, KeyError, OverflowError) as exc:
            self._fail("RECORD_SCHEMA", str(exc))


class StreamWriter:
    def __init__(self, destination, run, stream_id, role, clock_id,
                 producer="g2", producer_commit="software"):
        self.header = dict(kind="header", schema="hng-g2-events-v1", run=run,
            stream_id=stream_id, role=role, producer=producer,
            producer_commit=producer_commit, clock_id=clock_id, seq=0)
        try:
            _header(self.header)
        except (ValueError, TypeError) as exc:
            raise StreamError("HEADER_SCHEMA", str(exc)) from exc
        self._owned = isinstance(destination, (str, bytes, Path))
        self._destination = open(destination, "x", encoding="utf-8", newline="\n") if self._owned else destination
        self._hash = hashlib.sha256()
        self._count = 0
        self._done = False
        self._write(self.header)

    def _write(self, row, hashed=True):
        data = canonical(row)+b"\n"
        self._destination.write(data.decode("ascii"))
        if hashed:
            self._hash.update(data)

    def emit(self, kind, **values):
        if self._done or set(values) & set(ENVELOPE):
            raise StreamError("WRITER_STATE", "finished writer or envelope override")
        if self._count >= U64-1:
            raise StreamError("STREAM_SEQUENCE_OVERFLOW", "no uint64 seal sequence remains")
        row = dict(values, kind=kind, run=self.header["run"], stream_id=self.header["stream_id"], seq=self._count+1)
        validate_record(row, self.header["role"])
        self._write(row)
        self._count += 1
        return row

    def finish(self, complete=True, dropped_records=0):
        if self._done:
            raise StreamError("WRITER_STATE", "writer already finished")
        try:
            boolean(complete, "complete")
            uint(dropped_records, "dropped records")
        except ValueError as exc:
            raise StreamError("SEAL_SCHEMA", str(exc)) from exc
        seal = dict(kind="seal", run=self.header["run"], stream_id=self.header["stream_id"],
            seq=self._count+1, record_count=self._count, records_sha256=self._hash.hexdigest(),
            complete=complete, dropped_records=dropped_records)
        self._write(seal, hashed=False)
        self._destination.flush()
        if self._owned:
            self._destination.close()
        self._done = True
        return seal


def main():
    parser=argparse.ArgumentParser(description="Validate complete event-stream seals and ordinals without network access.")
    parser.add_argument("stream")
    args=parser.parse_args()
    try:
        reader=StreamReader(args.stream)
        for _ in reader:
            pass
        result=dict(status="PASS",measurement_status="PASS",scope="software-only",g2_qualified=False,
                    reason_codes=[],stream_integrity=reader.integrity)
    except (StreamError,OSError) as exc:
        result=dict(status="INVALID",measurement_status=getattr(exc,"measurement_status","INVALID"),
                    scope="software-only",g2_qualified=False,reason_codes=[getattr(exc,"code","STREAM_IO")])
    print(json.dumps(result,allow_nan=False,sort_keys=True))
    return 0 if result["status"]=="PASS" else 2


if __name__=="__main__":
    sys.exit(main())
