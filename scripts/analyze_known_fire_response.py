#!/usr/bin/env python3
"""Post-hoc PM2.5 response diagnostic for configured known fires.

This script is intentionally outside the production detector. It reads the
PM2.5 and weather databases, reuses the unchanged v1 anomaly and wind paths,
and writes validation artifacts only.
"""

from __future__ import annotations

import argparse
import csv
import html
import json
import math
import statistics
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import duckdb  # noqa: E402

from airtrace.analysis.anomaly import (  # noqa: E402
    AnomalyConfig,
    BinnedObservation,
    _analyse_station,
    _smooth_series,
    bbox_contains,
    floor_time,
    haversine_km,
    iso_utc,
    load_pilot_config,
    parse_iso_utc,
    robust_stats,
)
from airtrace.analysis.wind import (  # noqa: E402
    WindFieldSnapshot,
    estimate_to_dict,
    get_wind,
    load_wind_snapshot_range,
)

UTC = timezone.utc
DEFAULT_DATABASE = ROOT / "data" / "airtrace.duckdb"
DEFAULT_WEATHER_DATABASE = ROOT / "data" / "weather.duckdb"
DEFAULT_CONFIG = ROOT / "config" / "pilot_region.json"
DEFAULT_EVENTS = ROOT / "config" / "validation" / "known_fires_20260903.json"
DEFAULT_OUTPUT = ROOT / "reports" / "validation" / "fires"
REPLAY_REPORT = ROOT / "reports" / "events" / "latest_events.json"


def finite(value: Any) -> bool:
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


def number(value: Any, digits: int = 6) -> float | None:
    return round(float(value), digits) if finite(value) else None


def safe_median(values: Iterable[float]) -> float | None:
    values = [float(value) for value in values if finite(value)]
    return number(statistics.median(values)) if values else None


def parse_event_time(value: str, timezone_name: str = "Asia/Taipei") -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=ZoneInfo(timezone_name))
    return parsed.astimezone(UTC)


def distance_bucket(distance_km: float | None) -> str:
    if distance_km is None or not finite(distance_km):
        return "unknown"
    for radius in (0.5, 1.0, 2.0, 3.0, 5.0):
        if float(distance_km) <= radius:
            return f"{radius:g}km"
    return ">5km"


def bearing_deg(first_lat: float, first_lon: float, second_lat: float, second_lon: float) -> float:
    lat1, lat2 = math.radians(first_lat), math.radians(second_lat)
    dlon = math.radians(second_lon - first_lon)
    y = math.sin(dlon) * math.cos(lat2)
    x = math.cos(lat1) * math.sin(lat2) - math.sin(lat1) * math.cos(lat2) * math.cos(dlon)
    return round((math.degrees(math.atan2(y, x)) + 360.0) % 360.0, 3)


def angular_difference(first: float, second: float) -> float:
    return abs((float(first) - float(second) + 180.0) % 360.0 - 180.0)


def classify_wind_relation(
    fire_to_sensor_bearing: float | None,
    wind_to_deg: float | None,
    *,
    downwind_limit: float = 45.0,
    crosswind_limit: float = 135.0,
) -> str:
    if fire_to_sensor_bearing is None or wind_to_deg is None:
        return "unknown"
    difference = angular_difference(fire_to_sensor_bearing, wind_to_deg)
    if difference <= downwind_limit:
        return "downwind"
    if difference < crosswind_limit:
        return "crosswind"
    return "upwind"


def nominal_travel_time_minutes(distance_km: float, wind_speed_mps: float | None) -> float | None:
    if not finite(distance_km) or not finite(wind_speed_mps) or float(wind_speed_mps) <= 0:
        return None
    return round(float(distance_km) * 1000.0 / float(wind_speed_mps) / 60.0, 6)


def threshold_margin(value: float | None, threshold: float) -> dict[str, Any]:
    return {
        "value": number(value),
        "threshold": number(threshold),
        "margin": number(float(value) - threshold) if finite(value) else None,
        "pass": bool(finite(value) and float(value) >= threshold),
    }


def valid_pm25(value: Any, quality_flags: Any = "") -> bool:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return False
    flags = str(quality_flags or "").split(";")
    return math.isfinite(parsed) and parsed >= 0 and "invalid_pm25" not in flags


def _window_values(records: Iterable[BinnedObservation], start: datetime, end: datetime) -> list[BinnedObservation]:
    # Window membership follows the source phenomenon timestamp. Bins remain
    # the production detector's smoothing unit, but must not move an edge
    # observation across a known-fire interval boundary.
    return [item for item in records if start <= item.observed_at < end]


