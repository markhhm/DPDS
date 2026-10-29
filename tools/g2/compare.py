"""Compare every admission and emitted record with the real core replay adapter."""
import argparse
import hashlib
import json
from pathlib import Path
import random
import subprocess
import tempfile

from .reference import Reference, canonical


def compare(executable, total=1 << 20, seeds=32):
    if total < seeds or total % seeds or seeds < 1:
        raise ValueError("total must be a positive multiple of seed count")
    executable = Path(executable).resolve(strict=True)
    counts, digest = {}, hashlib.sha256()
    for seed in range(seeds):
        rng = random.Random(seed)
        # Short corruption fixtures still require the full registered storage envelope.
        capacity = max(total // seeds, 32768) + 16
        model = dict(c_bps=[8_000_000_000, 3_000_000_000, 99_999_999_989][seed % 3],
                     q_bytes=[160, 1522, 8192][seed % 3], t_ns=6500,
                     delta_ns=[0, 5000, 32000][seed % 3], p=["0", "1", "0.1"][seed % 3],
                     seed=seed, storage_capacity_packets=capacity,
                     storage_capacity_bytes=capacity * 1600)
        trace = None
        if seed % 2:
            model.update(t_ns=0, delta_ns=0, p="0")
            trace = dict(end_ns=4_000_000, terminal="hold_last_sample",
                         terminal_horizon_ns=10**12, loss_seed=seed + 31,
                         outage_action="hold" if seed % 4 == 1 else "drop",
                         rows=[dict(at_ns=0, t_ns=50000, loss="0", outage=False),
                               dict(at_ns=100000, t_ns=5000, loss="0.1", outage=False),
                               dict(at_ns=1000000, t_ns=6500, loss="0", outage=True),
                               dict(at_ns=1100000, t_ns=6500, loss="0", outage=False),
                               dict(at_ns=3000000, t_ns=6500, loss="1", outage=False)])
        cfg = dict(schema="hng-v1", lines=[dict(name="line", ingress_port=0, egress_port=1,
                   vlan_id=100, model=model, trace=None if trace is None else {"path": "fixture", "sha256": "0" * 64},
                   envelope=dict(min_q_bytes=64, max_q_bytes=1522, burst_bound_packets=16))])
        ref = Reference(cfg, "differential", traces={"line": trace} if trace else {})
        expected, arrival = [], 0
        with tempfile.TemporaryDirectory(prefix="g2-compare-") as directory:
            source, dest = Path(directory) / "input.jsonl", Path(directory) / "actual.jsonl"
            with source.open("w") as stream:
                stream.write(json.dumps(dict(model=model, trace=trace)) + "\n")
                for seq in range(total // seeds):
                    arrival += rng.randrange(800)
                    length = rng.choice([60, 76, 1518])
                    event = dict(kind="packet", run="differential", ingress_port=0, line="line",
                                 ingress_seq=seq, selection_seq=seq, external_identity=str(seq),
                                 arrival_ns=arrival, length=length)
                    row = ref.consume(event)
                    expected.append(row)
                    counts[row["outcome"]] = counts.get(row["outcome"], 0) + 1
                    stream.write(json.dumps(dict(ingress_seq=seq, arrival_ns=arrival, length=length)) + "\n")
            ref.consume(dict(kind="end", run="differential", ingress_port=0, next_ingress_seq=len(expected)))
            ref.finish()
            with source.open() as stdin, dest.open("w") as stdout:
                subprocess.run([str(executable)], stdin=stdin, stdout=stdout, check=True, timeout=60)
            outputs = []
            index = 0
            with dest.open() as stream:
                for line in stream:
                    actual = json.loads(line)
                    digest.update(canonical(actual) + b"\n")
                    if actual["kind"] == "admission":
                        want = expected[index]
                        index += 1
                        if (actual["ingress_seq"], actual["outcome"], actual["q_bytes"]) != (
                                want["ingress_seq"], want["outcome"], want["q_bytes"]):
                            raise ValueError(f"admission mismatch: seed={seed}, actual={actual}, want={want}")
                    else:
                        outputs.append(actual)
            survivors = sorted((r for r in expected if r["outcome"] == "accepted"),
                               key=lambda r: (r["release_ns"], r["ingress_seq"]))
            if index != len(expected) or len(outputs) != len(survivors):
                raise ValueError(f"record count mismatch: seed={seed}")
            for actual, want in zip(outputs, survivors):
                for field in ("ingress_seq", "selection_seq", "service_ns", "deadline_ns", "release_ns",
                              "late", "clamp_extra_ns", "l2_bytes", "wire_bytes"):
                    if actual[field] != want[field]:
                        raise ValueError(f"output mismatch: seed={seed}, field={field}, actual={actual}, want={want}")
    return dict(events=total, cases=seeds, mismatches=0, outcomes=counts,
                production_output_sha256=digest.hexdigest(), scope="software-only, not G2")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("executable")
    args = parser.parse_args()
    print(json.dumps(compare(args.executable), sort_keys=True))
