import math
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

import airtrace.analysis.wind as wind_module
from airtrace.analysis.wind import (
    RawWindObservation,
    StationWindUse,
    WindConfig,
    WindFieldSnapshot,
    _Snapshot,
    _Station,
    directions_from_vector,
)
from airtrace.analysis.wind_validation import (
    ValidationTarget,
    circular_angle_error,
    distance_bucket,
    evaluate_target,
    speed_bucket,
    station_count_bucket,
    summarize_rows,
)


UTC = timezone.utc
T0 = datetime(2026, 1, 1, tzinfo=UTC)


def use(station_id, lat, lon, u=3.0, v=4.0):
    speed, to, from_ = directions_from_vector(u, v)
    return StationWindUse(station_id, station_id, lat, lon, 0.0, T0, None, None, "exact", 0.0, u, v, speed, to, from_, "valid", (), 0.0)


def target(station_id="target", lat=25.0, lon=121.0, u=3.0, v=4.0, speed=5.0):
    return ValidationTarget(station_id, station_id, lat, lon, T0, u, v, speed, 216.869898, "valid")


class WindValidationTests(unittest.TestCase):
    def test_target_station_is_excluded_before_selection(self):
        stations = tuple(_Station(sid, name, lat, lon) for sid, name, lat, lon in (
            ("target", "target", 25.0, 121.0), ("a", "a", 25.001, 121.0),
            ("b", "b", 25.002, 121.0), ("c", "c", 25.003, 121.0),
        ))
        records = {sid: (RawWindObservation(sid, T0, 270.0, 5.0, 5.0, 0.0, "valid"),) for sid in ("target", "a", "b", "c")}
        field = WindFieldSnapshot(_Snapshot(stations, records, T0, T0), T0, WindConfig())
        estimate = field.estimate(25.0, 121.0, exclude_station_ids={"target"})
        self.assertNotIn("target", {item.station_id for item in estimate.stations_used})

    def test_perfect_synthetic_field_is_zero_error(self):
        stations = tuple(_Station(sid, sid, 25.0 + index * 0.001, 121.0) for index, sid in enumerate(("target", "a", "b", "c")))
        records = {sid: (RawWindObservation(sid, T0, 216.869898, 5.0, 3.0, 4.0, "valid"),) for sid in ("target", "a", "b", "c")}
        field = WindFieldSnapshot(_Snapshot(stations, records, T0, T0), T0, WindConfig())
        row = evaluate_target(target(), field.estimate(25.0, 121.0, exclude_station_ids={"target"}))
        self.assertAlmostEqual(row["vector_error_mps"], 0.0, places=6)
        self.assertAlmostEqual(row["speed_error_mps"], 0.0, places=6)
        self.assertAlmostEqual(row["direction_error_deg"], 0.0, places=5)

    def test_circular_angle_error_wraps(self):
        self.assertEqual(circular_angle_error(359.0, 1.0), 2.0)

    def test_calm_observation_does_not_force_direction_error(self):
        calm_target = target(u=0.0, v=0.0, speed=0.0)
        estimate = type("Estimate", (), {"speed_mps": 0.1, "wind_from_deg": None, "u_east_mps": 0.1, "v_north_mps": 0.0, "station_count": 3, "nearest_station_km": 2.0, "stations_used": (), "quality_category": "GOOD"})()
        row = evaluate_target(calm_target, estimate)
        self.assertIsNone(row["direction_error_deg"])

    def test_vector_rmse_math(self):
        rows = [
            {"vector_error_mps": 3.0, "speed_error_mps": 0.0, "direction_error_deg": None, "predicted_u": 1.0, "actual_u": 0.0, "predicted_v": 0.0, "actual_v": 0.0, "predicted_speed": 1.0, "actual_speed": 1.0},
            {"vector_error_mps": 4.0, "speed_error_mps": 0.0, "direction_error_deg": None, "predicted_u": 2.0, "actual_u": 0.0, "predicted_v": 0.0, "actual_v": 0.0, "predicted_speed": 2.0, "actual_speed": 2.0},
        ]
        self.assertAlmostEqual(summarize_rows(rows)["vector"]["rmse_mps"], math.sqrt(12.5), places=6)

    def test_distance_and_count_buckets(self):
        self.assertEqual(distance_bucket(3.0), "<= 3 km")
        self.assertEqual(distance_bucket(5.0), "3–5 km")
        self.assertEqual(distance_bucket(10.0), "5–10 km")
        self.assertEqual(distance_bucket(10.01), "> 10 km")
        self.assertEqual(station_count_bucket(4), "3–4")
        self.assertEqual(station_count_bucket(6), "5–6")
        self.assertEqual(station_count_bucket(8), "7–8")

    def test_insufficient_other_stations_has_no_prediction(self):
        stations = tuple(_Station(sid, sid, 25.0 + index * 0.001, 121.0) for index, sid in enumerate(("target", "a", "b")))
        records = {sid: (RawWindObservation(sid, T0, 270.0, 5.0, 5.0, 0.0, "valid"),) for sid in ("target", "a", "b")}
        field = WindFieldSnapshot(_Snapshot(stations, records, T0, T0), T0, WindConfig())
        estimate = field.estimate(25.0, 121.0, exclude_station_ids={"target"})
        self.assertEqual(estimate.quality_category, "INSUFFICIENT_STATIONS")
        self.assertIsNone(estimate.u_east_mps)

    def test_default_estimate_is_unchanged_without_exclusion(self):
        stations = tuple(_Station(sid, sid, 25.0 + index * 0.001, 121.0) for index, sid in enumerate(("a", "b", "c")))
        records = {sid: (RawWindObservation(sid, T0, 270.0, 5.0, 5.0, 0.0, "valid"),) for sid in ("a", "b", "c")}
        field = WindFieldSnapshot(_Snapshot(stations, records, T0, T0), T0, WindConfig())
        self.assertEqual(field.estimate(25.0, 121.0), field.estimate(25.0, 121.0, exclude_station_ids=None))

    def test_get_wind_default_path_remains_compatible(self):
        stations = tuple(_Station(sid, sid, 25.0 + index * 0.001, 121.0) for index, sid in enumerate(("a", "b", "c")))
        records = {sid: (RawWindObservation(sid, T0, 270.0, 5.0, 5.0, 0.0, "valid"),) for sid in ("a", "b", "c")}
        field = WindFieldSnapshot(_Snapshot(stations, records, T0, T0), T0, WindConfig())
        with patch.object(wind_module, "load_wind_snapshot", return_value=field):
            actual = wind_module.get_wind(25.0, 121.0, T0)
        self.assertEqual(actual, field.estimate(25.0, 121.0))

    def test_deterministic_repeatability(self):
        rows = [{"vector_error_mps": 1.0, "speed_error_mps": 0.5, "direction_error_deg": 2.0, "predicted_u": 1.0, "actual_u": 0.0, "predicted_v": 0.0, "actual_v": 0.0, "predicted_speed": 1.0, "actual_speed": 0.5}]
        self.assertEqual(summarize_rows(rows), summarize_rows(rows))
        self.assertEqual(speed_bucket(0.49), "calm / <0.5")


if __name__ == "__main__":
    unittest.main()
