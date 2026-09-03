"""AirTrace Analysis / Replay Orchestrator v1.

This module composes the existing anomaly, event, wind, backtrace, and
evidence APIs.  It intentionally owns orchestration and artifact contracts,
not any of the algorithms used by those stages.
"""

from __future__ import annotations

import csv
import hashlib
import json
import re
import time
from dataclasses import asdict, dataclass, field, is_dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Mapping

import duckdb

from airtrace.config import get_firms_map_key
from airtrace.analysis.anomaly import (
    AnomalyConfig,
    DetectionResult,
    CONTEXT_DIAGNOSTIC_REPLAY,
    NOT_PRODUCTION_EVENT_DETECTION,
    detect_anomalies_range,
    latest_observation_time,
    parse_iso_utc,
)
from airtrace.analysis.backtrace import (
    BacktraceConfig,
    no_traceable_event_payload,
    trace_event,
    write_evidence_csv as write_trace_evidence_csv,
    write_trace_json,
    write_trace_map,
)
from airtrace.analysis.events import (
    EventConfig,
    build_event_payload,
    write_event_csv,
    write_event_json,
    write_event_map,
    write_membership_csv,
)
from airtrace.analysis.evidence import (
    match_source_evidence,
    write_evidence_json,
    write_evidence_map,
    write_facility_csv,
    write_fire_csv,
)
from airtrace.analysis.wind import WindConfig, estimate_to_dict, get_wind
from airtrace.data.cems import load_cems
from airtrace.data.facilities import load_facilities
from airtrace.public_paths import public_path, sanitize_public_paths


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATABASE = ROOT / "data" / "airtrace.duckdb"
DEFAULT_WEATHER_DATABASE = ROOT / "data" / "weather.duckdb"
DEFAULT_CONFIG = ROOT / "config" / "pilot_region.json"
DEFAULT_OUTPUT_ROOT = ROOT / "reports" / "pipeline"
DEFAULT_FACILITIES_DATABASE = ROOT / "data" / "facilities.duckdb"
DEFAULT_CEMS_DATABASE = ROOT / "data" / "cems.duckdb"
DEFAULT_CEMS_METADATA = ROOT / "data" / "cems_ingest_metadata.json"
DEFAULT_REFERENCE_DATABASE = ROOT / "data" / "reference_air.duckdb"
PIPELINE_VERSION = "analysis-pipeline-v1"
_SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")


@dataclass(frozen=True)
class EvidenceConfig:
    """Evidence-stage inputs; scoring remains in evidence.py."""

    source_buffer_km: float = 3.0
    firms_window_hours: float = 12.0
    max_facilities: int = 50
    max_fire_groups: int = 20
    firms_enabled: bool = True


@dataclass
class _Stage:
    name: str
    status: str = "pending"
    started_at: str | None = None
    finished_at: str | None = None
    duration_seconds: float = 0.0
    warnings: list[str] = field(default_factory=list)
    error: str | None = None

    def payload(self) -> dict[str, Any]:
        return asdict(self)


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _iso(value: datetime | None) -> str | None:
    return _utc(value).isoformat(timespec="seconds").replace("+00:00", "Z") if value else None


