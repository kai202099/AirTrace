#!/usr/bin/env python3
"""Post-hoc multi-scale spatial-baseline diagnostic for known fires.

This module deliberately does not participate in the production detector. It
replays the unchanged v1 rows, then computes fire-centred groups and target-
centred alternative rings from the same read-only PM2.5 snapshot.
"""

from __future__ import annotations

import argparse
import csv
import html
import json
import math
import statistics
import sys
from collections import OrderedDict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import duckdb  # noqa: E402

from airtrace.analysis.anomaly import (  # noqa: E402
    AnomalyConfig,
    BinnedObservation,
    _analyse_station,
    _current_for_station,
    _quality_flags,
    _smooth_series,
    bbox_contains,
    floor_time,
    haversine_km,
    iso_utc,
    load_pilot_config,
    robust_stats,
)
from scripts.analyze_known_fire_response import load_events  # noqa: E402

UTC = timezone.utc
DEFAULT_DATABASE = ROOT / "data" / "airtrace.duckdb"
DEFAULT_REFERENCE_DATABASE = ROOT / "data" / "reference_air.duckdb"
DEFAULT_CONFIG = ROOT / "config" / "pilot_region.json"
DEFAULT_EVENTS = ROOT / "config" / "validation" / "known_fires_20260903.json"
DEFAULT_REPLAY = ROOT / "reports" / "events" / "latest_events.json"
DEFAULT_OUTPUT = ROOT / "reports" / "validation" / "spatial_baseline"

# Boundaries are intentional: overlapping target-centred rings are useful
# counterfactual baselines, while fire-centred groups partition the area.
RING_DEFINITIONS = OrderedDict(
    (
        ("1_2", (1.0, 2.0, False)),
        ("1_3", (1.0, 3.0, False)),
        ("2_3", (2.0, 3.0, False)),
        ("2_5", (2.0, 5.0, True)),
    )
)
GROUP_DEFINITIONS = OrderedDict(
    (("inner", (0.0, 1.0, False)), ("middle", (1.0, 2.0, False)), ("outer", (2.0, 5.0, True)))
)


def finite(value: Any) -> bool:
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


def number(value: Any, digits: int = 6) -> float | None:
    return round(float(value), digits) if finite(value) else None


def safe_median(values: Iterable[float]) -> float | None:
    clean = [float(value) for value in values if finite(value)]
    return number(statistics.median(clean)) if clean else None


def percentile(values: Iterable[float], fraction: float) -> float | None:
    ordered = sorted(float(value) for value in values if finite(value))
    if not ordered:
        return None
    if len(ordered) == 1:
        return number(ordered[0])
    position = (len(ordered) - 1) * fraction
    low = int(position)
    high = min(low + 1, len(ordered) - 1)
    return number(ordered[low] + (ordered[high] - ordered[low]) * (position - low))


def ring_membership(distance_km: float, lower_km: float, upper_km: float, include_upper: bool = False) -> bool:
    """Return deterministic half-open ring membership, with optional closed upper edge."""

    if not finite(distance_km):
        return False
    distance = float(distance_km)
    return distance >= lower_km and (distance <= upper_km if include_upper else distance < upper_km)


def spatial_group(distance_km: float) -> str | None:
    for name, (lower, upper, include_upper) in GROUP_DEFINITIONS.items():
        if ring_membership(distance_km, lower, upper, include_upper):
            return name
    return None


def robust_ring_diagnostic(values: Iterable[float], minimum: int = 3, sigma_floor: float = 1.5) -> dict[str, Any]:
    clean = [float(value) for value in values if finite(value)]
    stats = robust_stats(clean, sigma_floor)
    return {
        "neighbor_count": len(clean),
        "median": number(stats.median),
        "mad": number(stats.mad),
        "robust_sigma": number(stats.sigma),
        "status": "sufficient" if len(clean) >= minimum else "insufficient",
    }


def select_ring_neighbors(
    target_id: str,
    target: dict[str, Any],
    stations: Iterable[dict[str, Any]],
    current_by_station: dict[str, BinnedObservation],
    ring: tuple[float, float, bool],
) -> list[tuple[float, str, BinnedObservation]]:
    """Select valid, time-aligned context sensors; always exclude the target."""

    result = []
    lat, lon = target.get("lat"), target.get("lon")
    if not finite(lat) or not finite(lon):
        return result
    for station in stations:
        station_id = str(station["station_id"])
        if station_id == str(target_id) or station_id not in current_by_station:
            continue
        if not finite(station.get("lat")) or not finite(station.get("lon")):
            continue
        distance = haversine_km((float(lon), float(lat)), (float(station["lon"]), float(station["lat"])))
        if ring_membership(distance, *ring):
            result.append((distance, station_id, current_by_station[station_id]))
    return sorted(result, key=lambda item: (item[0], item[1]))


def ring_metric(target_value: float | None, neighbors: list[tuple[float, str, BinnedObservation]], config: AnomalyConfig) -> dict[str, Any]:
    stats = robust_ring_diagnostic((item[2].smoothed_pm25 for item in neighbors), config.min_neighbors, config.robust_sigma_floor)
    excess = float(target_value) - float(stats["median"]) if finite(target_value) and finite(stats["median"]) else None
    z = excess / float(stats["robust_sigma"]) if finite(excess) and finite(stats["robust_sigma"]) and float(stats["robust_sigma"]) else None
    return stats | {"excess": number(excess), "z": number(z)}


