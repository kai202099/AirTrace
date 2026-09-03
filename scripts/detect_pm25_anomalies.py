#!/usr/bin/env python3
"""Detect explainable PM2.5 sensor-level anomaly candidates from local DuckDB data."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from airtrace.analysis.anomaly import (  # noqa: E402
    AnomalyConfig,
    detect_anomalies,
    parse_iso_utc,
    print_terminal_report,
    write_csv,
    write_json,
    write_map,
)

DEFAULT_DATABASE = ROOT / "data" / "airtrace.duckdb"
DEFAULT_CONFIG = ROOT / "config" / "pilot_region.json"
DEFAULT_OUTPUT_DIR = ROOT / "reports" / "anomaly"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument("--latest", action="store_true", help="analyze the latest available phenomenon time")
    selection.add_argument("--at", metavar="ISO_UTC", help="analyze observations at or before this UTC timestamp")
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--analysis-zone", choices=("core", "context"), default="core", help="target zone for analysis; default is the production Core Zone")
    parser.add_argument("--lookback-hours", type=float, default=24.0, help="analysis data query window; baseline remains 60 minutes")
    parser.add_argument("--json-output", type=Path, default=DEFAULT_OUTPUT_DIR / "latest_anomaly_report.json")
    parser.add_argument("--csv-output", type=Path, default=DEFAULT_OUTPUT_DIR / "latest_anomaly_report.csv")
    parser.add_argument("--html-output", type=Path, default=DEFAULT_OUTPUT_DIR / "latest_anomaly_map.html")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        cutoff = parse_iso_utc(args.at) if args.at else None
        result = detect_anomalies(
            database_path=args.database,
            config_path=args.config,
            cutoff=cutoff,
            latest=bool(args.latest or not args.at),
            lookback_hours=args.lookback_hours,
            config=AnomalyConfig(),
            analysis_zone=args.analysis_zone,
        )
        write_json(result, args.json_output)
        write_csv(result, args.csv_output)
        write_map(result, args.html_output)
        print_terminal_report(result, args.json_output, args.csv_output, args.html_output)
        return 0
    except (OSError, ValueError, KeyError, RuntimeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
