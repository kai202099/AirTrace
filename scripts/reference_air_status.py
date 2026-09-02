#!/usr/bin/env python3
"""Show compact status and latest Pilot relevance for reference air data."""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import duckdb  # noqa: E402

from airtrace.data.moenv import haversine_km  # noqa: E402

DEFAULT_CONFIG = ROOT / "config" / "pilot_region.json"
DEFAULT_DATABASE = ROOT / "data" / "reference_air.duckdb"


def core_center(config_path: Path) -> tuple[float, float]:
    config = json.loads(config_path.read_text(encoding="utf-8"))
    bbox = config["core_bbox"]
    return ((float(bbox["north"]) + float(bbox["south"])) / 2, (float(bbox["west"]) + float(bbox["east"])) / 2)


def percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * fraction
    low = int(position)
    high = min(low + 1, len(ordered) - 1)
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    args = parser.parse_args()

    print("AirTrace Reference Air Status")
    print(f"DB exists: {args.database.exists()}")
    if not args.database.exists():
        return 1
    print(f"DB path: {args.database}")
    print(f"DB size: {args.database.stat().st_size} bytes")
    connection = duckdb.connect(str(args.database), read_only=True)
    try:
        connection.execute("SET TimeZone='UTC'")
        station_count = connection.execute("SELECT count(*) FROM reference_air_station").fetchone()[0]
        observation_count = connection.execute("SELECT count(*) FROM reference_air_observation").fetchone()[0]
        latest_publish, latest_ingest = connection.execute(
            "SELECT max(publish_time_utc), max(ingested_at_utc) FROM reference_air_observation"
        ).fetchone()
        latest_cycle_count = connection.execute(
            """
            SELECT count(*) FROM reference_air_observation
            WHERE publish_time_utc = (SELECT max(publish_time_utc) FROM reference_air_observation)
            """
        ).fetchone()[0]
        print(f"Station count: {station_count}")
        print(f"Observation count: {observation_count}")
        print(f"Latest publish time: {latest_publish}")
        print(f"Latest ingest time: {latest_ingest}")
        print(f"Observations from latest publish cycle: {latest_cycle_count}")

        rows = connection.execute(
            """
            SELECT s.site_id, s.lat, s.lon, o.pm25_ugm3
            FROM reference_air_station s
            JOIN reference_air_observation o USING (site_id)
            WHERE o.publish_time_utc = (SELECT max(publish_time_utc) FROM reference_air_observation)
            """
        ).fetchall()
        center_lat, center_lon = core_center(args.config)
        distances = [
            (site_id, haversine_km((center_lon, center_lat), (lon, lat)), pm25)
            for site_id, lat, lon, pm25 in rows
            if lat is not None and lon is not None
        ]
        print(f"Stations within 30 km of Core center: {sum(distance <= 30 for _, distance, _ in distances)}")
        regional = [pm25 for _, distance, pm25 in distances if distance <= 50 and pm25 is not None]
        if regional:
            print(f"Latest PM2.5 regional median (within 50 km): {statistics.median(regional):.3f}")
            print(f"Latest PM2.5 regional min (within 50 km): {min(regional):.3f}")
            print(f"Latest PM2.5 regional max (within 50 km): {max(regional):.3f}")
            print(f"Latest PM2.5 regional IQR (within 50 km): {percentile(regional, 0.75) - percentile(regional, 0.25):.3f}")
        else:
            print("Latest PM2.5 regional median: NULL")
    finally:
        connection.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
