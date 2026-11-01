# SPDX-License-Identifier: Apache-2.0
"""Normalize typed snapshot deltas without treating unavailable counters as zero."""
import argparse
import json
from pathlib import Path
import sys

from .protocol import StreamError, StreamReader, TERMINALS, digest, validate_record
from .reference import strict_json

PORT_FIELDS = {"rx_returned", "tx_accepted", "still_owned"} | TERMINALS
OBSERVER_FIELDS = {"records_attempted", "records_emitted", "records_dropped", "capture_drops",
                   "queue_high_water", "queue_capacity", "malformed_records", "identity_errors",
                   "timestamp_missing", "clock_invalid"}


def normalize_counters(rows, manifest):
    result = dict(schema="hng-g2-counters-v1", run=manifest["run"], check="counter-evidence",
                  scope=manifest["scope"], g2_qualified=False, measurement_status="PASS",
                  ports={}, observer={}, diagnostics={}, descriptors={}, endpoints={}, reason_codes=[])
    reasons, invalid = set(), False
    def issue(code, structural=False):
        nonlocal invalid
        reasons.add(code)
        invalid |= structural
    snapshots = {}
    for row in rows:
        validate_record(row, "counters")
        if row["run"] != manifest["run"]:
            raise StreamError("COUNTER_RUN", "counter run differs from registration")
        key = row["scope"], row["scope_id"]
        if key not in snapshots:
            if len(snapshots) >= manifest["capacity"]["max_unresolved"]:
                issue("counter_scope_capacity")
                continue
            snapshots[key] = dict(start=None, end=None, last=None, reset=False, bad=set(), peaks={})
        state = snapshots[key]
        previous = state["last"]
        if previous is not None:
            if row["at_ns"] < previous["at_ns"]:
                issue("counter_time_backward", True)
            if row["clock_id"] != previous["clock_id"]:
                issue("counter_clock_changed")
                state["reset"] = True
            if row["reset_generation"] != previous["reset_generation"]:
                issue("counter_reset")
                state["reset"] = True
            for name, descriptor in row["descriptors"].items():
                old = previous["descriptors"].get(name)
                if old is not None and any(descriptor[k] != old[k] for k in ("unit", "semantics", "source")):
                    issue("counter_descriptor_changed", True)
                    state["bad"].add(name)
                value, before = row["values"][name], previous["values"].get(name)
                if descriptor["semantics"] != "gauge" and value is not None and before is not None and value < before:
                    issue("counter_regression")
                    state["bad"].add(name)
        phase = row.get("phase")
        if phase in {"start", "end"}:
            if state[phase] is not None or (phase == "start" and previous is not None):
                issue("counter_duplicate_endpoint", True)
            else:
                state[phase] = row
        elif phase is None:
            issue("counter_endpoint_missing")
        if state["end"] is not None and state["end"] is not row:
            issue("counter_after_end", True)
        for name, value in row["values"].items():
            if value is not None and row["descriptors"][name]["semantics"] == "gauge":
                state["peaks"][name] = max(state["peaks"].get(name, value), value)
        state["last"] = row
    for (scope, token), state in snapshots.items():
        start, end = state["start"], state["end"]
        if start is None or end is None:
            issue("counter_endpoint_missing")
        result["endpoints"][f"{scope}:{token}"] = dict(
            start_ns=start["at_ns"] if start else None, end_ns=end["at_ns"] if end else None,
            start_clock_id=start["clock_id"] if start else None,
            end_clock_id=end["clock_id"] if end else None,
            start_generation=start["reset_generation"] if start else None,
            end_generation=end["reset_generation"] if end else None, reset_seen=state["reset"])
        final_row = end or state["last"]
        values = {}
        for name, final in final_row["values"].items():
            descriptor = final_row["descriptors"][name]
            before = start["values"].get(name) if start is not None else None
            unavailable = (start is None or end is None or name in state["bad"] or
                           descriptor.get("status", "measured") != "measured")
            if unavailable:
                values[name] = None
            elif descriptor["semantics"] == "gauge":
                values[name] = final
            else:
                values[name] = None if state["reset"] or final is None or before is None else final-before
            if name == "queue_high_water" and values[name] is not None:
                values[name] = state["peaks"][name]
        result["descriptors"][f"{scope}:{token}"] = final_row["descriptors"]
        if scope in {"port", "observer"}:
            result["ports" if scope=="port" else "observer"][token] = values
        else:
            result["diagnostics"][f"{scope}:{token}"] = values
    required = [("port", str(port["ingress_port"]), PORT_FIELDS) for port in manifest["ports"]]
    required += [("observer", stream["stream_id"], OBSERVER_FIELDS) for stream in manifest["streams"]
                 if stream["role"] not in {"expected", "counters"}]
    for scope, token, fields in required:
        target = result["ports" if scope=="port" else "observer"]
        if token not in target:
            issue("counter_endpoint_missing")
        values = target.setdefault(token, {})
        for name in fields:
            if values.setdefault(name, None) is None:
                issue("required_counter_missing")
        if scope == "port":
            descriptors = result["descriptors"].get(f"port:{token}", {})
            for name in TERMINALS | {"tx_accepted"}:
                descriptor = descriptors.get(name)
                if descriptor is not None and (descriptor["semantics"] != "terminal_disjoint" or descriptor["unit"] != "packets"):
                    issue("counter_terminal_semantics", True)
            owned = descriptors.get("still_owned")
            if owned is not None and (owned["semantics"] != "gauge" or owned["unit"] != "packets"):
                issue("counter_terminal_semantics", True)
            received = descriptors.get("rx_returned")
            if received is not None and (received["semantics"] == "gauge" or received["unit"] != "packets"):
                issue("counter_terminal_semantics", True)
            if values["still_owned"]:
                issue("counter_drain_incomplete")
            initial = snapshots.get((scope, token), {}).get("start")
            if initial is not None and initial["values"].get("still_owned"):
                issue("counter_initial_ownership")
            if all(values[name] is not None for name in PORT_FIELDS):
                if values["rx_returned"] != values["tx_accepted"] + sum(values[name] for name in TERMINALS) + values["still_owned"]:
                    issue("counter_conservation")
        else:
            descriptors = result["descriptors"].get(f"observer:{token}", {})
            for name in OBSERVER_FIELDS:
                descriptor = descriptors.get(name)
                if descriptor is not None and ((descriptor["semantics"] == "gauge") !=
                                                (name in {"queue_high_water", "queue_capacity"})):
                    issue("observer_counter_semantics", True)
            if any(values.get(name) for name in ("records_dropped", "capture_drops", "malformed_records",
                                                "identity_errors", "timestamp_missing", "clock_invalid")):
                issue("observer_evidence_incomplete")
            if all(values[name] is not None for name in ("records_attempted", "records_emitted", "records_dropped")):
                if values["records_attempted"] != values["records_emitted"] + values["records_dropped"]:
                    issue("observer_counter_imbalance")
            if values["queue_high_water"] is not None and values["queue_capacity"] is not None:
                if values["queue_high_water"] > values["queue_capacity"] or not values["queue_capacity"]:
                    issue("observer_queue_overflow")
    result["reason_codes"] = sorted(reasons)
    result["measurement_status"] = "INVALID" if invalid else "INCONCLUSIVE" if reasons else "PASS"
    cost = dict(status="PASS", warnings=[], end_backlog={})
    counts = ("records_attempted", "records_emitted", "records_dropped")
    gauges = ("queue_high_water", "queue_capacity")
    fields = (*counts, "capture_drops", *gauges)
    for token, values in result["observer"].items():
        end = snapshots.get(("observer", token), {}).get("end")
        descriptors = result["descriptors"].get("observer:"+token, {})
        available = (not invalid and end is not None and all(values.get(name) is not None for name in fields)
                     and all((descriptors.get(name, {}).get("semantics")=="gauge") == (name in gauges)
                             for name in fields))
        backlog = (end["values"]["records_attempted"]-end["values"]["records_emitted"]
                   -end["values"]["records_dropped"]) if available else None
        cost["end_backlog"][token] = backlog
        checks = dict(observer_cost_unavailable=not available or bool(reasons),
            observer_cost_loss=any(values.get(name) for name in ("records_dropped", "capture_drops")),
            observer_cost_queue_full=(values.get("queue_capacity")==0 or
                all(values.get(name) is not None for name in gauges)
                and values["queue_high_water"]>=values["queue_capacity"]),
            observer_cost_backlog=backlog is not None and backlog != 0)
        cost["warnings"].extend(dict(observer=token, code=code) for code, warning in checks.items() if warning)
    if cost["warnings"]:
        cost["status"] = "INCONCLUSIVE"
    result["observer_cost"] = cost
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest")
    args = parser.parse_args()
    try:
        path = Path(args.manifest)
        manifest = strict_json(path.read_bytes())
        if not isinstance(manifest, dict) or not isinstance(manifest.get("streams"), list):
            raise ValueError("manifest requires a stream inventory")
        streams = []
        for stream in manifest["streams"]:
            if not isinstance(stream, dict):
                raise ValueError("stream inventory entries must be objects")
            if stream.get("role") == "counters":
                streams.append(stream)
        if not streams:
            raise StreamError("COUNTER_STREAM_MISSING", "counter stream is not registered", "INCONCLUSIVE")
        integrity = []
        def rows():
            for stream in streams:
                digest(stream["file_sha256"], "counter file hash")
                source = StreamReader(path.parent / stream["path"], role="counters", run=manifest["run"])
                if source.header["stream_id"] != stream["stream_id"]:
                    raise StreamError("COUNTER_STREAM_ID", "counter stream ID differs from registration")
                yield from source
                if source.integrity["file_sha256"] != stream["file_sha256"]:
                    raise StreamError("SOURCE_HASH_MISMATCH", "counter snapshot bytes changed")
                integrity.append(source.integrity)
        result = normalize_counters(rows(), manifest)
        result["streams"] = integrity
    except (ValueError, KeyError, TypeError, OSError, OverflowError) as exc:
        result = dict(schema="hng-g2-counters-v1", check="counter-evidence", scope="software-only",
                      g2_qualified=False, measurement_status=getattr(exc, "measurement_status", "INVALID"),
                      reason_codes=[getattr(exc, "code", "COUNTER_INVALID")], reason=str(exc))
    print(json.dumps(result, sort_keys=True, allow_nan=False))
    return 0 if result["measurement_status"] == "PASS" else 2


if __name__ == "__main__":
    sys.exit(main())