def _jsonable(value: Any) -> Any:
    if isinstance(value, Path):
        return public_path(value)
    if isinstance(value, datetime):
        return _iso(value)
    if is_dataclass(value):
        return _jsonable(asdict(value))
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(sanitize_public_paths(_jsonable(payload)), ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def _safe_output_name(value: str) -> str:
    name = str(value).strip()
    if not name or not _SAFE_NAME.fullmatch(name) or name in {".", ".."}:
        raise ValueError("output_name must contain only letters, numbers, '.', '_' or '-' and cannot be a path")
    return name


def _run_id(start: datetime, end: datetime, analysis_zone: str, fast_preview: bool) -> str:
    material = f"{_iso(start)}|{_iso(end)}|{analysis_zone}|fast={fast_preview}|{PIPELINE_VERSION}"
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()[:10]
    compact_start = _iso(start).replace("-", "").replace(":", "").replace("T", "").replace("Z", "")  # type: ignore[union-attr]
    compact_end = _iso(end).replace("-", "").replace(":", "").replace("T", "").replace("Z", "")  # type: ignore[union-attr]
    return f"AIRTRACE_{compact_start}_{compact_end}_{analysis_zone}_{digest}"


def _stage_run(stage: _Stage, operation: Callable[[], Any]) -> Any:
    stage.status = "running"
    started = time.perf_counter()
    stage.started_at = _iso(datetime.now(UTC))
    try:
        result = operation()
        stage.status = "completed"
        return result
    except Exception as exc:  # stage callers decide whether a core failure is fatal
        stage.status = "failed"
        stage.error = f"{type(exc).__name__}: {exc}"
        return None
    finally:
        stage.duration_seconds = round(time.perf_counter() - started, 6)
        stage.finished_at = _iso(datetime.now(UTC))


def _skip_stage(name: str, status: str, warning: str | None = None) -> _Stage:
    stage = _Stage(name=name, status=status)
    if warning:
        stage.warnings.append(warning)
    return stage


def _db_span(path: Path, table: str, column: str) -> dict[str, Any]:
    result: dict[str, Any] = {"path": public_path(path), "available": path.exists(), "start_utc": None, "end_utc": None, "row_count": None}
    if not path.exists():
        return result
    connection = duckdb.connect(str(path), read_only=True)
    try:
        row = connection.execute(f'SELECT min("{column}"), max("{column}"), count(*) FROM "{table}"').fetchone()
        result.update({"start_utc": _iso(row[0]), "end_utc": _iso(row[1]), "row_count": int(row[2])})
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        connection.close()
    return result


def _load_table(path: Path, loader: Callable[[Any], list[dict[str, Any]]]) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    connection = duckdb.connect(str(path), read_only=True)
    try:
        return loader(connection)
    finally:
        connection.close()


def _config_snapshot(
    anomaly_config: AnomalyConfig,
    event_config: EventConfig,
    wind_config: WindConfig,
    backtrace_config: BacktraceConfig,
    evidence_config: EvidenceConfig,
    pilot_config_path: Path,
) -> dict[str, Any]:
    backtrace_values = asdict(backtrace_config)
    backtrace_values.pop("wind_getter", None)
    return {
        "anomaly": asdict(anomaly_config),
        "event": asdict(event_config),
        "wind": asdict(wind_config),
        "backtrace": _jsonable(backtrace_values),
        "evidence": asdict(evidence_config),
        "pilot_region": json.loads(pilot_config_path.read_text(encoding="utf-8")),
    }


def _representative_wind(
    event: Mapping[str, Any],
    database_path: Path,
    config: WindConfig,
    getter: Callable[..., Any] | None,
) -> dict[str, Any]:
    centroid = event.get("latest_centroid") or event.get("initial_centroid") or {}
    lat, lon = centroid.get("lat"), centroid.get("lon")
    at = parse_iso_utc(str(event.get("last_seen_time_utc") or event.get("start_time_utc")))
    if lat is None or lon is None:
        raise ValueError("event has no centroid coordinates")
    estimate = getter(float(lat), float(lon), at) if getter else get_wind(float(lat), float(lon), at, database_path=database_path, config=config)
    converted = estimate_to_dict(estimate) if is_dataclass(estimate) else _jsonable(estimate)
    if not isinstance(converted, Mapping):
        raise ValueError("wind getter returned an unsupported result")
    quality = converted.get("quality_category") or converted.get("quality") or "UNKNOWN"
    return {
        "quality": quality,
        "representative": dict(converted),
        "disagreement": {
            "vector_disagreement_mps": converted.get("vector_disagreement_mps"),
            "station_count": converted.get("station_count"),
            "nearest_station_km": converted.get("nearest_station_km"),
            "farthest_station_km": converted.get("farthest_station_km"),
        },
    }


def _incident_event_payload(event_payload: Mapping[str, Any], event: Mapping[str, Any], incident_id: str) -> dict[str, Any]:
    event_id = event.get("event_id")
    return {
        "schema_version": event_payload.get("schema_version", 1),
        "analysis_window": event_payload.get("analysis_window"),
        "analysis_zone": event_payload.get("analysis_zone", "core"),
        "zones": event_payload.get("zones", {}),
        "summary": {"event_count": 1},
        "events": [{**dict(event), "incident_id": incident_id}],
        "membership": [row for row in event_payload.get("membership", []) if row.get("event_id") == event_id],
        "context_sensors": event_payload.get("context_sensors", []),
    }


def _incident_row(incident: Mapping[str, Any]) -> dict[str, Any]:
    event = incident.get("event", {})
    return {
        "incident_id": incident.get("incident_id"),
        "event_id": event.get("event_id"),
        "start_time": event.get("start_time_utc"),
        "end_time": event.get("last_seen_time_utc"),
        "duration_minutes": event.get("duration_minutes"),
        "analysis_zone": incident.get("analysis_zone"),
        "event_strength": event.get("event_strength"),
        "sensor_count": event.get("unique_sensor_count"),
        "seed_count": event.get("peak_seed_count"),
        "peak_pm25": event.get("peak_pm25"),
        "max_anomaly_score": event.get("max_anomaly_score"),
        "trace_status": incident.get("trace", {}).get("status"),
        "evidence_status": incident.get("evidence", {}).get("status"),
        "firms_status": incident.get("evidence", {}).get("firms_status"),
        "cems_status": incident.get("evidence", {}).get("cems_status"),
        "reference_aq_status": incident.get("reference_aq", {}).get("status"),
        "no_strong_facility_match": incident.get("evidence", {}).get("no_strong_facility_match"),
        "limitations": "; ".join(incident.get("limitations", [])),
    }


def _write_incidents_csv(path: Path, incidents: list[Mapping[str, Any]]) -> None:
    fields = list(_incident_row({}).keys())
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(_incident_row(item) for item in incidents)


def _read_cems_metadata(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"status": "METADATA_UNREADABLE"}


def _reference_status(path: Path, start: datetime, end: datetime) -> dict[str, Any]:
    result = {"path": public_path(path), "status": "REFERENCE_AQ_UNAVAILABLE", "rows_in_window": 0}
    if not path.exists():
        return result
    connection = duckdb.connect(str(path), read_only=True)
    try:
        count = connection.execute(
            'SELECT count(*) FROM "reference_air_observation" WHERE "publish_time_utc" >= ? AND "publish_time_utc" <= ?',
            [start, end],
        ).fetchone()[0]
        result["rows_in_window"] = int(count)
        if count:
            result["status"] = "REFERENCE_AQ_AVAILABLE"
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        connection.close()
    return result


def _status_for_evidence(firms_diagnostics: Mapping[str, Any], cems_rows: list[dict[str, Any]], cems_metadata: Mapping[str, Any] | None) -> tuple[str, str]:
    if not firms_diagnostics.get("requested") and any(
        "missing" in str(item).lower() or "not set" in str(item).lower()
        for item in firms_diagnostics.get("errors", [])
    ):
        firms_status = "NOT_QUERIED_MISSING_KEY"
    elif firms_diagnostics.get("errors"):
        firms_status = "PARTIAL / UNAVAILABLE"
    elif sum(int(item.get("detections", 0)) for item in firms_diagnostics.get("sources", {}).values()) == 0:
        firms_status = "NO FIRMS HOTSPOT DETECTED"
    else:
        firms_status = "QUERIED"
    if not cems_rows:
        cems_status = "CEMS_CONTEXT_UNAVAILABLE"
    elif cems_metadata and cems_metadata.get("dataset_complete") is False:
        cems_status = "PARTIAL / UNAVAILABLE"
    else:
        cems_status = "AVAILABLE"
    return firms_status, cems_status


def run_analysis(
    start_time: datetime | None = None,
    end_time: datetime | None = None,
    analysis_zone: str = "core",
    event_id: str | None = None,
    *,
    latest: bool = False,
    latest_hours: float = 1.5,
    database_path: Path = DEFAULT_DATABASE,
    weather_database_path: Path = DEFAULT_WEATHER_DATABASE,
    config_path: Path = DEFAULT_CONFIG,
    output_root: Path = DEFAULT_OUTPUT_ROOT,
    output_name: str | None = None,
    fast_preview: bool = False,
    anomaly_lookback_hours: float = 2.0,
    anomaly_config: AnomalyConfig | None = None,
    event_config: EventConfig | None = None,
    wind_config: WindConfig | None = None,
    backtrace_config: BacktraceConfig | None = None,
    evidence_config: EvidenceConfig | Mapping[str, Any] | None = None,
    facilities_database_path: Path = DEFAULT_FACILITIES_DATABASE,
    cems_database_path: Path = DEFAULT_CEMS_DATABASE,
    cems_metadata_path: Path = DEFAULT_CEMS_METADATA,
    reference_air_database_path: Path = DEFAULT_REFERENCE_DATABASE,
    firms_map_key: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Run one bounded analysis window and materialize its report directory.

    ``latest=True`` resolves the window from the current PM2.5 database and is
    marked LIVE_ANALYSIS. Explicit windows are REPLAY. The default backtrace
    mode keeps exact-coordinate caching; ``fast_preview`` is the only opt-in
    for 500 m spatial quantization.
    """

    from scripts.match_source_evidence import fetch_firms

    analysis_zone = str(analysis_zone).strip().lower()
    if analysis_zone not in {"core", "context"}:
        raise ValueError("analysis_zone must be one of: core, context")
    explicit_window = start_time is not None and end_time is not None
    if start_time is None or end_time is None:
        if not latest:
            raise ValueError("start_time and end_time are required unless latest=True")
        if latest_hours <= 0:
            raise ValueError("latest_hours must be positive")
        resolved_end = latest_observation_time(database_path)
        start_time, end_time = resolved_end - timedelta(hours=latest_hours), resolved_end
    start, end = _utc(start_time), _utc(end_time)
    if end < start:
        raise ValueError("end_time must be at or after start_time")
    if anomaly_lookback_hours <= 0:
        raise ValueError("anomaly_lookback_hours must be positive")
    mode = "REPLAY" if explicit_window else "LIVE_ANALYSIS"
    stable_now = _utc(now or datetime.now(UTC))
    anomaly_cfg = anomaly_config or AnomalyConfig()
    event_cfg = event_config or EventConfig()
    wind_cfg = wind_config or WindConfig()
    evidence_cfg = evidence_config if isinstance(evidence_config, EvidenceConfig) else EvidenceConfig(**dict(evidence_config or {}))
    if backtrace_config is None:
        trace_cfg = BacktraceConfig(
            database_path=weather_database_path,
            region_config_path=config_path,
            wind_cache_quantized=bool(fast_preview),
            wind_cache_spatial_m=500.0 if fast_preview else 250.0,
        )
    else:
        trace_cfg = backtrace_config
        if fast_preview and not trace_cfg.wind_cache_quantized:
            trace_cfg = replace(trace_cfg, wind_cache_quantized=True, wind_cache_spatial_m=500.0)
    run_id = _run_id(start, end, analysis_zone, fast_preview)
    run_dir = Path(output_root) / _safe_output_name(output_name) if output_name else Path(output_root) / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    stages: list[_Stage] = []
    anomaly_stage = _Stage("anomaly")
    stages.append(anomaly_stage)
    anomaly_results = _stage_run(anomaly_stage, lambda: detect_anomalies_range(
        database_path, config_path, start, end, lookback_hours=max(1.0, anomaly_lookback_hours),
        config=anomaly_cfg, now=stable_now, analysis_zone=analysis_zone,
    ))
    if anomaly_results is None:
        raise RuntimeError(anomaly_stage.error or "anomaly stage failed")

    event_stage = _Stage("events")
    stages.append(event_stage)
    event_payload = _stage_run(event_stage, lambda: build_event_payload(
        anomaly_results, config_path, event_cfg, start, end, analysis_zone,
    ))
    if event_payload is None:
        raise RuntimeError(event_stage.error or "events stage failed")
    selected_events = list(event_payload.get("events", []))
    if event_id:
        selected_events = [event for event in selected_events if event.get("event_id") == event_id]
        if not selected_events:
            event_stage.warnings.append(f"event_id not found in selected window: {event_id}")
    no_events = not selected_events
    if no_events:
        event_stage.warnings.append("NO EVENTS DETECTED" if not event_id else "NO SELECTED EVENTS")

    incidents: list[dict[str, Any]] = []
    if no_events:
        stages.extend([
            _skip_stage("wind_diagnostics", "skipped_no_event", "NO EVENTS DETECTED"),
            _skip_stage("backtrace", "skipped_no_event", "NO EVENTS DETECTED"),
            _skip_stage("evidence", "skipped_no_event", "NO EVENTS DETECTED"),
        ])
    else:
        wind_stage = _Stage("wind_diagnostics")
        stages.append(wind_stage)
        wind_results: dict[str, dict[str, Any]] = {}

        def run_wind() -> None:
            for event in selected_events:
                try:
                    wind_results[str(event["event_id"])] = _representative_wind(event, weather_database_path, wind_cfg, trace_cfg.wind_getter)
                except Exception as exc:
                    wind_results[str(event["event_id"])] = {"quality": "UNAVAILABLE", "error": f"{type(exc).__name__}: {exc}", "representative": {}, "disagreement": {}}
                    wind_stage.warnings.append(f"{event.get('event_id')}: wind diagnostics unavailable")
        _stage_run(wind_stage, run_wind)

        backtrace_stage = _Stage("backtrace")
        stages.append(backtrace_stage)
        traces: dict[str, dict[str, Any]] = {}

        def run_backtrace() -> None:
            for event in selected_events:
                original_id = str(event["event_id"])
                memberships = [row for row in event_payload.get("membership", []) if row.get("event_id") == event.get("event_id")]
                try:
                    traces[original_id] = trace_event(event, memberships, trace_cfg)
                    if not str(traces[original_id].get("status", "")).startswith("TRACE_COMPLETE"):
                        backtrace_stage.warnings.append(f"{original_id}: TRACE_FAILED ({traces[original_id].get('status')})")
                except Exception as exc:
                    traces[original_id] = {**no_traceable_event_payload("TRACE_FAILED"), "event": dict(event), "error": f"{type(exc).__name__}: {exc}"}
                    backtrace_stage.warnings.append(f"{original_id}: TRACE_FAILED ({type(exc).__name__})")
        _stage_run(backtrace_stage, run_backtrace)

        evidence_stage = _Stage("evidence")
        stages.append(evidence_stage)
        evidence_results: dict[str, dict[str, Any]] = {}
        map_key = firms_map_key if firms_map_key is not None else get_firms_map_key()
        facilities: list[dict[str, Any]] = []
        cems: list[dict[str, Any]] = []
        cems_metadata = _read_cems_metadata(Path(cems_metadata_path))

        def run_evidence() -> None:
            nonlocal facilities, cems
            try:
                facilities = _load_table(Path(facilities_database_path), load_facilities)
            except Exception as exc:
                evidence_stage.warnings.append(f"facility catalog unavailable: {type(exc).__name__}")
            try:
                cems = _load_table(Path(cems_database_path), load_cems)
            except Exception as exc:
                evidence_stage.warnings.append(f"CEMS context unavailable: {type(exc).__name__}")
            for event in selected_events:
                original_id = str(event["event_id"])
                trace = traces[original_id]
                try:
                    if evidence_cfg.firms_enabled:
                        fires, firms_diagnostics = fetch_firms(trace, map_key, evidence_cfg.firms_window_hours)
                    else:
                        fires, firms_diagnostics = [], {"requested": False, "errors": ["FIRMS query disabled by configuration"], "sources": {}}
                    report = match_source_evidence(
                        trace, facilities, fires, cems,
                        source_buffer_km=evidence_cfg.source_buffer_km,
                        firms_window_hours=evidence_cfg.firms_window_hours,
                        max_facilities=evidence_cfg.max_facilities,
                        max_fire_groups=evidence_cfg.max_fire_groups,
                    )
                    firms_status, cems_status = _status_for_evidence(firms_diagnostics, cems, cems_metadata)
                    report["firms_query_diagnostics"] = firms_diagnostics
                    report["firms_status"] = firms_status
                    report["cems_status"] = cems_status
                    report["cems_ingestion_diagnostics"] = {"db_rows_loaded": len(cems), "metadata": cems_metadata}
                    report["no_strong_match_flag"] = not bool(report.get("facility_matches")) or report.get("facility_matches", [{}])[0].get("evidence_score", 0) < 0.35
                    report["no_strong_facility_match"] = report["no_strong_match_flag"]
                    evidence_results[original_id] = report
                except Exception as exc:
                    evidence_results[original_id] = {"schema_version": 1, "status": "EVIDENCE_PARTIAL", "firms_status": "PARTIAL / UNAVAILABLE", "cems_status": "CEMS_CONTEXT_UNAVAILABLE", "facility_matches": [], "fire_matches": [], "limitations": [f"Evidence stage failed: {type(exc).__name__}: {exc}"], "error": f"{type(exc).__name__}: {exc}"}
                    evidence_stage.warnings.append(f"{original_id}: evidence partial ({type(exc).__name__})")
        _stage_run(evidence_stage, run_evidence)

        for sequence, event in enumerate(sorted(selected_events, key=lambda item: (str(item.get("start_time_utc")), str(item.get("event_id")))), 1):
            original_id = str(event["event_id"])
            incident_id = f"AIRTRACE_{str(event['start_time_utc']).replace('-', '').replace(':', '').replace('T', '').replace('Z', '')}_{sequence:03d}"
            trace = {**traces[original_id], "event": {**dict(traces[original_id].get("event") or event), "incident_id": incident_id}}
            evidence = evidence_results[original_id]
            limitations = list(event.get("limitations", []))
            if str(trace.get("status", "")).startswith("TRACE_FAILED") or trace.get("status") not in {"TRACE_COMPLETE"}:
                limitations.append("TRACE_FAILED: source trace was unavailable or incomplete.")
            if evidence.get("cems_status") != "AVAILABLE":
                limitations.append(str(evidence.get("cems_status")))
            incident = {
                "incident_id": incident_id,
                "event_id": original_id,
                "analysis_zone": analysis_zone,
                "event": dict(event),
                "reference_aq": None,
                "wind": wind_results.get(original_id, {"quality": "UNAVAILABLE", "representative": {}, "disagreement": {}}),
                "trace": trace,
                "evidence": evidence,
                "limitations": list(dict.fromkeys(item for item in limitations if item)),
            }
            incidents.append(incident)

    stages_payload = [stage.payload() for stage in stages]
    reference_aq = _reference_status(Path(reference_air_database_path), start, end)
    for incident in incidents:
        incident["reference_aq"] = reference_aq
    config_snapshot = _config_snapshot(anomaly_cfg, event_cfg, wind_cfg, trace_cfg, evidence_cfg, Path(config_path))
    config_path_out = run_dir / "config_snapshot.json"
    _write_json(config_path_out, config_snapshot)
    for incident in incidents:
        incident_dir = run_dir / f"incident_{incident['incident_id']}"
        incident_dir.mkdir(parents=True, exist_ok=True)
        event_view = _incident_event_payload(event_payload, incident["event"], incident["incident_id"])
        incident["artifacts"] = {
            "incident": (incident_dir / "incident.json").relative_to(run_dir).as_posix(),
            "event_map": (incident_dir / "event_map.html").relative_to(run_dir).as_posix(),
            "source_trace": (incident_dir / "source_trace.html").relative_to(run_dir).as_posix(),
            "source_trace_json": (incident_dir / "source_trace.json").relative_to(run_dir).as_posix(),
            "evidence_map": (incident_dir / "evidence_map.html").relative_to(run_dir).as_posix(),
            "evidence_json": (incident_dir / "evidence.json").relative_to(run_dir).as_posix(),
            "membership": (incident_dir / "membership.csv").relative_to(run_dir).as_posix(),
            "source_evidence": (incident_dir / "source_evidence.csv").relative_to(run_dir).as_posix(),
            "facility_candidates": (incident_dir / "facility_candidates.csv").relative_to(run_dir).as_posix(),
            "fire_candidates": (incident_dir / "fire_candidates.csv").relative_to(run_dir).as_posix(),
        }
        trace_payload = incident["trace"]
        write_event_map(event_view, incident_dir / "event_map.html")
        write_membership_csv(event_view, incident_dir / "membership.csv")
        write_trace_map(trace_payload, incident_dir / "source_trace.html")
        incident["trace"] = sanitize_public_paths(trace_payload)
        write_trace_json(incident["trace"], incident_dir / "source_trace.json")
        write_trace_evidence_csv(incident["trace"], incident_dir / "source_evidence.csv")
        write_evidence_json(incident["evidence"], incident_dir / "evidence.json")
        write_facility_csv(incident["evidence"], incident_dir / "facility_candidates.csv")
        write_fire_csv(incident["evidence"], incident_dir / "fire_candidates.csv")
        write_evidence_map(incident["evidence"], incident["trace"], incident_dir / "evidence_map.html")
        _write_json(incident_dir / "incident.json", incident)

    summary = {
        "schema_version": 1,
        "run_id": run_id,
        "mode": mode,
        "diagnostic": analysis_zone == "context",
        "message": "NO EVENTS DETECTED" if no_events else None,
        "analysis_window": {"start_time_utc": _iso(start), "end_time_utc": _iso(end)},
        "analysis_zone": analysis_zone,
        "reference_aq": reference_aq,
        "event_count": len(incidents),
        "events": [_incident_row(item) for item in incidents],
        "stage_status": {stage.name: stage.status for stage in stages},
        # Presentation-only snapshot for replay consumers. The analysis
        # stages continue to use the same rows and semantics as before.
        "historical_sensors": event_payload.get("context_sensors", []),
    }
    manifest_warnings = list(dict.fromkeys(warning for stage in stages for warning in stage.warnings))
    if analysis_zone == "context":
        manifest_warnings.extend([CONTEXT_DIAGNOSTIC_REPLAY, NOT_PRODUCTION_EVENT_DETECTION])
    manifest = {
        "schema_version": 1,
        "run_id": run_id,
        "pipeline_version": PIPELINE_VERSION,
        "code_config_semantics_version": "existing-algorithm-v1-semantics-preserved",
        "mode": mode,
        "diagnostic": analysis_zone == "context",
        "analysis_start_utc": _iso(start),
        "analysis_end_utc": _iso(end),
        "analysis_zone": analysis_zone,
        "event_id_filter": event_id,
        "fast_preview": fast_preview,
        "provenance": {"type": "observed_replay" if mode == "REPLAY" else "live_observation"},
        "input_db_spans": {
            "pm25": _db_span(Path(database_path), "pm25_observation", "phenomenon_time_utc"),
            "weather": _db_span(Path(weather_database_path), "weather_observation", "observation_time_utc"),
            "facilities": _db_span(Path(facilities_database_path), "facility", "ingested_at_utc"),
            "cems": _db_span(Path(cems_database_path), "cems_measurement", "measurement_time_utc"),
            "reference_aq": _db_span(Path(reference_air_database_path), "reference_air_observation", "publish_time_utc"),
        },
        "pipeline_stages": stages_payload,
        "produced_artifacts": {
            "manifest": "manifest.json",
            "analysis_summary": "analysis_summary.json",
            "incidents": "incidents.csv",
            "README": "README.md",
            "config_snapshot": "config_snapshot.json",
            "incidents_detail": [incident["artifacts"] for incident in incidents],
        },
        "warnings": manifest_warnings,
        "manual_diagnostic_flags": {
            "context_diagnostic_replay": analysis_zone == "context",
            "not_production_event_detection": analysis_zone == "context",
            "known_fire_validation_loaded": False,
            "known_fire_coordinates_in_replay": False,
        },
    }
    _write_json(run_dir / "analysis_summary.json", summary)
    _write_incidents_csv(run_dir / "incidents.csv", incidents)
    readme = [
        f"# AirTrace Analysis Pipeline v1 — {run_id}",
        "",
        f"Mode: {mode}",
        f"Window: {_iso(start)} → {_iso(end)}",
        f"Analysis zone: {analysis_zone}",
        "",
        "This directory is a reproducible pipeline/replay report. Existing anomaly, event, wind, backtrace, and evidence semantics are reused unchanged.",
    ]
    if no_events:
        readme.extend(["", "## Result", "", "NO EVENTS DETECTED", "", "Wind, backtrace, and evidence stages were skipped because there were no selected events."])
    if analysis_zone == "context":
        readme.extend(["", CONTEXT_DIAGNOSTIC_REPLAY, NOT_PRODUCTION_EVENT_DETECTION])
    readme.extend(["", "Scores in this report are evidence/strength scores, not probabilities."])
    (run_dir / "README.md").write_text("\n".join(readme) + "\n", encoding="utf-8")
    _write_json(run_dir / "manifest.json", manifest)
    return {"manifest": manifest, "summary": summary, "stages": stages_payload, "incidents": incidents, "output_dir": str(run_dir)}


__all__ = ["EvidenceConfig", "run_analysis"]
