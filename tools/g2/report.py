# SPDX-License-Identifier: Apache-2.0
"""Draft software G2 gate reporter. Full receiver identities; separately sampled timing."""
import argparse
import csv
from collections import Counter, defaultdict
from functools import lru_cache
from fractions import Fraction
import hashlib
import json
import math
from pathlib import Path
import re
import sys

from .reference import Reference, canonical, load_config, strict_json, uint
from .protocol import StreamError, StreamReader

DROP_FIELDS = ("q_drop", "trace_loss_drop", "trace_outage_drop", "delay_storage_drop",
               "invalid_input_drop", "handoff_drop", "terminal_tx_drop",
               "missed_deadline_outage_drop", "shutdown_drop")
STAGE1_SHA = "de035a63f5d0135ee70b0bceb27d5051897b85a1"
CSV_COLUMNS = ("condition_id", "repeat", "direction", "line", "phase", "status", "measurement_status",
    "g2_pass", "g2_qualified", "error_median_ms", "error_p99_ms", "delay_storage_drops", "capture_drops",
    "nic_drops", "shared_q_status", "shared_q_drops", "trace_loss_drops", "trace_outage_drops", "probe_sent",
    "probe_received", "probe_lost", "probe_selected_late", "probe_actual_ooo", "probe_selected_late_ooo",
    "probe_unexpected_ooo", "timing_samples", "reasons")


def write_outputs(result, destination):
    (destination / "report.json").write_text(json.dumps(result, allow_nan=False, indent=2) + "\n")
    with (destination / "results.csv").open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        for row in result.get("rows", []):
            values = {name:row.get(name) for name in CSV_COLUMNS}
            values["reasons"] = ";".join(row.get("reason_codes", [])) if "reason_codes" in row else row.get("reasons", "")
            writer.writerow(values)
    rows = [row for row in result.get("rows", []) if row.get("required", True)]
    lines = [f"{result.get('scope', 'unknown')}: {result['status']}",
             f"Passed cells: {sum(row['status']=='PASS' for row in rows)}/{len(rows)}",
             "Thresholds: median 100000 ns; p99 500000 ns; order 0; storage 0",
             "Report: report.json"]
    lines += [f"{row.get('direction')} {row.get('line')} {row.get('phase')}: {row['status']} "
              + ";".join(row.get("reason_codes", [])[:3]) for row in rows if row["status"]!="PASS"]
    lines += result.get("reason_codes", [])[:3]
    (destination / "summary.txt").write_text("\n".join(lines) + "\n")


def error_report(exc, manifest):
    code = getattr(exc, "code", "REPORT_INVALID")
    state = getattr(exc, "measurement_status", "INVALID")
    rows = []
    for cell in manifest.get("required_cells", []) if isinstance(manifest.get("required_cells", []), list) else []:
        if isinstance(cell, dict):
            rows.append({**{name:cell.get(name) for name in ("condition_id","repeat","direction","line","phase")},
                         "required":cell.get("required",True),"status":"INVALID","measurement_status":state,
                         "g2_pass":False,"g2_qualified":False,"reason_codes":[code]})
    return dict(schema="hng-g2-report-v1",run=manifest.get("run"),condition_id=manifest.get("condition_id"),
        status="INVALID",measurement_status=state,scope=manifest.get("scope","software-only"),g2_qualified=False,
        reason_codes=[code],reason=str(exc),rows=rows,sources={},directions={},stream_integrity=[],
        observer_cost=dict(status="INCONCLUSIVE", warnings=[dict(observer=None, code="observer_cost_unavailable")],
                           end_backlog={}),
        registration=manifest,stage1_source_commit=manifest.get("stage1_source_commit"),
        stage1_source_review=manifest.get("stage1_source_review"),unavailable=["handled_setup_or_schema_error"])


