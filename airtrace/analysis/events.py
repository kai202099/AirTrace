"""Explainable two-stage PM2.5 spatiotemporal event clustering.

Stage A clusters anomaly/supporting sensors within each 3-minute bin.  Stage B
links those spatial clusters through time.  This module deliberately stops at
sensor-observed event evidence; it does not infer a source, plume physics, or
LOCAL/REGIONAL classification.
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

from airtrace.analysis.anomaly import (
    AnomalyConfig,
    DetectionResult,
    detect_anomalies_range,
    haversine_km,
    iso_utc,
    load_pilot_config,
    parse_iso_utc,
    latest_observation_time,
    CONTEXT_DIAGNOSTIC_REPLAY,
    NOT_PRODUCTION_EVENT_DETECTION,
    validate_analysis_zone,
)


UTC = timezone.utc


@dataclass(frozen=True)
class EventConfig:
    """Centralized v1 spatial, support, linking, and lifecycle heuristics."""

    bin_minutes: int = 3
    spatial_eps_km: float = 0.75
    spatial_min_samples: int = 2
    support_score_threshold: float = 0.35
    support_temporal_excess_threshold: float = 3.0
    support_spatial_excess_threshold: float = 3.0
    link_centroid_km: float = 1.0
    link_member_km: float = 0.75
    max_gap_bins: int = 1
    movement_floor_km: float = 0.1
    isolated_seed_score_threshold: float = 8.0
    strong_cluster_score_cap: float = 0.30


def _finite(value: Any) -> bool:
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


def _number(value: Any, digits: int = 6) -> float | None:
    return None if not _finite(value) else round(float(value), digits)


def _row_usable(row: dict[str, Any]) -> bool:
    return (
        _finite(row.get("lat")) and _finite(row.get("lon"))
        and _finite(row.get("anomaly_score"))
        and row.get("temporal_status") == "sufficient"
        and row.get("spatial_status") == "sufficient"
        and "stale_current_observation" not in str(row.get("quality_flags") or "")
    )


def is_supporting_sensor(row: dict[str, Any], config: EventConfig = EventConfig()) -> bool:
    """Return whether a non-candidate row is eligible as v1 spatial support."""

    if not _row_usable(row) or bool(row.get("is_candidate")):
        return False
    return (
        float(row["anomaly_score"]) >= config.support_score_threshold
        and _finite(row.get("temporal_excess"))
        and float(row["temporal_excess"]) >= config.support_temporal_excess_threshold
        and _finite(row.get("spatial_excess"))
        and float(row["spatial_excess"]) >= config.support_spatial_excess_threshold
    )


def _point_distance(first: dict[str, Any], second: dict[str, Any]) -> float:
    return haversine_km(
        (float(first["lon"]), float(first["lat"])),
        (float(second["lon"]), float(second["lat"])),
    )


def _weighted_centroid(members: list[dict[str, Any]]) -> tuple[float, float]:
    weights = [max(float(member.get("anomaly_score") or 0.0), 0.001) for member in members]
    total = sum(weights)
    return (
        sum(float(member["lat"]) * weight for member, weight in zip(members, weights)) / total,
        sum(float(member["lon"]) * weight for member, weight in zip(members, weights)) / total,
    )


def _cluster_strength(members: list[dict[str, Any]], seed_count: int, config: EventConfig) -> float:
    scores = [float(member["anomaly_score"]) for member in members if _finite(member.get("anomaly_score"))]
    temporal = [float(member["temporal_excess"]) for member in members if _finite(member.get("temporal_excess"))]
    spatial = [float(member["spatial_excess"]) for member in members if _finite(member.get("spatial_excess"))]
    raw = (
        0.45 * min(seed_count / 3.0, 1.0)
        + 0.20 * min(len(members) / 5.0, 1.0)
        + 0.20 * min(max(scores, default=0.0) / 10.0, 1.0)
        + 0.075 * min(max(temporal, default=0.0) / 10.0, 1.0)
        + 0.075 * min(max(spatial, default=0.0) / 10.0, 1.0)
    )
    if len(members) == 1 and seed_count == 1:
        raw = min(raw, config.strong_cluster_score_cap)
    return round(min(max(raw, 0.0), 1.0), 6)


def spatial_clusters_for_bin(
    rows: Iterable[dict[str, Any]],
    time_bin: datetime,
    config: EventConfig = EventConfig(),
) -> list[dict[str, Any]]:
    """Cluster seed and support rows in one bin with deterministic DBSCAN."""

    candidates: dict[str, dict[str, Any]] = {}
    for source in rows:
        row = dict(source)
        station_id = str(row.get("station_id"))
        if station_id == "None" or station_id in candidates or not _row_usable(row):
            continue
        role = "seed" if bool(row.get("is_candidate")) else "support"
        if role == "support" and not is_supporting_sensor(row, config):
            continue
        row["station_id"] = station_id
        row["role"] = role
        candidates[station_id] = row
    points = [candidates[key] for key in sorted(candidates)]
    if not points:
        return []

    adjacency: list[set[int]] = [set() for _ in points]
    for left in range(len(points)):
        for right in range(left + 1, len(points)):
            if _point_distance(points[left], points[right]) <= config.spatial_eps_km:
                adjacency[left].add(right)
                adjacency[right].add(left)
    core = {index for index, neighbors in enumerate(adjacency) if len(neighbors) + 1 >= config.spatial_min_samples}
    components: list[list[int]] = []
    remaining = set(core)
    while remaining:
        root = min(remaining)
        remaining.remove(root)
        component = [root]
        queue = [root]
        while queue:
            current = queue.pop(0)
            for neighbor in sorted(adjacency[current] & remaining):
                remaining.remove(neighbor)
                component.append(neighbor)
                queue.append(neighbor)
        components.append(sorted(component))

    groups: list[list[int]] = []
    for component in components:
        members = set(component)
        border = set().union(*(adjacency[index] for index in component)) if component else set()
        members.update(border)
        groups.append(sorted(members))

    # Preserve a candidate singleton as low-confidence diagnostic evidence.
    assigned = set(index for group in groups for index in group)
    for index, point in enumerate(points):
        if index not in assigned and point["role"] == "seed":
            groups.append([index])
    groups.sort(key=lambda group: tuple(points[index]["station_id"] for index in group))

    output: list[dict[str, Any]] = []
    time_bin = time_bin.astimezone(UTC)
    for cluster_number, group in enumerate(groups, start=1):
        members = sorted((points[index] for index in group), key=lambda row: row["station_id"])
        seed_count = sum(member["role"] == "seed" for member in members)
        support_count = sum(member["role"] == "support" for member in members)
        centroid = (
            sum(float(member["lat"]) for member in members) / len(members),
            sum(float(member["lon"]) for member in members) / len(members),
        )
        weighted = _weighted_centroid(members)
        spread = max(
            haversine_km((float(member["lon"]), float(member["lat"])), (centroid[1], centroid[0]))
            for member in members
        )
        peak_pm25_values = [member.get("smoothed_pm25", member.get("raw_pm25")) for member in members if _finite(member.get("smoothed_pm25", member.get("raw_pm25")))]
        scores = [float(member["anomaly_score"]) for member in members if _finite(member.get("anomaly_score"))]
        temporal = [float(member["temporal_excess"]) for member in members if _finite(member.get("temporal_excess"))]
        spatial = [float(member["spatial_excess"]) for member in members if _finite(member.get("spatial_excess"))]
        strong_single = len(members) == 1 and seed_count == 1 and max(scores, default=0.0) >= config.isolated_seed_score_threshold
        cluster_id = f"{iso_utc(time_bin).replace('-', '').replace(':', '')}-c{cluster_number:02d}"
        output.append({
            "cluster_id_in_bin": cluster_id,
            "time_bin": iso_utc(time_bin),
            "members": members,
            "member_sensors": [member["station_id"] for member in members],
            "seed_count": seed_count,
            "support_count": support_count,
            "member_count": len(members),
            "centroid": {"lat": _number(centroid[0]), "lon": _number(centroid[1])},
            "weighted_centroid": {"lat": _number(weighted[0]), "lon": _number(weighted[1])},
            "cluster_spread_km": _number(spread),
            "max_anomaly_score": _number(max(scores) if scores else None),
            "median_anomaly_score": _number(median(scores) if scores else None),
            "mean_spatial_excess": _number(sum(spatial) / len(spatial) if spatial else None),
            "mean_temporal_excess": _number(sum(temporal) / len(temporal) if temporal else None),
            "peak_pm25": _number(max(peak_pm25_values) if peak_pm25_values else None),
            "median_pm25": _number(median(peak_pm25_values) if peak_pm25_values else None),
            "cluster_strength": _cluster_strength(members, seed_count, config),
            "event_eligible": seed_count > 0,
            "isolated_seed_cluster": len(members) == 1 and seed_count == 1,
            "very_strong_isolated_seed": strong_single,
        })
    return output


def _bearing_deg(first: dict[str, float], second: dict[str, float]) -> float:
    lat1, lat2 = math.radians(first["lat"]), math.radians(second["lat"])
    dlon = math.radians(second["lon"] - first["lon"])
    y = math.sin(dlon) * math.cos(lat2)
    x = math.cos(lat1) * math.sin(lat2) - math.sin(lat1) * math.cos(lat2) * math.cos(dlon)
    return round((math.degrees(math.atan2(y, x)) + 360.0) % 360.0, 3)


def _cluster_linkable(previous: dict[str, Any], current: dict[str, Any], config: EventConfig) -> tuple[bool, int, float]:
    previous_ids = set(previous["member_sensors"])
    current_ids = set(current["member_sensors"])
    shared = len(previous_ids & current_ids)
    centroid_distance = haversine_km(
        (previous["centroid"]["lon"], previous["centroid"]["lat"]),
        (current["centroid"]["lon"], current["centroid"]["lat"]),
    )
    member_distance = min(
        (haversine_km((left["lon"], left["lat"]), (right["lon"], right["lat"]))
         for left in previous["members"] for right in current["members"]),
        default=float("inf"),
    )
    linked = shared > 0 or centroid_distance <= config.link_centroid_km or member_distance <= config.link_member_km
    return linked, shared, centroid_distance


def _event_sort_key(event: dict[str, Any]) -> tuple[str, str]:
    return (str(event["start_time_utc"]), str(event["event_id"]))


def _new_event(event_id: str, cluster: dict[str, Any], split_from: str | None = None) -> dict[str, Any]:
    time_bin = cluster["time_bin"]
    centroid = dict(cluster["centroid"])
    return {
        "event_id": event_id,
        "start_time_utc": time_bin,
        "last_seen_time_utc": time_bin,
        "duration_minutes": 3,
        "bins_seen": 1,
        "active_bin_count": 1,
        "gap_bins": 0,
        "unique_sensor_count": cluster["member_count"],
        "peak_member_count": cluster["member_count"],
        "peak_seed_count": cluster["seed_count"],
        "centroid_path": [{
            "time_bin": time_bin,
            "centroid": centroid,
            "cluster_id_in_bin": cluster["cluster_id_in_bin"],
            "member_count": cluster["member_count"],
            "seed_count": cluster["seed_count"],
            "peak_pm25": cluster["peak_pm25"],
            "max_anomaly_score": cluster["max_anomaly_score"],
            "displacement_km": None,
            "bearing_deg": None,
            "speed_kmh": None,
        }],
        "initial_centroid": centroid,
        "latest_centroid": centroid,
        "max_anomaly_score": cluster["max_anomaly_score"],
        "peak_pm25": cluster["peak_pm25"],
        "max_temporal_excess": cluster["mean_temporal_excess"],
        "max_spatial_excess": cluster["mean_spatial_excess"],
        "event_strength": cluster["cluster_strength"],
        "event_status": "emerging",
        "lifecycle_reason": "first_seed_cluster",
        "merged_into": None,
        "split_from": split_from,
        "cluster_ids": [cluster["cluster_id_in_bin"]],
        "sensor_ids": sorted(cluster["member_sensors"]),
        "isolated": bool(cluster["isolated_seed_cluster"]),
    }


def _update_event(event: dict[str, Any], cluster: dict[str, Any], config: EventConfig) -> None:
    previous = event["centroid_path"][-1]
    previous_time = parse_iso_utc(previous["time_bin"])
    current_time = parse_iso_utc(cluster["time_bin"])
    delta_minutes = max((current_time - previous_time).total_seconds() / 60.0, config.bin_minutes)
    displacement = haversine_km(
        (previous["centroid"]["lon"], previous["centroid"]["lat"]),
        (cluster["centroid"]["lon"], cluster["centroid"]["lat"]),
    )
    if displacement < config.movement_floor_km:
        displacement_value, bearing, speed = 0.0, None, 0.0
    else:
        displacement_value = round(displacement, 6)
        bearing = _bearing_deg(previous["centroid"], cluster["centroid"])
        speed = round(displacement / (delta_minutes / 60.0), 6)
    event["centroid_path"].append({
        "time_bin": cluster["time_bin"],
        "centroid": dict(cluster["centroid"]),
        "cluster_id_in_bin": cluster["cluster_id_in_bin"],
        "member_count": cluster["member_count"],
        "seed_count": cluster["seed_count"],
        "peak_pm25": cluster["peak_pm25"],
        "max_anomaly_score": cluster["max_anomaly_score"],
        "displacement_km": displacement_value,
        "bearing_deg": bearing,
        "speed_kmh": speed,
    })
    event["last_seen_time_utc"] = cluster["time_bin"]
    event["duration_minutes"] = round((current_time - parse_iso_utc(event["start_time_utc"])).total_seconds() / 60.0 + config.bin_minutes, 6)
    event["bins_seen"] += 1
    event["active_bin_count"] += 1
    event["gap_bins"] += max(0, round(delta_minutes / config.bin_minutes) - 1)
    event["unique_sensor_count"] = len(set(event["sensor_ids"]) | set(cluster["member_sensors"]))
    event["sensor_ids"] = sorted(set(event["sensor_ids"]) | set(cluster["member_sensors"]))
    event["peak_member_count"] = max(event["peak_member_count"], cluster["member_count"])
    event["peak_seed_count"] = max(event["peak_seed_count"], cluster["seed_count"])
    event["latest_centroid"] = dict(cluster["centroid"])
    event["max_anomaly_score"] = _number(max(filter(_finite, [event["max_anomaly_score"], cluster["max_anomaly_score"]]), default=None))
    event["peak_pm25"] = _number(max(filter(_finite, [event["peak_pm25"], cluster["peak_pm25"]]), default=None))
    event["max_temporal_excess"] = _number(max(filter(_finite, [event["max_temporal_excess"], cluster["mean_temporal_excess"]]), default=None))
    event["max_spatial_excess"] = _number(max(filter(_finite, [event["max_spatial_excess"], cluster["mean_spatial_excess"]]), default=None))
    event["event_strength"] = _number(max(float(event["event_strength"]), float(cluster["cluster_strength"])))
    event["cluster_ids"].append(cluster["cluster_id_in_bin"])
    event["isolated"] = event["isolated"] and bool(cluster["isolated_seed_cluster"])


def _finalize_event(event: dict[str, Any], final_bin: datetime, config: EventConfig) -> None:
    if event["merged_into"]:
        event["event_status"] = "ended"
        event.pop("sensor_ids", None)
        return
    if event["isolated"]:
        event["event_status"] = "isolated"
    elif event["event_strength"] < 0.35:
        event["event_status"] = "weak"
    elif event["bins_seen"] == 1:
        event["event_status"] = "emerging" if parse_iso_utc(event["last_seen_time_utc"]) == final_bin else "transient"
    elif parse_iso_utc(event["last_seen_time_utc"]) == final_bin:
        event["event_status"] = "active"
    else:
        event["event_status"] = "ended"
    # Keep a concise, explicit event object while retaining the path and IDs.
    event.pop("sensor_ids", None)


def cluster_event_results(
    results: list[DetectionResult],
    config: EventConfig = EventConfig(),
) -> dict[str, Any]:
    """Build spatial diagnostics and deterministic event lifecycles."""

    bins: list[dict[str, Any]] = []
    for result in results:
        time_bin = parse_iso_utc(result.payload["analysis_bin_start_utc"])
        clusters = spatial_clusters_for_bin(result.rows, time_bin, config)
        bins.append({"time_bin": iso_utc(time_bin), "clusters": clusters})
    bins.sort(key=lambda item: item["time_bin"])
    events: list[dict[str, Any]] = []
    event_sequence = 1
    previous_by_event: dict[str, dict[str, Any]] = {}
    assigned_event_ids: dict[str, str] = {}
    for bin_index, bin_data in enumerate(bins):
        eligible_clusters = [cluster for cluster in bin_data["clusters"] if cluster["event_eligible"]]
        eligible_clusters.sort(key=lambda cluster: cluster["cluster_id_in_bin"])
        link_candidates: dict[str, list[str]] = {}
        for cluster in eligible_clusters:
            candidates: list[str] = []
            for event in sorted(events, key=_event_sort_key):
                if event["merged_into"] or not event["cluster_ids"]:
                    continue
                previous_cluster = previous_by_event.get(event["event_id"])
                if previous_cluster is None:
                    continue
                previous_index = int(event.get("_last_bin_index", -999999))
                if bin_index - previous_index > config.max_gap_bins + 1:
                    continue
                linked, _, _ = _cluster_linkable(previous_cluster, cluster, config)
                if linked:
                    candidates.append(event["event_id"])
            link_candidates[cluster["cluster_id_in_bin"]] = candidates

        # A split keeps the best-overlap/proximity continuation of each old event.
        split_from: dict[str, str] = {}
        for event_id in sorted({event_id for candidates in link_candidates.values() for event_id in candidates}):
            cluster_ids = [cluster_id for cluster_id, candidates in link_candidates.items() if event_id in candidates]
            if len(cluster_ids) <= 1:
                continue
            event = next(event for event in events if event["event_id"] == event_id)
            previous_cluster = previous_by_event[event_id]
            scored = []
            for cluster_id in cluster_ids:
                cluster = next(cluster for cluster in eligible_clusters if cluster["cluster_id_in_bin"] == cluster_id)
                _, shared, distance = _cluster_linkable(previous_cluster, cluster, config)
                scored.append((shared, -distance, cluster["cluster_strength"], cluster_id))
            scored.sort(key=lambda item: (-item[0], -item[1], -item[2], item[3]))
            winner = scored[0][3]
            for cluster_id in cluster_ids:
                if cluster_id != winner:
                    link_candidates[cluster_id].remove(event_id)
                    split_from[cluster_id] = event_id

        # One current cluster can merge several old events; earliest event wins.
        for cluster in eligible_clusters:
            cluster_id = cluster["cluster_id_in_bin"]
            candidates = [event_id for event_id in link_candidates[cluster_id]]
            if candidates:
                primary = min((event for event in events if event["event_id"] in candidates), key=_event_sort_key)
                for event in events:
                    if event["event_id"] in candidates and event["event_id"] != primary["event_id"]:
                        event["merged_into"] = primary["event_id"]
                        event["event_status"] = "ended"
                        event["lifecycle_reason"] = "merged_into_primary"
                link_candidates[cluster_id] = [primary["event_id"]]

            if link_candidates[cluster_id]:
                event_id = link_candidates[cluster_id][0]
                event = next(event for event in events if event["event_id"] == event_id)
                _update_event(event, cluster, config)
            else:
                event_id = f"evt-{event_sequence:04d}"
                event_sequence += 1
                event = _new_event(event_id, cluster, split_from.get(cluster_id))
                events.append(event)
            event["_last_bin_index"] = bin_index
            previous_by_event[event["event_id"]] = cluster
            assigned_event_ids[cluster_id] = event["event_id"]
            cluster["event_id"] = event["event_id"]

        # Support-only clusters are retained as diagnostics, never events.
        for cluster in bin_data["clusters"]:
            cluster.setdefault("event_id", None)

    final_bin = parse_iso_utc(bins[-1]["time_bin"]) if bins else datetime.min.replace(tzinfo=UTC)
    for event in events:
        _finalize_event(event, final_bin, config)
        event.pop("_last_bin_index", None)
    events.sort(key=_event_sort_key)
    for index, event in enumerate(events, start=1):
        # IDs are already deterministic, but event output order is part of the contract.
        event["event_order"] = index

    membership: list[dict[str, Any]] = []
    for bin_data in bins:
        for cluster in bin_data["clusters"]:
            event_id = cluster.get("event_id")
            if not event_id:
                continue
            for member in cluster["members"]:
                membership.append({
                    "event_id": event_id,
                    "time_bin": cluster["time_bin"],
                    "station_id": member["station_id"],
                    "role": member["role"],
                    "anomaly_score": _number(member.get("anomaly_score")),
                    "pm25": _number(member.get("smoothed_pm25", member.get("raw_pm25"))),
                    "lat": _number(member.get("lat")),
                    "lon": _number(member.get("lon")),
                })
    clusters_json = []
    for bin_data in bins:
        for cluster in bin_data["clusters"]:
            item = {key: value for key, value in cluster.items() if key != "members"}
            item["members"] = [{key: value for key, value in member.items() if key in {
                "station_id", "station_name", "lat", "lon", "role", "anomaly_score", "raw_pm25", "smoothed_pm25",
                "temporal_excess", "spatial_excess",
            }} for member in cluster["members"]]
            clusters_json.append(item)
    context_sensors = results[-1].context_sensors if results else []
    return {
        "events": events,
        "clusters": clusters_json,
        "bin_diagnostics": bins,
        "membership": membership,
        "context_sensors": context_sensors,
    }


def build_event_payload(
    results: list[DetectionResult],
    config_path: Path,
    event_config: EventConfig = EventConfig(),
    selected_start: datetime | None = None,
    selected_end: datetime | None = None,
    analysis_zone: str = "core",
) -> dict[str, Any]:
    if not results:
        raise ValueError("no analysis bins available")
    analysis_zone = validate_analysis_zone(analysis_zone)
    clustered = cluster_event_results(results, event_config)
    pilot = load_pilot_config(config_path)
    first = parse_iso_utc(results[0].payload["analysis_bin_start_utc"])
    last = parse_iso_utc(results[-1].payload["analysis_bin_start_utc"])
    window_start = selected_start.astimezone(UTC) if selected_start else first
    window_end = selected_end.astimezone(UTC) if selected_end else last + timedelta(minutes=event_config.bin_minutes)
    seed_count = sum(1 for result in results for row in result.rows if bool(row.get("is_candidate")))
    transient = sum(event["event_status"] == "transient" for event in clustered["events"])
    isolated = sum(event["event_status"] == "isolated" for event in clustered["events"])
    payload = {
        "schema_version": 1,
        "detector": "AirTrace PM2.5 Spatiotemporal Event Clustering v1",
        "generated_at_utc": iso_utc(datetime.now(UTC)),
        "analysis_window": {"start_time_utc": iso_utc(window_start), "end_time_utc": iso_utc(window_end)},
        "analysis_zone": analysis_zone,
        "scope": "sensor-observed event evidence only; no wind, source attribution, facility matching, or LOCAL/REGIONAL classification",
        "config": {
            "event": asdict(event_config),
            "anomaly_v1": results[-1].payload.get("config", {}),
            "heuristic_label": "event support/link/strength values are v1 heuristics; anomaly v1 thresholds are unchanged",
        },
        "zones": {"context_bbox": pilot["context_bbox"], "core_bbox": pilot["core_bbox"], "geometry": "WGS84; distances use haversine km"},
        "summary": {
            "bins_analyzed": len(results),
            "seed_count": seed_count,
            "event_count": len(clustered["events"]),
            "transient_count": transient,
            "isolated_count": isolated,
            "support_only_cluster_count": sum(not cluster["event_eligible"] for cluster in clustered["clusters"]),
            "context_sensor_count": len(results[-1].context_sensors),
            "usable_sensor_count": sum(sensor.get("status") == "usable" for sensor in results[-1].context_sensors) if analysis_zone == "context" else sum(row.get("raw_pm25") is not None and "stale_current_observation" not in str(row.get("quality_flags") or "") and "suspicious_future_timestamp" not in str(row.get("quality_flags") or "") for row in results[-1].rows),
        },
        "movement_note": "displacement, bearing, and speed describe observed anomaly centroid movement only; they are not physical plume velocity",
        "events": clustered["events"],
        "clusters": clustered["clusters"],
        "bin_diagnostics": [{"time_bin": item["time_bin"], "cluster_ids": [cluster["cluster_id_in_bin"] for cluster in item["clusters"]]} for item in clustered["bin_diagnostics"]],
        "membership": clustered["membership"],
        "context_sensors": clustered["context_sensors"],
    }
    if analysis_zone == "context":
        payload["mode_notice"] = [CONTEXT_DIAGNOSTIC_REPLAY, NOT_PRODUCTION_EVENT_DETECTION]
    return payload


def write_event_json(payload: dict[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def write_event_csv(payload: dict[str, Any], path: Path) -> None:
    fields = ["event_id", "start", "end", "duration", "unique_sensors", "max_members", "peak_pm25", "max_score", "event_strength", "status", "centroid_lat", "centroid_lon"]
    is_context = payload.get("analysis_zone", "core") == "context"
    if is_context:
        fields = ["analysis_zone", "mode_notice"] + fields
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        if is_context:
            handle.write(f"# {CONTEXT_DIAGNOSTIC_REPLAY}; {NOT_PRODUCTION_EVENT_DETECTION}\n")
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for event in payload["events"]:
            row = {
                "event_id": event["event_id"], "start": event["start_time_utc"], "end": event["last_seen_time_utc"],
                "duration": event["duration_minutes"], "unique_sensors": event["unique_sensor_count"],
                "max_members": event["peak_member_count"], "peak_pm25": event["peak_pm25"],
                "max_score": event["max_anomaly_score"], "event_strength": event["event_strength"],
                "status": event["event_status"], "centroid_lat": event["latest_centroid"]["lat"], "centroid_lon": event["latest_centroid"]["lon"],
            }
            if is_context:
                row = {"analysis_zone": "context", "mode_notice": f"{CONTEXT_DIAGNOSTIC_REPLAY} / {NOT_PRODUCTION_EVENT_DETECTION}", **row}
            writer.writerow(row)


def write_membership_csv(payload: dict[str, Any], path: Path) -> None:
    fields = ["event_id", "time_bin", "station_id", "role", "anomaly_score", "pm25", "lat", "lon"]
    is_context = payload.get("analysis_zone", "core") == "context"
    if is_context:
        fields = ["analysis_zone", "mode_notice"] + fields
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        if is_context:
            handle.write(f"# {CONTEXT_DIAGNOSTIC_REPLAY}; {NOT_PRODUCTION_EVENT_DETECTION}\n")
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        if is_context:
            marker = f"{CONTEXT_DIAGNOSTIC_REPLAY} / {NOT_PRODUCTION_EVENT_DETECTION}"
            writer.writerows([{**row, "analysis_zone": "context", "mode_notice": marker} for row in payload["membership"]])
        else:
            writer.writerows(payload["membership"])


def _html_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")


def write_event_map(payload: dict[str, Any], path: Path) -> None:
    zones, events = payload["zones"], payload["events"]
    context = payload["context_sensors"]
    center = [(zones["context_bbox"]["south"] + zones["context_bbox"]["north"]) / 2, (zones["context_bbox"]["west"] + zones["context_bbox"]["east"]) / 2]
    empty_notice = "<div class=\"empty\">No events in selected window</div>" if not events else ""
    mode_notice = "" if payload.get("analysis_zone", "core") == "core" else f"<div class=\"note\"><strong>{CONTEXT_DIAGNOSTIC_REPLAY}</strong><br><strong>{NOT_PRODUCTION_EVENT_DETECTION}</strong></div>"
    html = f"""<!doctype html><html lang="zh-Hant"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>AirTrace Event Diagnostics</title><link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"><style>body{{margin:0;font-family:system-ui,-apple-system,"Segoe UI",sans-serif;color:#172033}}header{{padding:14px 18px;border-bottom:1px solid #d9e0ea;background:#fff}}h1{{margin:0 0 5px;font-size:21px}}.note{{color:#586579;font-size:13px}}.empty{{margin-top:9px;padding:8px 10px;background:#f3f6fa;border-left:3px solid #94a3b8;color:#475569;font-size:13px}}#map{{height:calc(100vh - 105px);min-height:560px}}.popup-table td{{padding:2px 6px 2px 0;vertical-align:top}}.popup-table td:first-child{{color:#586579;white-space:nowrap}}</style></head><body><header><h1>AirTrace Spatiotemporal Event Diagnostics v1</h1><div class="note">Window: {payload["analysis_window"]["start_time_utc"]} → {payload["analysis_window"]["end_time_utc"]} · zone: {payload.get("analysis_zone", "core")} · {payload["summary"]["event_count"]} events · observed centroid movement is not plume velocity.</div>{mode_notice}{empty_notice}</header><div id="map"></div><script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script><script>
