"""Explainable, sensor-level PM2.5 anomaly diagnostics.

This module deliberately stops at local sensor anomaly candidates.  It does
not infer a source, classify an event, or write to the recorder database.
"""

from __future__ import annotations

import csv
import json
import math
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import median
from typing import Any, Iterable

import duckdb


EARTH_RADIUS_KM = 6371.0088
UTC = timezone.utc
ANALYSIS_ZONES = ("core", "context")
CONTEXT_DIAGNOSTIC_REPLAY = "CONTEXT DIAGNOSTIC REPLAY"
NOT_PRODUCTION_EVENT_DETECTION = "NOT PRODUCTION EVENT DETECTION"
REQUIRED_REPORT_FIELDS = (
    "analysis_time_utc", "station_id", "lat", "lon", "raw_pm25",
    "smoothed_pm25", "temporal_median", "temporal_mad", "temporal_excess",
    "temporal_z", "temporal_sample_count", "temporal_status",
    "spatial_median", "spatial_mad", "spatial_excess", "spatial_z",
    "neighbor_count", "spatial_radius_km", "spatial_status", "anomaly_score",
    "is_candidate", "quality_flags",
)


def validate_analysis_zone(value: str) -> str:
    zone = str(value).strip().lower()
    if zone not in ANALYSIS_ZONES:
        raise ValueError(f"analysis_zone must be one of: {', '.join(ANALYSIS_ZONES)}")
    return zone


@dataclass(frozen=True)
class AnomalyConfig:
    """All v1 knobs, including values that are intentionally heuristics."""

    bin_minutes: int = 3
    baseline_minutes: int = 60
    min_temporal_samples: int = 10
    neighbor_radius_km: float = 1.0
    max_neighbor_radius_km: float = 1.5
    min_neighbors: int = 3
    robust_sigma_floor: float = 1.5
    smoothing_points: int = 3
    max_current_age_minutes: float = 3.0
    temporal_excess_threshold: float = 5.0
    spatial_excess_threshold: float = 5.0
    temporal_z_threshold: float = 3.0
    spatial_z_threshold: float = 3.0


@dataclass(frozen=True)
class BinnedObservation:
    station_id: str
    bucket: datetime
    observed_at: datetime
    raw_pm25: float
    smoothed_pm25: float
    quality_flags: tuple[str, ...] = ()


@dataclass(frozen=True)
class RobustStats:
    median: float | None
    mad: float | None
    sigma: float | None


@dataclass
class DetectionResult:
    payload: dict[str, Any]
    rows: list[dict[str, Any]]
    context_sensors: list[dict[str, Any]]


