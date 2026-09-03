#!/usr/bin/env python3
"""Post-hoc known-fire sensor spike forensics.

This diagnostic deliberately lives outside the production detector.  It reads
the recorder databases read-only, reuses the existing anomaly and wind paths,
and writes only ``reports/validation/sensor_forensics`` artifacts.
"""

from __future__ import annotations

import argparse
import csv
import gzip
import html
import json
import math
import os
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
from dotenv import load_dotenv  # noqa: E402

from airtrace.analysis.anomaly import (  # noqa: E402
    AnomalyConfig,
    BinnedObservation,
    _smooth_series,
    bbox_contains,
    floor_time,
    haversine_km,
    iso_utc,
    load_pilot_config,
    robust_stats,
)
from airtrace.analysis.wind import (  # noqa: E402
    estimate_to_dict,
    get_wind,
    load_wind_snapshot_range,
)
from airtrace.data.moenv import MoenvClient, MoenvError, parse_timestamp_utc  # noqa: E402
from scripts.analyze_known_fire_response import (  # noqa: E402
    bearing_deg,
    classify_wind_relation,
    nominal_travel_time_minutes,
    parse_event_time,
    replay_anomaly_from_snapshot,
)

UTC = timezone.utc
TAIPEI = ZoneInfo("Asia/Taipei")
DEFAULT_DATABASE = ROOT / "data" / "airtrace.duckdb"
DEFAULT_WEATHER_DATABASE = ROOT / "data" / "weather.duckdb"
DEFAULT_REFERENCE_DATABASE = ROOT / "data" / "reference_air.duckdb"
DEFAULT_CONFIG = ROOT / "config" / "pilot_region.json"
DEFAULT_EVENTS = ROOT / "config" / "validation" / "known_fires_20260903.json"
DEFAULT_OUTPUT = ROOT / "reports" / "validation" / "sensor_forensics"
load_dotenv(ROOT / ".env")

LAGS_MINUTES = (-12, -9, -6, -3, 0, 3, 6, 9, 12)
BANDS = (
    ("within_100m", 0.0, 0.100, False),
    ("100_250m", 0.100, 0.250, False),
    ("250_500m", 0.250, 0.500, False),
    ("500_1000m", 0.500, 1.000, True),
)


def finite(value: Any) -> bool:
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


def num(value: Any, digits: int = 6) -> float | None:
    return round(float(value), digits) if finite(value) else None


def median(values: Iterable[Any]) -> float | None:
    clean = [float(value) for value in values if finite(value)]
    return num(statistics.median(clean)) if clean else None


def iqr(values: Iterable[Any]) -> float | None:
    clean = sorted(float(value) for value in values if finite(value))
    if not clean:
        return None
    if len(clean) == 1:
        return 0.0
    return num(statistics.quantiles(clean, n=4, method="inclusive")[2] - statistics.quantiles(clean, n=4, method="inclusive")[0])


def parse_events(path: Path) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    tz_name = str(payload.get("timezone", "Asia/Taipei"))
    events = []
    for source in payload.get("events", []):
        item = dict(source)
        item["start_utc"] = parse_event_time(source["fire_start_local"], tz_name)
        item["end_utc"] = parse_event_time(source["fire_end_local"], tz_name)
        if item["end_utc"] < item["start_utc"]:
            raise ValueError(f"{item.get('event_id')}: fire end precedes start")
        events.append(item)
    if not events:
        raise ValueError("known-fire config contains no events")
    return events


def read_pm_snapshot(database: Path, end: datetime) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    connection = duckdb.connect(str(database), read_only=True)
    try:
        station_fields = ("thing_id", "station_id", "station_name", "lat", "lon", "city", "township", "area_type", "is_outdoor", "is_mobile")
        stations = [dict(zip(station_fields, row)) for row in connection.execute(
            "SELECT thing_id, station_id, station_name, lat, lon, city, township, area_type, is_outdoor, is_mobile FROM sensor_station ORDER BY station_id"
        ).fetchall()]
        observation_fields = ("station_id", "datastream_id", "phenomenon_time_utc", "pm25_ugm3", "ingested_at_utc", "source_status", "quality_flags")
        observations = [dict(zip(observation_fields, row)) for row in connection.execute(
            """SELECT station_id, datastream_id, phenomenon_time_utc, pm25_ugm3,
                      ingested_at_utc, source_status, quality_flags
               FROM pm25_observation WHERE phenomenon_time_utc <= ?
               ORDER BY station_id, phenomenon_time_utc""", [end]).fetchall()]
        return stations, observations
    finally:
        connection.close()


def raw_bins(observations: list[dict[str, Any]], config: AnomalyConfig) -> dict[str, dict[datetime, dict[str, Any]]]:
    output: dict[str, dict[datetime, dict[str, Any]]] = {}
    for record in observations:
        if not finite(record.get("pm25_ugm3")) or float(record["pm25_ugm3"]) < 0:
            continue
        flags = str(record.get("quality_flags") or "")
        if "invalid_pm25" in flags or "future_timestamp" in flags or "invalid_timestamp" in flags:
            continue
        observed = record["phenomenon_time_utc"].astimezone(UTC)
        bucket = floor_time(observed, config.bin_minutes)
        station = str(record["station_id"])
        existing = output.setdefault(station, {}).get(bucket)
        if existing is None or observed > existing["phenomenon_time_utc"]:
            output[station][bucket] = {**record, "station_id": station, "phenomenon_time_utc": observed, "bucket": bucket}
    return output


def timeline(raw: dict[datetime, dict[str, Any]], start: datetime, end: datetime, step_minutes: int = 3) -> list[dict[str, Any]]:
    current = floor_time(start, step_minutes)
    output = []
    while current < end:
        item = raw.get(current)
        output.append({
            "bucket_utc": iso_utc(current),
            "phenomenon_time_utc": iso_utc(item["phenomenon_time_utc"]) if item else None,
            "ingested_at_utc": iso_utc(item["ingested_at_utc"]) if item else None,
            "raw_pm25": num(item.get("pm25_ugm3")) if item else None,
            "source_status": item.get("source_status") if item else None,
            "quality_flags": item.get("quality_flags") if item else None,
        })
        current += timedelta(minutes=step_minutes)
    return output


def values_between(raw: dict[datetime, dict[str, Any]], start: datetime, end: datetime) -> list[dict[str, Any]]:
    return [item for bucket, item in sorted(raw.items()) if start <= bucket < end]


def consecutive_runs(buckets: list[datetime], predicate: set[datetime]) -> tuple[int, int]:
    if not buckets:
        return 0, 0
    max_run = run = 0
    previous: datetime | None = None
    elevated_count = 0
    for bucket in buckets:
        if bucket in predicate:
            elevated_count += 1
            run = run + 1 if previous is not None and bucket - previous == timedelta(minutes=3) else 1
            max_run = max(max_run, run)
        else:
            run = 0
        previous = bucket
    return elevated_count, max_run


