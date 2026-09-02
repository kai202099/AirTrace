"""MOENV EMS_S_01 facility records and the local DuckDB store."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

import duckdb

from airtrace.data.moenv import _field, _text, parse_number


FACILITY_DATASET = "EMS_S_01"


@dataclass(frozen=True)
class FacilityRecord:
    ems_no: str
    facility_name: str
    lat: float | None
    lon: float | None
    county: str
    township: str
    address: str
    industrial_area: str
    industry_id: str
    industry_name: str
    is_air_regulated: bool | None
    is_waste_regulated: bool | None
    air_release_date: str
    factory_registration_no: str
    ingested_at_utc: datetime

    @property
    def valid_coordinates(self) -> bool:
        return self.lat is not None and self.lon is not None


def _flag(value: Any) -> bool | None:
    if value is None or not str(value).strip():
        return None
    text = str(value).strip().casefold()
    if text in {"true", "1", "y", "yes", "t", "是", "有"}:
        return True
    if text in {"false", "0", "n", "no", "f", "否", "無"}:
        return False
    return None


def parse_facility_record(raw: Mapping[str, Any], *, ingested_at_utc: datetime | None = None) -> FacilityRecord | None:
    """Parse one EMS_S_01 row; malformed IDs are skipped, bad coordinates are NULL."""
    ems_no = _text(_field(dict(raw), "EmsNo"))
    if not ems_no:
        return None
    lat, lat_invalid = parse_number(_field(dict(raw), "WGS84Lat"), minimum=-90, maximum=90)
    lon, lon_invalid = parse_number(_field(dict(raw), "WGS84Lon"), minimum=-180, maximum=180)
    if lat_invalid or lon_invalid or (lat == 0 and lon == 0):
        lat, lon = None, None
    seen = ingested_at_utc or datetime.now(timezone.utc)
    if seen.tzinfo is None:
        seen = seen.replace(tzinfo=timezone.utc)
    return FacilityRecord(
        ems_no=ems_no,
        facility_name=_text(_field(dict(raw), "FacilityName")),
        lat=lat,
        lon=lon,
        county=_text(_field(dict(raw), "County")),
        township=_text(_field(dict(raw), "Township")),
        address=_text(_field(dict(raw), "FacilityAddress")),
        industrial_area=_text(_field(dict(raw), "IndustryAreaName")),
        industry_id=_text(_field(dict(raw), "IndustryID")),
        industry_name=_text(_field(dict(raw), "IndustryName")),
        is_air_regulated=_flag(_field(dict(raw), "IsAir")),
        is_waste_regulated=_flag(_field(dict(raw), "IsWaste")),
        air_release_date=_text(_field(dict(raw), "AirReleaseDate")),
        factory_registration_no=_text(_field(dict(raw), "FACNO")),
        ingested_at_utc=seen.astimezone(timezone.utc),
    )


def ensure_schema(connection: duckdb.DuckDBPyConnection) -> None:
    connection.execute("SET TimeZone='UTC'")
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS facility (
            ems_no VARCHAR PRIMARY KEY,
            facility_name VARCHAR,
            lat DOUBLE,
            lon DOUBLE,
            county VARCHAR,
            township VARCHAR,
            address VARCHAR,
            industrial_area VARCHAR,
            industry_id VARCHAR,
            industry_name VARCHAR,
            is_air_regulated BOOLEAN,
            is_waste_regulated BOOLEAN,
            air_release_date VARCHAR,
            factory_registration_no VARCHAR,
            ingested_at_utc TIMESTAMPTZ NOT NULL
        )
        """
    )


