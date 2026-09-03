from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import duckdb

from airtrace.analysis.anomaly import (
    AnomalyConfig,
    CONTEXT_DIAGNOSTIC_REPLAY,
    DetectionResult,
    NOT_PRODUCTION_EVENT_DETECTION,
    detect_anomalies,
    write_csv,
    write_json,
    write_map,
)
from airtrace.analysis.events import (
    build_event_payload,
    write_event_csv,
    write_event_json,
    write_event_map,
    write_event_timeline,
    write_membership_csv,
)


ROOT = Path(__file__).resolve().parents[1]
UTC = timezone.utc
AT = datetime(2026, 9, 2, 20, 30, tzinfo=UTC)


def load_script(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / filename)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def make_fixture_database(path: Path) -> None:
    connection = duckdb.connect(str(path))
    connection.execute("CREATE TABLE sensor_station (thing_id VARCHAR, station_id VARCHAR, station_name VARCHAR, lat DOUBLE, lon DOUBLE, city VARCHAR, township VARCHAR, area_type VARCHAR)")
    connection.execute("CREATE TABLE pm25_observation (station_id VARCHAR, datastream_id VARCHAR, phenomenon_time_utc TIMESTAMPTZ, pm25_ugm3 DOUBLE, source_status VARCHAR, quality_flags VARCHAR)")
    stations = [
        ("t-core-1", "core-1", "Core 1", 25.0600, 121.4500, "x", "x", "x"),
        ("t-core-2", "core-2", "Core 2", 25.0610, 121.4510, "x", "x", "x"),
        ("t-core-3", "core-3", "Core 3", 25.0620, 121.4520, "x", "x", "x"),
        ("t-context-target", "context-target", "Context target", 25.1000, 121.5000, "x", "x", "x"),
        ("t-context-1", "context-1", "Context 1", 25.1005, 121.5000, "x", "x", "x"),
        ("t-context-2", "context-2", "Context 2", 25.1000, 121.5005, "x", "x", "x"),
        ("t-context-3", "context-3", "Context 3", 25.1005, 121.5005, "x", "x", "x"),
        ("t-outside", "outside", "Outside", 25.2000, 121.5000, "x", "x", "x"),
    ]
    connection.executemany("INSERT INTO sensor_station VALUES (?, ?, ?, ?, ?, ?, ?, ?)", stations)
    rows = []
    station_ids = [station[1] for station in stations]
    current = AT - timedelta(hours=2)
    while current <= AT:
        for station_id in station_ids:
            if station_id == "context-target" and current >= AT - timedelta(minutes=6):
                value = 30.0
            elif station_id.startswith("context-") and current >= AT - timedelta(minutes=6):
                value = 20.0
            elif station_id == "outside" and current >= AT - timedelta(minutes=6):
                value = 100.0
            else:
                value = 10.0
            rows.append((station_id, f"ds-{station_id}", current, value, "fresh", ""))
        current += timedelta(minutes=3)
    connection.executemany("INSERT INTO pm25_observation VALUES (?, ?, ?, ?, ?, ?)", rows)
    connection.close()


