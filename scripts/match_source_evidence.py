#!/usr/bin/env python3
"""Fuse one Backtrace source surface with facilities, FIRMS, and CEMS context."""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import duckdb  # noqa: E402
from dotenv import load_dotenv  # noqa: E402

from airtrace.analysis.evidence import (  # noqa: E402
    _event_time, _occupied_extent, match_source_evidence, write_evidence_json, write_evidence_map,
    write_facility_csv, write_fire_csv,
)
from airtrace.data.cems import ensure_schema as ensure_cems_schema, load_cems  # noqa: E402
from airtrace.data.facilities import ensure_schema as ensure_facility_schema, facility_diagnostics, load_facilities  # noqa: E402
from airtrace.data.firms import DEFAULT_SOURCES, FirmsClient  # noqa: E402

DEFAULT_TRACE = ROOT / "reports" / "source_trace" / "latest_source_trace.json"
DEFAULT_OUTPUT = ROOT / "reports" / "evidence"
DEFAULT_FACILITIES = ROOT / "data" / "facilities.duckdb"
DEFAULT_CEMS = ROOT / "data" / "cems.duckdb"
DEFAULT_REGION = ROOT / "config" / "pilot_region.json"
load_dotenv(ROOT / ".env")


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _load_table(path: Path, ensure_schema, loader) -> list[dict]:
    if not path.exists():
        return []
    connection = duckdb.connect(str(path), read_only=True)
    try:
        return loader(connection)
    finally:
        connection.close()


def _query_dates(trace: dict, window_hours: float) -> list[str | int]:
    event_time = _event_time(trace)
    if event_time is None:
        return [1]
    start = (event_time - timedelta(hours=window_hours)).date()
    end = (event_time + timedelta(hours=window_hours)).date()
    current = start
    result = []
    while current <= end:
        result.append(current.isoformat())
        current += timedelta(days=1)
    return result


def _firms_bbox(trace: dict, buffer_km: float = 10.0) -> dict[str, float] | None:
    extent = _occupied_extent(trace, buffer_km)
    if extent is None:
        return None
    # _occupied_extent returns a WGS84 bbox in named order. FirmsClient emits
    # the required west,south,east,north order when building the URL.
    return {key: float(value) for key, value in extent.items()}