const contextBbox={_html_json(zones["context_bbox"])},coreBbox={_html_json(zones["core_bbox"])},sensors={_html_json(context)},events={_html_json(events)};
const map=L.map('map',{{preferCanvas:true}}).setView({_html_json(center)},12);const osm=L.tileLayer('https://{{s}}.tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png',{{maxZoom:19,attribution:'&copy; OpenStreetMap contributors'}}).addTo(map);L.rectangle([[contextBbox.south,contextBbox.west],[contextBbox.north,contextBbox.east]],{{color:'#26364f',weight:2,fill:false,dashArray:'7 5'}}).bindPopup('<strong>Context Zone</strong>').addTo(map);L.rectangle([[coreBbox.south,coreBbox.west],[coreBbox.north,coreBbox.east]],{{color:'#8e2a86',weight:3,fillColor:'#c77dff',fillOpacity:.08,dashArray:'8 4'}}).bindPopup('<strong>Core Zone</strong>').addTo(map);
function safe(v){{return String(v??'—').replace(/[&<>"']/g,c=>({{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}}[c]))}}function n(v){{return v===null||v===undefined?'—':Number(v).toFixed(2)}}const bg=L.layerGroup().addTo(map),fg=L.layerGroup().addTo(map);sensors.forEach(s=>{{if(s.lat===null||s.lon===null)return;L.circleMarker([s.lat,s.lon],{{radius:3,color:'#94a3b8',fillColor:'#cbd5e1',fillOpacity:.35,weight:1}}).bindPopup('<strong>'+safe(s.station_id)+'</strong><br>Context sensor background<br>PM2.5: '+n(s.pm25)).addTo(bg)}});const colors=['#d92d20','#2563eb','#059669','#9333ea','#ea580c','#0891b2','#be185d'];events.forEach((e,i)=>{{const color=colors[i%colors.length],layer=L.layerGroup().addTo(fg),members={{}};e.centroid_path.forEach((p,j)=>{{const marker=L.circleMarker([p.centroid.lat,p.centroid.lon],{{radius:j===0?8:6,color,fillColor:color,fillOpacity:.75,weight:2}}).bindPopup('<strong>'+safe(e.event_id)+'</strong><table class="popup-table"><tr><td>status</td><td>'+safe(e.event_status)+'</td></tr><tr><td>time</td><td>'+safe(p.time_bin)+'</td></tr><tr><td>members</td><td>'+p.member_count+'</td></tr><tr><td>seed count</td><td>'+p.seed_count+'</td></tr><tr><td>peak PM2.5</td><td>'+n(p.peak_pm25)+'</td></tr><tr><td>score</td><td>'+n(p.max_anomaly_score)+'</td></tr></table>');marker.addTo(layer);if(j){{L.polyline([[e.centroid_path[j-1].centroid.lat,e.centroid_path[j-1].centroid.lon],[p.centroid.lat,p.centroid.lon]],{{color,weight:3,opacity:.8}}).addTo(layer)}}}});e.centroid_path.forEach(p=>{{}});const first=e.centroid_path[0],last=e.centroid_path[e.centroid_path.length-1];L.circleMarker([first.centroid.lat,first.centroid.lon],{{radius:10,color,fill:false,weight:2}}).bindTooltip(e.event_id+' start').addTo(layer);L.circleMarker([last.centroid.lat,last.centroid.lon],{{radius:10,color,fill:false,weight:2,dashArray:'3 3'}}).bindTooltip(e.event_id+' latest').addTo(layer)}});L.control.layers({{'OpenStreetMap':osm}},{{'Context sensors':bg,'Events':fg}}).addTo(map);map.fitBounds([[contextBbox.south,contextBbox.west],[contextBbox.north,contextBbox.east]],{{padding:[14,14]}});</script></body></html>"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(html, encoding="utf-8")


def write_event_timeline(payload: dict[str, Any], path: Path) -> None:
    rows = []
    for event in payload["events"]:
        rows.append(f"<section><h2>{event['event_id']} · {event['event_status']}</h2><p>{event['start_time_utc']} → {event['last_seen_time_utc']} · strength {event['event_strength']} · sensors {event['unique_sensor_count']}</p><table><tr><th>time</th><th>members</th><th>seeds</th><th>peak PM2.5</th><th>max score</th><th>movement km / bearing / kmh</th></tr>" + "".join(f"<tr><td>{point['time_bin']}</td><td>{point['member_count']}</td><td>{point['seed_count']}</td><td>{point['peak_pm25']}</td><td>{point['max_anomaly_score']}</td><td>{point['displacement_km']} / {point['bearing_deg'] or '—'} / {point['speed_kmh']}</td></tr>" for point in event["centroid_path"]) + "</table></section>")
    body = "<p>No events in selected window.</p>" if not rows else "".join(rows)
    marker = f"<p><strong>{CONTEXT_DIAGNOSTIC_REPLAY}</strong><br><strong>{NOT_PRODUCTION_EVENT_DETECTION}</strong></p>" if payload.get("analysis_zone", "core") == "context" else ""
    html = f"<!doctype html><html lang='zh-Hant'><meta charset='utf-8'><title>AirTrace Event Timeline</title><style>body{{font-family:system-ui,sans-serif;margin:24px;color:#172033}}section{{margin:0 0 28px}}table{{border-collapse:collapse;width:100%;font-size:13px}}th,td{{border:1px solid #d9e0ea;padding:6px;text-align:left}}th{{background:#f3f6fa}}h1{{font-size:22px}}</style><h1>AirTrace Event Timeline Diagnostic</h1>{marker}<p>Observed centroid movement only; not physical plume velocity.</p>{body}</html>"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(html, encoding="utf-8")


def run_event_analysis(
    database_path: Path,
    config_path: Path,
    start_time: datetime,
    end_time: datetime,
    anomaly_lookback_hours: float = 2.0,
    event_config: EventConfig = EventConfig(),
    analysis_zone: str = "core",
) -> dict[str, Any]:
    analysis_zone = validate_analysis_zone(analysis_zone)
    anomaly_config = AnomalyConfig(bin_minutes=event_config.bin_minutes)
    results = detect_anomalies_range(
        database_path, config_path, start_time, end_time,
        lookback_hours=anomaly_lookback_hours,
        config=anomaly_config,
        analysis_zone=analysis_zone,
    )
    payload = build_event_payload(results, config_path, event_config, start_time, end_time, analysis_zone)
    payload["database"] = {"path": str(database_path), "read_only": True}
    return payload


__all__ = [
    "EventConfig", "is_supporting_sensor", "spatial_clusters_for_bin", "cluster_event_results",
    "build_event_payload", "run_event_analysis", "write_event_json", "write_event_csv",
    "write_membership_csv", "write_event_map", "write_event_timeline", "latest_observation_time",
]
