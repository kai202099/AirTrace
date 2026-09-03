#!/usr/bin/env python3
"""Replay PM2.5 anomaly v1 results into explainable spatiotemporal events."""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from airtrace.analysis.anomaly import latest_observation_time, parse_iso_utc  # noqa: E402
from airtrace.analysis.events import (  # noqa: E402
    EventConfig,
    run_event_analysis,
    write_event_csv,
    write_event_json,
    write_event_map,
    write_event_timeline,
    write_membership_csv,
)


DEFAULT_DATABASE = ROOT / "data" / "airtrace.duckdb"
DEFAULT_CONFIG = ROOT / "config" / "pilot_region.json"
DEFAULT_OUTPUT_DIR = ROOT / "reports" / "events"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument("--last-hours", type=float, help="analyze a window ending at the latest observation")
    selection.add_argument("--latest", action="store_true", help="analyze the latest 90 minutes")
    selection.add_argument("--start", metavar="ISO_UTC", help="explicit replay start time; pair with --end")
    parser.add_argument("--end", metavar="ISO_UTC", help="explicit replay end time")
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--analysis-zone", choices=("core", "context"), default="core", help="target zone for analysis; default is the production Core Zone")
    parser.add_argument("--lookback-hours", type=float, default=2.0, help="anomaly v1 source lookback; must cover its 60-minute baseline")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    return parser.parse_args()


def resolve_window(args: argparse.Namespace) -> tuple[datetime, datetime]:
    if args.start and not args.end or args.end and not args.start:
        raise ValueError("--start and --end must be supplied together")
    if args.last_hours is not None and args.last_hours <= 0:
        raise ValueError("--last-hours must be positive")
    if args.lookback_hours <= 0:
        raise ValueError("--lookback-hours must be positive")
    if args.start:
        start, end = parse_iso_utc(args.start), parse_iso_utc(args.end)
        if end < start:
            raise ValueError("--end must be at or after --start")
        return start, end
    end = latest_observation_time(args.database)
    hours = args.last_hours if args.last_hours is not None else 1.5
    return end - timedelta(hours=hours), end


def main() -> int:
    args = parse_args()
    try:
        start, end = resolve_window(args)
        payload = run_event_analysis(
            database_path=args.database,
            config_path=args.config,
            start_time=start,
            end_time=end,
            anomaly_lookback_hours=max(1.0, args.lookback_hours),
            event_config=EventConfig(),
            analysis_zone=args.analysis_zone,
        )
        output_dir = args.output_dir
        json_path = output_dir / "latest_events.json"
        csv_path = output_dir / "latest_events.csv"
        membership_path = output_dir / "latest_event_membership.csv"
        map_path = output_dir / "latest_event_map.html"
        timeline_path = output_dir / "latest_event_timeline.html"
        write_event_json(payload, json_path)
        write_event_csv(payload, csv_path)
        write_membership_csv(payload, membership_path)
        write_event_map(payload, map_path)
        write_event_timeline(payload, timeline_path)

        summary = payload["summary"]
        if args.analysis_zone == "context":
            print("CONTEXT DIAGNOSTIC REPLAY")
            print("NOT PRODUCTION EVENT DETECTION")
        print("AirTrace PM2.5 Spatiotemporal Event Clustering v1")
        print(f"Analysis window: {payload['analysis_window']['start_time_utc']} → {payload['analysis_window']['end_time_utc']}")
        print(f"Bins analyzed: {summary['bins_analyzed']}")
        if args.analysis_zone == "context":
            print(f"Context sensors: {summary['context_sensor_count']}")
            print(f"Usable sensors: {summary['usable_sensor_count']}")
        print(f"Real seed count: {summary['seed_count']}")
        print(f"Event count: {summary['event_count']}")
        print(f"Transient count: {summary['transient_count']}")
        print(f"Isolated count: {summary['isolated_count']}")
        if summary["event_count"] == 0:
            print("NO EVENTS DETECTED")
        print(f"JSON: {json_path}")
        print(f"CSV: {csv_path}")
        print(f"Membership CSV: {membership_path}")
        print(f"Map: {map_path}")
        print(f"Timeline: {timeline_path}")
        return 0
    except (OSError, ValueError, KeyError, RuntimeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
