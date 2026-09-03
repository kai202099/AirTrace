"""FastAPI integration layer for the AirTrace Web UI.

The API reads recorder databases through short-lived read-only DuckDB
connections. It does not own recorder loops or write recorder databases.
Analysis jobs are intentionally small, in-process jobs for a single-machine
demo and reuse :func:`airtrace.pipeline.run_analysis` unchanged.
"""

from __future__ import annotations

import csv
import json
import math
import threading
import time
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from statistics import median
from typing import Any, Callable

import duckdb
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from airtrace.analysis.anomaly import parse_iso_utc
from airtrace.config import firms_map_key_configured, get_cors_origins
from airtrace.provenance import is_synthetic_manifest
from airtrace.pipeline import (
    DEFAULT_CONFIG,
    DEFAULT_DATABASE,
    DEFAULT_OUTPUT_ROOT,
    DEFAULT_WEATHER_DATABASE,
    run_analysis,
)


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CEMS_DATABASE = ROOT / "data" / "cems.duckdb"
DEFAULT_CEMS_METADATA = ROOT / "data" / "cems_ingest_metadata.json"
DEFAULT_FACILITIES_DATABASE = ROOT / "data" / "facilities.duckdb"
DEFAULT_FIXTURE_ROOT = ROOT / "fixtures" / "demo"

FRESH_SECONDS = 10 * 60
STALE_SECONDS = 30 * 60
DB_RETRY_COUNT = 3
DB_RETRY_DELAY_SECONDS = 0.08


class AnalyzeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    start: str
    end: str
    analysis_zone: str = Field(default="core", pattern="^(core|context)$")
    fast_preview: bool = False

    @field_validator("start", "end")
    @classmethod
    def validate_timestamp(cls, value: str) -> str:
        raw = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        if raw.tzinfo is None:
            raise ValueError("timestamps must include a timezone")
        parse_iso_utc(value)
        return value

    @model_validator(mode="after")
    def validate_window(self) -> "AnalyzeRequest":
        start, end = parse_iso_utc(self.start), parse_iso_utc(self.end)
        if end < start:
            raise ValueError("end must be at or after start")
        if (end - start).total_seconds() > 7 * 24 * 3600:
            raise ValueError("analysis window cannot exceed 7 days")
        return self


class Settings:
    database_path = DEFAULT_DATABASE
    weather_database_path = DEFAULT_WEATHER_DATABASE
    config_path = DEFAULT_CONFIG
    output_root = DEFAULT_OUTPUT_ROOT
    cems_database_path = DEFAULT_CEMS_DATABASE
    cems_metadata_path = DEFAULT_CEMS_METADATA
    facilities_database_path = DEFAULT_FACILITIES_DATABASE
    fixture_root = DEFAULT_FIXTURE_ROOT


settings = Settings()


def _json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=500, detail=f"artifact unreadable: {path.name}") from exc


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
    return str(value)


def _safe_child(root: Path, relative: str | Path) -> Path:
    """Resolve a manifest-relative artifact without allowing path escape."""

    candidate = (root / Path(relative)).resolve()
    resolved_root = root.resolve()
    if candidate != resolved_root and resolved_root not in candidate.parents:
        raise HTTPException(status_code=400, detail="artifact path escapes report root")
    return candidate


def _db_read(path: Path, operation: Callable[[Any], Any]) -> tuple[Any | None, str | None]:
    """Run a bounded read-only query and preserve lock state for the UI."""

    if not path.exists():
        return None, "UNAVAILABLE"
    last_error: Exception | None = None
    for attempt in range(DB_RETRY_COUNT):
        connection = None
        try:
            connection = duckdb.connect(str(path), read_only=True)
            connection.execute("SET TimeZone='UTC'")
            return operation(connection), None
        except Exception as exc:  # DuckDB exposes lock errors by version-specific type/message.
            last_error = exc
            if "lock" not in str(exc).lower() and attempt == 0:
                break
            if attempt + 1 < DB_RETRY_COUNT:
                time.sleep(DB_RETRY_DELAY_SECONDS * (attempt + 1))
        finally:
            if connection is not None:
                connection.close()
    if last_error and "lock" in str(last_error).lower():
        return None, "DB_LOCKED"
    return None, "UNAVAILABLE"


