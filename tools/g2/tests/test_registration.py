# SPDX-License-Identifier: Apache-2.0
"""Registration checks operate before capture and never open a network device."""
import copy
from fractions import Fraction
import math
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from tools.g2.tests.test_reference import config
from tools.g2.tests.test_calibration import document
from tools.g2.reference import load_config

try:
    from tools.g2.registration import validate_registration
except ImportError:
    validate_registration = None


def manifest():
    return dict(schema="hng-g2-manifest-v1", run="demo", condition_id="fixture", scope="software-only",
        producer_versions=dict(host="synthetic", core="synthetic", oracle="synthetic",
                               reporter="synthetic", capture="synthetic", dependencies=dict(python="3")),
        stage1_source_commit="de035a63f5d0135ee70b0bceb27d5051897b85a1",
        stage1_source_review="pending second review", config=dict(path="engine.json", sha256="a"*64), traces=[],
        ports=[dict(ingress_port=0, egress_port=1, link_bps=8_000_000_000, queue_id=0,
                    rx_queue_ids=[0], tx_queue_ids=[0])],
        lines=[dict(name="cable", ingress_port=0, egress_port=1, vlan_id=100, apps=["app-a"])],
        timing_mode="stress", sampling=dict(algorithm="hng-g2-timing-v1", seed=17, mask_bits=10,
                                           denominator=1024, oversample_apps=["app-a"]),
        clocks=dict(engine="engine", observation={"0":"obs"}),
        windows=[dict(phase="steady", start_ns=0, end_ns=10**9, clock_id="engine")],
        required_cells=[dict(condition_id="fixture", repeat=0, direction="0", line="cable", phase="steady",
                             mask_bits=10, planned_survivors=440_433)],
        streams=[dict(role=role, stream_id=role, path=f"{role}.jsonl", file_sha256="b"*64)
                 for role in ("ingress", "before", "receiver", "host", "counters", "expected")],
        capacity=dict(max_unresolved=4096, timing_sample_capacity=10000, max_diagnostics=64,
                      max_join_lag_ns=10**9, observer_ring_records=4096,
                      raw_record_bytes=dict(ingress=64, before=64, receiver=64, host=128),
                      join_disk_bytes=0, processing_qualified=False, storage_qualified=False,
                      handoff_queues=[dict(handoff_queue_id="h", producer="scheduler", consumer="tx", capacity=4096)]),
        identity_profile=dict(kind="probe", probe_port=9000),
        offloads=dict(fcs_present=False, vlan_retained=True, segments=1, tso=False, gso=False,
                      gro=False, lro=False, cqe_compression=False, timestamp_mode="fake"),
        calibration_path="calibration.json",
        retention=dict(mode="stream", debug_windows=[], mismatch_cap=64))


