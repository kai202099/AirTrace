#!/usr/bin/env python3
"""Show weather recorder health and pilot-core station relevance."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import duckdb  # noqa: E402

from airtrace.data.cwa import haversine_km, iso_utc  # noqa: E402


DEFAULT_CONFIG = ROOT / "config" / "pilot_region.json"
DEFAULT_DATABASE = ROOT / "data" / "weather.duckdb"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    print("AirTrace Weather Status")
    print(f"Weather DB exists: {args.database.exists()}")
    if not args.database.exists():
        return 1
    print(f"DB file: {args.database}")
    print(f"DB file size: {args.database.stat().st_size} bytes")
    try:
        with args.config.open("r", encoding="utf-8") as handle:
            config = json.load(handle)
        core = config["core_bbox"]
        center = ((float(core["west"]) + float(core["east"])) / 2, (float(core["south"]) + float(core["north"])) / 2)
        connection = duckdb.connect(str(args.database), read_only=True)
        try:
            connection.execute("SET TimeZone='UTC'")
            first, latest, total = connection.execute(
                """
                SELECT CAST(min(observation_time_utc) AS VARCHAR),
                       CAST(max(observation_time_utc) AS VARCHAR), count(*)
                FROM weather_observation
                """
            ).fetchone()
            unique_stations = connection.execute("SELECT count(*) FROM weather_station").fetchone()[0]
            last_20, valid_wind_20 = connection.execute(
                """
                SELECT
                  count(*) FILTER (WHERE observation_time_utc >= CURRENT_TIMESTAMP - INTERVAL '20 minutes'),
                  count(DISTINCT station_id) FILTER (
                      WHERE observation_time_utc >= CURRENT_TIMESTAMP - INTERVAL '20 minutes'
                        AND wind_status = 'valid'
                  )
                FROM weather_observation
                """
            ).fetchone()
            stations = connection.execute(
                "SELECT station_id, station_name, lat, lon FROM weather_station WHERE lat IS NOT NULL AND lon IS NOT NULL"
            ).fetchall()
            nearest = connection.execute(
                """
                WITH latest AS (
                    SELECT station_id, observation_time_utc, wind_from_deg, wind_speed_mps,
                           wind_status, row_number() OVER (
                               PARTITION BY station_id ORDER BY observation_time_utc DESC
                           ) AS rank
                    FROM weather_observation
                )
                SELECT s.station_id, s.station_name, s.lat, s.lon,
                       l.observation_time_utc, l.wind_from_deg, l.wind_speed_mps, l.wind_status
                FROM weather_station s
                LEFT JOIN latest l ON l.station_id = s.station_id AND l.rank = 1
                WHERE s.lat IS NOT NULL AND s.lon IS NOT NULL
                """
            ).fetchall()
            poll = connection.execute(
                """
                SELECT CAST(poll_started_at_utc AS VARCHAR), CAST(completed_at_utc AS VARCHAR), success,
                       stations_received, observations_received, new_observations_inserted,
                       duplicates_skipped, api_errors, raw_snapshot_path, error_message
                FROM weather_poll ORDER BY poll_started_at_utc DESC LIMIT 1
                """
            ).fetchone()
        finally:
            connection.close()
    except (OSError, KeyError, ValueError, duckdb.Error) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    distances = [
        (haversine_km(center, (float(lon), float(lat))), station_id, name, float(lat), float(lon))
        for station_id, name, lat, lon in stations
    ]
    distances.sort()
    print(f"Pilot Core center (lon, lat): {center[0]:.6f}, {center[1]:.6f}")
    print(f"First observation: {first}")
    print(f"Latest observation: {latest}")
    print(f"Total unique observations: {total}")
    print(f"Unique stations: {unique_stations}")
    print(f"Observations in last 20 min: {last_20}")
    print(f"Stations with valid wind in last 20 min: {valid_wind_20}")
    for radius in (5, 10, 20, 30):
        print(f"Stations within {radius} km of Pilot Core center: {sum(distance <= radius for distance, *_ in distances)}")
    if poll:
        print(f"Latest poll health: {'success' if poll[2] else 'failed'}")
        print(f"Latest poll time: {poll[0]}")
        print(f"Latest poll stations/observations/new/duplicates: {poll[3]}/{poll[4]}/{poll[5]}/{poll[6]}")
        print(f"Latest poll API errors: {poll[7]}")
        print(f"Latest raw snapshot: {poll[8] or '(none)'}")
        if poll[9]:
            print(f"Latest poll note: {poll[9]}")
    else:
        print("Latest poll health: no poll recorded")

    print("\nNearest 15 weather stations")
    print("station_id | name | distance_km | lat | lon | latest wind direction | wind speed | observation time")
    by_station = {row[0]: row for row in nearest}
    for distance, station_id, name, lat, lon in distances[:15]:
        row = by_station[station_id]
        direction = "n/a" if row[5] is None else f"{row[5]:g}"
        speed = "n/a" if row[6] is None else f"{row[6]:g}"
        observation = row[4] or "n/a"
        print(f"{station_id} | {name} | {distance:.2f} | {lat:.6f} | {lon:.6f} | {direction} ({row[7]}) | {speed} m/s | {observation}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