def _load_region() -> dict[str, Any]:
    return _json(settings.config_path)


def _freshness(timestamp: Any, now: datetime) -> tuple[str, float | None]:
    if timestamp is None:
        return "offline", None
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=UTC)
    age = (now - timestamp.astimezone(UTC)).total_seconds()
    if age < -60:
        return "future_timestamp", age / 60
    if age <= FRESH_SECONDS:
        return "fresh", age / 60
    if age <= STALE_SECONDS:
        return "stale", age / 60
    return "offline", age / 60


def _sensor_state(now: datetime) -> dict[str, Any]:
    region = _load_region()
    bbox = region["context_bbox"]

    def read(connection: Any) -> list[tuple[Any, ...]]:
        return connection.execute(
            """
            WITH latest AS (
              SELECT station_id, pm25_ugm3, phenomenon_time_utc, source_status, quality_flags,
                     row_number() OVER (PARTITION BY station_id ORDER BY phenomenon_time_utc DESC) AS rn
              FROM pm25_observation
            )
            SELECT s.station_id, s.station_name, s.lat, s.lon, s.city, s.township,
                   l.pm25_ugm3, l.phenomenon_time_utc, l.source_status, l.quality_flags
            FROM sensor_station s
            LEFT JOIN latest l ON l.station_id = s.station_id AND l.rn = 1
            WHERE s.lat BETWEEN ? AND ? AND s.lon BETWEEN ? AND ?
            ORDER BY s.station_id
            """,
            [bbox["south"], bbox["north"], bbox["west"], bbox["east"]],
        ).fetchall()

    rows, error = _db_read(settings.database_path, read)
    if error:
        return {"status": error, "sensors": [], "counts": {"total": 0, "fresh": 0, "stale": 0, "offline": 0}}
    sensors = []
    for row in rows or []:
        freshness, age = _freshness(row[7], now)
        sensors.append({
            "station_id": row[0], "station_name": row[1], "lat": row[2], "lon": row[3],
            "city": row[4], "township": row[5], "pm25": row[6],
            "timestamp_utc": _iso(row[7]), "freshness": freshness, "age_minutes": round(age, 2) if age is not None else None,
            "source_status": row[8], "quality_flags": row[9],
        })
    counts = {key: sum(sensor["freshness"] == key for sensor in sensors) for key in ("fresh", "stale", "offline", "future_timestamp")}
    counts["total"] = len(sensors)
    values = [float(sensor["pm25"]) for sensor in sensors if sensor["pm25"] is not None]
    return {"status": "OK", "sensors": sensors, "counts": counts, "pm25_median": median(values) if values else None}


def _weather_state(now: datetime) -> dict[str, Any]:
    region = _load_region()
    bbox = region["context_bbox"]

    def read(connection: Any) -> list[tuple[Any, ...]]:
        return connection.execute(
            """
            WITH latest AS (
              SELECT o.*, row_number() OVER (PARTITION BY o.station_id ORDER BY o.observation_time_utc DESC) AS rn
              FROM weather_observation o
              JOIN weather_station s ON s.station_id = o.station_id
              WHERE s.lat BETWEEN ? AND ? AND s.lon BETWEEN ? AND ?
            )
            SELECT l.station_id, s.station_name, s.lat, s.lon, l.observation_time_utc,
                   l.wind_from_deg, l.wind_speed_mps, l.wind_u_east_mps, l.wind_v_north_mps, l.wind_status
            FROM latest l JOIN weather_station s ON s.station_id = l.station_id
            WHERE l.rn = 1
            """,
            [bbox["south"], bbox["north"], bbox["west"], bbox["east"]],
        ).fetchall()

    rows, error = _db_read(settings.weather_database_path, read)
    if error:
        return {"status": error, "stations": [], "summary": None}
    stations = []
    for row in rows or []:
        freshness, age = _freshness(row[4], now)
        stations.append({
            "station_id": row[0], "station_name": row[1], "lat": row[2], "lon": row[3],
            "timestamp_utc": _iso(row[4]), "wind_from_deg": row[5], "wind_speed_mps": row[6],
            "wind_u_east_mps": row[7], "wind_v_north_mps": row[8], "wind_status": row[9],
            "freshness": freshness, "age_minutes": round(age, 2) if age is not None else None,
        })
    valid = [s for s in stations if s["wind_u_east_mps"] is not None and s["wind_v_north_mps"] is not None]
    if valid:
        u = sum(s["wind_u_east_mps"] for s in valid) / len(valid)
        v = sum(s["wind_v_north_mps"] for s in valid) / len(valid)
        speed = math.hypot(u, v)
        wind_to = (math.degrees(math.atan2(u, v)) + 360) % 360
        summary = {"station_count": len(valid), "u_east_mps": round(u, 3), "v_north_mps": round(v, 3), "speed_mps": round(speed, 3), "wind_to_deg": round(wind_to, 1), "wind_from_deg": round((wind_to + 180) % 360, 1), "quality": "GOOD" if len(valid) >= 3 else "LIMITED"}
    else:
        summary = None
    return {"status": "OK", "stations": stations, "summary": summary}


