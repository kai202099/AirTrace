from __future__ import annotations

import unittest

from airtrace.analysis.anomaly import (
    AnomalyConfig,
    candidate_from_metrics,
    haversine_km,
    robust_stats,
    synthetic_metrics,
)


class AnomalyMathTests(unittest.TestCase):
    def test_flat_history_is_not_candidate(self) -> None:
        result = synthetic_metrics(20.1, [20.0] * 12, [20.0, 20.1, 19.9])
        self.assertAlmostEqual(result["temporal_excess"], 0.1)
        self.assertFalse(result["is_candidate"])

    def test_isolated_sensor_spike_is_spatially_unsupported(self) -> None:
        result = synthetic_metrics(35.0, [20.0] * 12, [20.0, 20.1, 19.9], neighbor_histories=[[20.0] * 12] * 3)
        self.assertGreater(result["temporal_z"], 3)
        self.assertGreater(result["spatial_z"], 3)
        self.assertFalse(result["is_candidate"], "local candidate requires a supported spatial excess")
        self.assertEqual(result["spatial_support_count"], 0)

    def test_local_multi_sensor_rise_is_candidate(self) -> None:
        result = synthetic_metrics(35.0, [20.0] * 12, [25.0, 25.2, 24.8], neighbor_histories=[[20.0] * 12] * 3)
        self.assertTrue(result["is_candidate"])
        self.assertGreater(result["anomaly_score"], 3)

    def test_regional_rise_has_small_spatial_excess(self) -> None:
        result = synthetic_metrics(35.0, [20.0] * 12, [35.0, 35.2, 34.8])
        self.assertGreater(result["temporal_z"], 3)
        self.assertAlmostEqual(result["spatial_excess"], 0.0)
        self.assertFalse(result["is_candidate"])

    def test_mad_zero_uses_sigma_floor(self) -> None:
        stats = robust_stats([10.0] * 10, sigma_floor=1.5)
        self.assertEqual(stats.mad, 0.0)
        self.assertEqual(stats.sigma, 1.5)
        result = synthetic_metrics(20.0, [10.0] * 12, [10.0, 10.0, 10.0])
        self.assertTrue(result["temporal_z"] < float("inf"))

    def test_missing_neighbors_is_insufficient(self) -> None:
        result = synthetic_metrics(30.0, [20.0] * 12, [20.0, 20.0], AnomalyConfig(min_neighbors=3))
        self.assertEqual(result["spatial_status"], "insufficient_neighbors")
        self.assertFalse(result["is_candidate"])

    def test_cardinal_haversine_sanity(self) -> None:
        self.assertAlmostEqual(haversine_km((0.0, 0.0), (1.0, 0.0)), 111.195, delta=0.2)
        self.assertAlmostEqual(haversine_km((0.0, 0.0), (0.0, 1.0)), 111.195, delta=0.2)
        self.assertAlmostEqual(haversine_km((0.0, 0.0), (0.0, 0.0)), 0.0, delta=1e-9)

    def test_candidate_thresholds_are_explicit(self) -> None:
        self.assertTrue(candidate_from_metrics("sufficient", "sufficient", 5, 5, 3, 3))
        self.assertFalse(candidate_from_metrics("insufficient_history", "sufficient", 100, 100, 100, 100))


if __name__ == "__main__":
    unittest.main()
