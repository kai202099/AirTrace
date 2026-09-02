#!/usr/bin/env python3
"""Build a local-data-only interactive sensor coverage map and summary."""

from __future__ import annotations

import argparse
import csv
import json
import math
import sys
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from statistics import median
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_INPUT = ROOT / "data" / "normalized" / "pilot_sensor_inventory.csv"
DEFAULT_CONFIG = ROOT / "config" / "pilot_region.json"
DEFAULT_HTML = ROOT / "reports" / "pilot_sensor_coverage.html"
DEFAULT_SUMMARY = ROOT / "reports" / "pilot_sensor_coverage_summary.md"

EARTH_RADIUS_KM = 6371.0088
GRID_CELL_KM = 1.0
INDUSTRIAL_AREA_TYPES = {"工業區", "鄰近工業區社區"}
STATUS_ORDER = ["fresh", "stale", "offline", "missing observation", "suspicious/future"]


def meters_per_degree(latitude: float) -> tuple[float, float]:
    radians = math.radians(latitude)
    return (
        111132.92
        - 559.82 * math.cos(2 * radians)
        + 1.175 * math.cos(4 * radians)
        - 0.0023 * math.cos(6 * radians),
        111412.84 * math.cos(radians)
        - 93.5 * math.cos(3 * radians)
        + 0.118 * math.cos(5 * radians),
    )


def haversine_km(first: tuple[float, float], second: tuple[float, float]) -> float:
    lon1, lat1 = map(math.radians, first)
    lon2, lat2 = map(math.radians, second)
    dlon, dlat = lon2 - lon1, lat2 - lat1
    a = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(math.sqrt(min(1.0, a)))


def percentile(values: Iterable[float], fraction: float) -> float | None:
    ordered = sorted(values)
    if not ordered:
        return None
    position = (len(ordered) - 1) * fraction
    lower, upper = math.floor(position), math.ceil(position)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def as_float(value: Any) -> float | None:
    if value is None or str(value).strip() == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def as_bool(value: Any) -> bool:
    return str(value).strip().lower() in {"true", "1", "yes", "y"}


