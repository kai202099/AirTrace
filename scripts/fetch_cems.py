#!/usr/bin/env python3
"""Fetch MOENV AQX_P_186 CEMS records into the local context database."""

from __future__ import annotations

import argparse
import gzip
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import duckdb  # noqa: E402
import airtrace.config  # noqa: E402,F401

from airtrace.data.cems import CEMS_DATASET, ensure_schema, parse_cems_record, upsert_cems  # noqa: E402
from airtrace.data.moenv import MoenvClient, MoenvError  # noqa: E402

DEFAULT_DATABASE = ROOT / "data" / "cems.duckdb"
DEFAULT_RAW_ROOT = ROOT / "data" / "raw" / "cems"
DEFAULT_METADATA = ROOT / "data" / "cems_ingest_metadata.json"
DEFAULT_YEAR_MONTH_MAX_PAGES = 100
MAX_ROWS_ONLY_MAX_PAGES = 1000


def resolve_limits(year_month: str | None, max_pages: int | None, max_rows: int | None) -> tuple[str | None, int]:
    """Return a finite request bound; never permit an implicit unbounded scan."""
    if year_month is not None and not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", year_month):
        raise ValueError("--year-month must use YYYY-MM")
    if year_month is None and max_pages is None and max_rows is None:
        raise ValueError("CEMS fetch is bounded: set --year-month, --max-pages, or --max-rows")
    if max_pages is not None and max_pages <= 0:
        raise ValueError("--max-pages must be positive")
    if max_rows is not None and max_rows <= 0:
        raise ValueError("--max-rows must be positive")
    if max_pages is not None:
        return year_month, max_pages
    return year_month, DEFAULT_YEAR_MONTH_MAX_PAGES if year_month is not None else MAX_ROWS_ONLY_MAX_PAGES


def write_metadata(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def write_raw_snapshot(root: Path, started: datetime, payload: dict, page_number: int) -> Path:
    directory = root / started.strftime("%Y-%m-%d")
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


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE)
    parser.add_argument("--raw-root", type=Path, default=DEFAULT_RAW_ROOT)
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--start-offset", type=int, default=0, help="resume after a prior bounded ingest")
    parser.add_argument("--year-month", "--year_month", dest="year_month", help="bounded client-side selection window, YYYY-MM")
    parser.add_argument("--max-pages", "--max_pages", dest="max_pages", type=int, help="explicit maximum API pages")
    parser.add_argument("--max-rows", "--max_rows", dest="max_rows", type=int, help="explicit maximum rows considered from this request")
    parser.add_argument("--metadata", type=Path, default=DEFAULT_METADATA)
    args = parser.parse_args()
    try:
        year_month, effective_max_pages = resolve_limits(args.year_month, args.max_pages, args.max_rows)
    except ValueError as exc:
        parser.error(str(exc))
    if args.start_offset < 0:
        parser.error("--start-offset must not be negative")
    started = datetime.now(timezone.utc)
    connection = duckdb.connect(str(args.database))
    metadata = {
        "schema_version": 1, "dataset": CEMS_DATASET, "started_at_utc": started.isoformat().replace("+00:00", "Z"),
        "status": "RUNNING", "dataset_complete": False, "year_month": year_month,
        "max_pages": effective_max_pages, "max_rows": args.max_rows, "start_offset": args.start_offset,
    }
    total_raw = total_parsed = inserted = page_count = 0
    try:
        ensure_schema(connection)
        client = MoenvClient(os.environ.get("MOENV_API_KEY", ""), dataset=CEMS_DATASET, timeout_seconds=args.timeout, max_pages=effective_max_pages)
        pending = []
        unique_cno: set[str] = set()
        raw_root_for_report = None
        stopped_by_max_rows = False
        first_page_number = args.start_offset // client.page_limit + 1
        for page_count, (raw_records, payload) in enumerate(client.iter_pages(args.start_offset), first_page_number):
            if args.max_rows is not None:
                remaining = args.max_rows - total_raw
                if remaining <= 0:
                    stopped_by_max_rows = True
                    break
                raw_records = raw_records[:remaining]
            parsed = [parse_cems_record(row, ingested_at_utc=started) for row in raw_records]
            records = [row for row in parsed if row is not None]
            if year_month is not None:
                records = [row for row in records if row.measurement_time_utc is not None and row.measurement_time_utc.strftime("%Y-%m") == year_month]
            pending.extend(records)
            if len(pending) >= 50_000:
                inserted += upsert_cems(connection, pending)
                pending.clear()
            total_raw += len(raw_records)
            total_parsed += len(records)
            unique_cno.update(row.cno for row in records)
            raw_root_for_report = write_raw_snapshot(args.raw_root, started, {"dataset": CEMS_DATASET, "source": client.api_url, "fetched_at_utc": started.isoformat().replace("+00:00", "Z"), "page_number": page_count, "records": raw_records}, page_count).parent
            if args.max_rows is not None and total_raw >= args.max_rows:
                stopped_by_max_rows = True
                break
        if pending:
            inserted += upsert_cems(connection, pending)
        metadata.update({"status": "BOUNDED_REQUEST_COMPLETE", "completed_at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"), "dataset_complete": False, "pages_fetched": page_count, "raw_rows_considered": total_raw, "parsed_rows_selected": total_parsed, "unique_cno": len(unique_cno), "rows_upserted": inserted, "raw_root": str(raw_root_for_report or args.raw_root), "stopped_by_max_rows": stopped_by_max_rows, "selection_semantics": "year_month is a client-side selection over bounded pages; it is not a server-side filter"})
        print("AirTrace CEMS Ingestion")
        print(f"Dataset: {CEMS_DATASET}")
        print(f"Pages fetched: {page_count}")
        print(f"Raw records: {total_raw}")
        print(f"Parsed records: {total_parsed}")
        print(f"Unique CNO: {len(unique_cno)}")
        print(f"Rows upserted: {inserted}")
        print(f"Raw snapshots: {raw_root_for_report or args.raw_root}")
        print(f"Metadata: {args.metadata}")
        write_metadata(args.metadata, metadata)
        return 0
    except (MoenvError, OSError, ValueError, KeyError, duckdb.Error) as exc:
        metadata.update({"status": "BOUNDED_REQUEST_FAILED", "completed_at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"), "dataset_complete": False, "error": str(exc), "pages_fetched": page_count, "raw_rows_considered": total_raw})
        write_metadata(args.metadata, metadata)
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    finally:
        connection.close()


if __name__ == "__main__":
    raise SystemExit(main())
