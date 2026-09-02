#!/usr/bin/env python3
"""Inspect PM2.5 sensor health and spatial coverage for the configured region."""

from __future__ import annotations

import argparse
import csv
import math
import sys
from collections import Counter, deque
from datetime import datetime, timezone
from pathlib import Path
from statistics import median
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from airtrace.data.sensorthings import (  # noqa: E402
    API_BASE_URL,
    ApiClient,
    RegionConfig,
    SensorThingsError,
    fetch_pm25_records,
    iso_utc,
)

DEFAULT_CONFIG = ROOT / "config" / "pilot_region.json"
DEFAULT_OUTPUT_PATH = ROOT / "data" / "normalized" / "pilot_sensor_inventory.csv"
FRESH_SECONDS = 10 * 60
STALE_SECONDS = 30 * 60
GRID_CELL_KM = 1.0
EARTH_RADIUS_KM = 6371.0088
CSV_FIELDS = [
    "thing_id", "stationID", "stationName", "city", "township", "areaType",
    "areaDescription", "isOutdoor", "isMobile", "projectName", "latitude",
    "longitude", "coordinate_crs", "pm25_datastream_id", "latest_pm25_value",
    "latest_observation_phenomenon_time", "freshness_status", "observation_age_minutes",
    "valid_coordinate", "valid_pm25", "future_timestamp", "bbox_status",
]


def meters_per_degree(latitude: float) -> tuple[float, float]:
    radians = math.radians(latitude)
    return (
        111132.92 - 559.82 * math.cos(2 * radians) + 1.175 * math.cos(4 * radians) - 0.0023 * math.cos(6 * radians),
        111412.84 * math.cos(radians) - 93.5 * math.cos(3 * radians) + 0.118 * math.cos(5 * radians),
    )


def haversine_km(first: tuple[float, float], second: tuple[float, float]) -> float:
    lon1, lat1 = map(math.radians, first)
    lon2, lat2 = map(math.radians, second)
    dlon, dlat = lon2 - lon1, lat2 - lat1
    a = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(math.sqrt(min(1.0, a)))


def percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * fraction
    lower, upper = math.floor(position), math.ceil(position)
    return ordered[lower] if lower == upper else ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def fresh_valid_points(rows: list[dict[str, Any]]) -> list[tuple[float, float]]:
    return [
        (float(row["lon"]), float(row["lat"]))
        for row in rows
        if row.get("freshness_status") == "fresh" and row.get("lat") is not None and row.get("lon") is not None and row.get("pm25_ugm3") is not None
    ]


def spacing_stats(points: list[tuple[float, float]]) -> tuple[float | None, float | None]:
    if len(points) < 2:
        return None, None
    nearest = [min(haversine_km(point, other) for index, other in enumerate(points) if index != i) for i, point in enumerate(points)]
    return median(nearest), percentile(nearest, 0.90)


def grid_shape(bbox: dict[str, float]) -> tuple[int, int]:
    lat_m, lon_m = meters_per_degree((bbox["north"] + bbox["south"]) / 2)
    return max(1, math.ceil((bbox["north"] - bbox["south"]) * lat_m / 1000)), max(1, math.ceil((bbox["east"] - bbox["west"]) * lon_m / 1000))


def coverage_stats(points: list[tuple[float, float]], bbox: dict[str, float]) -> dict[str, Any]:
    rows, columns = grid_shape(bbox)
    occupied: set[tuple[int, int]] = set()
    for longitude, latitude in points:
        column = min(columns - 1, int((longitude - bbox["west"]) / (bbox["east"] - bbox["west"]) * columns))
        row = min(rows - 1, int((latitude - bbox["south"]) / (bbox["north"] - bbox["south"]) * rows))
        if 0 <= row < rows and 0 <= column < columns:
            occupied.add((row, column))
    empty = {(r, c) for r in range(rows) for c in range(columns) if (r, c) not in occupied}
    components: list[list[tuple[int, int]]] = []
    remaining = set(empty)
    while remaining:
        start = remaining.pop()
        component = [start]
        queue: deque[tuple[int, int]] = deque([start])
        while queue:
            r, c = queue.popleft()
            for neighbor in ((r - 1, c), (r + 1, c), (r, c - 1), (r, c + 1)):
                if neighbor in remaining:
                    remaining.remove(neighbor)
                    component.append(neighbor)
                    queue.append(neighbor)
        components.append(sorted(component))
    return {"rows": rows, "columns": columns, "occupied": len(occupied), "total": rows * columns, "empty_components": sorted(components, key=lambda item: -len(item))}