def ring_supports(metric: dict[str, Any], config: AnomalyConfig) -> bool:
    """Evaluate a counterfactual ring threshold without mutating a v1 row."""

    return bool(
        metric.get("status") == "sufficient"
        and finite(metric.get("excess"))
        and finite(metric.get("z"))
        and float(metric["excess"]) >= config.spatial_excess_threshold
        and float(metric["z"]) >= config.spatial_z_threshold
    )


def valid_current_map(
    stations: Iterable[dict[str, Any]],
    series: dict[str, list[BinnedObservation]],
    cutoff: datetime,
    now: datetime,
    config: AnomalyConfig,
) -> dict[str, BinnedObservation]:
    result = {}
    for station in stations:
        station_id = str(station["station_id"])
        current = _current_for_station(series.get(station_id, []), cutoff)
        if current is None:
            continue
        flags = _quality_flags(current, cutoff, now, config)
        if "stale_current_observation" in flags or "suspicious_future_timestamp" in flags:
            continue
        result[station_id] = current
    return result


def read_pm_snapshot(database: Path, start: datetime, end: datetime) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    connection = duckdb.connect(str(database), read_only=True)
    try:
        connection.execute("SET TimeZone='UTC'")
        station_fields = ("thing_id", "station_id", "station_name", "lat", "lon", "city", "township", "area_type")
        stations = [dict(zip(station_fields, row)) for row in connection.execute("SELECT thing_id, station_id, station_name, lat, lon, city, township, area_type FROM sensor_station").fetchall()]
        fields = ("station_id", "datastream_id", "phenomenon_time_utc", "pm25_ugm3", "source_status", "quality_flags")
        observations = [dict(zip(fields, row)) for row in connection.execute(
            "SELECT station_id, datastream_id, phenomenon_time_utc, pm25_ugm3, source_status, quality_flags FROM pm25_observation WHERE phenomenon_time_utc >= ? AND phenomenon_time_utc <= ? ORDER BY station_id, phenomenon_time_utc",
            [start, end],
        ).fetchall()]
        return stations, observations
    finally:
        connection.close()