def _recorder_poll(path: Path, table: str, columns: str) -> dict[str, Any]:
    def read(connection: Any) -> tuple[Any, ...] | None:
        return connection.execute(f"SELECT {columns} FROM {table} ORDER BY 1 DESC LIMIT 1").fetchone()

    row, error = _db_read(path, read)
    if error:
        return {"status": error, "latest": None}
    return {"status": "OK", "latest": list(row) if row else None}


def _status_payload() -> dict[str, Any]:
    now = datetime.now(UTC)
    sensors = _sensor_state(now)
    weather = _weather_state(now)
    pm_poll = _recorder_poll(settings.database_path, "recorder_poll", "poll_started_at_utc, completed_at_utc, success, sensors_queried, observations_received, error_message")
    weather_poll = _recorder_poll(settings.weather_database_path, "weather_poll", "poll_started_at_utc, completed_at_utc, success, stations_received, observations_received, error_message")
    return {
        "as_of_utc": _iso(now), "timezone": _load_region().get("timezone", "Asia/Taipei"),
        "pm25": {"database_status": sensors["status"], "freshness": sensors["counts"], "median": sensors.get("pm25_median"), "latest_poll": pm_poll},
        "weather": {"database_status": weather["status"], "station_count": len(weather["stations"]), "summary": weather["summary"], "latest_poll": weather_poll},
        "db_spans": {
            "pm25": _span(settings.database_path, "pm25_observation", "phenomenon_time_utc"),
            "weather": _span(settings.weather_database_path, "weather_observation", "observation_time_utc"),
        },
        "cems": {"metadata": _json(settings.cems_metadata_path) if settings.cems_metadata_path.exists() else None},
        "firms": {"configured": firms_map_key_configured()},
    }


def _span(path: Path, table: str, column: str) -> dict[str, Any]:
    def read(connection: Any) -> tuple[Any, ...]:
        return connection.execute(f'SELECT min("{column}"), max("{column}"), count(*) FROM "{table}"').fetchone()

    row, error = _db_read(path, read)
    if error:
        return {"available": path.exists(), "status": error, "start_utc": None, "end_utc": None, "row_count": None}
    return {"available": True, "status": "OK", "start_utc": _iso(row[0]), "end_utc": _iso(row[1]), "row_count": int(row[2])}


def _manifest_dirs() -> list[Path]:
    candidates: list[Path] = []
    for root in (settings.output_root, settings.fixture_root):
        if root.exists():
            candidates.extend(path.parent for path in root.glob("*/manifest.json"))
    unique = {path.resolve(): path for path in candidates}
    return sorted(unique.values(), key=lambda path: path.name, reverse=True)


