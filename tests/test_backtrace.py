import csv
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from airtrace.analysis.backtrace import BacktraceConfig, LocalMetricProjection, select_receptor_seeds, trace_event

UTC = timezone.utc
T0 = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
REGION = Path(__file__).parents[1] / "config" / "pilot_region.json"


def event_and_memberships(points, *, event_id="e"):
    event = {"event_id": event_id, "latest_centroid": {"lat": points[0][0], "lon": points[0][1]}}
    memberships = [{"event_id": event_id, "time_bin": T0.isoformat().replace("+00:00", "Z"), "station_id": f"s{i}", "role": role, "anomaly_score": 8.0, "temporal_excess": 8.0, "spatial_excess": 8.0, "pm25": 30.0, "lat": lat, "lon": lon} for i, (lat, lon, role) in enumerate(points)]
    return event, memberships


class FakeWind:
    def __init__(self, u=4.0, v=0.0, *, nearest=1.0, quality="GOOD"):
        self.u, self.v, self.nearest, self.quality = u, v, nearest, quality
        self.calls = []

    def __call__(self, lat, lon, at):
        self.calls.append((lat, lon, at))
        return {"u_east_mps": self.u, "v_north_mps": self.v, "nearest_station_km": self.nearest, "quality_category": self.quality}


def run(points, wind, **kwargs):
    event, memberships = event_and_memberships(points)
    kwargs.setdefault("residual_csv_path", Path("missing-residuals.csv"))
    config = BacktraceConfig(region_config_path=REGION, wind_getter=wind, **kwargs)
    return trace_event(event, memberships, config)