def fetch_firms(trace: dict, map_key: str, window_hours: float) -> tuple[list, dict]:
    bbox = _firms_bbox(trace, 10.0)
    diagnostics = {"requested": bool(map_key and bbox), "bbox_named": bbox, "bbox_api_order": None, "dates": _query_dates(trace, window_hours), "sources": {}, "errors": []}
    if not map_key:
        diagnostics["errors"].append("FIRMS_MAP_KEY is not set; no FIRMS query performed")
        return [], diagnostics
    if bbox is None:
        diagnostics["errors"].append("trace has no occupied source extent; no FIRMS query performed")
        return [], diagnostics
    diagnostics["bbox_api_order"] = [bbox["west"], bbox["south"], bbox["east"], bbox["north"]]
    client = FirmsClient(map_key)
    detections = []
    for source in DEFAULT_SOURCES:
        source_rows = []
        try:
            for query in diagnostics["dates"]:
                source_rows.extend(client.fetch_detections(source, bbox, query))
            detections.extend(source_rows)
            diagnostics["sources"][source] = {"detections": len(source_rows), "queries": len(diagnostics["dates"])}
        except Exception as exc:
            diagnostics["sources"][source] = {"detections": len(source_rows), "queries": len(diagnostics["dates"])}
            diagnostics["errors"].append(f"{source}: {exc}")
    return detections, diagnostics


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument("--trace", type=Path, default=DEFAULT_TRACE)
    selection.add_argument("--latest", action="store_true", help="use latest_source_trace.json")
    parser.add_argument("--facilities-db", type=Path, default=DEFAULT_FACILITIES)
    parser.add_argument("--cems-db", type=Path, default=DEFAULT_CEMS)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--region-config", type=Path, default=DEFAULT_REGION)
    parser.add_argument("--firms-map-key", default=os.environ.get("FIRMS_MAP_KEY", ""))
    parser.add_argument("--firms-window-hours", type=float, default=12.0)
    parser.add_argument("--source-buffer-km", type=float, default=3.0)
    parser.add_argument("--no-firms", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.firms_window_hours <= 0 or args.source_buffer_km < 0:
        print("ERROR: windows must be positive and source buffer cannot be negative", file=sys.stderr)
        return 2
    trace = _read_json(args.trace)
    facilities = _load_table(args.facilities_db, ensure_facility_schema, load_facilities)
    cems = _load_table(args.cems_db, ensure_cems_schema, load_cems)
    firms_rows, firms_diagnostics = ([], {"requested": False, "errors": ["FIRMS query disabled by --no-firms"]}) if args.no_firms else fetch_firms(trace, args.firms_map_key, args.firms_window_hours)
    report = match_source_evidence(trace, facilities, firms_rows, cems, source_buffer_km=args.source_buffer_km, firms_window_hours=args.firms_window_hours)
    report["trace_metadata"] = {"trace_path": str(args.trace), "trace_status": trace.get("status"), "trace_schema_version": trace.get("schema_version"), "event": trace.get("event", {})}
    report["facility_catalog_diagnostics"] = facility_diagnostics(facilities, _read_json(args.region_config)) if facilities and args.region_config.exists() else {"total_facilities": len(facilities), "valid_coordinates": sum(row.get("lat") is not None and row.get("lon") is not None for row in facilities)}
    report["data_sources"] = {"facilities_db": str(args.facilities_db), "cems_db": str(args.cems_db), "firms": "NASA FIRMS Area API"}
    report["firms_query_diagnostics"] = firms_diagnostics
    cems_raw_files = list((ROOT / "data" / "raw" / "cems").rglob("*.json.gz")) if (ROOT / "data" / "raw" / "cems").exists() else []
    report["cems_ingestion_diagnostics"] = {"db_rows_loaded": len(cems), "raw_snapshot_file_count": len(cems_raw_files), "note": "CEMS raw ingest is not considered complete unless the API reaches an empty page; a prior run may have stopped on timeout/pagination cap."}
    if not facilities:
        report.setdefault("limitations", []).append("Facility DB is empty or unavailable; run scripts/fetch_facilities.py first.")
    if not cems:
        report.setdefault("limitations", []).append("CEMS DB is empty or unavailable; run scripts/fetch_cems.py first.")
    args.output_dir.mkdir(parents=True, exist_ok=True)
    json_path = args.output_dir / "latest_evidence.json"
    facility_path = args.output_dir / "facility_candidates.csv"
    fire_path = args.output_dir / "fire_candidates.csv"
    map_path = args.output_dir / "latest_evidence_map.html"
    write_evidence_json(report, json_path)
    write_facility_csv(report, facility_path)
    write_fire_csv(report, fire_path)
    region = _read_json(args.region_config) if args.region_config.exists() else None
    write_evidence_map(report, trace, map_path, region=region)
    print("AirTrace Source Evidence Fusion v1")
    print(f"Trace: {args.trace}")
    print(f"Trace type: {report['trace_type']}")
    print(f"Facilities loaded: {len(facilities)}; candidates after spatial prefilter: {report['facility_match_count_after_prefilter']}")
    print(f"Facility status: {report['facility_match_status']}")
    print(f"FIRMS detections: {sum(item.get('detection_count', 0) for item in report['fire_matches'])}; fire groups after spatial prefilter: {report['fire_group_count_after_prefilter']}")
    print(f"Fire status: {report['fire_match_status']}")
    print(f"CEMS records loaded: {len(cems)}; annotations: {len(report['cems_annotations'])}")
    print(f"JSON: {json_path}")
    print(f"Facility CSV: {facility_path}")
    print(f"Fire CSV: {fire_path}")
    print(f"Map: {map_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
