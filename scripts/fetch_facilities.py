#!/usr/bin/env python3
"""Fetch the daily MOENV EMS_S_01 facility catalogue into DuckDB."""

from __future__ import annotations

import argparse
import gzip
import json
import os
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import duckdb  # noqa: E402
from dotenv import load_dotenv  # noqa: E402

from airtrace.data.facilities import (  # noqa: E402
    FACILITY_DATASET, ensure_schema, facility_diagnostics, parse_facility_record, upsert_facilities,
)
from airtrace.data.moenv import MoenvClient, MoenvError  # noqa: E402

DEFAULT_DATABASE = ROOT / "data" / "facilities.duckdb"
DEFAULT_RAW_ROOT = ROOT / "data" / "raw" / "facilities"
DEFAULT_CONFIG = ROOT / "config" / "pilot_region.json"
load_dotenv(ROOT / ".env")


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def write_raw_snapshot(raw_root: Path, started: datetime, payload: dict, page_number: int) -> Path:
    directory = raw_root / started.strftime("%Y-%m-%d")
    directory.mkdir(parents=True, exist_ok=True)
    stem = f"{started.strftime('%Y%m%dT%H%M%SZ')}-page{page_number:04d}"
    for suffix in range(1000):
        path = directory / (f"{stem}.json.gz" if suffix == 0 else f"{stem}-{suffix:02d}.json.gz")
        try:
            with path.open("xb") as raw:
                with gzip.GzipFile(fileobj=raw, mode="wb", mtime=0) as compressed:
                    compressed.write(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8"))
            return path
        except FileExistsError:
            continue
    raise OSError(f"could not allocate raw snapshot name for {stem}")


def run_once(database: Path, raw_root: Path, config: Path, timeout: float) -> int:
    started = utc_now()
    key = os.environ.get("MOENV_API_KEY", "")
    connection = duckdb.connect(str(database))
    try:
        ensure_schema(connection)
        client = MoenvClient(key, dataset=FACILITY_DATASET, timeout_seconds=timeout, max_pages=1000)
        region = json.loads(config.read_text(encoding="utf-8"))
        total_raw = total_parsed = valid_coordinates = air_true = context_count = core_count = page_count = 0
        seen_ids = set()
        pending = []
        context_industries: Counter[str] = Counter()
        core_industries: Counter[str] = Counter()
        raw_root_for_report: Path | None = None
        for page_count, (raw_records, payload) in enumerate(client.iter_pages(), 1):
            records = [parse_facility_record(raw, ingested_at_utc=started) for raw in raw_records]
            parsed = [record for record in records if record is not None]
            pending.extend(parsed)
            if len(pending) >= 50_000:
                upsert_facilities(connection, pending)
                pending.clear()
            total_raw += len(raw_records)
            for record in parsed:
                if record.ems_no in seen_ids:
                    continue
                seen_ids.add(record.ems_no)
                total_parsed += 1
                valid_coordinates += record.valid_coordinates
                air_true += record.is_air_regulated is True
                row = record.__dict__
                in_context = (
                    record.valid_coordinates and region["context_bbox"]["south"] <= record.lat <= region["context_bbox"]["north"]
                    and region["context_bbox"]["west"] <= record.lon <= region["context_bbox"]["east"]
                )
                in_core = (
                    record.valid_coordinates and region["core_bbox"]["south"] <= record.lat <= region["core_bbox"]["north"]
                    and region["core_bbox"]["west"] <= record.lon <= region["core_bbox"]["east"]
                )
                label = record.industry_name or record.industrial_area or "UNKNOWN"
                context_count += in_context
                core_count += in_core
                if in_context:
                    context_industries[label] += 1
                if in_core:
                    core_industries[label] += 1
            raw_root_for_report = write_raw_snapshot(raw_root, started, {"dataset": FACILITY_DATASET, "source": client.api_url, "fetched_at_utc": started.isoformat().replace("+00:00", "Z"), "page_number": page_count, "records": raw_records}, page_count).parent
        if pending:
            upsert_facilities(connection, pending)
        diagnostic = {"total_facilities": total_parsed, "valid_coordinates": valid_coordinates, "context_zone_count": context_count, "core_zone_count": core_count, "is_air_true_count": air_true, "top_industry_context": [{"industry": key, "count": value} for key, value in context_industries.most_common(10)], "top_industry_core": [{"industry": key, "count": value} for key, value in core_industries.most_common(10)]}
        print("AirTrace Facility Ingestion")
        print(f"Dataset: {FACILITY_DATASET}")
        print(f"Pages fetched: {page_count}")
        print(f"Raw records: {total_raw}")
        print(f"Parsed facilities: {total_parsed}")
        print(f"Raw snapshots: {raw_root_for_report or raw_root}")
        for key_, value in diagnostic.items():
            print(f"{key_}: {value}")
        return 0
    except (MoenvError, OSError, ValueError, KeyError, duckdb.Error) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    finally:
        connection.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE)
    parser.add_argument("--raw-root", type=Path, default=DEFAULT_RAW_ROOT)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--timeout", type=float, default=30.0)
    args = parser.parse_args()
    if args.timeout <= 0:
        parser.error("--timeout must be positive")
    args.database.parent.mkdir(parents=True, exist_ok=True)
    return run_once(args.database, args.raw_root, args.config, args.timeout)


if __name__ == "__main__":
    raise SystemExit(main())
