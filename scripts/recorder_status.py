#!/usr/bin/env python3
"""Show a compact health summary for the AirTrace recorder."""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import duckdb  # noqa: E402

DEFAULT_DATABASE = ROOT / "data" / "airtrace.duckdb"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    print("AirTrace Recorder Status")
    print(f"Database exists: {args.database.exists()}")
    if not args.database.exists():
        return 1
    print(f"Database path: {args.database}")
    print(f"Storage file size: {args.database.stat().st_size} bytes")
    connection = duckdb.connect(str(args.database), read_only=True)
    try:
        connection.execute("SET TimeZone='UTC'")
        first, latest, total = connection.execute(
            """
            SELECT CAST(min(phenomenon_time_utc) AS VARCHAR), CAST(max(phenomenon_time_utc) AS VARCHAR), count(*)
            FROM pm25_observation
            """
        ).fetchone()
        sensors = connection.execute("SELECT count(*) FROM sensor_station").fetchone()[0]
        last_10m, last_1h = connection.execute(
            """
            SELECT
              count(*) FILTER (WHERE phenomenon_time_utc >= CURRENT_TIMESTAMP - INTERVAL '10 minutes'),
              count(*) FILTER (WHERE phenomenon_time_utc >= CURRENT_TIMESTAMP - INTERVAL '1 hour')
            FROM pm25_observation
            """
        ).fetchone()
        poll = connection.execute(
            """
            SELECT CAST(poll_started_at_utc AS VARCHAR), CAST(completed_at_utc AS VARCHAR), success, sensors_queried,
                   observations_received, new_observations_inserted, duplicates_skipped,
                   http_api_errors, raw_snapshot_path, error_message
            FROM recorder_poll ORDER BY poll_started_at_utc DESC LIMIT 1
            """
        ).fetchone()
        print(f"First observation timestamp: {first}")
        print(f"Latest observation timestamp: {latest}")
        print(f"Total unique observations: {total}")
        print(f"Unique sensors seen: {sensors}")
        print(f"Observations in last 10 min: {last_10m}")
        print(f"Observations in last 1 h: {last_1h}")
        if poll:
            print(f"Latest poll health: {'success' if poll[2] else 'failed'}")
            print(f"Latest poll time: {poll[0]}")
            print(f"Latest poll sensors/observations/new/duplicates: {poll[3]}/{poll[4]}/{poll[5]}/{poll[6]}")
            print(f"Latest poll HTTP/API errors: {poll[7]}")
            print(f"Latest raw snapshot: {poll[8] or '(none)'}")
            if poll[9]:
                print(f"Latest poll note: {poll[9]}")
        else:
            print("Latest poll health: no poll recorded")
    finally:
        connection.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