def utc_datetime(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def iso_utc(value: datetime | None) -> str | None:
    if value is None:
        return None
    return utc_datetime(value).isoformat(timespec="seconds").replace("+00:00", "Z")


def parse_iso_utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    return utc_datetime(parsed)


def floor_time(value: datetime, minutes: int) -> datetime:
    value = utc_datetime(value)
    seconds = int(value.timestamp())
    step = minutes * 60
    return datetime.fromtimestamp(seconds - seconds % step, UTC)


def haversine_km(first: tuple[float, float], second: tuple[float, float]) -> float:
    """Great-circle distance for (lon, lat) points in WGS84 degrees."""

    lon1, lat1 = map(math.radians, first)
    lon2, lat2 = map(math.radians, second)
    dlon, dlat = lon2 - lon1, lat2 - lat1
    a = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(math.sqrt(min(1.0, a)))


def robust_stats(values: Iterable[float], sigma_floor: float = 1.5) -> RobustStats:
    clean = [float(value) for value in values if math.isfinite(float(value))]
    if not clean:
        return RobustStats(None, None, None)
    center = float(median(clean))
    mad = float(median([abs(value - center) for value in clean]))
    sigma = max(1.4826 * mad, sigma_floor)
    return RobustStats(center, mad, sigma)


def compute_anomaly_score(
    temporal_z: float | None,
    spatial_z: float | None,
    temporal_excess: float | None,
    spatial_excess: float | None,
    config: AnomalyConfig = AnomalyConfig(),
    spatial_support_fraction: float = 1.0,
) -> float | None:
    """Return a bounded evidence strength score, never a probability.

    The deterministic v1 score weights positive temporal/spatial z evidence
    at 40% each and the larger absolute excess at 20%.
    """

    values = (temporal_z, spatial_z, temporal_excess, spatial_excess)
    if any(value is None or not math.isfinite(float(value)) for value in values):
        return None
    z_temporal = min(max(float(temporal_z), 0.0), 10.0)
    # Raw spatial_z remains in the report. This support factor prevents one
    # sensor that jumps above a flat neighborhood from looking like a fully
    # supported local plume signal.
    support = min(max(float(spatial_support_fraction), 0.25), 1.0)
    z_spatial = min(max(float(spatial_z), 0.0), 10.0) * support
    excess = min(
        max(abs(float(temporal_excess)), abs(float(spatial_excess)))
        / config.temporal_excess_threshold,
        10.0,
    )
    return round(min(10.0, 0.4 * z_temporal + 0.4 * z_spatial + 0.2 * excess), 6)


def candidate_from_metrics(
    temporal_status: str,
    spatial_status: str,
    temporal_excess: float | None,
    spatial_excess: float | None,
    temporal_z: float | None,
    spatial_z: float | None,
    usable_current: bool = True,
    config: AnomalyConfig = AnomalyConfig(),
    spatial_support_count: int | None = None,
) -> bool:
    if not usable_current or temporal_status != "sufficient" or spatial_status != "sufficient":
        return False
    if spatial_support_count is not None and spatial_support_count < 1:
        return False
    values = (temporal_excess, spatial_excess, temporal_z, spatial_z)
    if any(value is None or not math.isfinite(float(value)) for value in values):
        return False
    return (
        float(temporal_excess) >= config.temporal_excess_threshold
        and float(spatial_excess) >= config.spatial_excess_threshold
        and float(temporal_z) >= config.temporal_z_threshold
        and float(spatial_z) >= config.spatial_z_threshold
    )


def synthetic_metrics(
    current: float,
    history: Iterable[float],
    neighbors: Iterable[float],
    config: AnomalyConfig = AnomalyConfig(),
    neighbor_histories: Iterable[Iterable[float]] | None = None,
) -> dict[str, Any]:
    """Small pure helper used by deterministic tests and future replay tools."""

    history_values = list(history)
    temporal = robust_stats(history_values, config.robust_sigma_floor)
    spatial_values = list(neighbors)
    spatial = robust_stats(spatial_values, config.robust_sigma_floor)
    temporal_excess = current - temporal.median if temporal.median is not None else None
    spatial_excess = current - spatial.median if spatial.median is not None else None
    temporal_z = temporal_excess / temporal.sigma if temporal_excess is not None and temporal.sigma else None
    spatial_z = spatial_excess / spatial.sigma if spatial_excess is not None and spatial.sigma else None
    temporal_status = "sufficient" if len(history_values) >= config.min_temporal_samples else "insufficient_history"
    spatial_status = "sufficient" if len(spatial_values) >= config.min_neighbors else "insufficient_neighbors"
    support_count: int | None = None
    support_fraction = 1.0
    if neighbor_histories is not None:
        support_count = 0
        for neighbor_value, neighbor_history in zip(spatial_values, neighbor_histories):
            neighbor_stats = robust_stats(neighbor_history, config.robust_sigma_floor)
            if neighbor_stats.median is None or neighbor_stats.sigma is None:
                continue
            neighbor_excess = neighbor_value - neighbor_stats.median
            if neighbor_excess >= config.temporal_excess_threshold and neighbor_excess / neighbor_stats.sigma >= config.temporal_z_threshold:
                support_count += 1
        support_fraction = support_count / len(spatial_values) if spatial_values else 0.0
    return {
        "temporal_median": temporal.median,
        "temporal_mad": temporal.mad,
        "temporal_excess": temporal_excess,
        "temporal_z": temporal_z,
        "temporal_sample_count": len(history_values),
        "temporal_status": temporal_status,
        "spatial_median": spatial.median,
        "spatial_mad": spatial.mad,
        "spatial_excess": spatial_excess,
        "spatial_z": spatial_z,
        "neighbor_count": len(spatial_values),
        "spatial_status": spatial_status,
        "spatial_support_count": support_count,
        "spatial_support_fraction": support_fraction,
        "anomaly_score": compute_anomaly_score(temporal_z, spatial_z, temporal_excess, spatial_excess, config, support_fraction),
        "is_candidate": candidate_from_metrics(
            temporal_status, spatial_status, temporal_excess, spatial_excess,
            temporal_z, spatial_z, config=config, spatial_support_count=support_count,
        ),
    }


def load_pilot_config(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    for name in ("context_bbox", "core_bbox"):
        bbox = payload[name]
        if not {"north", "south", "west", "east"}.issubset(bbox):
            raise ValueError(f"{name} must contain north, south, west, east")
        if not (float(bbox["south"]) < float(bbox["north"]) and float(bbox["west"]) < float(bbox["east"])):
            raise ValueError(f"{name} has invalid bounds")
        payload[name] = {key: float(bbox[key]) for key in ("north", "south", "west", "east")}
    return payload


def bbox_contains(lat: float | None, lon: float | None, bbox: dict[str, float]) -> bool:
    return lat is not None and lon is not None and bbox["south"] <= lat <= bbox["north"] and bbox["west"] <= lon <= bbox["east"]


def _flag_tokens(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    return tuple(token for token in str(value).split(";") if token)


def _valid_pm25(value: Any, flags: tuple[str, ...]) -> bool:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return False
    return math.isfinite(parsed) and parsed >= 0 and "invalid_pm25" not in flags


def _smooth_series(records: list[dict[str, Any]], config: AnomalyConfig) -> list[BinnedObservation]:
    buckets: dict[tuple[str, datetime], dict[str, Any]] = {}
    for record in records:
        observed_at = utc_datetime(record["phenomenon_time_utc"])
        flags = _flag_tokens(record.get("quality_flags"))
        if not _valid_pm25(record.get("pm25_ugm3"), flags):
            continue
        if any("future_timestamp" in flag or "invalid_timestamp" in flag for flag in flags):
            continue
        bucket = floor_time(observed_at, config.bin_minutes)
        key = (str(record["station_id"]), bucket)
        if key not in buckets or observed_at > buckets[key]["phenomenon_time_utc"]:
            buckets[key] = {**record, "phenomenon_time_utc": observed_at, "_flags": flags, "_bucket": bucket}

    by_station: dict[str, list[dict[str, Any]]] = {}
    for record in buckets.values():
        by_station.setdefault(str(record["station_id"]), []).append(record)

    output: list[BinnedObservation] = []
    step = timedelta(minutes=config.bin_minutes)
    for station_id, station_records in by_station.items():
        station_records.sort(key=lambda item: item["_bucket"])
        for index, record in enumerate(station_records):
            recent: list[float] = []
            for prior in station_records[max(0, index - config.smoothing_points + 1): index + 1]:
                if record["_bucket"] - prior["_bucket"] <= step:
                    recent.append(float(prior["pm25_ugm3"]))
            smoothed = float(median(recent)) if config.smoothing_points > 1 else float(record["pm25_ugm3"])
            output.append(BinnedObservation(
                station_id=station_id,
                bucket=record["_bucket"],
                observed_at=record["phenomenon_time_utc"],
                raw_pm25=float(record["pm25_ugm3"]),
                smoothed_pm25=smoothed,
                quality_flags=record["_flags"],
            ))
    return output


def _read_snapshot(
    database_path: Path,
    query_start: datetime,
    query_end: datetime,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], tuple[datetime | None, datetime | None]]:
    """Read one consistent snapshot without creating or modifying a database."""

    try:
        connection = duckdb.connect(str(database_path), read_only=True)
    except Exception as exc:  # DuckDB's lock errors vary by version/platform.
        raise RuntimeError(
            f"READ_ONLY_ACCESS_FAILED: could not open {database_path} while recorder may be writing; "
            "recorder was not stopped or modified. Details: " + str(exc)
        ) from exc
    try:
        stations = [
            dict(zip(("thing_id", "station_id", "station_name", "lat", "lon", "city", "township", "area_type"), row))
            for row in connection.execute(
                """
                SELECT thing_id, station_id, station_name, lat, lon, city, township, area_type
                FROM sensor_station
                """
            ).fetchall()
        ]
        observations = [
            dict(zip(("station_id", "datastream_id", "phenomenon_time_utc", "pm25_ugm3", "source_status", "quality_flags"), row))
            for row in connection.execute(
                """
                SELECT station_id, datastream_id, phenomenon_time_utc, pm25_ugm3, source_status, quality_flags
                FROM pm25_observation
                WHERE phenomenon_time_utc >= ? AND phenomenon_time_utc <= ?
                ORDER BY station_id, phenomenon_time_utc
                """,
                [utc_datetime(query_start), utc_datetime(query_end)],
            ).fetchall()
        ]
        span = connection.execute(
            "SELECT min(phenomenon_time_utc), max(phenomenon_time_utc) FROM pm25_observation"
        ).fetchone()
        return stations, observations, (span[0], span[1])
    finally:
        connection.close()


def _current_for_station(records: list[BinnedObservation], cutoff: datetime) -> BinnedObservation | None:
    eligible = [record for record in records if record.observed_at <= cutoff]
    return max(eligible, key=lambda record: record.observed_at) if eligible else None


def _quality_flags(current: BinnedObservation | None, cutoff: datetime, now: datetime, config: AnomalyConfig) -> list[str]:
    flags = list(current.quality_flags) if current else ["missing_current_value"]
    if current:
        age_minutes = max(0.0, (cutoff - current.observed_at).total_seconds() / 60)
        if age_minutes > config.max_current_age_minutes:
            flags.append("stale_current_observation")
        if current.observed_at > now + timedelta(minutes=5):
            flags.append("suspicious_future_timestamp")
    return list(dict.fromkeys(flags))


def _row_number(value: float | None) -> float | None:
    return None if value is None else round(float(value), 6)


def _analyse_station(
    station: dict[str, Any],
    series: dict[str, list[BinnedObservation]],
    all_stations: dict[str, dict[str, Any]],
    cutoff: datetime,
    now: datetime,
    config: AnomalyConfig,
) -> tuple[dict[str, Any], dict[str, Any]]:
    station_id = str(station["station_id"])
    records = sorted(series.get(station_id, []), key=lambda item: item.observed_at)
    current = _current_for_station(records, cutoff)
    flags = _quality_flags(current, cutoff, now, config)
    current_usable = current is not None and "stale_current_observation" not in flags and "suspicious_future_timestamp" not in flags
    current_bucket = current.bucket if current else floor_time(cutoff, config.bin_minutes)
    history_start = current_bucket - timedelta(minutes=config.baseline_minutes)
    history = [
        item.smoothed_pm25
        for item in records
        if history_start <= item.bucket < current_bucket
    ]
    temporal = robust_stats(history, config.robust_sigma_floor)
    temporal_status = "sufficient" if len(history) >= config.min_temporal_samples else "insufficient_history"
    current_value = current.smoothed_pm25 if current else None
    temporal_excess = current_value - temporal.median if current_value is not None and temporal.median is not None else None
    temporal_z = temporal_excess / temporal.sigma if temporal_excess is not None and temporal.sigma else None

    lat, lon = station.get("lat"), station.get("lon")
    neighbors: list[tuple[float, str, BinnedObservation]] = []
    radius = config.neighbor_radius_km
    if lat is None or lon is None:
        spatial_status = "invalid_coordinates"
    else:
        for neighbor_id, neighbor_station in all_stations.items():
            if neighbor_id == station_id:
                continue
            nlat, nlon = neighbor_station.get("lat"), neighbor_station.get("lon")
            if nlat is None or nlon is None:
                continue
            distance = haversine_km((float(lon), float(lat)), (float(nlon), float(nlat)))
            neighbor_current = _current_for_station(series.get(neighbor_id, []), cutoff)
            if neighbor_current is None:
                continue
            neighbor_flags = _quality_flags(neighbor_current, cutoff, now, config)
            if "stale_current_observation" in neighbor_flags or "suspicious_future_timestamp" in neighbor_flags:
                continue
            if distance <= config.neighbor_radius_km:
                neighbors.append((distance, neighbor_id, neighbor_current))
        if len(neighbors) < config.min_neighbors and config.max_neighbor_radius_km > config.neighbor_radius_km:
            radius = config.max_neighbor_radius_km
            expanded: list[tuple[float, str, BinnedObservation]] = []
            for neighbor_id, neighbor_station in all_stations.items():
                if neighbor_id == station_id:
                    continue
                nlat, nlon = neighbor_station.get("lat"), neighbor_station.get("lon")
                if nlat is None or nlon is None:
                    continue
                distance = haversine_km((float(lon), float(lat)), (float(nlon), float(nlat)))
                neighbor_current = _current_for_station(series.get(neighbor_id, []), cutoff)
                if neighbor_current is None:
                    continue
                neighbor_flags = _quality_flags(neighbor_current, cutoff, now, config)
                if "stale_current_observation" not in neighbor_flags and "suspicious_future_timestamp" not in neighbor_flags and distance <= radius:
                    expanded.append((distance, neighbor_id, neighbor_current))
            neighbors = expanded
        neighbors.sort(key=lambda item: item[0])
        spatial_status = "sufficient" if len(neighbors) >= config.min_neighbors else "insufficient_neighbors"

    spatial_values = [item[2].smoothed_pm25 for item in neighbors]
    spatial = robust_stats(spatial_values, config.robust_sigma_floor)
    spatial_excess = current_value - spatial.median if current_value is not None and spatial.median is not None else None
    spatial_z = spatial_excess / spatial.sigma if spatial_excess is not None and spatial.sigma else None
    spatial_support_count = 0
    for _, neighbor_id, neighbor_current in neighbors:
        neighbor_history_start = neighbor_current.bucket - timedelta(minutes=config.baseline_minutes)
        neighbor_history = [
            item.smoothed_pm25 for item in series.get(neighbor_id, [])
            if neighbor_history_start <= item.bucket < neighbor_current.bucket
        ]
        neighbor_stats = robust_stats(neighbor_history, config.robust_sigma_floor)
        neighbor_excess = neighbor_current.smoothed_pm25 - neighbor_stats.median if neighbor_stats.median is not None else None
        neighbor_z = neighbor_excess / neighbor_stats.sigma if neighbor_excess is not None and neighbor_stats.sigma else None
        if (
            neighbor_excess is not None and neighbor_z is not None
            and len(neighbor_history) >= config.min_temporal_samples
            and neighbor_excess >= config.temporal_excess_threshold
            and neighbor_z >= config.temporal_z_threshold
        ):
            spatial_support_count += 1
    support_fraction = spatial_support_count / len(neighbors) if neighbors else 0.0
    score = compute_anomaly_score(temporal_z, spatial_z, temporal_excess, spatial_excess, config, support_fraction)
    is_candidate = candidate_from_metrics(
        temporal_status, spatial_status, temporal_excess, spatial_excess,
        temporal_z, spatial_z, current_usable, config, spatial_support_count,
    )
    isolated = bool(
        current_usable and temporal_excess is not None and temporal_z is not None
        and temporal_excess >= config.temporal_excess_threshold and temporal_z >= config.temporal_z_threshold
        and not is_candidate
        and (spatial_status != "sufficient" or spatial_excess is None or spatial_z is None
             or spatial_excess < config.spatial_excess_threshold or spatial_z < config.spatial_z_threshold)
    )
    if isolated:
        flags.append("isolated_sensor_spike_suspect")
    if current and current.quality_flags:
        flags.extend(current.quality_flags)
    flags = list(dict.fromkeys(flags))
    age_minutes = None if current is None else max(0.0, (cutoff - current.observed_at).total_seconds() / 60)
    row = {
        "analysis_time_utc": iso_utc(cutoff),
        "station_id": station_id,
        "station_name": station.get("station_name"),
        "lat": station.get("lat"),
        "lon": station.get("lon"),
        "raw_pm25": _row_number(current.raw_pm25 if current else None),
        "smoothed_pm25": _row_number(current_value),
        "observation_time_utc": iso_utc(current.observed_at if current else None),
        "current_age_minutes": _row_number(age_minutes),
        "temporal_median": _row_number(temporal.median),
        "temporal_mad": _row_number(temporal.mad),
        "temporal_excess": _row_number(temporal_excess),
        "temporal_z": _row_number(temporal_z),
        "temporal_sample_count": len(history),
        "temporal_status": temporal_status,
        "spatial_median": _row_number(spatial.median),
        "spatial_mad": _row_number(spatial.mad),
        "spatial_excess": _row_number(spatial_excess),
        "spatial_z": _row_number(spatial_z),
        "spatial_support_count": spatial_support_count,
        "spatial_support_fraction": _row_number(support_fraction),
        "neighbor_count": len(neighbors),
        "spatial_radius_km": radius if lat is not None and lon is not None else None,
        "spatial_status": spatial_status,
        "anomaly_score": score,
        "is_candidate": is_candidate,
        "isolated_suspicious": isolated,
        "quality_flags": ";".join(flags),
    }
    context_row = {
        "station_id": station_id,
        "station_name": station.get("station_name"),
        "lat": station.get("lat"),
        "lon": station.get("lon"),
        "pm25": _row_number(current_value),
        "observation_time_utc": iso_utc(current.observed_at if current else None),
        "age_minutes": _row_number(age_minutes),
        "status": "usable" if current_usable else ("missing" if current is None else "stale"),
    }
    return row, context_row


def detect_anomalies(
    database_path: Path,
    config_path: Path,
    cutoff: datetime | None = None,
    latest: bool = False,
    lookback_hours: float = 24.0,
    config: AnomalyConfig = AnomalyConfig(),
    now: datetime | None = None,
    analysis_zone: str = "core",
) -> DetectionResult:
    analysis_zone = validate_analysis_zone(analysis_zone)
    if lookback_hours <= 0:
        raise ValueError("lookback_hours must be positive")
    pilot = load_pilot_config(config_path)
    now = utc_datetime(now or datetime.now(UTC))
    if cutoff is None and not latest:
        latest = True
    if cutoff is None:
        # A small read-only metadata snapshot establishes the latest target.
        stations, initial_observations, span = _read_snapshot(database_path, datetime(2000, 1, 1, tzinfo=UTC), now + timedelta(days=3650))
        if span[1] is None:
            raise ValueError("no PM2.5 observations available")
        cutoff = utc_datetime(span[1])
    else:
        cutoff = utc_datetime(cutoff)
        stations, initial_observations, span = _read_snapshot(
            database_path,
            cutoff - timedelta(hours=lookback_hours),
            cutoff,
        )
    if latest:
        # Re-read the bounded analysis interval at the resolved latest time so
        # the exact report is based on one target and never future-filled.
        stations, observations, span = _read_snapshot(
            database_path,
            cutoff - timedelta(hours=lookback_hours),
            cutoff,
        )
    else:
        observations = initial_observations
    if span[0] is None or span[1] is None:
        raise ValueError("no PM2.5 observations available")

    station_map = {str(station["station_id"]): station for station in stations}
    series_records = _smooth_series(observations, config)
    series: dict[str, list[BinnedObservation]] = {}
    for record in series_records:
        series.setdefault(record.station_id, []).append(record)
    context_bbox = pilot["context_bbox"]
    core_bbox = pilot["core_bbox"]
    context_stations = [station for station in stations if bbox_contains(station.get("lat"), station.get("lon"), context_bbox)]
    core_stations = [station for station in context_stations if bbox_contains(station.get("lat"), station.get("lon"), core_bbox)]
    all_context = {str(station["station_id"]): station for station in context_stations}
    analysis_stations = core_stations if analysis_zone == "core" else context_stations
    rows: list[dict[str, Any]] = []
    context_rows: list[dict[str, Any]] = []
    for station in sorted(context_stations, key=lambda item: str(item["station_id"])):
        row, context_row = _analyse_station(station, series, all_context, cutoff, now, config)
        context_rows.append(context_row)
        if station in analysis_stations:
            rows.append(row)
    rows.sort(key=lambda row: (-float(row["anomaly_score"] or -1), str(row["station_id"])))
    usable_rows = [row for row in rows if row["raw_pm25"] is not None and "stale_current_observation" not in row["quality_flags"] and "suspicious_future_timestamp" not in row["quality_flags"]]
    pm_values = [float(row["raw_pm25"]) for row in usable_rows]
    candidates = [row for row in rows if row["is_candidate"]]
    isolated = [row["station_id"] for row in rows if row["isolated_suspicious"]]
    span_minutes = (utc_datetime(span[1]) - utc_datetime(span[0])).total_seconds() / 60
    available_minutes = max(0.0, (cutoff - utc_datetime(span[0])).total_seconds() / 60)
    summary = {
        "core_sensors": len(core_stations),
        "context_sensor_count": len(context_stations),
        "analysis_zone": analysis_zone,
        "analysis_sensor_count": len(rows),
        "usable_sensors": len(usable_rows),
        "insufficient_history": sum(row["temporal_status"] != "sufficient" for row in rows),
        "insufficient_neighbors": sum(row["spatial_status"] != "sufficient" for row in rows),
        "candidates": len(candidates),
        "isolated_suspicious_sensors": isolated,
        "pm25_median": _row_number(median(pm_values)) if pm_values else None,
        "pm25_min": _row_number(min(pm_values)) if pm_values else None,
        "pm25_max": _row_number(max(pm_values)) if pm_values else None,
        "history_available_minutes_at_analysis": _row_number(available_minutes),
        "insufficient_history_overall": available_minutes < config.baseline_minutes,
    }
    payload = {
        "schema_version": 1,
        "detector": "AirTrace PM2.5 Anomaly Detector",
        "generated_at_utc": iso_utc(now),
        "analysis_time_utc": iso_utc(cutoff),
        "analysis_bin_start_utc": iso_utc(floor_time(cutoff, config.bin_minutes)),
        "analysis_zone": analysis_zone,
        "scope": "sensor-level anomaly candidates and diagnostics only; not a pollution-source detector",
        "score_note": "anomaly_score is a deterministic 0-10 evidence/anomaly-strength score, not a statistical probability",
        "config": asdict(config) | {"heuristic_label": "v1 heuristic; thresholds require calibration on real historical data"},
        "zones": {"context_bbox": context_bbox, "core_bbox": core_bbox, "geometry": "WGS84 latitude/longitude; Core boundary does not truncate spatial neighbors"},
        "database": {"path": str(database_path), "observation_start_utc": iso_utc(span[0]), "observation_end_utc": iso_utc(span[1]), "observation_span_minutes": _row_number(span_minutes)},
        "summary": summary,
        "top_anomaly_scores": rows[:10],
        "top_anomaly_candidates": candidates[:15],
        "sensors": rows,
        "context_sensors": context_rows,
    }
    if analysis_zone == "context":
        payload["mode_notice"] = [CONTEXT_DIAGNOSTIC_REPLAY, NOT_PRODUCTION_EVENT_DETECTION]
    return DetectionResult(payload=payload, rows=rows, context_sensors=context_rows)


def latest_observation_time(database_path: Path) -> datetime:
    """Return the latest PM2.5 timestamp using a read-only connection."""

    try:
        connection = duckdb.connect(str(database_path), read_only=True)
    except Exception as exc:
        raise RuntimeError(f"READ_ONLY_ACCESS_FAILED: could not open {database_path}: {exc}") from exc
    try:
        value = connection.execute("SELECT max(phenomenon_time_utc) FROM pm25_observation").fetchone()[0]
    finally:
        connection.close()
    if value is None:
        raise ValueError("no PM2.5 observations available")
    return utc_datetime(value)


def detect_anomalies_range(
    database_path: Path,
    config_path: Path,
    start_time: datetime,
    end_time: datetime,
    lookback_hours: float = 2.0,
    config: AnomalyConfig = AnomalyConfig(),
    now: datetime | None = None,
    analysis_zone: str = "core",
) -> list[DetectionResult]:
    """Evaluate the unchanged v1 detector once per analysis bin in a range.

    Each result is still the regular sensor-level v1 report.  The range helper
    only supplies deterministic bin cutoffs and never changes candidate math.
    The one-bin-minus-a-microsecond cutoff includes all observations in a bin
    without future-filling it from the next bin.
    """

    analysis_zone = validate_analysis_zone(analysis_zone)
    start_time = utc_datetime(start_time)
    end_time = utc_datetime(end_time)
    if end_time < start_time:
        raise ValueError("end_time must be at or after start_time")
    if lookback_hours <= 0:
        raise ValueError("lookback_hours must be positive")
    first_bin = floor_time(start_time, config.bin_minutes)
    last_bin = floor_time(end_time, config.bin_minutes)
    step = timedelta(minutes=config.bin_minutes)
    stable_now = utc_datetime(now or datetime.now(UTC))
    results: list[DetectionResult] = []
    current_bin = first_bin
    while current_bin <= last_bin:
        cutoff = current_bin + step - timedelta(microseconds=1)
        results.append(detect_anomalies(
            database_path=database_path,
            config_path=config_path,
            cutoff=cutoff,
            latest=False,
            lookback_hours=lookback_hours,
            config=config,
            now=stable_now,
            analysis_zone=analysis_zone,
        ))
        current_bin += step
    return results


def write_json(result: DetectionResult, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result.payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def write_csv(result: DetectionResult, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = list(REQUIRED_REPORT_FIELDS) + [
        "station_name", "observation_time_utc", "current_age_minutes", "isolated_suspicious",
    ]
    is_context = result.payload.get("analysis_zone", "core") == "context"
    if is_context:
        fields = ["analysis_zone", "mode_notice"] + fields
    with path.open("w", encoding="utf-8", newline="") as handle:
        if is_context:
            handle.write(f"# {CONTEXT_DIAGNOSTIC_REPLAY}; {NOT_PRODUCTION_EVENT_DETECTION}\n")
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        if is_context:
            marker = f"{CONTEXT_DIAGNOSTIC_REPLAY} / {NOT_PRODUCTION_EVENT_DETECTION}"
            writer.writerows([{**row, "analysis_zone": "context", "mode_notice": marker} for row in result.rows])
        else:
            writer.writerows(result.rows)


def _html_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")


def write_map(result: DetectionResult, path: Path) -> None:
    payload = result.payload
    context = payload["zones"]["context_bbox"]
    core = payload["zones"]["core_bbox"]
    diagnostic_layer_label = "Context diagnostics" if payload.get("analysis_zone", "core") == "context" else "Core diagnostics"
    center = [(context["south"] + context["north"]) / 2, (context["west"] + context["east"]) / 2]
    html = f"""<!doctype html>
<html lang="zh-Hant"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>AirTrace PM2.5 Anomaly Diagnostics</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">
<style>body{{margin:0;font-family:system-ui,-apple-system,"Segoe UI",sans-serif;color:#172033}}header{{padding:14px 18px;border-bottom:1px solid #d9e0ea;background:#fff}}h1{{margin:0 0 5px;font-size:21px}}.note{{color:#586579;font-size:13px}}#map{{height:calc(100vh - 105px);min-height:560px}}.popup-table td{{padding:2px 6px 2px 0;vertical-align:top}}.popup-table td:first-child{{color:#586579;white-space:nowrap}}</style></head>
<body><header><h1>AirTrace PM2.5 Anomaly Diagnostics</h1><div class="note">Analysis: {payload["analysis_time_utc"]} · zone: {payload.get("analysis_zone", "core")} · anomaly_score 是 0–10 evidence strength，不是 probability；此圖不是正式 frontend，也不代表污染源。</div>{('<div class="note"><strong>' + CONTEXT_DIAGNOSTIC_REPLAY + '</strong><br><strong>' + NOT_PRODUCTION_EVENT_DETECTION + '</strong></div>' if payload.get("analysis_zone", "core") == "context" else '')}</header><div id="map"></div>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script><script>
const contextBbox={_html_json(context)}, coreBbox={_html_json(core)}, sensors={_html_json(result.context_sensors)}, coreRows={_html_json(result.rows)};
const map=L.map('map',{{preferCanvas:true}}).setView({_html_json(center)},12); const osm=L.tileLayer('https://{{s}}.tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png',{{maxZoom:19,attribution:'&copy; OpenStreetMap contributors'}}).addTo(map);
L.rectangle([[contextBbox.south,contextBbox.west],[contextBbox.north,contextBbox.east]],{{color:'#26364f',weight:2,fill:false,dashArray:'7 5'}}).bindPopup('<strong>Context Zone</strong>').addTo(map);
L.rectangle([[coreBbox.south,coreBbox.west],[coreBbox.north,coreBbox.east]],{{color:'#8e2a86',weight:3,fillColor:'#c77dff',fillOpacity:.08,dashArray:'8 4'}}).bindPopup('<strong>Core Zone</strong>').addTo(map);
function safe(v){{return String(v??'—').replace(/[&<>"']/g,c=>({{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}}[c]))}} function n(v){{return v===null||v===undefined?'—':Number(v).toFixed(2)}}
const coreById=Object.fromEntries(coreRows.map(r=>[r.station_id,r])); const bg=L.layerGroup().addTo(map); const fg=L.layerGroup().addTo(map);
sensors.forEach(s=>{{if(s.lat===null||s.lon===null)return; const r=coreById[s.station_id]; const score=r?.anomaly_score??0; const isCandidate=Boolean(r?.is_candidate); const color=isCandidate?'#d92d20':(r?'#f79009':'#94a3b8'); const popup=r?'<strong>'+safe(r.station_id)+'</strong><table class="popup-table"><tr><td>PM2.5</td><td>'+n(r.raw_pm25)+' µg/m³</td></tr><tr><td>temporal</td><td>'+n(r.temporal_median)+' / excess '+n(r.temporal_excess)+' / z '+n(r.temporal_z)+'</td></tr><tr><td>spatial</td><td>'+n(r.spatial_median)+' / excess '+n(r.spatial_excess)+' / z '+n(r.spatial_z)+'</td></tr><tr><td>score</td><td>'+n(r.anomaly_score)+'</td></tr><tr><td>neighbors</td><td>'+r.neighbor_count+' @ '+n(r.spatial_radius_km)+' km</td></tr><tr><td>quality</td><td>'+safe(r.quality_flags)+'</td></tr></table>':'<strong>'+safe(s.station_id)+'</strong><br>Context sensor background<br>PM2.5: '+n(s.pm25)+' µg/m³'; const marker=L.circleMarker([s.lat,s.lon],{{radius:r?Math.max(4,4+score*1.1):3,weight:isCandidate?3:1,opacity:.9,fillOpacity:r?.6:.3,color,fillColor:color}}).bindPopup(popup); marker.addTo(r?fg:bg)}});
L.control.layers({{'OpenStreetMap':osm}},{{'Context sensors':bg,'{diagnostic_layer_label}':fg}}).addTo(map); map.fitBounds([[contextBbox.south,contextBbox.west],[contextBbox.north,contextBbox.east]],{{padding:[14,14]}});
</script></body></html>
"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(html, encoding="utf-8")


def print_terminal_report(result: DetectionResult, json_path: Path, csv_path: Path, html_path: Path) -> None:
    payload = result.payload
    summary = payload["summary"]
    database = payload["database"]
    if summary["insufficient_history_overall"] or summary["insufficient_history"]:
        print("INSUFFICIENT_HISTORY")
    if payload.get("analysis_zone", "core") == "context":
        print(CONTEXT_DIAGNOSTIC_REPLAY)
        print(NOT_PRODUCTION_EVENT_DETECTION)
    print("AirTrace PM2.5 Anomaly Detector")
    print(f"Analysis time: {payload['analysis_time_utc']} (3-minute bin {payload['analysis_bin_start_utc']})")
    sensor_label = "Context sensors" if payload.get("analysis_zone", "core") == "context" else "Core sensors"
    print(f"{sensor_label}: {summary['analysis_sensor_count']}")
    print(f"Usable sensors: {summary['usable_sensors']}")
    print(f"Insufficient history: {summary['insufficient_history']}")
    print(f"Insufficient neighbors: {summary['insufficient_neighbors']}")
    print(f"Candidates: {summary['candidates']}")
    print(f"PM2.5: median={summary['pm25_median']} min={summary['pm25_min']} max={summary['pm25_max']} µg/m³")
    print(f"DB accumulation: {database['observation_start_utc']} → {database['observation_end_utc']} ({database['observation_span_minutes']} minutes)")
    isolated = summary["isolated_suspicious_sensors"]
    print(f"Isolated suspicious sensor: {'yes (' + ', '.join(isolated[:10]) + ')' if isolated else 'no'}")
    print("Top anomaly candidates:")
    print("station_id | PM2.5 | temporal excess / z | spatial excess / z | score | neighbor count")
    candidates = payload["top_anomaly_candidates"]
    if not candidates:
        print("none")
    for row in candidates:
        print(
            f"{row['station_id']} | {row['raw_pm25']} | {row['temporal_excess']} / {row['temporal_z']} | "
            f"{row['spatial_excess']} / {row['spatial_z']} | {row['anomaly_score']} | {row['neighbor_count']}"
        )
    print(f"JSON: {json_path}")
    print(f"CSV: {csv_path}")
    print(f"HTML: {html_path}")