def read_reference_snapshot(database: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if not database.exists():
        return [], []
    connection = duckdb.connect(str(database), read_only=True)
    try:
        connection.execute("SET TimeZone='UTC'")
        stations = [dict(zip(("site_id", "site_name", "county", "lat", "lon"), row)) for row in connection.execute("SELECT site_id, site_name, county, lat, lon FROM reference_air_station").fetchall()]
        observations = [dict(zip(("site_id", "publish_time_utc", "pm25_ugm3", "quality_flags"), row)) for row in connection.execute("SELECT site_id, publish_time_utc, pm25_ugm3, quality_flags FROM reference_air_observation").fetchall()]
        return stations, observations
    finally:
        connection.close()


def current_ring_metrics(
    target_id: str,
    target: dict[str, Any],
    context_stations: list[dict[str, Any]],
    current_by_station: dict[str, BinnedObservation],
    config: AnomalyConfig,
) -> dict[str, Any]:
    target_current = current_by_station.get(str(target_id))
    target_value = target_current.smoothed_pm25 if target_current else None
    result = {}
    for name, definition in RING_DEFINITIONS.items():
        neighbors = select_ring_neighbors(target_id, target, context_stations, current_by_station, definition)
        result[name] = ring_metric(target_value, neighbors, config)
        result[name]["neighbor_ids"] = [item[1] for item in neighbors]
    return result


def group_values(
    event: dict[str, Any],
    group: str,
    stations: list[dict[str, Any]],
    current_by_station: dict[str, BinnedObservation],
) -> list[tuple[str, float]]:
    lat, lon = float(event["location"]["lat"]), float(event["location"]["lon"])
    lower, upper, include_upper = GROUP_DEFINITIONS[group]
    values = []
    for station in stations:
        station_id = str(station["station_id"])
        current = current_by_station.get(station_id)
        if current is None or not finite(station.get("lat")) or not finite(station.get("lon")):
            continue
        distance = haversine_km((lon, lat), (float(station["lon"]), float(station["lat"])))
        if ring_membership(distance, lower, upper, include_upper):
            values.append((station_id, float(current.smoothed_pm25)))
    return values


def fire_centered_row(
    event: dict[str, Any],
    stations: list[dict[str, Any]],
    series: dict[str, list[BinnedObservation]],
    cutoff: datetime,
    now: datetime,
    config: AnomalyConfig,
    production_rows: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    current = valid_current_map(stations, series, cutoff, now, config)
    groups: dict[str, dict[str, Any]] = {}
    for group in GROUP_DEFINITIONS:
        pairs = group_values(event, group, stations, current)
        values = [value for _, value in pairs]
        groups[group] = {"sensor_count": len(values), "median_pm25": safe_median(values), "p75_pm25": percentile(values, 0.75), "max_pm25": number(max(values)) if values else None, "sensor_ids": [item[0] for item in pairs]}
    return {"time_bin_utc": iso_utc(cutoff - timedelta(minutes=config.bin_minutes) + timedelta(microseconds=1)), "groups": groups, "production": production_aggregate(production_rows)}


def production_aggregate(rows: dict[str, dict[str, Any]]) -> dict[str, Any]:
    values = [row for row in rows.values() if row]
    return {
        "spatial_baseline_median": safe_median(row.get("spatial_median") for row in values),
        "spatial_excess_median": safe_median(row.get("spatial_excess") for row in values),
        "spatial_z_median": safe_median(row.get("spatial_z") for row in values),
        "sufficient_count": sum(row.get("spatial_status") == "sufficient" for row in values),
        "row_count": len(values),
    }


def add_group_excess(timeline: list[dict[str, Any]], start: datetime, config: AnomalyConfig) -> dict[str, float | None]:
    baseline: dict[str, float | None] = {}
    for group in GROUP_DEFINITIONS:
        baseline[group] = safe_median(
            row["groups"][group]["median_pm25"]
            for row in timeline
            if start - timedelta(minutes=60) <= datetime.fromisoformat(row["time_bin_utc"].replace("Z", "+00:00")) < start
        )
    for row in timeline:
        for group in GROUP_DEFINITIONS:
            value = row["groups"][group]["median_pm25"]
            base = baseline[group]
            row["groups"][group]["pre_fire_baseline_median"] = number(base)
            row["groups"][group]["median_excess_relative_pre_fire_baseline"] = number(float(value) - base) if finite(value) and finite(base) else None
        row["inner_outer_contrast"] = number(
            float(row["groups"]["inner"]["median_pm25"]) - float(row["groups"]["outer"]["median_pm25"])
            if finite(row["groups"]["inner"]["median_pm25"]) and finite(row["groups"]["outer"]["median_pm25"]) else None
        )
        row["middle_outer_contrast"] = number(
            float(row["groups"]["middle"]["median_pm25"]) - float(row["groups"]["outer"]["median_pm25"])
            if finite(row["groups"]["middle"]["median_pm25"]) and finite(row["groups"]["outer"]["median_pm25"]) else None
        )
    return baseline


def reference_control(event: dict[str, Any], stations: list[dict[str, Any]], observations: list[dict[str, Any]]) -> dict[str, Any]:
    lat, lon = float(event["location"]["lat"]), float(event["location"]["lon"])
    start, end = event["start_utc"], event["end_utc"]
    station_map = {str(item["site_id"]): item for item in stations}
    nearby = {site_id: item for site_id, item in station_map.items() if finite(item.get("lat")) and finite(item.get("lon")) and haversine_km((lon, lat), (float(item["lon"]), float(item["lat"]))) <= 50.0}
    by_site: dict[str, list[dict[str, Any]]] = {}
    for row in observations:
        if str(row["site_id"]) in nearby and finite(row.get("pm25_ugm3")):
            by_site.setdefault(str(row["site_id"]), []).append(row)
    site_rows = []
    for site_id, site in nearby.items():
        values = sorted(by_site.get(site_id, []), key=lambda item: item["publish_time_utc"])
        baseline_values = [float(item["pm25_ugm3"]) for item in values if start - timedelta(hours=3) <= item["publish_time_utc"] < start]
        active_values = [float(item["pm25_ugm3"]) for item in values if start <= item["publish_time_utc"] < end]
        base = safe_median(baseline_values)
        active = safe_median(active_values)
        site_rows.append({"site_id": site_id, "site_name": site.get("site_name"), "distance_km": number(haversine_km((lon, lat), (float(site["lon"]), float(site["lat"])))), "baseline_count": len(baseline_values), "active_count": len(active_values), "baseline_median": base, "active_median": active, "delta": number(float(active) - base) if finite(active) and finite(base) else None})
    deltas = [float(row["delta"]) for row in site_rows if finite(row.get("delta"))]
    sufficient = [value for value in deltas if finite(value)]
    return {"radius_km": 50, "site_count": len(site_rows), "sites_with_active_and_baseline": len(sufficient), "baseline_window_hours": 3, "active_window": {"start_utc": iso_utc(start), "end_utc": iso_utc(end)}, "median_delta": safe_median(deltas), "fraction_delta_at_least_2": number(sum(value >= 2.0 for value in deltas) / len(deltas)) if deltas else None, "behavior": "INSUFFICIENT_DATA" if len(sufficient) < 3 else ("REGIONAL_BACKGROUND_CONTAMINATION" if sum(value >= 2.0 for value in deltas) / len(deltas) >= 0.5 else "NO_REGIONAL_BACKGROUND_RISE"), "sites": sorted(site_rows, key=lambda item: (item["distance_km"] or 999, item["site_id"]))[:50]}


def outer_sensor_control(timeline: list[dict[str, Any]], start: datetime, end: datetime) -> dict[str, Any]:
    active = []
    for row in timeline:
        stamp = datetime.fromisoformat(row["time_bin_utc"].replace("Z", "+00:00"))
        if start <= stamp < end:
            group = row["groups"]["outer"]
            value, baseline = group.get("median_pm25"), group.get("pre_fire_baseline_median")
            if finite(value) and finite(baseline):
                active.append(float(value) - float(baseline))
    return {"radius_km": 5, "definition": "fire-centred outer group 2–5 km", "active_bin_count": len(active), "median_delta": safe_median(active), "fraction_delta_at_least_2": number(sum(value >= 2.0 for value in active) / len(active)) if active else None, "behavior": "INSUFFICIENT_DATA" if len(active) < 3 else ("REGIONAL_BACKGROUND_CONTAMINATION" if sum(value >= 2.0 for value in active) / len(active) >= 0.5 else "NO_REGIONAL_BACKGROUND_RISE")}


def combine_regional_controls(outer: dict[str, Any], reference: dict[str, Any]) -> str:
    if outer["behavior"] == "INSUFFICIENT_DATA" or reference["behavior"] == "INSUFFICIENT_DATA":
        return "INSUFFICIENT_DATA"
    if outer["behavior"] == "REGIONAL_BACKGROUND_CONTAMINATION" and reference["behavior"] == "REGIONAL_BACKGROUND_CONTAMINATION":
        return "REGIONAL_BACKGROUND_CONTAMINATION"
    return "NO_REGIONAL_BACKGROUND_RISE"


def production_row_lookup(results_by_bin: dict[datetime, dict[str, dict[str, Any]]], station_ids: set[str], start: datetime, end: datetime, config: AnomalyConfig) -> list[dict[str, Any]]:
    pairs = []
    current = floor_time(start, config.bin_minutes)
    while current < end:
        for station_id in station_ids:
            row = results_by_bin.get(current, {}).get(station_id)
            if row:
                pairs.append(dict(row) | {"_bin": current})
        current += timedelta(minutes=config.bin_minutes)
    return pairs


def top_pairs(
    event: dict[str, Any],
    stations: list[dict[str, Any]],
    context_stations: list[dict[str, Any]],
    series: dict[str, list[BinnedObservation]],
    results_by_bin: dict[datetime, dict[str, dict[str, Any]]],
    config: AnomalyConfig,
    now: datetime,
) -> tuple[list[dict[str, Any]], dict[str, Any], dict[str, dict[str, float | None]], list[dict[str, Any]]]:
    lat, lon = float(event["location"]["lat"]), float(event["location"]["lon"])
    nearby_ids = {str(station["station_id"]) for station in stations if finite(station.get("lat")) and finite(station.get("lon")) and haversine_km((lon, lat), (float(station["lon"]), float(station["lat"]))) <= 5.0}
    station_map = {str(station["station_id"]): station for station in context_stations}
    pairs = production_row_lookup(results_by_bin, nearby_ids, event["start_utc"], event["end_utc"], config)
    ranked = sorted(pairs, key=lambda row: (float(row.get("anomaly_score") or -math.inf), str(row["station_id"]), str(row["analysis_time_utc"])), reverse=True)
    all_pairs = []
    support_counts = {"production": 0, "1_3": 0, "2_5": 0}
    temporal_pairs = 0
    ring_values_by_bin: dict[str, dict[str, list[float]]] = {}
    for row in ranked:
        station = station_map.get(str(row["station_id"]))
        if station is None:
            continue
        current = valid_current_map(context_stations, series, row["_bin"] + timedelta(minutes=config.bin_minutes) - timedelta(microseconds=1), now, config)
        rings = current_ring_metrics(str(row["station_id"]), station, context_stations, current, config)
        output = {key: value for key, value in row.items() if key != "_bin"} | {"ring_metrics": rings}
        output["production_threshold_margins"] = {key: {"value": row.get(key), "threshold": threshold, "margin": number(float(row[key]) - threshold) if finite(row.get(key)) else None, "pass": bool(finite(row.get(key)) and float(row[key]) >= threshold)} for key, threshold in (("spatial_excess", config.spatial_excess_threshold), ("spatial_z", config.spatial_z_threshold))}
        output["ring_threshold_margins"] = {name: {key: {"value": rings[name].get(key), "threshold": threshold, "margin": number(float(rings[name][key]) - threshold) if finite(rings[name].get(key)) else None, "pass": bool(finite(rings[name].get(key)) and float(rings[name][key]) >= threshold)} for key, threshold in (("excess", config.spatial_excess_threshold), ("z", config.spatial_z_threshold))} for name in RING_DEFINITIONS}
        all_pairs.append(output)
        bin_key = iso_utc(row["_bin"])
        ring_values_by_bin.setdefault(bin_key, {})
        for name in RING_DEFINITIONS:
            if finite(rings[name].get("median")):
                ring_values_by_bin[bin_key].setdefault(name, []).append(float(rings[name]["median"]))
        if row.get("temporal_status") == "sufficient" and finite(row.get("temporal_excess")) and finite(row.get("temporal_z")) and float(row["temporal_excess"]) >= config.temporal_excess_threshold and float(row["temporal_z"]) >= config.temporal_z_threshold:
            temporal_pairs += 1
            for name in ("1_3", "2_5"):
                metric = rings[name]
                if ring_supports(metric, config):
                    support_counts[name] += 1
        if row.get("spatial_status") == "sufficient" and finite(row.get("spatial_excess")) and finite(row.get("spatial_z")) and float(row["spatial_excess"]) >= config.spatial_excess_threshold and float(row["spatial_z"]) >= config.spatial_z_threshold:
            support_counts["production"] += 1
    ring_baselines = {bin_key: {name: safe_median(values) for name, values in by_ring.items()} for bin_key, by_ring in ring_values_by_bin.items()}
    return all_pairs[:20], {"active_pair_count": len(all_pairs), "temporal_passing_pair_count": temporal_pairs, "production_spatial_supported_pair_count": support_counts["production"], "counterfactual_1_3_supported_temporal_pair_count": support_counts["1_3"], "counterfactual_2_5_supported_temporal_pair_count": support_counts["2_5"], "note": "COUNTERFACTUAL DIAGNOSTIC ONLY; no production candidate was recomputed"}, ring_baselines, all_pairs


def classify_spatial(timeline: list[dict[str, Any]], start: datetime, end: datetime, regional_behavior: str) -> str:
    contrasts = [float(row["inner_outer_contrast"]) for row in timeline if finite(row.get("inner_outer_contrast")) and row["groups"]["inner"]["sensor_count"] >= 1 and row["groups"]["outer"]["sensor_count"] >= 3]
    active = [float(row["inner_outer_contrast"]) for row in timeline if start <= datetime.fromisoformat(row["time_bin_utc"].replace("Z", "+00:00")) < end and finite(row.get("inner_outer_contrast"))]
    if not contrasts:
        return "INSUFFICIENT_DATA"
    meaningful = sum(value >= 5.0 for value in active)
    if regional_behavior == "REGIONAL_BACKGROUND_CONTAMINATION":
        return "REGIONAL_BACKGROUND_CONTAMINATION"
    if meaningful >= 1:
        return "LOCAL_CONTRAST_PRESENT"
    return "NO_LOCAL_CONTRAST"


def contamination_answer(timeline: list[dict[str, Any]], start: datetime, end: datetime) -> str:
    pre = [row["production"]["spatial_baseline_median"] for row in timeline if start - timedelta(minutes=60) <= datetime.fromisoformat(row["time_bin_utc"].replace("Z", "+00:00")) < start]
    during = [row["production"]["spatial_baseline_median"] for row in timeline if start <= datetime.fromisoformat(row["time_bin_utc"].replace("Z", "+00:00")) < end]
    pre_median, during_median = safe_median(pre), safe_median(during)
    contrasts = [row.get("inner_outer_contrast") for row in timeline if start <= datetime.fromisoformat(row["time_bin_utc"].replace("Z", "+00:00")) < end and finite(row.get("inner_outer_contrast"))]
    if pre_median is None or during_median is None or not contrasts:
        return "AMBIGUOUS"
    baseline_delta = float(during_median) - float(pre_median)
    if baseline_delta >= 2.0 and max(float(value) for value in contrasts) >= 5.0:
        return "YES"
    if max(float(value) for value in contrasts) >= 5.0:
        return "NO"
    return "AMBIGUOUS"


def read_replay(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"available": False, "station_ids": set()}
    payload = json.loads(path.read_text(encoding="utf-8"))
    return {"available": True, "station_ids": {str(row["station_id"]) for row in payload.get("context_sensors", [])}}


def html_detail(payload: dict[str, Any]) -> str:
    blob = json.dumps(payload, ensure_ascii=False, allow_nan=False).replace("</", "<\\/")
    title = html.escape(payload["event"]["name"])
    return f'''<!doctype html><html><head><meta charset="utf-8"><title>{title} spatial baseline</title><style>body{{font:14px system-ui;margin:24px;color:#17202a}}.note{{color:#5d6d7e}}.grid{{display:grid;grid-template-columns:1fr 1fr;gap:16px}}.card{{border:1px solid #d5d8dc;border-radius:8px;padding:12px;margin:12px 0;overflow:auto}}table{{border-collapse:collapse;width:100%;font-size:12px}}th,td{{border-bottom:1px solid #e5e7e9;padding:5px;text-align:left;white-space:nowrap}}th{{background:#f4f6f7}}canvas{{width:100%;height:auto;border:1px solid #e5e7e9;background:#fff}}pre{{white-space:pre-wrap}}</style></head><body><h1>{title} — Multi-scale Spatial Baseline Diagnostic</h1><p class="note">POST-HOC DIAGNOSTIC ONLY · COUNTERFACTUAL DIAGNOSTIC ONLY · production detector was not modified.</p><p><strong id="class"></strong> · fire active interval: {payload["event"]["fire_start_utc"]} → {payload["event"]["fire_end_utc"]}</p><div class="grid"><div class="card"><h2>Conclusion</h2><pre id="conclusion"></pre></div><div class="card"><h2>Regional control</h2><pre id="regional"></pre></div></div><div class="card"><h2>Fire-centred PM2.5 timeline</h2><canvas id="chart" width="1300" height="560"></canvas></div><div class="card"><h2>Contrast / baseline comparison</h2><canvas id="chart2" width="1300" height="420"></canvas></div><div class="card"><h2>Top 20 anomaly-relevant sensor/time pairs</h2><table id="pairs"></table></div><div class="card"><h2>Method and raw diagnostic payload</h2><pre id="method"></pre></div><script>const D={blob};const f=v=>v==null?'—':(typeof v==='number'?v.toFixed(2):v);document.getElementById('class').textContent=D.diagnostic_classification;document.getElementById('conclusion').textContent=JSON.stringify({{classification:D.diagnostic_classification,baseline_contamination:D.baseline_contamination,summary:D.summary,counterfactual:D.counterfactual}},null,2);document.getElementById('regional').textContent=JSON.stringify(D.regional_background,null,2);document.getElementById('method').textContent=JSON.stringify(D.method,null,2);const c=document.getElementById('chart'),x=c.getContext('2d'),T=D.timeline;let vals=[];T.forEach(r=>['inner','middle','outer'].forEach(g=>{{if(r.groups[g].median_pm25!=null)vals.push(r.groups[g].median_pm25)}}));let lo=Math.min(...vals),hi=Math.max(...vals),ts=T.map(r=>new Date(r.time_bin_utc).getTime()),t0=Math.min(...ts),t1=Math.max(...ts),X=t=>55+(t-t0)/(t1-t0||1)*1190,Y=v=>495-(v-lo)/(hi-lo||1)*390;x.clearRect(0,0,c.width,c.height);x.fillStyle='#fff4d6';let fs=new Date(D.event.fire_start_utc).getTime(),fe=new Date(D.event.fire_end_utc).getTime();x.fillRect(X(fs),45,X(fe)-X(fs),450);[['inner','#d92d20'],['middle','#f79009'],['outer','#1677ff']].forEach(([g,col])=>{{x.strokeStyle=col;x.lineWidth=2;x.beginPath();T.forEach((r,i)=>{{let v=r.groups[g].median_pm25;if(v!=null){{if(i)x.lineTo(X(new Date(r.time_bin_utc).getTime()),Y(v));else x.moveTo(X(new Date(r.time_bin_utc).getTime()),Y(v));}}}});x.stroke()}});x.fillStyle='#17202a';x.fillText('PM2.5 median (inner red / middle orange / outer blue)',55,25);x.fillText('fire active interval',X(fs)+5,60);x.fillText('max='+f(hi),8,55);x.fillText('min='+f(lo),8,495);const c2=document.getElementById('chart2'),y=c2.getContext('2d');let bvals=[];T.forEach(r=>{{['production_spatial_baseline_median','1_2','1_3','2_5'].forEach(k=>{{let v=k==='production_spatial_baseline_median'?r.production.spatial_baseline_median:r.alternative_ring_baselines?.[k];if(v!=null)bvals.push(v)}})}});let blo=Math.min(...bvals),bhi=Math.max(...bvals),BY=v=>365-(v-blo)/(bhi-blo||1)*270,BC=(name,col)=>{{y.strokeStyle=col;y.beginPath();T.forEach((r,i)=>{{let v=name==='production_spatial_baseline_median'?r.production.spatial_baseline_median:r.alternative_ring_baselines?.[name];if(v!=null){{if(i)y.lineTo(X(new Date(r.time_bin_utc).getTime()),BY(v));else y.moveTo(X(new Date(r.time_bin_utc).getTime()),BY(v));}}}});y.stroke()}};let cvals=T.map(r=>r.inner_outer_contrast).filter(v=>v!=null),clo=Math.min(...cvals,0),chi=Math.max(...cvals,1),CY=v=>365-(v-clo)/(chi-clo||1)*270;const CC=(col)=>{{y.strokeStyle=col;y.setLineDash([6,4]);y.beginPath();T.forEach((r,i)=>{{let v=r.inner_outer_contrast;if(v!=null){{if(i)y.lineTo(X(new Date(r.time_bin_utc).getTime()),CY(v));else y.moveTo(X(new Date(r.time_bin_utc).getTime()),CY(v));}}}});y.stroke();y.setLineDash([])}};y.clearRect(0,0,c2.width,c2.height);y.fillStyle='#fff4d6';y.fillRect(X(fs),35,X(fe)-X(fs),330);BC('production_spatial_baseline_median','#111827');BC('1_2','#7c3aed');BC('1_3','#059669');BC('2_5','#0891b2');CC('#d92d20');y.fillStyle='#17202a';y.fillText('baselines: production 0–1/1.5 km black; rings 1–2 purple, 1–3 green, 2–5 teal; contrast dashed red (right scale)',55,20);y.fillText('fire active interval',X(fs)+5,50);const rows=D.top_pairs;document.getElementById('pairs').innerHTML='<tr><th>time</th><th>station</th><th>score</th><th>temporal ex/z</th><th>prod spatial ex/z</th><th>1–3 ex/z/status</th><th>2–5 ex/z/status</th></tr>'+rows.map(r=>{{let a=r.ring_metrics;return '<tr><td>'+r.analysis_time_utc+'</td><td>'+r.station_id+'</td><td>'+f(r.anomaly_score)+'</td><td>'+f(r.temporal_excess)+' / '+f(r.temporal_z)+'</td><td>'+f(r.spatial_excess)+' / '+f(r.spatial_z)+'</td><td>'+f(a['1_3'].excess)+' / '+f(a['1_3'].z)+' / '+a['1_3'].status+'</td><td>'+f(a['2_5'].excess)+' / '+f(a['2_5'].z)+' / '+a['2_5'].status+'</td></tr>'}}).join('');</script></body></html>'''


def build_event(event: dict[str, Any], stations: list[dict[str, Any]], context_stations: list[dict[str, Any]], series: dict[str, list[BinnedObservation]], results_by_bin: dict[datetime, dict[str, dict[str, Any]]], reference_stations: list[dict[str, Any]], reference_observations: list[dict[str, Any]], config: AnomalyConfig, now: datetime) -> dict[str, Any]:
    start, end = event["start_utc"], event["end_utc"]
    lat, lon = float(event["location"]["lat"]), float(event["location"]["lon"])
    timeline = []
    cursor = floor_time(start - timedelta(minutes=60), config.bin_minutes)
    limit = end + timedelta(minutes=120)
    while cursor < limit:
        cutoff = cursor + timedelta(minutes=config.bin_minutes) - timedelta(microseconds=1)
        production = {station_id: row for station_id, row in results_by_bin.get(cursor, {}).items() if finite(row.get("spatial_median"))}
        row = fire_centered_row(event, context_stations, series, cutoff, now, config, production)
        row["time_bin_utc"] = iso_utc(cursor)
        timeline.append(row)
        cursor += timedelta(minutes=config.bin_minutes)
    baseline = add_group_excess(timeline, start, config)
    reference = reference_control(event, reference_stations, reference_observations)
    outer = outer_sensor_control(timeline, start, end)
    regional = {"behavior": combine_regional_controls(outer, reference), "outer_5km_sensors": outer, "national_reference_stations": reference}
    pair_payload, counterfactual, ring_baselines, all_ring_pairs = top_pairs(event, stations, context_stations, series, results_by_bin, config, now)
    for row in timeline:
        row["alternative_ring_baselines"] = ring_baselines.get(row["time_bin_utc"], {})
    contamination = contamination_answer(timeline, start, end)
    classification = classify_spatial(timeline, start, end, regional["behavior"])
    active_rows = [row for row in timeline if start <= datetime.fromisoformat(row["time_bin_utc"].replace("Z", "+00:00")) < end]
    contrasts = [row["inner_outer_contrast"] for row in active_rows if finite(row.get("inner_outer_contrast"))]
    peak_inner = max((row["groups"]["inner"]["median_pm25"] for row in active_rows if finite(row["groups"]["inner"].get("median_pm25"))), default=None)
    peak_outer = max((row["groups"]["outer"]["median_pm25"] for row in active_rows if finite(row["groups"]["outer"].get("median_pm25"))), default=None)
    pre = next((row for row in timeline if row["time_bin_utc"] == iso_utc(floor_time(start - timedelta(minutes=3), config.bin_minutes))), timeline[0])
    return {
        "event": {"event_id": event["event_id"], "name": event["name"], "fire_start_local": event["fire_start_local"], "fire_end_local": event["fire_end_local"], "fire_start_utc": iso_utc(start), "fire_end_utc": iso_utc(end), "location": event["location"]},
        "method": {"scope": "post-hoc multi-scale spatial baseline diagnostic; not a production algorithm change", "baseline_preserved": "production-equivalent rows are copied from the unchanged _analyse_station path and retained in production_baseline_control.rows", "production_spatial_semantics": "anomaly v1 neighbor_radius_km=1.0 with its existing max_neighbor_radius_km=1.5 fallback; no production setting was changed", "rings": {name: {"lower_km": definition[0], "upper_km": definition[1], "include_upper": definition[2], "self_excluded": True, "context_only": True, "minimum_neighbors": config.min_neighbors} for name, definition in RING_DEFINITIONS.items()}, "fire_groups": {name: {"lower_km": definition[0], "upper_km": definition[1], "include_upper": definition[2]} for name, definition in GROUP_DEFINITIONS.items()}, "time_alignment": "same production-compatible latest observation at or before each 3-minute cutoff; valid when no more than 3 minutes old", "sigma": "robust_sigma=max(1.4826*MAD, 1.5 ug/m3), reused only for diagnostic metrics", "contrast_rule": "LOCAL_CONTRAST_PRESENT when at least one active bin has inner-minus-outer >=5 ug/m3 with usable inner and >=3 usable outer sensors", "regional_rule": "both 2–5 km outer-sensor control and National Reference AQ control must have at least 3 aligned samples and >=50% deltas >=2 ug/m3 to report REGIONAL_BACKGROUND_CONTAMINATION; otherwise the control remains separate or INSUFFICIENT_DATA", "classification_note": "diagnostic classifications are restricted to LOCAL_CONTRAST_PRESENT, NO_LOCAL_CONTRAST, REGIONAL_BACKGROUND_CONTAMINATION, INSUFFICIENT_DATA", "thresholds_are_not_production": True},
        "production_baseline_control": {"scope": "all nearby production-equivalent rows during the fire interval", "rows": [{key: value for key, value in row.items() if key != "_bin"} for row in production_row_lookup(results_by_bin, {str(station["station_id"]) for station in stations if finite(station.get("lat")) and finite(station.get("lon")) and haversine_km((lon, lat), (float(station["lon"]), float(station["lat"]))) <= 5.0}, start, end, config)]},
        "ring_diagnostic_control": {"scope": "all active context target sensor/time pairs with current production fields plus 1–2, 1–3, 2–3 and 2–5 ring metrics", "rows": all_ring_pairs},
        "summary": {"pre_fire_inner_median": pre["groups"]["inner"].get("pre_fire_baseline_median"), "pre_fire_outer_median": pre["groups"]["outer"].get("pre_fire_baseline_median"), "peak_inner_median": number(peak_inner), "peak_outer_median": number(peak_outer), "max_inner_outer_contrast": number(max(contrasts) if contrasts else None), "max_middle_outer_contrast": number(max((row["middle_outer_contrast"] for row in active_rows if finite(row.get("middle_outer_contrast"))), default=None)), "production_spatial_support": counterfactual["production_spatial_supported_pair_count"], "ring_1_3_support": counterfactual["counterfactual_1_3_supported_temporal_pair_count"], "ring_2_5_support": counterfactual["counterfactual_2_5_supported_temporal_pair_count"], "regional_background_behavior": regional["behavior"], "baseline_contamination": contamination, "diagnostic_classification": classification},
        "regional_background": regional,
        "counterfactual": counterfactual,
        "baseline_values": baseline,
        "timeline": timeline,
        "top_pairs": pair_payload,
        "diagnostic_classification": classification,
        "baseline_contamination": contamination,
    }


def write_summary_html(summary: dict[str, Any], output: Path) -> None:
    body = "".join(f'<tr><td><a href="{html.escape(item["detail_html"])}">{html.escape(item["name"])}</a></td><td>{item["diagnostic_classification"]}</td><td>{item.get("pre_fire_inner_median") or "—"}</td><td>{item.get("pre_fire_outer_median") or "—"}</td><td>{item.get("peak_inner_median") or "—"}</td><td>{item.get("peak_outer_median") or "—"}</td><td>{item.get("max_inner_outer_contrast") or "—"}</td><td>{item.get("production_spatial_support")}</td><td>{item.get("ring_1_3_support")}</td><td>{item.get("ring_2_5_support")}</td><td>{item.get("regional_background_behavior")}</td><td>{item.get("baseline_contamination")}</td></tr>' for item in summary["events"])
    output.write_text(f'<!doctype html><html><head><meta charset="utf-8"><title>Multi-scale Spatial Baseline Diagnostic</title><style>body{{font:14px system-ui;margin:24px;color:#17202a}}table{{border-collapse:collapse;width:100%;font-size:12px}}th,td{{border:1px solid #d5d8dc;padding:6px;text-align:left}}th{{background:#f4f6f7}}.note{{color:#5d6d7e}}</style></head><body><h1>Multi-scale Spatial Baseline Diagnostic</h1><p class="note">Post-hoc diagnostic only. Blind replay is preserved. Production detector, thresholds, clustering, recorders, wind, backtrace and evidence fusion were not modified.</p><table><tr><th>event</th><th>classification</th><th>pre inner</th><th>pre outer</th><th>peak inner</th><th>peak outer</th><th>max contrast</th><th>prod support</th><th>1–3 support</th><th>2–5 support</th><th>regional behavior</th><th>baseline contamination</th></tr>{body}</table><pre>{html.escape(json.dumps(summary["method"], ensure_ascii=False, indent=2))}</pre></body></html>', encoding="utf-8")


def write_csv(summary: dict[str, Any], output: Path) -> None:
    fields = ["event_id", "name", "fire_start_utc", "fire_end_utc", "pre_fire_inner_median", "pre_fire_outer_median", "peak_inner_median", "peak_outer_median", "max_inner_outer_contrast", "max_middle_outer_contrast", "production_spatial_support", "ring_1_3_support", "ring_2_5_support", "regional_background_behavior", "baseline_contamination", "diagnostic_classification", "detail_html"]
    with output.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows({field: item.get(field) for field in fields} for item in summary["events"])


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--events", type=Path, default=DEFAULT_EVENTS)
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE)
    parser.add_argument("--reference-database", type=Path, default=DEFAULT_REFERENCE_DATABASE)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--replay-report", type=Path, default=DEFAULT_REPLAY)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    events = load_events(args.events)
    start = min(item["start_utc"] for item in events) - timedelta(hours=2)
    end = max(item["end_utc"] for item in events) + timedelta(minutes=120)
    stations, observations = read_pm_snapshot(args.database, start, end)
    config = AnomalyConfig()
    series: dict[str, list[BinnedObservation]] = {}
    for item in _smooth_series(observations, config):
        series.setdefault(item.station_id, []).append(item)
    pilot = load_pilot_config(args.config)
    context_stations = [station for station in stations if bbox_contains(station.get("lat"), station.get("lon"), pilot["context_bbox"])]
    all_context = {str(station["station_id"]): station for station in context_stations}
    results_by_bin: dict[datetime, dict[str, dict[str, Any]]] = {}
    replay_start = min(item["start_utc"] for item in events) - timedelta(minutes=60)
    replay_end = max(item["end_utc"] for item in events) + timedelta(minutes=120)
    cursor = floor_time(replay_start, config.bin_minutes)
    while cursor <= floor_time(replay_end, config.bin_minutes):
        cutoff = cursor + timedelta(minutes=config.bin_minutes) - timedelta(microseconds=1)
        results_by_bin[cursor] = {}
        for station in sorted(context_stations, key=lambda item: str(item["station_id"])):
            row, _ = _analyse_station(station, series, all_context, cutoff, datetime.now(UTC), config)
            results_by_bin[cursor][str(row["station_id"])] = row
        cursor += timedelta(minutes=config.bin_minutes)
    reference_stations, reference_observations = read_reference_snapshot(args.reference_database)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    reports = []
    method_payload: dict[str, Any] = {}
    now = datetime.now(UTC)
    for event in events:
        report = build_event(event, stations, context_stations, series, results_by_bin, reference_stations, reference_observations, config, now)
        method_payload = report["method"]
        detail_name = f'{event["event_id"]}_spatial.html'
        (args.output_dir / detail_name).write_text(html_detail(report), encoding="utf-8")
        item = {"event_id": event["event_id"], "name": event["name"], "fire_start_utc": report["event"]["fire_start_utc"], "fire_end_utc": report["event"]["fire_end_utc"], "detail_html": detail_name} | report["summary"]
        reports.append(item)
    summary = {"schema_version": 1, "generated_at_utc": iso_utc(now), "blind_replay_preserved": True, "blind_replay_report": str(args.replay_report), "known_events_config": str(args.events), "production_detector_modified": False, "scope": "post-hoc diagnostic only", "events": reports, "method": method_payload}
    (args.output_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    write_csv(summary, args.output_dir / "summary.csv")
    write_summary_html(summary, args.output_dir / "multi_scale_spatial_baseline.html")
    print("AirTrace Multi-scale Spatial Baseline Diagnostic")
    for item in reports:
        print(f'{item["name"]}: {item["diagnostic_classification"]} | pre inner/outer={item["pre_fire_inner_median"]}/{item["pre_fire_outer_median"]} | peak inner/outer={item["peak_inner_median"]}/{item["peak_outer_median"]} | max contrast={item["max_inner_outer_contrast"]} | production/1-3/2-5 support={item["production_spatial_support"]}/{item["ring_1_3_support"]}/{item["ring_2_5_support"]} | background={item["regional_background_behavior"]} | contaminated={item["baseline_contamination"]}')
    print(f'JSON: {args.output_dir / "summary.json"}')
    print(f'CSV: {args.output_dir / "summary.csv"}')
    print(f'HTML: {args.output_dir / "multi_scale_spatial_baseline.html"}')
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
