#!/usr/bin/env python3
"""Run Backward Particle Tracing v1 for a detected event or manual receptor."""

from __future__ import annotations

import argparse
import csv
import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from airtrace.analysis.backtrace import (  # noqa: E402
    BacktraceConfig,
    no_traceable_event_payload,
    trace_event,
    write_evidence_csv,
    write_trace_json,
    write_trace_map,
)

DEFAULT_EVENTS = ROOT / "reports" / "events" / "latest_events.json"
DEFAULT_MEMBERSHIP = ROOT / "reports" / "events" / "latest_event_membership.csv"
DEFAULT_OUTPUT = ROOT / "reports" / "source_trace"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument("--latest", action="store_true", help="trace the first traceable event in latest_events.json")
    selection.add_argument("--event-id", help="trace this event ID from the event JSON")
    parser.add_argument("--events", type=Path, default=DEFAULT_EVENTS)
    parser.add_argument("--membership", type=Path, default=DEFAULT_MEMBERSHIP)
    parser.add_argument("--database", type=Path, default=ROOT / "data" / "weather.duckdb")
    parser.add_argument("--region-config", type=Path, default=ROOT / "config" / "pilot_region.json")
    parser.add_argument("--residuals", type=Path, default=ROOT / "reports" / "wind_validation" / "wind_validation_samples.csv")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--particles-per-receptor", type=int, default=200)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--dt-seconds", type=int, default=60)
    parser.add_argument("--backtrace-minutes", type=int, default=60)
    parser.add_argument("--grid-cell-m", type=float, default=250.0)
    parser.add_argument("--domain-buffer-km", type=float, default=10.0)
    parser.add_argument("--display-trajectory-limit", type=int, default=120)
    parser.add_argument("--no-wind-cache", action="store_true", help="disable v1.1 quantized wind cache and use strict/reference resolution")
    parser.add_argument("--wind-cache-spatial-m", type=float, choices=(250.0, 500.0), default=250.0)
    parser.add_argument("--wind-cache-temporal-seconds", type=int, default=60)
    parser.add_argument("--quantized-wind-cache", action="store_true", help="enable spatially quantized wind cache (accuracy tradeoff; default is exact-coordinate cache)")
    parser.add_argument("--profile", action="store_true", help="write reports/performance/backtrace_profile.json")
    parser.add_argument("--manual-lat", type=float)
    parser.add_argument("--manual-lon", type=float)
    parser.add_argument("--at", dest="manual_at", help="timezone-aware ISO time for manual diagnostic")
    return parser.parse_args()


def _read_json(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def _read_membership(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def _event_and_memberships(args: argparse.Namespace) -> tuple[dict | None, list[dict], bool]:
    manual = args.manual_lat is not None or args.manual_lon is not None or args.manual_at is not None
    if manual:
        if args.manual_lat is None or args.manual_lon is None or not args.manual_at:
            raise ValueError("manual mode requires --manual-lat, --manual-lon, and --at")
        event = {"event_id": "manual-diagnostic", "event_status": "manual", "manual_diagnostic": True, "note": "MANUAL DIAGNOSTIC — NOT DETECTED EVENT"}
        membership = [{"event_id": event["event_id"], "time_bin": args.manual_at, "station_id": "manual-receptor", "role": "seed", "anomaly_score": 1.0, "pm25": None, "lat": args.manual_lat, "lon": args.manual_lon}]
        return event, membership, True
    payload = _read_json(args.events)
    events = payload.get("events", [])
    if args.event_id:
        events = [event for event in events if event.get("event_id") == args.event_id]
    event = events[0] if events else None
    memberships = payload.get("membership") or _read_membership(args.membership)
    return event, memberships, False


def main() -> int:
    args = parse_args()
    output_dir = args.output_dir
    try:
        event, memberships, manual = _event_and_memberships(args)
        if event is None:
            result = no_traceable_event_payload()
            print("NO TRACEABLE EVENT")
        else:
            result = trace_event(event, memberships, BacktraceConfig(
                database_path=args.database, region_config_path=args.region_config, residual_csv_path=args.residuals,
                particles_per_receptor=args.particles_per_receptor, random_seed=args.seed, dt_seconds=args.dt_seconds,
                maximum_backtrace_minutes=args.backtrace_minutes, grid_cell_m=args.grid_cell_m,
                domain_buffer_km=args.domain_buffer_km, display_trajectory_limit=args.display_trajectory_limit,
                wind_cache_enabled=not args.no_wind_cache, wind_cache_spatial_m=args.wind_cache_spatial_m,
                wind_cache_temporal_seconds=args.wind_cache_temporal_seconds, wind_cache_quantized=args.quantized_wind_cache,
                profile=args.profile,
            ))
            if manual:
                result["manual_diagnostic_notice"] = "MANUAL DIAGNOSTIC — NOT DETECTED EVENT"
                print("MANUAL DIAGNOSTIC — NOT DETECTED EVENT")
            print(f"Trace status: {result['status']}")
            stats = result.get("particle_stats", {})
            print(f"Receptors: {stats.get('receptor_count', 0)} · particles: {stats.get('particle_count', 0)} · steps/particle: {stats.get('integration_steps_per_particle', 0)}")
            print(f"Candidate source regions: {len(result.get('candidate_source_regions', []))}")
        performance = result.pop("_performance_profile", None)
        if performance is not None:
            profile_path = ROOT / "reports" / "performance" / "backtrace_profile.json"
            profile_path.parent.mkdir(parents=True, exist_ok=True)
            profile_path.write_text(json.dumps({"schema_version": 1, "mode": "strict" if args.no_wind_cache else "optimized", "profile": performance}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            print(f"Profile: {profile_path}")
        json_path = output_dir / "latest_source_trace.json"
        csv_path = output_dir / "latest_source_evidence.csv"
        map_path = output_dir / "latest_source_trace.html"
        write_trace_json(result, json_path)
        write_evidence_csv(result, csv_path)
        write_trace_map(result, map_path)
        print(f"JSON: {json_path}")
        print(f"Grid: {csv_path}")
        print(f"Map: {map_path}")
        return 0
    except (OSError, ValueError, KeyError, RuntimeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
