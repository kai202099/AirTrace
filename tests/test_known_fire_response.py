from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from airtrace.analysis.anomaly import BinnedObservation
from scripts.analyze_known_fire_response import (
    bearing_deg,
    classify_response,
    classify_wind_relation,
    distance_bucket,
    nominal_travel_time_minutes,
    parse_event_time,
    pm_response_metrics,
    threshold_margin,
)


UTC = timezone.utc
T0 = datetime(2026, 9, 2, 17, 26, tzinfo=UTC)


def series(values: list[float], start: datetime = T0) -> list[BinnedObservation]:
    return [BinnedObservation("s", start + timedelta(minutes=3 * i), start + timedelta(minutes=3 * i), value, value) for i, value in enumerate(values)]


class KnownFireResponseTests(unittest.TestCase):
    def test_fire_timezone_conversion(self) -> None:
        self.assertEqual(parse_event_time("2026-09-03T01:26:00+08:00"), T0)

    def test_distance_buckets(self) -> None:
        self.assertEqual(distance_bucket(0.5), "0.5km")
        self.assertEqual(distance_bucket(0.51), "1km")
        self.assertEqual(distance_bucket(5.01), ">5km")
        self.assertEqual(distance_bucket(None), "unknown")

    def test_bearing_and_wind_relation(self) -> None:
        self.assertAlmostEqual(bearing_deg(25.0, 121.0, 25.01, 121.0), 0.0, delta=0.1)
        self.assertEqual(classify_wind_relation(0, 0), "downwind")
        self.assertEqual(classify_wind_relation(90, 0), "crosswind")
        self.assertEqual(classify_wind_relation(180, 0), "upwind")
        self.assertEqual(classify_wind_relation(None, 0), "unknown")

    def test_nominal_travel_time(self) -> None:
        self.assertAlmostEqual(nominal_travel_time_minutes(1.0, 2.0), 8.333333, places=5)
        self.assertIsNone(nominal_travel_time_minutes(1.0, 0.0))

    def test_threshold_margin_logic(self) -> None:
        self.assertEqual(threshold_margin(6.0, 5.0), {"value": 6.0, "threshold": 5.0, "margin": 1.0, "pass": True})
        self.assertFalse(threshold_margin(None, 5.0)["pass"])

    def test_no_sensor_case_and_flat_pm25(self) -> None:
        self.assertEqual(classify_response([]), "INSUFFICIENT_SENSOR_COVERAGE")
        result = pm_response_metrics(series([10.0] * 70), T0 + timedelta(minutes=60), T0 + timedelta(minutes=90))
        self.assertAlmostEqual(result["absolute_delta"], 0.0)
        self.assertAlmostEqual(result["relative_delta_percent"], 0.0)

    def test_clear_synthetic_fire_response(self) -> None:
        responses = [
            {"peak_excess_over_baseline": 12.0, "wind_relation": "downwind"},
            {"peak_excess_over_baseline": 9.0, "wind_relation": "downwind"},
        ]
        self.assertEqual(classify_response(responses), "CLEAR_SENSOR_RESPONSE")

    def test_regional_rise_ambiguity(self) -> None:
        responses = [
            {"peak_excess_over_baseline": 5.0, "wind_relation": "downwind"},
            {"peak_excess_over_baseline": 4.0, "wind_relation": "crosswind"},
        ]
        self.assertEqual(classify_response(responses, background_deltas=[3.0, 4.0, 3.0]), "AMBIGUOUS_REGIONAL_BACKGROUND")


if __name__ == "__main__":
    unittest.main()
