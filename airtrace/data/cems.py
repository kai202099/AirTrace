"""MOENV AQX_P_186 CEMS parsing, storage, joins, and event annotations."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Mapping

import duckdb

from airtrace.data.moenv import _field, _text, parse_number, parse_timestamp_utc

CEMS_DATASET = "AQX_P_186"


@dataclass(frozen=True)
class CEMSRecord:
    cno: str
    company_abbr: str
    stack_id: str
    pollutant_code: str
    pollutant_name: str
    measurement_time_utc: datetime | None
    value: float | None
    standard: float | None
    unit: str
    data_code: str
    data_status: str
    standard_basis: str
    ingested_at_utc: datetime


def _first(raw: Mapping[str, Any], *names: str) -> Any:
    for name in names:
        value = _field(dict(raw), name)
        if value is not None:
            return value
    return None


def normalize_control_id(value: Any) -> str:
    return re.sub(r"\s+", "", str(value or "").strip().upper())


def parse_cems_record(raw: Mapping[str, Any], *, ingested_at_utc: datetime | None = None) -> CEMSRecord | None:
    cno = _text(_first(raw, "CNO", "Cno", "EMSNo", "EmsNo"))
    if not cno:
        return None
    value, _ = parse_number(_first(raw, "M_Value", "MValue", "Value"))
    standard, _ = parse_number(_first(raw, "Std", "Standard", "M_Std"), minimum=0)
    timestamp = _first(raw, "M_Date", "M_Time", "MeasurementTime", "DataTime", "MonitorDate", "Time")
    when = parse_timestamp_utc(timestamp) if timestamp is not None else None
    seen = ingested_at_utc or datetime.now(timezone.utc)
    if seen.tzinfo is None:
        seen = seen.replace(tzinfo=timezone.utc)
    return CEMSRecord(
        cno=cno,
        company_abbr=_text(_first(raw, "CompanyAbbr", "Company", "CName", "Abbr")),
        stack_id=_text(_first(raw, "StackID", "StackId", "StackNo", "PointNo", "Item")),
        pollutant_code=_text(_first(raw, "PollutantCode", "Code", "ItemCode", "PolNo")),
        pollutant_name=_text(_first(raw, "PollutantName", "Pollutant", "ItemName", "ItemDesc")),
        measurement_time_utc=when,
        value=value,
        standard=standard,
        unit=_text(_first(raw, "Unit", "M_Unit")),
        data_code=_text(_first(raw, "Code2", "DataCode", "Data_Code")),
        data_status=_text(_first(raw, "DataStatus", "Status", "StatusDescription", "Code2Desc")),
        standard_basis=_text(_first(raw, "StandardBasis", "StdBasis", "StandardType", "StdS")),
        ingested_at_utc=seen.astimezone(timezone.utc),
    )


def ensure_schema(connection: duckdb.DuckDBPyConnection) -> None:
    connection.execute("SET TimeZone='UTC'")
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS cems_measurement (
            cno VARCHAR NOT NULL,
            company_abbr VARCHAR,
            stack_id VARCHAR,
            pollutant_code VARCHAR,
            pollutant_name VARCHAR,
            measurement_time_utc TIMESTAMPTZ,
            value DOUBLE,
            standard DOUBLE,
            unit VARCHAR,
            data_code VARCHAR,
            data_status VARCHAR,
            standard_basis VARCHAR,
            ingested_at_utc TIMESTAMPTZ NOT NULL,
            PRIMARY KEY (cno, stack_id, pollutant_code, measurement_time_utc)
        )
        """
    )


def upsert_cems(connection: duckdb.DuckDBPyConnection, records: Iterable[CEMSRecord]) -> int:
    rows = [record for record in records if record.measurement_time_utc is not None]
    values = [[r.cno, r.company_abbr, r.stack_id, r.pollutant_code, r.pollutant_name, r.measurement_time_utc,
               r.value, r.standard, r.unit, r.data_code, r.data_status, r.standard_basis, r.ingested_at_utc] for r in rows]
    statement = """
        INSERT INTO cems_measurement VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (cno, stack_id, pollutant_code, measurement_time_utc) DO UPDATE SET
            company_abbr=excluded.company_abbr, value=excluded.value, standard=excluded.standard,
            unit=excluded.unit, data_code=excluded.data_code, data_status=excluded.data_status,
            standard_basis=excluded.standard_basis, ingested_at_utc=excluded.ingested_at_utc
        """
    try:
        import pandas as pd
        columns = ["cno", "company_abbr", "stack_id", "pollutant_code", "pollutant_name", "measurement_time_utc", "value", "standard", "unit", "data_code", "data_status", "standard_basis", "ingested_at_utc"]
        connection.register("_cems_batch", pd.DataFrame(values, columns=columns))
        try:
            connection.execute("INSERT INTO cems_measurement SELECT * FROM _cems_batch ON CONFLICT (cno, stack_id, pollutant_code, measurement_time_utc) DO UPDATE SET company_abbr=excluded.company_abbr, pollutant_name=excluded.pollutant_name, value=excluded.value, standard=excluded.standard, unit=excluded.unit, data_code=excluded.data_code, data_status=excluded.data_status, standard_basis=excluded.standard_basis, ingested_at_utc=excluded.ingested_at_utc")
        finally:
            connection.unregister("_cems_batch")
    except ImportError:
        connection.executemany(statement, values)
    return len(rows)


