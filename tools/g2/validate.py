# SPDX-License-Identifier: Apache-2.0
"""Check registration/configuration/calibration before capture; never open devices."""
import argparse
import hashlib
import json
from pathlib import Path
import sys

from .calibration import Calibration
from .protocol import StreamError
from .reference import Reference, load_config, strict_json
from .registration import validate_registration


def preflight(path):
    path = Path(path)
    raw_manifest = path.read_bytes()
    manifest = strict_json(raw_manifest)
    config_path = path.parent / manifest["config"]["path"]
    raw_config = config_path.read_bytes()
    config_hash = hashlib.sha256(raw_config).hexdigest()
    if config_hash != manifest["config"]["sha256"]:
        raise StreamError("SOURCE_HASH_MISMATCH", "engine configuration bytes changed")
    config, traces = load_config(config_path, contents=raw_config)
    Reference(config, manifest["run"], traces)
    validate_registration(manifest, config)
    calibration_path = path.parent / manifest["calibration_path"]
    raw_calibration = calibration_path.read_bytes()
    calibration = Calibration(strict_json(raw_calibration), manifest["scope"], manifest["run"])
    if {manifest["clocks"]["engine"], *manifest["clocks"]["observation"].values()} - set(calibration.clocks):
        raise StreamError("CALIBRATION_CLOCK_MISSING", "registered clock is absent from calibration", "INCONCLUSIVE")
    for trace in manifest["traces"]:
        if hashlib.sha256((path.parent / trace["path"]).read_bytes()).hexdigest() != trace["sha256"]:
            raise StreamError("SOURCE_HASH_MISMATCH", "registered normalized trace bytes changed")
    return dict(schema="hng-g2-preflight-v1", check="registration-preflight", status="PASS",
                scope="software-only", registered_scope=manifest["scope"], g2_qualified=False,
                reason_codes=[], sources=dict(manifest_sha256=hashlib.sha256(raw_manifest).hexdigest(),
                    config_sha256=config_hash, calibration_sha256=hashlib.sha256(raw_calibration).hexdigest(),
                    trace_sha256={row["line"]:row["sha256"] for row in manifest["traces"]}),
                unavailable=["capture_integrity", "data_path_gates", "hardware_qualification"])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest")
    args = parser.parse_args()
    try:
        result = preflight(args.manifest)
    except (ValueError, KeyError, TypeError, OSError, OverflowError) as exc:
        result = dict(schema="hng-g2-preflight-v1", check="registration-preflight", status="INVALID",
                      scope="software-only", g2_qualified=False,
                      reason_codes=[getattr(exc, "code", "REGISTRATION_INVALID")], reason=str(exc))
    print(json.dumps(result, sort_keys=True, allow_nan=False))
    return 0 if result["status"] == "PASS" else 2


if __name__ == "__main__":
    sys.exit(main())
