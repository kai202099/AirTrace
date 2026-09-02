"""Strict leave-one-station-out validation for the production wind field.

The validator deliberately calls :mod:`airtrace.analysis.wind` for every
prediction.  It therefore measures the current temporal selection, spatial
selection, IDW weighting, and quality categorisation without creating a
second validation-only interpolation implementation.
"""

from __future__ import annotations

import csv
import json
import math
from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

import duckdb

from airtrace.analysis.wind import (
    DEFAULT_DATABASE,
    RawWindObservation,
    WindConfig,
    WindEstimate,
    directions_from_vector,
    haversine_km,
    load_wind_snapshot,
    estimate_to_dict,
)

UTC = timezone.utc
MIN_SPEED_FOR_DIRECTION = 0.5
DISTANCE_BUCKETS = ("<= 3 km", "3–5 km", "5–10 km", "> 10 km", "unavailable")
STATION_COUNT_BUCKETS = ("3–4", "5–6", "7–8", "other")
SPEED_BUCKETS = ("calm / <0.5", "0.5–2", "2–5", "> 5 m/s")
QUALITY_BUCKETS = ("GOOD", "MEDIUM_UNCERTAINTY", "HIGH_UNCERTAINTY", "CALM / LOW_DIRECTION_CONFIDENCE", "INSUFFICIENT_STATIONS")


@dataclass(frozen=True)
class ValidationTarget:
    station_id: str
    station_name: str
    lat: float
    lon: float
    timestamp_utc: datetime
    actual_u: float
    actual_v: float
    actual_speed: float
    actual_from_deg: float | None
    target_status: str


def circular_angle_error(first_deg: float, second_deg: float) -> float:
    """Return the smallest unsigned angular distance in degrees."""

    difference = abs(float(first_deg) - float(second_deg)) % 360.0
    return min(difference, 360.0 - difference)


def distance_bucket(distance_km: float | None) -> str:
    if distance_km is None or not math.isfinite(float(distance_km)):
        return "unavailable"
    distance = float(distance_km)
    if distance <= 3:
        return "<= 3 km"
    if distance <= 5:
        return "3–5 km"
    if distance <= 10:
        return "5–10 km"
    return "> 10 km"


def station_count_bucket(count: int | None) -> str:
    if count in (3, 4):
        return "3–4"
    if count in (5, 6):
        return "5–6"
    if count in (7, 8):
        return "7–8"
    return "other"


def speed_bucket(speed_mps: float) -> str:
    speed = float(speed_mps)
    if speed < MIN_SPEED_FOR_DIRECTION:
        return "calm / <0.5"
    if speed < 2:
        return "0.5–2"
    if speed <= 5:
        return "2–5"
    return "> 5 m/s"


def _parse_flags(value: Any) -> tuple[str, ...]:
    return tuple(token for token in str(value or "").split(";") if token)


def _utc(value: datetime) -> datetime:
    return value.astimezone(UTC) if value.tzinfo else value.replace(tzinfo=UTC)


