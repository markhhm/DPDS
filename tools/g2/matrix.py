# SPDX-License-Identifier: Apache-2.0
"""Evaluate registered M6 runs with the existing G2 reporter."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import uuid

from .bundle import build_bundle_report
from .protocol import StreamError, shape
from .reference import strict_json
from .report import error_report, write_outputs
from .validate import load_registration


EXIT = {"PASS": 0, "FAIL": 1, "INVALID": 2, "INCONCLUSIVE": 2}
WORKER = "from tools.g2.matrix import report_one; import sys; sys.exit(report_one(*sys.argv[1:]))"


def file_hash(path):
    if not path.is_file():
        raise ValueError("resume validation requires regular input files")
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def source_hashes(path, mode):
    files = [path]
    manifest = strict_json(path.read_bytes())
    if mode == "pcapng":
        files += [path.parent / manifest[role]["path"] for role in ("before", "receiver")]
        path = (path.parent / manifest["manifest"]).resolve()
        files.append(path)
        manifest = strict_json(path.read_bytes())
    config_path = path.parent / manifest["config"]["path"]
    config = strict_json(config_path.read_bytes())
    files += [config_path, path.parent / manifest["calibration_path"]]
    files += [config_path.parent / line["trace"]["path"] for line in config["lines"] if line["trace"]]
    files += [path.parent / row["path"] for row in manifest["traces"]]
    files += [path.parent / row["path"] for row in manifest["streams"]
              if mode != "pcapng" or row["role"] not in {"before", "receiver"}]
    verification = manifest.get("verification", {})
    if not isinstance(verification, dict):
        raise ValueError("verification must be an object")
    if verification.get("mode") == "sampled":
        files += [path.parent / row[field] for row in verification.get("seqchecks", [])
                  for field in ("path", "ingress_path", "receiver_path")]
        if 'native_binary' in verification:
            files.append(path.parent/verification['native_binary']['path'])
            for entry in verification['capture_pairs']:
                pair_path=path.parent/entry
                pair=strict_json(pair_path.read_bytes())
                files.append(pair_path)
                files.extend(pair_path.parent/pair[role]['path'] for role in ('before','receiver'))
    return {str(item): file_hash(item) for item in dict.fromkeys(item.resolve() for item in files)}


def output_hashes(path):
    files = {}
    for item in path.rglob("*"):
        if item == path / ".complete.json":
            continue
        name = str(item.relative_to(path))
        if item.is_symlink():
            files[name] = dict(symlink=str(item.readlink()))
            if item.is_file():
                files[name]["sha256"] = file_hash(item)
        elif item.is_file():
            files[name] = file_hash(item)
    return files


def write_json(path, value):
    temporary = path.with_name("." + path.name.lstrip(".") + ".tmp")
    temporary.write_text(json.dumps(value, allow_nan=False, indent=2) + "\n")
    temporary.replace(path)


def archive(destination, *paths):
    paths = [path for path in paths if path.exists() or path.is_symlink()]
    if paths:
        backup = destination / ".incomplete" / uuid.uuid4().hex
        backup.mkdir(parents=True)
        for path in paths:
            path.rename(backup / path.name)


def read_verdict(path, run_id, scope, returncode):
    verdict = strict_json((path / "verdict.json").read_bytes())
    if (not isinstance(verdict, dict) or verdict.get("run") != run_id or verdict.get("scope") != scope
            or verdict.get("status") not in EXIT or returncode != EXIT[verdict["status"]]
            or any(not (path / name).is_file() for name in ("report.json", "results.csv", "summary.txt"))):
        raise ValueError("reporter returned inconsistent run artifacts")
    return verdict


def completed_run(path, source, run_id, scope, mode):
    try:
        seal = strict_json((path / ".complete.json").read_bytes())
        if (seal["schema"] != "hng-m6-complete-v1" or seal["mode"] != mode
                or seal["sources"] != source_hashes(source, mode)
                or seal["outputs"] != output_hashes(path)):
            return None
        return read_verdict(path, run_id, scope, seal["exit"]), seal["exit"]
    except (ValueError, KeyError, TypeError, OSError):
        return None


def report_one(manifest_path, output, run_id, scope, mode="software"):
    """Keep the reporter's large result in a fresh process, not the controller."""
    path, destination = Path(manifest_path), Path(output)
    destination.mkdir()
    manifest = {"run": run_id, "scope": scope}
    inputs = None
    try:
        manifest = strict_json(path.read_bytes())
        if mode == "pcapng":
            pair_path = path
            manifest = strict_json((path.parent / manifest["manifest"]).read_bytes())
        if not isinstance(manifest, dict) or manifest.get("run") != run_id or manifest.get("scope") != scope:
            raise StreamError("MATRIX_METADATA_MISMATCH", "run/scope differs from matrix registration")
        inputs = source_hashes(Path(manifest_path), mode)
        if mode == "pcapng":
            from .pcapng import prepare_pair
            path = prepare_pair(pair_path, destination / "capture")
            manifest = strict_json(path.read_bytes())
        checked, config, traces = load_registration(path)
        sources = {row["stream_id"]: path.parent / row["path"] for row in manifest["streams"]}
        calibration = strict_json((path.parent / manifest["calibration_path"]).read_bytes())
        result = build_bundle_report(config, traces, manifest, sources, calibration, destination,
                                     registration_root=path.parent)
        result["sources"].update(checked["sources"])
    except (ValueError, KeyError, TypeError, OSError, MemoryError) as error:
        print(str(error), file=sys.stderr)
        result = error_report(error, manifest if isinstance(manifest, dict) else {})
    if inputs is not None:
        try:
            if inputs != source_hashes(Path(manifest_path), mode):
                raise StreamError("MATRIX_SOURCE_CHANGED", "run inputs changed during reporting", "INCONCLUSIVE")
        except (ValueError, KeyError, TypeError, OSError) as error:
            inputs = None
            result = error_report(error, manifest)
    write_outputs(result, destination)
    verdict = {key: result.get(key) for key in
               ("run", "condition_id", "scope", "status", "measurement_status", "g2_qualified", "reason_codes")}
    verdict["rows"] = [{key: row.get(key) for key in
                        ("condition_id", "repeat", "direction", "line", "phase", "required", "status")}
                       for row in result.get("rows", [])]
    (destination / "verdict.json").write_text(json.dumps(verdict, allow_nan=False) + "\n")
    if inputs is not None:
        write_json(destination / ".complete.json", dict(schema="hng-m6-complete-v1", mode=mode,
            sources=inputs, outputs=output_hashes(destination), exit=EXIT[result["status"]]))
    return EXIT[result["status"]]


