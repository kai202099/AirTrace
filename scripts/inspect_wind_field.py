#!/usr/bin/env python3
"""Inspect the deterministic CWA wind field around the AirTrace Pilot Core."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from airtrace.analysis.anomaly import load_pilot_config  # noqa: E402
from airtrace.analysis.wind import (  # noqa: E402
    DEFAULT_DATABASE,
    WindConfig,
    WindEstimate,
    WindFieldError,
    build_summary,
    estimate_to_dict,
    grid_points,
    load_wind_snapshot,
)


DEFAULT_CONFIG = ROOT / "config" / "pilot_region.json"
DEFAULT_MAP = ROOT / "reports" / "wind" / "latest_wind_field.html"
DEFAULT_SUMMARY = ROOT / "reports" / "wind" / "latest_wind_summary.json"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    location = parser.add_mutually_exclusive_group(required=True)
    location.add_argument("--latest", action="store_true", help="use the latest weather observation time")
    location.add_argument("--at", help="timezone-aware ISO-8601 query time, for example 2026-09-02T21:20:00+08:00")
    parser.add_argument("--lat", type=float, help="WGS84 latitude for a single estimate")
    parser.add_argument("--lon", type=float, help="WGS84 longitude for a single estimate")
    parser.add_argument("--grid", action="store_true", help="also evaluate a low-resolution Core Zone grid")
    parser.add_argument("--grid-spacing-km", type=float, default=1.0)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE)
    parser.add_argument("--map-output", type=Path, default=DEFAULT_MAP)
    parser.add_argument("--summary-output", type=Path, default=DEFAULT_SUMMARY)
    parser.add_argument("--preferred-radius-km", type=float, default=15.0)
    parser.add_argument("--maximum-radius-km", type=float, default=30.0)
    parser.add_argument("--minimum-stations", type=int, default=3)
    parser.add_argument("--maximum-stations", type=int, default=8)
    parser.add_argument("--maximum-temporal-distance-minutes", type=float, default=15.0)
    parser.add_argument("--idw-power", type=float, default=2.0)
    return parser.parse_args()


def html_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")


def _grid_payload(grid: list[WindEstimate]) -> list[dict[str, Any]]:
    return [estimate_to_dict(item) for item in grid]


def write_map(
    path: Path,
    config: dict[str, Any],
    center: WindEstimate,
    grid: list[WindEstimate],
) -> None:
    context = config["context_bbox"]
    core = config["core_bbox"]
    center_payload = estimate_to_dict(center)
    stations = center_payload["stations_used"]
    grid_payload = _grid_payload(grid)
    map_center = [(core["south"] + core["north"]) / 2, (core["west"] + core["east"]) / 2]
    html = f"""<!doctype html>
