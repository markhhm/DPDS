# SPDX-License-Identifier: Apache-2.0
"""Static v1 registration checks; data completeness is checked during capture."""
from functools import lru_cache
import math
import re

from .online import Retention
from .protocol import ROLES, boolean, digest, identifier, shape, handoff_actors
from .reference import uint


def _array(value, name):
    if not isinstance(value, list):
        raise ValueError(f"{name} must be an array")
    return value


def _labels(value, name):
    values = _array(value, name)
    for item in values:
        identifier(item, name)
    if len(set(values)) != len(values):
        raise ValueError(f"duplicate {name}")
    return values


@lru_cache(maxsize=11)
def planning_target(mask_bits):
    """Smallest N with P(Binomial(N, 2**-m) < 368) <= 0.001."""
    uint(mask_bits, "mask bits", 10)
    if not mask_bits:
        return 368
    p = 2**-mask_bits
    def insufficient(n):
        term = math.exp(n*math.log1p(-p))
        terms = [term]
        for k in range(1, 368):
            term *= (n-k+1)*p/(k*(1-p))
            terms.append(term)
        return math.fsum(terms) > 0.001
    low, high = 367, 500 << mask_bits
    while low+1 < high:
        middle = (low+high)//2
        if insufficient(middle):
            low = middle
        else:
            high = middle
    return high


def validate_registration(manifest, config):
    try:
        _validate(manifest, config)
    except (KeyError, TypeError, OverflowError) as exc:
        raise ValueError(f"invalid manifest fields/types: {exc}") from exc


