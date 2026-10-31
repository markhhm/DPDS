# SPDX-License-Identifier: Apache-2.0
"""Compare production scheduling with the independent Fraction oracle."""
import argparse
from concurrent.futures import ProcessPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import random
import subprocess
import tempfile

from .reference import Reference, U64, canonical


ADMISSION_FIELDS = ("ingress_seq", "selection_seq", "outcome", "late", "q_bytes", "q_drain_ns")
OUTPUT_FIELDS = ("ingress_seq", "selection_seq", "service_ns", "deadline_ns", "release_ns",
                 "late", "clamp_extra_ns", "l2_bytes", "wire_bytes", "ingress_port", "egress_port")


def _configuration(seed, capacity, directory):
    lines, traces = [], {}
    for index in range(4):
        t, delta = [(0, 0), (6500, 0), (0, 32000), (6500, 32000)][(seed + index) % 4]
        model = dict(c_bps=[8_000_000_000, 3_000_000_000, 99_999_999_989][(seed + index) % 3],
                     q_bytes=[160, 1522, 8192][(seed + index) % 3], t_ns=t, delta_ns=delta,
                     p=["0", "1", "0.1"][(seed + index) % 3], seed=seed * 4 + index,
                     storage_capacity_packets=capacity, storage_capacity_bytes=capacity * 1600)
        name = f"line-{index}"
        descriptor = None
        if seed % 4 == 3:
            model.update(t_ns=0, delta_ns=0, p="0")
            profile = dict(end_ns=4_000_000, terminal="hold_last_sample",
                           terminal_horizon_ns=10**12, loss_seed=seed * 4 + index + 31,
                           outage_action="hold" if (seed + index) % 2 else "drop",
                           rows=[dict(at_ns=0, t_ns=50000, loss="0", outage=False),
                                 dict(at_ns=100000, t_ns=5000, loss="0.1", outage=False),
                                 dict(at_ns=1000000, t_ns=6500, loss="0", outage=True),
                                 dict(at_ns=1100000, t_ns=6500, loss="0", outage=False),
                                 dict(at_ns=3000000, t_ns=6500, loss="1", outage=False)])
            document = dict(schema="hng-trace-v1", source_id="synthetic-differential",
                            direction=f"{index // 2}-to-{1 - index // 2}", units="ns_owd",
                            conversion="exact-ns", source_sha256=hashlib.sha256(canonical(profile)).hexdigest(),
                            splice_policy="natural_hold", trace_epoch_ns=0, time_scale=1, profile=profile)
            data = canonical(document)
            descriptor = dict(path=f"trace-{index}.json", sha256=hashlib.sha256(data).hexdigest())
            (directory / descriptor["path"]).write_bytes(data)
            traces[name] = profile
        lines.append(dict(name=name, ingress_port=index // 2, egress_port=1 - index // 2,
                          vlan_id=100 + index % 2, model=model, trace=descriptor,
                          envelope=dict(min_q_bytes=64, max_q_bytes=1522, burst_bound_packets=16)))
    return dict(schema="hng-v1", lines=lines), traces


def _match(actual, want, fields, *, seed, kind):
    for field in fields:
        if field not in actual or actual[field] != want[field]:
            raise ValueError(f"{kind} mismatch: seed={seed}, field={field}, actual={actual}, want={want}")


