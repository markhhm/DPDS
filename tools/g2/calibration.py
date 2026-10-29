# SPDX-License-Identifier: Apache-2.0
"""Offline verification of retained raw clock data, using exact rational maps."""
from fractions import Fraction

from .protocol import (StreamError, boolean, digest, identifier, nullable,
                       shape, signed, timestamp_fields)
from .reference import uint, U64


class Calibration:
    def __init__(self, document, scope, run):
        self.document = document
        self.scope = scope
        self.run = run
        self.clocks, self.mappings, self.pairs = {}, {}, {}
        self._last_raw = {}
        try:
            self._validate(document)
        except (ValueError, TypeError, KeyError, OverflowError) as exc:
            raise StreamError("CALIBRATION_INVALID", str(exc), "INCONCLUSIVE") from exc

    def _validate(self, document):
        shape(document, ("schema", "calibration_id", "run", "clocks", "mappings", "pairs", "qualification"),
              ("source_versions", "firmware_versions", "driver_versions", "timestamp_points"))
        if document["schema"] != "hng-g2-calibration-v1" or self.scope not in {"software-only", "physical"}:
            raise ValueError("unsupported calibration schema/scope")
        identifier(document["calibration_id"], "calibration_id")
        identifier(document["run"], "run")
        if document["run"] != self.run:
            raise ValueError("calibration run mismatch")
        for section in ("clocks", "mappings", "pairs"):
            if not isinstance(document[section], list):
                raise ValueError(f"calibration {section} must be an array")
        for clock in document["clocks"]:
            shape(clock, ("clock_id", "kind", "unit", "width_bits", "device_token", "mode"))
            for key in ("clock_id", "device_token", "mode"):
                identifier(clock[key], key)
            if clock["kind"] not in {"engine_monotonic", "nic_rx", "nic_tx_port", "fake"} or clock["unit"] not in {"ns", "ticks"}:
                raise ValueError("unsupported clock kind/unit")
            if uint(clock["width_bits"], "clock width", 64) == 0:
                raise ValueError("clock width must be positive")
            if clock["clock_id"] in self.clocks:
                raise ValueError("duplicate clock_id")
            if self.scope == "physical" and clock["kind"] == "fake":
                raise ValueError("fake clocks are software evidence only")
            self.clocks[clock["clock_id"]] = clock
        if not self.clocks:
            raise ValueError("calibration requires clocks")
        for mapping in document["mappings"]:
            shape(mapping, ("mapping_id", "raw_clock_id", "target_clock_id", "raw_origin",
                            "target_origin_ns", "scale_num", "scale_den", "valid_from_raw",
                            "valid_to_raw", "residual_bound_ns"))
            for key in ("mapping_id", "raw_clock_id", "target_clock_id"):
                identifier(mapping[key], key)
            if mapping["mapping_id"] in self.mappings:
                raise ValueError("duplicate mapping_id")
            if any(mapping[key] not in self.clocks for key in ("raw_clock_id", "target_clock_id")):
                raise ValueError("mapping references unknown clock")
            for key in ("raw_origin", "target_origin_ns", "scale_num", "scale_den",
                        "valid_from_raw", "valid_to_raw", "residual_bound_ns"):
                uint(mapping[key], key)
            if not mapping["scale_num"] or not mapping["scale_den"]:
                raise ValueError("mapping scale must be positive")
            if mapping["valid_from_raw"] >= mapping["valid_to_raw"]:
                raise ValueError("mapping validity interval must be nonempty and half-open")
            maximum = (1 << self.clocks[mapping["raw_clock_id"]]["width_bits"])-1
            if mapping["raw_origin"] > maximum or mapping["valid_to_raw"]-1 > maximum:
                raise ValueError("mapping exceeds raw clock width")
            # Adjacent anchors for one domain must preserve time; overlapping fits are ambiguous.
            for other in self.mappings.values():
                if (mapping["raw_clock_id"], mapping["target_clock_id"]) != (other["raw_clock_id"], other["target_clock_id"]):
                    continue
                left, right = sorted((mapping, other), key=lambda item:item["valid_from_raw"])
                if left["valid_to_raw"] > right["valid_from_raw"]:
                    raise ValueError("overlapping mappings for one raw clock")
                if self._mapped(left, left["valid_to_raw"]-1) > self._mapped(right, right["valid_from_raw"]):
                    raise ValueError("calibration anchors step backward")
                if left["valid_to_raw"] == right["valid_from_raw"] and abs(
                        self._mapped(left, right["valid_from_raw"])-self._mapped(right, right["valid_from_raw"])) > left["residual_bound_ns"]+right["residual_bound_ns"]:
                    raise ValueError("calibration anchors contain an unbounded clock step")
            self.mappings[mapping["mapping_id"]] = mapping
        for pair in document["pairs"]:
            shape(pair, ("pair_id", "ingress_clock_id", "egress_clock_id", "target_clock_id",
                         "offset_ns", "offset_bound_ns", "drift_bound_ns", "quantization_bound_ns",
                         "load_bound_ns", "mapping_bound_ns", "uncertainty_ns", "evidence_sha256"))
            for key in ("pair_id", "ingress_clock_id", "egress_clock_id", "target_clock_id"):
                identifier(pair[key], key)
            if pair["pair_id"] in self.pairs:
                raise ValueError("duplicate pair_id")
            if any(pair[key] not in self.clocks for key in ("ingress_clock_id", "egress_clock_id", "target_clock_id")):
                raise ValueError("pair references unknown clock")
            if any((pair["ingress_clock_id"], pair["egress_clock_id"], pair["target_clock_id"]) ==
                   (other["ingress_clock_id"], other["egress_clock_id"], other["target_clock_id"]) for other in self.pairs.values()):
                raise ValueError("ambiguous clock pair")
            signed(pair["offset_ns"], "offset_ns")
            for key in ("offset_bound_ns", "drift_bound_ns", "quantization_bound_ns",
                        "load_bound_ns", "mapping_bound_ns", "uncertainty_ns"):
                uint(pair[key], key)
            total = sum(pair[key] for key in ("offset_bound_ns", "drift_bound_ns",
                                             "quantization_bound_ns", "load_bound_ns", "mapping_bound_ns"))
            if total > U64 or pair["uncertainty_ns"] < total:
                raise ValueError("OWD uncertainty underbounds registered error terms")
            if self.scope == "physical" and pair["uncertainty_ns"] > 10000:
                raise ValueError("physical OWD uncertainty exceeds 10000 ns")
            bounds = []
            for key in ("ingress_clock_id", "egress_clock_id"):
                candidates = [mapping["residual_bound_ns"] for mapping in self.mappings.values()
                              if mapping["raw_clock_id"] == pair[key] and mapping["target_clock_id"] == pair["target_clock_id"]]
                if not candidates:
                    raise ValueError("pair lacks raw-to-target mapping")
                bounds.append(max(candidates))
            if pair["mapping_bound_ns"] < sum(bounds):
                raise ValueError("pair mapping bound underbounds its raw clock maps")
            digest(pair["evidence_sha256"], "evidence_sha256")
            self.pairs[pair["pair_id"]] = pair
        qualification = document["qualification"]
        shape(qualification, ("scope", "offered_bps", "frame_profile_sha256", "duration_ns",
                              "before_after_checked", "counter_resets", "observer_drops", "uncertainty_pass"))
        if qualification["scope"] != self.scope:
            raise ValueError("calibration qualification scope mismatch")
        for key in ("offered_bps", "duration_ns", "counter_resets", "observer_drops"):
            uint(qualification[key], key)
        digest(qualification["frame_profile_sha256"], "frame_profile_sha256")
        for key in ("before_after_checked", "uncertainty_pass"):
            if boolean(qualification[key], key) is not True:
                raise ValueError(f"calibration {key} is not verified")
        if qualification["counter_resets"] or qualification["observer_drops"]:
            raise ValueError("clock reset or observer drop prevents calibration")
        if self.scope == "physical":
            if not qualification["offered_bps"] or not qualification["duration_ns"]:
                raise ValueError("physical calibration requires actual load and duration")
            if any(not document.get(key) for key in ("source_versions", "firmware_versions", "driver_versions", "timestamp_points")):
                raise ValueError("physical calibration lacks source/settings provenance")

    @staticmethod
    def _mapped(mapping, raw):
        return mapping["target_origin_ns"] + round(Fraction(
            (raw-mapping["raw_origin"])*mapping["scale_num"], mapping["scale_den"]))

    def _map(self, timestamp, mapped_ns, chronological):
        if timestamp is None or mapped_ns is None:
            raise StreamError("TIMESTAMP_MISSING", "retained raw/mapped timestamp required", "INCONCLUSIVE")
        try:
            timestamp_fields(timestamp)
            uint(mapped_ns, "mapped timestamp")
        except (ValueError, TypeError, KeyError) as exc:
            raise StreamError("TIMESTAMP_SCHEMA", str(exc)) from exc
        mapping = self.mappings.get(timestamp["mapping_id"])
        if mapping is None or mapping["raw_clock_id"] != timestamp["clock_id"]:
            raise StreamError("CLOCK_MAPPING_MISSING", "unknown/mismatched clock mapping", "INCONCLUSIVE")
        raw = timestamp["raw"]
        clock = self.clocks[timestamp["clock_id"]]
        if raw >= 1 << clock["width_bits"]:
            raise StreamError("CLOCK_WIDTH", "raw timestamp exceeds clock width", "INCONCLUSIVE")
        if not mapping["valid_from_raw"] <= raw < mapping["valid_to_raw"]:
            raise StreamError("CLOCK_EXTRAPOLATION", "raw timestamp is outside mapping validity", "INCONCLUSIVE")
        allowed = {"fake", "software_rx", "external_rx", "tg_rx", "tg_tx_port"}
        if self.scope == "physical":
            allowed = {"external_rx", "tg_rx", "tg_tx_port"}
        if timestamp["point"] not in allowed or (timestamp["point"] == "fake" and clock["kind"] != "fake"):
            raise StreamError("TIMESTAMP_POINT", "unqualified timestamp boundary", "INCONCLUSIVE")
        result = self._mapped(mapping, raw)
        if not 0 <= result <= U64:
            raise StreamError("CLOCK_RANGE", "mapped timestamp is outside uint64", "INCONCLUSIVE")
        if result != mapped_ns:
            raise StreamError("TIMESTAMP_MISMATCH", "retained raw time does not match mapped timestamp")
        if chronological:
            prior = self._last_raw.get(timestamp["clock_id"])
            if prior is not None and raw < prior:
                raise StreamError("CLOCK_BACKWARD", "raw clock stepped backward/reset/wrapped", "INCONCLUSIVE")
            self._last_raw[timestamp["clock_id"]] = raw
        return result, mapping

    def map_timestamp(self, timestamp, mapped_ns):
        """Validate in capture order; equal raw times are allowed, resets are not."""
        return self._map(timestamp, mapped_ns, chronological=True)[0]

    def pair(self, before, receive):
        """Join order is independent of capture order; retain uncorrected OWD."""
        if not isinstance(before, dict) or not isinstance(receive, dict):
            raise StreamError("TIMESTAMP_MISSING", "before and receive records required", "INCONCLUSIVE")
        first, before_map = self._map(before.get("timestamp"), before.get("ingress_ts_ns"), chronological=False)
        last, after_map = self._map(receive.get("timestamp"), receive.get("receive_ns"), chronological=False)
        if before["timestamp"]["point"] not in {"external_rx", "tg_tx_port", "fake"} or receive["timestamp"]["point"] not in {"external_rx", "tg_rx", "software_rx", "fake"}:
            raise StreamError("TIMESTAMP_POINT", "before/receive timestamp boundary mismatch", "INCONCLUSIVE")
        candidates = [pair for pair in self.pairs.values()
                      if pair["ingress_clock_id"] == before["timestamp"]["clock_id"]
                      and pair["egress_clock_id"] == receive["timestamp"]["clock_id"]
                      and pair["target_clock_id"] == before_map["target_clock_id"] == after_map["target_clock_id"]]
        if len(candidates) != 1:
            raise StreamError("CLOCK_PAIR_MISSING", "no unique calibrated common-clock pair", "INCONCLUSIVE")
        pair = candidates[0]
        actual_mapping_bound = sum(max(mapping["residual_bound_ns"], record["timestamp"]["uncertainty_ns"])
                                   for mapping, record in ((before_map,before),(after_map,receive)))
        if actual_mapping_bound > pair["mapping_bound_ns"]:
            raise StreamError("CLOCK_UNCERTAINTY", "retained timestamps exceed calibrated mapping bound", "INCONCLUSIVE")
        raw_owd = last-first
        return dict(raw_owd_ns=raw_owd, owd_ns=raw_owd-pair["offset_ns"],
                    uncertainty_ns=pair["uncertainty_ns"], pair_id=pair["pair_id"],
                    target_clock_id=pair["target_clock_id"])