def load_validation_targets(
    database_path: Path = DEFAULT_DATABASE,
    *,
    center_lat: float,
    center_lon: float,
    maximum_distance_km: float = 30.0,
) -> tuple[list[ValidationTarget], dict[str, Any]]:
    """Read eligible target observations and DB metadata without writing."""

    connection = duckdb.connect(str(database_path), read_only=True)
    try:
        station_rows = connection.execute(
            "SELECT station_id, station_name, lat, lon FROM weather_station ORDER BY station_id"
        ).fetchall()
        observation_rows = connection.execute(
            """
            SELECT station_id, observation_time_utc, wind_from_deg, wind_speed_mps,
                   wind_u_east_mps, wind_v_north_mps, wind_status, quality_flags
            FROM weather_observation
            WHERE wind_status IN ('valid', 'calm')
            ORDER BY observation_time_utc, station_id
            """
        ).fetchall()
        span = connection.execute(
            "SELECT min(observation_time_utc), max(observation_time_utc) FROM weather_observation"
        ).fetchone()
    finally:
        connection.close()

    eligible_stations: dict[str, tuple[str, float, float]] = {}
    for station_id, station_name, lat, lon in station_rows:
        if lat is None or lon is None:
            continue
        distance = haversine_km((center_lon, center_lat), (float(lon), float(lat)))
        if distance <= maximum_distance_km:
            eligible_stations[str(station_id)] = (str(station_name or ""), float(lat), float(lon))

    targets: list[ValidationTarget] = []
    for row in observation_rows:
        station_id = str(row[0])
        station = eligible_stations.get(station_id)
        if station is None or row[1] is None:
            continue
        status = str(row[6] or "").casefold()
        if status == "valid":
            if row[4] is None or row[5] is None:
                continue
            u, v = float(row[4]), float(row[5])
            if not math.isfinite(u) or not math.isfinite(v):
                continue
            actual_speed = math.hypot(u, v)
            actual_from = None if actual_speed < MIN_SPEED_FOR_DIRECTION else (float(row[2]) if row[2] is not None else directions_from_vector(u, v)[2])
        elif status == "calm":
            u = v = actual_speed = 0.0
            actual_from = None
        else:
            continue
        targets.append(ValidationTarget(
            station_id, station[0], station[1], station[2], _utc(row[1]),
            u, v, actual_speed, actual_from, status,
        ))

    metadata = {
        "database_start_utc": None if span[0] is None else _utc(span[0]),
        "database_end_utc": None if span[1] is None else _utc(span[1]),
        "eligible_station_count": len(eligible_stations),
        "eligible_station_ids": sorted(eligible_stations),
    }
    return targets, metadata


def _aggregate_temporal_mode(estimate: WindEstimate) -> str:
    modes = sorted({item.temporal_mode for item in estimate.stations_used})
    if not modes:
        return "no_prediction"
    return modes[0] if len(modes) == 1 else "mixed"


def evaluate_target(target: ValidationTarget, estimate: WindEstimate) -> dict[str, Any]:
    predicted_speed = estimate.speed_mps
    predicted_from = estimate.wind_from_deg
    vector_error = None
    speed_error = None
    direction_error = None
    if estimate.u_east_mps is not None and estimate.v_north_mps is not None:
        vector_error = math.hypot(estimate.u_east_mps - target.actual_u, estimate.v_north_mps - target.actual_v)
        speed_error = abs((predicted_speed or 0.0) - target.actual_speed)
        if target.actual_speed >= MIN_SPEED_FOR_DIRECTION and target.actual_from_deg is not None and predicted_from is not None:
            direction_error = circular_angle_error(predicted_from, target.actual_from_deg)
    return {
        "target_station_id": target.station_id,
        "target_station_name": target.station_name,
        "timestamp_utc": target.timestamp_utc.isoformat().replace("+00:00", "Z"),
        "lat": target.lat,
        "lon": target.lon,
        "actual_u": round(target.actual_u, 6),
        "actual_v": round(target.actual_v, 6),
        "actual_speed": round(target.actual_speed, 6),
        "actual_from_deg": None if target.actual_from_deg is None else round(target.actual_from_deg % 360.0, 6),
        "predicted_u": estimate.u_east_mps,
        "predicted_v": estimate.v_north_mps,
        "predicted_speed": predicted_speed,
        "predicted_from_deg": predicted_from,
        "vector_error_mps": None if vector_error is None else round(vector_error, 6),
        "speed_error_mps": None if speed_error is None else round(speed_error, 6),
        "direction_error_deg": None if direction_error is None else round(direction_error, 6),
        "stations_used": estimate.station_count,
        "nearest_station_km": estimate.nearest_station_km,
        "temporal_mode": _aggregate_temporal_mode(estimate),
        "predicted_quality": estimate.quality_category,
    }