def _run_index(manifest: dict[str, Any], path: Path) -> dict[str, Any]:
    summary_path = _safe_child(path, manifest.get("produced_artifacts", {}).get("analysis_summary", "analysis_summary.json"))
    summary = _json(summary_path)
    return {
        "run_id": manifest.get("run_id"), "mode": manifest.get("mode"), "diagnostic": manifest.get("diagnostic", False),
        "synthetic_validation": is_synthetic_manifest(manifest), "analysis_start_utc": manifest.get("analysis_start_utc"),
        "analysis_end_utc": manifest.get("analysis_end_utc"), "analysis_zone": manifest.get("analysis_zone"),
        "event_count": summary.get("event_count", 0), "message": summary.get("message"), "warnings": manifest.get("warnings", []),
        "stage_status": summary.get("stage_status", {}),
    }


def _find_run(run_id: str) -> tuple[Path, dict[str, Any]]:
    for path in _manifest_dirs():
        manifest = _json(path / "manifest.json")
        if manifest.get("run_id") == run_id or path.name == run_id:
            return path, manifest
    raise HTTPException(status_code=404, detail="run not found")


def _read_csv(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def _historical_sensor_state(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    """Read the latest sensor observation at a replay cutoff, read-only."""

    try:
        cutoff = parse_iso_utc(str(manifest.get("analysis_end_utc")))
        region = _load_region()
        bbox = region["context_bbox"]
    except (KeyError, TypeError, ValueError):
        return []

    def read(connection: Any) -> list[tuple[Any, ...]]:
        return connection.execute(
            """
            WITH latest AS (
              SELECT station_id, pm25_ugm3, phenomenon_time_utc, source_status, quality_flags,
                     row_number() OVER (PARTITION BY station_id ORDER BY phenomenon_time_utc DESC) AS rn
              FROM pm25_observation
              WHERE phenomenon_time_utc <= ?
            )
            SELECT s.station_id, s.station_name, s.lat, s.lon, s.city, s.township,
                   l.pm25_ugm3, l.phenomenon_time_utc, l.source_status, l.quality_flags
            FROM sensor_station s
            LEFT JOIN latest l ON l.station_id = s.station_id AND l.rn = 1
            WHERE s.lat BETWEEN ? AND ? AND s.lon BETWEEN ? AND ?
            ORDER BY s.station_id
            """,
            [cutoff, bbox["south"], bbox["north"], bbox["west"], bbox["east"]],
        ).fetchall()

    rows, error = _db_read(settings.database_path, read)
    if error:
        return []
    sensors = []
    for row in rows or []:
        observation_time = _iso(row[7])
        age = None if row[7] is None else max(0.0, (cutoff - row[7]).total_seconds() / 60)
        sensors.append({
            "station_id": row[0], "station_name": row[1], "lat": row[2], "lon": row[3],
            "city": row[4], "township": row[5], "pm25": row[6],
            "timestamp_utc": observation_time, "observation_time_utc": observation_time,
            "freshness": "historical", "status": "historical",
            "age_minutes": round(age, 2) if age is not None else None,
            "source_status": row[8], "quality_flags": row[9],
        })
    return sensors


def _read_run_detail(path: Path, manifest: dict[str, Any]) -> dict[str, Any]:
    produced = manifest.get("produced_artifacts", {})
    summary = _json(_safe_child(path, produced.get("analysis_summary", "analysis_summary.json")))
    incidents = []
    for artifact_set in produced.get("incidents_detail", []):
        incident_path = _safe_child(path, artifact_set.get("incident", ""))
        if not incident_path.exists():
            continue
        incident = _json(incident_path)
        detail = {"incident": incident, "membership": _read_csv(_safe_child(path, artifact_set.get("membership", ""))), "source_evidence": _read_csv(_safe_child(path, artifact_set.get("source_evidence", ""))), "facilities": _read_csv(_safe_child(path, artifact_set.get("facility_candidates", ""))), "fires": _read_csv(_safe_child(path, artifact_set.get("fire_candidates", "")))}
        incidents.append(detail)
    historical_sensors = summary.get("historical_sensors", [])
    if not historical_sensors:
        historical_sensors = _historical_sensor_state(manifest)
    return {"manifest": manifest, "summary": summary, "incidents": incidents, "historical_sensors": historical_sensors}


class JobStore:
    def __init__(self) -> None:
        self.executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="airtrace-analysis")
        self.jobs: dict[str, dict[str, Any]] = {}
        self.lock = threading.Lock()

    def submit(self, request: AnalyzeRequest) -> str:
        with self.lock:
            active = sum(item["status"] in {"queued", "running"} for item in self.jobs.values())
            if active >= 2:
                raise HTTPException(status_code=429, detail="analysis queue is full; wait for an existing job")
            job_id = uuid.uuid4().hex[:12]
            self.jobs[job_id] = {"job_id": job_id, "status": "queued", "stage": "queued", "progress": 0, "error": None, "result": None, "created_at_utc": _iso(datetime.now(UTC))}
            future = self.executor.submit(self._run, job_id, request)
            self.jobs[job_id]["future"] = future
            return job_id

    def _run(self, job_id: str, request: AnalyzeRequest) -> None:
        with self.lock:
            self.jobs[job_id].update(status="running", stage="anomaly", progress=5)
        try:
            result = run_analysis(parse_iso_utc(request.start), parse_iso_utc(request.end), analysis_zone=request.analysis_zone, fast_preview=request.fast_preview, database_path=settings.database_path, weather_database_path=settings.weather_database_path, config_path=settings.config_path, output_root=settings.output_root, facilities_database_path=settings.facilities_database_path, cems_database_path=settings.cems_database_path, cems_metadata_path=settings.cems_metadata_path)
            with self.lock:
                self.jobs[job_id].update(status="completed", stage="complete", progress=100, result={"run_id": result["manifest"]["run_id"], "event_count": result["summary"]["event_count"]})
        except Exception as exc:
            with self.lock:
                self.jobs[job_id].update(status="failed", stage="failed", progress=100, error=f"{type(exc).__name__}: {exc}")

    def get(self, job_id: str) -> dict[str, Any]:
        with self.lock:
            if job_id not in self.jobs:
                raise HTTPException(status_code=404, detail="job not found")
            return {key: value for key, value in self.jobs[job_id].items() if key != "future"}


jobs = JobStore()
app = FastAPI(title="AirTrace API", version="1.0")
app.add_middleware(CORSMiddleware, allow_origins=get_cors_origins(), allow_credentials=True, allow_methods=["*"], allow_headers=["*"])


@app.get("/api/status")
def status() -> dict[str, Any]:
    return _status_payload()


@app.get("/api/live")
def live() -> dict[str, Any]:
    now = datetime.now(UTC)
    sensor_state = _sensor_state(now)
    weather_state = _weather_state(now)
    runs = [_run_index(_json(path / "manifest.json"), path) for path in _manifest_dirs()]
    latest = next((item for item in runs if item["mode"] == "LIVE_ANALYSIS"), None)
    return {"as_of_utc": _iso(now), "region": _load_region(), "sensors": sensor_state, "weather": weather_state, "latest_run": latest, "recent_runs": runs[:5]}


@app.get("/api/runs")
def runs() -> dict[str, Any]:
    return {"runs": [_run_index(_json(path / "manifest.json"), path) for path in _manifest_dirs()]}


@app.get("/api/runs/{run_id}")
def run_detail(run_id: str) -> dict[str, Any]:
    path, manifest = _find_run(run_id)
    return _read_run_detail(path, manifest)


@app.get("/api/incidents/{incident_id}")
def incident_detail(incident_id: str) -> dict[str, Any]:
    for path in _manifest_dirs():
        manifest = _json(path / "manifest.json")
        detail = _read_run_detail(path, manifest)
        for incident in detail["incidents"]:
            payload = incident["incident"]
            if payload.get("incident_id") == incident_id:
                return {"run": _run_index(manifest, path), "manifest": manifest, **incident}
    raise HTTPException(status_code=404, detail="incident not found")


@app.post("/api/analyze", status_code=202)
def analyze(request: AnalyzeRequest) -> dict[str, Any]:
    return {"job_id": jobs.submit(request), "status": "queued"}


@app.get("/api/jobs/{job_id}")
def job(job_id: str) -> dict[str, Any]:
    return jobs.get(job_id)
