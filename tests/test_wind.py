import math
import unittest
from datetime import datetime, timedelta, timezone

from airtrace.analysis.wind import (
    RawWindObservation,
    StationWindUse,
    WindConfig,
    WindFieldSnapshot,
    _Snapshot,
    _Station,
    directions_from_vector,
    haversine_km,
    interpolate_vectors,
    temporal_select,
    vector_from_wind_from,
)


UTC = timezone.utc
T0 = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)


def observation(station_id, at, *, direction=270.0, speed=1.0, status="valid", u=None, v=None):
    if u is None or v is None:
        if status == "calm":
            u, v = None, None
        elif status == "valid":
            u, v = vector_from_wind_from(direction, speed)
        else:
            u, v = None, None
    return RawWindObservation(station_id, at, direction, speed, u, v, status)


def station_use(station_id, distance, u, v, *, status="valid"):
    speed, to, from_ = directions_from_vector(u, v)
    return StationWindUse(
        station_id, station_id, 25.0, 121.0, distance, T0, None, None, "exact", 0.0,
        u, v, speed, to, from_, status, (), 0.0,
    )


def snapshot(station_specs, records):
    stations = tuple(_Station(station_id, station_id, lat, lon) for station_id, lat, lon in station_specs)
    return WindFieldSnapshot(_Snapshot(stations, records, T0, T0), T0, WindConfig())


class WindInterpolationTests(unittest.TestCase):
    def test_west_wind_points_east(self):
        u, v = vector_from_wind_from(270, 4)
        self.assertGreater(u, 0)
        self.assertAlmostEqual(v, 0, places=8)

    def test_east_wind_points_west(self):
        u, _ = vector_from_wind_from(90, 4)
        self.assertLess(u, 0)

    def test_north_wind_points_south(self):
        _, v = vector_from_wind_from(0, 4)
        self.assertLess(v, 0)

    def test_south_wind_points_north(self):
        _, v = vector_from_wind_from(180, 4)
        self.assertGreater(v, 0)

    def test_359_and_1_degree_interpolate_as_vector(self):
        selected = temporal_select([
            observation("s", T0, direction=359),
            observation("s", T0 + timedelta(minutes=10), direction=1),
        ], T0 + timedelta(minutes=5))
        self.assertIsNotNone(selected)
        speed, _, wind_from = directions_from_vector(selected.u_east_mps, selected.v_north_mps)
        self.assertAlmostEqual(speed, 1.0, places=3)
        self.assertLess(selected.v_north_mps, -0.99)
        self.assertTrue(wind_from < 1 or wind_from > 359)

    def test_idw_closer_station_dominates(self):
        u, v, _ = interpolate_vectors([station_use("near", 0.1, 4, 0), station_use("far", 10, -4, 0)])
        self.assertGreater(u, 3.9)
        self.assertAlmostEqual(v, 0, places=8)

    def test_exact_station_distance_does_not_divide_by_zero(self):
        u, v, _ = interpolate_vectors([station_use("exact", 0.0, 2, 0), station_use("other", 1.0, -2, 0)])
        self.assertTrue(math.isfinite(u))
        self.assertTrue(math.isfinite(v))
        self.assertGreater(u, 1.9)

    def test_temporal_midpoint_interpolates_u_and_v(self):
        selected = temporal_select([
            observation("s", T0, u=0, v=0),
            observation("s", T0 + timedelta(minutes=10), u=2, v=4),
        ], T0 + timedelta(minutes=5))
        self.assertEqual(selected.temporal_mode, "bracketed")
        self.assertAlmostEqual(selected.u_east_mps, 1)
        self.assertAlmostEqual(selected.v_north_mps, 2)

    def test_single_side_temporal_fallback_is_explicit(self):
        selected = temporal_select([observation("s", T0)], T0 + timedelta(minutes=5))
        self.assertEqual(selected.temporal_mode, "nearest_only")
        self.assertAlmostEqual(selected.temporal_offset_minutes, 5)

    def test_insufficient_stations_is_reported(self):
        field = snapshot(
            [("a", 25.0, 121.0), ("b", 25.001, 121.0)],
            {"a": (observation("a", T0),), "b": (observation("b", T0),)},
        )
        estimate = field.estimate(25.0, 121.0)
        self.assertEqual(estimate.quality_category, "INSUFFICIENT_STATIONS")
        self.assertIsNone(estimate.u_east_mps)

    def test_calm_surroundings_lower_direction_confidence(self):
        records = {sid: (observation(sid, T0, status="calm", speed=0),) for sid in ("a", "b", "c")}
        field = snapshot([(sid, 25.0 + i * 0.001, 121.0) for i, sid in enumerate(records)], records)
        estimate = field.estimate(25.0, 121.0)
        self.assertEqual(estimate.quality_category, "CALM / LOW_DIRECTION_CONFIDENCE")
        self.assertEqual(estimate.speed_mps, 0.0)
        self.assertIsNone(estimate.wind_from_deg)

    def test_variable_and_invalid_are_excluded(self):
        records = {
            "a": (observation("a", T0, status="variable"),),
            "b": (observation("b", T0, status="invalid"),),
            "c": (observation("c", T0),),
        }
        field = snapshot([(sid, 25.0 + i * 0.001, 121.0) for i, sid in enumerate(records)], records)
        estimate = field.estimate(25.0, 121.0)
        self.assertEqual(estimate.quality_category, "INSUFFICIENT_STATIONS")
        self.assertEqual(estimate.diagnostics["excluded_variable_or_invalid_observation_count"], 2)

    def test_vector_direction_roundtrip(self):
        for wind_from in (0, 1, 90, 180, 270, 359):
            u, v = vector_from_wind_from(wind_from, 3)
            _, _, recovered = directions_from_vector(u, v)
            error = abs((recovered - wind_from + 180) % 360 - 180)
            self.assertLess(error, 1e-8)

    def test_haversine_spatial_selection_respects_maximum_radius(self):
        records = {sid: (observation(sid, T0),) for sid in ("a", "b", "c", "far")}
        field = snapshot([
            ("a", 25.0, 121.0), ("b", 25.001, 121.0), ("c", 25.0, 121.001), ("far", 25.4, 121.0),
        ], records)
        estimate = field.estimate(25.0, 121.0)
        self.assertEqual(estimate.station_count, 3)
        self.assertEqual(estimate.diagnostics["effective_station_counts_within_km"]["5"], 3)
        self.assertGreater(haversine_km((121.0, 25.0), (121.0, 25.4)), 30)


if __name__ == "__main__":
    unittest.main()