class RegistrationTests(unittest.TestCase):
    def assert_invalid(self, registration, configuration=None, reason=None):
        with self.assertRaisesRegex(ValueError, reason or ".+"):
            validate_registration(registration, config() if configuration is None else configuration)

    def test_accepts_registered_mapping_windows_and_default_sample_plan(self):
        self.assertTrue(callable(validate_registration), "v1 registration validator is missing")
        registration = manifest()
        original = copy.deepcopy(registration)
        validate_registration(registration, config())
        self.assertEqual(registration, original)

    def test_raw_mapping_queue_layout_and_apps_match_registered_config(self):
        changes = [lambda m: m["lines"][0].update(vlan_id=101),
                   lambda m: m["lines"].append(copy.deepcopy(m["lines"][0])),
                   lambda m: m["ports"][0].update(egress_port=0),
                   lambda m: m["ports"][0].update(link_bps=True),
                   lambda m: m["ports"][0].update(rx_queue_ids=[0,0]),
                   lambda m: m["ports"][0].update(tx_queue_ids=[]),
                   lambda m: m["lines"][0].update(apps=["app-a","app-a"]),
                   lambda m: m["lines"][0].update(typo=1)]
        for change in changes:
            registration = manifest()
            change(registration)
            with self.subTest(change=change):
                self.assert_invalid(registration)

    def test_many_ingress_sources_cannot_invent_a_total_order_on_one_egress(self):
        registration, configuration = manifest(), config()
        reverse = copy.deepcopy(configuration["lines"][0])
        reverse.update(name="other", ingress_port=1)
        configuration["lines"].append(reverse)
        registration["lines"].append(dict(registration["lines"][0], name="other", ingress_port=1))
        registration["ports"].append(dict(registration["ports"][0], ingress_port=1))
        self.assert_invalid(registration, configuration, "many_ingress")

    def test_planning_target_has_spec_vector_and_exact_binomial_boundary(self):
        from tools.g2 import registration
        self.assertTrue(callable(getattr(registration, "planning_target", None)))
        self.assertEqual(registration.planning_target(0), 368)
        self.assertEqual(registration.planning_target(10), 440_433)
        target = registration.planning_target(1)
        for count, sufficient in [(target-1, False), (target, True)]:
            exact = Fraction(sum(math.comb(count, k) for k in range(368)), 2**count)
            self.assertEqual(exact <= Fraction(1, 1000), sufficient)

    def test_low_rate_cells_need_registered_oversampling_or_pooling(self):
        registration = manifest()
        registration["required_cells"][0]["planned_survivors"] = 440_432
        self.assert_invalid(registration, reason="insufficient_planned_samples")
        registration["required_cells"][0].update(mask_bits=0, planned_survivors=400)
        validate_registration(registration, config())
        registration["required_cells"][0].update(planned_survivors=367)
        self.assert_invalid(registration, reason="insufficient_planned_samples")
        registration["required_cells"][0].update(repeat="pooled", pooled_repeats=[0,1], planned_survivors=600)
        validate_registration(registration, config())
        registration["required_cells"][0]["pooled_repeats"] = [0,0]
        self.assert_invalid(registration)

    def test_sampling_plan_uses_the_largest_supported_mask(self):
        registration = manifest()
        registration["required_cells"][0]["mask_bits"] = 0
        self.assert_invalid(registration, reason="sampling_plan_mask")
        registration["required_cells"][0].update(mask_bits=9, planned_survivors=440_432)
        validate_registration(registration, config())

    def test_windows_cells_and_hash_parameters_cannot_be_ambiguous(self):
        changes = [lambda m: m["sampling"].update(seed=True),
                   lambda m: m["sampling"].update(algorithm="other"),
                   lambda m: m["sampling"].update(denominator=512),
                   lambda m: m["sampling"].update(oversample_apps=["unregistered"]),
                   lambda m: m["windows"][0].update(start_ns=1, end_ns=1),
                   lambda m: m["windows"][0].update(clock_id="obs"),
                   lambda m: m["windows"].append(dict(phase="drain", start_ns=1, end_ns=2, clock_id="engine")),
                   lambda m: m["required_cells"].append(copy.deepcopy(m["required_cells"][0])),
                   lambda m: m["required_cells"][0].update(direction="1"),
                   lambda m: m["required_cells"][0].update(phase="unknown"),
                   lambda m: m["required_cells"][0].update(condition_id="other"),
                   lambda m: m["required_cells"][0].update(mask_bits=True),
                   lambda m: m["required_cells"].clear()]
        for change in changes:
            registration = manifest()
            change(registration)
            with self.subTest(change=change):
                self.assert_invalid(registration)

    def test_debug_retention_cannot_expand_or_duplicate_a_cell_window(self):
        registration = manifest()
        registration["retention"]["debug_windows"] = [dict(phase="steady", start_ns=0, end_ns=100)]
        validate_registration(registration, config())
        changes = [lambda m: m["retention"].update(mode="other"),
                   lambda m: m["retention"]["debug_windows"][0].update(end_ns=2_000_000_001),
                   lambda m: m["retention"]["debug_windows"][0].update(phase="transition"),
                   lambda m: m["retention"]["debug_windows"][0].update(line="unknown"),
                   lambda m: m["retention"]["debug_windows"].append(dict(phase="steady", direction="0", line="cable", start_ns=100, end_ns=200)),
                   lambda m: m["retention"].update(mismatch_cap=True)]
        for change in changes:
            changed = copy.deepcopy(registration)
            change(changed)
            with self.subTest(change=change):
                self.assert_invalid(changed)
        registration["scope"] = "physical"
        registration["retention"].update(mode="full", debug_windows=[])
        registration["windows"][0]["end_ns"] = 2_000_000_001
        self.assert_invalid(registration)

    def test_sources_capacities_and_offload_settings_are_explicit(self):
        changes = [lambda m: m.update(stage1_source_commit="not-a-commit"),
                   lambda m: m["producer_versions"].pop("host"),
                   lambda m: m["config"].update(sha256="bad"),
                   lambda m: m["streams"].append(copy.deepcopy(m["streams"][0])),
                   lambda m: m["streams"].pop(),
                   lambda m: m["capacity"].update(max_unresolved=0),
                   lambda m: m["capacity"].update(timing_sample_capacity=True),
                   lambda m: m["capacity"].update(processing_qualified=1),
                   lambda m: m["capacity"]["handoff_queues"][0].update(capacity=0),
                   lambda m: m["offloads"].update(gro=True),
                   lambda m: m["offloads"].pop("timestamp_mode"),
                   lambda m: m["identity_profile"].update(probe_port=0),
                   lambda m: m.update(typo=1)]
        for change in changes:
            registration = manifest()
            change(registration)
            with self.subTest(change=change):
                self.assert_invalid(registration)

    def test_bad_types_cannot_bypass_required_sampling_or_crash_validation(self):
        changes = [lambda m: m["required_cells"][0].update(required=0, planned_survivors=1),
                   lambda m: m["required_cells"][0].update(trace_segment=False),
                   lambda m: m["required_cells"][0].update(repeat=[]),
                   lambda m: m.update(scope=[]),
                   lambda m: m["retention"].update(debug_windows=None),
                   lambda m: m["retention"].update(typo=True)]
        for change in changes:
            registration = manifest()
            change(registration)
            with self.subTest(change=change):
                self.assert_invalid(registration)

    def test_preflight_cli_verifies_sources_without_claiming_capture_or_gates(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            registration = manifest()
            raw = json.dumps(config()).encode()
            (root / "engine.json").write_bytes(raw)
            registration["config"]["sha256"] = hashlib.sha256(raw).hexdigest()
            calibration = document()
            calibration["clocks"].append(dict(clock_id="engine", kind="fake", unit="ns", width_bits=64,
                                               device_token="fake-engine", mode="synthetic"))
            (root / "calibration.json").write_text(json.dumps(calibration))
            source = root / "manifest.json"
            source.write_text(json.dumps(registration))
            command = [sys.executable, "-m", "tools.g2.validate", str(source)]
            result = subprocess.run(command, capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            output = json.loads(result.stdout)
            self.assertEqual(output["check"], "registration-preflight")
            self.assertEqual(output["sources"]["config_sha256"], registration["config"]["sha256"])
            self.assertFalse(output["g2_qualified"])
            self.assertIn("capture_integrity", output["unavailable"])
            (root / "engine.json").write_bytes(raw + b" ")
            result = subprocess.run(command, capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 2)
            self.assertIn("SOURCE_HASH_MISMATCH", json.loads(result.stdout)["reason_codes"])

    def test_config_parser_can_use_the_exact_bytes_checked_by_preflight(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "engine.json"
            path.write_text("not the checked snapshot")
            checked = json.dumps(config()).encode()
            parsed, traces = load_config(path, contents=checked)
            self.assertEqual(parsed, config())
            self.assertEqual(traces, {})

    def test_proposed_identity_index_limit_is_positive_and_explicit(self):
        registration = manifest()
        registration["capacity"]["identity_index_capacity"] = 1_000_000
        validate_registration(registration, config())
        for value in (0, True, -1):
            registration["capacity"]["identity_index_capacity"] = value
            with self.subTest(value=value):
                self.assert_invalid(registration)