def pm_response_metrics(records: Iterable[BinnedObservation], start: datetime, end: datetime) -> dict[str, Any]:
    records = sorted(records, key=lambda item: item.bucket)
    baseline = _window_values(records, start - timedelta(minutes=60), start)
    during = _window_values(records, start, end)
    post = _window_values(records, end, end + timedelta(minutes=120))
    post_0_30 = _window_values(records, end, end + timedelta(minutes=30))
    post_30_60 = _window_values(records, end + timedelta(minutes=30), end + timedelta(minutes=60))
    post_60_120 = _window_values(records, end + timedelta(minutes=60), end + timedelta(minutes=120))

    baseline_median = safe_median(item.smoothed_pm25 for item in baseline)
    during_values = [item.smoothed_pm25 for item in during]
    response_values = during + post
    peak = max(response_values, key=lambda item: item.smoothed_pm25) if response_values else None
    during_median = safe_median(during_values)
    post_median = safe_median(item.smoothed_pm25 for item in post)
    delta = during_median - baseline_median if during_median is not None and baseline_median is not None else None
    denominator = max(abs(baseline_median), 1.0) if baseline_median is not None else None
    return {
        "baseline_sample_count": len(baseline),
        "during_sample_count": len(during),
        "post_sample_count": len(post),
        "baseline_median": number(baseline_median),
        "peak_pm25": number(peak.smoothed_pm25 if peak else None),
        "peak_excess_over_baseline": number(peak.smoothed_pm25 - baseline_median if peak and baseline_median is not None else None),
        "time_of_peak_utc": iso_utc(peak.observed_at if peak else None),
        "median_during_fire": number(during_median),
        "median_post_fire": number(post_median),
        "absolute_delta": number(delta),
        "relative_delta_percent": number(100.0 * delta / denominator if delta is not None and denominator else None),
        "relative_delta_denominator_floor_ugm3": 1.0,
        "post_0_30_median": safe_median(item.smoothed_pm25 for item in post_0_30),
        "post_30_60_median": safe_median(item.smoothed_pm25 for item in post_30_60),
        "post_60_120_median": safe_median(item.smoothed_pm25 for item in post_60_120),
    }


def classify_response(
    responses: list[dict[str, Any]],
    *,
    background_deltas: list[float] | None = None,
    clear_peak_excess: float = 5.0,
    weak_peak_excess: float = 2.0,
    regional_delta: float = 2.0,
    regional_fraction: float = 0.5,
) -> str:
    valid = [item for item in responses if finite(item.get("peak_excess_over_baseline"))]
    if not valid:
        return "INSUFFICIENT_SENSOR_COVERAGE"
    positive = [item for item in valid if float(item["peak_excess_over_baseline"]) >= weak_peak_excess]
    clear = [item for item in valid if float(item["peak_excess_over_baseline"]) >= clear_peak_excess]
    background = [float(value) for value in (background_deltas or []) if finite(value)]
    if background and sum(value >= regional_delta for value in background) / len(background) >= regional_fraction:
        local_positive = len(positive) / len(valid)
        if local_positive >= regional_fraction:
            return "AMBIGUOUS_REGIONAL_BACKGROUND"
    downwind_clear = [item for item in clear if item.get("wind_relation") == "downwind"]
    if len(clear) >= 2 and (len(downwind_clear) >= 2 or len(clear) >= 3):
        return "CLEAR_SENSOR_RESPONSE"
    if positive:
        return "WEAK_SENSOR_RESPONSE"
    return "NO_MEASURABLE_RESPONSE"


def load_events(path: Path) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    timezone_name = str(payload.get("timezone", "Asia/Taipei"))
    events = []
    for source in payload.get("events", []):
        location = source["location"]
        if not finite(location.get("lat")) or not finite(location.get("lon")):
            raise ValueError(f"{source.get('event_id')}: location must have confirmed lat/lon")
        item = dict(source)
        item["start_utc"] = parse_event_time(source["fire_start_local"], timezone_name)
        item["end_utc"] = parse_event_time(source["fire_end_local"], timezone_name)
        if item["end_utc"] < item["start_utc"]:
            raise ValueError(f"{source.get('event_id')}: fire end precedes start")
        events.append(item)
    if not events:
        raise ValueError("known-fire config contains no events")
    return events


def read_database_snapshot(database_path: Path, start: datetime, end: datetime) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    connection = duckdb.connect(str(database_path), read_only=True)
    try:
        stations = [
            dict(zip(("thing_id", "station_id", "station_name", "lat", "lon", "city", "township", "area_type"), row))
            for row in connection.execute(
                "SELECT thing_id, station_id, station_name, lat, lon, city, township, area_type FROM sensor_station ORDER BY station_id"
            ).fetchall()
        ]
        observations = [
            dict(zip(("station_id", "datastream_id", "phenomenon_time_utc", "pm25_ugm3", "source_status", "quality_flags"), row))
            for row in connection.execute(
                """SELECT station_id, datastream_id, phenomenon_time_utc, pm25_ugm3, source_status, quality_flags
                   FROM pm25_observation WHERE phenomenon_time_utc >= ? AND phenomenon_time_utc < ?
                   ORDER BY station_id, phenomenon_time_utc""",
                [start, end],
            ).fetchall()
        ]
        return stations, observations
    finally:
        connection.close()


def load_replay_context(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"available": False, "station_ids": set(), "statuses": {}, "window": None}
    payload = json.loads(path.read_text(encoding="utf-8"))
    return {
        "available": True,
        "station_ids": {str(item["station_id"]) for item in payload.get("context_sensors", [])},
        "statuses": {str(item["station_id"]): item.get("status") for item in payload.get("context_sensors", [])},
        "window": payload.get("analysis_window"),
        "summary": payload.get("summary", {}),
    }


