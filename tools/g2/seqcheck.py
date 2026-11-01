# SPDX-License-Identifier: Apache-2.0
"""Bind external full-run seqcheck evidence and reconcile intentional drops."""
import hashlib
import os
from pathlib import Path
import re
import stat

from .protocol import StreamError, boolean, digest, shape
from .reference import strict_json, uint

METRICS = ("ingress", "receiver", "ordinary_reorder", "missing", "duplicate_ingress",
           "duplicate_receiver", "unexpected", "flag_mismatch")
ISSUES = METRICS[2:]
ORDER = ("ordinary_reorder", "duplicate_receiver", "unexpected", "flag_mismatch")
JSON_LIMIT = 16 * 1024 * 1024


def _source(root, path, expected, *, size=None, json_file=False):
    digest(expected)
    if not isinstance(path, str) or not path:
        raise ValueError("source path must be a nonempty string")
    fd = os.open(Path(root) / path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as source:
        before = os.fstat(source.fileno())
        if not stat.S_ISREG(before.st_mode):
            raise ValueError("seqcheck sources must be regular files")
        if size is not None and before.st_size != size:
            raise StreamError("SEQCHECK_SOURCE_SIZE", "capture byte size differs from checker input")
        if json_file and before.st_size > JSON_LIMIT:
            raise ValueError("seqcheck JSON exceeds 16 MiB")
        checksum, length, chunks = hashlib.sha256(), 0, []
        while chunk := source.read(1024 * 1024):
            length += len(chunk)
            if json_file and length > JSON_LIMIT:
                raise ValueError("seqcheck JSON exceeds 16 MiB")
            checksum.update(chunk)
            if json_file:
                chunks.append(chunk)
        after = os.fstat(source.fileno())
        if (length != before.st_size or before.st_size != after.st_size
                or before.st_mtime_ns != after.st_mtime_ns or checksum.hexdigest() != expected):
            raise StreamError("SEQCHECK_SOURCE_HASH", "registered seqcheck source bytes changed")
    return strict_json(b"".join(chunks)) if json_file else None


def _counts(row, extra=(), partial=False):
    shape(row, METRICS + tuple(extra))
    for key in METRICS:
        uint(row[key], key)
    if partial:
        return
    if row["missing"] + row["duplicate_ingress"] > row["ingress"]:
        raise ValueError("missing count exceeds unique ingress count")
    if any(row[key] > row["receiver"] for key in ORDER):
        raise ValueError("receiver issue count exceeds receiver packets")


def _validate(doc, pair, manifest):
    shape(doc, ("schema", "scope", "error", "status", "complete", "inputs", "totals", "flows",
                "details_limit", "details", "details_truncated"))
    if doc["schema"] != "hng-seqcheck-v1" or doc["status"] not in {"PASS", "FAIL", "INVALID"}:
        raise ValueError("unsupported seqcheck schema/status")
    if doc["scope"] != "supplied-captures":
        raise ValueError("unsupported seqcheck capture scope")
    if doc["error"] is not None and not isinstance(doc["error"], str):
        raise ValueError("seqcheck error diagnostic must be a string or null")
    boolean(doc["complete"], "comparison complete")
    partial = doc["status"] == "INVALID"
    if partial == doc["complete"]:
        raise ValueError("seqcheck status contradicts comparison completeness")
    shape(doc["inputs"], ("ingress", "receiver"))
    healthy = doc["complete"] and doc["error"] is None
    _counts(doc["totals"], partial=partial)
    for role, source in doc["inputs"].items():
        shape(source, ("path", "error", "sha256", "byte_size", "packets", "health"))
        if (not isinstance(source["path"], str)
                or (source["error"] is not None and not isinstance(source["error"], str))):
            raise ValueError("input path/error diagnostics must be strings or null error")
        if source["sha256"] is not None or not partial:
            digest(source["sha256"])
        uint(source["byte_size"], "capture byte size")
        uint(source["packets"], "capture packets")
        if not partial and source["packets"] != doc["totals"][role]:
            raise ValueError("input packet count differs from totals")
        shape(source["health"], ("complete", "capture_drops"))
        boolean(source["health"]["complete"], "capture complete")
        if source["health"]["capture_drops"] is not None or not partial:
            uint(source["health"]["capture_drops"], "capture drops")
        healthy &= (source["health"]["complete"] and source["health"]["capture_drops"] == 0
                    and source["error"] is None and source["sha256"] is not None)
    if not isinstance(doc["flows"], list) or len(doc["flows"]) > 65536:
        raise ValueError("flows must be a bounded array")
    vlans = {line["vlan_id"] for line in manifest["lines"]
             if (line["ingress_port"], line["egress_port"]) == pair}
    flows, sums = set(), dict.fromkeys(METRICS, 0)
    for flow in doc["flows"]:
        _counts(flow, ("flow_id", "vlan_id"), partial=partial)
        uint(flow["vlan_id"], "flow VLAN", 4095)
        if (not isinstance(flow["flow_id"], str)
                or not re.fullmatch(r"[0-9a-f]{8}:[0-9a-f]{8}:[0-9]{1,5}:[0-9]{1,5}", flow["flow_id"])):
            raise ValueError("unsupported probe flow ID")
        for port in flow["flow_id"].split(":")[2:]:
            uint(int(port), "flow UDP port", 65535)
        key = flow["flow_id"], flow["vlan_id"]
        if flow["vlan_id"] not in vlans or key in flows:
            raise ValueError("unknown VLAN or duplicate flow identity")
        flows.add(key)
        for name in METRICS:
            sums[name] += flow[name]
    if not partial and sums != {key: doc["totals"][key] for key in METRICS}:
        raise ValueError("seqcheck totals do not equal flow sums")
    uint(doc["details_limit"], "details limit", 100000)
    shape(doc["details"], ISSUES)
    shape(doc["details_truncated"], ISSUES)
    for name in ISSUES:
        details, count = doc["details"][name], doc["totals"][name]
        if (not isinstance(details, list) or len(details) > doc["details_limit"]
                or (not partial and len(details) > count)):
            raise ValueError("detail list exceeds count/limit")
        boolean(doc["details_truncated"][name], "details truncated")
        if not partial and doc["details_truncated"][name] != (count > len(details)):
            raise ValueError("detail truncation contradicts full count")
        for item in details:
            shape(item, ("flow_id", "vlan_id", "sequence"), ("ingress_rank", "receiver_index"))
            if (item["flow_id"], item["vlan_id"]) not in flows:
                raise ValueError("detail identifies an unregistered flow")
            for key in ("vlan_id", "sequence", "ingress_rank", "receiver_index"):
                if key in item:
                    uint(item[key], key, 4095 if key == "vlan_id" else (1 << 64)-1)
            for key, role in (("ingress_rank", "ingress"), ("receiver_index", "receiver")):
                if not partial and key in item and item[key] >= doc["inputs"][role]["packets"]:
                    raise ValueError("detail index exceeds packet count")
    violations = any(doc["totals"][key] for key in ISSUES)
    if ((not partial and not healthy) or (doc["status"] == "PASS" and violations)
            or (doc["status"] == "FAIL" and (not violations or doc["totals"]["duplicate_ingress"]))):
        raise ValueError("seqcheck status contradicts counts/health")
    return healthy and doc["status"] != "INVALID", flows


def _check(status="INCONCLUSIVE", value=None):
    return dict(status=status, threshold=0, value=value)


def load_checks(registrations, root, manifest, counters):
    """Return input-port checks; missing is reconciled with Q/loss/outage run totals.

    Receiver evidence never independently verifies Q. Probe-only ingress coverage
    requires the full native rx_returned count; no phase-local verdict is implied.
    """
    try:
        return _load_checks(registrations, root, manifest, counters)
    except StreamError:
        raise
    except (OSError, ValueError, TypeError, KeyError, OverflowError, RecursionError) as exc:
        raise StreamError("SEQCHECK_SCHEMA", str(exc), "INCONCLUSIVE") from exc


def _load_checks(registrations, root, manifest, counters):
    pairs = {(row["ingress_port"], row["egress_port"]) for row in manifest["ports"]}
    result = {str(port): dict(order=_check(), storage=_check(), coverage_scope="input-run",
        flows=[], totals=None, details={}, details_truncated={}, reason_codes=["seqcheck_missing"])
        for port, _ in pairs}
    if counters is None:
        counters = {}
    if not isinstance(counters, dict) or not isinstance(counters.get("ports", {}), dict):
        raise ValueError("normalized counter evidence must contain port objects")
    native_ports = {}
    for port, current in result.items():
        values = counters.get("ports", {}).get(port, {})
        if not isinstance(values, dict):
            raise ValueError("normalized port counters must be an object")
        native_ports[port] = native = {}
        for key in ("q_drop", "trace_loss_drop", "trace_outage_drop", "delay_storage_drop", "rx_returned"):
            if values.get(key) is not None:
                native[key] = uint(values[key], key)
        if native.get("delay_storage_drop", 0):
            current["storage"] = _check("FAIL", native["delay_storage_drop"])
            current["reason_codes"].append("seqcheck_delay_storage_drop")
    seen, identities = set(), set()
    if not isinstance(registrations, list):
        raise ValueError("seqcheck registrations must be an array")
    for registration in registrations:
        shape(registration, ("path", "sha256", "ingress_port", "egress_port", "ingress_path", "receiver_path"))
        pair = tuple(uint(registration[key], key, 65535) for key in ("ingress_port", "egress_port"))
        if pair not in pairs or pair[0] in seen:
            raise ValueError("unknown or duplicate seqcheck direction")
        seen.add(pair[0])
        doc = _source(root, registration["path"], registration["sha256"], json_file=True)
        healthy, flows = _validate(doc, pair, manifest)
        if identities & flows:
            raise ValueError("ambiguous flow identity across directional captures")
        identities.update(flows)
        for role in ("ingress", "receiver"):
            source = doc["inputs"][role]
            if source["sha256"] is not None:
                _source(root, registration[role + "_path"], source["sha256"], size=source["byte_size"])
        counts = doc["totals"]
        current = result[str(pair[0])]
        current.update({key: doc[key] for key in ("flows", "totals", "details", "details_truncated", "details_limit", "inputs")})
        current["reason_codes"] = reasons = []
        native = native_ports[str(pair[0])]
        probe = manifest.get("identity_profile", {}).get("kind") == "probe"
        covered = healthy and probe and native.get("rx_returned") == counts["ingress"]
        if not healthy:
            reasons.append("seqcheck_capture_incomplete" if doc["status"] != "INVALID" else "seqcheck_invalid")
        if not probe:
            reasons.append("seqcheck_probe_coverage_required")
        if "rx_returned" not in native:
            reasons.append("seqcheck_counter_missing")
        elif native["rx_returned"] != counts["ingress"]:
            reasons.append("seqcheck_counter_mismatch")
        if covered:
            order = sum(counts[key] for key in ORDER)
            current["order"] = _check("FAIL" if order else "PASS", order)
            if order:
                reasons.append("seqcheck_order_violation")
        if covered and len(native) == 5 and (counters or {}).get("measurement_status", "PASS") == "PASS":
            allowed = sum(native[key] for key in ("q_drop", "trace_loss_drop", "trace_outage_drop"))
            mismatch = abs(counts["missing"] - allowed)
            value = mismatch + native["delay_storage_drop"]
            current["storage"] = _check("FAIL" if value else "PASS", value)
            if mismatch:
                reasons.append("seqcheck_missing_mismatch")
        else:
            reasons.append("seqcheck_counter_evidence_incomplete")
        if native.get("delay_storage_drop", 0):
            current["storage"] = _check("FAIL", max(current["storage"]["value"] or 0, native["delay_storage_drop"]))
            reasons.append("seqcheck_delay_storage_drop")
    return result
