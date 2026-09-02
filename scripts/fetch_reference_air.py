#!/usr/bin/env python3
"""Fetch one MOENV AQX_P_432 snapshot into the independent reference-air DB."""

from __future__ import annotations

import argparse
import gzip
import json
import logging
import os
import statistics
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import duckdb  # noqa: E402
from dotenv import load_dotenv  # noqa: E402

from airtrace.data.moenv import (  # noqa: E402
    MOENV_API_URL,
    MOENV_DATASET,
    MoenvClient,
    MoenvError,
    ReferenceAirRecord,
    haversine_km,
    iso_utc,
    parse_record,
)


LOGGER = logging.getLogger("airtrace.reference_air")
DEFAULT_CONFIG = ROOT / "config" / "pilot_region.json"
DEFAULT_DATABASE = ROOT / "data" / "reference_air.duckdb"
DEFAULT_RAW_ROOT = ROOT / "data" / "raw" / "reference_air"
load_dotenv(ROOT / ".env")


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def ensure_schema(connection: duckdb.DuckDBPyConnection) -> None:
    connection.execute("SET TimeZone='UTC'")
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS reference_air_station (
            site_id VARCHAR PRIMARY KEY,
            site_name VARCHAR,
            county VARCHAR,
            lat DOUBLE,
            lon DOUBLE,
            first_seen_at_utc TIMESTAMPTZ NOT NULL,
            last_seen_at_utc TIMESTAMPTZ NOT NULL,
            quality_flags VARCHAR
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS reference_air_observation (
            site_id VARCHAR NOT NULL,
            publish_time_utc TIMESTAMPTZ NOT NULL,
            aqi DOUBLE,
            status VARCHAR,
            primary_pollutant VARCHAR,
            pm25_ugm3 DOUBLE,
            pm25_avg_ugm3 DOUBLE,
            pm10_ugm3 DOUBLE,
            pm10_avg_ugm3 DOUBLE,
            so2_ppb DOUBLE,
            so2_avg_ppb DOUBLE,
            no2_ppb DOUBLE,
            nox_ppb DOUBLE,
            no_ppb DOUBLE,
            co_ppm DOUBLE,
            co_8hr_ppm DOUBLE,
            o3_ppb DOUBLE,
            o3_8hr_ppb DOUBLE,
            wind_speed_mps DOUBLE,
            wind_direction_deg DOUBLE,
            ingested_at_utc TIMESTAMPTZ NOT NULL,
            quality_flags VARCHAR,
            PRIMARY KEY (site_id, publish_time_utc)
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS reference_air_ingest (
            ingest_started_at_utc TIMESTAMPTZ NOT NULL,
            completed_at_utc TIMESTAMPTZ NOT NULL,
            success BOOLEAN NOT NULL,
            api_records_received INTEGER NOT NULL,
            parsed_records INTEGER NOT NULL,
            pages_fetched INTEGER NOT NULL,
            invalid_coordinate_records INTEGER NOT NULL,
            malformed_records INTEGER NOT NULL,
            observations_received INTEGER NOT NULL,
            new_observations_inserted INTEGER NOT NULL,
            duplicates_skipped INTEGER NOT NULL,
            raw_snapshot_path VARCHAR,
            warning_message VARCHAR
        )
        """
    )


def write_raw_snapshot(raw_root: Path, fetch_time: datetime, payload: dict[str, Any]) -> Path:
    directory = raw_root / fetch_time.strftime("%Y-%m-%d")
    directory.mkdir(parents=True, exist_ok=True)
    stem = fetch_time.strftime("%Y%m%dT%H%M%SZ")
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


def _station_rows(records: list[ReferenceAirRecord]) -> list[ReferenceAirRecord]:
    latest: dict[str, ReferenceAirRecord] = {}
    for record in records:
        current = latest.get(record.site_id)
        if current is None or (record.publish_time_utc or datetime.min.replace(tzinfo=timezone.utc)) >= (
            current.publish_time_utc or datetime.min.replace(tzinfo=timezone.utc)
        ):
            latest[record.site_id] = record
    return list(latest.values())


def upsert_stations(connection: duckdb.DuckDBPyConnection, records: list[ReferenceAirRecord], seen_at: datetime) -> None:
    rows = _station_rows(records)
    connection.executemany(
        """
        INSERT INTO reference_air_station
            (site_id, site_name, county, lat, lon, first_seen_at_utc, last_seen_at_utc, quality_flags)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (site_id) DO UPDATE SET
            site_name = excluded.site_name,
            county = excluded.county,
            lat = excluded.lat,
            lon = excluded.lon,
            last_seen_at_utc = excluded.last_seen_at_utc,
            quality_flags = excluded.quality_flags
        """,
        [
            [
                row.site_id, row.site_name, row.county, row.lat, row.lon, seen_at, seen_at,
                ";".join(row.quality_flags),
            ]
            for row in rows
        ],
    )


def insert_observations(
    connection: duckdb.DuckDBPyConnection,
    records: list[ReferenceAirRecord],
    ingested_at: datetime,
) -> tuple[int, int]:
    unique: dict[tuple[str, datetime], ReferenceAirRecord] = {}
    for row in records:
        if row.publish_time_utc is not None:
            unique[(row.site_id, row.publish_time_utc)] = row
    before = int(connection.execute("SELECT count(*) FROM reference_air_observation").fetchone()[0])
    connection.executemany(
        """
        INSERT INTO reference_air_observation VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (site_id, publish_time_utc) DO NOTHING
        """,
        [
            [
                row.site_id, row.publish_time_utc, row.aqi, row.status, row.primary_pollutant,
                row.pm25_ugm3, row.pm25_avg_ugm3, row.pm10_ugm3, row.pm10_avg_ugm3,
                row.so2_ppb, row.so2_avg_ppb, row.no2_ppb, row.nox_ppb, row.no_ppb,
                row.co_ppm, row.co_8hr_ppm, row.o3_ppb, row.o3_8hr_ppb,
                row.wind_speed_mps, row.wind_direction_deg, ingested_at, ";".join(row.quality_flags),
            ]
            for row in unique.values()
        ],
    )
    after = int(connection.execute("SELECT count(*) FROM reference_air_observation").fetchone()[0])
    inserted = after - before
    return inserted, len(unique) - inserted


def core_center(config_path: Path) -> tuple[float, float]:
    config = json.loads(config_path.read_text(encoding="utf-8"))
    bbox = config["core_bbox"]
    return ((float(bbox["north"]) + float(bbox["south"])) / 2, (float(bbox["west"]) + float(bbox["east"])) / 2)


def percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * fraction
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def pilot_rows(records: list[ReferenceAirRecord], center: tuple[float, float]) -> list[dict[str, Any]]:
    latest: dict[str, ReferenceAirRecord] = {}
    for record in records:
        if record.lat is None or record.lon is None:
            continue
        previous = latest.get(record.site_id)
        if previous is None or (record.publish_time_utc or datetime.min.replace(tzinfo=timezone.utc)) >= (
            previous.publish_time_utc or datetime.min.replace(tzinfo=timezone.utc)
        ):
            latest[record.site_id] = record
    center_lon, center_lat = center[1], center[0]
    rows = []
    for record in latest.values():
        rows.append({
            "record": record,
            "distance_km": haversine_km((center_lon, center_lat), (record.lon, record.lat)),
        })
    return sorted(rows, key=lambda row: row["distance_km"])


def print_diagnostic(records: list[ReferenceAirRecord], center: tuple[float, float]) -> None:
    rows = pilot_rows(records, center)
    print("\nPilot reference-air relevance")
    for radius in (5, 10, 20, 30, 50):
        print(f"Stations within {radius} km: {sum(row['distance_km'] <= radius for row in rows)}")
    print("\nNearest 15 valid reference stations")
    print("SiteId | name | county | distance_km | PM2.5 | PM10 | AQI | publish_time_utc")
    for row in rows[:15]:
        record = row["record"]
        print(
            f"{record.site_id} | {record.site_name} | {record.county} | {row['distance_km']:.2f} | "
            f"{record.pm25_ugm3 if record.pm25_ugm3 is not None else 'NULL'} | "
            f"{record.pm10_ugm3 if record.pm10_ugm3 is not None else 'NULL'} | "
            f"{record.aqi if record.aqi is not None else 'NULL'} | {iso_utc(record.publish_time_utc) or 'NULL'}"
        )

    regional = [row["record"].pm25_ugm3 for row in rows if row["distance_km"] <= 50 and row["record"].pm25_ugm3 is not None]
    print("\nRegional-background diagnostic (within 50 km; descriptive only)")
    if regional:
        q1, q3 = percentile(regional, 0.25), percentile(regional, 0.75)
        median = statistics.median(regional)
        iqr = q3 - q1
        outliers = [value for value in regional if value > q3 + 1.5 * iqr] if len(regional) >= 4 else []
        print(f"Median PM2.5: {median:.3f}")
        print(f"Min PM2.5: {min(regional):.3f}")
        print(f"Max PM2.5: {max(regional):.3f}")
        print(f"IQR PM2.5: {iqr:.3f}")
        print(f"Valid station count: {len(regional)}")
        print("Broad simultaneous high: indeterminate (no validated temporal baseline/event threshold)")
        outlier_status = "yes" if outliers else ("no" if len(regional) >= 4 else "indeterminate")
        print(f"Single-station relative high outlier: {outlier_status} (Tukey diagnostic only)")
    else:
        print("Median/Min/Max/IQR PM2.5: NULL")
        print("Valid station count: 0")
        print("Broad simultaneous high: indeterminate")
        print("Single-station relative high outlier: indeterminate")


def save_ingest_health(connection: duckdb.DuckDBPyConnection, started: datetime, completed: datetime, values: dict[str, Any]) -> None:
    connection.execute(
        """
        INSERT INTO reference_air_ingest VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            started, completed, values.get("success", False), values.get("api_records", 0),
            values.get("parsed_records", 0), values.get("pages", 0), values.get("invalid_coordinates", 0),
            values.get("malformed_records", 0), values.get("observations", 0), values.get("inserted", 0),
            values.get("duplicates", 0), values.get("raw_path", ""), values.get("warning", ""),
        ],
    )