def classify_spike_morphology(raw: list[dict[str, Any]], start: datetime, end: datetime, baseline: float | None) -> dict[str, Any]:
    fire = [item for item in raw if start <= item["bucket"] < end and finite(item.get("pm25_ugm3"))]
    buckets = [item["bucket"] for item in fire]
    if baseline is None:
        return {"classification": "NO_CLEAR_SPIKE", "reason": "NO_BASELINE", "elevated_bin_count": 0, "consecutive_elevated_bins": 0}
    thresholds = {str(delta): baseline + delta for delta in (3, 5, 10)}
    elevated = {delta: {item["bucket"] for item in fire if float(item["pm25_ugm3"]) > baseline + delta} for delta in (3, 5, 10)}
    count3, run3 = consecutive_runs(buckets, elevated[3])
    peak = max(fire, key=lambda item: float(item["pm25_ugm3"]), default=None)
    peaks = []
    for index, item in enumerate(fire):
        value = float(item["pm25_ugm3"])
        before = float(fire[index - 1]["pm25_ugm3"]) if index else -math.inf
        after = float(fire[index + 1]["pm25_ugm3"]) if index + 1 < len(fire) else -math.inf
        if value > baseline + 3 and value >= before and value >= after:
            if not peaks or item["bucket"] - peaks[-1]["bucket"] >= timedelta(minutes=6):
                peaks.append(item)
    if count3 == 0:
        classification = "NO_CLEAR_SPIKE"
    elif len(peaks) >= 2:
        classification = "MULTI_PEAK"
    elif count3 == 1 and run3 == 1:
        classification = "SINGLE_BIN_SPIKE"
    elif run3 >= 3:
        classification = "SUSTAINED_RISE"
    else:
        classification = "SHORT_PULSE"
    rises = []
    falls = []
    for first, second in zip(fire, fire[1:]):
        minutes = (second["phenomenon_time_utc"] - first["phenomenon_time_utc"]).total_seconds() / 60
        if minutes <= 0 or minutes > 9:
            continue
        change = float(second["pm25_ugm3"]) - float(first["pm25_ugm3"])
        (rises if change >= 0 else falls).append(change / minutes)
    return {
        "classification": classification,
        "baseline_plus_thresholds_ugm3": thresholds,
        "elevated_bin_count": count3,
        "duration_above_baseline_plus_3_minutes": count3 * 3,
        "duration_above_baseline_plus_5_minutes": len(elevated[5]) * 3,
        "duration_above_baseline_plus_10_minutes": len(elevated[10]) * 3,
        "elevated_bin_count_over_5": len(elevated[5]),
        "elevated_bin_count_over_10": len(elevated[10]),
        "consecutive_elevated_bins": run3,
        "peak_is_only_one_elevated_bin": count3 == 1,
        "local_peak_count": len(peaks),
        "max_rise_rate_ugm3_per_min": num(max(rises) if rises else None),
        "max_fall_rate_ugm3_per_min": num(min(falls) if falls else None),
        "peak_bucket_utc": iso_utc(peak["bucket"]) if peak else None,
    }


def distance_band(distance_km: float, band: tuple[str, float, float, bool]) -> bool:
    _, lower, upper, include_upper = band
    if lower == 0:
        return distance_km <= upper
    return lower <= distance_km <= upper if include_upper else lower <= distance_km < upper


def pearson(first: list[float], second: list[float]) -> float | None:
    if len(first) != len(second) or len(first) < 5:
        return None
    first_mean, second_mean = statistics.mean(first), statistics.mean(second)
    numerator = sum((a - first_mean) * (b - second_mean) for a, b in zip(first, second))
    denominator = math.sqrt(sum((a - first_mean) ** 2 for a in first) * sum((b - second_mean) ** 2 for b in second))
    return num(numerator / denominator) if denominator else None


def lagged_correlations(target: dict[datetime, dict[str, Any]], neighbor: dict[datetime, dict[str, Any]], start: datetime, end: datetime, target_base: float | None, neighbor_base: float | None) -> list[dict[str, Any]]:
    if target_base is None or neighbor_base is None:
        return []
    output = []
    cursor = floor_time(start, 3)
    while cursor <= floor_time(end, 3):
        for lag in LAGS_MINUTES:
            other = cursor + timedelta(minutes=lag)
            a, b = target.get(cursor), neighbor.get(other)
            if a and b and finite(a.get("pm25_ugm3")) and finite(b.get("pm25_ugm3")):
                output.append((lag, float(a["pm25_ugm3"]) - target_base, float(b["pm25_ugm3"]) - neighbor_base, cursor))
        cursor += timedelta(minutes=3)
    by_lag = []
    for lag in LAGS_MINUTES:
        values = [(a, b) for candidate_lag, a, b, _ in output if candidate_lag == lag]
        correlation = pearson([a for a, _ in values], [b for _, b in values])
        by_lag.append({"lag_minutes": lag, "correlation": correlation, "sample_count": len(values)})
    return by_lag


def prior_spike_diagnostic(raw: dict[datetime, dict[str, Any]], start: datetime, known_intervals: list[tuple[datetime, datetime]], peak_excess: float | None) -> dict[str, Any]:
    items = []
    for bucket, item in sorted(raw.items()):
        if bucket >= start or any(left <= bucket < right for left, right in known_intervals):
            continue
        items.append(item)
    values = [float(item["pm25_ugm3"]) for item in items if finite(item.get("pm25_ugm3"))]
    stats = robust_stats(values)
    base = stats.median
    if base is None:
        return {"status": "LIMITED_HISTORY", "history_sample_count": 0, "reason": "NO_PRE_FIRE_DATA"}
    by_bucket = {item["bucket"]: item for item in items}
    counts = {}
    isolated_by_delta: dict[int, list[datetime]] = {}
    for delta in (10, 20, 30):
        candidates = {bucket for bucket, item in by_bucket.items() if float(item["pm25_ugm3"]) > base + delta}
        isolated = [bucket for bucket in candidates if bucket - timedelta(minutes=3) not in candidates and bucket + timedelta(minutes=3) not in candidates]
        counts[f"isolated_spikes_over_baseline_plus_{delta}"] = len(isolated)
        isolated_by_delta[delta] = isolated
    earliest = min(item["bucket"] for item in items)
    latest = max(item["bucket"] for item in items)
    span_hours = (latest - earliest).total_seconds() / 3600
    similar_delta = max(3.0, float(peak_excess or 0.0))
    similar_candidates = {bucket for bucket, item in by_bucket.items() if float(item["pm25_ugm3"]) > base + similar_delta}
    similar = [bucket for bucket in similar_candidates if bucket - timedelta(minutes=3) not in similar_candidates and bucket + timedelta(minutes=3) not in similar_candidates]
    status = "SUFFICIENT_HISTORY" if len(values) >= 60 and span_hours >= 6 else "LIMITED_HISTORY"
    return {
        "status": status,
        "history_sample_count": len(values),
        "history_start_utc": iso_utc(earliest),
        "history_end_utc": iso_utc(latest),
        "history_span_hours": num(span_hours),
        "baseline_median": num(base),
        "mad": num(stats.mad),
        **counts,
        "maximum_prior_spike_over_baseline": num(max((float(item["pm25_ugm3"]) - base for item in items), default=None)),
        "prior_similar_threshold_excess_ugm3": num(similar_delta),
        "prior_similar_spike_count": len(similar),
        "prior_similar_spike_times_utc": [iso_utc(value) for value in similar[:50]],
        "limited_history_reason": "less than 60 valid bins or 6 hours" if status == "LIMITED_HISTORY" else None,
    }