def load_context(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    bbox = payload["context_bbox"]
    required = {"north", "south", "west", "east"}
    if not required.issubset(bbox):
        raise ValueError(f"context_bbox must contain {sorted(required)}")
    result = {key: float(bbox[key]) for key in required}
    if not (result["south"] < result["north"] and result["west"] < result["east"]):
        raise ValueError("context_bbox has invalid bounds")
    return {"name": payload.get("name", "AirTrace Pilot Context Zone"), "bbox": result}


def map_status(row: dict[str, Any]) -> str:
    if as_bool(row.get("future_timestamp")) or row.get("freshness_status") == "future_timestamp":
        return "suspicious/future"
    status = row.get("freshness_status", "")
    if status == "missing_observation" or not row.get("latest_observation_phenomenon_time"):
        return "missing observation"
    if status == "offline":
        return "offline"
    if status == "stale":
        return "stale"
    if status == "fresh":
        return "fresh"
    return status or "unknown"


def load_sensors(path: Path) -> list[dict[str, Any]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        raw_rows = list(csv.DictReader(handle))
    if not raw_rows:
        raise ValueError(f"no rows found in {path}")
    sensors: list[dict[str, Any]] = []
    for raw in raw_rows:
        lat = as_float(raw.get("latitude"))
        lon = as_float(raw.get("longitude"))
        latest_pm25 = as_float(raw.get("latest_pm25_value"))
        sensors.append(
            {
                "station_id": raw.get("stationID", ""),
                "station_name": raw.get("stationName", ""),
                "lat": lat,
                "lon": lon,
                "township": raw.get("township", "") or "—",
                "city": raw.get("city", "") or "—",
                "area_type": raw.get("areaType", "") or "—",
                "area_description": raw.get("areaDescription", "") or "",
                "latest_pm25": latest_pm25,
                "latest_timestamp": raw.get("latest_observation_phenomenon_time", "") or "—",
                "freshness_status": raw.get("freshness_status", "") or "unknown",
                "observation_age_minutes": as_float(raw.get("observation_age_minutes")),
                "future_timestamp": as_bool(raw.get("future_timestamp")),
                "valid_coordinate": as_bool(raw.get("valid_coordinate")) and lat is not None and lon is not None,
                "valid_pm25": as_bool(raw.get("valid_pm25")) and latest_pm25 is not None,
                "map_status": map_status(raw),
            }
        )
    return sensors


def grid_shape(bbox: dict[str, float]) -> tuple[int, int, float, float]:
    lat_m, lon_m = meters_per_degree((bbox["north"] + bbox["south"]) / 2)
    rows = max(1, math.ceil((bbox["north"] - bbox["south"]) * lat_m / (GRID_CELL_KM * 1000)))
    columns = max(1, math.ceil((bbox["east"] - bbox["west"]) * lon_m / (GRID_CELL_KM * 1000)))
    return rows, columns, lat_m, lon_m


def point_cell(lat: float, lon: float, bbox: dict[str, float], rows: int, columns: int) -> tuple[int, int] | None:
    if not (bbox["south"] <= lat <= bbox["north"] and bbox["west"] <= lon <= bbox["east"]):
        return None
    row = min(rows - 1, int((lat - bbox["south"]) / (bbox["north"] - bbox["south"]) * rows))
    column = min(columns - 1, int((lon - bbox["west"]) / (bbox["east"] - bbox["west"]) * columns))
    return row, column


def cell_bounds(row: int, column: int, bbox: dict[str, float], rows: int, columns: int) -> tuple[float, float, float, float]:
    lat_step = (bbox["north"] - bbox["south"]) / rows
    lon_step = (bbox["east"] - bbox["west"]) / columns
    south = bbox["south"] + row * lat_step
    west = bbox["west"] + column * lon_step
    return south, west, south + lat_step, west + lon_step


def cell_center(bounds: tuple[float, float, float, float]) -> tuple[float, float]:
    south, west, north, east = bounds
    return (south + north) / 2, (west + east) / 2


def make_grid(sensors: list[dict[str, Any]], bbox: dict[str, float]) -> dict[str, Any]:
    rows, columns, lat_m, lon_m = grid_shape(bbox)
    fresh_points = [
        (sensor["lon"], sensor["lat"])
        for sensor in sensors
        if sensor["map_status"] == "fresh" and sensor["valid_coordinate"]
    ]
    cells: list[dict[str, Any]] = []
    by_key: dict[tuple[int, int], dict[str, Any]] = {}
    for row in range(rows):
        for column in range(columns):
            bounds = cell_bounds(row, column, bbox, rows, columns)
            center_lat, center_lon = cell_center(bounds)
            cell = {
                "row": row,
                "column": column,
                "south": bounds[0],
                "west": bounds[1],
                "north": bounds[2],
                "east": bounds[3],
                "center_lat": center_lat,
                "center_lon": center_lon,
                "sensor_count": 0,
                "fresh_count": 0,
                "industrial_fresh_count": 0,
            }
            cells.append(cell)
            by_key[(row, column)] = cell
    for sensor in sensors:
        if not sensor["valid_coordinate"]:
            continue
        key = point_cell(sensor["lat"], sensor["lon"], bbox, rows, columns)
        if key is None:
            continue
        cell = by_key[key]
        cell["sensor_count"] += 1
        if sensor["map_status"] == "fresh":
            cell["fresh_count"] += 1
            if sensor["area_type"] in INDUSTRIAL_AREA_TYPES:
                cell["industrial_fresh_count"] += 1
    occupied = sum(cell["fresh_count"] > 0 for cell in cells)
    empty_keys = {(cell["row"], cell["column"]) for cell in cells if cell["fresh_count"] == 0}
    return {
        "rows": rows,
        "columns": columns,
        "lat_m": lat_m,
        "lon_m": lon_m,
        "cell_area_km2": (
            ((bbox["north"] - bbox["south"]) / rows) * lat_m / 1000
            * ((bbox["east"] - bbox["west"]) / columns) * lon_m / 1000
        ),
        "cells": cells,
        "by_key": by_key,
        "fresh_points": fresh_points,
        "occupied": occupied,
        "total": rows * columns,
        "empty_keys": empty_keys,
    }


def connected_components(keys: set[tuple[int, int]]) -> list[list[tuple[int, int]]]:
    remaining = set(keys)
    components: list[list[tuple[int, int]]] = []
    while remaining:
        start = remaining.pop()
        component = [start]
        queue: deque[tuple[int, int]] = deque([start])
        while queue:
            row, column = queue.popleft()
            for neighbor in ((row - 1, column), (row + 1, column), (row, column - 1), (row, column + 1)):
                if neighbor in remaining:
                    remaining.remove(neighbor)
                    component.append(neighbor)
                    queue.append(neighbor)
        components.append(sorted(component))
    return sorted(components, key=lambda component: (-len(component), min(component)))


def spacing_stats(points: list[tuple[float, float]]) -> tuple[float | None, float | None]:
    if len(points) < 2:
        return None, None
    nearest: list[float] = []
    for index, point in enumerate(points):
        nearest.append(min(haversine_km(point, other) for other_index, other in enumerate(points) if other_index != index))
    return median(nearest), percentile(nearest, 0.90)


def gap_records(grid: dict[str, Any]) -> list[dict[str, Any]]:
    components = connected_components(grid["empty_keys"])
    records: list[dict[str, Any]] = []
    fresh_points = grid["fresh_points"]
    for index, component in enumerate(components, start=1):
        selected = [grid["by_key"][key] for key in component]
        south = min(cell["south"] for cell in selected)
        west = min(cell["west"] for cell in selected)
        north = max(cell["north"] for cell in selected)
        east = max(cell["east"] for cell in selected)
        center_lat = (south + north) / 2
        center_lon = (west + east) / 2
        nearest = (
            min(
                haversine_km((cell["center_lon"], cell["center_lat"]), point)
                for cell in selected
                for point in fresh_points
            )
            if fresh_points
            else None
        )
        records.append(
            {
                "gap_id": f"G{index:02d}",
                "cell_count": len(component),
                "area_km2": len(component) * grid["cell_area_km2"],
                "south": south,
                "west": west,
                "north": north,
                "east": east,
                "center_lat": center_lat,
                "center_lon": center_lon,
                "nearest_fresh_km": nearest,
                "keys": [[row, column] for row, column in component],
            }
        )
    return records


def choose_core_candidate(grid: dict[str, Any]) -> dict[str, Any]:
    cells = grid["cells"]
    affinity_keys = {
        (cell["row"], cell["column"])
        for cell in cells
        if cell["fresh_count"] >= 2
        and (
            cell["industrial_fresh_count"] >= 2
            or cell["industrial_fresh_count"] / cell["fresh_count"] >= 0.5
        )
    }
    components = connected_components(affinity_keys)
    if not components:
        dense_threshold = max(
            2,
            int(percentile([cell["fresh_count"] for cell in cells if cell["fresh_count"]], 0.75) or 2),
        )
        affinity_keys = {(cell["row"], cell["column"]) for cell in cells if cell["fresh_count"] >= dense_threshold}
        components = connected_components(affinity_keys)
    if not components:
        raise ValueError("could not derive a Core Zone candidate from fresh sensor cells")
    by_key = grid["by_key"]
    selected_component = max(
        components,
        key=lambda component: (
            sum(by_key[key]["industrial_fresh_count"] for key in component),
            sum(by_key[key]["fresh_count"] for key in component),
            len(component),
        ),
    )
    core_rows = [row for row, _ in selected_component]
    core_columns = [column for _, column in selected_component]
    buffer = 1
    min_row = max(0, min(core_rows) - buffer)
    max_row = min(grid["rows"] - 1, max(core_rows) + buffer)
    min_column = max(0, min(core_columns) - buffer)
    max_column = min(grid["columns"] - 1, max(core_columns) + buffer)
    candidate_keys = {
        (row, column)
        for row in range(min_row, max_row + 1)
        for column in range(min_column, max_column + 1)
    }
    candidate_coverage = sum(by_key[key]["fresh_count"] > 0 for key in candidate_keys) / len(candidate_keys)
    if candidate_coverage < 0.75:
        buffer = 0
        min_row, max_row = min(core_rows), max(core_rows)
        min_column, max_column = min(core_columns), max(core_columns)
        candidate_keys = set(selected_component)
    selected_cells = [by_key[key] for key in sorted(candidate_keys)]
    south = min(cell["south"] for cell in selected_cells)
    west = min(cell["west"] for cell in selected_cells)
    north = max(cell["north"] for cell in selected_cells)
    east = max(cell["east"] for cell in selected_cells)
    return {
        "method": "highest-industrial-affinity connected fresh-cell component with a one-cell continuity-preserving buffer",
        "affinity_component_cells": len(selected_component),
        "affinity_component_fresh": sum(by_key[key]["fresh_count"] for key in selected_component),
        "affinity_component_industrial_fresh": sum(by_key[key]["industrial_fresh_count"] for key in selected_component),
        "buffer_cells": buffer,
        "min_row": min_row,
        "max_row": max_row,
        "min_column": min_column,
        "max_column": max_column,
        "keys": [[row, column] for row, column in sorted(candidate_keys)],
        "south": south,
        "west": west,
        "north": north,
        "east": east,
        "cell_count": len(candidate_keys),
        "fresh_grid_cell_count": sum(by_key[key]["fresh_count"] > 0 for key in candidate_keys),
        "fresh_sensor_count": sum(by_key[key]["fresh_count"] for key in candidate_keys),
        "sensor_count": sum(by_key[key]["sensor_count"] for key in candidate_keys),
        "industrial_fresh_sensor_count": sum(by_key[key]["industrial_fresh_count"] for key in candidate_keys),
    }


def candidate_sensor_points(sensors: list[dict[str, Any]], candidate: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        sensor
        for sensor in sensors
        if sensor["valid_coordinate"]
        and candidate["south"] <= sensor["lat"] <= candidate["north"]
        and candidate["west"] <= sensor["lon"] <= candidate["east"]
    ]


def bbox_direction(gap: dict[str, Any], context_bbox: dict[str, float]) -> str:
    context_lat = (context_bbox["south"] + context_bbox["north"]) / 2
    context_lon = (context_bbox["west"] + context_bbox["east"]) / 2
    lat_delta = gap["center_lat"] - context_lat
    lon_delta = gap["center_lon"] - context_lon
    vertical = "north" if lat_delta > 0.01 else "south" if lat_delta < -0.01 else "central"
    horizontal = "east" if lon_delta > 0.01 else "west" if lon_delta < -0.01 else "central"
    if vertical == "central" and horizontal == "central":
        return "central"
    if vertical == "central":
        return horizontal
    if horizontal == "central":
        return vertical
    return f"{vertical}-{horizontal}"


def format_km(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.2f} km"


def spatial_bbox(sensors: list[dict[str, Any]]) -> tuple[float, float, float, float] | None:
    points = [(sensor["lat"], sensor["lon"]) for sensor in sensors if sensor["valid_coordinate"]]
    if not points:
        return None
    return (
        min(lat for lat, _ in points),
        max(lat for lat, _ in points),
        min(lon for _, lon in points),
        max(lon for _, lon in points),
    )


def json_for_html(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")


def render_html(
    sensors: list[dict[str, Any]],
    context: dict[str, Any],
    grid: dict[str, Any],
    gaps: list[dict[str, Any]],
    candidate: dict[str, Any],
    html_path: Path,
) -> None:
    serial_sensors = [
        {key: value for key, value in sensor.items() if key not in {"valid_coordinate", "valid_pm25"}}
        for sensor in sensors
    ]
    serial_cells = [
        {
            key: cell[key]
            for key in (
                "row",
                "column",
                "south",
                "west",
                "north",
                "east",
                "sensor_count",
                "fresh_count",
                "industrial_fresh_count",
            )
        }
        for cell in grid["cells"]
    ]
    context_bbox = context["bbox"]
    map_center = [
        (context_bbox["south"] + context_bbox["north"]) / 2,
        (context_bbox["west"] + context_bbox["east"]) / 2,
    ]
    total = len(sensors)
    fresh_count = sum(sensor["map_status"] == "fresh" for sensor in sensors)
    coverage_pct = 100 * grid["occupied"] / grid["total"]
    html_content = f"""<!doctype html>
<html lang="zh-Hant">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="airtrace-sensor-count" content="{total}">
  <title>AirTrace Pilot Region Sensor Coverage</title>
  <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">
  <link rel="stylesheet" href="https://unpkg.com/leaflet.markercluster@1.5.3/dist/MarkerCluster.css">
  <link rel="stylesheet" href="https://unpkg.com/leaflet.markercluster@1.5.3/dist/MarkerCluster.Default.css">
  <style>
    :root {{ color-scheme: light; font-family: system-ui, -apple-system, "Segoe UI", sans-serif; }}
    body {{ margin: 0; color: #172033; background: #f7f9fc; }}
    header {{ padding: 14px 18px 10px; background: #ffffff; border-bottom: 1px solid #d9e0ea; }}
    h1 {{ margin: 0 0 5px; font-size: 21px; }}
    .subtitle {{ color: #586579; font-size: 13px; }}
    .stats {{ display: flex; flex-wrap: wrap; gap: 12px 22px; margin-top: 10px; font-size: 13px; }}
    .stats strong {{ color: #172033; }}
    #map {{ height: calc(100vh - 118px); min-height: 560px; }}
    .legend {{ background: white; padding: 9px 11px; line-height: 1.5; border-radius: 4px; box-shadow: 0 1px 5px rgba(0,0,0,.25); }}
    .legend i {{ display: inline-block; width: 12px; height: 12px; margin-right: 5px; vertical-align: -1px; border: 1px solid #7c8798; }}
    .popup-table td {{ padding: 2px 6px 2px 0; vertical-align: top; }}
    .popup-table td:first-child {{ color: #586579; white-space: nowrap; }}
    .leaflet-control-layers {{ max-height: 75vh; overflow-y: auto; }}
  </style>
</head>
<body>
  <header>
    <h1>AirTrace Pilot Region Sensor Coverage Map</h1>
    <div class="subtitle">本地 inventory 產生；位置以 latitude / longitude 為準，township 僅作 metadata 顯示。</div>
    <div class="stats">
      <span><strong>{total}</strong> sensors</span>
      <span><strong>{fresh_count}</strong> fresh</span>
      <span><strong>{grid["occupied"]}/{grid["total"]}</strong> covered cells ({coverage_pct:.2f}%)</span>
      <span><strong>{len(gaps)}</strong> empty-cell components</span>
      <span>Core candidate: <strong>{candidate["fresh_sensor_count"]}</strong> fresh / <strong>{candidate["sensor_count"]}</strong> all</span>
    </div>
  </header>
  <div id="map" aria-label="Interactive sensor coverage map"></div>
  <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
  <script src="https://unpkg.com/leaflet.markercluster@1.5.3/dist/leaflet.markercluster.js"></script>
  <script>
    const contextBbox = {json_for_html(context_bbox)};
    const sensors = {json_for_html(serial_sensors)};
    const gridCells = {json_for_html(serial_cells)};
    const coreCandidate = {json_for_html({key: value for key, value in candidate.items() if key != "keys"})};
    const majorGaps = {json_for_html(gaps[:10])};
    const map = L.map('map', {{preferCanvas: true}}).setView({json_for_html(map_center)}, 12);
    const osm = L.tileLayer('https://{{s}}.tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png', {{
      maxZoom: 19, attribution: '&copy; OpenStreetMap contributors'
    }}).addTo(map);
    const contextLayer = L.layerGroup();
    L.rectangle([[contextBbox.south, contextBbox.west], [contextBbox.north, contextBbox.east]], {{
      color: '#26364f', weight: 2, fill: false, dashArray: '7 5'
    }}).bindPopup('<strong>Context Zone</strong><br>south ' + contextBbox.south + ', west ' + contextBbox.west + '<br>north ' + contextBbox.north + ', east ' + contextBbox.east).addTo(contextLayer);
    contextLayer.addTo(map);

    function coverageStyle(cell) {{
      if (cell.fresh_count === 0) return {{color:'#b42318', fillColor:'#f04438', fillOpacity:0.22, weight:1}};
      if (cell.fresh_count >= 8) return {{color:'#146c43', fillColor:'#1f9d62', fillOpacity:0.52, weight:1}};
      if (cell.fresh_count >= 3) return {{color:'#55701a', fillColor:'#a8c957', fillOpacity:0.40, weight:1}};
      return {{color:'#9a6700', fillColor:'#f2c94c', fillOpacity:0.30, weight:1}};
    }}
    const coverageLayer = L.layerGroup();
    gridCells.forEach(cell => {{
      const covered = cell.fresh_count > 0;
      const popup = '<strong>Grid cell r' + cell.row + ' c' + cell.column + '</strong>' +
        '<table class="popup-table"><tr><td>fresh sensors</td><td>' + cell.fresh_count + '</td></tr>' +
        '<tr><td>all sensors</td><td>' + cell.sensor_count + '</td></tr>' +
        '<tr><td>industrial / near-industrial</td><td>' + cell.industrial_fresh_count + '</td></tr>' +
        '<tr><td>covered</td><td>' + (covered ? 'yes' : 'no') + '</td></tr></table>';
      L.rectangle([[cell.south, cell.west], [cell.north, cell.east]], coverageStyle(cell)).bindPopup(popup).addTo(coverageLayer);
    }});
    coverageLayer.addTo(map);

    const coreLayer = L.layerGroup();
    L.rectangle([[coreCandidate.south, coreCandidate.west], [coreCandidate.north, coreCandidate.east]], {{
      color: '#8e2a86', weight: 3, fillColor: '#c77dff', fillOpacity: 0.10, dashArray: '8 4'
    }}).bindPopup('<strong>Data-derived Core Zone candidate</strong><br>' +
      'fresh sensors: ' + coreCandidate.fresh_sensor_count + '<br>all sensors: ' + coreCandidate.sensor_count +
      '<br>grid cells: ' + coreCandidate.cell_count + '<br>buffer cells: ' + coreCandidate.buffer_cells).addTo(coreLayer);
    coreLayer.addTo(map);

    const styles = {{
      'fresh': {{color:'#1677ff', fillColor:'#1677ff'}},
      'stale': {{color:'#d97706', fillColor:'#f59e0b'}},
      'offline': {{color:'#7c3aed', fillColor:'#8b5cf6'}},
      'missing observation': {{color:'#64748b', fillColor:'#94a3b8'}},
      'suspicious/future': {{color:'#dc2626', fillColor:'#ef4444'}}
    }};
    const groups = {{}};
    const statusOrder = ['fresh','stale','offline','missing observation','suspicious/future'];
    statusOrder.forEach(status => {{ groups[status] = L.markerClusterGroup({{disableClusteringAtZoom: 15, maxClusterRadius: 42, spiderfyOnMaxZoom: true}}); }});
    function safe(value) {{ return String(value ?? '—').replace(/[&<>\"']/g, ch => ({{'&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;',"'":'&#39;'}}[ch])); }}
    function pm25(value) {{ return value === null || value === undefined ? '—' : Number(value).toFixed(2) + ' µg/m³'; }}
    sensors.forEach(sensor => {{
      if (sensor.lat === null || sensor.lon === null) return;
      const style = styles[sensor.map_status] || styles['missing observation'];
      const popup = '<strong>' + safe(sensor.station_name) + '</strong>' +
        '<table class="popup-table"><tr><td>station_id</td><td>' + safe(sensor.station_id) + '</td></tr>' +
        '<tr><td>lat / lon</td><td>' + Number(sensor.lat).toFixed(6) + ' / ' + Number(sensor.lon).toFixed(6) + '</td></tr>' +
        '<tr><td>township metadata</td><td>' + safe(sensor.township) + '</td></tr>' +
        '<tr><td>area_type</td><td>' + safe(sensor.area_type) + '</td></tr>' +
        '<tr><td>latest PM2.5</td><td>' + pm25(sensor.latest_pm25) + '</td></tr>' +
        '<tr><td>latest timestamp</td><td>' + safe(sensor.latest_timestamp) + '</td></tr>' +
        '<tr><td>status</td><td>' + safe(sensor.map_status) + ' (' + safe(sensor.freshness_status) + ')</td></tr></table>';
      L.circleMarker([sensor.lat, sensor.lon], {{radius: 4, weight: 1.3, opacity: 0.9, fillOpacity: 0.72, color: style.color, fillColor: style.fillColor}})
        .bindPopup(popup).addTo(groups[sensor.map_status] || groups['missing observation']);
    }});
    statusOrder.forEach(status => groups[status].addTo(map));

    const overlayMaps = {{
      'Context boundary': contextLayer,
      'Coverage grid': coverageLayer,
      'Core candidate': coreLayer,
      'Fresh sensors': groups['fresh'],
      'Stale sensors': groups['stale'],
      'Offline sensors': groups['offline'],
      'Missing observation': groups['missing observation'],
      'Suspicious / future': groups['suspicious/future']
    }};
    L.control.layers({{'OpenStreetMap': osm}}, overlayMaps, {{collapsed:false}}).addTo(map);
    const legend = L.control({{position:'bottomright'}});
    legend.onAdd = function() {{
      const div = L.DomUtil.create('div', 'legend');
      div.innerHTML = '<strong>Coverage / status</strong><br>' +
        '<i style="background:#1f9d62"></i>fresh count ≥ 8<br>' +
        '<i style="background:#a8c957"></i>fresh count 3–7<br>' +
        '<i style="background:#f2c94c"></i>fresh count 1–2<br>' +
        '<i style="background:#f04438"></i>empty fresh cell<br>' +
        '<hr style="border:0;border-top:1px solid #d9e0ea">' +
        '<span style="color:#1677ff">●</span> fresh &nbsp; <span style="color:#d97706">●</span> stale<br>' +
        '<span style="color:#7c3aed">●</span> offline &nbsp; <span style="color:#64748b">●</span> missing<br>' +
        '<span style="color:#dc2626">●</span> suspicious/future';
      return div;
    }};
    legend.addTo(map);
    map.fitBounds([[contextBbox.south, contextBbox.west], [contextBbox.north, contextBbox.east]], {{padding:[14,14]}});
  </script>
</body>
</html>
"""
    html_path.parent.mkdir(parents=True, exist_ok=True)
    html_path.write_text(html_content, encoding="utf-8")


def render_summary(
    sensors: list[dict[str, Any]],
    context: dict[str, Any],
    grid: dict[str, Any],
    gaps: list[dict[str, Any]],
    candidate: dict[str, Any],
    summary_path: Path,
) -> None:
    bbox = context["bbox"]
    fresh = [sensor for sensor in sensors if sensor["map_status"] == "fresh" and sensor["valid_coordinate"]]
    fresh_points = [(sensor["lon"], sensor["lat"]) for sensor in fresh]
    med, p90 = spacing_stats(fresh_points)
    candidate_sensors = candidate_sensor_points(sensors, candidate)
    candidate_fresh = [sensor for sensor in candidate_sensors if sensor["map_status"] == "fresh"]
    candidate_points = [(sensor["lon"], sensor["lat"]) for sensor in candidate_fresh]
    candidate_med, candidate_p90 = spacing_stats(candidate_points)
    candidate_area_km2 = candidate["cell_count"] * grid["cell_area_km2"]
    background = [sensor for sensor in sensors if sensor not in candidate_sensors]
    status_counts = {status: sum(sensor["map_status"] == status for sensor in sensors) for status in STATUS_ORDER}
    spatial = spatial_bbox(fresh)
    generated_at = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    latest_times = [sensor["latest_timestamp"] for sensor in sensors if sensor["latest_timestamp"] not in {"", "—"}]
    as_of = max(latest_times) if latest_times else "n/a"
    direction = bbox_direction(gaps[0], bbox) if gaps else "n/a"
    dominant_townships: dict[str, int] = {}
    for sensor in candidate_fresh:
        township = sensor["township"]
        dominant_townships[township] = dominant_townships.get(township, 0) + 1
    township_text = ", ".join(
        f"{name} {count}" for name, count in sorted(dominant_townships.items(), key=lambda item: (-item[1], item[0]))
    ) or "n/a"
    lines = [
        "# Pilot Region Sensor Coverage Map Summary",
        "",
        f"Generated at UTC: {generated_at}  ",
        f"Inventory as-of (latest timestamp string): {as_of}",
        "",
        "## Context Zone stats",
        "",
        f"- Bbox from config/pilot_region.json: south {bbox['south']}, north {bbox['north']}, west {bbox['west']}, east {bbox['east']}.",
        f"- Sensors: **{len(sensors)}**; fresh **{status_counts['fresh']}**; stale **{status_counts['stale']}**; offline **{status_counts['offline']}**; missing observation **{status_counts['missing observation']}**; suspicious/future **{status_counts['suspicious/future']}**.",
        f"- Fresh valid-coordinate spatial bbox: lat {spatial[0]:.6f}..{spatial[1]:.6f}, lon {spatial[2]:.6f}..{spatial[3]:.6f}." if spatial else "- Fresh valid-coordinate spatial bbox: n/a.",
        f"- Fresh nearest-neighbor spacing: median **{format_km(med)}**, P90 **{format_km(p90)}**.",
        f"- Coverage grid: **{grid['rows']} × {grid['columns']} = {grid['total']}** cells; fresh-covered **{grid['occupied']}**; coverage **{100 * grid['occupied'] / grid['total']:.2f}%**; approximate cell area **{grid['cell_area_km2']:.3f} km²**.",
        "",
        "## Major coverage gaps",
        "",
        "Empty cells are grouped with 4-neighbor connected components. Nearest fresh is the minimum distance from any empty-cell center to a fresh sensor.",
        "",
        "| Gap | Empty cells | Approx. area km² | Bounding box (S,W,N,E) | Center (lat, lon) | Nearest fresh | Direction |",
        "|---|---:|---:|---|---|---:|---|",
    ]
    for gap in gaps[:10]:
        lines.append(
            f"| {gap['gap_id']} | {gap['cell_count']} | {gap['area_km2']:.2f} | "
            f"({gap['south']:.5f}, {gap['west']:.5f}, {gap['north']:.5f}, {gap['east']:.5f}) | "
            f"({gap['center_lat']:.5f}, {gap['center_lon']:.5f}) | {format_km(gap['nearest_fresh_km'])} | {bbox_direction(gap, bbox)} |"
        )
    if gaps:
        lines.extend(
            [
                "",
                f"The largest connected empty area is **{gaps[0]['gap_id']}** ({gaps[0]['cell_count']} cells, {gaps[0]['area_km2']:.2f} km²), located in the **{direction}** part of the Context Zone.",
            ]
        )
    lines.extend(
        [
            "",
            "## Fivegu / upper-Xinzhuang Core Zone candidate",
            "",
            "The candidate is not an administrative boundary. It is derived from the highest-scoring 4-connected component of fresh grid cells with an industrial-affinity signal: at least two industrial/near-industrial fresh sensors, or at least half of the cell's fresh sensors carrying those area types. A one-cell buffer is retained only when the buffered rectangle remains at least 75% fresh-covered.",
            "",
            f"- Candidate bbox (S,W,N,E): **({candidate['south']:.6f}, {candidate['west']:.6f}, {candidate['north']:.6f}, {candidate['east']:.6f})**.",
            f"- Candidate grid cells: **{candidate['cell_count']}**; fresh-covered cells **{candidate['fresh_grid_cell_count']}**; continuity **{100 * candidate['fresh_grid_cell_count'] / candidate['cell_count']:.1f}%**.",
            f"- Candidate sensors (all statuses): **{len(candidate_sensors)}**.",
            f"- Candidate fresh sensors: **{len(candidate_fresh)}**.",
            f"- Candidate sensor density: **{len(candidate_sensors) / candidate_area_km2:.2f} all sensors/km²**; **{len(candidate_fresh) / candidate_area_km2:.2f} fresh sensors/km²** over approximately **{candidate_area_km2:.2f} km²**.",
            f"- Candidate industrial/near-industrial fresh sensors: **{candidate['industrial_fresh_sensor_count']}**.",
            f"- Candidate median/P90 nearest-neighbor spacing: **{format_km(candidate_med)} / {format_km(candidate_p90)}**.",
            f"- Candidate fresh metadata labels, descriptive only: {township_text}.",
            f"- Context background sensors outside candidate: **{len(background)}** ({sum(sensor['map_status'] == 'fresh' for sensor in background)} fresh).",
            "",
            "## Recommendations",
            "",
            "- **Keep the current Context bbox: Yes.** It contains the full fresh sensor distribution, the connected gap components for comparison, and a substantial outside-candidate background population; shrinking it would remove useful contrast.",
            "- **Adjust Core candidate: Yes, for analysis only.** Use the data-derived bbox above as the current candidate for Fivegu / upper-Xinzhuang analysis. Do not write it back to config/pilot_region.json.",
            "- Coordinates are authoritative for all geometry. Township metadata is shown as a diagnostic label and was not used as the candidate boundary.",
            "",
        ]
    )
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text("\n".join(lines), encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--html-output", type=Path, default=DEFAULT_HTML)
    parser.add_argument("--summary-output", type=Path, default=DEFAULT_SUMMARY)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        context = load_context(args.config)
        sensors = load_sensors(args.input)
        grid = make_grid(sensors, context["bbox"])
        gaps = gap_records(grid)
        candidate = choose_core_candidate(grid)
        render_html(sensors, context, grid, gaps, candidate, args.html_output)
        render_summary(sensors, context, grid, gaps, candidate, args.summary_output)
        print(f"HTML: {args.html_output}")
        print(f"Summary: {args.summary_output}")
        print(f"Sensors: {len(sensors)}; fresh: {sum(sensor['map_status'] == 'fresh' for sensor in sensors)}")
        print(f"Grid: {grid['rows']}x{grid['columns']}; covered: {grid['occupied']}/{grid['total']} ({100 * grid['occupied'] / grid['total']:.2f}%)")
        print(f"Connected empty components: {len(gaps)}")
        print(f"Core candidate bbox S,W,N,E: {candidate['south']:.6f},{candidate['west']:.6f},{candidate['north']:.6f},{candidate['east']:.6f}")
        print(f"Core candidate sensors: {candidate['sensor_count']}; fresh: {candidate['fresh_sensor_count']}")
        for gap in gaps[:10]:
            print(f"{gap['gap_id']}: cells={gap['cell_count']} area={gap['area_km2']:.2f}km2 center=({gap['center_lat']:.5f},{gap['center_lon']:.5f}) nearest={format_km(gap['nearest_fresh_km'])} direction={bbox_direction(gap, context['bbox'])}")
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
