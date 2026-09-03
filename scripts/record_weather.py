#!/usr/bin/env python3
"""Record all CWA O-A0003-001 stations into an independent DuckDB file."""

from __future__ import annotations

import argparse
import gzip
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import duckdb  # noqa: E402
import airtrace.config  # noqa: E402,F401

from airtrace.data.cwa import (  # noqa: E402
    CWA_API_URL,
    CwaClient,
    CwaError,
    WeatherStation,
    iso_utc,
    parse_response,
)


LOGGER = logging.getLogger("airtrace.weather")
DEFAULT_DATABASE = ROOT / "data" / "weather.duckdb"
DEFAULT_RAW_ROOT = ROOT / "data" / "raw" / "weather"
DEFAULT_INTERVAL_SECONDS = 600.0



def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def ensure_schema(connection: duckdb.DuckDBPyConnection) -> None:
    connection.execute("SET TimeZone='UTC'")
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS weather_station (
            station_id VARCHAR PRIMARY KEY,
            station_name VARCHAR NOT NULL,
            lat DOUBLE,
            lon DOUBLE,
            altitude_m DOUBLE,
            county VARCHAR,
            township VARCHAR,
            quality_flags VARCHAR,
            first_seen_at_utc TIMESTAMPTZ NOT NULL,
            last_seen_at_utc TIMESTAMPTZ NOT NULL
        )
        """
    )
    connection.execute("ALTER TABLE weather_station ADD COLUMN IF NOT EXISTS quality_flags VARCHAR")
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS weather_observation (
            station_id VARCHAR NOT NULL,
            observation_time_utc TIMESTAMPTZ NOT NULL,
            wind_from_deg DOUBLE,
            wind_speed_mps DOUBLE,
            wind_u_east_mps DOUBLE,
            wind_v_north_mps DOUBLE,
            wind_status VARCHAR NOT NULL,
            temperature_c DOUBLE,
            relative_humidity_pct DOUBLE,
            pressure_hpa DOUBLE,
            precipitation_mm DOUBLE,
            ingested_at_utc TIMESTAMPTZ NOT NULL,
            quality_flags VARCHAR,
            PRIMARY KEY (station_id, observation_time_utc)
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS weather_poll (
            poll_started_at_utc TIMESTAMPTZ NOT NULL,
            completed_at_utc TIMESTAMPTZ NOT NULL,
            success BOOLEAN NOT NULL,
            stations_received INTEGER NOT NULL,
            valid_stations INTEGER NOT NULL,
            observations_received INTEGER NOT NULL,
            new_observations_inserted INTEGER NOT NULL,
            duplicates_skipped INTEGER NOT NULL,
            invalid_coordinates INTEGER NOT NULL,
            invalid_wind INTEGER NOT NULL,
            calm INTEGER NOT NULL,
            variable INTEGER NOT NULL,
            api_errors INTEGER NOT NULL,
            raw_snapshot_path VARCHAR,
            error_message VARCHAR
        )
        """
    )


def write_raw_snapshot(raw_root: Path, poll_time: datetime, payload: dict[str, Any]) -> Path:
    directory = raw_root / poll_time.strftime("%Y-%m-%d")
    directory.mkdir(parents=True, exist_ok=True)
    stem = poll_time.strftime("%Y%m%dT%H%M%SZ")
    for suffix in range(1000):
        name = f"{stem}.json.gz" if suffix == 0 else f"{stem}-{suffix:02d}.json.gz"
        path = directory / name
        try:
            with path.open("xb") as raw_handle:
                with gzip.GzipFile(fileobj=raw_handle, mode="wb", mtime=0) as gzip_handle:
                    gzip_handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8"))
            return path
        except FileExistsError:
            continue
    raise OSError(f"could not allocate a non-overwriting raw snapshot name for {stem}")


def upsert_stations(connection: duckdb.DuckDBPyConnection, rows: list[WeatherStation], seen_at: datetime) -> None:
    connection.executemany(
        """
        INSERT INTO weather_station (
            station_id, station_name, lat, lon, altitude_m, county, township,
            first_seen_at_utc, last_seen_at_utc, quality_flags
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (station_id) DO UPDATE SET
            station_name = excluded.station_name,
            lat = excluded.lat,
            lon = excluded.lon,
            altitude_m = excluded.altitude_m,
            county = excluded.county,
            township = excluded.township,
            quality_flags = excluded.quality_flags,
            last_seen_at_utc = excluded.last_seen_at_utc
        """,
        [
            [row.station_id, row.station_name, row.lat, row.lon, row.altitude_m, row.county,
             row.township, seen_at, seen_at, ";".join(row.quality_flags)]
            for row in rows
        ],
    )


