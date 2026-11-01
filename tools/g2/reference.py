# SPDX-License-Identifier: Apache-2.0
"""Independent event-time Fraction oracle; no Rust imports or computed deadlines as input."""
import argparse
from bisect import bisect_right
from collections import deque
from fractions import Fraction
import hashlib
import json
from pathlib import Path
import re
import sys

U64 = (1 << 64) - 1


def fields(value, required, optional=()):
    if not isinstance(value, dict) or set(required) - value.keys() or value.keys() - set(required) - set(optional):
        raise ValueError(f"unsupported/missing fields; expected {required}, optional {optional}")


def uint(value, name, maximum=U64):
    if type(value) is not int or not 0 <= value <= maximum:
        raise ValueError(f"{name} must be an unsigned integer <= {maximum}")
    return value


def probability(value):
    if not isinstance(value, str) or not re.fullmatch(r"[01](?:\.[0-9]{1,18})?", value):
        raise ValueError("probability must be a decimal string, <=18 fractional digits")
    result = Fraction(value)
    if result > 1:
        raise ValueError("probability exceeds 1")
    return result


def selection_word(seed, ordinal):
    word = (seed + (ordinal + 1) * 0x9e3779b97f4a7c15) & U64
    word = ((word ^ (word >> 30)) * 0xbf58476d1ce4e5b9) & U64
    word = ((word ^ (word >> 27)) * 0x94d049bb133111eb) & U64
    return word ^ (word >> 31)


def selected(chance, seed, ordinal):
    return chance == 1 or selection_word(seed, ordinal) < int(chance * (1 << 64))


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode()


def strict_json(text):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError(f"duplicate JSON key {key}")
            result[key] = value
        return result

    def invalid(value):
        raise ValueError(f"nonfinite JSON value {value}")
    return json.loads(text, object_pairs_hook=pairs, parse_constant=invalid)


def trace_reference(value):
    fields(value, ("path", "sha256"))
    if (not isinstance(value["path"], str) or not value["path"]
            or not isinstance(value["sha256"], str)
            or not re.fullmatch(r"[0-9a-fA-F]{64}", value["sha256"])):
        raise ValueError("trace path/hash must be nonempty path and SHA-256 strings")


def load_config(path, *, contents=None):
    path = Path(path)
    config = strict_json(path.read_bytes() if contents is None else contents)
    fields(config, ("schema", "lines"))
    if not isinstance(config["lines"], list):
        raise ValueError("lines must be an array")
    traces = {}
    for line in config["lines"]:
        fields(line, ("name", "ingress_port", "egress_port", "vlan_id", "model", "envelope", "trace"))
        if line["trace"] is None:
            continue
        trace_reference(line["trace"])
        data = (path.parent / line["trace"]["path"]).read_bytes()
        if hashlib.sha256(data).hexdigest() != line["trace"]["sha256"].lower():
            raise ValueError("trace SHA-256 mismatch")
        doc = strict_json(data)
        fields(doc, ("schema", "source_id", "direction", "units", "conversion", "source_sha256",
                     "splice_policy", "trace_epoch_ns", "time_scale", "profile"))
        if (any(not isinstance(doc[key], str) or not doc[key]
                for key in ("source_id", "direction", "conversion", "source_sha256", "splice_policy"))
                or uint(doc["trace_epoch_ns"], "trace epoch") != 0
                or uint(doc["time_scale"], "trace scale") != 1):
            raise ValueError("invalid normalized trace metadata types/epoch/scale")
        if (doc["schema"] != "hng-trace-v1" or doc["units"] != "ns_owd"
                or doc["trace_epoch_ns"] != 0 or doc["time_scale"] != 1
                or not doc["source_id"] or not doc["direction"]
                or not re.fullmatch(r"[0-9a-fA-F]{64}", doc["source_sha256"])
                or not doc["conversion"]
                or doc["splice_policy"] not in {"natural_hold", "paired_splice", "unspliced_phases"}):
            raise ValueError("invalid normalized trace provenance/units")
        traces[line["name"]] = doc["profile"]
    return config, traces