def _validate(manifest, config):
    shape(manifest, ("schema", "run", "condition_id", "scope", "producer_versions",
        "stage1_source_commit", "stage1_source_review", "config", "traces", "ports", "lines",
        "timing_mode", "sampling", "clocks", "windows", "required_cells", "streams", "capacity",
        "identity_profile", "offloads", "calibration_path", "retention"), ("verification",))
    if manifest["schema"] != "hng-g2-manifest-v1" or manifest["scope"] not in {"software-only", "physical"}:
        raise ValueError("unsupported manifest schema/scope")
    for key in ("run", "condition_id", "stage1_source_review"):
        identifier(manifest[key], key)
    if manifest["timing_mode"] not in {"stress", "calibration"}:
        raise ValueError("unsupported timing mode")
    verification = manifest.get('verification')
    if verification is not None:
        shape(verification, ('mode','seqchecks'))
        if verification['mode'] != 'sampled' or not isinstance(verification['seqchecks'],list):
            raise ValueError('unsupported verification mode/seqchecks')
        for pair in verification['seqchecks']:
            shape(pair, ('path','sha256','ingress_port','egress_port','ingress_path','receiver_path'))
            digest(pair['sha256'])
            for field in ('path','ingress_path','receiver_path'): identifier(pair[field], field)
            for field in ('ingress_port','egress_port'): uint(pair[field],field,65535)
    pairs, egress_sources = set(), {}
    for port in _array(manifest["ports"], "ports"):
        shape(port, ("ingress_port", "egress_port", "link_bps", "queue_id", "rx_queue_ids", "tx_queue_ids"))
        for key in ("ingress_port", "egress_port", "queue_id"):
            uint(port[key], key, 65535)
        if not uint(port["link_bps"], "link_bps"):
            raise ValueError("link rate must be positive")
        for key in ("rx_queue_ids", "tx_queue_ids"):
            queues = _array(port[key], key)
            for queue in queues:
                uint(queue, key, 65535)
            if not queues or len(queues) != len(set(queues)):
                raise ValueError("queue layout must be nonempty and unique")
        if port["queue_id"] not in port["tx_queue_ids"]:
            raise ValueError("TX queue is absent from its layout")
        pair = port["ingress_port"], port["egress_port"]
        if pair in pairs:
            raise ValueError("duplicate port mapping")
        pairs.add(pair)
        if pair[1] in egress_sources and egress_sources[pair[1]] != pair[0]:
            raise ValueError("many_ingress_to_one_egress requires a versioned total order")
        egress_sources[pair[1]] = pair[0]
    expected = {line["name"]: line for line in config["lines"]}
    registered = {}
    for line in _array(manifest["lines"], "lines"):
        shape(line, ("name", "ingress_port", "egress_port", "vlan_id", "apps"))
        name = identifier(line["name"], "line")
        for key in ("ingress_port", "egress_port", "vlan_id"):
            uint(line[key], key, 4094 if key == "vlan_id" else 65535)
        if name in registered or name not in expected or any(
                line[key] != expected[name][key] for key in ("ingress_port", "egress_port", "vlan_id")):
            raise ValueError("line/VLAN registration disagrees with config")
        if (line["ingress_port"], line["egress_port"]) not in pairs:
            raise ValueError("line has no registered port mapping")
        _labels(line["apps"], "app labels")
        registered[name] = line
    if set(registered) != set(expected) or pairs != {
            (line["ingress_port"], line["egress_port"]) for line in expected.values()}:
        raise ValueError("registration must cover exactly the configured lines and ports")
    sampling = manifest["sampling"]
    shape(sampling, ("algorithm", "seed", "mask_bits", "denominator", "oversample_apps"))
    uint(sampling["seed"], "sample seed")
    uint(sampling["mask_bits"], "main mask bits", 10)
    uint(sampling["denominator"], "main denominator")
    if (sampling["algorithm"] != "hng-g2-timing-v1" or sampling["mask_bits"] != 10
            or sampling["denominator"] != 1024):
        raise ValueError("main sampling must register hng-g2-timing-v1 at 1/1024")
    apps = {app for line in registered.values() for app in line["apps"]}
    if set(_labels(sampling["oversample_apps"], "oversample apps")) - apps:
        raise ValueError("unregistered extra app")
    clocks = manifest["clocks"]
    shape(clocks, ("engine", "observation"))
    identifier(clocks["engine"], "engine clock")
    if not isinstance(clocks["observation"], dict) or set(clocks["observation"]) != {str(p[0]) for p in pairs}:
        raise ValueError("observation clocks must cover every direction")
    for clock in clocks["observation"].values():
        identifier(clock, "observation clock")
    windows = {}
    for window in _array(manifest["windows"], "windows"):
        shape(window, ("phase", "start_ns", "end_ns", "clock_id"))
        if window["phase"] not in {"fill", "steady", "transition", "drain"} or window["phase"] in windows:
            raise ValueError("unknown/duplicate phase")
        uint(window["start_ns"], "window start")
        uint(window["end_ns"], "window end")
        if window["end_ns"] <= window["start_ns"] or window["clock_id"] != clocks["engine"]:
            raise ValueError("invalid window interval/clock")
        windows[window["phase"]] = window
    ordered = sorted(windows.values(), key=lambda row: row["start_ns"])
    if not ordered or any(a["end_ns"] > b["start_ns"] for a,b in zip(ordered, ordered[1:])):
        raise ValueError("windows must be nonempty and nonoverlapping")
    cells, coverage = set(), set()
    for cell in _array(manifest["required_cells"], "required cells"):
        shape(cell, ("condition_id", "repeat", "direction", "line", "phase", "mask_bits", "planned_survivors"),
              ("pooled_repeats", "required", "trace_segment"))
        if (cell["condition_id"] != manifest["condition_id"] or cell["line"] not in registered
                or cell["direction"] != str(registered[cell["line"]]["ingress_port"])
                or cell["phase"] not in windows):
            raise ValueError("cell condition/direction/line/phase is not registered")
        key = cell["repeat"], cell["direction"], cell["line"], cell["phase"]
        if key in cells:
            raise ValueError("duplicate required cell")
        cells.add(key)
        coverage.add((cell["line"], cell["phase"]))
        if cell["repeat"] == "pooled":
            repeats = _array(cell.get("pooled_repeats"), "pooled repeats")
            for repeat in repeats:
                uint(repeat, "pooled repeat")
            if len(repeats) < 2 or len(set(repeats)) != len(repeats):
                raise ValueError("pooled row needs at least two distinct repeats")
        else:
            uint(cell["repeat"], "repeat")
            if "pooled_repeats" in cell:
                raise ValueError("only pooled rows register pooled repeats")
        mask = uint(cell["mask_bits"], "cell mask bits", 10)
        planned = uint(cell["planned_survivors"], "planned survivors")
        if "required" in cell:
            boolean(cell["required"], "required cell")
        if "trace_segment" in cell:
            identifier(cell["trace_segment"], "trace segment")
        if planned and cell.get("required", True) and planned < planning_target(mask):
            raise ValueError("insufficient_planned_samples")
        if verification is None and planned and cell.get("required", True) and mask != max(
                candidate for candidate in range(11) if planned >= planning_target(candidate)):
            raise ValueError("sampling_plan_mask must use the largest qualifying mask")
    if coverage != {(line, phase) for line in registered for phase in windows}:
        raise ValueError("missing required cell registration")
    shape(manifest["retention"], ("mode", "debug_windows", "mismatch_cap"))
    _array(manifest["retention"]["debug_windows"], "debug windows")
    Retention(manifest)
    debug_cells = set()
    for window in manifest["retention"]["debug_windows"]:
        shape(window, ("phase", "start_ns", "end_ns"), ("direction", "line"))
        phase = windows.get(window["phase"])
        if phase is None or not phase["start_ns"] <= window["start_ns"] < window["end_ns"] <= phase["end_ns"]:
            raise ValueError("debug window is outside its registered phase")
        matches = {(str(line["ingress_port"]), name, window["phase"])
                   for name,line in registered.items()
                   if window.get("direction", str(line["ingress_port"])) == str(line["ingress_port"])
                   and window.get("line", name) == name}
        if not matches or debug_cells & matches:
            raise ValueError("debug window has unknown/duplicate cells")
        debug_cells.update(matches)
    if not isinstance(manifest["stage1_source_commit"], str) or not re.fullmatch(r"[0-9a-f]{40}", manifest["stage1_source_commit"]):
        raise ValueError("stage1_source_commit must be a complete commit")
    versions = manifest["producer_versions"]
    shape(versions, ("host", "core", "oracle", "reporter", "capture", "dependencies"))
    for key in ("host", "core", "oracle", "reporter", "capture"):
        identifier(versions[key], key)
    if not isinstance(versions["dependencies"], dict) or not versions["dependencies"]:
        raise ValueError("dependency versions are required")
    for name,value in versions["dependencies"].items():
        identifier(name, "dependency")
        identifier(value, "dependency version")
    shape(manifest["config"], ("path", "sha256"))
    identifier(manifest["config"]["path"], "config path")
    digest(manifest["config"]["sha256"])
    trace_hashes = {}
    for trace in _array(manifest["traces"], "traces"):
        shape(trace, ("line", "path", "sha256", "provenance"))
        for key in ("line", "path", "provenance"):
            identifier(trace[key], key)
        digest(trace["sha256"])
        if trace["line"] in trace_hashes:
            raise ValueError("duplicate trace registration")
        trace_hashes[trace["line"]] = trace["sha256"]
    if trace_hashes != {name:line["trace"]["sha256"].lower() for name,line in expected.items() if line["trace"]}:
        raise ValueError("trace registration disagrees with config")
    ids, paths, roles = set(), set(), set()
    for stream in _array(manifest["streams"], "streams"):
        shape(stream, ("role", "stream_id", "path", "file_sha256"))
        for key in ("stream_id", "path"):
            identifier(stream[key], key)
        digest(stream["file_sha256"])
        if stream["role"] not in ROLES or stream["stream_id"] in ids or stream["path"] in paths:
            raise ValueError("unknown/duplicate stream registration")
        ids.add(stream["stream_id"])
        paths.add(stream["path"])
        roles.add(stream["role"])
    if roles != set(ROLES) and not (verification is not None and roles == set(ROLES)-{"expected"}):
        raise ValueError("missing required stream role")
    capacity = manifest["capacity"]
    shape(capacity, ("max_unresolved", "timing_sample_capacity", "max_diagnostics", "max_join_lag_ns",
                    "observer_ring_records", "raw_record_bytes", "join_disk_bytes", "processing_qualified",
                    "storage_qualified", "handoff_queues", "identity_index_capacity"))
    if not uint(capacity["identity_index_capacity"], "identity index capacity"):
        raise ValueError("identity index capacity must be positive")
    for key in ("max_unresolved", "timing_sample_capacity", "max_diagnostics", "max_join_lag_ns", "observer_ring_records"):
        if not uint(capacity[key], key):
            raise ValueError(f"{key} must be positive")
    uint(capacity["join_disk_bytes"], "join disk bytes")
    for key in ("processing_qualified", "storage_qualified"):
        boolean(capacity[key], key)
    shape(capacity["raw_record_bytes"], ("ingress", "before", "receiver", "host"))
    for role in ("ingress", "before", "receiver", "host"):
        if not uint(capacity["raw_record_bytes"][role], "record width"):
            raise ValueError("record width must be positive")
    queues = set()
    for queue in _array(capacity["handoff_queues"], "handoff queues"):
        shape(queue, ("handoff_queue_id", "producer", "consumer", "capacity"))
        for key in ("handoff_queue_id", "producer", "consumer"):
            identifier(queue[key], key)
        if queue["handoff_queue_id"] in queues or not uint(queue["capacity"], "handoff capacity"):
            raise ValueError("duplicate/empty handoff queue")
        for stream, _ in handoff_actors(queue).values():
            if not any(row["stream_id"] == stream and row["role"] == "host" for row in manifest["streams"]):
                raise ValueError("native actor requires a registered host stream")
        queues.add(queue["handoff_queue_id"])
    if not queues:
        raise ValueError("handoff queue registration is required")
    identity = manifest["identity_profile"]
    shape(identity, ("kind",), ("probe_port", "occurrence_policy", "policy_sha256"))
    if identity["kind"] == "probe":
        if not uint(identity.get("probe_port"), "probe port", 65535):
            raise ValueError("probe port must be positive")
    elif identity["kind"] == "packet_occurrence":
        identifier(identity.get("occurrence_policy"), "packet occurrence policy")
        digest(identity.get("policy_sha256"), "packet occurrence policy hash")
    else:
        raise ValueError("unsupported identity profile")
    offloads = manifest["offloads"]
    shape(offloads, ("fcs_present", "vlan_retained", "segments", "tso", "gso", "gro", "lro", "cqe_compression", "timestamp_mode"))
    for key in ("fcs_present", "tso", "gso", "gro", "lro"):
        if boolean(offloads[key], key):
            raise ValueError("v1 requires retained one-segment frames without coalescing/segmentation")
    if boolean(offloads["vlan_retained"], "vlan retained") is not True or uint(offloads["segments"], "segments") != 1:
        raise ValueError("v1 requires retained VLAN and one segment")
    boolean(offloads["cqe_compression"], "CQE compression")
    identifier(offloads["timestamp_mode"], "timestamp mode")
    identifier(manifest["calibration_path"], "calibration path")