def insert_observations(connection: duckdb.DuckDBPyConnection, rows: list[WeatherStation], ingested_at: datetime) -> tuple[int, int]:
    candidates = [row for row in rows if row.observation_time_utc is not None]
    # A defensive in-memory dedupe also prevents a malformed response from
    # making the per-poll counts misleading.
    unique: dict[tuple[str, datetime], WeatherStation] = {
        (row.station_id, row.observation_time_utc): row for row in candidates
    }
    before = int(connection.execute("SELECT count(*) FROM weather_observation").fetchone()[0])
    connection.executemany(
        """
        INSERT INTO weather_observation
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (station_id, observation_time_utc) DO NOTHING
        """,
        [
            [
                row.station_id, row.observation_time_utc, row.wind_from_deg, row.wind_speed_mps,
                row.wind_u_east_mps, row.wind_v_north_mps, row.wind_status, row.temperature_c,
                row.relative_humidity_pct, row.pressure_hpa, row.precipitation_mm, ingested_at,
                ";".join(row.quality_flags),
            ]
            for row in unique.values()
        ],
    )
    after = int(connection.execute("SELECT count(*) FROM weather_observation").fetchone()[0])
    inserted = after - before
    return inserted, len(unique) - inserted


def save_poll_health(connection: duckdb.DuckDBPyConnection, started: datetime, completed: datetime, values: dict[str, Any]) -> None:
    connection.execute(
        """
        INSERT INTO weather_poll
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            started, completed, values.get("success", False), values.get("stations_received", 0),
            values.get("valid_stations", 0), values.get("observations_received", 0),
            values.get("inserted", 0), values.get("duplicates", 0), values.get("invalid_coordinates", 0),
            values.get("invalid_wind", 0), values.get("calm", 0), values.get("variable", 0),
            values.get("api_errors", 0), values.get("raw_path", ""), values.get("error", ""),
        ],
    )


def print_poll(values: dict[str, Any], started: datetime, duration: float) -> None:
    print("AirTrace Weather Recorder")
    print(f"Poll time: {iso_utc(started)}")
    print(f"Stations received: {values.get('stations_received', 0)}")
    print(f"Valid stations: {values.get('valid_stations', 0)}")
    print(f"Observations received: {values.get('observations_received', 0)}")
    print(f"New observations inserted: {values.get('inserted', 0)}")
    print(f"Duplicates skipped: {values.get('duplicates', 0)}")
    print(f"Invalid coordinates: {values.get('invalid_coordinates', 0)}")
    print(f"Invalid wind: {values.get('invalid_wind', 0)}")
    print(f"Calm: {values.get('calm', 0)}")
    print(f"Variable wind: {values.get('variable', 0)}")
    print(f"API errors: {values.get('api_errors', 0)}")
    print(f"Duration: {duration:.2f}s")
    if values.get("raw_path"):
        print(f"Raw snapshot: {values['raw_path']}")
    if values.get("error"):
        print(f"Error: {values['error']}")


def run_once(database_path: Path, raw_root: Path, timeout: float) -> bool:
    started = utc_now()
    database_path.parent.mkdir(parents=True, exist_ok=True)
    connection = duckdb.connect(str(database_path))
    ensure_schema(connection)
    values: dict[str, Any] = {"success": False, "api_errors": 0}
    try:
        api_key = os.environ.get("CWA_API_KEY", "")
        payload = CwaClient(api_key, timeout_seconds=timeout).fetch()
        rows, summary = parse_response(payload)
        values.update({
            "stations_received": summary.stations_received,
            "valid_stations": summary.valid_stations,
            "observations_received": summary.observations_received,
            "invalid_coordinates": summary.invalid_coordinates,
            "invalid_wind": summary.invalid_wind,
            "calm": summary.calm,
            "variable": summary.variable,
        })
        try:
            values["raw_path"] = str(write_raw_snapshot(raw_root, started, payload))
        except OSError as exc:
            values["error"] = f"raw snapshot write failed: {exc}"
            LOGGER.warning("raw snapshot write failed; normalized data will still be committed: %s", exc)
        connection.begin()
        try:
            upsert_stations(connection, rows, started)
            values["inserted"], values["duplicates"] = insert_observations(connection, rows, started)
            connection.commit()
            values["success"] = True
        except Exception:
            connection.rollback()
            raise
    except (CwaError, OSError, ValueError, KeyError, duckdb.Error) as exc:
        values["api_errors"] = 1
        values["error"] = str(exc)
        LOGGER.error("poll failed; waiting for next cycle: %s", exc)
    finally:
        completed = utc_now()
        try:
            save_poll_health(connection, started, completed, values)
        finally:
            connection.close()
    print_poll(values, started, (completed - started).total_seconds())
    return bool(values["success"])


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true", help="poll once and exit")
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE)
    parser.add_argument("--raw-root", type=Path, default=DEFAULT_RAW_ROOT)
    parser.add_argument("--interval", type=float, default=DEFAULT_INTERVAL_SECONDS)
    parser.add_argument("--timeout", type=float, default=30.0)
    return parser.parse_args()


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    args = parse_args()
    if args.timeout <= 0 or args.interval <= 0:
        print("ERROR: --timeout and --interval must be positive", file=sys.stderr)
        return 2
    if args.once:
        return 0 if run_once(args.database, args.raw_root, args.timeout) else 1
    print(f"AirTrace Weather Recorder continuous mode; interval={args.interval:.0f}s", flush=True)
    while True:
        try:
            run_once(args.database, args.raw_root, args.timeout)
            time.sleep(args.interval)
        except KeyboardInterrupt:
            print("Recorder stopped.")
            return 0


if __name__ == "__main__":
    raise SystemExit(main())