def station_response(raw: dict[datetime, dict[str, Any]], start: datetime, end: datetime) -> tuple[float | None, dict[str, Any] | None, float | None]:
    baseline_values = [float(item["pm25_ugm3"]) for item in values_between(raw, start - timedelta(minutes=60), start)]
    baseline = median(baseline_values)
    response = values_between(raw, start, end)
    peak = max(response, key=lambda item: float(item["pm25_ugm3"]), default=None)
    excess = float(peak["pm25_ugm3"]) - baseline if peak and baseline is not None else None
    return baseline, peak, excess


def neighbor_metrics(target_id: str, target_peak: datetime | None, target_station: dict[str, Any], stations: list[dict[str, Any]], raw_by_station: dict[str, dict[datetime, dict[str, Any]]], start: datetime, response_end: datetime) -> dict[str, Any]:
    result = {}
    for name, lower, upper, include_upper in BANDS:
        members = []
        for station in stations:
            station_id = str(station["station_id"])
            if station_id == target_id or not finite(station.get("lat")) or not finite(station.get("lon")):
                continue
            distance = haversine_km((float(target_station["lon"]), float(target_station["lat"])), (float(station["lon"]), float(station["lat"])))
            if not distance_band(distance, (name, lower, upper, include_upper)):
                continue
            baseline, peak, excess = station_response(raw_by_station.get(station_id, {}), start, response_end)
            member = {"station_id": station_id, "distance_m": num(distance * 1000), "baseline_median": num(baseline), "peak_pm25": num(peak.get("pm25_ugm3") if peak else None), "peak_excess": num(excess), "peak_time_utc": iso_utc(peak["phenomenon_time_utc"]) if peak else None}
            if peak and target_peak:
                member["peak_delta_minutes"] = num((peak["phenomenon_time_utc"] - target_peak).total_seconds() / 60)
            members.append(member)
        usable = [item for item in members if finite(item.get("peak_excess"))]
        by_bucket: dict[datetime, list[float]] = {}
        for station in members:
            sid = station["station_id"]
            base = next((item["baseline_median"] for item in usable if item["station_id"] == sid), None)
            if base is None:
                continue
            for bucket, item in raw_by_station.get(sid, {}).items():
                if start <= bucket < response_end and finite(item.get("pm25_ugm3")):
                    by_bucket.setdefault(bucket, []).append(float(item["pm25_ugm3"]) - float(base))
        medians = [statistics.median(values) for values in by_bucket.values() if values]
        result[name] = {
            "lower_m": lower * 1000, "upper_m": upper * 1000, "sensor_count": len(members), "usable_count": len(usable),
            "baseline_median": median(item.get("baseline_median") for item in usable),
            "peak_median": median(item.get("peak_pm25") for item in usable),
            "max_median_excess": num(max(medians) if medians else None),
            "number_sensors_over_3": sum(float(item["peak_excess"]) > 3 for item in usable),
            "number_sensors_over_5": sum(float(item["peak_excess"]) > 5 for item in usable),
            "number_sensors_over_10": sum(float(item["peak_excess"]) > 10 for item in usable),
            "number_peaking_within_6_min": sum(abs(float(item.get("peak_delta_minutes", math.inf))) <= 6 for item in usable),
            "number_peaking_within_12_min": sum(abs(float(item.get("peak_delta_minutes", math.inf))) <= 12 for item in usable),
            "members": sorted(members, key=lambda item: item["distance_m"]),
        }
    return result


def pairwise_coherence(target_id: str, target_station: dict[str, Any], stations: list[dict[str, Any]], raw_by_station: dict[str, dict[datetime, dict[str, Any]]], start: datetime, end: datetime) -> dict[str, Any]:
    target_raw = raw_by_station.get(target_id, {})
    target_base = median(float(item["pm25_ugm3"]) for bucket, item in target_raw.items() if start - timedelta(minutes=60) <= bucket < start)
    candidates = []
    for station in stations:
        sid = str(station["station_id"])
        if sid == target_id or not finite(station.get("lat")) or not finite(station.get("lon")):
            continue
        distance = haversine_km((float(target_station["lon"]), float(target_station["lat"])), (float(station["lon"]), float(station["lat"])))
        if distance > 1.0:
            continue
        neighbor_raw = raw_by_station.get(sid, {})
        neighbor_base = median(float(item["pm25_ugm3"]) for bucket, item in neighbor_raw.items() if start - timedelta(minutes=60) <= bucket < start)
        correlations = lagged_correlations(target_raw, neighbor_raw, start - timedelta(minutes=60), end + timedelta(minutes=60), target_base, neighbor_base)
        best = max((item for item in correlations if finite(item.get("correlation"))), key=lambda item: float(item["correlation"]), default=None)
        if best:
            candidates.append({"station_id": sid, "distance_m": num(distance * 1000), "best_lag_minutes": best["lag_minutes"], "best_correlation": best["correlation"], "sample_count": best["sample_count"], "all_lags": correlations})
    best = max(candidates, key=lambda item: float(item["best_correlation"]), default=None)
    return {"neighbor_count_within_1km": len(candidates), "best_correlated_neighbor": best, "lag_semantics": "correlation(target[t], neighbor[t + lag]); positive lag means neighbor is later", "all_neighbors": sorted(candidates, key=lambda item: float(item["best_correlation"]), reverse=True)}


