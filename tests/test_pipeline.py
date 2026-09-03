from __future__ import annotations

import json
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from airtrace.analysis.anomaly import DetectionResult
from airtrace.analysis.backtrace import BacktraceConfig
from airtrace.pipeline import run_analysis

from tests.test_analysis_zone import AT, make_fixture_database


ROOT = Path(__file__).resolve().parents[1]


class FakeWind:
    def __call__(self, lat, lon, at):
        return {
            "u_east_mps": 1.0,
            "v_north_mps": 0.0,
            "speed_mps": 1.0,
            "wind_to_deg": 90.0,
            "wind_from_deg": 270.0,
            "quality_category": "GOOD",
            "station_count": 2,
            "nearest_station_km": 1.0,
            "farthest_station_km": 2.0,
            "vector_disagreement_mps": 0.1,
        }


def synthetic_results(groups=("a", "b")) -> list[DetectionResult]:
    rows = []
    locations = {"a": (25.06, 121.45), "b": (25.07, 121.49)}
    for prefix in groups:
        lat, lon = locations[prefix]
        for index, (dlat, dlon) in enumerate(((0.0, 0.0), (0.0002, 0.0002))):
            rows.append({
                "station_id": f"{prefix}{index}", "station_name": f"{prefix}{index}",
                "lat": lat + dlat, "lon": lon + dlon, "is_candidate": True,
                "anomaly_score": 7.0 - index, "temporal_excess": 8.0,
                "spatial_excess": 8.0, "temporal_status": "sufficient",
                "spatial_status": "sufficient", "smoothed_pm25": 30.0,
                "raw_pm25": 30.0, "quality_flags": "",
            })
    return [DetectionResult(
        payload={"analysis_bin_start_utc": "2026-09-02T20:30:00Z", "config": {}},
        rows=rows, context_sensors=[],
    )]


def trace_config(directory: Path, *, fast: bool = False, wind_getter=None) -> BacktraceConfig:
    return BacktraceConfig(
        region_config_path=ROOT / "config" / "pilot_region.json",
        residual_csv_path=directory / "missing-residuals.csv",
        particles_per_receptor=1,
        maximum_backtrace_minutes=2,
        wind_getter=wind_getter or FakeWind(),
        wind_cache_quantized=fast,
        wind_cache_spatial_m=500.0 if fast else 250.0,
    )