def station_coverage(
    station: dict[str, Any],
    records: list[BinnedObservation],
    event: dict[str, Any],
    replay: dict[str, Any],
    *,
    analysis_start: datetime,
    analysis_end: datetime,
) -> dict[str, Any]:
    start, end = event["start_utc"], event["end_utc"]
    distance = haversine_km((float(event["location"]["lon"]), float(event["location"]["lat"])), (float(station["lon"]), float(station["lat"])))
    valid_bins = {item.bucket for item in records if analysis_start <= item.bucket < analysis_end}
    expected_bins = max(1, math.ceil((analysis_end - analysis_start).total_seconds() / 180.0))
    current = max((item for item in records if item.observed_at <= start), key=lambda item: item.observed_at, default=None)
    history = [item for item in records if current and current.bucket - timedelta(minutes=60) <= item.bucket < current.bucket]
    age = (start - current.observed_at).total_seconds() / 60.0 if current else None
    fresh = current is not None and 0 <= age <= 3.0
    history_complete = len(history) >= 10
    active = _window_values(records, start, end)
    return {
        "station_id": str(station["station_id"]),
        "station_name": station.get("station_name"),
        "lat": number(station.get("lat")),
        "lon": number(station.get("lon")),
        "distance_km": number(distance),
        "distance_bucket": distance_bucket(distance),
        "pm25_data_coverage_percent": number(min(100.0, 100.0 * len(valid_bins) / expected_bins)),
        "valid_binned_points": len(valid_bins),
        "expected_binned_points": expected_bins,
        "usable_in_active_interval": bool(active),
        "fresh_at_fire_start": fresh,
        "history_complete_at_fire_start": history_complete,
        "fresh_and_history_complete": fresh and history_complete,
        "history_sample_count_at_fire_start": len(history),
        "current_age_minutes_at_fire_start": number(age),
        "included_in_context_diagnostic_replay": str(station["station_id"]) in replay["station_ids"],
        "context_replay_status_at_replay_end": replay["statuses"].get(str(station["station_id"])),
    }


def rows_for_station(results_by_bin: dict[datetime, dict[str, Any]], station_id: str, start: datetime, end: datetime) -> list[dict[str, Any]]:
    rows = []
    current = floor_time(start, 3)
    while current < end:
        row = results_by_bin.get(current, {}).get(station_id)
        if row:
            rows.append(dict(row))
        current += timedelta(minutes=3)
    return rows


def anomaly_miss_reason(row: dict[str, Any] | None, config: AnomalyConfig) -> list[str]:
    if row is None:
        return ["no_anomaly_row"]
    reasons = []
    if row.get("temporal_status") != "sufficient":
        reasons.append(str(row.get("temporal_status")))
    if row.get("spatial_status") != "sufficient":
        reasons.append(str(row.get("spatial_status")))
    for key, threshold in (
        ("temporal_excess", config.temporal_excess_threshold),
        ("temporal_z", config.temporal_z_threshold),
        ("spatial_excess", config.spatial_excess_threshold),
        ("spatial_z", config.spatial_z_threshold),
    ):
        if not finite(row.get(key)) or float(row[key]) < threshold:
            reasons.append(f"{key}_below_threshold")
    if int(row.get("spatial_support_count") or 0) < 1:
        reasons.append("spatial_support_count_below_one")
    if not reasons and not row.get("is_candidate"):
        reasons.append("candidate_gate_false")
    return reasons