def cross_sensor_artifact(target_id: str, target_peak: dict[str, Any] | None, target_station: dict[str, Any], stations: list[dict[str, Any]], raw_by_station: dict[str, dict[datetime, dict[str, Any]]]) -> dict[str, Any]:
    raw = raw_by_station.get(target_id, {})
    peak_bucket = target_peak.get("bucket") if target_peak else None
    nearby = []
    if peak_bucket:
        for station in stations:
            sid = str(station["station_id"])
            if sid == target_id or not finite(station.get("lat")) or not finite(station.get("lon")):
                continue
            distance = haversine_km((float(target_station["lon"]), float(target_station["lat"])), (float(station["lon"]), float(station["lat"])))
            if distance <= 1.0:
                nearby.append((sid, raw_by_station.get(sid, {}).get(peak_bucket)))
    target_delay = None
    clock_status = "UNAVAILABLE"
    if target_peak and target_peak.get("ingested_at_utc") and target_peak.get("phenomenon_time_utc"):
        target_delay = (target_peak["ingested_at_utc"] - target_peak["phenomenon_time_utc"]).total_seconds() / 60
        clock_status = "CLOCK_AHEAD" if target_delay < -5 else ("STALE_INGESTION" if target_delay > 15 else "NORMAL_OR_UNKNOWN")
    times = sorted(raw)
    gaps = [(second - first).total_seconds() / 60 for first, second in zip(times, times[1:])]
    large_steps = []
    for first, second in zip(sorted(raw.values(), key=lambda item: item["phenomenon_time_utc"]), sorted(raw.values(), key=lambda item: item["phenomenon_time_utc"])[1:]):
        delta = abs(float(second["pm25_ugm3"]) - float(first["pm25_ugm3"]))
        if delta > 50:
            large_steps.append({"from_time_utc": iso_utc(first["phenomenon_time_utc"]), "to_time_utc": iso_utc(second["phenomenon_time_utc"]), "absolute_step_ugm3": num(delta)})
    duplicate_times = len(times) - len(set(times))
    return {
        "peak_neighbor_observation_count_within_1km": sum(item is not None for _, item in nearby),
        "peak_neighbor_missing_count_within_1km": sum(item is None for _, item in nearby),
        "peak_neighbor_ids_missing": [sid for sid, item in nearby if item is None],
        "target_ingestion_delay_minutes": num(target_delay),
        "target_clock_or_ingestion_status": clock_status,
        "median_cadence_minutes": num(statistics.median(gaps) if gaps else None),
        "unusual_gap_count_over_9_minutes": sum(gap > 9 for gap in gaps),
        "duplicate_phenomenon_timestamp_count": duplicate_times,
        "sudden_impossible_step_threshold_ugm3": 50.0,
        "sudden_impossible_steps": large_steps[:50],
        "metadata_abnormalities": [flag for flag, condition in (("missing_coordinates", not finite(target_station.get("lat")) or not finite(target_station.get("lon"))), ("mobile_station", bool(target_station.get("is_mobile"))), ("not_outdoor", target_station.get("is_outdoor") is False)) if condition],
        "unavailable_fields": ["request_id", "source_payload_timestamp", "transport_cadence_before_database_dedupe"],
    }


def wind_diagnostic(event: dict[str, Any], station: dict[str, Any], peak: dict[str, Any] | None, wind_field: Any) -> dict[str, Any]:
    event_lat, event_lon = float(event["location"]["lat"]), float(event["location"]["lon"])
    sensor_bearing = bearing_deg(event_lat, event_lon, float(station["lat"]), float(station["lon"]))
    times = [event["start_utc"] + (event["end_utc"] - event["start_utc"]) / 2]
    if peak:
        times.append(peak["phenomenon_time_utc"])
    estimates = []
    for at in dict.fromkeys(times):
        estimate = estimate_to_dict(get_wind(event_lat, event_lon, at, snapshot=wind_field)) if wind_field else None
        if estimate:
            wind_to = estimate.get("wind_to_deg")
            diff = abs((sensor_bearing - float(wind_to) + 180) % 360 - 180) if finite(wind_to) else None
            speed = estimate.get("speed_mps")
            distance = haversine_km((event_lon, event_lat), (float(station["lon"]), float(station["lat"])))
            relation = classify_wind_relation(sensor_bearing, wind_to)
            travel = nominal_travel_time_minutes(distance, speed) if relation == "downwind" else None
            estimates.append({"time_utc": iso_utc(at), "wind_to_deg": num(wind_to), "speed_mps": num(speed), "quality_category": estimate.get("quality_category"), "fire_to_sensor_bearing_deg": num(sensor_bearing), "angular_difference_deg": num(diff), "wind_relation": relation, "nominal_travel_time_minutes": travel, "expected_arrival_utc": iso_utc(event["start_utc"] + timedelta(minutes=travel)) if travel is not None else None, "wind_diagnostics": estimate.get("diagnostics")})
    observed_start = None
    observed_peak = peak["phenomenon_time_utc"] if peak else None
    if peak:
        observed_start = peak.get("first_elevated_time_utc")
    return {"estimates": estimates, "observed_spike_start_utc": observed_start, "observed_peak_utc": iso_utc(observed_peak), "observed_peak_delay_from_fire_start_minutes": num((observed_peak - event["start_utc"]).total_seconds() / 60) if observed_peak else None, "uncertainty": ["distance <100m" if estimates and haversine_km((event_lon, event_lat), (float(station["lon"]), float(station["lat"]))) < 0.1 else None, "wind quality not good" if estimates and any(item.get("quality_category") not in {"good", "usable"} for item in estimates) else None]}


def parse_aqx_p13(records: list[dict[str, Any]], start: datetime, end: datetime) -> list[dict[str, Any]]:
    output = []
    for record in records:
        keys = {str(key).casefold(): value for key, value in record.items()}
        item_name = str(keys.get("itemengname") or keys.get("itemname") or "")
        if "pm2.5" not in item_name.casefold() or "avg" in item_name.casefold():
            continue
        site_id = str(keys.get("siteid") or "").strip()
        monitor_date = str(keys.get("monitordate") or "").strip()
        if not site_id or not monitor_date:
            continue
        for hour in range(24):
            value = keys.get(f"monitorvalue{hour:02d}")
            if not finite(value) or float(value) < 0:
                continue
            try:
                local = datetime.fromisoformat(monitor_date.replace("/", "-"))
            except ValueError:
                try:
                    local = datetime.strptime(monitor_date, "%Y-%m-%d")
                except ValueError:
                    continue
            point = local.replace(hour=hour, tzinfo=TAIPEI).astimezone(UTC)
            if start <= point < end:
                output.append({"site_id": site_id, "publish_time_utc": point, "pm25_ugm3": float(value), "dataset": "AQX_P_13"})
    return output


def parse_aqx_p488(records: list[dict[str, Any]], start: datetime, end: datetime) -> list[dict[str, Any]]:
    output = []
    for record in records:
        keys = {str(key).casefold(): value for key, value in record.items()}
        value = keys.get("pm2.5")
        if not finite(value) or float(value) < 0:
            continue
        timestamp = parse_timestamp_utc(keys.get("datacreationdate") or keys.get("publishtime"))
        site_id = str(keys.get("siteid") or "").strip()
        if site_id and timestamp and start <= timestamp < end:
            output.append({"site_id": site_id, "publish_time_utc": timestamp, "pm25_ugm3": float(value), "dataset": "AQX_P_488"})
    return output