def identity(row):
    run, port, external = row["run"], row["ingress_port"], row["external_identity"]
    uint(port, "identity ingress port", 65535)
    if not isinstance(run, str) or not run or not isinstance(external, str) or not external:
        raise ValueError("invalid external identity")
    return run, port, external


def timing_sample(row, seed):
    """Separate hash domain from late selection; exactly 10 low hash bits are zero."""
    uint(seed, "sampling seed")
    data = b"hng-g2-timing-v1\0" + seed.to_bytes(8, "big") + canonical(identity(row))
    return int.from_bytes(hashlib.sha256(data).digest()[:8], "big") & 1023 == 0


@lru_cache(maxsize=256)
def quantile_ranks(n, p):
    # Equal-tail binomial order-statistic interval, 95%; beyond observations is unbounded.
    cdf, lower, upper = 0.0, None, n + 1
    for k in range(n + 1):
        log_mass = (math.lgamma(n + 1) - math.lgamma(k + 1) - math.lgamma(n - k + 1)
                    + k * math.log(p) + (n - k) * math.log1p(-p))
        cdf += math.exp(log_mass)
        if lower is None and cdf >= 0.025:
            lower = k
        if cdf >= 0.975:
            upper = k + 1
            break
    return lower or 0, upper


def timing_stats(errors):
    ordered = sorted(errors)
    result = dict(n=len(ordered), confidence=0.95, ci_method="binomial order statistics; iid sampling assumption")
    for name, p in (("median", 0.5), ("p99", 0.99)):
        if not ordered:
            point, interval = None, [0, None]
        else:
            position = (len(ordered) - 1) * p
            low_index, high_index = math.floor(position), math.ceil(position)
            point = ordered[low_index] + (ordered[high_index] - ordered[low_index]) * (position - low_index)
            low, high = quantile_ranks(len(ordered), p)
            interval = [ordered[low - 1] if low else 0,
                        ordered[high - 1] if high <= len(ordered) else None]
        result[name + "_abs_error_ns"] = point
        result[name + "_ci_ns"] = interval
    return result


def timing_gate(stats, name, threshold, uncertainty):
    low, high = stats[name + "_ci_ns"]
    low = max(0, low - uncertainty)
    high = None if high is None else high + uncertainty
    status = "INCONCLUSIVE"
    if low > threshold:
        status = "FAIL"
    elif high is not None and high <= threshold:
        status = "PASS"
    return dict(status=status, value_ns=stats[name + "_abs_error_ns"], threshold_ns=threshold,
                ci_with_clock_uncertainty_ns=[low, high])


