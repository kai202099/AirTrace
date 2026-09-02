#!/usr/bin/env python3
"""Fetch MOENV AQX_P_186 CEMS records into the local context database."""

from __future__ import annotations

import argparse
import gzip
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import duckdb  # noqa: E402
from dotenv import load_dotenv  # noqa: E402

from airtrace.data.cems import CEMS_DATASET, ensure_schema, parse_cems_record, upsert_cems  # noqa: E402
from airtrace.data.moenv import MoenvClient, MoenvError  # noqa: E402

DEFAULT_DATABASE = ROOT / "data" / "cems.duckdb"
DEFAULT_RAW_ROOT = ROOT / "data" / "raw" / "cems"
load_dotenv(ROOT / ".env")


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
    parser.add_argument("--max-pages", type=int, default=1000)
    args = parser.parse_args()
    started = datetime.now(timezone.utc)
    connection = duckdb.connect(str(args.database))
    try:
        ensure_schema(connection)
        client = MoenvClient(os.environ.get("MOENV_API_KEY", ""), dataset=CEMS_DATASET, timeout_seconds=args.timeout, max_pages=args.max_pages)
        total_raw = total_parsed = inserted = page_count = 0
        pending = []
        unique_cno: set[str] = set()
        raw_root_for_report = None
        first_page_number = args.start_offset // client.page_limit + 1
        for page_count, (raw_records, payload) in enumerate(client.iter_pages(args.start_offset), first_page_number):
            parsed = [parse_cems_record(row, ingested_at_utc=started) for row in raw_records]
            records = [row for row in parsed if row is not None]
            pending.extend(records)
            if len(pending) >= 50_000:
                inserted += upsert_cems(connection, pending)
                pending.clear()
            total_raw += len(raw_records)
            total_parsed += len(records)
            unique_cno.update(row.cno for row in records)
            raw_root_for_report = write_raw_snapshot(args.raw_root, started, {"dataset": CEMS_DATASET, "source": client.api_url, "fetched_at_utc": started.isoformat().replace("+00:00", "Z"), "page_number": page_count, "records": raw_records}, page_count).parent
        if pending:
            inserted += upsert_cems(connection, pending)
        print("AirTrace CEMS Ingestion")
        print(f"Dataset: {CEMS_DATASET}")
        print(f"Pages fetched: {page_count}")
        print(f"Raw records: {total_raw}")
        print(f"Parsed records: {total_parsed}")
        print(f"Unique CNO: {len(unique_cno)}")
        print(f"Rows upserted: {inserted}")
        print(f"Raw snapshots: {raw_root_for_report or args.raw_root}")
        return 0
    except (MoenvError, OSError, ValueError, KeyError, duckdb.Error) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    finally:
        connection.close()


if __name__ == "__main__":
    raise SystemExit(main())