def _percentile(values: Sequence[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(float(value) for value in values)
    position = (len(ordered) - 1) * fraction
    lower, upper = math.floor(position), math.ceil(position)
    if lower == upper:
        return round(ordered[lower], 6)
    weight = position - lower
    return round(ordered[lower] + (ordered[upper] - ordered[lower]) * weight, 6)


def summarize_rows(rows: Sequence[dict[str, Any]]) -> dict[str, Any]:
    vectors = [float(row["vector_error_mps"]) for row in rows if row.get("vector_error_mps") is not None]
    speeds = [float(row["speed_error_mps"]) for row in rows if row.get("speed_error_mps") is not None]
    directions = [float(row["direction_error_deg"]) for row in rows if row.get("direction_error_deg") is not None]
    speed_bias = [float(row["predicted_speed"]) - float(row["actual_speed"]) for row in rows if row.get("predicted_speed") is not None]
    valid_count = len(vectors)

    def mean(values: Sequence[float]) -> float | None:
        return None if not values else round(sum(values) / len(values), 6)

    result = {
        "sample_count": len(rows),
        "valid_prediction_count": valid_count,
        "prediction_coverage_pct": round(100.0 * valid_count / len(rows), 6) if rows else 0.0,
        "vector": {
            "count": len(vectors),
            "mae_mps": mean(vectors),
            "rmse_mps": None if not vectors else round(math.sqrt(sum(value * value for value in vectors) / len(vectors)), 6),
            "median_mps": _percentile(vectors, 0.5),
            "p75_mps": _percentile(vectors, 0.75),
            "p90_mps": _percentile(vectors, 0.90),
            "p95_mps": _percentile(vectors, 0.95),
        },
        "speed": {"count": len(speeds), "mae_mps": mean(speeds), "median_mps": _percentile(speeds, 0.5), "p90_mps": _percentile(speeds, 0.90)},
        "direction": {
            "count": len(directions),
            "minimum_speed_for_direction_mps": MIN_SPEED_FOR_DIRECTION,
            "threshold_note": "diagnostic heuristic; direction error is omitted below this observed speed",
            "circular_mae_deg": mean(directions),
            "median_deg": _percentile(directions, 0.5),
            "p75_deg": _percentile(directions, 0.75),
            "p90_deg": _percentile(directions, 0.90),
        },
        "bias": {
            "mean_u_error_mps": mean([float(row["predicted_u"]) - float(row["actual_u"]) for row in rows if row.get("predicted_u") is not None]),
            "mean_v_error_mps": mean([float(row["predicted_v"]) - float(row["actual_v"]) for row in rows if row.get("predicted_v") is not None]),
            "mean_speed_bias_mps": mean(speed_bias),
        },
    }
    return result


def _stratify(rows: Sequence[dict[str, Any]], key_function: Any, buckets: Sequence[str]) -> dict[str, Any]:
    groups: dict[str, list[dict[str, Any]]] = {bucket: [] for bucket in buckets}
    for row in rows:
        groups.setdefault(key_function(row), []).append(row)
    return {bucket: summarize_rows(groups[bucket]) for bucket in groups}


def _evaluate_targets(
    targets: Sequence[ValidationTarget],
    database_path: Path,
    config: WindConfig,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    snapshots: dict[datetime, Any] = {}
    for target in targets:
        snapshot = snapshots.get(target.timestamp_utc)
        if snapshot is None:
            snapshot = load_wind_snapshot(database_path, target.timestamp_utc, config)
            snapshots[target.timestamp_utc] = snapshot
        estimate = snapshot.estimate(target.lat, target.lon, exclude_station_ids={target.station_id})
        if any(item.station_id == target.station_id for item in estimate.stations_used):
            raise RuntimeError(f"strict leave-one-station-out invariant violated for {target.station_id} at {target.timestamp_utc.isoformat()}")
        rows.append(evaluate_target(target, estimate))
    return rows


def _parameter_diagnostic(
    targets: Sequence[ValidationTarget],
    database_path: Path,
    base_config: WindConfig,
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for preferred_radius in (10.0, 15.0, 20.0):
        for power in (1.0, 2.0, 3.0):
            config = WindConfig(
                preferred_radius_km=preferred_radius,
                maximum_radius_km=30.0,
                minimum_stations=base_config.minimum_stations,
                maximum_stations=base_config.maximum_stations,
                maximum_temporal_distance_minutes=base_config.maximum_temporal_distance_minutes,
                idw_power=power,
                idw_epsilon_km=base_config.idw_epsilon_km,
            )
            rows = _evaluate_targets(targets, database_path, config)
            metrics = summarize_rows(rows)
            results.append({
                "preferred_radius_km": preferred_radius,
                "idw_power": power,
                "maximum_radius_km": 30.0,
                "vector_rmse_mps": metrics["vector"]["rmse_mps"],
                "vector_median_error_mps": metrics["vector"]["median_mps"],
                "direction_median_error_deg": metrics["direction"]["median_deg"],
                "valid_prediction_coverage_pct": metrics["prediction_coverage_pct"],
                "valid_prediction_count": metrics["valid_prediction_count"],
            })
    return results


def _proxy_rows(rows: Sequence[dict[str, Any]], quality: str) -> list[dict[str, Any]]:
    return [row for row in rows if row.get("nearest_station_km") is not None and 3.0 <= float(row["nearest_station_km"]) <= 7.0 and 6 <= int(row["stations_used"]) <= 8 and row.get("predicted_quality") == quality]


def run_validation(
    database_path: Path = DEFAULT_DATABASE,
    *,
    center_lat: float,
    center_lon: float,
    base_config: WindConfig = WindConfig(),
) -> dict[str, Any]:
    targets, metadata = load_validation_targets(
        database_path, center_lat=center_lat, center_lon=center_lon,
        maximum_distance_km=base_config.maximum_radius_km,
    )
    rows = _evaluate_targets(targets, database_path, base_config)
    current_snapshot = load_wind_snapshot(database_path, None, base_config)
    current_center = current_snapshot.estimate(center_lat, center_lon)
    current_proxy = _proxy_rows(rows, current_center.quality_category)
    quality_groups = _stratify(rows, lambda row: row["predicted_quality"], QUALITY_BUCKETS)
    summary = summarize_rows(rows)
    summary.update({
        "validation_status": "PRELIMINARY — LIMITED HISTORY",
        "validation_span_utc": {
            "start": None if metadata["database_start_utc"] is None else metadata["database_start_utc"].isoformat().replace("+00:00", "Z"),
            "end": None if metadata["database_end_utc"] is None else metadata["database_end_utc"].isoformat().replace("+00:00", "Z"),
        },
        "station_count_evaluated": len({target.station_id for target in targets}),
        "station_count_within_30km": metadata["eligible_station_count"],
        "distance_buckets": _stratify(rows, lambda row: distance_bucket(row.get("nearest_station_km")), DISTANCE_BUCKETS),
        "stations_used_buckets": _stratify(rows, lambda row: station_count_bucket(row.get("stations_used")), STATION_COUNT_BUCKETS),
        "wind_speed_buckets": _stratify(rows, lambda row: speed_bucket(float(row["actual_speed"])), SPEED_BUCKETS),
        "quality_categories": quality_groups,
        "quality_diagnostic": {
            "note": "Descriptive comparison only; this short history is insufficient to claim statistical correlation.",
            "categories": {key: {"sample_count": value["sample_count"], "vector_median_mps": value["vector"]["median_mps"], "vector_p90_mps": value["vector"]["p90_mps"]} for key, value in quality_groups.items()},
        },
        "parameter_diagnostic": _parameter_diagnostic(targets, database_path, base_config),
        "current_production_parameters": asdict(base_config),
        "core_center": {
            "lat": center_lat,
            "lon": center_lon,
            "current_estimate": estimate_to_dict(current_center),
            "empirical_error_proxy": {
                "selection": "nearest station 3–7 km; stations used 6–8; exact current quality category",
                "quality_category": current_center.quality_category,
                "sample_count": len(current_proxy),
                "metrics": summarize_rows(current_proxy),
            },
        },
    })
    return {
        "schema_version": 1,
        "analysis": {
            "name": "Wind Field Validation v1",
            "method": "strict leave-one-station-out cross-validation",
            "target_scope": "CWA weather stations within 30 km of Pilot Core center",
            "database_path": str(database_path),
            "read_only": True,
        },
        "summary": summary,
        "samples": rows,
    }


def write_samples_csv(path: Path, rows: Sequence[dict[str, Any]]) -> None:
    fields = [
        "target_station_id", "target_station_name", "timestamp_utc", "lat", "lon",
        "actual_u", "actual_v", "actual_speed", "actual_from_deg", "predicted_u",
        "predicted_v", "predicted_speed", "predicted_from_deg", "vector_error_mps",
        "speed_error_mps", "direction_error_deg", "stations_used", "nearest_station_km",
        "temporal_mode", "predicted_quality",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows({field: row.get(field) for field in fields} for row in rows)


def write_validation_map(path: Path, payload: dict[str, Any], pilot_config: dict[str, Any]) -> None:
    rows = payload["samples"]
    by_station: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_station[row["target_station_id"]].append(row)
    stations: list[dict[str, Any]] = []
    for station_id, station_rows in sorted(by_station.items()):
        valid = [row for row in station_rows if row.get("vector_error_mps") is not None]
        directions = [row["direction_error_deg"] for row in station_rows if row.get("direction_error_deg") is not None]
        first = station_rows[0]
        stations.append({
            "station_id": station_id,
            "station_name": first["target_station_name"],
            "lat": first["lat"], "lon": first["lon"], "sample_count": len(station_rows),
            "median_vector_error_mps": _percentile([row["vector_error_mps"] for row in valid], 0.5),
            "p90_vector_error_mps": _percentile([row["vector_error_mps"] for row in valid], 0.9),
            "median_direction_error_deg": _percentile(directions, 0.5),
            "valid_prediction_count": len(valid),
        })
    context = pilot_config["context_bbox"]
    core = pilot_config["core_bbox"]
    encoded = json.dumps(stations, ensure_ascii=False, separators=(",", ":"))
    context_json = json.dumps(context, separators=(",", ":"))
    core_json = json.dumps(core, separators=(",", ":"))
    title = "AirTrace Wind Field Validation v1"
    html = f'''<!doctype html><html lang="zh-Hant"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>{title}</title><link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"><style>body{{margin:0;font-family:system-ui,-apple-system,"Segoe UI",sans-serif;color:#172033}}header{{padding:14px 18px;border-bottom:1px solid #d9e0ea;background:#fff}}h1{{margin:0 0 5px;font-size:21px}}.note{{color:#586579;font-size:13px}}#map{{height:calc(100vh - 105px);min-height:560px}}.popup-table td{{padding:2px 6px 2px 0;vertical-align:top}}.popup-table td:first-child{{color:#586579;white-space:nowrap}}</style></head><body><header><h1>{title}</h1><div class="note">PRELIMINARY — LIMITED HISTORY · 每個 marker 是一個 target station 的 aggregate strict leave-one-station-out error。</div></header><div id="map"></div><script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script><script>const contextBbox={context_json},coreBbox={core_json},stations={encoded};const map=L.map('map',{{preferCanvas:true}}).setView([25.05,121.55],10);const osm=L.tileLayer('https://{{s}}.tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png',{{maxZoom:19,attribution:'&copy; OpenStreetMap contributors'}}).addTo(map);L.rectangle([[contextBbox.south,contextBbox.west],[contextBbox.north,contextBbox.east]],{{color:'#26364f',weight:2,fill:false,dashArray:'7 5'}}).bindPopup('<strong>Context Zone</strong>').addTo(map);L.rectangle([[coreBbox.south,coreBbox.west],[coreBbox.north,coreBbox.east]],{{color:'#8e2a86',weight:3,fill:false,dashArray:'8 4'}}).bindPopup('<strong>Core Zone</strong>').addTo(map);function safe(v){{return String(v??'—').replace(/[&<>"']/g,c=>({{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}}[c]))}}function n(v){{return v===null||v===undefined?'—':Number(v).toFixed(2)}}stations.forEach(s=>{{const popup='<strong>'+safe(s.station_id)+' · '+safe(s.station_name)+'</strong><table class="popup-table"><tr><td>samples</td><td>'+s.sample_count+'</td></tr><tr><td>valid predictions</td><td>'+s.valid_prediction_count+'</td></tr><tr><td>median vector error</td><td>'+n(s.median_vector_error_mps)+' m/s</td></tr><tr><td>P90 vector error</td><td>'+n(s.p90_vector_error_mps)+' m/s</td></tr><tr><td>median direction error</td><td>'+n(s.median_direction_error_deg)+'°</td></tr></table>';L.circleMarker([s.lat,s.lon],{{radius:Math.max(5,Math.min(13,5+(s.median_vector_error_mps||0))),color:'#2563eb',fillColor:'#60a5fa',fillOpacity:.75,weight:2}}).bindPopup(popup).addTo(map)}});map.fitBounds([[contextBbox.south,contextBbox.west],[contextBbox.north,contextBbox.east]],{{padding:[14,14]}});</script></body></html>'''
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(html, encoding="utf-8")


def markdown_summary(payload: dict[str, Any]) -> str:
    summary = payload["summary"]
    vector = summary["vector"]
    speed = summary["speed"]
    direction = summary["direction"]
    proxy = summary["core_center"]["empirical_error_proxy"]
    lines = [
        "# Wind Field Validation v1",
        "",
        "PRELIMINARY — LIMITED HISTORY",
        "",
        "Strict leave-one-station-out cross-validation of the production `get_wind()` path. The target station is excluded by station ID before temporal selection; no recorder writes are performed.",
        "",
        f"- Validation span: {summary['validation_span_utc']['start']} → {summary['validation_span_utc']['end']}",
        f"- Stations evaluated: {summary['station_count_evaluated']} (eligible within 30 km: {summary['station_count_within_30km']})",
        f"- Validation samples: {summary['sample_count']}",
        f"- Valid predictions: {summary['valid_prediction_count']} ({summary['prediction_coverage_pct']:.2f}%)",
        f"- Vector error: median {vector['median_mps']} m/s · RMSE {vector['rmse_mps']} m/s · P90 {vector['p90_mps']} m/s",
        f"- Speed error: MAE {speed['mae_mps']} m/s · P90 {speed['p90_mps']} m/s",
        f"- Direction error: median {direction['median_deg']}° · P90 {direction['p90_deg']}° · n={direction['count']} (observed speed ≥ {MIN_SPEED_FOR_DIRECTION} m/s)",
        f"- Quality diagnostic: {summary['quality_diagnostic']['note']}",
        "",
        "## Stratified error",
        "",
        "The JSON summary contains full vector, speed, direction, and bias metrics for distance, stations-used, observed-speed, and quality strata.",
        "",
        "## Parameter diagnostic",
        "",
        "Compared IDW powers 1/2/3 and preferred radii 10/15/20 km while keeping maximum radius at 30 km. These are diagnostic results only; production defaults were not changed.",
        "",
        "| preferred radius | IDW power | vector RMSE | vector median | direction median | coverage |",
        "|---:|---:|---:|---:|---:|---:|",
    ]
    for item in summary["parameter_diagnostic"]:
        lines.append(f"| {item['preferred_radius_km']} km | {item['idw_power']} | {item['vector_rmse_mps']} | {item['vector_median_error_mps']} | {item['direction_median_error_deg']} | {item['valid_prediction_coverage_pct']:.2f}% |")
    lines.extend([
        "",
        "## Core-center empirical error proxy",
        "",
        f"Current center estimate: quality `{summary['core_center']['current_estimate']['quality_category']}`, nearest {summary['core_center']['current_estimate']['nearest_station_km']} km, stations used {summary['core_center']['current_estimate']['station_count']}.",
        f"Matching validation subset ({proxy['selection']}): n={proxy['sample_count']}, median vector {proxy['metrics']['vector']['median_mps']} m/s, P90 vector {proxy['metrics']['vector']['p90_mps']} m/s, median direction {proxy['metrics']['direction']['median_deg']}°, P90 direction {proxy['metrics']['direction']['p90_deg']}°.",
        "This is an empirical error proxy, not the Core center's true error.",
        "",
        "## Reproduction",
        "",
        "```powershell",
        "python scripts/validate_wind_field.py",
        "```",
    ])
    return "\n".join(lines)


__all__ = [
    "MIN_SPEED_FOR_DIRECTION", "ValidationTarget", "circular_angle_error", "distance_bucket",
    "station_count_bucket", "speed_bucket", "load_validation_targets", "evaluate_target",
    "summarize_rows", "run_validation", "write_samples_csv", "write_validation_map", "markdown_summary",
]