class Trace:
    def __init__(self, profile):
        fields(profile, ("end_ns", "loss_seed", "rows", "outage_action", "terminal", "terminal_horizon_ns"))
        self.profile = profile
        self.end = uint(profile["end_ns"], "trace end")
        self.seed = uint(profile["loss_seed"], "loss seed")
        self.rows = profile["rows"]
        if not isinstance(self.rows, list):
            raise ValueError("trace rows must be an array")
        for row in self.rows:
            fields(row, ("at_ns", "t_ns", "loss", "outage"))
        self.starts = [uint(row["at_ns"], "trace at") for row in self.rows]
        if (not self.starts or self.starts[0] != 0 or self.starts[-1] >= self.end
                or any(a >= b for a, b in zip(self.starts, self.starts[1:]))):
            raise ValueError("trace must increase from zero through a finite end")
        self.losses = [probability(row["loss"]) for row in self.rows]
        for row in self.rows:
            uint(row["t_ns"], "trace delay", U64 - self.end)
            if type(row["outage"]) is not bool:
                raise ValueError("outage must be boolean")
        if profile["outage_action"] not in {"hold", "drop"}:
            raise ValueError("unsupported outage action")
        if profile["terminal"] == "hold_last_sample":
            self.end = uint(profile["terminal_horizon_ns"], "trace horizon")
            if self.end <= profile["end_ns"] or self.rows[-1]["outage"]:
                raise ValueError("terminal HOLD needs a longer horizon and an up last row")
        elif profile["terminal"] != "error" or profile["terminal_horizon_ns"] is not None:
            raise ValueError("invalid trace terminal policy")
        if profile["outage_action"] == "hold" and self.rows[-1]["outage"]:
            raise ValueError("outage hold needs a finite up recovery")

    def index(self, ns):
        if ns >= self.end:
            raise ValueError("trace coverage exhausted")
        return bisect_right(self.starts, ns) - 1

    def release(self, deadline):
        index = self.index(deadline)
        if not self.rows[index]["outage"]:
            return deadline
        if self.profile["outage_action"] == "drop":
            return None
        return next(row["at_ns"] for row in self.rows[index:] if not row["outage"])


def q_admit(state, arrival, q, service=None):
    """Advance the exact service ledger; propagation never releases Q bytes."""
    ledger = state['ledger']
    while ledger and ledger[0][0] <= arrival:
        state['used'] -= ledger.popleft()[1]
    model = state['config']['model']
    if state['used'] + q > model['q_bytes']:
        return None
    finish = max(Fraction(arrival), state['tail']) + (Fraction(8*(q+20)*10**9, model['c_bps']) if service is None else service)
    state['tail'] = finish
    ledger.append((finish,q))
    state['used'] += q
    return finish


def trace_decision(state, service, ordinal):
    trace = state['trace']
    index = trace.index(service)
    raw = service + trace.rows[index]['t_ns']
    deadline = max(raw, state['floor'])
    if selected(trace.losses[index], trace.seed, ordinal):
        outcome, release = 'trace_loss', None
    else:
        release = trace.release(deadline)
        outcome = 'trace_outage' if release is None else 'accepted'
    if outcome == 'accepted':
        state['floor'] = deadline
    return deadline, release, outcome, deadline-raw