def build_event_report(
    event: dict[str, Any],
    stations: list[dict[str, Any]],
    series: dict[str, list[BinnedObservation]],
    results_by_bin: dict[datetime, dict[str, Any]],
    wind_field: WindFieldSnapshot | None,
    replay: dict[str, Any],
    anomaly_config: AnomalyConfig,
    analysis_start: datetime,
    analysis_end: datetime,
) -> dict[str, Any]:
    event_lat, event_lon = float(event["location"]["lat"]), float(event["location"]["lon"])
    nearby = [
        station for station in stations
        if finite(station.get("lat")) and finite(station.get("lon"))
        and haversine_km((event_lon, event_lat), (float(station["lon"]), float(station["lat"]))) <= 5.0
    ]
    nearby.sort(key=lambda station: haversine_km((event_lon, event_lat), (float(station["lon"]), float(station["lat"]))))
    coverage = [station_coverage(station, series.get(str(station["station_id"]), []), event, replay, analysis_start=analysis_start, analysis_end=analysis_end) for station in nearby]
    wind = None
    if wind_field is not None:
        midpoint = event["start_utc"] + (event["end_utc"] - event["start_utc"]) / 2
        wind = estimate_to_dict(get_wind(event_lat, event_lon, midpoint, snapshot=wind_field))
    responses = []
    anomaly_rows = []
    for item in coverage:
        station_id = item["station_id"]
        pm = pm_response_metrics(series.get(station_id, []), event["start_utc"], event["end_utc"])
        active_rows = rows_for_station(results_by_bin, station_id, event["start_utc"], event["end_utc"])
        best = max(active_rows, key=lambda row: float(row.get("anomaly_score") or -1), default=None)
        temporal_best = max(active_rows, key=lambda row: float(row.get("temporal_excess") or -math.inf), default=None)
        spatial_best = max(active_rows, key=lambda row: float(row.get("spatial_excess") or -math.inf), default=None)
        relation = "unknown"
        if wind and finite(wind.get("wind_to_deg")):
            relation = classify_wind_relation(bearing_deg(event_lat, event_lon, item["lat"], item["lon"]), wind["wind_to_deg"])
        pm["wind_relation"] = relation
        if wind:
            pm["fire_to_sensor_bearing_deg"] = bearing_deg(event_lat, event_lon, item["lat"], item["lon"])
            pm["wind_to_deg"] = wind.get("wind_to_deg")
            pm["wind_speed_mps"] = wind.get("speed_mps")
            travel = nominal_travel_time_minutes(item["distance_km"], wind.get("speed_mps")) if relation == "downwind" else None
            peak_time = parse_iso_utc(pm["time_of_peak_utc"]) if pm.get("time_of_peak_utc") else None
            actual_delay = (peak_time - event["start_utc"]).total_seconds() / 60.0 if peak_time else None
            pm["expected_nominal_arrival_delay_minutes"] = travel
            pm["expected_nominal_arrival_utc"] = iso_utc(event["start_utc"] + timedelta(minutes=travel)) if travel is not None else None
            pm["actual_peak_delay_minutes"] = number(actual_delay)
            pm["arrival_difference_minutes"] = number(actual_delay - travel) if actual_delay is not None and travel is not None else None
        response = item | pm
        responses.append(response)
        if best:
            margins = {
                "temporal_excess": threshold_margin(best.get("temporal_excess"), anomaly_config.temporal_excess_threshold),
                "temporal_z": threshold_margin(best.get("temporal_z"), anomaly_config.temporal_z_threshold),
                "spatial_excess": threshold_margin(best.get("spatial_excess"), anomaly_config.spatial_excess_threshold),
                "spatial_z": threshold_margin(best.get("spatial_z"), anomaly_config.spatial_z_threshold),
            }
            anomaly_rows.append({
                "station_id": station_id,
                "best_active_bin_utc": best.get("analysis_time_utc"),
                "quality_flags": best.get("quality_flags"),
                "temporal_status": best.get("temporal_status"),
                "spatial_status": best.get("spatial_status"),
                "anomaly_score": best.get("anomaly_score"),
                "is_candidate": bool(best.get("is_candidate")),
                "temporal_excess": best.get("temporal_excess"),
                "temporal_z": best.get("temporal_z"),
                "spatial_excess": best.get("spatial_excess"),
                "spatial_z": best.get("spatial_z"),
                "spatial_support_count": best.get("spatial_support_count"),
                "threshold_margins": margins,
                "detector_miss_reason": anomaly_miss_reason(best, anomaly_config),
            })
        else:
            anomaly_rows.append({"station_id": station_id, "threshold_margins": {}, "detector_miss_reason": ["no_anomaly_row"]})

    context_deltas = []
    nearby_ids = {item["station_id"] for item in coverage}
    for station in stations:
        station_id = str(station["station_id"])
        if station_id in nearby_ids or not finite(station.get("lat")) or not finite(station.get("lon")):
            continue
        pm = pm_response_metrics(series.get(station_id, []), event["start_utc"], event["end_utc"])
        if finite(pm.get("absolute_delta")):
            context_deltas.append(float(pm["absolute_delta"]))

    active_rows = [row for row in anomaly_rows if finite(row.get("anomaly_score"))]
    temporal_pass_ids = set()
    spatial_pass_ids = set()
    candidate_ids = set()
    for station_id in {item["station_id"] for item in coverage}:
        station_rows = rows_for_station(results_by_bin, station_id, event["start_utc"], event["end_utc"])
        if any(row.get("temporal_status") == "sufficient" and finite(row.get("temporal_excess")) and float(row["temporal_excess"]) >= anomaly_config.temporal_excess_threshold and finite(row.get("temporal_z")) and float(row["temporal_z"]) >= anomaly_config.temporal_z_threshold for row in station_rows):
            temporal_pass_ids.add(station_id)
        if any(row.get("spatial_status") == "sufficient" and finite(row.get("spatial_excess")) and float(row["spatial_excess"]) >= anomaly_config.spatial_excess_threshold and finite(row.get("spatial_z")) and float(row["spatial_z"]) >= anomaly_config.spatial_z_threshold for row in station_rows):
            spatial_pass_ids.add(station_id)
        if any(bool(row.get("is_candidate")) for row in station_rows):
            candidate_ids.add(station_id)

    response_class = classify_response(responses, background_deltas=context_deltas)
    counts_by_radius = {
        f"{radius:g}km": {
            "total_sensors": sum(float(item["distance_km"]) <= radius for item in coverage),
            "usable_sensors": sum(float(item["distance_km"]) <= radius and item["usable_in_active_interval"] for item in coverage),
            "fresh_history_complete_sensors": sum(float(item["distance_km"]) <= radius and item["fresh_and_history_complete"] for item in coverage),
        }
        for radius in (0.5, 1.0, 2.0, 3.0, 5.0)
    }
    wind_groups = {}
    for relation in ("downwind", "crosswind", "upwind", "unknown"):
        values = [float(item["peak_excess_over_baseline"]) for item in responses if item.get("wind_relation") == relation and finite(item.get("peak_excess_over_baseline"))]
        wind_groups[relation] = {"sensor_count": len(values), "median_peak_excess": number(statistics.median(values)) if values else None, "max_peak_excess": number(max(values)) if values else None}
    closest = coverage[0] if coverage else None
    highest = max(responses, key=lambda item: float(item.get("peak_excess_over_baseline") or -math.inf), default=None)
    highest_anomaly = max(active_rows, key=lambda item: float(item.get("anomaly_score") or -math.inf), default=None)
    strongest_temporal = max((row for row in anomaly_rows if finite(row.get("temporal_excess"))), key=lambda row: float(row["temporal_excess"]), default=None)
    strongest_spatial = max((row for row in anomaly_rows if finite(row.get("spatial_excess"))), key=lambda row: float(row["spatial_excess"]), default=None)
    return {
        "event": {
            "event_id": event["event_id"],
            "name": event["name"],
            "fire_start_local": event["fire_start_local"],
            "fire_end_local": event["fire_end_local"],
            "fire_start_utc": iso_utc(event["start_utc"]),
            "fire_end_utc": iso_utc(event["end_utc"]),
            "location": event["location"],
        },
        "analysis_window": {"start_utc": iso_utc(event["start_utc"] - timedelta(minutes=60)), "end_utc": iso_utc(event["end_utc"] + timedelta(minutes=120)), "baseline_minutes": 60, "post_minutes": 120, "time_basis": "phenomenon_time_utc"},
        "method": {
            "scope": "post-hoc known-fire response diagnostic; not source attribution and not production event detection",
            "distance_buckets_km": [0.5, 1, 2, 3, 5],
            "relative_delta_denominator_floor_ugm3": 1.0,
            "wind_relation_note": "geometric heuristic using fire-to-sensor bearing and production wind_to direction; downwind <=45°, crosswind <135°, upwind >=135°",
            "travel_time_note": "distance / wind speed nominal advection only; not an atmospheric dispersion model",
            "fresh_definition": "historical latest PM2.5 at fire start is no more than 3 minutes old",
            "history_complete_definition": "at least 10 three-minute smoothed bins in the preceding 60 minutes",
            "classification_rules": {"clear_peak_excess_ugm3": 5.0, "weak_peak_excess_ugm3": 2.0, "regional_delta_ugm3": 2.0, "regional_fraction": 0.5},
        },
        "sensor_inventory": {"counts_by_radius": counts_by_radius, "nearest_20": coverage[:20], "total_within_5km": len(coverage)},
        "pm25_responses": responses,
        "anomaly": {
            "highest_anomaly_score": highest_anomaly,
            "closest_sensor": closest,
            "strongest_temporal_only_rise": strongest_temporal,
            "strongest_spatial_only_rise": strongest_spatial,
            "number_sensors_passing_temporal_conditions": len(temporal_pass_ids),
            "number_sensors_passing_spatial_conditions": len(spatial_pass_ids),
            "number_sensors_passing_all_candidate_conditions": len(candidate_ids),
            "threshold_diagnostic": anomaly_rows,
        },
        "wind": {"estimate": wind, "quality_warning": bool(wind and str(wind.get("quality_category", "")).upper() in {"HIGH_UNCERTAINTY", "CALM / LOW_DIRECTION_CONFIDENCE", "INSUFFICIENT_STATIONS"}), "relation_comparison": wind_groups},
        "regional_background": {"outside_5km_delta_count": len(context_deltas), "outside_5km_delta_median": number(statistics.median(context_deltas)) if context_deltas else None, "outside_5km_deltas": [number(value) for value in context_deltas[:200]]},
        "classification": response_class,
        "diagnostic_miss_reason": Counter(reason for row in anomaly_rows for reason in row.get("detector_miss_reason", [])).most_common(),
        "timeline": {station_id: rows_for_station(results_by_bin, station_id, event["start_utc"] - timedelta(minutes=60), event["end_utc"] + timedelta(minutes=120)) for station_id in [item["station_id"] for item in coverage[:20]]},
    }