def load_cems(connection: duckdb.DuckDBPyConnection) -> list[dict[str, Any]]:
    columns = ["cno", "company_abbr", "stack_id", "pollutant_code", "pollutant_name", "measurement_time_utc", "value", "standard", "unit", "data_code", "data_status", "standard_basis", "ingested_at_utc"]
    return [dict(zip(columns, row)) for row in connection.execute("SELECT " + ",".join(columns) + " FROM cems_measurement").fetchall()]


def join_diagnostics(cems: Iterable[Mapping[str, Any]], facilities: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    cems_rows, facility_rows = list(cems), list(facilities)
    cems_ids = {str(row.get("cno") or "").strip() for row in cems_rows if str(row.get("cno") or "").strip()}
    facility_ids = {str(row.get("ems_no") or "").strip() for row in facility_rows if str(row.get("ems_no") or "").strip()}
    normalized_cems = {normalize_control_id(value) for value in cems_ids}
    normalized_facilities = {normalize_control_id(value) for value in facility_ids}
    exact = cems_ids & facility_ids
    normalized = normalized_cems & normalized_facilities
    return {
        "cems_record_count": len(cems_rows),
        "cems_unique_cno_count": len(cems_ids),
        "facility_unique_ems_no_count": len(facility_ids),
        "exact_match_count": len(exact),
        "exact_string_match_rate": len(exact) / len(cems_ids) if cems_ids else None,
        "normalized_match_count": len(normalized),
        "normalized_match_rate": len(normalized) / len(normalized_cems) if normalized_cems else None,
        "unmatched_cems_cno_count": len(cems_ids - exact),
        "unmatched_facility_ems_no_count": len(facility_ids - exact),
        "normalized_only_match_count": len(normalized - {normalize_control_id(value) for value in exact}),
        "join_rule": "exact string and trim/uppercase whitespace-normalized control ID only; no fuzzy company-name matching",
    }


def _valid_status(record: Mapping[str, Any]) -> bool:
    status = str(record.get("data_status") or "").casefold()
    code = str(record.get("data_code") or "").casefold()
    invalid_words = ("invalid", "maintenance", "calibration", "maint", "cal", "校正", "維護", "無效")
    return not any(word in status or word in code for word in invalid_words)


def cems_annotations(cems: Iterable[Mapping[str, Any]], facilities: Iterable[Mapping[str, Any]], event_time: datetime | None, *, window_hours: float = 2.0) -> list[dict[str, Any]]:
    """Return contextual observations; these never contribute to facility score."""
    facility_by_id = {normalize_control_id(row.get("ems_no")): row for row in facilities}
    if event_time is not None and event_time.tzinfo is None:
        event_time = event_time.replace(tzinfo=timezone.utc)
    lower = event_time - timedelta(hours=window_hours) if event_time else None
    upper = event_time + timedelta(hours=window_hours) if event_time else None
    result = []
    for row in cems:
        facility = facility_by_id.get(normalize_control_id(row.get("cno")))
        when = row.get("measurement_time_utc")
        if isinstance(when, str):
            when = parse_timestamp_utc(when)
        if facility is None or when is None or (lower and not lower <= when <= upper):
            continue
        standard = row.get("standard")
        value = row.get("value")
        ratio = None
        if isinstance(value, (int, float)) and isinstance(standard, (int, float)) and standard > 0:
            ratio = float(value) / float(standard)
        age = (when - event_time).total_seconds() if event_time else None
        result.append({
            "ems_no": facility.get("ems_no"), "facility_name": facility.get("facility_name"),
            "cno": row.get("cno"), "pollutant": row.get("pollutant_name") or row.get("pollutant_code"),
            "pollutant_code": row.get("pollutant_code"), "value": value, "unit": row.get("unit"),
            "standard": standard, "value_standard_ratio": ratio, "data_code": row.get("data_code"),
            "data_status": row.get("data_status"), "standard_basis": row.get("standard_basis"),
            "measurement_time_utc": when.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
            "age_relative_to_event_seconds": age, "valid_for_supporting_context": _valid_status(row),
        })
    return sorted(result, key=lambda row: (abs(row["age_relative_to_event_seconds"] or 0), str(row.get("ems_no")), str(row.get("pollutant"))))


__all__ = ["CEMS_DATASET", "CEMSRecord", "cems_annotations", "ensure_schema", "join_diagnostics", "load_cems", "normalize_control_id", "parse_cems_record", "upsert_cems"]