class Reference:
    """One bounded Q ledger per line. Emits decisions immediately in ingress order.

    Propagation storage is deliberately not a model loss: any shortage belongs
    to the reporter's storage gate. The receiver checker owns full identity sets.
    """

    def __init__(self, config, run, traces=None):
        fields(config, ("schema", "lines"))
        if config["schema"] != "hng-v1" or not isinstance(run, str) or not run:
            raise ValueError("invalid schema/run")
        self.config, self.run = config, run
        self.lines, self.ports = {}, {}
        self.vlan_lines = {}
        self.digest = hashlib.sha256()
        self.expected_digest = hashlib.sha256()
        self.count = 0
        self.resource_peaks = dict(q_ledger_packets=0, q_bytes=0, q_drain_ns=0)
        mappings = set()
        if not isinstance(config["lines"], list) or not 1 <= len(config["lines"]) <= 4:
            raise ValueError("hng-v1 requires 1..4 lines")
        for line in config["lines"]:
            fields(line, ("name", "ingress_port", "egress_port", "vlan_id", "model", "envelope", "trace"))
            name, model = line["name"], line["model"]
            fields(model, ("c_bps", "q_bytes", "t_ns", "delta_ns", "p", "seed",
                           "storage_capacity_packets", "storage_capacity_bytes"))
            if not isinstance(name, str) or not name or name in self.lines:
                raise ValueError("duplicate/empty line name")
            port = uint(line["ingress_port"], "ingress port", 65535)
            uint(line["egress_port"], "egress port", 65535)
            mapping = port, uint(line["vlan_id"], "VLAN", 4094)
            if mapping in mappings or mapping[1] == 0:
                raise ValueError("invalid/duplicate VLAN mapping")
            mappings.add(mapping)
            self.vlan_lines[mapping] = name
            for key in ("c_bps", "q_bytes", "t_ns", "delta_ns", "seed", "storage_capacity_packets", "storage_capacity_bytes"):
                uint(model[key], key)
            if (not model["c_bps"] or not model["storage_capacity_packets"]
                    or not model["storage_capacity_bytes"] or model["t_ns"] + model["delta_ns"] > U64):
                raise ValueError("invalid rate/delay")
            chance = probability(model["p"])
            trace = None
            if line["trace"] is not None:
                trace_reference(line["trace"])
                if model["t_ns"] or chance or model["delta_ns"] or name not in (traces or {}):
                    raise ValueError("trace needs registered profile, t=0, p=0 and delta=0")
                trace = traces[name] if isinstance(traces[name], Trace) else Trace(traces[name])
            envelope = line["envelope"]
            fields(envelope, ("min_q_bytes", "max_q_bytes", "burst_bound_packets"))
            low = uint(envelope["min_q_bytes"], "minimum frame")
            high = uint(envelope["max_q_bytes"], "maximum frame")
            if low < 64 or high < low or high > 1522 or not uint(envelope["burst_bound_packets"], "burst bound"):
                raise ValueError("invalid frame envelope")
            delay = model["t_ns"] + model["delta_ns"]
            if trace:
                delay = max(row["t_ns"] for row in trace.rows)
                if trace.profile["outage_action"] == "hold":
                    holds = []
                    start = None
                    for row in trace.rows:
                        if row["outage"] and start is None:
                            start = row["at_ns"]
                        elif not row["outage"] and start is not None:
                            holds.append(row["at_ns"] - start)
                            start = None
                    delay += max(holds, default=0)
            uint(delay, "delay/hold bound")
            duration_packets = Fraction(model["c_bps"] * delay, 8 * (low + 20) * 10**9)
            required = model["q_bytes"] // low + envelope["burst_bound_packets"] - (-duration_packets.numerator // duration_packets.denominator)
            if (required > model["storage_capacity_packets"]
                    or required * (high - 4) > model["storage_capacity_bytes"]):
                raise ValueError("storage is below the declared envelope")
            self.lines[name] = dict(config=line, chance=chance, trace=trace,
                                    tail=Fraction(0), ledger=deque(), used=0, seq=0, floor=0,
                                    counts=dict(offers=0, accepted=0, q_drop=0, trace_loss=0,
                                                trace_outage=0, invalid_input=0))
            self.ports.setdefault(port, dict(next_seq=0, arrival=0, frontier=-1, ended=False))
        if not self.lines:
            raise ValueError("no lines")

    def consume(self, event):
        if not isinstance(event, dict):
            raise ValueError("ingress event must be a JSON object")
        if event["run"] != self.run:
            raise ValueError("mixed run IDs")
        port = self.ports[uint(event["ingress_port"], "ingress port", 65535)]
        if port["ended"]:
            raise ValueError("event after end marker")
        kind = event["kind"]
        if kind in {"frontier", "end"}:
            if uint(event["next_ingress_seq"], "marker sequence") != port["next_seq"]:
                raise ValueError("frontier/end event count mismatch")
            if kind == "end":
                port["ended"] = True
            else:
                frontier = uint(event["through_ns"], "frontier")
                if frontier < max(port["arrival"], port["frontier"]):
                    raise ValueError("backward frontier")
                port["frontier"] = frontier
            return None
        if kind != "packet":
            raise ValueError("unknown ingress event")
        arrival = uint(event["arrival_ns"], "arrival")
        seq = uint(event["ingress_seq"], "ingress sequence", U64 - 1)
        length = uint(event["length"], "length", (1 << 32) - 1)
        identity = event["external_identity"]
        if seq != port["next_seq"] or arrival < port["arrival"] or arrival <= port["frontier"]:
            raise ValueError("invalid/gapped packet sequence or event time")
        if "stream_id" in event:
            name = self.vlan_lines.get((event["ingress_port"], event["vlan_id"]))
            state = self.lines.get(name)
            q = max(length + 4, 64)
            invalid = (state is None or not length or event["segments"] != 1
                       or identity is None or event["flow_id"] is None
                       or not state["config"]["envelope"]["min_q_bytes"] <= q <= state["config"]["envelope"]["max_q_bytes"])
            if invalid:
                if (not event["input_error"] or event["selection_seq"] is not None
                        or event["line"] not in {None, name}):
                    raise ValueError("invalid raw input requires an explicit rejection and no line ordinal")
                port.update(next_seq=seq + 1, arrival=arrival)
                self.count += 1
                self.digest.update(canonical(event) + b"\n")
                if state is not None:
                    while state["ledger"] and state["ledger"][0][0] <= arrival:
                        state["used"] -= state["ledger"].popleft()[1]
                drain = max(Fraction(0), state["tail"] - arrival) if state is not None else Fraction(0)
                result = dict(event, kind="expected", egress_port=None if state is None else state["config"]["egress_port"],
                              l2_bytes=q, wire_bytes=q + 20, outcome="invalid_input", late=False,
                              service_ns=None, deadline_ns=None, release_ns=None, clamp_extra_ns=0,
                              q_bytes=0 if state is None else state["used"],
                              q_drain_ns=-(-drain.numerator // drain.denominator))
                if state is not None:
                    state["counts"]["invalid_input"] += 1
                self.expected_digest.update(canonical(result) + b"\n")
                return result
            if event["line"] != name:
                raise ValueError("host line disagrees with raw port/VLAN mapping")
        state = self.lines[event["line"]]
        line, ordinal = state["config"], state["seq"]
        selection_seq = uint(event["selection_seq"], "selection sequence", U64 - 1)
        if (line["ingress_port"] != event["ingress_port"] or seq != port["next_seq"]
                or selection_seq != ordinal or arrival < port["arrival"]
                or arrival <= port["frontier"] or not length
                or not isinstance(identity, str) or not identity):
            raise ValueError("invalid/gapped packet identity, sequence or event time")
        q = max(length + 4, 64)
        envelope = line["envelope"]
        if not envelope["min_q_bytes"] <= q <= envelope["max_q_bytes"]:
            raise ValueError("frame outside registered envelope")
        port.update(next_seq=seq + 1, arrival=arrival)
        state["seq"] += 1
        self.count += 1
        self.digest.update(canonical(event) + b"\n")
        finish = q_admit(state, arrival, q)
        is_late = selected(state["chance"], line["model"]["seed"], ordinal)
        result = dict(event, kind="expected", egress_port=line["egress_port"],
                      l2_bytes=q, wire_bytes=q + 20, outcome="q_drop", late=is_late,
                      service_ns=None, deadline_ns=None, release_ns=None, clamp_extra_ns=0)
        model = line["model"]
        if finish is not None:
            service = -(-finish.numerator // finish.denominator)
            deadline = service + model["t_ns"] + is_late * model["delta_ns"]
            release, outcome, clamp = deadline, "accepted", 0
            trace = state["trace"]
            if trace:
                deadline, release, outcome, clamp = trace_decision(state, service, ordinal)
            uint(deadline, "deadline")
            uint(service, "service completion")
            result.update(outcome=outcome, late=is_late, service_ns=service,
                          deadline_ns=deadline, release_ns=release, clamp_extra_ns=clamp)
        result["q_bytes"] = state["used"]
        drain = max(Fraction(0), state["tail"] - arrival)
        result["q_drain_ns"] = -(-drain.numerator // drain.denominator)
        state["counts"]["offers"] += 1
        state["counts"][result["outcome"]] += 1
        for name, value in dict(q_ledger_packets=sum(len(s["ledger"]) for s in self.lines.values()),
                                q_bytes=sum(s["used"] for s in self.lines.values()),
                                q_drain_ns=result["q_drain_ns"]).items():
            self.resource_peaks[name] = max(self.resource_peaks[name], value)
        self.expected_digest.update(canonical(result) + b"\n")
        return result

    def finish(self):
        if not all(port["ended"] for port in self.ports.values()):
            raise ValueError("missing end marker(s): incomplete full-ingress evidence")
        return dict(kind="summary", schema="hng-g2-reference-draft-v1", run=self.run,
                    input_complete=True, event_count=self.count, ingress_sha256=self.digest.hexdigest(),
                    expected_sha256=self.expected_digest.hexdigest(),
                    config_sha256=hashlib.sha256(canonical(self.config)).hexdigest(),
                    end_counts={str(p): s["next_seq"] for p, s in self.ports.items()},
                    line_counts={name: dict(s["counts"]) for name, s in self.lines.items()},
                    resource_peaks=dict(self.resource_peaks))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("config")
    parser.add_argument("run")
    parser.add_argument("ingress_jsonl")
    args = parser.parse_args()
    # Import here: the interchange parser shares only primitive JSON helpers.
    from .protocol import StreamReader, StreamWriter
    writer = None
    try:
        config_contents = Path(args.config).read_bytes()
        config, traces = load_config(args.config, contents=config_contents)
        ref = Reference(config, args.run, traces)
        source = StreamReader(args.ingress_jsonl, role="ingress", run=args.run)
        writer = StreamWriter(sys.stdout, args.run, "expected", "expected", source.header["clock_id"],
                              producer="independent-fraction", producer_commit="independent-fraction-v1")
        output_digest = hashlib.sha256()
        for event in source:
            row = ref.consume(event)
            if row is not None:
                written = writer.emit("expected", **{key: value for key, value in row.items()
                    if key not in {"kind", "run", "stream_id", "seq"}})
                output_digest.update(canonical(written) + b"\n")
        summary = ref.finish()
        summary.update(schema="hng-g2-reference-v1", expected_sha256=output_digest.hexdigest(),
                       trace_sha256={line["name"]: line["trace"]["sha256"].lower()
                                     for line in config["lines"] if line["trace"]},
                       config_file_sha256=hashlib.sha256(config_contents).hexdigest(),
                       input_streams=[source.integrity], oracle_version="independent-fraction-v1")
        writer.emit("summary", **{key: value for key, value in summary.items() if key not in {"kind", "run"}})
        writer.finish()
    except (ValueError, KeyError, TypeError, OSError, StopIteration) as exc:
        if writer is not None:
            writer.finish(complete=False)
        print(f"INVALID: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