def replay_anomaly_from_snapshot(
    stations: list[dict[str, Any]],
    observations: list[dict[str, Any]],
    config_path: Path,
    start: datetime,
    end: datetime,
    config: AnomalyConfig,
    now: datetime,
) -> dict[datetime, dict[str, Any]]:
    """Replay production station analysis without reopening a locked DB.

    ``_analyse_station`` is the same production implementation used by
    ``detect_anomalies``. The only difference is that this diagnostic passes a
    consistent read-only snapshot to it for every bin, which is necessary on
    Windows while the recorder owns the DuckDB file.
    """
    pilot = load_pilot_config(config_path)
    context_stations = [
        station for station in stations
        if bbox_contains(station.get("lat"), station.get("lon"), pilot["context_bbox"])
    ]
    all_context = {str(station["station_id"]): station for station in context_stations}
    series: dict[str, list[BinnedObservation]] = {}
    for item in _smooth_series(observations, config):
        series.setdefault(item.station_id, []).append(item)
    output: dict[datetime, dict[str, Any]] = {}
    current = floor_time(start, config.bin_minutes)
    last = floor_time(end, config.bin_minutes)
    while current <= last:
        cutoff = current + timedelta(minutes=config.bin_minutes) - timedelta(microseconds=1)
        rows = {}
        for station in sorted(context_stations, key=lambda item: str(item["station_id"])):
            row, _ = _analyse_station(station, series, all_context, cutoff, now, config)
            rows[str(row["station_id"])] = row
        output[current] = rows
        current += timedelta(minutes=config.bin_minutes)
    return output