<html lang="zh-Hant"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>AirTrace Wind Field v1</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">
<style>body{{margin:0;font-family:system-ui,-apple-system,"Segoe UI",sans-serif;color:#172033}}header{{padding:14px 18px;border-bottom:1px solid #d9e0ea;background:#fff}}h1{{margin:0 0 5px;font-size:21px}}.note{{color:#586579;font-size:13px}}#map{{height:calc(100vh - 105px);min-height:560px}}.popup-table td{{padding:2px 6px 2px 0;vertical-align:top}}.popup-table td:first-child{{color:#586579;white-space:nowrap}}</style></head>
<body><header><h1>AirTrace Wind Field Interpolation v1</h1><div class="note">Query: {center_payload["query_time_utc"]} · 箭頭表示風的 TO 方向；IDW u/v interpolation。confidence 是資料/插值品質分數，不是 probability。</div></header><div id="map"></div>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script><script>
const contextBbox={html_json(context)}, coreBbox={html_json(core)}, center={html_json(center_payload)}, stations={html_json(stations)}, grid={html_json(grid_payload)};
const map=L.map('map',{{preferCanvas:true}}).setView({html_json(map_center)},12); const osm=L.tileLayer('https://{{s}}.tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png',{{maxZoom:19,attribution:'&copy; OpenStreetMap contributors'}}).addTo(map);
L.rectangle([[contextBbox.south,contextBbox.west],[contextBbox.north,contextBbox.east]],{{color:'#26364f',weight:2,fill:false,dashArray:'7 5'}}).bindPopup('<strong>Context Zone</strong>').addTo(map);
L.rectangle([[coreBbox.south,coreBbox.west],[coreBbox.north,coreBbox.east]],{{color:'#8e2a86',weight:3,fillColor:'#c77dff',fillOpacity:.08,dashArray:'8 4'}}).bindPopup('<strong>Core Zone</strong>').addTo(map);
function safe(v){{return String(v??'—').replace(/[&<>"']/g,c=>({{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}}[c]))}}
function n(v){{return v===null||v===undefined?'—':Number(v).toFixed(2)}}
function arrow(lat,lon,u,v,color,scale,layer){{const dlat=v*scale/111195, dlon=u*scale/(111195*Math.max(.2,Math.cos(lat*Math.PI/180))); const end=[lat+dlat,lon+dlon]; L.polyline([[lat,lon],end],{{color,weight:2,opacity:.8}}).addTo(layer); const angle=Math.atan2(dlat,dlon), size=.0008; const p1=[end[0]-size*Math.sin(angle+Math.PI/6),end[1]-size*Math.cos(angle+Math.PI/6)],p2=[end[0]-size*Math.sin(angle-Math.PI/6),end[1]-size*Math.cos(angle-Math.PI/6)]; L.polyline([p1,end,p2],{{color,weight:2,opacity:.8}}).addTo(layer)}}
const stationLayer=L.layerGroup().addTo(map), gridLayer=L.layerGroup().addTo(map);
stations.forEach(s=>{{const popup='<strong>'+safe(s.station_id)+' · '+safe(s.station_name)+'</strong><table class="popup-table"><tr><td>observation</td><td>'+safe(s.observation_time_utc||((s.source_before_utc||'')+' → '+(s.source_after_utc||'')))+'</td></tr><tr><td>mode</td><td>'+safe(s.temporal_mode)+' / offset '+n(s.temporal_offset_minutes)+' min</td></tr><tr><td>wind_from</td><td>'+n(s.wind_from_deg)+'°</td></tr><tr><td>wind speed</td><td>'+n(s.speed_mps)+' m/s</td></tr><tr><td>u / v</td><td>'+n(s.u_east_mps)+' / '+n(s.v_north_mps)+' m/s</td></tr><tr><td>distance</td><td>'+n(s.distance_km)+' km</td></tr><tr><td>status</td><td>'+safe(s.wind_status)+'</td></tr></table>'; L.circleMarker([s.lat,s.lon],{{radius:5,color:'#d92d20',fillColor:'#fff',fillOpacity:1,weight:2}}).bindPopup(popup).addTo(stationLayer); arrow(s.lat,s.lon,s.u_east_mps,s.v_north_mps,'#d92d20',.9,stationLayer)}});
grid.forEach(g=>{{if(g.u_east_mps===null||g.v_north_mps===null)return; arrow(g.query_lat,g.query_lon,g.u_east_mps,g.v_north_mps,g.quality_category==='GOOD'?'#2563eb':'#f79009',.65,gridLayer)}});
L.circleMarker([center.query_lat,center.query_lon],{{radius:8,color:'#111827',fillColor:'#facc15',fillOpacity:.9,weight:3}}).bindPopup('<strong>Core center estimate</strong><table class="popup-table"><tr><td>u / v</td><td>'+n(center.u_east_mps)+' / '+n(center.v_north_mps)+' m/s</td></tr><tr><td>speed</td><td>'+n(center.speed_mps)+' m/s</td></tr><tr><td>from / to</td><td>'+n(center.wind_from_deg)+'° / '+n(center.wind_to_deg)+'°</td></tr><tr><td>quality</td><td>'+safe(center.quality_category)+'</td></tr><tr><td>spatial / temporal</td><td>'+safe(center.spatial_uncertainty)+' / '+safe(center.temporal_uncertainty)+'</td></tr><tr><td>disagreement</td><td>'+n(center.vector_disagreement_mps)+' m/s</td></tr></table>').addTo(map);
L.control.layers({{'OpenStreetMap':osm}},{{'Stations used (TO vectors)':stationLayer,'Interpolation grid (TO vectors)':gridLayer}}).addTo(map); map.fitBounds([[contextBbox.south,contextBbox.west],[contextBbox.north,contextBbox.east]],{{padding:[14,14]}});
</script></body></html>"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(html, encoding="utf-8")


def print_report(summary: dict[str, Any], center: WindEstimate, grid: list[WindEstimate], map_path: Path, summary_path: Path) -> None:
    payload = estimate_to_dict(center)
    diagnostics = payload["diagnostics"]
    print("AirTrace Wind Field Interpolation v1")
    print(f"Analysis time UTC: {payload['query_time_utc']}")
    print(f"Weather DB time span: {summary['analysis']['database_time_span_utc']['start']} → {summary['analysis']['database_time_span_utc']['end']}")
    print(f"Effective nearby stations: {payload['station_count']} (preferred {diagnostics['candidate_count_preferred_radius']}, maximum {diagnostics['candidate_count_maximum_radius']})")
    print("5/10/20/30 km effective station counts: " + "/".join(str(diagnostics["effective_station_counts_within_km"][str(radius)]) for radius in (5, 10, 20, 30)))
    if payload["quality_category"] == "INSUFFICIENT_STATIONS":
        print("INSUFFICIENT_STATIONS")
    else:
        print(f"Core center u/v: {payload['u_east_mps']} / {payload['v_north_mps']} m/s")
        print(f"Core center speed: {payload['speed_mps']} m/s; from={payload['wind_from_deg']}°; to={payload['wind_to_deg']}°")
    print(f"Stations used: {', '.join(item['station_id'] for item in payload['stations_used']) or 'none'}")
    print(f"Nearest/farthest: {payload['nearest_station_km']} / {payload['farthest_station_km']} km")
    print(f"Temporal mode/offset: {payload['temporal_uncertainty']} / {payload['temporal_offset_minutes']} min")
    print(f"Spatial uncertainty/disagreement: {payload['spatial_uncertainty']} / {payload['vector_disagreement_mps']} m/s")
    print(f"Quality/confidence: {payload['quality_category']} / {payload['confidence']}")
    print(f"Grid points: {len(grid)}")
    print(f"Map: {map_path}")
    print(f"JSON: {summary_path}")


def main() -> int:
    args = parse_args()
    if (args.lat is None) != (args.lon is None):
        print("ERROR: --lat and --lon must be supplied together", file=sys.stderr)
        return 2
    if args.grid and args.lat is not None:
        print("ERROR: --grid is for the Pilot Core; omit --lat/--lon", file=sys.stderr)
        return 2
    try:
        pilot = load_pilot_config(args.config)
        wind_config = WindConfig(
            preferred_radius_km=args.preferred_radius_km,
            maximum_radius_km=args.maximum_radius_km,
            minimum_stations=args.minimum_stations,
            maximum_stations=args.maximum_stations,
            maximum_temporal_distance_minutes=args.maximum_temporal_distance_minutes,
            idw_power=args.idw_power,
        )
        field = load_wind_snapshot(args.database, None if args.latest else args.at, wind_config)
        core = pilot["core_bbox"]
        if args.lat is None:
            center = ((core["south"] + core["north"]) / 2, (core["west"] + core["east"]) / 2)
            center_estimate = field.estimate(*center)
        else:
            center_estimate = field.estimate(args.lat, args.lon)
        grid = []
        if args.grid or args.lat is None:
            grid = field.estimates_for_grid(grid_points(core, args.grid_spacing_km))
        summary = build_summary(field, center_estimate, grid, database_path=args.database, analysis_scope="Core center and Core Zone grid" if grid else "single WGS84 point")
        args.summary_output.parent.mkdir(parents=True, exist_ok=True)
        args.summary_output.write_text(json.dumps(summary, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
        write_map(args.map_output, pilot, center_estimate, grid)
        print_report(summary, center_estimate, grid, args.map_output, args.summary_output)
    except (WindFieldError, OSError, ValueError, KeyError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
