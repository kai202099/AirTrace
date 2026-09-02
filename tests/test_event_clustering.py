import unittest
from datetime import datetime, timedelta, timezone

from airtrace.analysis.events import EventConfig, cluster_event_results, spatial_clusters_for_bin


UTC = timezone.utc
T0 = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)


def row(station_id, lat, lon, *, candidate=True, score=6.0, pm25=30.0):
    return {
        "station_id": station_id, "lat": lat, "lon": lon, "is_candidate": candidate,
        "anomaly_score": score, "temporal_excess": 8.0 if candidate else 4.0,
        "spatial_excess": 8.0 if candidate else 4.0, "temporal_status": "sufficient",
        "spatial_status": "sufficient", "smoothed_pm25": pm25, "raw_pm25": pm25,
        "quality_flags": "",
    }


def result(bin_time, rows):
    class FakeResult:
        pass
    fake = FakeResult()
    fake.rows = rows
    fake.context_sensors = []
    fake.payload = {"analysis_bin_start_utc": bin_time.isoformat().replace("+00:00", "Z"), "config": {}}
    return fake


def run(bins):
    return cluster_event_results([result(T0 + timedelta(minutes=3 * index), rows) for index, rows in enumerate(bins)])


class EventClusteringTests(unittest.TestCase):
    def test_no_anomaly(self):
        output = run([[row("s1", 25.0, 121.0, candidate=False), row("s2", 25.0001, 121.0001, candidate=False)]])
        self.assertEqual(output["events"], [])

    def test_isolated_single_spike_is_not_strong_persistent(self):
        output = run([[row("s1", 25.0, 121.0, score=9.0)]])
        self.assertEqual(len(output["events"]), 1)
        self.assertEqual(output["events"][0]["event_status"], "isolated")
        self.assertLess(output["events"][0]["event_strength"], 0.35)

    def test_stationary_local_event(self):
        sensors = [row("s1", 25.0, 121.0), row("s2", 25.0002, 121.0002), row("s3", 24.9998, 120.9998)]
        output = run([sensors, sensors, sensors])
        self.assertEqual(len(output["events"]), 1)
        self.assertEqual(output["events"][0]["bins_seen"], 3)
        self.assertEqual(output["events"][0]["event_status"], "active")

    def test_moving_event_links_and_path(self):
        bins = []
        for index in range(3):
            lon = 121.0 + index * 0.004
            bins.append([row("s1", 25.0, lon), row("s2", 25.0002, lon + 0.0002)])
        output = run(bins)
        event = output["events"][0]
        self.assertEqual(len(output["events"]), 1)
        self.assertEqual(len(event["centroid_path"]), 3)
        self.assertGreater(event["centroid_path"][-1]["displacement_km"], 0.1)

    def test_two_simultaneous_distant_events(self):
        output = run([[row("a1", 25.0, 121.0), row("a2", 25.0002, 121.0002), row("b1", 25.0, 121.05), row("b2", 25.0002, 121.0502)]])
        self.assertEqual(len(output["events"]), 2)

    def test_one_missing_bin_links(self):
        sensors = [row("s1", 25.0, 121.0), row("s2", 25.0002, 121.0002)]
        output = run([sensors, [], sensors])
        self.assertEqual(len(output["events"]), 1)
        self.assertEqual(output["events"][0]["bins_seen"], 2)
        self.assertEqual(output["events"][0]["gap_bins"], 1)

    def test_long_gap_ends_event(self):
        sensors = [row("s1", 25.0, 121.0), row("s2", 25.0002, 121.0002)]
        output = run([sensors, [], [], sensors])
        self.assertEqual(len(output["events"]), 2)
        self.assertIn(output["events"][0]["event_status"], {"transient", "ended"})

    def test_regional_rise_does_not_create_local_events(self):
        rows = [row(f"s{index}", 25.0 + index * 0.001, 121.0 + index * 0.001, candidate=False, score=0.1) for index in range(8)]
        output = run([rows])
        self.assertEqual(output["events"], [])

    def test_support_only_cluster_is_not_event(self):
        rows = [row("s1", 25.0, 121.0, candidate=False), row("s2", 25.0002, 121.0002, candidate=False)]
        self.assertTrue(spatial_clusters_for_bin(rows, T0)[0]["event_eligible"] is False)
        self.assertEqual(run([rows])["events"], [])

    def test_two_clusters_merge_with_primary_earliest(self):
        first = [[row("a", 25.0, 121.0, score=9), row("b", 25.0, 121.014, score=9)]]
        second = [[row("c", 25.0, 121.007, score=9), row("d", 25.0002, 121.0072, score=9)]]
        output = run(first + second)
        self.assertEqual(len(output["events"]), 2)
        self.assertEqual(output["events"][1]["merged_into"], output["events"][0]["event_id"])

    def test_one_cluster_splits_deterministically(self):
        first = [row("a", 25.0, 121.0), row("b", 25.0002, 121.0002), row("c", 25.0, 121.004)]
        second = [row("a", 25.0, 121.0), row("b", 25.0002, 121.0002), row("c", 25.0, 121.008), row("d", 25.0002, 121.0082)]
        output = run([first, second])
        self.assertEqual(len(output["events"]), 2)
        self.assertEqual(output["events"][1]["split_from"], output["events"][0]["event_id"])


if __name__ == "__main__":
    unittest.main()