def html_detail(payload: dict[str, Any]) -> str:
    blob = json.dumps(payload, ensure_ascii=False, allow_nan=False).replace("</", "<\\/")
    template = '''<!doctype html><html lang="en"><head><meta charset="utf-8"><title>__TITLE__ response diagnostic</title>
<style>body{{font:14px system-ui,sans-serif;margin:24px;color:#17202a}}h1{{margin-bottom:4px}}.note{{color:#5d6d7e}}.grid{{display:grid;grid-template-columns:1fr 1fr;gap:16px}}.card{{border:1px solid #d5d8dc;border-radius:8px;padding:12px;overflow:auto}}table{{border-collapse:collapse;width:100%;font-size:12px}}th,td{{border-bottom:1px solid #e5e7e9;padding:5px;text-align:left;white-space:nowrap}}th{{background:#f4f6f7}}canvas,svg{{width:100%;height:auto;border:1px solid #e5e7e9;background:#fff}}.pill{{display:inline-block;padding:4px 8px;border-radius:10px;background:#eaf2f8;font-weight:600}}.warn{{background:#fef2e0}}pre{{white-space:pre-wrap}}</style></head><body>
<h1>__TITLE__ — Known Fire Response Diagnostic</h1><div class="note">__SCOPE__ · __ACCURACY__</div>
<p><span class="pill" id="classification"></span> Fire: <span id="firetime"></span></p><div class="grid"><div class="card"><h2>Location / wind geometry</h2><svg id="map" viewBox="0 0 720 330"></svg><p id="wind"></p></div><div class="card"><h2>Response summary</h2><pre id="summary"></pre><h3>Wind relation comparison</h3><pre id="windgroups"></pre></div></div>
<div class="card"><h2>PM2.5 timeline and anomaly score (nearest 20)</h2><canvas id="chart" width="1200" height="520"></canvas></div>
<div class="card"><h2>Nearest sensors and PM2.5 response</h2><table id="responses"></table></div><div class="card"><h2>Existing v1 anomaly threshold margins</h2><table id="margins"></table></div>
<script>const D=__BLOB__;const fmt=v=>v==null?'—':(typeof v==='number'?v.toFixed(2):v);document.getElementById('classification').textContent=D.classification;document.getElementById('firetime').textContent=D.event.fire_start_utc+' → '+D.event.fire_end_utc;
const w=D.wind.estimate;document.getElementById('wind').textContent=w?`wind_to ${{fmt(w.wind_to_deg)}}°, speed ${{fmt(w.speed_mps)}} m/s, quality ${{w.quality_category}}`:'wind unavailable';document.getElementById('summary').textContent=JSON.stringify({inventory:D.sensor_inventory.counts_by_radius,highest_anomaly:D.anomaly.highest_anomaly_score,temporal_pass:D.anomaly.number_sensors_passing_temporal_conditions,spatial_pass:D.anomaly.number_sensors_passing_spatial_conditions,candidate_pass:D.anomaly.number_sensors_passing_all_candidate_conditions,miss_reason:D.diagnostic_miss_reason},null,2);document.getElementById('windgroups').textContent=JSON.stringify(D.wind.relation_comparison,null,2);
const svg=document.getElementById('map'), pts=D.sensor_inventory.nearest_20, all=pts.concat([{lat:D.event.location.lat,lon:D.event.location.lon}]);let minLat=Math.min(...all.map(x=>x.lat)),maxLat=Math.max(...all.map(x=>x.lat)),minLon=Math.min(...all.map(x=>x.lon)),maxLon=Math.max(...all.map(x=>x.lon));const sx=lon=>40+(lon-minLon)/(maxLon-minLon||1)*640,sy=lat=>290-(lat-minLat)/(maxLat-minLat||1)*250;svg.innerHTML=`<rect x="0" y="0" width="720" height="330" fill="#f8fafc"/><circle cx="${sx(D.event.location.lon)}" cy="${sy(D.event.location.lat)}" r="8" fill="#d92d20"/><text x="${sx(D.event.location.lon)+10}" y="${sy(D.event.location.lat)}">fire</text>`+pts.map(p=>`<circle cx="${sx(p.lon)}" cy="${sy(p.lat)}" r="${p.fresh_and_history_complete?5:3}" fill="${p.wind_relation==='downwind'?'#1677ff':'#64748b'}"/><title>${p.station_id} ${fmt(p.distance_km)} km</title>`).join('')+(w&&w.wind_to_deg!=null?`<line x1="${sx(D.event.location.lon)}" y1="${sy(D.event.location.lat)}" x2="${sx(D.event.location.lon)+55*Math.sin(w.wind_to_deg*Math.PI/180)}" y2="${sy(D.event.location.lat)-55*Math.cos(w.wind_to_deg*Math.PI/180)}" stroke="#d97706" stroke-width="3" marker-end="url(#arrow)"/>`:'');
const rows=D.pm25_responses.slice(0,20);document.getElementById('responses').innerHTML='<tr><th>station</th><th>dist km</th><th>coverage %</th><th>base</th><th>peak excess</th><th>during median</th><th>post median</th><th>wind</th><th>peak UTC</th></tr>'+rows.map(r=>`<tr><td>${r.station_id}</td><td>${fmt(r.distance_km)}</td><td>${fmt(r.pm25_data_coverage_percent)}</td><td>${fmt(r.baseline_median)}</td><td>${fmt(r.peak_excess_over_baseline)}</td><td>${fmt(r.median_during_fire)}</td><td>${fmt(r.median_post_fire)}</td><td>${r.wind_relation}</td><td>${r.time_of_peak_utc||'—'}</td></tr>`).join('');const ms=D.anomaly.threshold_diagnostic.slice(0,20);document.getElementById('margins').innerHTML='<tr><th>station</th><th>best bin</th><th>score</th><th>temporal excess</th><th>temporal z</th><th>spatial excess</th><th>spatial z</th><th>miss reason</th></tr>'+ms.map(r=>`<tr><td>${r.station_id}</td><td>${r.best_active_bin_utc||'—'}</td><td>${fmt(r.anomaly_score)}</td><td>${r.threshold_margins.temporal_excess?r.threshold_margins.temporal_excess.pass?'PASS':'FAIL':'—'}</td><td>${r.threshold_margins.temporal_z?r.threshold_margins.temporal_z.pass?'PASS':'FAIL':'—'}</td><td>${r.threshold_margins.spatial_excess?r.threshold_margins.spatial_excess.pass?'PASS':'FAIL':'—'}</td><td>${r.threshold_margins.spatial_z?r.threshold_margins.spatial_z.pass?'PASS':'FAIL':'—'}</td><td>${(r.detector_miss_reason||[]).join(', ')}</td></tr>`).join('');
const c=document.getElementById('chart'),ctx=c.getContext('2d'),names=Object.keys(D.timeline);let points=[];names.forEach(id=>D.timeline[id].forEach(x=>points.push({t:new Date(x.analysis_time_utc),pm:x.smoothed_pm25,score:x.anomaly_score,id})));let ts=points.map(x=>x.t.getTime()),t0=Math.min(...ts),t1=Math.max(...ts),pm=points.map(x=>x.pm).filter(x=>x!=null),p0=pm.length?Math.min(...pm):0,p1=pm.length?Math.max(...pm):1;const X=t=>55+(t-t0)/(t1-t0||1)*1100,YP=v=>470-(v-p0)/(p1-p0||1)*390,YS=v=>470-(v/10)*390;ctx.clearRect(0,0,c.width,c.height);ctx.fillStyle='#fff4d6';ctx.fillRect(X(new Date(D.event.fire_start_utc).getTime()),55,X(new Date(D.event.fire_end_utc).getTime())-X(new Date(D.event.fire_start_utc).getTime()),415);ctx.strokeStyle='#d5d8dc';ctx.beginPath();ctx.moveTo(55,470);ctx.lineTo(1155,470);ctx.stroke();names.forEach((id,i)=>{let line=D.timeline[id].filter(x=>x.smoothed_pm25!=null);ctx.strokeStyle=`hsl(${i*47%360} 55% 42%)`;ctx.beginPath();line.forEach((x,j)=>{let xx=X(new Date(x.analysis_time_utc).getTime()),yy=YP(x.smoothed_pm25);j?ctx.lineTo(xx,yy):ctx.moveTo(xx,yy)});ctx.stroke();let s=D.timeline[id].filter(x=>x.anomaly_score!=null);ctx.strokeStyle=`hsl(${i*47%360} 55% 42% / .35)`;ctx.beginPath();s.forEach((x,j)=>{let xx=X(new Date(x.analysis_time_utc).getTime()),yy=YS(x.anomaly_score);j?ctx.lineTo(xx,yy):ctx.moveTo(xx,yy)});ctx.stroke()});ctx.fillStyle='#17202a';ctx.fillText('PM2.5',8,65);ctx.fillText('anomaly score (0–10)',8,85);ctx.fillText('fire active interval',X(new Date(D.event.fire_start_utc).getTime())+5,70);</script></body></html>'''
    template = template.replace("{{", "{").replace("}}", "}")
    return template.replace("__TITLE__", html.escape(payload["event"]["name"])).replace("__SCOPE__", html.escape(payload["method"]["scope"])).replace("__ACCURACY__", html.escape(payload["event"]["location"]["accuracy"])).replace("__BLOB__", blob)


