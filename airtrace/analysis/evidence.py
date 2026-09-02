"""Source Evidence Fusion v1.

This is a deterministic cross-reference layer. Its scores are relative to one
trace and are not probabilities, guilt scores, violation scores, or PM2.5
emission estimates. CEMS is deliberately annotation-only.
"""

from __future__ import annotations

import csv
import html
import json
import math
from dataclasses import asdict, is_dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

from airtrace.data.cems import cems_annotations, normalize_control_id
from airtrace.data.firms import FireDetection, group_detections, parse_detection
from airtrace.data.moenv import haversine_km, parse_timestamp_utc

FACILITY_STRONG_THRESHOLD = 0.35
FIRE_STRONG_THRESHOLD = 0.35
DEFAULT_SOURCE_BUFFER_KM = 3.0
DEFAULT_LOCAL_RADIUS_M = 500.0
DEFAULT_FIRMS_WINDOW_HOURS = 12.0


def _row(value: Any) -> dict[str, Any]:
    if is_dataclass(value):
        return asdict(value)
    return dict(value) if isinstance(value, Mapping) else {}


def _finite(value: Any) -> bool:
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


def _time(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value.replace(tzinfo=value.tzinfo or timezone.utc).astimezone(timezone.utc)
    if value is None or not str(value).strip():
        return None
    return parse_timestamp_utc(value) or _iso_time(value)


def _iso_time(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed.replace(tzinfo=parsed.tzinfo or timezone.utc).astimezone(timezone.utc)
    except ValueError:
        return None


def _iso(value: datetime | None) -> str | None:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z") if value else None


def _event_time(trace: Mapping[str, Any]) -> datetime | None:
    event = trace.get("event") or {}
    for key in ("event_time_utc", "detected_at_utc", "last_seen_time_utc", "first_seen_time_utc", "time_utc"):
        value = _time(event.get(key))
        if value:
            return value
    times = [_time(seed.get("seed_time_utc")) for seed in trace.get("receptor_seeds", []) if isinstance(seed, Mapping)]
    times = [value for value in times if value]
    return min(times) if times else None


def _occupied_extent(trace: Mapping[str, Any], buffer_km: float = DEFAULT_SOURCE_BUFFER_KM) -> dict[str, float] | None:
    points = [(float(row["center_lat"]), float(row["center_lon"])) for row in trace.get("source_evidence_grid", []) if _finite(row.get("center_lat")) and _finite(row.get("center_lon"))]
    if not points:
        for region in trace.get("candidate_source_regions", []):
            centroid = region.get("centroid", {})
            if _finite(centroid.get("lat")) and _finite(centroid.get("lon")):
                points.append((float(centroid["lat"]), float(centroid["lon"])))
    if not points:
        return None
    lat_pad = buffer_km / 110.54
    lon_pad = buffer_km / (111.32 * max(math.cos(math.radians(sum(p[0] for p in points) / len(points))), 0.1))
    return {"south": min(p[0] for p in points) - lat_pad, "north": max(p[0] for p in points) + lat_pad, "west": min(p[1] for p in points) - lon_pad, "east": max(p[1] for p in points) + lon_pad}


def _in_extent(lat: float, lon: float, extent: Mapping[str, float]) -> bool:
    return extent["south"] <= lat <= extent["north"] and extent["west"] <= lon <= extent["east"]


def _surface_metrics(lat: float, lon: float, grid: Iterable[Mapping[str, Any]], radius_m: float = DEFAULT_LOCAL_RADIUS_M) -> dict[str, Any]:
    grid_index = grid if isinstance(grid, Mapping) else None
    if grid_index is not None:
        grid = [item for bucket in grid_index.values() for item in bucket]
    candidates = []
    source_cells = grid
    if grid_index is not None:
        bucket_lat, bucket_lon = int(math.floor(lat * 100)), int(math.floor(lon * 100))
        source_cells = [cell for lat_bucket in range(bucket_lat - 1, bucket_lat + 2) for lon_bucket in range(bucket_lon - 1, bucket_lon + 2) for cell in grid_index.get((lat_bucket, lon_bucket), [])]
    for cell in source_cells:
        if not (_finite(cell.get("center_lat")) and _finite(cell.get("center_lon"))):
            continue
        distance = haversine_km((lon, lat), (float(cell["center_lon"]), float(cell["center_lat"]))) * 1000
        score = float(cell.get("source_evidence_score") or cell.get("normalized_density") or 0.0)
        if math.isfinite(score):
            candidates.append((distance, score, float(cell.get("receptor_support_fraction") or 0.0), int(cell.get("receptor_support_count") or 0)))
    if not candidates:
        return {"point_score": 0.0, "local_max_score": 0.0, "local_mean_score": 0.0, "receptor_support_fraction": 0.0, "receptor_support_count": 0, "nearest_grid_distance_km": None}
    candidates.sort(key=lambda item: (item[0], -item[1]))
    point = candidates[0]
    nearby = [item for item in candidates if item[0] <= radius_m]
    if not nearby:
        return {"point_score": 0.0, "local_max_score": 0.0, "local_mean_score": 0.0, "receptor_support_fraction": 0.0, "receptor_support_count": 0, "nearest_grid_distance_km": round(point[0] / 1000, 6)}
    local_max = max(item[1] for item in nearby)
    total_weight = sum(1.0 / max(item[0], 25.0) for item in nearby)
    local_mean = sum(item[1] / max(item[0], 25.0) for item in nearby) / total_weight
    support = max(nearby, key=lambda item: (item[1], item[2]))
    return {
        "point_score": round(max(0.0, min(1.0, point[1])), 9),
        "local_max_score": round(max(0.0, min(1.0, local_max)), 9),
        "local_mean_score": round(max(0.0, min(1.0, local_mean)), 9),
        "receptor_support_fraction": round(max(item[2] for item in nearby), 9),
        "receptor_support_count": max(item[3] for item in nearby),
        "nearest_grid_distance_km": round(point[0] / 1000, 6),
    }


def _top_region_distance(lat: float, lon: float, regions: Iterable[Mapping[str, Any]]) -> float | None:
    points = []
    for region in regions:
        centroid = region.get("centroid", {})
        if _finite(centroid.get("lat")) and _finite(centroid.get("lon")):
            points.append(haversine_km((lon, lat), (float(centroid["lon"]), float(centroid["lat"]))))
    return round(min(points), 6) if points else None


def _facility_match(row: Mapping[str, Any], trace: Mapping[str, Any], grid: Mapping[Any, Any] | list[Mapping[str, Any]], extent: Mapping[str, float] | None, cems: set[str]) -> dict[str, Any] | None:
    if row.get("lat") is None or row.get("lon") is None or extent is None:
        return None
    lat, lon = float(row["lat"]), float(row["lon"])
    if not _in_extent(lat, lon, extent):
        return None
    metrics = _surface_metrics(lat, lon, grid)
    score = 0.60 * metrics["local_max_score"] + 0.25 * metrics["local_mean_score"] + 0.15 * metrics["receptor_support_fraction"]
    cems_available = normalize_control_id(row.get("ems_no")) in cems
    return {
        "entity_type": "FACILITY", "ems_no": row.get("ems_no"), "name": row.get("facility_name", ""),
        "facility_name": row.get("facility_name", ""), "industry": row.get("industry_name") or row.get("industrial_area", ""),
        "industry_id": row.get("industry_id", ""), "is_air_regulated": row.get("is_air_regulated"),
        "is_waste_regulated": row.get("is_waste_regulated"), "lat": lat, "lon": lon,
        **metrics, "distance_to_top_source_region_km": _top_region_distance(lat, lon, trace.get("candidate_source_regions", [])),
        "evidence_score": round(max(0.0, min(1.0, score)), 9), "cems_available": cems_available,
        "score_semantics": "relative evidence within this event; CEMS availability does not change this score",
    }


def _fire_time(group: Mapping[str, Any]) -> datetime | None:
    return _time(group.get("first_acquisition_time_utc") or group.get("acquisition_time_utc"))


def _fire_match(group: Mapping[str, Any], trace: Mapping[str, Any], grid: Mapping[Any, Any] | list[Mapping[str, Any]], extent: Mapping[str, float] | None, window_hours: float) -> dict[str, Any] | None:
    centroid = group.get("centroid") or {}
    if extent is None or not (_finite(centroid.get("lat")) and _finite(centroid.get("lon"))) or not _in_extent(float(centroid["lat"]), float(centroid["lon"]), extent):
        return None
    metrics = _surface_metrics(float(centroid["lat"]), float(centroid["lon"]), grid)
    event_time = _event_time(trace)
    acquired = _fire_time(group)
    offset_hours = abs((acquired - event_time).total_seconds()) / 3600 if acquired and event_time else None
    temporal = max(0.0, 1.0 - offset_hours / window_hours) if offset_hours is not None and window_hours > 0 else 0.0
    confidence = float((group.get("confidence_summary") or {}).get("max", 0.5))
    score = 0.55 * metrics["local_max_score"] + 0.20 * metrics["receptor_support_fraction"] + 0.20 * temporal + 0.05 * confidence
    return {
        "entity_type": "FIRE_HOTSPOT", "fire_group_id": group.get("fire_group_id"), "lat": float(centroid["lat"]), "lon": float(centroid["lon"]),
        "first_acquisition_time_utc": group.get("first_acquisition_time_utc"), "last_acquisition_time_utc": group.get("last_acquisition_time_utc"),
        "detection_count": group.get("detection_count", len(group.get("detections", []))), "satellites": group.get("satellites", []),
        "max_frp": group.get("max_frp"), "median_frp": group.get("median_frp"), "confidence": (group.get("confidence_summary") or {}).get("values", []),
        "temporal_offset_hours": round(offset_hours, 6) if offset_hours is not None else None, "temporal_score": round(temporal, 9),
        "spatial_evidence": metrics["local_max_score"], "receptor_support_fraction": metrics["receptor_support_fraction"],
        "evidence_score": round(max(0.0, min(1.0, score)), 9),
        "score_semantics": "relative hotspot evidence within this event; FRP is descriptive and not an emission estimate",
    }


def match_source_evidence(trace: Mapping[str, Any], facilities: Iterable[Any] = (), fires: Iterable[Any] = (), cems_context: Iterable[Any] | Mapping[str, Any] = (), *, source_buffer_km: float = DEFAULT_SOURCE_BUFFER_KM, firms_window_hours: float = DEFAULT_FIRMS_WINDOW_HOURS, max_facilities: int = 50, max_fire_groups: int = 20) -> dict[str, Any]:
    """Match one backtrace payload against facilities, fire groups, and CEMS context."""
    trace_dict = dict(trace)
    facility_rows = [_row(item) for item in facilities]
    fire_rows = [_row(item) for item in fires]
    if fire_rows and not any(row.get("fire_group_id") for row in fire_rows):
        detections = []
        for item in fires:
            if isinstance(item, FireDetection):
                detections.append(item)
            elif is_dataclass(item) and hasattr(item, "acquisition_time_utc"):
                detections.append(item)
            elif isinstance(item, Mapping):
                parsed = parse_detection(item, source=str(item.get("source") or ""))
                if parsed is not None:
                    detections.append(parsed)
        fire_rows = group_detections(detections)
    cems_rows = [_row(item) for item in (cems_context.values() if isinstance(cems_context, Mapping) else cems_context)]
    grid = [dict(item) for item in trace_dict.get("source_evidence_grid", []) if isinstance(item, Mapping)]
    grid_index: dict[tuple[int, int], list[Mapping[str, Any]]] = {}
    for cell in grid:
        if _finite(cell.get("center_lat")) and _finite(cell.get("center_lon")):
            key = (int(math.floor(float(cell["center_lat"]) * 100)), int(math.floor(float(cell["center_lon"]) * 100)))
            grid_index.setdefault(key, []).append(cell)
    extent = _occupied_extent(trace_dict, source_buffer_km)
    cems_ids = {normalize_control_id(item.get("cno") or item.get("ems_no")) for item in cems_rows if normalize_control_id(item.get("cno") or item.get("ems_no"))}
    facility_matches = [item for row in facility_rows if (item := _facility_match(row, trace_dict, grid_index, extent, cems_ids)) is not None]
    facility_matches.sort(key=lambda row: (-row["evidence_score"], row.get("ems_no") or ""))
    fire_matches = [item for row in fire_rows if (item := _fire_match(row, trace_dict, grid_index, extent, firms_window_hours)) is not None]
    fire_matches.sort(key=lambda row: (-row["evidence_score"], row.get("fire_group_id") or ""))
    for rank, item in enumerate(facility_matches, 1):
        item["rank"] = rank
    for rank, item in enumerate(fire_matches, 1):
        item["rank"] = rank
    event = trace_dict.get("event") or {}
    manual = bool(event.get("manual_diagnostic")) or "MANUAL DIAGNOSTIC" in str(trace_dict.get("manual_diagnostic_notice") or event.get("note") or "")
    no_strong_facility = not facility_matches or facility_matches[0]["evidence_score"] < FACILITY_STRONG_THRESHOLD
    no_firms = not fire_matches
    cems_join = _cems_join_summary(cems_rows, facility_rows)
    annotations = cems_annotations(cems_rows, facility_rows, _event_time(trace_dict)) if cems_rows else []
    return {
        "schema_version": 1, "status": "EVIDENCE_COMPLETE", "trace_status": trace_dict.get("status"),
        "event": event, "trace_type": "MANUAL DIAGNOSTIC — NOT DETECTED EVENT" if manual else "DETECTED EVENT TRACE",
        "candidate_source_regions": trace_dict.get("candidate_source_regions", []),
        "manual_diagnostic_notice": "MANUAL DIAGNOSTIC — NOT DETECTED EVENT" if manual else None,
        "source_evidence_extent": extent, "source_buffer_km": source_buffer_km,
        "facility_matches": facility_matches[:max_facilities], "facility_match_count_after_prefilter": len(facility_matches),
        "facility_match_status": "NO STRONG FACILITY MATCH" if no_strong_facility else "STRONGEST RELATIVE FACILITY MATCHES",
        "fire_matches": fire_matches[:max_fire_groups], "fire_group_count_after_prefilter": len(fire_matches),
        "fire_match_status": "NO FIRMS HOTSPOT DETECTED" if no_firms else "FIRMS HOTSPOT CANDIDATES",
        "cems_annotations": annotations, "cems_join_diagnostics": cems_join,
        "cems_semantics": "CEMS measures stack pollutants, not ambient PM2.5. It is supporting context only and does not increase core facility likelihood or ranking.",
        "scoring_semantics": "Facility and fire scores are deterministic 0–1 relative evidence within this trace, not probability, guilt, violation, or calibrated emission scores.",
        "firms_temporal_window_hours": firms_window_hours, "firms_temporal_window_semantics": "heuristic; satellite non-continuity means minute-exact agreement is not required",
        "limitations": ["FIRMS non-detection does not rule out small or obscured fires.", "CEMS values are stack observations and are not ambient PM2.5.", "Registered facilities and satellite hotspots are candidate evidence entities, not attribution.", "Source regions may remain UNREGISTERED / UNKNOWN SOURCE REGION."],
    }


def _cems_join_summary(cems: list[Mapping[str, Any]], facilities: list[Mapping[str, Any]]) -> dict[str, Any]:
    cems_ids = {str(row.get("cno") or "").strip() for row in cems if str(row.get("cno") or "").strip()}
    facility_ids = {str(row.get("ems_no") or "").strip() for row in facilities if str(row.get("ems_no") or "").strip()}
    exact = cems_ids & facility_ids
    norm_cems = {normalize_control_id(item) for item in cems_ids}
    norm_facilities = {normalize_control_id(item) for item in facility_ids}
    return {"cems_unique_cno_count": len(cems_ids), "facility_unique_ems_no_count": len(facility_ids), "exact_match_count": len(exact), "exact_string_match_rate": len(exact) / len(cems_ids) if cems_ids else None, "normalized_match_count": len(norm_cems & norm_facilities), "normalized_match_rate": len(norm_cems & norm_facilities) / len(norm_cems) if norm_cems else None, "unmatched_cems_cno_count": len(cems_ids - exact), "unmatched_facility_ems_no_count": len(facility_ids - exact), "join_rule": "exact then whitespace/case normalization; no fuzzy company-name matching"}


def write_facility_csv(report: Mapping[str, Any], path: Path) -> None:
    fields = ["rank", "ems_no", "name", "industry", "is_air_regulated", "lat", "lon", "point_score", "local_max_score", "local_mean_score", "receptor_support_fraction", "distance_to_top_source_region_km", "evidence_score", "cems_available"]
    _write_csv(path, fields, report.get("facility_matches", []))


def write_fire_csv(report: Mapping[str, Any], path: Path) -> None:
    fields = ["rank", "fire_group_id", "lat", "lon", "first_acquisition_time_utc", "last_acquisition_time_utc", "detection_count", "satellites", "max_frp", "confidence", "temporal_offset_hours", "spatial_evidence", "receptor_support_fraction", "evidence_score"]
    rows = []
    for row in report.get("fire_matches", []):
        copied = dict(row)
        copied["satellites"] = ",".join(copied.get("satellites", []))
        copied["confidence"] = ",".join(copied.get("confidence", []))
        rows.append(copied)
    _write_csv(path, fields, rows)


def _write_csv(path: Path, fields: list[str], rows: Iterable[Mapping[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows({field: row.get(field) for field in fields} for row in rows)


def write_evidence_json(report: Mapping[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=_json_default, allow_nan=False) + "\n", encoding="utf-8")


def _json_default(value: Any) -> str:
    if isinstance(value, datetime):
        return _iso(value) or ""
    if isinstance(value, Path):
        return str(value)
    raise TypeError(f"not JSON serializable: {type(value).__name__}")


def write_evidence_map(report: Mapping[str, Any], trace: Mapping[str, Any], path: Path, *, region: Mapping[str, Any] | None = None) -> None:
    region = region or {"context_bbox": {"south": 25.0, "north": 25.12, "west": 121.4, "east": 121.52}, "core_bbox": {"south": 25.05, "north": 25.086, "west": 121.428, "east": 121.474}}
    context = json.dumps(region["context_bbox"], separators=(",", ":"))
    core = json.dumps(region["core_bbox"], separators=(",", ":"))
    grid = json.dumps(list(trace.get("source_evidence_grid", [])), ensure_ascii=False, separators=(",", ":"))
    trajectories = json.dumps(list(trace.get("display_trajectories", [])), ensure_ascii=False, separators=(",", ":"))
    seeds = json.dumps(list(trace.get("receptor_seeds", [])), ensure_ascii=False, separators=(",", ":"))
    regions = json.dumps(list(trace.get("candidate_source_regions", [])), ensure_ascii=False, separators=(",", ":"))
    facilities = json.dumps(list(report.get("facility_matches", [])), ensure_ascii=False, separators=(",", ":"))
    fires = json.dumps(list(report.get("fire_matches", [])), ensure_ascii=False, separators=(",", ":"))
    notice = html.escape(str(report.get("trace_type") or "DETECTED EVENT TRACE"))
    page = f'''<!doctype html><html lang="zh-Hant"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>AirTrace Source Evidence Fusion v1</title><link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"><style>body{{margin:0;font-family:system-ui,sans-serif;color:#172033}}header{{padding:12px 18px;border-bottom:1px solid #d9e0ea;background:#fff}}h1{{margin:0 0 4px;font-size:20px}}.note{{color:#586579;font-size:13px}}#map{{height:calc(100vh - 92px);min-height:560px}}</style></head><body><header><h1>AirTrace Source Evidence Fusion v1</h1><div class="note">{notice} · scores are relative evidence, not probability or attribution · CEMS is supporting context only</div></header><div id="map"></div><script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script><script>const ctx={context},core={core},grid={grid},trajs={trajectories},seeds={seeds},regions={regions},facilities={facilities},fires={fires};const map=L.map('map',{{preferCanvas:true}}).setView([25.06,121.47],11);L.tileLayer('https://{{s}}.tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png',{{maxZoom:19,attribution:'&copy; OpenStreetMap contributors'}}).addTo(map);L.rectangle([[ctx.south,ctx.west],[ctx.north,ctx.east]],{{color:'#26364f',weight:2,fill:false,dashArray:'7 5'}}).bindPopup('Context Zone').addTo(map);L.rectangle([[core.south,core.west],[core.north,core.east]],{{color:'#8e2a86',weight:3,fill:false,dashArray:'8 4'}}).bindPopup('Core Zone').addTo(map);const max=Math.max(1,...grid.map(x=>x.source_evidence_score||0));grid.forEach(x=>{{const score=x.source_evidence_score||0;const color='hsl('+(230-210*score/max)+',85%,50%)';L.circle([x.center_lat,x.center_lon],{{radius:125,color,fillColor:color,fillOpacity:.2+.6*score/max,weight:0}}).bindPopup('Relative source evidence: '+score.toFixed(3)+'<br>Receptor support: '+x.receptor_support_count).addTo(map)}});trajs.forEach(t=>L.polyline((t.points||[]).map(p=>[p.lat,p.lon]),{{color:'#64748b',weight:1,opacity:.18}}).addTo(map));seeds.forEach(s=>L.circleMarker([s.lat,s.lon],{{radius:6,color:'#d97706',fillColor:'#fbbf24',fillOpacity:.9}}).bindPopup('Receptor '+s.station_id).addTo(map));regions.forEach(r=>L.circleMarker([r.centroid.lat,r.centroid.lon],{{radius:10,color:'#b91c1c',fill:false,weight:3}}).bindPopup('Candidate source region #'+r.rank+'<br>Relative peak score: '+r.peak_score.toFixed(3)).addTo(map));facilities.forEach(f=>L.circleMarker([f.lat,f.lon],{{radius:Math.max(5,10*f.evidence_score),color:'#2563eb',fillColor:'#60a5fa',fillOpacity:.75}}).bindPopup('<b>FACILITY #'+f.rank+'</b><br>'+String(f.name||'')+'<br>EmsNo: '+String(f.ems_no||'')+'<br>Industry: '+String(f.industry||'')+'<br>Air regulated: '+String(f.is_air_regulated)+'<br>Point/local max/local mean: '+f.point_score.toFixed(3)+' / '+f.local_max_score.toFixed(3)+' / '+f.local_mean_score.toFixed(3)+'<br>CEMS available: '+String(f.cems_available)+'<br>Relative evidence: '+f.evidence_score.toFixed(3)).addTo(map));fires.forEach(f=>L.circleMarker([f.lat,f.lon],{{radius:Math.max(5,10*f.evidence_score),color:'#ea580c',fillColor:'#fb923c',fillOpacity:.75}}).bindPopup('<b>FIRE_HOTSPOT #'+f.rank+'</b><br>Group: '+String(f.fire_group_id)+'<br>Acquisition: '+String(f.first_acquisition_time_utc)+' — '+String(f.last_acquisition_time_utc)+'<br>Satellites: '+String((f.satellites||[]).join(', '))+'<br>Detections: '+f.detection_count+'<br>Confidence: '+String((f.confidence||[]).join(', '))+'<br>FRP (descriptive): '+String(f.max_frp)+'<br>Relative evidence: '+f.evidence_score.toFixed(3)).addTo(map));map.fitBounds([[ctx.south,ctx.west],[ctx.north,ctx.east]],{{padding:[12,12]}});</script></body></html>'''
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(page, encoding="utf-8")


__all__ = ["match_source_evidence", "write_evidence_json", "write_evidence_map", "write_facility_csv", "write_fire_csv"]
