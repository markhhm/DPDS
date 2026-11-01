# SPDX-License-Identifier: Apache-2.0
"""Evaluate registered M6 runs sequentially with the existing G2 reporter."""
import argparse
import json
from pathlib import Path
import re
import subprocess
import sys

from .bundle import build_bundle_report
from .protocol import StreamError, shape
from .reference import load_config, strict_json
from .report import error_report, write_outputs
from .validate import preflight


EXIT = {"PASS": 0, "FAIL": 1, "INVALID": 2, "INCONCLUSIVE": 2}
WORKER = "from tools.g2.matrix import report_one; import sys; sys.exit(report_one(*sys.argv[1:]))"


def report_one(manifest_path, output, run_id, scope, mode="software"):
    """Keep the reporter's large result in a fresh process, not the controller."""
    path, destination = Path(manifest_path), Path(output)
    destination.mkdir()
    manifest = {"run": run_id, "scope": scope}
    try:
        manifest = strict_json(path.read_bytes())
        if mode == "pcapng":
            pair_path = path
            manifest = strict_json((path.parent / manifest["manifest"]).read_bytes())
        if not isinstance(manifest, dict) or manifest.get("run") != run_id or manifest.get("scope") != scope:
            raise StreamError("MATRIX_METADATA_MISMATCH", "run/scope differs from matrix registration")
        if mode == "pcapng":
            from .pcapng import prepare_pair
            path = prepare_pair(pair_path, destination / "capture")
            manifest = strict_json(path.read_bytes())
        checked = preflight(path)
        config, traces = load_config(path.parent / manifest["config"]["path"])
        sources = {row["stream_id"]: path.parent / row["path"] for row in manifest["streams"]}
        calibration = strict_json((path.parent / manifest["calibration_path"]).read_bytes())
        result = build_bundle_report(config, traces, manifest, sources, calibration, destination)
        result["sources"].update(checked["sources"])
    except (ValueError, KeyError, TypeError, OSError, MemoryError) as error:
        print(str(error), file=sys.stderr)
        result = error_report(error, manifest if isinstance(manifest, dict) else {})
    write_outputs(result, destination)
    verdict = {key: result.get(key) for key in
               ("run", "condition_id", "scope", "status", "measurement_status", "g2_qualified", "reason_codes")}
    verdict["rows"] = [{key: row.get(key) for key in
                        ("condition_id", "repeat", "direction", "line", "phase", "required", "status")}
                       for row in result.get("rows", [])]
    (destination / "verdict.json").write_text(json.dumps(verdict, allow_nan=False) + "\n")
    return EXIT[result["status"]]


def run_matrix(matrix_path, output):
    path = Path(matrix_path)
    matrix = strict_json(path.read_bytes())
    shape(matrix, ("schema", "scope", "plan", "runs"), ("mode",))
    mode = matrix.get("mode", "software")
    if mode not in ("software", "pcapng"):
        raise ValueError("unsupported matrix mode")
    if matrix["schema"] != "hng-m6-matrix-v1" or matrix["scope"] not in ("software-only", "physical"):
        raise ValueError("unsupported matrix schema/scope")
    if not isinstance(matrix["plan"], dict) or not isinstance(matrix["runs"], list) or not matrix["runs"]:
        raise ValueError("matrix requires plan metadata and at least one registered run")
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
    destination.mkdir(parents=True, exist_ok=False)
    results = []
    for run in matrix["runs"]:
        run_id = run["id"]
        with (destination / (run_id + ".stderr.log")).open("w") as log:
            process = subprocess.run([sys.executable, "-c", WORKER,
                str((path.parent / run["manifest"]).resolve()), str((destination / run_id).resolve()),
                run_id, matrix["scope"], mode], stdout=subprocess.DEVNULL, stderr=log)
        try:
            verdict = strict_json((destination / run_id / "verdict.json").read_bytes())
            if (verdict["run"] != run_id or verdict["scope"] != matrix["scope"]
                    or verdict["status"] not in EXIT or process.returncode != EXIT[verdict["status"]]
                    or any(not (destination / run_id / name).is_file()
                           for name in ("report.json", "results.csv", "summary.txt"))):
                raise ValueError("reporter returned inconsistent run artifacts")
        except (ValueError, KeyError, TypeError, OSError):
            verdict = dict(run=run_id, scope=matrix["scope"], status="INVALID",
                           measurement_status="INCONCLUSIVE", g2_qualified=False,
                           reason_codes=["REPORTER_PROCESS_ERROR"], rows=[])
        results.append(dict(verdict, id=run_id, reporter_exit=process.returncode,
                            family=run.get("family"),
                            report=run_id + "/report.json", stderr=run_id + ".stderr.log"))
    state = ("FAIL" if any(run["measurement_status"] == "FAIL" for run in results) else
             "INCONCLUSIVE" if any(run["measurement_status"] != "PASS" for run in results) else "PASS")
    result = dict(schema="hng-m6-report-v1", scope=matrix["scope"], plan=matrix["plan"],
                  status="INVALID" if state == "INCONCLUSIVE" else state, measurement_status=state,
                  g2_qualified=matrix["scope"] == "physical" and state == "PASS"
                               and all(run["g2_qualified"] is True for run in results), runs=results)
    (destination / "summary.json").write_text(json.dumps(result, indent=2, allow_nan=False) + "\n")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("matrix")
    parser.add_argument("--out", required=True, help="new output directory")
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