def _compare_case(task):
    executable, total, seeds, seed, directory = task
    directory = Path(directory)
    directory.mkdir()
    counts = {}
    coverage = dict(shared_egress_cases=1, frontiers=0, ends=0, q_drop_late=0,
                    trace_holds=0, clamped=0, outputs_before_end=0)
    directions, fixed_states, trace_actions = set(), set(), set()
    rng = random.Random(seed)
    cfg, traces = _configuration(seed, max(total // seeds, 32768) + 16, directory)
    ref = Reference(cfg, "differential", traces=traces)
    expected, pending, snapshots = [], [], {}
    port_sequences, line_sequences = [0, 0], [0] * 4
    frontiers, ended = {}, set()
    arrival, trigger = 0, 0
    source, dest = directory / "input.jsonl", directory / "actual.jsonl"
    for line in cfg["lines"]:
        directions.add((line["ingress_port"], line["egress_port"]))
        if line["trace"] is None:
            fixed_states.add((line["model"]["t_ns"], line["model"]["delta_ns"]))
        else:
            trace_actions.add(traces[line["name"]]["outage_action"])

    def marker(kind, port, now, stream):
        nonlocal trigger, pending
        event = dict(kind=kind, run="differential", ingress_port=port,
                     next_ingress_seq=port_sequences[port])
        if kind == "frontier":
            event["through_ns"] = arrival
            frontiers[port] = arrival
        else:
            ended.add(port)
        ref.consume(event)
        stream.write(json.dumps({k: v for k, v in dict(event, now_ns=now).items() if k != "run"}) + "\n")
        coverage["frontiers" if kind == "frontier" else "ends"] += 1
        eligible, waiting = [], []
        for row in pending:
            index = row["line"]
            state = ref.lines[cfg["lines"][index]["name"]]
            owner = row["ingress_port"]
            minimum = (min(r["t_ns"] for r in state["trace"].rows) if state["trace"]
                       else state["config"]["model"]["t_ns"])
            certified = owner in ended or (owner in frontiers
                                           and row["release_ns"] <= frontiers[owner] + minimum)
            (eligible if certified and row["release_ns"] <= now else waiting).append(row)
        pending = waiting
        for row in sorted(eligible, key=lambda r: (r["egress_port"], r["release_ns"], r["ingress_seq"])):
            expected.append(dict(row, kind="output", now_ns=now, trigger=trigger))
            coverage["outputs_before_end"] += row["ingress_port"] not in ended
        states = []
        for index, line in enumerate(cfg["lines"]):
            if line["ingress_port"] in ended:
                state = ref.lines[line["name"]]
                remaining = max(0, state["tail"] - now)
                states.append(dict(line=index, q_bytes=sum(q for finish, q in state["ledger"] if finish > now),
                                   q_drain_ns=-(-remaining.numerator // remaining.denominator),
                                   ledger_watermark_ns=now, input_complete=True))
            else:
                states.append(dict(snapshots.get(index, dict(q_bytes=0, q_drain_ns=0,
                                   ledger_watermark_ns=0)), line=index, input_complete=False))
        expected.append(dict(kind="marker", trigger=trigger, ingress_port=port, marker=kind, lines=states))
        trigger += 1

    with source.open("w") as stream:
        stream.write(json.dumps(dict(schema="hng-m2-replay-v1", config=cfg, base_dir=str(directory))) + "\n")
        for sequence in range(total // seeds):
            index = rng.randrange(4)
            port = index // 2
            # A marker is closed: the next offered arrival must be strictly later.
            arrival += rng.randrange(800) + (sequence % 128 == 0)
            length = rng.choice([1, 60, 76, 1518])
            event = dict(kind="packet", run="differential", ingress_port=port,
                         line=cfg["lines"][index]["name"], ingress_seq=port_sequences[port],
                         selection_seq=line_sequences[index], external_identity=f"{port}:{port_sequences[port]}",
                         arrival_ns=arrival, length=length)
            row = ref.consume(event)
            row["line"] = index
            expected.append(dict(row, kind="admission"))
            snapshots[index] = dict(q_bytes=row["q_bytes"], q_drain_ns=row["q_drain_ns"],
                                    ledger_watermark_ns=arrival)
            counts[row["outcome"]] = counts.get(row["outcome"], 0) + 1
            coverage["q_drop_late"] += row["outcome"] == "q_drop" and row["late"]
            coverage["clamped"] += row["clamp_extra_ns"] > 0
            coverage["trace_holds"] += (row["outcome"] == "accepted"
                                          and row["release_ns"] > row["deadline_ns"])
            if row["outcome"] == "accepted":
                pending.append(row)
            stream.write(json.dumps(dict(kind="packet", line=index, ingress_port=port,
                         ingress_seq=port_sequences[port], arrival_ns=arrival, length=length)) + "\n")
            port_sequences[port] += 1
            line_sequences[index] += 1
            trigger += 1
            if (sequence + 1) % 128 == 0:
                for owner in range(2):
                    marker("frontier", owner, arrival + 100000, stream)
        for owner in range(2):
            marker("frontier", owner, arrival + 100000, stream)
            marker("end", owner, arrival + 100000, stream)
    ref.finish()
    for row in sorted(pending, key=lambda r: (r["egress_port"], r["release_ns"], r["ingress_seq"])):
        expected.append(dict(row, kind="output", now_ns=U64, trigger=trigger))
    expected.append(dict(kind="drained", lines=[dict(line=index, q_bytes=0, q_drain_ns=0,
                          ledger_watermark_ns=U64, input_complete=True) for index in range(4)]))
    with source.open() as stdin, dest.open("w") as stdout:
        subprocess.run([str(executable)], stdin=stdin, stdout=stdout, check=True, timeout=60)
    count = 0
    with dest.open() as stream, (directory / "canonical.jsonl").open("wb") as canonical_stream:
        for count, line in enumerate(stream, 1):
            actual = json.loads(line)
            canonical_stream.write(canonical(actual) + b"\n")
            if count > len(expected):
                raise ValueError(f"record count mismatch: seed={seed}")
            want = expected[count - 1]
            _match(actual, want, ("kind",), seed=seed, kind="record")
            if want["kind"] == "admission":
                fields = ADMISSION_FIELDS + ("line", "ingress_port", "egress_port")
            elif want["kind"] == "output":
                fields = OUTPUT_FIELDS + ("line", "trigger", "now_ns")
            else:
                fields = tuple(want)
            _match(actual, want, fields, seed=seed, kind=want["kind"])
    if count != len(expected):
        raise ValueError(f"record count mismatch: seed={seed}, actual={count}, expected={len(expected)}")
    source.unlink()
    dest.unlink()
    coverage.update(directions=sorted(map(list, directions)), fixed_states=sorted(map(list, fixed_states)),
                    trace_actions=sorted(trace_actions))
    return dict(outcomes=counts, coverage=coverage, canonical_output=str(directory / "canonical.jsonl"))


def compare(executable, total=1 << 20, seeds=32):
    if total < seeds or total % seeds or seeds < 1:
        raise ValueError("total must be a positive multiple of seed count")
    executable = str(Path(executable).resolve(strict=True))
    cpus = len(os.sched_getaffinity(0)) if hasattr(os, "sched_getaffinity") else os.cpu_count() or 1
    counts, coverage, digest = {}, {}, hashlib.sha256()
    directions, fixed_states, trace_actions = set(), set(), set()
    with tempfile.TemporaryDirectory(prefix="g2-compare-") as temporary:
        tasks = ((executable, total, seeds, seed, str(Path(temporary) / str(seed))) for seed in range(seeds))
        with ProcessPoolExecutor(max_workers=min(4, seeds, cpus)) as pool:
            # map yields seed order even when workers finish out of order.
            for result in pool.map(_compare_case, tasks):
                for outcome, count in result["outcomes"].items():
                    counts[outcome] = counts.get(outcome, 0) + count
                for name, value in result["coverage"].items():
                    if isinstance(value, int):
                        coverage[name] = coverage.get(name, 0) + value
                directions.update(map(tuple, result["coverage"]["directions"]))
                fixed_states.update(map(tuple, result["coverage"]["fixed_states"]))
                trace_actions.update(result["coverage"]["trace_actions"])
                path = Path(result["canonical_output"])
                with path.open("rb") as stream:
                    while chunk := stream.read(256 * 1024):
                        digest.update(chunk)
                path.unlink()
    coverage.update(directions=sorted(map(list, directions)), fixed_states=sorted(map(list, fixed_states)),
                    trace_actions=sorted(trace_actions))
    return dict(events=total, cases=seeds, mismatches=0, outcomes=counts, coverage=coverage,
                production_output_sha256=digest.hexdigest(), scope="software-only, not G2")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("executable")
    args = parser.parse_args()
    print(json.dumps(compare(args.executable), sort_keys=True))
