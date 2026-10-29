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
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


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


def load_config(path):
    path = Path(path)
    config = strict_json(path.read_text())
    traces = {}
    for line in config["lines"]:
        if line["trace"] is None:
            continue
        data = (path.parent / line["trace"]["path"]).read_bytes()
        if hashlib.sha256(data).hexdigest() != line["trace"]["sha256"]:
            raise ValueError("trace SHA-256 mismatch")
        doc = strict_json(data)
        if (doc["schema"] != "hng-trace-v1" or doc["units"] != "ns_owd"
                or doc["trace_epoch_ns"] != 0 or doc["time_scale"] != 1
                or not doc["source_id"] or not doc["direction"]
                or not re.fullmatch(r"[0-9a-f]{64}", doc["source_sha256"])
                or not doc["conversion"]
                or doc["splice_policy"] not in {"natural_hold", "paired_splice", "unspliced_phases"}):
            raise ValueError("invalid normalized trace provenance/units")
        traces[line["name"]] = doc["profile"]
    return config, traces


class Trace:
    def __init__(self, profile):
        self.profile = profile
        self.end = uint(profile["end_ns"], "trace end")
        self.seed = uint(profile["loss_seed"], "loss seed")
        self.rows = profile["rows"]
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


class Reference:
    """One bounded Q ledger per line. Emits decisions immediately in ingress order.

    Propagation storage is deliberately not a model loss: any shortage belongs
    to the reporter's storage gate. The receiver checker owns full identity sets.
    """

    def __init__(self, config, run, traces=None):
        if config["schema"] != "hng-v1" or not isinstance(run, str) or not run:
            raise ValueError("invalid schema/run")
        self.config, self.run = config, run
        self.lines, self.ports = {}, {}
        self.digest = hashlib.sha256()
        self.count = 0
        mappings = set()
        for line in config["lines"]:
            name, model = line["name"], line["model"]
            if not isinstance(name, str) or not name or name in self.lines:
                raise ValueError("duplicate/empty line name")
            port = uint(line["ingress_port"], "ingress port", 65535)
            uint(line["egress_port"], "egress port", 65535)
            mapping = port, uint(line["vlan_id"], "VLAN", 4094)
            if mapping in mappings or mapping[1] == 0:
                raise ValueError("invalid/duplicate VLAN mapping")
            mappings.add(mapping)
            for key in ("c_bps", "q_bytes", "t_ns", "delta_ns", "seed"):
                uint(model[key], key)
            if not model["c_bps"] or model["t_ns"] + model["delta_ns"] > U64:
                raise ValueError("invalid rate/delay")
            chance = probability(model["p"])
            trace = None
            if line["trace"] is not None:
                if model["t_ns"] or chance or model["delta_ns"] or name not in (traces or {}):
                    raise ValueError("trace needs registered profile, t=0, p=0 and delta=0")
                trace = Trace(traces[name])
            envelope = line["envelope"]
            low = uint(envelope["min_q_bytes"], "minimum frame")
            high = uint(envelope["max_q_bytes"], "maximum frame")
            if low < 64 or high < low:
                raise ValueError("invalid frame envelope")
            self.lines[name] = dict(config=line, chance=chance, trace=trace,
                                    tail=Fraction(0), ledger=deque(), used=0, seq=0, floor=0)
            self.ports.setdefault(port, dict(next_seq=0, arrival=0, frontier=-1, ended=False))
        if not self.lines:
            raise ValueError("no lines")

    def consume(self, event):
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
        state = self.lines[event["line"]]
        line, ordinal = state["config"], state["seq"]
        arrival = uint(event["arrival_ns"], "arrival")
        seq = uint(event["ingress_seq"], "ingress sequence", U64 - 1)
        selection_seq = uint(event["selection_seq"], "selection sequence", U64 - 1)
        length = uint(event["length"], "length", (1 << 32) - 1)
        identity = event["external_identity"]
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
        ledger = state["ledger"]
        while ledger and ledger[0][0] <= arrival:
            state["used"] -= ledger.popleft()[1]
        result = dict(event, kind="expected", egress_port=line["egress_port"],
                      l2_bytes=q, wire_bytes=q + 20, outcome="q_drop", late=False,
                      service_ns=None, deadline_ns=None, release_ns=None, clamp_extra_ns=0)
        model = line["model"]
        if state["used"] + q <= model["q_bytes"]:
            finish = max(Fraction(arrival), state["tail"]) + Fraction(8 * (q + 20) * 10**9, model["c_bps"])
            service = -(-finish.numerator // finish.denominator)
            is_late = selected(state["chance"], model["seed"], ordinal)
            deadline = service + model["t_ns"] + is_late * model["delta_ns"]
            release, outcome, clamp = deadline, "accepted", 0
            trace = state["trace"]
            if trace:
                index = trace.index(service)
                raw = service + trace.rows[index]["t_ns"]
                deadline = max(raw, state["floor"])
                clamp = deadline - raw
                if selected(trace.losses[index], trace.seed, ordinal):
                    outcome, release = "trace_loss", None
                else:
                    release = trace.release(deadline)
                    if release is None:
                        outcome = "trace_outage"
            uint(deadline, "deadline")
            uint(service, "service completion")
            if outcome == "accepted" and trace:
                state["floor"] = deadline
            state["tail"] = finish
            ledger.append((finish, q))
            state["used"] += q
            result.update(outcome=outcome, late=is_late, service_ns=service,
                          deadline_ns=deadline, release_ns=release, clamp_extra_ns=clamp)
        result["q_bytes"] = state["used"]
        drain = max(Fraction(0), state["tail"] - arrival)
        result["q_drain_ns"] = -(-drain.numerator // drain.denominator)
        return result

    def finish(self):
        if not all(port["ended"] for port in self.ports.values()):
            raise ValueError("missing end marker(s): incomplete full-ingress evidence")
        return dict(kind="summary", schema="hng-g2-reference-draft-v1", run=self.run,
                    input_complete=True, event_count=self.count, ingress_sha256=self.digest.hexdigest(),
                    config_sha256=hashlib.sha256(canonical(self.config)).hexdigest())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("config")
    parser.add_argument("run")
    parser.add_argument("ingress_jsonl")
    args = parser.parse_args()
    try:
        config, traces = load_config(args.config)
        ref = Reference(config, args.run, traces)
        with open(args.ingress_jsonl) as source:
            for line in source:
                row = ref.consume(strict_json(line))
                if row is not None:
                    print(json.dumps(row, allow_nan=False))
        print(json.dumps(ref.finish(), allow_nan=False))
    except (ValueError, KeyError, TypeError, OSError, StopIteration) as exc:
        print(f"INVALID: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