def fetch_reference_history(cache_dir: Path, start_local: datetime, end_local: datetime, timeout: float = 30.0) -> dict[str, Any]:
    cache_dir.mkdir(parents=True, exist_ok=True)
    start_utc, end_utc = start_local.astimezone(UTC), end_local.astimezone(UTC)
    result: dict[str, Any] = {"requested_window_local": f"{start_local.isoformat()} to {end_local.isoformat()}", "requested_window_utc": {"start": iso_utc(start_utc), "end": iso_utc(end_utc)}, "records": [], "attempts": [], "status": "UNAVAILABLE"}
    api_key = os.environ.get("MOENV_API_KEY", "")
    if not api_key.strip():
        result["reason"] = "MOENV_API_KEY is not set"
        return result
    for dataset, parser in (("AQX_P_13", parse_aqx_p13), ("AQX_P_488", parse_aqx_p488)):
        cache_path = cache_dir / f"{dataset.lower()}_{start_local.strftime('%Y%m%d')}_{start_local.strftime('%H%M')}_{end_local.strftime('%H%M')}.json.gz"
        attempt: dict[str, Any] = {"dataset": dataset, "cache_path": str(cache_path), "status": "NOT_RUN"}
        pages: list[Any] = []
        partial_reason: str | None = None
        try:
            if cache_path.exists():
                with gzip.open(cache_path, "rt", encoding="utf-8") as handle:
                    payload = json.load(handle)
                pages = payload.get("pages", [])
                partial_reason = payload.get("partial_reason")
            else:
                client = MoenvClient(api_key, dataset=dataset, timeout_seconds=timeout, max_pages=40)
                try:
                    for _, page in client.iter_pages():
                        pages.append(page)
                except MoenvError as exc:
                    # Keep the bounded partial response as evidence.  A page
                    # cap is not permission to claim that the date is absent.
                    partial_reason = str(exc)
                with gzip.open(cache_path, "wt", encoding="utf-8") as handle:
                    json.dump({"dataset": dataset, "fetched_at_utc": iso_utc(datetime.now(UTC)), "complete": partial_reason is None, "partial_reason": partial_reason, "pages": pages}, handle, ensure_ascii=False, sort_keys=True)
            records = []
            for page in pages:
                records.extend(page if isinstance(page, list) else page.get("result", {}).get("records", []))
            parsed = parser(records, start_utc, end_utc)
            attempt.update({"status": "PARTIAL" if partial_reason else "OK", "api_record_count": len(records), "pages_fetched": len(pages), "parsed_window_record_count": len(parsed), "partial_reason": partial_reason})
            result["attempts"].append(attempt)
            if parsed:
                result.update({"status": "OK", "selected_dataset": dataset, "records": [{**item, "publish_time_utc": iso_utc(item["publish_time_utc"])} for item in parsed]})
                return result
        except (MoenvError, OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
            attempt.update({"status": "ERROR", "reason": str(exc)})
            result["attempts"].append(attempt)
    result["reason"] = "No PM2.5 records in requested historical window"
    return result


def reference_event_summary(event: dict[str, Any], reference: dict[str, Any], reference_stations: list[dict[str, Any]]) -> dict[str, Any]:
    center = (float(event["location"]["lon"]), float(event["location"]["lat"]))
    station_map = {str(item["site_id"]): item for item in reference_stations}
    records = []
    for item in reference.get("records", []):
        station = station_map.get(str(item["site_id"]))
        if station and finite(station.get("lat")) and haversine_km(center, (float(station["lon"]), float(station["lat"]))) <= 30:
            records.append({**item, "distance_km": num(haversine_km(center, (float(station["lon"]), float(station["lat"])))), "site_name": station.get("site_name")})
    by_hour: dict[str, list[float]] = {}
    for item in records:
        hour = item["publish_time_utc"][:13] + "00:00Z"
        by_hour.setdefault(hour, []).append(float(item["pm25_ugm3"]))
    hourly = []
    for hour, values in sorted(by_hour.items()):
        hourly.append({"hour_utc": hour, "hour_local": datetime.fromisoformat(hour.replace("Z", "+00:00")).astimezone(TAIPEI).isoformat(), "valid_station_count": len(values), "median": median(values), "min": num(min(values)), "max": num(max(values)), "iqr": iqr(values)})
    fire_hours = [item for item in hourly if event["start_utc"] <= datetime.fromisoformat(item["hour_utc"].replace("Z", "+00:00")) < event["end_utc"] + timedelta(hours=1)]
    nonfire = [item["median"] for item in hourly if item not in fire_hours and finite(item.get("median"))]
    fire_medians = [item["median"] for item in fire_hours if finite(item.get("median"))]
    return {"radius_km": 30, "records_in_radius": len(records), "hourly": hourly, "known_fire_hours": fire_hours, "regional_nonfire_baseline_median": median(nonfire), "known_fire_hour_median": median(fire_medians), "known_fire_hour_delta": num(median(fire_medians) - median(nonfire)) if fire_medians and nonfire else None, "status": "OK" if fire_hours else "NO_ALIGNED_FIRE_HOURS"}


def classify_sensor(sensor: dict[str, Any]) -> str:
    excess = sensor.get("peak_excess_ugm3")
    if not finite(excess) or float(excess) < 3:
        return "NO_SIGNIFICANT_RESPONSE"
    corroborating = sensor.get("corroborating_sensors_within_500m", 0)
    morphology = sensor.get("morphology", {}).get("classification")
    if corroborating >= 2:
        return "COHERENT_LOCAL_RESPONSE"
    if float(excess) >= 10 and morphology in {"SUSTAINED_RISE", "MULTI_PEAK"} and sensor.get("wind_plausible"):
        return "NARROW_POSSIBLE_PLUME"
    prior = sensor.get("prior_history", {})
    artifact = sensor.get("cross_sensor_artifact", {})
    if morphology in {"SINGLE_BIN_SPIKE", "SHORT_PULSE"} and (prior.get("prior_similar_spike_count", 0) > 0 or artifact.get("peak_neighbor_observation_count_within_1km", 0) == 0 or artifact.get("sudden_impossible_steps")):
        return "ISOLATED_SENSOR_SPIKE"
    return "AMBIGUOUS"


def classify_event(sensors: list[dict[str, Any]]) -> str:
    significant = [item for item in sensors if item.get("classification") != "NO_SIGNIFICANT_RESPONSE"]
    if not significant:
        return "NO_MEASURABLE_RESPONSE"
    if sum(item.get("classification") == "COHERENT_LOCAL_RESPONSE" for item in significant) >= 2:
        return "MULTI_SENSOR_FIRE_RESPONSE"
    if any(item.get("classification") == "NARROW_POSSIBLE_PLUME" for item in significant):
        return "NARROW_RESPONSE_ONLY"
    if significant and all(item.get("classification") == "ISOLATED_SENSOR_SPIKE" for item in significant):
        return "SENSOR_ARTIFACT_LIKELY"
    return "AMBIGUOUS"


def build_event(event: dict[str, Any], stations: list[dict[str, Any]], raw_by_station: dict[str, dict[datetime, dict[str, Any]]], series: dict[str, list[BinnedObservation]], replay_rows: dict[datetime, dict[str, Any]], wind_field: Any, all_events: list[dict[str, Any]], reference: dict[str, Any], reference_stations: list[dict[str, Any]], config: AnomalyConfig) -> dict[str, Any]:
    start, end = event["start_utc"], event["end_utc"]
    response_end = end + timedelta(minutes=180)
    center = (float(event["location"]["lon"]), float(event["location"]["lat"]))
    nearby = [station for station in stations if finite(station.get("lat")) and finite(station.get("lon")) and haversine_km(center, (float(station["lon"]), float(station["lat"]))) <= 5]
    responses = []
    for station in nearby:
        sid = str(station["station_id"])
        baseline, peak, excess = station_response(raw_by_station.get(sid, {}), start, response_end)
        active_rows = [row for bucket, rows in replay_rows.items() if start <= bucket < end and (row := rows.get(sid))]
        max_fields = {key: max((row for row in active_rows if finite(row.get(key))), key=lambda row: float(row[key]), default=None) for key in ("anomaly_score", "temporal_z", "spatial_z")}
        responses.append({"station_id": sid, "station_name": station.get("station_name"), "lat": num(station.get("lat")), "lon": num(station.get("lon")), "distance_km": num(haversine_km(center, (float(station["lon"]), float(station["lat"])))), "baseline_median_ugm3": num(baseline), "peak_pm25_ugm3": num(peak.get("pm25_ugm3") if peak else None), "peak_excess_ugm3": num(excess), "peak_time_utc": iso_utc(peak["phenomenon_time_utc"]) if peak else None, "peak_bucket": peak, "max_anomaly_score": {"value": num(max_fields["anomaly_score"].get("anomaly_score") if max_fields["anomaly_score"] else None), "time_utc": max_fields["anomaly_score"].get("analysis_time_utc") if max_fields["anomaly_score"] else None}, "max_temporal_z": {"value": num(max_fields["temporal_z"].get("temporal_z") if max_fields["temporal_z"] else None), "time_utc": max_fields["temporal_z"].get("analysis_time_utc") if max_fields["temporal_z"] else None}, "max_spatial_z": {"value": num(max_fields["spatial_z"].get("spatial_z") if max_fields["spatial_z"] else None), "time_utc": max_fields["spatial_z"].get("analysis_time_utc") if max_fields["spatial_z"] else None}})
    top_ids = set()
    for key in ("peak_excess_ugm3",):
        candidate = max((item for item in responses if finite(item.get(key))), key=lambda item: float(item[key]), default=None)
        if candidate: top_ids.add(candidate["station_id"])
    for metric in ("max_anomaly_score", "max_temporal_z", "max_spatial_z"):
        candidate = max((item for item in responses if finite(item.get(metric, {}).get("value"))), key=lambda item: float(item[metric]["value"]), default=None)
        if candidate: top_ids.add(candidate["station_id"])
    station_by_id = {str(item["station_id"]): item for item in stations}
    sensors = []
    known_intervals = [(item["start_utc"], item["end_utc"]) for item in all_events]
    for sid in sorted(top_ids):
        station = station_by_id[sid]
        response = next(item for item in responses if item["station_id"] == sid)
        raw = raw_by_station.get(sid, {})
        base, peak, excess = station_response(raw, start, response_end)
        morphology = classify_spike_morphology(list(raw.values()), start, response_end, base)
        first_elevated = next((item for item in values_between(raw, start, response_end) if base is not None and float(item["pm25_ugm3"]) > base + 3), None)
        morphology["first_elevated_time_utc"] = iso_utc(first_elevated["phenomenon_time_utc"]) if first_elevated else None
        neighbors = neighbor_metrics(sid, peak["phenomenon_time_utc"] if peak else None, station, nearby, raw_by_station, start, response_end)
        corroborating = sum(
            1
            for name, band in neighbors.items()
            if name != "within_100m" and band["upper_m"] <= 500
            for member in band["members"]
            if finite(member.get("peak_excess"))
            and float(member["peak_excess"]) > 3
            and finite(member.get("peak_delta_minutes"))
            and abs(float(member["peak_delta_minutes"])) <= 12
        )
        coherence = pairwise_coherence(sid, station, nearby, raw_by_station, start - timedelta(minutes=60), end + timedelta(minutes=60))
        prior = prior_spike_diagnostic(raw, start, known_intervals, excess)
        artifact = cross_sensor_artifact(sid, peak, station, nearby, raw_by_station)
        wind = wind_diagnostic(event, station, peak, wind_field)
        wind_plausible = any(item.get("wind_relation") == "downwind" and finite(item.get("angular_difference_deg")) and float(item["angular_difference_deg"]) <= 90 and item.get("quality_category") in {"good", "usable"} for item in wind["estimates"])
        item = {"station_id": sid, "station_name": station.get("station_name"), "lat": num(station.get("lat")), "lon": num(station.get("lon")), "distance_to_known_fire_km": response["distance_km"], "fire_to_sensor_bearing_deg": num(bearing_deg(float(event["location"]["lat"]), float(event["location"]["lon"]), float(station["lat"]), float(station["lon"]))), "baseline_median_ugm3": num(base), "peak_pm25_ugm3": num(peak.get("pm25_ugm3") if peak else None), "peak_excess_ugm3": num(excess), "peak_time_utc": iso_utc(peak["phenomenon_time_utc"]) if peak else None, "peak_delay_relative_fire_start_minutes": num((peak["phenomenon_time_utc"] - start).total_seconds() / 60) if peak else None, "peak_delay_relative_fire_end_minutes": num((peak["phenomenon_time_utc"] - end).total_seconds() / 60) if peak else None, "max_anomaly_score": response["max_anomaly_score"], "max_temporal_z": response["max_temporal_z"], "max_spatial_z": response["max_spatial_z"], "anomaly_metrics_at_peak": next((row for bucket, rows in replay_rows.items() if peak and bucket == peak["bucket"] and (row := rows.get(sid))), None), "raw_timeline": timeline(raw, start - timedelta(minutes=90), response_end), "morphology": morphology, "neighbor_distance_bands": neighbors, "corroborating_sensors_within_500m": corroborating, "pairwise_temporal_coherence": coherence, "prior_history": prior, "cross_sensor_artifact": artifact, "wind_travel": wind, "wind_plausible": wind_plausible}
        item["classification"] = classify_sensor(item)
        sensors.append(item)
    classification = classify_event(sensors)
    return {"event": {**{key: value for key, value in event.items() if key not in {"start_utc", "end_utc"}}, "fire_start_utc": iso_utc(start), "fire_end_utc": iso_utc(end)}, "classification": classification, "top_response_sensor_set": [item["station_id"] for item in sensors], "sensors": sensors, "all_nearby_sensor_response": sorted([{key: value for key, value in item.items() if key != "peak_bucket"} for item in responses], key=lambda item: (item["peak_excess_ugm3"] is None, -(item["peak_excess_ugm3"] or -math.inf))), "historical_reference": reference_event_summary(event, reference, reference_stations), "method": {"scope": "post-hoc known-fire sensor spike forensics; not source attribution and not a production algorithm change", "raw_timeline_window": "fire start -90 minutes through fire end +180 minutes; 3-minute buckets; raw PM2.5, phenomenon and ingestion timestamps retained", "production_paths_reused": ["_smooth_series", "_analyse_station via replay_anomaly_from_snapshot", "get_wind"], "production_detector_modified": False, "thresholds_are_diagnostic_only": True, "neighbor_bands": [{"name": name, "lower_m": lower * 1000, "upper_m": upper * 1000} for name, lower, upper, _ in BANDS], "impossible_step_heuristic_ugm3": 50.0, "history_limited_rule": "LIMITED_HISTORY when fewer than 60 valid pre-fire bins or less than 6 hours", "classification_rules": {"A": "at least two corroborating sensors over +3 within 500m and within +/-12 minutes", "B": "peak excess >=10, sustained/multi-peak, and downwind/quality-plausible", "C": "single/short response plus prior similar spike, no neighbors, or quality signal", "E": "peak excess <3 or no usable baseline"}, "correlation_causation_note": "pairwise correlation and wind/travel are diagnostics only and are not causation"}}


def html_report(payload: dict[str, Any], title: str) -> str:
    blob = json.dumps(payload, ensure_ascii=False, allow_nan=False).replace("</", "<\\/")
    template = r'''<!doctype html><html><head><meta charset="utf-8"><title>__TITLE__</title><style>body{font:14px system-ui;margin:24px;color:#17202a}h1{margin-bottom:4px}.note{color:#5d6d7e}.grid{display:grid;grid-template-columns:1fr 1fr;gap:14px}.card{border:1px solid #d5d8dc;border-radius:8px;padding:12px;margin:12px 0;overflow:auto}table{border-collapse:collapse;width:100%;font-size:12px}th,td{border-bottom:1px solid #e5e7e9;padding:5px;text-align:left;white-space:nowrap}th{background:#f4f6f7}canvas{width:100%;height:auto;border:1px solid #e5e7e9;background:#fff}pre{white-space:pre-wrap}.pill{font-weight:700;padding:4px 8px;border-radius:10px;background:#eaf2f8}</style></head><body><h1>__TITLE__</h1><p class="note">POST-HOC DIAGNOSTIC ONLY · production detector, thresholds, clustering, recorders, wind interpolation, backtrace and evidence fusion were not modified.</p><p><span class="pill" id="class"></span> <span id="fire"></span></p><div class="grid"><div class="card"><h2>Event conclusion</h2><pre id="conclusion"></pre></div><div class="card"><h2>Historical reference AQ</h2><pre id="reference"></pre></div></div><div class="card"><h2>Top response sensors — raw PM2.5</h2><canvas id="chart" width="1300" height="560"></canvas></div><div class="card"><h2>Top response sensor summary</h2><table id="sensors"></table></div><div class="card"><h2>Very-local neighbor distance bands</h2><pre id="neighbors"></pre></div><div class="card"><h2>Wind / travel and artifact diagnostics</h2><pre id="diagnostics"></pre></div><div class="card"><h2>Raw diagnostic payload</h2><details><summary>show JSON</summary><pre id="json"></pre></details></div><script>const D=__BLOB__;const f=v=>v==null?'—':(typeof v==='number'?v.toFixed(2):v);document.getElementById('class').textContent=D.classification;document.getElementById('fire').textContent=D.event.fire_start_utc+' → '+D.event.fire_end_utc;document.getElementById('conclusion').textContent=JSON.stringify({classification:D.classification,top_response_sensor_set:D.top_response_sensor_set,method:D.method},null,2);document.getElementById('reference').textContent=JSON.stringify(D.historical_reference,null,2);document.getElementById('neighbors').textContent=JSON.stringify(Object.fromEntries(D.sensors.map(s=>[s.station_id,s.neighbor_distance_bands])),null,2);document.getElementById('diagnostics').textContent=JSON.stringify(Object.fromEntries(D.sensors.map(s=>[s.station_id,{wind_travel:s.wind_travel,prior_history:s.prior_history,cross_sensor_artifact:s.cross_sensor_artifact,pairwise_temporal_coherence:s.pairwise_temporal_coherence}])),null,2);document.getElementById('json').textContent=JSON.stringify(D,null,2);document.getElementById('sensors').innerHTML='<tr><th>sensor</th><th>dist km</th><th>peak excess</th><th>peak UTC</th><th>morphology</th><th>classification</th><th>prior similar</th><th>best corr / lag</th></tr>'+D.sensors.map(s=>{let c=s.pairwise_temporal_coherence.best_correlated_neighbor;return '<tr><td>'+s.station_id+'</td><td>'+f(s.distance_to_known_fire_km)+'</td><td>'+f(s.peak_excess_ugm3)+'</td><td>'+(s.peak_time_utc||'—')+'</td><td>'+s.morphology.classification+'</td><td>'+s.classification+'</td><td>'+f(s.prior_history.prior_similar_spike_count)+'</td><td>'+(c?f(c.best_correlation)+' / '+c.best_lag_minutes+'m':'—')+'</td></tr>'}).join('');const c=document.getElementById('chart'),x=c.getContext('2d');const series=D.sensors.map((s,i)=>({s,i,rows:s.raw_timeline.filter(r=>r.raw_pm25!=null)}));let all=series.flatMap(q=>q.rows.map(r=>({t:new Date(r.bucket_utc).getTime(),v:r.raw_pm25})));let t0=Math.min(...all.map(q=>q.t)),t1=Math.max(...all.map(q=>q.t)),v0=Math.min(...all.map(q=>q.v)),v1=Math.max(...all.map(q=>q.v));const X=t=>55+(t-t0)/(t1-t0||1)*1190,Y=v=>500-(v-v0)/(v1-v0||1)*420;x.strokeStyle='#d5d8dc';x.beginPath();x.moveTo(55,500);x.lineTo(1245,500);x.stroke();const fs=new Date(D.event.fire_start_utc).getTime(),fe=new Date(D.event.fire_end_utc).getTime();x.fillStyle='#fff4d6';x.fillRect(X(fs),40,X(fe)-X(fs),460);series.forEach(q=>{x.strokeStyle=`hsl(${q.i*83%360} 55% 42%)`;x.beginPath();q.rows.forEach((r,j)=>{let xx=X(new Date(r.bucket_utc).getTime()),yy=Y(r.raw_pm25);j?x.lineTo(xx,yy):x.moveTo(xx,yy)});x.stroke();x.fillStyle=x.strokeStyle;x.fillText(q.s.station_id,70+q.i*180,20)});x.fillStyle='#17202a';x.fillText('raw PM2.5 (µg/m³)',8,55);x.fillText('fire active interval',X(fs)+5,60);</script></body></html>'''
    return template.replace("__TITLE__", html.escape(title)).replace("__BLOB__", blob)


def write_summary(summary: dict[str, Any], output: Path) -> None:
    fields = ["event_id", "name", "fire_start_utc", "fire_end_utc", "classification", "top_response_sensor_count", "strongest_sensor_id", "strongest_sensor_distance_km", "strongest_peak_excess_ugm3", "strongest_duration_above_baseline_plus_3_minutes", "strongest_morphology", "corroborating_within_100m", "corroborating_within_250m", "corroborating_within_500m", "corroborating_within_1000m", "best_neighbor_correlation", "best_neighbor_lag_minutes", "prior_similar_spike_count", "wind_angular_difference_deg", "nominal_travel_time_minutes", "observed_peak_delay_minutes", "historical_reference_median_pm25", "historical_reference_delta_pm25", "historical_reference_status", "detail_html"]
    with (output / "summary.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows({field: row.get(field) for field in fields} for row in summary["events"])
    body = "".join(f'<tr><td><a href="{html.escape(row["detail_html"])}">{html.escape(row["name"])}</a></td><td>{row["classification"]}</td><td>{row.get("strongest_sensor_id") or "—"}</td><td>{row.get("strongest_sensor_distance_km") or "—"}</td><td>{row.get("strongest_peak_excess_ugm3") or "—"}</td><td>{row.get("strongest_morphology") or "—"}</td><td>{row.get("historical_reference_status")}</td></tr>' for row in summary["events"])
    (output / "known_fire_sensor_forensics.html").write_text(f'<!doctype html><html><head><meta charset="utf-8"><title>Known Fire Sensor Forensics</title><style>body{{font:14px system-ui;margin:24px;color:#17202a}}table{{border-collapse:collapse;width:100%}}th,td{{border:1px solid #d5d8dc;padding:7px;text-align:left}}th{{background:#f4f6f7}}.note{{color:#5d6d7e}}</style></head><body><h1>Known Fire Sensor Spike Forensics</h1><p class="note">Post-hoc validation only. No production algorithm or existing blind replay output was modified.</p><table><tr><th>event</th><th>classification</th><th>strongest sensor</th><th>distance km</th><th>peak excess</th><th>morphology</th><th>reference</th></tr>{body}</table><pre>{html.escape(json.dumps(summary["method"], ensure_ascii=False, indent=2))}</pre></body></html>', encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--events", type=Path, default=DEFAULT_EVENTS)
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE)
    parser.add_argument("--weather-database", type=Path, default=DEFAULT_WEATHER_DATABASE)
    parser.add_argument("--reference-database", type=Path, default=DEFAULT_REFERENCE_DATABASE)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--skip-reference-fetch", action="store_true")
    parser.add_argument("--timeout", type=float, default=30.0)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    events = parse_events(args.events)
    config = AnomalyConfig()
    analysis_start = min(item["start_utc"] for item in events) - timedelta(minutes=90)
    analysis_end = max(item["end_utc"] for item in events) + timedelta(minutes=180)
    stations, observations = read_pm_snapshot(args.database, analysis_end)
    raw_by_station = raw_bins(observations, config)
    series: dict[str, list[BinnedObservation]] = {}
    for item in _smooth_series(observations, config):
        series.setdefault(item.station_id, []).append(item)
    replay_start = min(item["start_utc"] for item in events) - timedelta(minutes=60)
    replay_end = max(item["end_utc"] for item in events) + timedelta(minutes=180)
    replay_rows = replay_anomaly_from_snapshot(stations, observations, args.config, replay_start, replay_end, config, datetime.now(UTC))
    wind_field = load_wind_snapshot_range(args.weather_database, analysis_start, analysis_end) if args.weather_database.exists() else None
    reference_stations = []
    if args.reference_database.exists():
        connection = duckdb.connect(str(args.reference_database), read_only=True)
        try:
            reference_stations = [dict(zip(("site_id", "site_name", "county", "lat", "lon"), row)) for row in connection.execute("SELECT site_id, site_name, county, lat, lon FROM reference_air_station").fetchall()]
        finally:
            connection.close()
    start_local = datetime(2026, 9, 3, 0, 0, tzinfo=TAIPEI)
    end_local = datetime(2026, 9, 3, 6, 0, tzinfo=TAIPEI)
    reference = {"status": "SKIPPED", "reason": "--skip-reference-fetch"} if args.skip_reference_fetch else fetch_reference_history(args.output_dir / "reference_cache", start_local, end_local, args.timeout)
    reports = []
    args.output_dir.mkdir(parents=True, exist_ok=True)
    for event in events:
        report = build_event(event, stations, raw_by_station, series, replay_rows, wind_field, events, reference, reference_stations, config)
        detail = args.output_dir / f"{event['event_id']}_forensics.html"
        detail.write_text(html_report(report, f"{event['name']} — Known Fire Sensor Forensics"), encoding="utf-8")
        strongest = max(report["sensors"], key=lambda item: float(item.get("peak_excess_ugm3") or -math.inf), default=None)
        best_neighbor = strongest.get("pairwise_temporal_coherence", {}).get("best_correlated_neighbor") if strongest else None
        wind_estimates = strongest.get("wind_travel", {}).get("estimates", []) if strongest else []
        angular = next((item.get("angular_difference_deg") for item in wind_estimates if finite(item.get("angular_difference_deg"))), None)
        travel = next((item.get("nominal_travel_time_minutes") for item in wind_estimates if finite(item.get("nominal_travel_time_minutes"))), None)
        bands = strongest.get("neighbor_distance_bands", {}) if strongest else {}
        reports.append({"event_id": event["event_id"], "name": event["name"], "fire_start_utc": report["event"]["fire_start_utc"], "fire_end_utc": report["event"]["fire_end_utc"], "classification": report["classification"], "top_response_sensor_count": len(report["sensors"]), "strongest_sensor_id": strongest.get("station_id") if strongest else None, "strongest_sensor_distance_km": strongest.get("distance_to_known_fire_km") if strongest else None, "strongest_peak_excess_ugm3": strongest.get("peak_excess_ugm3") if strongest else None, "strongest_duration_above_baseline_plus_3_minutes": strongest.get("morphology", {}).get("duration_above_baseline_plus_3_minutes") if strongest else None, "strongest_morphology": strongest.get("morphology", {}).get("classification") if strongest else None, "corroborating_within_100m": bands.get("within_100m", {}).get("number_sensors_over_3"), "corroborating_within_250m": bands.get("100_250m", {}).get("number_sensors_over_3"), "corroborating_within_500m": sum(bands.get(name, {}).get("number_sensors_over_3", 0) for name in ("100_250m", "250_500m")), "corroborating_within_1000m": sum(bands.get(name, {}).get("number_sensors_over_3", 0) for name in ("100_250m", "250_500m", "500_1000m")), "best_neighbor_correlation": best_neighbor.get("best_correlation") if best_neighbor else None, "best_neighbor_lag_minutes": best_neighbor.get("best_lag_minutes") if best_neighbor else None, "prior_similar_spike_count": strongest.get("prior_history", {}).get("prior_similar_spike_count") if strongest else None, "wind_angular_difference_deg": angular, "nominal_travel_time_minutes": travel, "observed_peak_delay_minutes": strongest.get("peak_delay_relative_fire_start_minutes") if strongest else None, "historical_reference_median_pm25": report["historical_reference"].get("known_fire_hour_median"), "historical_reference_delta_pm25": report["historical_reference"].get("known_fire_hour_delta"), "historical_reference_status": report["historical_reference"].get("status"), "detail_html": detail.name})
    summary = {"schema_version": 1, "generated_at_utc": iso_utc(datetime.now(UTC)), "known_events_config": str(args.events), "blind_replay_preserved": True, "production_detector_modified": False, "existing_outputs_preserved": True, "reference_backfill": reference, "events": reports, "method": {"scope": "post-hoc known-fire sensor spike forensics; not source attribution", "historical_reference_source_priority": ["AQX_P_13", "AQX_P_488"], "reference_window_local": "2026-09-03 00:00 through 05:00 Asia/Taipei", "reference_radius_km": 30, "raw_timestamps_preserved": True, "recorder_schema_unavailable_fields": ["request_id", "source_payload_timestamp", "transport_cadence_before_database_dedupe"]}}
    (args.output_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    write_summary(summary, args.output_dir)
    print("AirTrace Known Fire Sensor Spike Forensics")
    for row in reports:
        print(f"{row['name']}: {row['classification']} | strongest={row['strongest_sensor_id']} | distance={row['strongest_sensor_distance_km']} km | excess={row['strongest_peak_excess_ugm3']} | morphology={row['strongest_morphology']} | reference={row['historical_reference_status']}")
    print(f"Output: {args.output_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