class PipelineTests(unittest.TestCase):
    def test_no_event_completes_and_skips_downstream_stages(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "airtrace.duckdb"
            make_fixture_database(database)
            output = run_analysis(
                AT - timedelta(minutes=9), AT, database_path=database,
                weather_database_path=root / "weather.duckdb", output_root=root / "reports",
                config_path=ROOT / "config" / "pilot_region.json", now=AT,
            )
            self.assertEqual(output["summary"]["message"], "NO EVENTS DETECTED")
            self.assertEqual(output["summary"]["stage_status"]["anomaly"], "completed")
            self.assertEqual(output["summary"]["stage_status"]["events"], "completed")
            self.assertEqual(output["summary"]["stage_status"]["backtrace"], "skipped_no_event")
            self.assertFalse(list((Path(output["output_dir"])).glob("incident_*")))

    def test_one_synthetic_event_writes_full_artifact_set(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch("airtrace.pipeline.detect_anomalies_range", return_value=synthetic_results(("a",))):
                output = run_analysis(
                    AT, AT, analysis_zone="context", database_path=root / "missing.duckdb",
                    weather_database_path=root / "weather.duckdb", output_root=root / "reports",
                    config_path=ROOT / "config" / "pilot_region.json", backtrace_config=trace_config(root),
                    facilities_database_path=root / "missing-facilities.duckdb",
                    cems_database_path=root / "missing-cems.duckdb", firms_map_key="", now=AT,
                )
            self.assertEqual(output["summary"]["event_count"], 1)
            for incident in output["incidents"]:
                artifact_dir = Path(output["output_dir"]) / f"incident_{incident['incident_id']}"
                for name in ("incident.json", "event_map.html", "source_trace.html", "evidence_map.html", "membership.csv", "source_evidence.csv", "facility_candidates.csv", "fire_candidates.csv"):
                    self.assertTrue((artifact_dir / name).exists(), name)
                self.assertEqual(incident["trace"]["status"], "TRACE_COMPLETE")
                self.assertEqual(incident["evidence"]["firms_status"], "NOT_QUERIED_MISSING_KEY")
                self.assertEqual(incident["evidence"]["cems_status"], "CEMS_CONTEXT_UNAVAILABLE")

    def test_two_events_get_deterministic_incident_ids(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch("airtrace.pipeline.detect_anomalies_range", return_value=synthetic_results()):
                output = run_analysis(AT, AT, database_path=root / "missing.duckdb", output_root=root / "reports", config_path=ROOT / "config" / "pilot_region.json", backtrace_config=trace_config(root), facilities_database_path=root / "missing-facilities.duckdb", cems_database_path=root / "missing-cems.duckdb", firms_map_key="", now=AT)
            ids = [item["incident_id"] for item in output["incidents"]]
            self.assertEqual(ids, ["AIRTRACE_20260902203000_001", "AIRTRACE_20260902203000_002"])
            self.assertTrue(all(item["event_id"] in {"evt-0001", "evt-0002"} for item in output["incidents"]))

    def test_one_trace_failure_does_not_kill_second_event(self):
        from airtrace import pipeline

        real_trace = pipeline.trace_event
        calls = {"count": 0}

        def fail_first(event, memberships, config):
            calls["count"] += 1
            if calls["count"] == 1:
                raise RuntimeError("synthetic wind failure")
            return real_trace(event, memberships, config)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch("airtrace.pipeline.detect_anomalies_range", return_value=synthetic_results()), patch("airtrace.pipeline.trace_event", side_effect=fail_first):
                output = run_analysis(AT, AT, database_path=root / "missing.duckdb", output_root=root / "reports", config_path=ROOT / "config" / "pilot_region.json", backtrace_config=trace_config(root), facilities_database_path=root / "missing-facilities.duckdb", cems_database_path=root / "missing-cems.duckdb", firms_map_key="", now=AT)
            statuses = [item["trace"]["status"] for item in output["incidents"]]
            self.assertEqual(statuses[0], "TRACE_FAILED")
            self.assertEqual(statuses[1], "TRACE_COMPLETE")
            self.assertEqual(output["summary"]["stage_status"]["evidence"], "completed")

    def test_manifest_completeness_context_and_fast_preview(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch("airtrace.pipeline.detect_anomalies_range", return_value=synthetic_results()):
                output = run_analysis(AT, AT, analysis_zone="context", database_path=root / "missing.duckdb", output_root=root / "reports", config_path=ROOT / "config" / "pilot_region.json", backtrace_config=trace_config(root), facilities_database_path=root / "missing-facilities.duckdb", cems_database_path=root / "missing-cems.duckdb", fast_preview=True, firms_map_key="", now=AT)
            report_dir = Path(output["output_dir"])
            manifest = json.loads((report_dir / "manifest.json").read_text(encoding="utf-8"))
            snapshot = json.loads((report_dir / "config_snapshot.json").read_text(encoding="utf-8"))
            self.assertTrue(manifest["diagnostic"])
            self.assertFalse(manifest["manual_diagnostic_flags"]["known_fire_validation_loaded"])
            self.assertIn("CONTEXT DIAGNOSTIC REPLAY", manifest["warnings"])
            self.assertTrue(snapshot["pilot_region"]["core_bbox"])
            self.assertTrue(manifest["produced_artifacts"]["incidents_detail"])

    def test_firms_key_is_not_persisted_in_replay_artifacts(self):
        secret = "unit-test-firms-secret"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch("airtrace.pipeline.detect_anomalies_range", return_value=synthetic_results(("a",))), patch("scripts.match_source_evidence.fetch_firms", return_value=([], {"requested": True, "errors": [], "sources": {}})):
                output = run_analysis(AT, AT, database_path=root / "missing.duckdb", output_root=root / "reports", config_path=ROOT / "config" / "pilot_region.json", backtrace_config=trace_config(root), facilities_database_path=root / "missing-facilities.duckdb", cems_database_path=root / "missing-cems.duckdb", firms_map_key=secret, now=AT)
            report_dir = Path(output["output_dir"])
            persisted = "\n".join(path.read_text(encoding="utf-8") for path in report_dir.rglob("*") if path.is_file())
            self.assertNotIn(secret, persisted)

    def test_same_inputs_have_same_run_and_incident_ids(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch("airtrace.pipeline.detect_anomalies_range", return_value=synthetic_results()):
                first = run_analysis(AT, AT, database_path=root / "missing.duckdb", output_root=root / "reports", config_path=ROOT / "config" / "pilot_region.json", backtrace_config=trace_config(root), facilities_database_path=root / "missing-facilities.duckdb", cems_database_path=root / "missing-cems.duckdb", firms_map_key="", now=AT)
                second = run_analysis(AT, AT, database_path=root / "missing.duckdb", output_root=root / "reports", config_path=ROOT / "config" / "pilot_region.json", backtrace_config=trace_config(root), facilities_database_path=root / "missing-facilities.duckdb", cems_database_path=root / "missing-cems.duckdb", firms_map_key="", now=AT)
            self.assertEqual(first["manifest"]["run_id"], second["manifest"]["run_id"])
            self.assertEqual([x["incident_id"] for x in first["incidents"]], [x["incident_id"] for x in second["incidents"]])


if __name__ == "__main__":
    unittest.main()