def write_summary_html(summary: dict[str, Any], output: Path) -> None:
    rows = summary["events"]
    body = "".join(f'<tr><td><a href="{html.escape(item["detail_html"])}">{html.escape(item["name"])}</a></td><td>{html.escape(item["classification"])}</td><td>{item.get("nearest_usable_sensor_distance_km") or "—"}</td><td>{item.get("maximum_pm25_excess") or "—"}</td><td>{item.get("maximum_anomaly_score") or "—"}</td><td>{item.get("temporal_pass_count")}</td><td>{item.get("spatial_pass_count")}</td><td>{item.get("candidate_pass_count")}</td></tr>' for item in rows)
    output.write_text(f'''<!doctype html><html><head><meta charset="utf-8"><title>Known Fire Response Summary</title><style>body{{font:14px system-ui;margin:24px;color:#17202a}}table{{border-collapse:collapse;width:100%}}th,td{{border:1px solid #d5d8dc;padding:8px;text-align:left}}th{{background:#f4f6f7}}.note{{color:#5d6d7e}}</style></head><body><h1>Known Fire Response Diagnostic</h1><p class="note">Post-hoc diagnostic only. Blind context replay is preserved and production detector settings were not modified.</p><table><tr><th>event</th><th>classification</th><th>nearest usable km</th><th>max PM2.5 excess</th><th>max anomaly score</th><th>temporal pass</th><th>spatial pass</th><th>candidate pass</th></tr>{body}</table><pre>{html.escape(json.dumps(summary.get("method"),ensure_ascii=False,indent=2))}</pre></body></html>''', encoding="utf-8")