def run_matrix(matrix_path, output):
    path = Path(matrix_path)
    matrix = strict_json(path.read_bytes())
    shape(matrix, ("schema", "scope", "plan", "runs"), ("mode", "workers"))
    mode = matrix.get("mode", "software")
    if mode not in ("software", "pcapng"):
        raise ValueError("unsupported matrix mode")
    if matrix["schema"] != "hng-m6-matrix-v1" or matrix["scope"] not in ("software-only", "physical"):
        raise ValueError("unsupported matrix schema/scope")
    if not isinstance(matrix["plan"], dict) or not isinstance(matrix["runs"], list) or not matrix["runs"]:
        raise ValueError("matrix requires plan metadata and at least one registered run")
    workers = matrix.get("workers", 1)
    available_cpus = len(os.sched_getaffinity(0)) if hasattr(os, "sched_getaffinity") else os.cpu_count() or 1
    if type(workers) is not int or not 1 <= workers <= available_cpus:
        raise ValueError("matrix workers must be a positive integer no greater than available CPUs")
    ids, manifests, names = set(), set(), {"summary.json"}
    for run in matrix["runs"]:
        shape(run, ("id", "manifest"), ("family",))
        if "family" in run and run["family"] not in ("main", "boundary", "trace"):
            raise ValueError("unknown short-plan family")
        if not isinstance(run["id"], str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", run["id"]):
            raise ValueError("run ID must be a safe directory name")
        if not isinstance(run["manifest"], str) or not run["manifest"]:
            raise ValueError("run manifest must be a path")
        manifest = (path.parent / run["manifest"]).resolve()
        if run["id"] in ids or manifest in manifests:
            raise ValueError("duplicate run ID or manifest evidence")
        generated = {run["id"], run["id"] + ".stderr.log"}
        if generated & names:
            raise ValueError("run ID collides with a generated output name")
        names.update(generated)
        ids.add(run["id"]); manifests.add(manifest)
    destination = Path(output)
    # shortcut: one writer per output directory; add locking if concurrent resumes are required.
    destination.mkdir(parents=True, exist_ok=True)
    marker = destination / ".matrix.json"
    registration = dict(schema="hng-m6-resume-v1", source=str(path.resolve()), matrix=matrix)
    if marker.exists():
        if strict_json(marker.read_bytes()) != registration:
            raise ValueError("output belongs to a different matrix registration")
    else:
        if any(item.name not in {".matrix.json.tmp", ".incomplete"} for item in destination.iterdir()):
            raise ValueError("existing output is not a resumable matrix directory")
        archive(destination, destination / ".matrix.json.tmp")
        write_json(marker, registration)
    archive(destination, destination / "summary.json", destination / ".summary.json.tmp")
    def evaluate(run):
        run_id = run["id"]
        source, target = (path.parent / run["manifest"]).resolve(), destination / run_id
        log_path = destination / (run_id + ".stderr.log")
        cached = completed_run(target, source, run_id, matrix["scope"], mode)
        if cached is not None:
            verdict, returncode = cached
        else:
            archive(destination, target, log_path)
            with log_path.open("w") as log:
                process = subprocess.run([sys.executable, "-c", WORKER,
                    str(source), str(target.resolve()), run_id, matrix["scope"], mode],
                    stdout=subprocess.DEVNULL, stderr=log)
            returncode = process.returncode
            try:
                verdict = read_verdict(target, run_id, matrix["scope"], returncode)
            except (ValueError, KeyError, TypeError, OSError):
                verdict = dict(run=run_id, scope=matrix["scope"], status="INVALID",
                               measurement_status="INCONCLUSIVE", g2_qualified=False,
                               reason_codes=["REPORTER_PROCESS_ERROR"], rows=[])
        return dict(verdict, id=run_id, reporter_exit=returncode, family=run.get("family"),
                    report=run_id + "/report.json", stderr=run_id + ".stderr.log")
    if workers == 1:
        results = list(map(evaluate, matrix["runs"]))
    else:
        with ThreadPoolExecutor(max_workers=workers) as executor:
            results = list(executor.map(evaluate, matrix["runs"]))
    state = ("FAIL" if any(run["measurement_status"] == "FAIL" for run in results) else
             "INCONCLUSIVE" if any(run["measurement_status"] != "PASS" for run in results) else "PASS")
    result = dict(schema="hng-m6-report-v1", scope=matrix["scope"], plan=matrix["plan"],
                  status="INVALID" if state == "INCONCLUSIVE" else state, measurement_status=state,
                  g2_qualified=matrix["scope"] == "physical" and state == "PASS"
                               and all(run["g2_qualified"] is True for run in results), runs=results)
    write_json(destination / "summary.json", result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("matrix")
    parser.add_argument("--out", required=True, help="new or resumable output directory")
    args = parser.parse_args()
    try:
        result = run_matrix(args.matrix, args.out)
    except (ValueError, KeyError, TypeError, OSError) as error:
        print(str(error), file=sys.stderr)
        return 2
    print(json.dumps(result, allow_nan=False))
    return EXIT[result["status"]]


if __name__ == "__main__":
    sys.exit(main())