def as_csv_rows(rows: list[dict[str, Any]], now: datetime) -> list[dict[str, Any]]:
    result = []
    for row in rows:
        phenomenon = row.get("phenomenon_time_utc")
        age = "" if phenomenon is None else round((now - phenomenon).total_seconds() / 60, 2)
        result.append({
            "thing_id": row["thing_id"], "stationID": row["station_id"], "stationName": row["station_name"],
            "city": row["city"], "township": row["township"], "areaType": row["area_type"],
            "areaDescription": row["area_description"], "isOutdoor": row["is_outdoor"], "isMobile": row["is_mobile"],
            "projectName": row["project_name"], "latitude": row["lat"], "longitude": row["lon"],
            "coordinate_crs": "EPSG:4326", "pm25_datastream_id": row["datastream_id"],
            "latest_pm25_value": row["pm25_ugm3"] if row["pm25_ugm3"] is not None else "",
            "latest_observation_phenomenon_time": iso_utc(phenomenon), "freshness_status": row["freshness_status"],
            "observation_age_minutes": age, "valid_coordinate": row["lat"] is not None and row["lon"] is not None,
            "valid_pm25": row["pm25_ugm3"] is not None, "future_timestamp": bool(phenomenon and phenomenon > now),
            "bbox_status": "inside_bbox",
        })
    return result


def write_csv(rows: list[dict[str, Any]], output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def print_report(rows: list[dict[str, Any]], details: dict[str, Any], region: RegionConfig, output: Path, now: datetime) -> None:
    quality = details["quality"]
    freshness = Counter(row["freshness_status"] for row in rows)
    points = fresh_valid_points(rows)
    median_spacing, p90_spacing = spacing_stats(points)
    coverage = coverage_stats(points, region.bbox)
    print("\n" + "=" * 72)
    print("AirTrace | Pilot Region Sensor Inventory")
    print(f"API: {API_BASE_URL}")
    print(f"BBox (WGS84 / EPSG:4326): {region.bbox}")
    print(f"As-of UTC: {iso_utc(now)}\n")
    print("Sensors")
    print(f"  total PM2.5 sensors in bbox : {len(rows)}")
    print(f"  fresh                       : {freshness['fresh']}")
    print(f"  stale                       : {freshness['stale']}")
    print(f"  offline                     : {freshness['offline']}")
    print(f"  missing observation         : {quality['missing_observation']}")
    print("\nData quality flags")
    print(f"  invalid PM2.5               : {quality['invalid_pm25']}")
    print(f"  invalid coordinates         : {quality['invalid_coordinate']}")
    print(f"  future timestamp            : {quality['future_timestamp']}")
    print(f"  invalid timestamp           : {quality['invalid_timestamp']}")
    if quality["future_ahead_seconds"]:
        print("  future ahead seconds        : " + ", ".join(f"{v:.3f}" for v in quality["future_ahead_seconds"]))
    print("\nSpatial coverage | valid + fresh sensors")
    print(f"  qualifying sensors          : {len(points)}")
    print(f"  median nearest-neighbor     : {'n/a' if median_spacing is None else f'{median_spacing:.2f} km'}")
    print(f"  P90 nearest-neighbor        : {'n/a' if p90_spacing is None else f'{p90_spacing:.2f} km'}")
    print(f"  grid                        : {coverage['rows']} rows x {coverage['columns']} columns")
    print(f"  occupied grid cells         : {coverage['occupied']}/{coverage['total']}")
    print(f"\nAPI / output\n  Things pages fetched        : {details['things_pages']}\n  API reported Thing count    : {details['api_reported_count']}\n  bounded latest expansion    : {'used' if details['nested_latest_used'] else 'per-datastream top=1 fallback'}\n  CSV                         : {output}")
    print("=" * 72)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT_PATH)
    parser.add_argument("--timeout", type=float, default=30.0)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.timeout <= 0:
        print("ERROR: --timeout must be positive", file=sys.stderr)
        return 2
    try:
        region = RegionConfig.load(args.config)
        now = datetime.now(timezone.utc)
        rows, details = fetch_pm25_records(ApiClient(timeout_seconds=args.timeout), region, now)
        write_csv(as_csv_rows(rows, now), args.output)
        print_report(rows, details, region, args.output, now)
    except (SensorThingsError, OSError, ValueError, KeyError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
