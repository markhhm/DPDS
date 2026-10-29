# SPDX-License-Identifier: Apache-2.0
"""Default v1 streaming oracle: bounded active joins, no packet-history database."""
from collections import Counter
import hashlib

from .calibration import Calibration
from .counters import normalize_counters
from .custody import MODEL_REASONS
from .identity_index import IdentityIndex
from .live_custody import LiveCustody, key
from .online import OnlineCells, ReceiveOrder
from .protocol import StreamError, StreamReader
from .reference import Reference, canonical
from .registration import validate_registration
from .witnesses import Witnesses


def build_bundle_report(config, traces, manifest, sources, calibration, retention_dir=None):
    validate_registration(manifest, config)
    cells = OnlineCells(manifest)
    index = IdentityIndex(manifest)
    clock = Calibration(calibration, manifest["scope"], manifest["run"])
    reference = Reference(config, manifest["run"], traces)
    active, readers, heads = {}, {}, {}
    reasons, structural, anomalies = set(), set(), Counter()
    mismatch_records, invalid_cells, lost_active_ports = [], set(), set()
    witnesses = None
    peak, expected_digest, summary = 0, hashlib.sha256(), None
    wire = ReceiveOrder(manifest["capacity"]["max_unresolved"])
    order_high, flow_high = {}, {}
    limit = manifest["capacity"]["max_unresolved"]

    def affected(row):
        if row and "arrival_ns" in row and row.get("line") is not None:
            cell = cells._cell(row)
            return [cell] if cell else []
        port = str(row["ingress_port"]) if row and "ingress_port" in row else None
        return [cell for cell_key,cell in cells.cells.items() if port is None or cell_key[0]==port]

    def issue(code, status="INCONCLUSIVE", row=None):
        reasons.add(code)
        anomalies[code] += 1
        if status=="INVALID":
            structural.add(code)
        for cell in affected(row):
            if status=="INVALID":
                invalid_cells.add(id(cell))
            if status=="FAIL":
                cell["failures"][code] += 1
            else:
                cell["incomplete"].add(code)
        if status in {"FAIL", "INVALID"}:
            state = active.get(key(row)) if row and "external_identity" in row else None
            if len(mismatch_records)<cells.retention.cap:
                mismatch_records.append(dict(code=code, measurement_status=status,
                    record=row, expected=state["expected"] if state else None,
                    before=state["before"] if state else None,
                    receive=state["receive"] if state else None,
                    terminal=state["terminal"] if state else None))
            else:
                reasons.add("mismatch_cap_exhausted")
                for cell in affected(row):
                    cell["incomplete"].add("mismatch_cap_exhausted")

    custody = LiveCustody(config, traces, manifest, active, issue)

    def advance(token):
        reader = readers[token]
        try:
            heads[token] = next(reader)
        except StopIteration:
            heads.pop(token, None)
            registration = next(row for row in manifest["streams"] if row["stream_id"]==token)
            if reader.integrity["file_sha256"]!=registration["file_sha256"]:
                issue("SOURCE_HASH_MISMATCH", "INVALID")
        except StreamError as exc:
            heads.pop(token, None)
            issue(exc.code, exc.measurement_status)

    def retire(state):
        row = state["expected"]
        if state["before"] is None:
            return
        if row["outcome"]=="accepted":
            if state["receive"] is None or not state["tx"]:
                return
        elif state["terminal"] is None:
            return
        if not state["offer"] and row["outcome"]!="invalid_input" and state["terminal"] is None:
            return
        active.pop(key(row), None)

    def delivered(observation):
        fact = index.receive(observation, input_complete=all(p["ended"] for p in reference.ports.values()))
        label = fact["class"]
        state = active.get(key(observation))
        if label=="join_overflow" and state is not None and state["receive"] is None:
            # The active join still proves this occurrence; the index overflow remains latched.
            label = "delivery" if state["expected"]["outcome"]=="accepted" else "delivered_intended_drop"
        if label!="delivery":
            if label!="join_overflow":
                issue(label, "INVALID" if label=="receiver_unknown_identity" else "FAIL", state["expected"] if state else observation)
            return
        if state is None:
            issue("active_join_unavailable", row=observation)
            return
        row = state["expected"]
        for name in ("egress_port", "line", "length", "vlan_id", "flow_id", "app"):
            if observation[name]!=row[name]:
                issue("receiver_identity_mapping", "INVALID", row)
        if observation["selected_late"] is not None and observation["selected_late"]!=int(row["late"]):
            issue("probe_flag_mismatch", "FAIL", row)
        token = row["ingress_port"], row["line"]
        order = row["release_ns"], row["ingress_seq"]
        if order<order_high.get(token, (-1, -1)):
            issue("unexpected_order", "FAIL", row)
        order_high[token] = max(order, order_high.get(token, (-1, -1)))
        # Full flow diagnostics are capped with active state; no per-flow history grows silently.
        flow = row["ingress_port"], row["flow_id"], row["line"]
        if flow not in flow_high and len(flow_high)>=limit:
            issue("flow_order_capacity", row=row)
        else:
            previous = flow_high.get(flow, -1)
            if row["ingress_seq"]<previous:
                cell = cells._cell(row)
                if cell is not None:
                    cell["counts"]["actual_ooo"] += 1
                    if row.get("selected_late") is not None:
                        cell["counts"]["probe_actual_ooo"] += 1
                        cell["counts"]["probe_selected_late_ooo"] += row["late"]
            flow_high[flow] = max(previous, row["ingress_seq"])
        state["receive"] = observation
        pair(state)

    def pair(state):
        row, before, received = state["expected"], state["before"], state["receive"]
        if state["paired"] or received is None or before is None:
            return
        state["paired"] = True
        pair_data = None
        if received["timestamp"] is not None and before["timestamp"] is not None:
            try:
                pair_data = clock.pair(before, received)
                if pair_data["target_clock_id"]!=manifest["clocks"]["observation"][str(row["ingress_port"])]:
                    raise StreamError("CLOCK_PAIR_DOMAIN", "pair target differs from registered observation domain", "INCONCLUSIVE")
            except StreamError as exc:
                issue(exc.code, exc.measurement_status, row)
        expected_ns = None
        if manifest["timing_mode"]=="calibration":
            line = next(line for line in config["lines"] if line["name"]==row["line"])
            if line["trace"]:
                issue("trace_calibration_unavailable", row=row)
                expected_ns = row["release_ns"]-row["arrival_ns"]
            else:
                expected_ns = line["model"]["t_ns"]+row["late"]*line["model"]["delta_ns"]
        cells.delivery(row, pair_data["owd_ns"] if pair_data else None,
                       pair_data["uncertainty_ns"] if pair_data else 0, expected_ns,
                       pair_data["raw_owd_ns"] if pair_data else None)
        retire(state)

    try:
        for registration in manifest["streams"]:
            token = registration["stream_id"]
            reader = StreamReader(sources[token], registration["role"], manifest["run"])
            readers[token] = reader
            if reader.header["stream_id"]!=token:
                raise StreamError("STREAM_REGISTRATION", "stream ID differs from registration")
        witnesses = Witnesses(cells, retention_dir, readers)
        # Counter scopes and endpoints are bounded independently of high-volume packet streams.
        counter_readers = [reader for reader in readers.values() if reader.header["role"]=="counters"]
        def counter_rows():
            for reader in counter_readers:
                for row in reader:
                    witnesses.keep(row)
                    yield row
                registration = next(row for row in manifest["streams"] if row["stream_id"]==reader.header["stream_id"])
                if reader.integrity["file_sha256"]!=registration["file_sha256"]:
                    raise StreamError("SOURCE_HASH_MISMATCH", "counter bytes differ from registration")
        try:
            counters = normalize_counters(counter_rows(), manifest)
            for code in counters["reason_codes"]:
                issue(code, counters["measurement_status"])
        except StreamError as exc:
            counters = dict(ports={}, observer={}, reason_codes=[exc.code], measurement_status=exc.measurement_status)
            issue(exc.code, exc.measurement_status)
        expected_readers = [reader for reader in readers.values() if reader.header["role"]=="expected"]
        if len(expected_readers)!=1:
            raise StreamError("EXPECTED_PARTITION_UNSUPPORTED", "one ordered reference stream is required")
        expected_reader = expected_readers[0]
        for token, reader in readers.items():
            if reader.header["role"] not in {"counters", "expected"}:
                advance(token)
        while heads:
            progressed = False
            # Observation heads are joined as soon as ingress membership is known.
            for token, observation in list(heads.items()):
                role = readers[token].header["role"]
                if role not in {"before", "receiver"}:
                    continue
                if key(observation) not in index.entries and key(observation) not in active and not all(p["ended"] for p in reference.ports.values()):
                    continue
                if role=="before":
                    state = active.get(key(observation))
                    if state is None:
                        if observation["ingress_port"] in lost_active_ports:
                            issue("active_join_unavailable", row=observation)
                        else:
                            issue("before_unknown_or_duplicate", "INVALID", observation)
                    elif state["before"] is not None:
                        issue("duplicate_before", "INVALID", state["expected"])
                    else:
                        witnesses.keep(observation, state["expected"])
                        state["before"] = observation
                        for name in ("line", "egress_port", "length", "vlan_id", "flow_id", "app"):
                            if observation[name]!=state["expected"][name]:
                                issue("before_identity_mapping", "INVALID", state["expected"])
                        pair(state)
                        retire(state)
                else:
                    state = active.get(key(observation))
                    witnesses.keep(observation, state["expected"] if state else None)
                    try:
                        for ready in wire.push(observation):
                            delivered(ready)
                    except ValueError:
                        issue("receive_order_capacity" if len(wire.pending)>=wire.capacity else "receive_sequence_invalid")
                advance(token)
                progressed = True
            engine = [(token,row) for token,row in heads.items() if readers[token].header["role"] in {"ingress", "host"}]
            def when(item):
                token, row = item
                if row["kind"] in {"frontier", "end"}:
                    return (row.get("through_ns", 2**64), 0, token)
                return (row.get("arrival_ns", row.get("at_ns", row.get("gate_ns", 0))),
                        0 if row["kind"]=="packet" else 1, token)
            if engine:
                token, event = min(engine, key=when)
                role = readers[token].header["role"]
                if role=="ingress":
                    want = reference.consume(event)
                    port = event["ingress_port"]
                    if event["kind"]=="packet":
                        custody.input_next[port] += 1
                        try:
                            claimed = next(expected_reader)
                            expected_digest.update(canonical(claimed)+b"\n")
                            stripped = lambda row:{name:value for name,value in row.items() if name not in {"stream_id", "seq"}}
                            if stripped(claimed)!=stripped(want):
                                issue("reference_decision_mismatch", "INVALID", want)
                            witnesses.keep(claimed, want)
                        except StopIteration:
                            issue("reference_decision_missing", "INVALID", want)
                        cells.expect(want)
                        witnesses.keep(event, want)
                        if want["external_identity"] is None:
                            issue("identity_missing", row=want)
                        else:
                            fact = index.remember(want)
                            if fact["class"]=="duplicate_ingress":
                                issue("duplicate_ingress", "INVALID", want)
                            if len(active)>=limit:
                                lost_active_ports.add(want["ingress_port"])
                                issue("active_join_capacity", row=want)
                            else:
                                active[key(want)] = dict(expected=want, before=None, receive=None,
                                    offer=None, terminal=None, tx=False, dispatch=None, pending=False, paired=False)
                                peak = max(peak, len(active))
                    elif event["kind"]=="end":
                        custody.input_end.add(port)
                        witnesses.keep(event)
                    else:
                        custody.input_frontier[port] = event["through_ns"]
                        witnesses.keep(event)
                    advance(token)
                    # A validated next-arrival lower bound certifies the preceding empty interval.
                    later = heads.get(token)
                    if later and later["kind"]=="packet":
                        custody.input_frontier[port] = later["arrival_ns"]-1
                    elif later and later["kind"]=="end":
                        custody.input_frontier[port] = 2**64-1
                else:
                    at = event.get("at_ns", event.get("submit_after_ns"))
                    items = event.get("identities", event.get("offered", [event]))
                    for item in items:
                        state = active.get(key(item)) if "external_identity" in item else None
                        if state and at-state["expected"]["arrival_ns"]>manifest["capacity"]["max_join_lag_ns"]:
                            issue("join_lag_exceeded", row=state["expected"])
                    state = active.get(key(event)) if "external_identity" in event else None
                    witnesses.keep(event, state["expected"] if state else None, active)
                    custody.event(event)
                    for state in list(active.values()):
                        retire(state)
                    advance(token)
                progressed = True
            if not progressed:
                # All ingress heads have ended; classify remaining observation identities on complete membership.
                if all(p["ended"] for p in reference.ports.values()):
                    for token in list(heads):
                        observation = heads[token]
                        if readers[token].header["role"]=="receiver":
                            delivered(observation)
                        else:
                            issue("before_unknown_identity", "INVALID", observation)
                        advance(token)
                else:
                    issue("join_unresolved")
                    break
        for code in wire.finish():
            issue(code)
        try:
            summary = next(expected_reader)
            witnesses.keep(summary)
            own = reference.finish()
            if (summary["kind"]!="summary" or not summary["input_complete"]
                    or summary["event_count"]!=own["event_count"]
                    or summary["ingress_sha256"]!=own["ingress_sha256"]
                    or summary["config_sha256"]!=own["config_sha256"]
                    or summary["expected_sha256"]!=expected_digest.hexdigest()):
                issue("reference_summary_mismatch", "INVALID")
            for _ in expected_reader:
                issue("reference_after_summary", "INVALID")
            registration = next(row for row in manifest["streams"] if row["stream_id"]==expected_reader.header["stream_id"])
            if expected_reader.integrity["file_sha256"]!=registration["file_sha256"]:
                issue("SOURCE_HASH_MISMATCH", "INVALID")
        except (StopIteration, StreamError, ValueError) as exc:
            issue(getattr(exc, "code", "reference_incomplete"), getattr(exc, "measurement_status", "INCONCLUSIVE"))
        for port, values in counters["ports"].items():
            count = reference.ports[int(port)]["next_seq"]
            if values.get("rx_returned")!=count:
                issue("counter_ingress_mismatch")
            for reason in (*MODEL_REASONS.values(), "delay_storage_drop", "handoff_drop", "terminal_tx_drop",
                           "missed_deadline_outage_drop", "shutdown_drop", "tx_accepted"):
                if values.get(reason)!=custody.port_counts[int(port), reason]:
                    issue("counter_terminal_mismatch", row=dict(ingress_port=int(port)))
        for reader in readers.values():
            if reader.header["role"] not in {"expected", "counters"}:
                health = counters["observer"].get(reader.header["stream_id"], {})
                if health.get("records_emitted")!=reader.integrity.get("record_count"):
                    issue("observer_stream_count_mismatch")
        complete = not any(cell["incomplete"] for cell in cells.cells.values()) and not structural
        for state in active.values():
            row = state["expected"]
            if state["before"] is None:
                issue("before_missing", row=row)
                complete = False
            if not state["offer"] and row["outcome"]!="invalid_input":
                if state["terminal"] and state["terminal"]["reason"] not in MODEL_REASONS.values():
                    issue("pre_offer_loss", "FAIL", row)
                else:
                    issue("missing_offer", row=row)
            if row["outcome"]=="accepted":
                if not state["tx"] and state["terminal"] is None:
                    issue("missing_tx_evidence", row=row)
                if state["receive"] is None:
                    terminal = state["terminal"]
                    cells.loss(row, "delay_storage_drop" if terminal and terminal["reason"]=="delay_storage_drop" else "missing_delivery", complete)
            elif state["terminal"] is None:
                issue("missing_terminal", row=row)
    except StreamError as exc:
        issue(exc.code, exc.measurement_status)
    finally:
        identity_report = index.finish()
        for reader in readers.values():
            reader._source.close()
        if witnesses is not None:
            witnesses.finish(readers)
        active.clear()
    result = cells.finish(identity_report)
    for row in result["rows"]:
        local = cells.cells[row["direction"], row["line"], row["phase"]]
        if id(local) in invalid_cells:
            row.update(status="INVALID", measurement_status="INVALID")
        row.update(g2_pass=all(gate["status"] in {"PASS", "not_applicable"} for gate in row["gates"].values()),
                   error_median_ms=None if row["timing"]["main"]["median_abs_error_ns"] is None else row["timing"]["main"]["median_abs_error_ns"]/1e6,
                   error_p99_ms=None if row["timing"]["main"]["p99_abs_error_ns"] is None else row["timing"]["main"]["p99_abs_error_ns"]/1e6)
        if row["repeat"]=="pooled":
            row["reason_codes"].append("pooled_repeat_evidence_unavailable")
            row.update(status="INVALID", measurement_status="INCONCLUSIVE")
    required = [row for row in result["rows"] if row["required"]]
    status = "FAIL" if any(row["status"]=="FAIL" for row in required) else "INVALID" if any(row["status"]!="PASS" for row in required) else "PASS"
    result.update(schema="hng-g2-report-v1", run=manifest["run"], condition_id=manifest["condition_id"],
        status=status, measurement_status="INCONCLUSIVE" if status=="INVALID" and not structural else status,
        reason_codes=sorted(reasons | structural), observed_failures=dict(anomalies),
        sources=dict(config_sha256=manifest["config"]["sha256"], producer_versions=manifest["producer_versions"]),
        stage1_source_commit=manifest["stage1_source_commit"], stage1_source_review=manifest["stage1_source_review"],
        directions={port:dict(required_cells=sum(row["direction"]==port for row in required)) for port in {row["direction"] for row in required}},
        stream_integrity=[reader.integrity for reader in readers.values()],
        observer_cost=dict(active_join_limit=limit, active_join_peak=peak, active_join_live=0,
                           processing_qualified=False, storage_qualified=False),
        registration=manifest, counters=locals().get("counters"),
        mismatches=dict(cap=cells.retention.cap, records=mismatch_records,
                        counts={code:count for code,count in anomalies.items() if code not in {"mismatch_cap_exhausted"}}),
        unavailable=["hardware_qualification", "R_valid", "pooled_repeat_evidence"])
    if witnesses is not None:
        witnesses.online(result)
    return result
