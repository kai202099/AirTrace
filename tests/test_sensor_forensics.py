from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from scripts.analyze_sensor_forensics import (
    classify_event,
    classify_sensor,
    classify_spike_morphology,
    distance_band,
    lagged_correlations,
    parse_aqx_p13,
    prior_spike_diagnostic,
)

UTC = timezone.utc
T0 = datetime(2026, 9, 3, 1, 0, tzinfo=UTC)


def raw(values: list[float], start: datetime = T0) -> list[dict]:
    return [{"bucket": start + timedelta(minutes=3 * i), "phenomenon_time_utc": start + timedelta(minutes=3 * i), "ingested_at_utc": start + timedelta(minutes=3 * i), "pm25_ugm3": value} for i, value in enumerate(values)]


def by_bucket(rows: list[dict]) -> dict[datetime, dict]:
    return {row["bucket"]: row for row in rows}


class SensorForensicsTests(unittest.TestCase):
    def test_single_bin_spike_classification(self):
        rows = raw([10, 10, 10, 25, 10, 10])
        result = classify_spike_morphology(rows, T0, T0 + timedelta(minutes=18), 10)
        self.assertEqual(result["classification"], "SINGLE_BIN_SPIKE")
        self.assertTrue(result["peak_is_only_one_elevated_bin"])

    def test_sustained_rise_classification(self):
        rows = raw([10, 12, 16, 20, 23, 24])
        result = classify_spike_morphology(rows, T0, T0 + timedelta(minutes=18), 10)
        self.assertEqual(result["classification"], "SUSTAINED_RISE")
        self.assertGreater(result["max_rise_rate_ugm3_per_min"], 0)

    def test_multi_peak_classification(self):
        rows = raw([10, 15, 10, 10, 16, 10])
        self.assertEqual(classify_spike_morphology(rows, T0, T0 + timedelta(minutes=18), 10)["classification"], "MULTI_PEAK")

    def test_distance_bands(self):
        self.assertTrue(distance_band(0.05, ("within_100m", 0, 0.1, False)))
        self.assertTrue(distance_band(0.1, ("100_250m", 0.1, 0.25, False)))
        self.assertFalse(distance_band(0.25, ("100_250m", 0.1, 0.25, False)))

    def test_lagged_correlation(self):
        target = by_bucket(raw([1, 2, 3, 4, 5, 6, 7]))
        neighbor = by_bucket(raw([1, 2, 3, 4, 5, 6, 7]))
        result = lagged_correlations(target, neighbor, T0, T0 + timedelta(minutes=18), 0, 0)
        zero = next(item for item in result if item["lag_minutes"] == 0)
        self.assertAlmostEqual(zero["correlation"], 1.0)

    def test_prior_spike_count_and_limited_history(self):
        rows = raw([10, 10, 25, 10, 10])
        result = prior_spike_diagnostic(by_bucket(rows), T0 + timedelta(minutes=30), [], 10)
        self.assertEqual(result["status"], "LIMITED_HISTORY")
        self.assertEqual(result["isolated_spikes_over_baseline_plus_10"], 1)

    def test_prior_known_fire_interval_is_excluded(self):
        rows = raw([10, 30, 10, 40, 10], T0 - timedelta(minutes=15))
        result = prior_spike_diagnostic(by_bucket(rows), T0 + timedelta(minutes=30), [(T0 - timedelta(minutes=15), T0 - timedelta(minutes=9))], 10)
        self.assertEqual(result["prior_similar_spike_count"], 1)

    def test_historical_aq_hourly_parsing_timezone(self):
        records = [{"SiteId": "A", "ItemEngName": "PM2.5", "MonitorDate": "2026-09-03", "MonitorValue00": "4", "MonitorValue01": "5"}]
        result = parse_aqx_p13(records, datetime(2026, 9, 2, 16, tzinfo=UTC), datetime(2026, 9, 2, 18, tzinfo=UTC))
        self.assertEqual([item["pm25_ugm3"] for item in result], [4.0, 5.0])
        self.assertEqual(result[0]["publish_time_utc"].hour, 16)

    def test_no_neighbor_case_in_sensor_classification(self):
        sensor = {"peak_excess_ugm3": 20, "morphology": {"classification": "SUSTAINED_RISE"}, "corroborating_sensors_within_500m": 0, "wind_plausible": False, "prior_history": {}, "cross_sensor_artifact": {}}
        self.assertEqual(classify_sensor(sensor), "AMBIGUOUS")

    def test_narrow_response_classification(self):
        sensor = {"peak_excess_ugm3": 20, "morphology": {"classification": "SUSTAINED_RISE"}, "corroborating_sensors_within_500m": 0, "wind_plausible": True, "prior_history": {}, "cross_sensor_artifact": {}}
        self.assertEqual(classify_sensor(sensor), "NARROW_POSSIBLE_PLUME")

    def test_coherent_event_classification(self):
        sensors = [{"classification": "COHERENT_LOCAL_RESPONSE"}, {"classification": "COHERENT_LOCAL_RESPONSE"}]
        self.assertEqual(classify_event(sensors), "MULTI_SENSOR_FIRE_RESPONSE")


if __name__ == "__main__":
    unittest.main()