def write_csv(summary: dict[str, Any], output: Path) -> None:
    fields = ["event_id", "name", "fire_start_utc", "fire_end_utc", "nearest_usable_sensor_distance_km", "sensor_count_within_1km", "sensor_count_within_2km", "sensor_count_within_3km", "maximum_pm25_excess", "maximum_anomaly_score", "temporal_pass_count", "spatial_pass_count", "candidate_pass_count", "downwind_median_peak_excess", "upwind_median_peak_excess", "crosswind_median_peak_excess", "detector_miss_reason", "classification", "detail_html"]
    with output.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows({field: item.get(field) for field in fields} for item in summary["events"])


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--events", type=Path, default=DEFAULT_EVENTS)
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE)
    parser.add_argument("--weather-database", type=Path, default=DEFAULT_WEATHER_DATABASE)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--replay-report", type=Path, default=REPLAY_REPORT)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    events = load_events(args.events)
    analysis_start = min(event["start_utc"] for event in events) - timedelta(minutes=60)
    analysis_end = max(event["end_utc"] for event in events) + timedelta(minutes=120)
    query_start = min(event["start_utc"] for event in events) - timedelta(hours=2)
    stations, observations = read_database_snapshot(args.database, query_start, analysis_end)
    config = AnomalyConfig()
    series: dict[str, list[BinnedObservation]] = {}
    for item in _smooth_series(observations, config):
        series.setdefault(item.station_id, []).append(item)
    replay = load_replay_context(args.replay_report)
    results_by_bin = replay_anomaly_from_snapshot(
        stations, observations, args.config, analysis_start, analysis_end,
        config, datetime.now(UTC),
    )
    wind_field = load_wind_snapshot_range(args.weather_database, analysis_start, analysis_end) if args.weather_database.exists() else None
    reports = []
    args.output_dir.mkdir(parents=True, exist_ok=True)
    for event in events:
        report = build_event_report(event, stations, series, results_by_bin, wind_field, replay, config, analysis_start, analysis_end)
        detail_path = args.output_dir / f"{event['event_id']}_response.html"
        detail_path.write_text(html_detail(report), encoding="utf-8")
        anomaly = report["anomaly"]
        wind_groups = report["wind"]["relation_comparison"]
        counts = report["sensor_inventory"]["counts_by_radius"]
        nearest_usable = next((item for item in report["sensor_inventory"]["nearest_20"] if item["usable_in_active_interval"]), None)
        summary_item = {
            "event_id": event["event_id"], "name": event["name"], "fire_start_utc": report["event"]["fire_start_utc"], "fire_end_utc": report["event"]["fire_end_utc"],
            "nearest_usable_sensor_distance_km": nearest_usable["distance_km"] if nearest_usable else None,
            "sensor_count_within_1km": counts["1km"]["total_sensors"], "sensor_count_within_2km": counts["2km"]["total_sensors"], "sensor_count_within_3km": counts["3km"]["total_sensors"],
            "maximum_pm25_excess": max((item["peak_excess_over_baseline"] for item in report["pm25_responses"] if finite(item.get("peak_excess_over_baseline"))), default=None),
            "maximum_anomaly_score": anomaly["highest_anomaly_score"].get("anomaly_score") if anomaly["highest_anomaly_score"] else None,
            "temporal_pass_count": anomaly["number_sensors_passing_temporal_conditions"], "spatial_pass_count": anomaly["number_sensors_passing_spatial_conditions"], "candidate_pass_count": anomaly["number_sensors_passing_all_candidate_conditions"],
            "downwind_median_peak_excess": wind_groups["downwind"]["median_peak_excess"], "upwind_median_peak_excess": wind_groups["upwind"]["median_peak_excess"], "crosswind_median_peak_excess": wind_groups["crosswind"]["median_peak_excess"],
            "detector_miss_reason": "; ".join(f"{key}:{value}" for key, value in report["diagnostic_miss_reason"]), "classification": report["classification"], "detail_html": detail_path.name,
        }
        reports.append({"report": report, "summary": summary_item})
    summary = {"schema_version": 1, "generated_at_utc": iso_utc(datetime.now(UTC)), "blind_replay_preserved": True, "blind_replay_report": str(args.replay_report), "known_events_config": str(args.events), "production_detector_modified": False, "events": [item["summary"] for item in reports], "method": reports[0]["report"]["method"] if reports else {}}
    (args.output_dir / "known_fire_response_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    write_csv(summary, args.output_dir / "known_fire_response_summary.csv")
    write_summary_html(summary, args.output_dir / "known_fire_response.html")
    print("AirTrace Known Fire Response Diagnostic")
    for item in summary["events"]:
        print(f"{item['name']}: {item['classification']} | nearest usable={item['nearest_usable_sensor_distance_km']} km | max excess={item['maximum_pm25_excess']} | max score={item['maximum_anomaly_score']} | temporal/spatial/candidate={item['temporal_pass_count']}/{item['spatial_pass_count']}/{item['candidate_pass_count']}")
    print(f"JSON: {args.output_dir / 'known_fire_response_summary.json'}")
    print(f"CSV: {args.output_dir / 'known_fire_response_summary.csv'}")
    print(f"HTML: {args.output_dir / 'known_fire_response.html'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
