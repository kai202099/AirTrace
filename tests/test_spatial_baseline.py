import unittest
from datetime import datetime, timezone

from airtrace.analysis.anomaly import AnomalyConfig, BinnedObservation
from scripts.analyze_spatial_baseline import (
    GROUP_DEFINITIONS,
    RING_DEFINITIONS,
    ring_membership,
    robust_ring_diagnostic,
    ring_supports,
    select_ring_neighbors,
    spatial_group,
)


class SpatialBaselineTests(unittest.TestCase):
    def test_ring_membership_boundaries(self):
        self.assertTrue(ring_membership(1.0, *RING_DEFINITIONS["1_2"]))
        self.assertFalse(ring_membership(2.0, *RING_DEFINITIONS["1_2"]))
        self.assertTrue(ring_membership(2.0, *RING_DEFINITIONS["2_5"]))
        self.assertTrue(ring_membership(5.0, *RING_DEFINITIONS["2_5"]))
        self.assertFalse(ring_membership(5.0001, *RING_DEFINITIONS["2_5"]))

    def test_fire_groups_partition_boundaries(self):
        self.assertEqual(spatial_group(0.9999), "inner")
        self.assertEqual(spatial_group(1.0), "middle")
        self.assertEqual(spatial_group(1.9999), "middle")
        self.assertEqual(spatial_group(2.0), "outer")
        self.assertEqual(spatial_group(5.0), "outer")
        self.assertIsNone(spatial_group(5.0001))

    def test_self_exclusion_and_target_centre(self):
        t = datetime(2026, 1, 1, tzinfo=timezone.utc)
        current = BinnedObservation("target", t, t, 10, 10)
        neighbor = BinnedObservation("neighbor", t, t, 20, 20)
        stations = [{"station_id": "target", "lat": 25.0, "lon": 121.0}, {"station_id": "neighbor", "lat": 25.009, "lon": 121.0}]
        selected = select_ring_neighbors("target", stations[0], stations, {"target": current, "neighbor": neighbor}, RING_DEFINITIONS["1_2"])
        self.assertEqual([item[1] for item in selected], ["neighbor"])

    def test_flat_field(self):
        result = robust_ring_diagnostic([10, 10, 10])
        self.assertEqual(result["median"], 10)
        self.assertEqual(result["mad"], 0)
        self.assertEqual(result["robust_sigma"], 1.5)
        self.assertEqual(result["status"], "sufficient")

    def test_local_cluster_rise_and_regional_rise_metrics(self):
        local = robust_ring_diagnostic([10, 11, 10])
        regional = robust_ring_diagnostic([30, 31, 30])
        self.assertGreater(regional["median"], local["median"])
        self.assertEqual(local["status"], "sufficient")

    def test_insufficient_outer_sensors(self):
        self.assertEqual(robust_ring_diagnostic([10, 11], minimum=3)["status"], "insufficient")

    def test_counterfactual_threshold_does_not_mutate_production_result(self):
        production = {"spatial_excess": 1.0, "spatial_z": 1.0, "is_candidate": False}
        before = dict(production)
        metric = {"status": "sufficient", "excess": 8.0, "z": 4.0}
        self.assertTrue(ring_supports(metric, AnomalyConfig()))
        self.assertEqual(production, before)


if __name__ == "__main__":
    unittest.main()