def build_report(expected, summary, received, counters, config, manifest, traces=None):
    if not all(isinstance(value, dict) for value in (summary, counters, config, manifest)):
        raise ValueError("summary, counters, config and manifest must be JSON objects")
    if counters.get("schema") == "hng-g2-counters-v1":
        if counters.get("run") != summary.get("run"):
            raise StreamError("COUNTER_RUN", "normalized counters have a mixed run")
        if counters.get("measurement_status") != "PASS":
            raise StreamError("COUNTER_EVIDENCE_INCOMPLETE", "normalized counter evidence is incomplete",
                              "INCONCLUSIVE" if counters.get("measurement_status") == "INCONCLUSIVE" else "INVALID")
        counters = counters["ports"]
        if not isinstance(counters, dict):
            raise ValueError("normalized counter ports must be an object")
    if (summary.get("schema") not in {"hng-g2-reference-draft-v1", "hng-g2-reference-v1"} or summary.get("kind") != "summary"
            or not isinstance(summary.get("run"), str) or not summary["run"]):
        raise ValueError("unsupported reference summary schema/kind/run")
    uint(summary["event_count"], "event count")
    for name in ("ingress_sha256", "config_sha256", "expected_sha256"):
        if not isinstance(summary.get(name), str) or not re.fullmatch(r"[0-9a-f]{64}", summary[name]):
            raise ValueError(f"invalid summary {name}")
    digest = hashlib.sha256()
    for row in expected:
        if not isinstance(row, dict) or row.get("kind") != "expected" or row.get("run") != summary["run"]:
            raise ValueError("unsupported expected row kind/run")
        digest.update(canonical(row) + b"\n")
    if digest.hexdigest() != summary["expected_sha256"]:
        raise ValueError("expected row hash/seal mismatch")
    if manifest["scope"] != "software-only":
        raise ValueError("draft M2 tools accept software-only evidence; hardware observer is pending")
    if manifest["timing_mode"] not in {"stress", "calibration"}:
        raise ValueError("unsupported timing mode")
    seed = uint(manifest["sampling_seed"], "sampling seed")
    clock = manifest["clock"]
    uncertainty = uint(clock["uncertainty_ns"], "clock uncertainty")
    if type(clock["common"]) is not bool:
        raise ValueError("clock common must be boolean")
    reasons = []
    if not clock["common"]:
        reasons.append("ingress/receiver clocks lack a common mapping")
    if summary.get("input_complete") is not True or summary.get("event_count") != len(expected):
        reasons.append("incomplete ingress/end evidence")
    if summary["config_sha256"] != hashlib.sha256(canonical(config)).hexdigest():
        raise ValueError("reference/config hash mismatch")
    Reference(config, summary["run"], traces)
    lines = {line["name"]: line for line in config["lines"]}
    # Configuration and trace validation is owned by the independent reference.
    expected_by_id = {}
    for row in expected:
        key = identity(row)
        if key in expected_by_id:
            raise ValueError("duplicate ingress identity")
        if row["outcome"] not in {"accepted", "q_drop", "trace_loss", "trace_outage"}:
            raise ValueError("unsupported reference outcome")
        line = lines[row["line"]]
        if row["ingress_port"] != line["ingress_port"] or row["egress_port"] != line["egress_port"]:
            raise ValueError("reference line/port mismatch")
        uint(row["arrival_ns"], "arrival")
        uint(row["q_bytes"], "Q occupancy")
        uint(row["q_drain_ns"], "Q drain time")
        if row["outcome"] == "accepted":
            for field in ("deadline_ns", "release_ns", "wire_bytes", "ingress_seq"):
                uint(row[field], field)
        expected_by_id[key] = row
    received=list(received)
    if any("receive_seq" in row for row in received):
        if not all("receive_seq" in row for row in received):
            raise ValueError("mixed receiver wire-order evidence")
        def wire_key(row):
            uint(row["receive_seq"],"receiver wire sequence")
            want=expected_by_id.get(identity(row))
            return row.get("egress_port",want["egress_port"] if want else 65535),row["receive_seq"]
        received.sort(key=wire_key)
        last={}
        for row in received:
            port,seq=wire_key(row)
            if seq<=last.get(port,-1):
                raise ValueError("duplicate receiver wire sequence")
            if seq!=last.get(port,-1)+1:
                reasons.append("receiver_order_gap")
            last[port]=seq
    seen, duplicates, unknown = {}, [], []
    observed_order = defaultdict(list)
    for row in received:
        key = identity(row)
        if row.get("receive_ns") is not None:
            uint(row["receive_ns"], "receiver timestamp")
        if key in seen:
            duplicates.append(key)
        seen[key] = row
        want = expected_by_id.get(key)
        if want is None or want["outcome"] != "accepted":
            unknown.append(key)
        else:
            observed_order[want["egress_port"]].append(key)
            if row.get("receive_ns") is not None and row["receive_ns"] + uncertainty < want["arrival_ns"]:
                reasons.append("negative OWD beyond clock uncertainty; invalid pairing/clock evidence")
    if any(key[1] not in {line["ingress_port"] for line in lines.values()} for key in unknown):
        reasons.append("receiver evidence contains an undeclared ingress direction")
    structural_unknown=any(key not in expected_by_id for key in unknown)

    serialized, late_collisions = {}, {}
    for egress in {line["egress_port"] for line in lines.values()}:
        rate = uint(manifest["egress_bps"][str(egress)], "egress rate")
        if not rate:
            raise ValueError("egress rate must be positive")
        tail, late_busy = Fraction(0), False
        survivors = sorted((row for row in expected if row["outcome"] == "accepted"
                            and row["egress_port"] == egress),
                           key=lambda row: (row["release_ns"], row["ingress_seq"]))
        # Fraction avoids adding a ceiling for every small serialization interval.
        for row in survivors:
            release = row["release_ns"]
            if tail <= release:
                late_busy = False
            start = max(release, tail)
            wait = Fraction(start - release)
            extra = -(-wait.numerator // wait.denominator)
            key = identity(row)
            serialized[key] = extra
            late_collisions[key] = extra if late_busy or row["late"] else 0
            late_busy = late_busy or row["late"]
            tail = start + Fraction(row["wire_bytes"] * 8 * 10**9, rate)

    directions, flat_rows = {}, []
    for port in sorted({line["ingress_port"] for line in lines.values()}):
        local_reasons = []
        rows = [row for row in expected if row["ingress_port"] == port]
        survivors = [row for row in rows if row["outcome"] == "accepted"]
        terminal = counters.get(str(port), {})
        if not isinstance(terminal, dict):
            raise ValueError("direction counters must be a JSON object")
        required = (*DROP_FIELDS, "rx_returned", "tx_accepted", "still_owned", "observer_drop_packets")
        for field in required:
            if terminal.get(field) is None:
                local_reasons.append(f"unknown counter: {field}")
            else:
                uint(terminal[field], field)
        if terminal.get("observer_drop_packets"):
            local_reasons.append("observer dropped records")
        if terminal.get("still_owned"):
            local_reasons.append("run has not drained")
        nic_loss = terminal.get("nic_drops")
        if nic_loss is not None:
            uint(nic_loss, "NIC drops")
        if terminal.get("rx_returned") != len(rows):
            local_reasons.append("RX/input event count mismatch")
        if all(terminal.get(field) is not None for field in (*DROP_FIELDS, "tx_accepted", "still_owned", "rx_returned")):
            if terminal["rx_returned"] != sum(terminal[field] for field in DROP_FIELDS) + terminal["tx_accepted"] + terminal["still_owned"]:
                local_reasons.append("terminal software counters do not conserve packets")
        model_counts = Counter(row["outcome"] for row in rows)
        for outcome, counter in (("q_drop", "q_drop"), ("trace_loss", "trace_loss_drop"), ("trace_outage", "trace_outage_drop")):
            if terminal.get(counter) != model_counts[outcome]:
                local_reasons.append(f"model/counter mismatch: {counter}")
        missing = [identity(row) for row in survivors if identity(row) not in seen]
        non_model_drop = sum(terminal.get(field) or 0 for field in DROP_FIELDS[3:])
        if len(missing) != non_model_drop:
            local_reasons.append("receiver survivor gap is not explained by terminal data-path loss")
        if len(survivors) - len(missing) != terminal.get("tx_accepted"):
            local_reasons.append("receiver/TX accepted mismatch")

        order_errors = sum(key[1] == port for key in duplicates + unknown)
        if any(key[1] == port and key not in expected_by_id for key in unknown):
            local_reasons.append("unmatched receiver identity")
        actual_ooo = selected_late_ooo = flag_mismatches = 0
        highest = {}
        for observation in received:
            key = identity(observation)
            row = expected_by_id.get(key)
            if key[1] != port or row is None or row["outcome"] != "accepted":
                continue
            line = row["line"]
            ooo = row["ingress_seq"] < highest.get(line, -1)
            actual_ooo += ooo
            selected_late_ooo += ooo and row["late"]
            highest[line] = max(row["ingress_seq"], highest.get(line, -1))
            if "selected_late" in observation:
                flag = observation["selected_late"]
                if type(flag) not in (bool, int) or flag not in (0, 1):
                    raise ValueError("invalid probe selected-late flag")
                flag_mismatches += flag != row["late"]
        if flag_mismatches:
            local_reasons.append("probe selected-late flag changed")
        for egress in {row["egress_port"] for row in survivors}:
            want = sorted((row for row in survivors if row["egress_port"] == egress and identity(row) in seen),
                          key=lambda row: (row["release_ns"], row["ingress_seq"]))
            actual = [key for key in observed_order[egress] if key[1] == port]
            # shortcut: TX attempt evidence is pending; enable cross-line trace verdicts after observer spec.
            gated = [line for line in lines.values() if line["egress_port"] == egress]
            if len(gated) > 1 and any(line["trace"] is not None for line in gated):
                local_reasons.append("cross-line trace arbitration needs observer attempt evidence")
                for line_name in {row["line"] for row in want}:
                    keys = [identity(row) for row in want if row["line"] == line_name]
                    actual_keys = [key for key in actual if expected_by_id[key]["line"] == line_name]
                    order_errors += sum(a != b for a, b in zip(actual_keys, keys))
            else:
                order_errors += sum(a != b for a, b in zip(actual, [identity(row) for row in want]))

        errors = dict(main=[], late_extra=[], app_extra=[])
        main_serialization, main_residual = [], []
        missing_timing = 0
        for row in survivors:
            key = identity(row)
            if key not in seen:
                continue
            main = timing_sample(row, seed)
            late = row["late"] and not main
            app = row.get("app") in manifest["oversample_apps"] and not main
            if not (main or late or app):
                continue
            measured = seen[key].get("receive_ns")
            if measured is None:
                missing_timing += 1
                continue
            line = lines[row["line"]]
            if manifest["timing_mode"] == "stress" or line["trace"] is not None:
                target = row["release_ns"] if line["trace"] is not None else row["deadline_ns"]
                predicted = target - row["arrival_ns"]
            else:
                model = line["model"]
                predicted = model["t_ns"] + row["late"] * model["delta_ns"]
            error = abs(measured - row["arrival_ns"] - predicted)
            if main:
                main_serialization.append(serialized[key])
                main_residual.append(abs(measured - row["arrival_ns"] - predicted - serialized[key]))
            for group, included in (("main", main), ("late_extra", late), ("app_extra", app)):
                if included:
                    errors[group].append(error)
        if missing_timing:
            local_reasons.append("missing selected timing pairs")
        timing = {name: timing_stats(values) for name, values in errors.items()}
        gates = dict(median=timing_gate(timing["main"], "median", 100_000, uncertainty),
                     p99=timing_gate(timing["main"], "p99", 500_000, uncertainty),
                     order=dict(status="FAIL" if order_errors else "PASS", mismatched_positions=order_errors),
                     storage=dict(status="INCONCLUSIVE" if terminal.get("delay_storage_drop") is None else
                                  "FAIL" if terminal["delay_storage_drop"] else "PASS",
                                  drops=terminal.get("delay_storage_drop")))
        q = {}
        for name, line in lines.items():
            if line["ingress_port"] != port:
                continue
            peak = max((row["q_bytes"] for row in rows if row["line"] == name), default=0)
            drain = max((row["q_drain_ns"] for row in rows if row["line"] == name), default=0)
            q[name] = dict(peak_l2_bytes=peak, configured_l2_bytes=line["model"]["q_bytes"],
                          peak_drain_ns_at_c=drain,
                          normalized_l2_ns=peak * 8 * 10**9 / line["model"]["c_bps"],
                          convention="Q: L2 incl VLAN/padding/FCS; drain: remaining exact wire service at C")
        collisions = [late_collisions[identity(row)] for row in survivors]
        statuses = [gate["status"] for gate in gates.values()]
        status = "INVALID" if structural_unknown else "INCONCLUSIVE" if reasons or local_reasons else "FAIL" if non_model_drop or nic_loss or "FAIL" in statuses else "INCONCLUSIVE" if "INCONCLUSIVE" in statuses else "PASS"
        directions[str(port)] = dict(status=status, gates=gates, timing=timing, q=q,
            counters=terminal, reasons=local_reasons, missing_survivors=len(missing),
            sample_design=dict(algorithm="sha256-hng-g2-timing-v1", seed=seed, rate="1/1024",
                               late_extra="all late outside main", app_extra=manifest["oversample_apps"],
                               extra_strata_may_overlap=True),
            serialization=dict(late_collision_packets=sum(value > 0 for value in collisions),
                               late_collision_max_ns=max(collisions, default=0),
                               total_serialization_max_ns=max((serialized[identity(row)] for row in survivors), default=0),
                               main_expected_serialization_ns=timing_stats(main_serialization),
                               main_error_after_serialization_ns=timing_stats(main_residual),
                               gate_uses_original_deadline=True, timestamp_convention="egress first bit"))
        main = timing["main"]
        row_reasons = list(reasons) + local_reasons + [name + "_" + gate["status"].lower()
                       for name, gate in gates.items() if gate["status"] != "PASS"]
        if nic_loss:
            row_reasons.append("nic_drops_nonzero")
        flat_rows.append(dict(condition_id=summary.get("run", "software-fixture"), direction=str(port),
            status="INVALID" if status == "INCONCLUSIVE" else status,
            measurement_status=status, g2_pass=status == "PASS", g2_qualified=False,
            error_median_ms=None if main["median_abs_error_ns"] is None else main["median_abs_error_ns"] / 10**6,
            error_p99_ms=None if main["p99_abs_error_ns"] is None else main["p99_abs_error_ns"] / 10**6,
            delay_storage_drops=terminal.get("delay_storage_drop"), capture_drops=terminal.get("observer_drop_packets"),
            nic_drops=nic_loss, nic_status="not_applicable_software" if nic_loss is None else "measured",
            shared_q_status="measured", shared_q_drops=terminal.get("q_drop"),
            probe_sent=len(rows), probe_received=len(survivors)-len(missing),
            probe_lost=len(rows)-len(survivors)+len(missing), probe_flag_mismatches=flag_mismatches,
            probe_selected_late=sum(row["late"] for row in rows),
            probe_selected_late_received=sum(row["late"] for row in survivors if identity(row) in seen),
            probe_actual_ooo=actual_ooo, probe_selected_late_ooo=selected_late_ooo,
            probe_unexpected_ooo=actual_ooo-selected_late_ooo,
            timing_samples=main["n"], reasons=";".join(row_reasons)))
    statuses = [direction["status"] for direction in directions.values()]
    status = "FAIL" if "FAIL" in statuses else "INVALID" if "INVALID" in statuses else "INCONCLUSIVE" if "INCONCLUSIVE" in statuses or reasons else "PASS"
    return dict(schema="hng-g2-report-draft-v1", status=status, scope="software-only, not G2",
                g2_qualified=False, reasons=reasons, directions=directions,
                ingress_summary=summary, timing_mode=manifest["timing_mode"],
                stage1_source_commit=STAGE1_SHA, stage1_source_review="not second-reviewed",
                rows=flat_rows,
                unavailable=["hardware observer",
                             "actual-time cross-line gate checker", "NIC xstats and wire clock calibration"])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for argument in ("config", "expected_jsonl", "receiver_jsonl", "counters_json", "manifest_json"):
        parser.add_argument(argument)
    parser.add_argument("--out", help="new directory for report.json, results.csv and summary.txt")
    args = parser.parse_args()
    destination = None
    manifest = {}
    counter_view = None
    try:
        if args.out:
            destination = Path(args.out)
            destination.mkdir(parents=True, exist_ok=False)
        with open(args.manifest_json) as source:
            manifest = strict_json(source.read())
        if not isinstance(manifest,dict):
            manifest = {}
            raise ValueError("manifest must be an object")
        config, traces = load_config(args.config)
        if manifest.get("schema")=="hng-g2-manifest-v1":
            from .bundle import build_bundle_report
            from .validate import preflight
            checked_sources = preflight(args.manifest_json)
            if hashlib.sha256(Path(args.config).read_bytes()).hexdigest()!=manifest["config"]["sha256"]:
                raise StreamError("SOURCE_HASH_MISMATCH", "positional config differs from registration")
            root = Path(args.manifest_json).parent
            paths = {stream["stream_id"]:root/stream["path"] for stream in manifest["streams"]}
            for role, path in (("expected", args.expected_jsonl), ("receiver", args.receiver_jsonl),
                               ("counters", args.counters_json)):
                streams = [stream for stream in manifest["streams"] if stream["role"]==role]
                if len(streams)!=1:
                    raise StreamError("POSITIONAL_PARTITION_UNSUPPORTED", "positional stream requires one registered partition")
                if role=="counters":
                    with open(path) as source:
                        try:
                            first = strict_json(source.readline())
                        except ValueError:
                            first = None
                    if not isinstance(first,dict) or first.get("kind")!="header":
                        counter_view = strict_json(Path(path).read_bytes())
                        if not isinstance(counter_view,dict) or counter_view.get("schema")!="hng-g2-counters-v1":
                            raise StreamError("COUNTER_VIEW_SCHEMA", "counter positional requires full snapshots or the normalized v1 view")
                        continue
                paths[streams[0]["stream_id"]] = path
            result = build_bundle_report(config, traces, manifest, paths,
                strict_json((root/manifest["calibration_path"]).read_bytes()), destination, registration_root=root)
            result["sources"].update(checked_sources["sources"])
            if counter_view is not None:
                actual = result.get("counters")
                fields = ("schema","run","scope","measurement_status","ports","observer","diagnostics", "descriptors","endpoints","reason_codes")
                if actual is None or any(counter_view.get(name)!=actual.get(name) for name in fields):
                    raise StreamError("COUNTER_VIEW_MISMATCH", "normalized counter view differs from sealed snapshots")
                actual_streams = [row for row in result["stream_integrity"] if row.get("role")=="counters"]
                claimed = {row["stream_id"]:row for row in counter_view.get("streams",[])}
                if set(claimed)!={row["stream_id"] for row in actual_streams} or any(
                        claimed[row["stream_id"]].get("file_sha256")!=row["file_sha256"] for row in actual_streams):
                    raise StreamError("COUNTER_VIEW_MISMATCH", "normalized counter view lacks matching source hashes")
                result["sources"]["counter_view_sha256"] = hashlib.sha256(Path(args.counters_json).read_bytes()).hexdigest()
        else:
            # Legacy draft input remains restricted to explicit software fixtures.
            with open(args.expected_jsonl) as source:
                first = strict_json(source.readline())
                rows = ([first]+[strict_json(line) for line in source] if first.get("kind")!="header"
                        else list(StreamReader(args.expected_jsonl, role="expected")))
            if not rows or rows[-1].get("kind")!="summary":
                raise ValueError("reference stream lacks terminal summary")
            with open(args.receiver_jsonl) as source:
                received = [strict_json(line) for line in source]
            with open(args.counters_json) as source:
                counters = strict_json(source.read())
            result = build_report(rows[:-1], rows[-1], received, counters, config, manifest, traces)
    except (ValueError, KeyError, TypeError, OSError) as exc:
        result = error_report(exc, manifest)
        if isinstance(exc, FileExistsError):
            destination = None
    if destination is not None:
        write_outputs(result, destination)
    print(json.dumps(result, allow_nan=False, sort_keys=True))
    return {"PASS": 0, "FAIL": 1, "INVALID": 2, "INCONCLUSIVE": 2}[result["status"]]


if __name__ == "__main__":
    sys.exit(main())