def upsert_facilities(connection: duckdb.DuckDBPyConnection, records: Iterable[FacilityRecord]) -> int:
    rows = {record.ems_no: record for record in records}
    values = [[
            r.ems_no, r.facility_name, r.lat, r.lon, r.county, r.township, r.address,
            r.industrial_area, r.industry_id, r.industry_name, r.is_air_regulated,
            r.is_waste_regulated, r.air_release_date, r.factory_registration_no, r.ingested_at_utc,
        ] for r in rows.values()]
    statement = """
        INSERT INTO facility VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (ems_no) DO UPDATE SET
            facility_name=excluded.facility_name, lat=excluded.lat, lon=excluded.lon,
            county=excluded.county, township=excluded.township, address=excluded.address,
            industrial_area=excluded.industrial_area, industry_id=excluded.industry_id,
            industry_name=excluded.industry_name, is_air_regulated=excluded.is_air_regulated,
            is_waste_regulated=excluded.is_waste_regulated, air_release_date=excluded.air_release_date,
            factory_registration_no=excluded.factory_registration_no, ingested_at_utc=excluded.ingested_at_utc
        """
    try:
        import pandas as pd
        frame = pd.DataFrame(values, columns=["ems_no", "facility_name", "lat", "lon", "county", "township", "address", "industrial_area", "industry_id", "industry_name", "is_air_regulated", "is_waste_regulated", "air_release_date", "factory_registration_no", "ingested_at_utc"])
        connection.register("_facility_batch", frame)
        try:
            connection.execute("INSERT INTO facility SELECT * FROM _facility_batch ON CONFLICT (ems_no) DO UPDATE SET facility_name=excluded.facility_name, lat=excluded.lat, lon=excluded.lon, county=excluded.county, township=excluded.township, address=excluded.address, industrial_area=excluded.industrial_area, industry_id=excluded.industry_id, industry_name=excluded.industry_name, is_air_regulated=excluded.is_air_regulated, is_waste_regulated=excluded.is_waste_regulated, air_release_date=excluded.air_release_date, factory_registration_no=excluded.factory_registration_no, ingested_at_utc=excluded.ingested_at_utc")
        finally:
            connection.unregister("_facility_batch")
    except ImportError:
        connection.executemany(statement, values)
    return len(rows)


def load_facilities(connection: duckdb.DuckDBPyConnection) -> list[dict[str, Any]]:
    columns = [
        "ems_no", "facility_name", "lat", "lon", "county", "township", "address",
        "industrial_area", "industry_id", "industry_name", "is_air_regulated",
        "is_waste_regulated", "air_release_date", "factory_registration_no", "ingested_at_utc",
    ]
    return [dict(zip(columns, row)) for row in connection.execute("SELECT " + ",".join(columns) + " FROM facility").fetchall()]


def in_bbox(record: Mapping[str, Any], bbox: Mapping[str, Any]) -> bool:
    return (
        record.get("lat") is not None and record.get("lon") is not None
        and float(bbox["south"]) <= float(record["lat"]) <= float(bbox["north"])
        and float(bbox["west"]) <= float(record["lon"]) <= float(bbox["east"])
    )


def facility_diagnostics(records: Iterable[Mapping[str, Any]], region: Mapping[str, Any]) -> dict[str, Any]:
    rows = list(records)
    context = [row for row in rows if in_bbox(row, region["context_bbox"])]
    core = [row for row in rows if in_bbox(row, region["core_bbox"])]
    def top(rows_: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
        counts: dict[str, int] = {}
        for row in rows_:
            label = str(row.get("industry_name") or row.get("industrial_area") or "UNKNOWN").strip() or "UNKNOWN"
            counts[label] = counts.get(label, 0) + 1
        return [{"industry": name, "count": count} for name, count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))[:10]]
    return {
        "total_facilities": len(rows),
        "valid_coordinates": sum(row.get("lat") is not None and row.get("lon") is not None for row in rows),
        "context_zone_count": len(context),
        "core_zone_count": len(core),
        "is_air_true_count": sum(row.get("is_air_regulated") is True for row in rows),
        "top_industry_context": top(context),
        "top_industry_core": top(core),
    }


def record_to_dict(record: FacilityRecord) -> dict[str, Any]:
    result = asdict(record)
    result["ingested_at_utc"] = result["ingested_at_utc"].isoformat().replace("+00:00", "Z")
    return result


__all__ = [
    "FACILITY_DATASET", "FacilityRecord", "ensure_schema", "facility_diagnostics",
    "in_bbox", "load_facilities", "parse_facility_record", "record_to_dict", "upsert_facilities",
]