class AnalysisZoneTests(unittest.TestCase):
    def test_cli_defaults_are_core_and_context_is_opt_in(self) -> None:
        anomaly_cli = load_script("detect_pm25_anomalies_cli", "detect_pm25_anomalies.py")
        events_cli = load_script("cluster_pm25_events_cli", "cluster_pm25_events.py")
        with patch.object(sys, "argv", ["detect_pm25_anomalies.py"]):
            self.assertEqual(anomaly_cli.parse_args().analysis_zone, "core")
        with patch.object(sys, "argv", ["detect_pm25_anomalies.py", "--analysis-zone", "context"]):
            self.assertEqual(anomaly_cli.parse_args().analysis_zone, "context")
        with patch.object(sys, "argv", ["cluster_pm25_events.py"]):
            self.assertEqual(events_cli.parse_args().analysis_zone, "core")
        with patch.object(sys, "argv", ["cluster_pm25_events.py", "--analysis-zone", "context"]):
            self.assertEqual(events_cli.parse_args().analysis_zone, "context")

    def test_core_default_equals_explicit_core_and_context_is_bbox_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "fixture.duckdb"
            make_fixture_database(database)
            kwargs = dict(database_path=database, config_path=ROOT / "config" / "pilot_region.json", cutoff=AT, lookback_hours=2.0, now=AT)
            default = detect_anomalies(**kwargs)
            core = detect_anomalies(**kwargs, analysis_zone="core")
            context = detect_anomalies(**kwargs, analysis_zone="context")
            self.assertEqual(default.rows, core.rows)
            self.assertEqual(default.payload["config"], core.payload["config"])
            self.assertEqual({row["station_id"] for row in core.rows}, {"core-1", "core-2", "core-3"})
            self.assertEqual({row["station_id"] for row in context.rows}, {"core-1", "core-2", "core-3", "context-target", "context-1", "context-2", "context-3"})
            self.assertNotIn("outside", {row["station_id"] for row in context.rows})
            self.assertTrue(next(row for row in context.rows if row["station_id"] == "context-target")["is_candidate"])
            self.assertEqual(context.payload["summary"]["context_sensor_count"], 7)
            self.assertEqual(context.payload["summary"]["analysis_sensor_count"], 7)
            self.assertEqual(context.payload["config"]["temporal_excess_threshold"], AnomalyConfig().temporal_excess_threshold)
            self.assertEqual(context.payload["config"]["spatial_excess_threshold"], AnomalyConfig().spatial_excess_threshold)
            self.assertEqual(context.payload["config"]["temporal_z_threshold"], AnomalyConfig().temporal_z_threshold)
            self.assertEqual(context.payload["config"]["spatial_z_threshold"], AnomalyConfig().spatial_z_threshold)
            self.assertEqual(context.rows, detect_anomalies(**kwargs, analysis_zone="context").rows)

    def test_context_outputs_are_marked(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "fixture.duckdb"
            output = Path(directory) / "output"
            make_fixture_database(database)
            result = detect_anomalies(database, ROOT / "config" / "pilot_region.json", cutoff=AT, lookback_hours=2.0, now=AT, analysis_zone="context")
            anomaly_json, anomaly_csv, anomaly_map = output / "anomaly.json", output / "anomaly.csv", output / "anomaly.html"
            write_json(result, anomaly_json)
            write_csv(result, anomaly_csv)
            write_map(result, anomaly_map)
            self.assertEqual(json.loads(anomaly_json.read_text(encoding="utf-8"))["mode_notice"], [CONTEXT_DIAGNOSTIC_REPLAY, NOT_PRODUCTION_EVENT_DETECTION])
            self.assertIn(CONTEXT_DIAGNOSTIC_REPLAY, anomaly_csv.read_text(encoding="utf-8"))
            self.assertIn(NOT_PRODUCTION_EVENT_DETECTION, anomaly_map.read_text(encoding="utf-8"))
            payload = build_event_payload([result], ROOT / "config" / "pilot_region.json", analysis_zone="context")
            self.assertTrue(payload["events"])
            self.assertIn("context-target", {member["station_id"] for member in payload["membership"]})
            event_paths = [output / name for name in ("events.json", "events.csv", "membership.csv", "map.html", "timeline.html")]
            write_event_json(payload, event_paths[0])
            write_event_csv(payload, event_paths[1])
            write_membership_csv(payload, event_paths[2])
            write_event_map(payload, event_paths[3])
            write_event_timeline(payload, event_paths[4])
            for path in event_paths:
                self.assertIn(CONTEXT_DIAGNOSTIC_REPLAY, path.read_text(encoding="utf-8"))
                self.assertIn(NOT_PRODUCTION_EVENT_DETECTION, path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
