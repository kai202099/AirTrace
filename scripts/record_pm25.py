#!/usr/bin/env python3
"""Record latest PM2.5 observations for the configured AirTrace region."""

from __future__ import annotations

import argparse
import gzip
import json
import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import duckdb  # noqa: E402

from airtrace.data.sensorthings import (  # noqa: E402
    API_BASE_URL,
    ApiClient,
    RegionConfig,
    SensorThingsError,
    as_bool,
    fetch_pm25_records,
    iso_utc,
)


DEFAULT_CONFIG = ROOT / "config" / "pilot_region.json"
DEFAULT_DATABASE = ROOT / "data" / "airtrace.duckdb"
DEFAULT_RAW_ROOT = ROOT / "data" / "raw" / "sensors"
POLL_INTERVAL_SECONDS = 180
LOGGER = logging.getLogger("airtrace.recorder")


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def ensure_schema(connection: duckdb.DuckDBPyConnection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS sensor_station (
            thing_id VARCHAR NOT NULL,
            station_id VARCHAR NOT NULL PRIMARY KEY,
            station_name VARCHAR,
            datastream_id VARCHAR,
            lat DOUBLE,
            lon DOUBLE,
            city VARCHAR,
            township VARCHAR,
            area_type VARCHAR,
            area_description VARCHAR,
            is_outdoor BOOLEAN,
            is_mobile BOOLEAN,
            project_name VARCHAR,
            first_seen_at TIMESTAMPTZ NOT NULL,
            last_seen_at TIMESTAMPTZ NOT NULL
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS pm25_observation (
            station_id VARCHAR NOT NULL,
            datastream_id VARCHAR NOT NULL,
            phenomenon_time_utc TIMESTAMPTZ NOT NULL,
            pm25_ugm3 DOUBLE NOT NULL,
            ingested_at_utc TIMESTAMPTZ NOT NULL,
            source_status VARCHAR,
            quality_flags VARCHAR,
            PRIMARY KEY (datastream_id, phenomenon_time_utc)
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS recorder_poll (
            poll_started_at_utc TIMESTAMPTZ PRIMARY KEY,
            completed_at_utc TIMESTAMPTZ,
            success BOOLEAN NOT NULL,
            sensors_queried INTEGER DEFAULT 0,
            observations_received INTEGER DEFAULT 0,
            new_observations_inserted INTEGER DEFAULT 0,
            duplicates_skipped INTEGER DEFAULT 0,
            missing INTEGER DEFAULT 0,
            invalid INTEGER DEFAULT 0,
            fresh INTEGER DEFAULT 0,
            stale INTEGER DEFAULT 0,
            clock_ahead INTEGER DEFAULT 0,
            http_api_errors INTEGER DEFAULT 0,
            raw_snapshot_path VARCHAR,
            error_message VARCHAR
        )
        """
    )


def write_raw_snapshot(raw_root: Path, poll_time: datetime, payload: dict[str, Any]) -> Path:
    directory = raw_root / poll_time.strftime("%Y-%m-%d")
    directory.mkdir(parents=True, exist_ok=True)
    stem = poll_time.strftime("%Y%m%dT%H%M%SZ")
    path = directory / f"{stem}.json.gz"
    suffix = 1
    while path.exists():
        path = directory / f"{stem}-{suffix:02d}.json.gz"
        suffix += 1
    with gzip.open(path, "xb") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    return path


def upsert_stations(connection: duckdb.DuckDBPyConnection, rows: list[dict[str, Any]], now: datetime) -> None:
    for row in rows:
        connection.execute(
            """
            INSERT INTO sensor_station VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (station_id) DO UPDATE SET
                thing_id = excluded.thing_id,
                station_name = excluded.station_name,
                datastream_id = excluded.datastream_id,
                lat = excluded.lat,
                lon = excluded.lon,
                city = excluded.city,
                township = excluded.township,
                area_type = excluded.area_type,
                area_description = excluded.area_description,
                is_outdoor = excluded.is_outdoor,
                is_mobile = excluded.is_mobile,
                project_name = excluded.project_name,
                last_seen_at = excluded.last_seen_at
            """,
            [
                row["thing_id"], row["station_id"], row["station_name"], row["datastream_id"],
                row["lat"], row["lon"], row["city"], row["township"], row["area_type"],
                row["area_description"], as_bool(row["is_outdoor"]), as_bool(row["is_mobile"]),
                row["project_name"], now, now,
            ],
        )


def insert_observations(connection: duckdb.DuckDBPyConnection, rows: list[dict[str, Any]], now: datetime) -> tuple[int, int]:
    candidates: list[dict[str, Any]] = []
    seen: set[tuple[str, datetime]] = set()
    for row in rows:
        phenomenon = row.get("phenomenon_time_utc")
        pm25 = row.get("pm25_ugm3")
        datastream_id = str(row.get("datastream_id", ""))
        if phenomenon is None or pm25 is None or not datastream_id:
            continue
        key = (datastream_id, phenomenon)
        if key not in seen:
            seen.add(key)
            candidates.append(row)
    before = int(connection.execute("SELECT count(*) FROM pm25_observation").fetchone()[0])
    connection.executemany(
        """
        INSERT INTO pm25_observation
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (datastream_id, phenomenon_time_utc) DO NOTHING
        """,
        [
            [
                row["station_id"], row["datastream_id"], row["phenomenon_time_utc"],
                row["pm25_ugm3"], now, row["freshness_status"], row["quality_flags"],
            ]
            for row in candidates
        ],
    )
    after = int(connection.execute("SELECT count(*) FROM pm25_observation").fetchone()[0])
    inserted = after - before
    return inserted, len(candidates) - inserted


def summarize(rows: list[dict[str, Any]], quality: dict[str, Any]) -> dict[str, int]:
    freshness = {"fresh": 0, "stale": 0, "clock_ahead": 0}
    for row in rows:
        status = row.get("freshness_status")
        if status in freshness:
            freshness[status] += 1
    invalid = sum(
        int(quality[key])
        for key in ("invalid_coordinate", "invalid_pm25", "invalid_timestamp")
    )
    return {
        "sensors": len(rows),
        "observations": sum(row.get("observation") is not None for row in rows),
        "missing": int(quality["missing_observation"]),
        "invalid": invalid,
        "fresh": freshness["fresh"],
        "stale": freshness["stale"],
        "clock_ahead": freshness["clock_ahead"],
    }


def save_poll_health(connection: duckdb.DuckDBPyConnection, started: datetime, completed: datetime, values: dict[str, Any]) -> None:
    connection.execute(
        """
        INSERT INTO recorder_poll
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            started, completed, values.get("success", False), values.get("sensors", 0),
            values.get("observations", 0), values.get("inserted", 0), values.get("duplicates", 0),
            values.get("missing", 0), values.get("invalid", 0), values.get("fresh", 0),
            values.get("stale", 0), values.get("clock_ahead", 0), values.get("api_errors", 0),
            values.get("raw_path", ""), values.get("error", ""),
        ],
    )


def record_poll_health(database_path: Path, started: datetime, completed: datetime, values: dict[str, Any]) -> None:
    """Persist one poll-health row with a connection scoped to that write."""

    connection: duckdb.DuckDBPyConnection | None = None
    try:
        connection = duckdb.connect(str(database_path))
        ensure_schema(connection)
        connection.begin()
        try:
            save_poll_health(connection, started, completed, values)
            connection.commit()
        except Exception:
            connection.rollback()
            raise
    finally:
        if connection is not None:
            connection.close()


def print_poll(values: dict[str, Any], started: datetime, duration: float) -> None:
    print("AirTrace PM2.5 Recorder")
    print(f"Poll time: {iso_utc(started)}")
    print(f"Sensors queried: {values.get('sensors', 0)}")
    print(f"Observations received: {values.get('observations', 0)}")
    print(f"New observations inserted: {values.get('inserted', 0)}")
    print(f"Duplicates skipped: {values.get('duplicates', 0)}")
    print(f"Missing: {values.get('missing', 0)}")
    print(f"Invalid: {values.get('invalid', 0)}")
    print(f"Fresh: {values.get('fresh', 0)}")
    print(f"Stale: {values.get('stale', 0)}")
    print(f"Clock-ahead: {values.get('clock_ahead', 0)}")
    print(f"HTTP/API errors: {values.get('api_errors', 0)}")
    print(f"Duration: {duration:.2f}s")
    if values.get("future_ahead_seconds"):
        print("Future timestamp ahead seconds: " + ", ".join(f"{item:.3f}" for item in values["future_ahead_seconds"]))
    if values.get("raw_path"):
        print(f"Raw snapshot: {values['raw_path']}")
    if values.get("error"):
        print(f"Error: {values['error']}")


def run_once(config_path: Path, database_path: Path, raw_root: Path, timeout: float) -> bool:
    started = utc_now()
    database_path.parent.mkdir(parents=True, exist_ok=True)
    values: dict[str, Any] = {"success": False, "api_errors": 0}
    completed = started
    try:
        region = RegionConfig.load(config_path)
        client = ApiClient(API_BASE_URL, timeout_seconds=timeout)
        rows, details = fetch_pm25_records(client, region, started)
        values.update(summarize(rows, details["quality"]))
        values["future_ahead_seconds"] = details["quality"]["future_ahead_seconds"]
        raw_payload = {
            "schema_version": 1,
            "region": {"region_id": region.region_id, "name": region.name, "context_bbox": region.bbox, "timezone": region.timezone},
            "source": API_BASE_URL,
            "poll_time_utc": iso_utc(started),
            "things_pages": details["things_pages"],
            "api_reported_count": details["api_reported_count"],
            "nested_latest_used": details["nested_latest_used"],
            "fallback_problems": details["fallback_problems"],
            "things": details["things"],
        }
        try:
            values["raw_path"] = str(write_raw_snapshot(raw_root, started, raw_payload))
        except OSError as exc:
            values["error"] = f"raw snapshot write failed: {exc}"
            LOGGER.exception("raw snapshot write failed; normalized data will still be committed")
        completed = utc_now()
        connection: duckdb.DuckDBPyConnection | None = None
        try:
            connection = duckdb.connect(str(database_path))
            ensure_schema(connection)
            connection.begin()
            upsert_stations(connection, rows, started)
            values["inserted"], values["duplicates"] = insert_observations(connection, rows, started)
            values["success"] = True
            save_poll_health(connection, started, completed, values)
            connection.commit()
        except Exception:
            values["success"] = False
            if connection is not None:
                connection.rollback()
            raise
        finally:
            if connection is not None:
                connection.close()
    except (SensorThingsError, OSError, ValueError, KeyError, duckdb.Error) as exc:
        values["api_errors"] = 1
        values["error"] = str(exc)
        LOGGER.error("poll failed; waiting for next cycle: %s", exc)
        completed = utc_now()
        try:
            record_poll_health(database_path, started, completed, values)
        except Exception as health_exc:
            LOGGER.error("poll health write failed: %s", health_exc)
    else:
        # The success path records health in the same short transaction as the
        # normalized rows; this timestamp is only used for the terminal report.
        completed = max(completed, utc_now())
    print_poll(values, started, (completed - started).total_seconds())
    return bool(values["success"])


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true", help="poll once and exit")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE)
    parser.add_argument("--raw-root", type=Path, default=DEFAULT_RAW_ROOT)
    parser.add_argument("--interval", type=float, default=POLL_INTERVAL_SECONDS)
    parser.add_argument("--timeout", type=float, default=30.0)
    return parser.parse_args()


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    args = parse_args()
    if args.timeout <= 0 or args.interval <= 0:
        print("ERROR: --timeout and --interval must be positive", file=sys.stderr)
        return 2
    if args.once:
        return 0 if run_once(args.config, args.database, args.raw_root, args.timeout) else 1
    print(f"AirTrace PM2.5 Recorder continuous mode; interval={args.interval:.0f}s", flush=True)
    while True:
        try:
            run_once(args.config, args.database, args.raw_root, args.timeout)
            time.sleep(args.interval)
        except KeyboardInterrupt:
            print("Recorder stopped.")
            return 0


if __name__ == "__main__":
    raise SystemExit(main())