def run_once(config_path: Path, database_path: Path, raw_root: Path, timeout: float) -> bool:
    started = utc_now()
    values: dict[str, Any] = {"success": False}
    database_path.parent.mkdir(parents=True, exist_ok=True)
    connection = duckdb.connect(str(database_path))
    ensure_schema(connection)
    try:
        api_key = os.environ.get("MOENV_API_KEY", "")
        client = MoenvClient(api_key, timeout_seconds=timeout)
        raw_records, pages = client.fetch_all()
        records = [parse_record(raw) for raw in raw_records]
        malformed = sum(record is None for record in records)
        parsed = [record for record in records if record is not None]
        values.update({
            "api_records": len(raw_records), "parsed_records": len(parsed), "pages": len(pages),
            "malformed_records": malformed,
            "invalid_coordinates": sum("invalid_coordinate" in record.quality_flags for record in parsed),
            "observations": sum(record.publish_time_utc is not None for record in parsed),
        })
        raw_payload = {
            "dataset": MOENV_DATASET,
            "source": MOENV_API_URL,
            "fetched_at_utc": iso_utc(started),
            "pages": pages,
        }
        try:
            values["raw_path"] = str(write_raw_snapshot(raw_root, started, raw_payload))
        except OSError as exc:
            values["warning"] = f"raw snapshot write failed: {exc}"
            LOGGER.warning("raw snapshot write failed; normalized data will still be committed: %s", exc)
        connection.begin()
        try:
            upsert_stations(connection, parsed, started)
            values["inserted"], values["duplicates"] = insert_observations(connection, parsed, started)
            connection.commit()
            values["success"] = True
        except Exception:
            connection.rollback()
            raise
        print_diagnostic(parsed, core_center(config_path))
    except (MoenvError, OSError, ValueError, KeyError, duckdb.Error) as exc:
        values["warning"] = str(exc)
        LOGGER.error("reference-air ingest failed: %s", exc)
    finally:
        completed = utc_now()
        try:
            save_ingest_health(connection, started, completed, values)
        finally:
            connection.close()
    print("\nAirTrace National Reference Air Ingestion")
    print(f"API records received: {values.get('api_records', 0)}")
    print(f"Parsed records: {values.get('parsed_records', 0)}")
    print(f"Pages fetched: {values.get('pages', 0)}")
    print(f"Valid-coordinate records: {values.get('parsed_records', 0) - values.get('invalid_coordinates', 0)}")
    print(f"Malformed records skipped: {values.get('malformed_records', 0)}")
    print(f"Observations received: {values.get('observations', 0)}")
    print(f"First-run/new observations inserted: {values.get('inserted', 0)}")
    print(f"Duplicates skipped: {values.get('duplicates', 0)}")
    print(f"Raw snapshot: {values.get('raw_path') or '(none)'}")
    if values.get("warning"):
        print(f"Warning/error: {values['warning']}")
    return bool(values.get("success"))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true", help="fetch once and exit")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE)
    parser.add_argument("--raw-root", type=Path, default=DEFAULT_RAW_ROOT)
    parser.add_argument("--timeout", type=float, default=30.0)
    return parser.parse_args()


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    args = parse_args()
    if not args.once:
        print("This script is intentionally one-shot; use --once.", file=sys.stderr)
        return 2
    if args.timeout <= 0:
        print("ERROR: --timeout must be positive", file=sys.stderr)
        return 2
    return 0 if run_once(args.config, args.database, args.raw_root, args.timeout) else 1


if __name__ == "__main__":
    raise SystemExit(main())