class BacktraceTests(unittest.TestCase):
    def test_local_projection_roundtrip(self):
        projection = LocalMetricProjection(25.06, 121.46)
        x, y = projection.project(25.061, 121.461)
        lat, lon = projection.inverse(x, y)
        self.assertAlmostEqual(lat, 25.061, places=9)
        self.assertAlmostEqual(lon, 121.461, places=9)

    def test_uniform_eastward_backward_moves_west(self):
        result = run([(25.06, 121.49, "seed")], FakeWind(u=4), particles_per_receptor=1, maximum_backtrace_minutes=10)
        points = result["particle_trajectories"][0]["points"]
        self.assertLess(points[-1]["lon"], points[0]["lon"])

    def test_uniform_northward_backward_moves_south(self):
        result = run([(25.09, 121.46, "seed")], FakeWind(u=0, v=4), particles_per_receptor=1, maximum_backtrace_minutes=10)
        points = result["particle_trajectories"][0]["points"]
        self.assertLess(points[-1]["lat"], points[0]["lat"])

    def test_same_seed_is_deterministic(self):
        first = run([(25.06, 121.49, "seed")], FakeWind(u=4), particles_per_receptor=5, random_seed=11)
        second = run([(25.06, 121.49, "seed")], FakeWind(u=4), particles_per_receptor=5, random_seed=11)
        self.assertEqual(first["particle_trajectories"], second["particle_trajectories"])
        self.assertEqual(first["source_evidence_grid"], second["source_evidence_grid"])

    def test_zero_residual_is_deterministic(self):
        result = run([(25.06, 121.49, "seed")], FakeWind(u=4), particles_per_receptor=4)
        endpoints = {tuple((p["lat"], p["lon"]) for p in item["points"]) for item in result["particle_trajectories"]}
        self.assertEqual(len(endpoints), 1)
        self.assertEqual(result["empirical_residual_policy"]["sample_count"], 0)

    def test_nonzero_residual_spreads_particles(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "residuals.csv"
            with path.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=["actual_u", "actual_v", "predicted_u", "predicted_v", "nearest_station_km", "predicted_quality"])
                writer.writeheader()
                writer.writerows([
                    {"actual_u": 5, "actual_v": 0, "predicted_u": 4, "predicted_v": 0, "nearest_station_km": 1, "predicted_quality": "GOOD"},
                    {"actual_u": 3, "actual_v": 0, "predicted_u": 4, "predicted_v": 0, "nearest_station_km": 1, "predicted_quality": "GOOD"},
                ])
            result = run([(25.06, 121.49, "seed")], FakeWind(u=4), particles_per_receptor=40, residual_csv_path=path)
            endpoints = {(p["points"][-1]["lat"], p["points"][-1]["lon"]) for p in result["particle_trajectories"]}
            self.assertGreater(len(endpoints), 1)

    def test_correlation_block_reuses_residual_for_ten_minutes(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "residuals.csv"
            with path.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=["actual_u", "actual_v", "predicted_u", "predicted_v", "nearest_station_km", "predicted_quality"])
                writer.writeheader()
                writer.writerow({"actual_u": 5, "actual_v": 0, "predicted_u": 4, "predicted_v": 0, "nearest_station_km": 1, "predicted_quality": "GOOD"})
                writer.writerow({"actual_u": 3, "actual_v": 0, "predicted_u": 4, "predicted_v": 0, "nearest_station_km": 1, "predicted_quality": "GOOD"})
            result = run([(25.06, 121.49, "seed")], FakeWind(u=4), particles_per_receptor=1, maximum_backtrace_minutes=21, residual_csv_path=path)
            self.assertEqual(sum(result["empirical_residual_policy"]["sampling_usage"].values()), 3)

    def test_multi_receptor_support_is_explicit(self):
        result = run([(25.0601, 121.49, "seed"), (25.0602, 121.49, "seed")], FakeWind(u=4), particles_per_receptor=2)
        self.assertEqual(max(item["receptor_support_count"] for item in result["source_evidence_grid"]), 2)
        self.assertEqual(max(item["receptor_support_fraction"] for item in result["source_evidence_grid"]), 1.0)

    def test_single_receptor_does_not_claim_multi_sensor_support(self):
        result = run([(25.06, 121.49, "seed")], FakeWind(u=4), particles_per_receptor=2)
        self.assertEqual({item["receptor_support_count"] for item in result["source_evidence_grid"]}, {1})
        self.assertEqual({item["receptor_support_fraction"] for item in result["source_evidence_grid"]}, {1.0})

    def test_seed_selection_deduplicates_station_and_prefers_seed_role(self):
        event = {"event_id": "e"}
        rows = [
            {"event_id": "e", "time_bin": "2026-01-01T12:03:00Z", "station_id": "s", "role": "support", "lat": 25, "lon": 121, "anomaly_score": 3},
            {"event_id": "e", "time_bin": "2026-01-01T12:00:00Z", "station_id": "s", "role": "seed", "lat": 25, "lon": 121, "anomaly_score": 8},
        ]
        seeds = select_receptor_seeds(event, rows)
        self.assertEqual(len(seeds), 1)
        self.assertEqual(seeds[0]["role"], "seed")
        self.assertEqual(seeds[0]["anomaly_score"], 8.0)

    def test_incompatible_receptors_have_no_false_two_support_peak(self):
        result = run([(25.06, 121.49, "seed"), (25.09, 121.49, "seed")], FakeWind(u=4), particles_per_receptor=1, maximum_backtrace_minutes=5)
        self.assertLessEqual(max(item["receptor_support_count"] for item in result["source_evidence_grid"]), 1)

    def test_domain_termination(self):
        result = run([(25.06, 121.49, "seed")], FakeWind(u=100), particles_per_receptor=1, maximum_backtrace_minutes=10, domain_buffer_km=0)
        self.assertEqual(result["particle_stats"]["termination_counts"].get("domain_limit"), 1)

    def test_unavailable_wind_does_not_abort_event(self):
        class Unavailable:
            def __call__(self, lat, lon, at):
                return {"u_east_mps": None, "v_north_mps": None, "quality_category": "INSUFFICIENT_STATIONS"}
        result = run([(25.06, 121.49, "seed"), (25.0602, 121.49, "seed")], Unavailable(), particles_per_receptor=2)
        self.assertEqual(result["particle_stats"]["termination_counts"].get("wind_unavailable"), 4)

    def test_evidence_normalization_and_support_count(self):
        result = run([(25.06, 121.49, "seed"), (25.0602, 121.49, "support")], FakeWind(u=4), particles_per_receptor=3)
        self.assertLessEqual(max(item["normalized_density"] for item in result["source_evidence_grid"]), 1)
        self.assertTrue(all(0 <= item["source_evidence_score"] <= 1 for item in result["source_evidence_grid"]))

    def test_candidate_connected_components(self):
        result = run([(25.06, 121.49, "seed")], FakeWind(u=4), particles_per_receptor=2, peak_threshold_fraction=0.9)
        self.assertGreaterEqual(len(result["candidate_source_regions"]), 1)
        self.assertEqual(result["candidate_source_regions"][0]["rank"], 1)

    def test_movement_too_small_diagnostic(self):
        event, memberships = event_and_memberships([(25.06, 121.49, "seed")])
        event["centroid_path"] = [{"time_bin": "2026-01-01T12:00:00Z", "centroid": {"lat": 25.06, "lon": 121.49}}, {"time_bin": "2026-01-01T12:03:00Z", "centroid": {"lat": 25.0601, "lon": 121.49}}]
        result = trace_event(event, memberships, BacktraceConfig(region_config_path=REGION, wind_getter=FakeWind(), residual_csv_path=Path("missing.csv"), particles_per_receptor=1, maximum_backtrace_minutes=1))
        self.assertEqual(result["wind_diagnostics"]["movement_consistency"]["status"], "MOVEMENT_TOO_SMALL")


if __name__ == "__main__":
    unittest.main()
