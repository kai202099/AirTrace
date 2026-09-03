#!/usr/bin/env python3
"""Run the AirTrace Analysis / Replay Orchestrator v1."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from airtrace.analysis.anomaly import parse_iso_utc  # noqa: E402
from airtrace.pipeline import run_analysis  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument("--latest", action="store_true", help="analyze the latest 90 minutes as LIVE_ANALYSIS")
    selection.add_argument("--start", help="explicit replay start, timezone-aware ISO-8601")
    parser.add_argument("--end", help="explicit replay end, required with --start")
    parser.add_argument("--analysis-zone", choices=("core", "context"), default="core")
    parser.add_argument("--event-id")
    parser.add_argument("--fast-preview", action="store_true", help="opt in to 500 m quantized backtrace wind cache")
    parser.add_argument("--lookback-hours", type=float, default=2.0, help="anomaly source lookback; baseline semantics remain unchanged")
    parser.add_argument("--output-name")
    parser.add_argument("--database", type=Path, default=ROOT / "data" / "airtrace.duckdb")
    parser.add_argument("--weather-database", type=Path, default=ROOT / "data" / "weather.duckdb")
    parser.add_argument("--config", type=Path, default=ROOT / "config" / "pilot_region.json")
    parser.add_argument("--output-root", type=Path, default=ROOT / "reports" / "pipeline")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if bool(args.start) != bool(args.end):
        print("ERROR: --start and --end must be supplied together", file=sys.stderr)
        return 2
    if not args.latest and not args.start:
        args.latest = True
    try:
        result = run_analysis(
            parse_iso_utc(args.start) if args.start else None,
            parse_iso_utc(args.end) if args.end else None,
            analysis_zone=args.analysis_zone,
            event_id=args.event_id,
            latest=args.latest,
            database_path=args.database,
            weather_database_path=args.weather_database,
            config_path=args.config,
            output_root=args.output_root,
            output_name=args.output_name,
            fast_preview=args.fast_preview,
            anomaly_lookback_hours=args.lookback_hours,
        )
        summary = result["summary"]
        if args.analysis_zone == "context":
            print("CONTEXT DIAGNOSTIC REPLAY")
            print("NOT PRODUCTION EVENT DETECTION")
        print("AirTrace Analysis Pipeline v1")
        print(f"Mode: {summary['mode']}")
        print(f"Analysis window: {summary['analysis_window']['start_time_utc']} → {summary['analysis_window']['end_time_utc']}")
        print(f"Event count: {summary['event_count']}")
        if summary["message"]:
            print(summary["message"])
        print(f"Output: {result['output_dir']}")
        print(json.dumps(summary["stage_status"], ensure_ascii=False, sort_keys=True))
        return 0
    except (OSError, ValueError, KeyError, RuntimeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
