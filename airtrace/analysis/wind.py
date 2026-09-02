"""Deterministic CWA wind-field interpolation diagnostics.

This module deliberately stops at a local, explainable wind estimate.  It
does not perform particle tracing, plume/source inference, or write to the
weather recorder database.

The interpolation convention is Cartesian in the local compass frame:
``u_east_mps`` is positive toward east and ``v_north_mps`` is positive toward
north.  Meteorological *from* directions are only used for display and are
never averaged directly.
"""

from __future__ import annotations

import json
import math
import time as time_module
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

import duckdb

from airtrace.data.cwa import haversine_km, iso_utc


UTC = timezone.utc
DEFAULT_DATABASE = Path(__file__).resolve().parents[2] / "data" / "weather.duckdb"
DEFAULT_PREFERRED_RADIUS_KM = 15.0
DEFAULT_MAXIMUM_RADIUS_KM = 30.0
DEFAULT_MIN_STATIONS = 3
DEFAULT_MAX_STATIONS = 8
DEFAULT_MAX_TEMPORAL_DISTANCE_MINUTES = 15.0
DEFAULT_IDW_POWER = 2.0
DEFAULT_IDW_EPSILON_KM = 0.1
READ_RETRIES = 3
READ_RETRY_DELAYS_SECONDS = (0.15, 0.35, 0.75)


class WindFieldError(RuntimeError):
    """An expected wind-field data or configuration failure."""


@dataclass(frozen=True)
class WindConfig:
    preferred_radius_km: float = DEFAULT_PREFERRED_RADIUS_KM
    maximum_radius_km: float = DEFAULT_MAXIMUM_RADIUS_KM
    minimum_stations: int = DEFAULT_MIN_STATIONS
    maximum_stations: int = DEFAULT_MAX_STATIONS
    maximum_temporal_distance_minutes: float = DEFAULT_MAX_TEMPORAL_DISTANCE_MINUTES
    idw_power: float = DEFAULT_IDW_POWER
    idw_epsilon_km: float = DEFAULT_IDW_EPSILON_KM

    def __post_init__(self) -> None:
        if self.preferred_radius_km <= 0 or self.maximum_radius_km < self.preferred_radius_km:
            raise ValueError("radius configuration is invalid")
        if self.minimum_stations < 1 or self.maximum_stations < self.minimum_stations:
            raise ValueError("station-count configuration is invalid")
        if self.maximum_temporal_distance_minutes <= 0:
            raise ValueError("maximum_temporal_distance_minutes must be positive")
        if self.idw_power <= 0 or self.idw_epsilon_km <= 0:
            raise ValueError("IDW power and epsilon must be positive")


@dataclass(frozen=True)
class RawWindObservation:
    station_id: str
    observation_time_utc: datetime
    wind_from_deg: float | None
    wind_speed_mps: float | None
    u_east_mps: float | None
    v_north_mps: float | None
    wind_status: str
    quality_flags: tuple[str, ...] = ()


@dataclass(frozen=True)
class TemporalWindValue:
    station_id: str
    observation_time_utc: datetime
    u_east_mps: float
    v_north_mps: float
    wind_status: str
    temporal_mode: str
    temporal_offset_minutes: float
    source_before_utc: datetime | None = None
    source_after_utc: datetime | None = None
    source_observation_utc: datetime | None = None
    quality_flags: tuple[str, ...] = ()


@dataclass(frozen=True)
class StationWindUse:
    station_id: str
    station_name: str
    lat: float
    lon: float
    distance_km: float
    observation_time_utc: datetime | None
    source_before_utc: datetime | None
    source_after_utc: datetime | None
    temporal_mode: str
    temporal_offset_minutes: float
    u_east_mps: float
    v_north_mps: float
    speed_mps: float
    wind_to_deg: float | None
    wind_from_deg: float | None
    wind_status: str
    quality_flags: tuple[str, ...]
    weight: float


@dataclass(frozen=True)
class WindEstimate:
    query_lat: float
    query_lon: float
    query_time_utc: datetime
    u_east_mps: float | None
    v_north_mps: float | None
    speed_mps: float | None
    wind_to_deg: float | None
    wind_from_deg: float | None
    station_count: int
    stations_used: tuple[StationWindUse, ...]
    nearest_station_km: float | None
    farthest_station_km: float | None
    temporal_offset_minutes: float | None
    spatial_uncertainty: str
    temporal_uncertainty: str
    vector_disagreement_mps: float | None
    confidence: float | None
    quality_category: str
    diagnostics: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class _Station:
    station_id: str
    station_name: str
    lat: float | None
    lon: float | None


@dataclass(frozen=True)
class _Snapshot:
    stations: tuple[_Station, ...]
    observations_by_station: dict[str, tuple[RawWindObservation, ...]]
    database_start_utc: datetime | None
    database_end_utc: datetime | None


def utc_datetime(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("query time must be timezone-aware; use an explicit UTC offset")
    return value.astimezone(UTC)


def parse_query_time(value: datetime | str) -> datetime:
    if isinstance(value, datetime):
        return utc_datetime(value)
    if not isinstance(value, str) or not value.strip():
        raise ValueError("time must be a timezone-aware datetime or ISO-8601 string")
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        return utc_datetime(datetime.fromisoformat(text))
    except ValueError as exc:
        raise ValueError(f"invalid timezone-aware ISO time: {value}") from exc


def vector_from_wind_from(wind_from_deg: float, speed_mps: float) -> tuple[float, float]:
    """Convert meteorological FROM direction into a vector pointing TO."""

    if not math.isfinite(float(wind_from_deg)) or not math.isfinite(float(speed_mps)):
        raise ValueError("wind direction and speed must be finite")
    if speed_mps < 0:
        raise ValueError("wind speed must be non-negative")
    theta = math.radians(float(wind_from_deg) % 360.0)
    return -float(speed_mps) * math.sin(theta), -float(speed_mps) * math.cos(theta)


def directions_from_vector(u_east_mps: float, v_north_mps: float) -> tuple[float, float | None, float | None]:
    """Return speed, TO degrees, and meteorological FROM degrees."""

    u = float(u_east_mps)
    v = float(v_north_mps)
    if not math.isfinite(u) or not math.isfinite(v):
        raise ValueError("wind vector must be finite")
    speed = math.hypot(u, v)
    if speed <= 1e-12:
        return 0.0, None, None  # type: ignore[return-value]
    wind_to = math.degrees(math.atan2(u, v)) % 360.0
    wind_from = (wind_to + 180.0) % 360.0
    return speed, wind_to, wind_from


def _usable_vector(observation: RawWindObservation) -> tuple[float, float] | None:
    status = observation.wind_status.casefold()
    if status == "calm":
        return 0.0, 0.0
    if status != "valid" or observation.u_east_mps is None or observation.v_north_mps is None:
        return None
    u, v = float(observation.u_east_mps), float(observation.v_north_mps)
    if not math.isfinite(u) or not math.isfinite(v):
        return None
    return u, v


def temporal_select(
    observations: Sequence[RawWindObservation],
    query_time: datetime,
    maximum_temporal_distance_minutes: float = DEFAULT_MAX_TEMPORAL_DISTANCE_MINUTES,
) -> TemporalWindValue | None:
    """Select or linearly interpolate one station's vector around a query."""

    query_time = utc_datetime(query_time)
    tolerance = timedelta(minutes=maximum_temporal_distance_minutes)
    usable = [
        item for item in observations
        if abs(utc_datetime(item.observation_time_utc) - query_time) <= tolerance
        and _usable_vector(item) is not None
    ]
    if not usable:
        return None
    usable.sort(key=lambda item: (utc_datetime(item.observation_time_utc), item.station_id))
    exact = [item for item in usable if utc_datetime(item.observation_time_utc) == query_time]
    if exact:
        item = exact[0]
        u, v = _usable_vector(item)  # type: ignore[misc]
        return TemporalWindValue(
            item.station_id, query_time, u, v, item.wind_status, "exact", 0.0,
            source_observation_utc=query_time, quality_flags=item.quality_flags,
        )

    before = max((item for item in usable if utc_datetime(item.observation_time_utc) < query_time), default=None, key=lambda item: item.observation_time_utc)
    after = min((item for item in usable if utc_datetime(item.observation_time_utc) > query_time), default=None, key=lambda item: item.observation_time_utc)
    before_delta = query_time - utc_datetime(before.observation_time_utc) if before else None
    after_delta = utc_datetime(after.observation_time_utc) - query_time if after else None
    if before and after and before_delta <= tolerance and after_delta <= tolerance:
        before_u, before_v = _usable_vector(before)  # type: ignore[misc]
        after_u, after_v = _usable_vector(after)  # type: ignore[misc]
        total = (utc_datetime(after.observation_time_utc) - utc_datetime(before.observation_time_utc)).total_seconds()
        fraction = (query_time - utc_datetime(before.observation_time_utc)).total_seconds() / total
        return TemporalWindValue(
            before.station_id, query_time,
            before_u + fraction * (after_u - before_u),
            before_v + fraction * (after_v - before_v),
            "calm" if before.wind_status.casefold() == after.wind_status.casefold() == "calm" else "valid",
            "bracketed", round(max(before_delta, after_delta).total_seconds() / 60.0, 6),
            source_before_utc=utc_datetime(before.observation_time_utc),
            source_after_utc=utc_datetime(after.observation_time_utc),
            quality_flags=tuple(dict.fromkeys(before.quality_flags + after.quality_flags)),
        )

    nearest = min(
        usable,
        key=lambda item: (abs(utc_datetime(item.observation_time_utc) - query_time), utc_datetime(item.observation_time_utc) > query_time, item.station_id),
    )
    u, v = _usable_vector(nearest)  # type: ignore[misc]
    offset = abs(utc_datetime(nearest.observation_time_utc) - query_time).total_seconds() / 60.0
    return TemporalWindValue(
        nearest.station_id, utc_datetime(nearest.observation_time_utc), u, v, nearest.wind_status,
        "nearest_only", round(offset, 6), source_observation_utc=utc_datetime(nearest.observation_time_utc),
        quality_flags=nearest.quality_flags,
    )


def _idw_weights(samples: Sequence[StationWindUse], power: float, epsilon_km: float) -> list[float]:
    return [1.0 / max(sample.distance_km, epsilon_km) ** power for sample in samples]


def interpolate_vectors(
    samples: Sequence[StationWindUse],
    power: float = DEFAULT_IDW_POWER,
    epsilon_km: float = DEFAULT_IDW_EPSILON_KM,
) -> tuple[float, float, float]:
    """Return IDW u, v, and weighted RMS vector disagreement."""

    if not samples:
        raise ValueError("at least one station is required")
    weights = _idw_weights(samples, power, epsilon_km)
    total = sum(weights)
    u = sum(weight * sample.u_east_mps for weight, sample in zip(weights, samples)) / total
    v = sum(weight * sample.v_north_mps for weight, sample in zip(weights, samples)) / total
    disagreement = math.sqrt(
        sum(weight * ((sample.u_east_mps - u) ** 2 + (sample.v_north_mps - v) ** 2) for weight, sample in zip(weights, samples)) / total
    )
    return u, v, disagreement


def _spatial_uncertainty(
    nearest: float | None,
    farthest: float | None,
    count: int,
    disagreement: float | None,
    minimum_stations: int = DEFAULT_MIN_STATIONS,
) -> str:
    if count < minimum_stations or disagreement is None:
        return "high"
    if disagreement >= 2.0 or (nearest is not None and nearest > 20.0) or (farthest is not None and farthest > 25.0):
        return "high"
    if disagreement >= 1.0 or (nearest is not None and nearest > 10.0) or (farthest is not None and farthest > 15.0) or count < 5:
        return "medium"
    return "low"


def _temporal_uncertainty(samples: Sequence[StationWindUse]) -> str:
    if not samples:
        return "STALE"
    if any(sample.temporal_mode == "nearest_only" for sample in samples):
        result = "NEAREST_ONLY"
    else:
        result = "EXACT_OR_BRACKETED"
    if max(sample.temporal_offset_minutes for sample in samples) > 10.0:
        return "STALE"
    return result


def _quality_confidence(
    samples: Sequence[StationWindUse],
    spatial_uncertainty: str,
    temporal_uncertainty: str,
    disagreement: float | None,
    maximum_stations: int,
) -> float:
    support = min(1.0, len(samples) / max(1, maximum_stations))
    distance_factor = 1.0
    if samples:
        distance_factor = max(0.0, 1.0 - max(sample.distance_km for sample in samples) / 30.0)
    disagreement_factor = 1.0 / (1.0 + max(0.0, disagreement or 0.0) / 2.0)
    spatial_factor = {"low": 1.0, "medium": 0.7, "high": 0.4}[spatial_uncertainty]
    temporal_factor = {"EXACT_OR_BRACKETED": 1.0, "NEAREST_ONLY": 0.75, "STALE": 0.45}[temporal_uncertainty]
    return round(max(0.0, min(1.0, support * distance_factor * disagreement_factor * spatial_factor * temporal_factor)), 3)


def _row_time(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return utc_datetime(value)
    try:
        return parse_query_time(str(value))
    except ValueError:
        return None


def _open_read_only(database_path: Path) -> duckdb.DuckDBPyConnection:
    last_error: Exception | None = None
    for attempt in range(READ_RETRIES + 1):
        try:
            connection = duckdb.connect(str(database_path), read_only=True)
            connection.execute("SET TimeZone='UTC'")
            return connection
        except Exception as exc:  # DuckDB lock errors vary by version/platform.
            last_error = exc
            if attempt >= READ_RETRIES:
                break
            time_module.sleep(READ_RETRY_DELAYS_SECONDS[attempt])
    raise WindFieldError(
        f"READ_ONLY_ACCESS_FAILED: could not open {database_path} while recorder may be writing; "
        f"recorder was not stopped or modified. Details: {last_error}"
    ) from last_error


def _read_snapshot(database_path: Path, query_time: datetime, config: WindConfig) -> _Snapshot:
    return _read_snapshot_range(database_path, query_time, query_time, config)


def _read_snapshot_range(
    database_path: Path,
    start_time: datetime,
    end_time: datetime,
    config: WindConfig,
    metrics: dict[str, float] | None = None,
) -> _Snapshot:
    start_time = utc_datetime(start_time)
    end_time = utc_datetime(end_time)
    if end_time < start_time:
        raise ValueError("snapshot end time must not precede start time")
    window = timedelta(minutes=config.maximum_temporal_distance_minutes)
    last_error: Exception | None = None
    for attempt in range(READ_RETRIES + 1):
        connection: duckdb.DuckDBPyConnection | None = None
        started = time_module.perf_counter()
        try:
            connection = _open_read_only(database_path)
            station_rows = connection.execute(
                "SELECT station_id, station_name, lat, lon FROM weather_station ORDER BY station_id"
            ).fetchall()
            stations = tuple(_Station(str(row[0]), str(row[1] or ""), row[2], row[3]) for row in station_rows)
            observation_rows = connection.execute(
                """
                SELECT station_id, observation_time_utc, wind_from_deg, wind_speed_mps,
                       wind_u_east_mps, wind_v_north_mps, wind_status, quality_flags
                FROM weather_observation
                WHERE observation_time_utc BETWEEN ? AND ?
                ORDER BY station_id, observation_time_utc
                """,
                [start_time - window, end_time + window],
            ).fetchall()
            observations: dict[str, list[RawWindObservation]] = {}
            for row in observation_rows:
                observations.setdefault(str(row[0]), []).append(
                    RawWindObservation(
                        str(row[0]), utc_datetime(row[1]), row[2], row[3], row[4], row[5], str(row[6] or "invalid"),
                        tuple(token for token in str(row[7] or "").split(";") if token),
                    )
                )
            span = connection.execute(
                "SELECT min(observation_time_utc), max(observation_time_utc) FROM weather_observation"
            ).fetchone()
            if metrics is not None:
                metrics["duckdb_read_seconds"] = metrics.get("duckdb_read_seconds", 0.0) + (time_module.perf_counter() - started)
                metrics["duckdb_query_count"] = metrics.get("duckdb_query_count", 0.0) + 3.0
            return _Snapshot(stations, {key: tuple(value) for key, value in observations.items()}, _row_time(span[0]), _row_time(span[1]))
        except Exception as exc:
            last_error = exc
            if attempt >= READ_RETRIES:
                break
            time_module.sleep(READ_RETRY_DELAYS_SECONDS[attempt])
        finally:
            if connection is not None:
                connection.close()
    raise WindFieldError(
        f"READ_ONLY_QUERY_FAILED: could not read {database_path} while recorder may be writing; "
        f"recorder was not stopped or modified. Details: {last_error}"
    ) from last_error


class WindFieldSnapshot:
    """One read-only weather snapshot used for one query time and grid."""

    def __init__(
        self,
        snapshot: _Snapshot,
        query_time: datetime,
        config: WindConfig,
        *,
        requested_start_utc: datetime | None = None,
        requested_end_utc: datetime | None = None,
        metrics: dict[str, float] | None = None,
        temporal_cache_enabled: bool = True,
    ) -> None:
        self._snapshot = snapshot
        self.query_time_utc = utc_datetime(query_time)
        self.config = config
        self.requested_start_utc = utc_datetime(requested_start_utc) if requested_start_utc else self.query_time_utc
        self.requested_end_utc = utc_datetime(requested_end_utc) if requested_end_utc else self.query_time_utc
        self.metrics = metrics
        self.temporal_cache_enabled = temporal_cache_enabled
        self._temporal_cache: dict[datetime, dict[str, TemporalWindValue | None]] = {}
        self._observation_exclusions: dict[str, tuple[int, int]] = {
            station_id: (
                sum(record.wind_status.casefold() == "variable" for record in records),
                sum(record.wind_status.casefold() in {"invalid", "inconsistent"} for record in records),
            )
            for station_id, records in snapshot.observations_by_station.items()
        }

    @property
    def database_start_utc(self) -> datetime | None:
        return self._snapshot.database_start_utc

    @property
    def database_end_utc(self) -> datetime | None:
        return self._snapshot.database_end_utc

    def _station_values(
        self,
        query_lat: float,
        query_lon: float,
        query_time: datetime,
        exclude_station_ids: set[str] | None = None,
    ) -> tuple[list[StationWindUse], dict[str, int]]:
        started = time_module.perf_counter()
        values: list[StationWindUse] = []
        excluded = {"variable": 0, "invalid": 0, "no_temporal_value": 0}
        excluded_ids = exclude_station_ids or set()
        temporal_values = self._temporal_cache.get(query_time) if self.temporal_cache_enabled else None
        if temporal_values is None:
            temporal_started = time_module.perf_counter()
            temporal_values = {
                station.station_id: temporal_select(
                    self._snapshot.observations_by_station.get(station.station_id, ()),
                    query_time,
                    self.config.maximum_temporal_distance_minutes,
                )
                for station in self._snapshot.stations
            }
            if self.temporal_cache_enabled:
                self._temporal_cache[query_time] = temporal_values
            if self.metrics is not None:
                self.metrics["temporal_interpolation_seconds"] = self.metrics.get("temporal_interpolation_seconds", 0.0) + (time_module.perf_counter() - temporal_started)
                if self.temporal_cache_enabled:
                    self.metrics["temporal_cache_misses"] = self.metrics.get("temporal_cache_misses", 0.0) + 1.0
        elif self.metrics is not None and self.temporal_cache_enabled:
            self.metrics["temporal_cache_hits"] = self.metrics.get("temporal_cache_hits", 0.0) + 1.0
        for station in self._snapshot.stations:
            if station.station_id in excluded_ids:
                continue
            if station.lat is None or station.lon is None:
                excluded["invalid"] += 1
                continue
            variable_count, invalid_count = self._observation_exclusions.get(station.station_id, (0, 0))
            excluded["variable"] += variable_count
            excluded["invalid"] += invalid_count
            selected = temporal_values.get(station.station_id)
            if selected is None:
                excluded["no_temporal_value"] += 1
                continue
            distance = haversine_km((query_lon, query_lat), (float(station.lon), float(station.lat)))
            speed, wind_to, wind_from = directions_from_vector(selected.u_east_mps, selected.v_north_mps)
            values.append(StationWindUse(
                station.station_id, station.station_name, float(station.lat), float(station.lon), distance,
                selected.source_observation_utc,
                selected.source_before_utc, selected.source_after_utc,
                selected.temporal_mode, selected.temporal_offset_minutes,
                selected.u_east_mps, selected.v_north_mps, speed, wind_to, wind_from,
                selected.wind_status, selected.quality_flags, 0.0,
            ))
        if self.metrics is not None:
            self.metrics["station_selection_seconds"] = self.metrics.get("station_selection_seconds", 0.0) + (time_module.perf_counter() - started)
        return values, excluded

    def estimate(
        self,
        lat: float,
        lon: float,
        *,
        query_time: datetime | str | None = None,
        exclude_station_ids: Iterable[str] | None = None,
    ) -> WindEstimate:
        if not math.isfinite(float(lat)) or not -90.0 <= float(lat) <= 90.0:
            raise ValueError("lat must be a finite WGS84 latitude")
        if not math.isfinite(float(lon)) or not -180.0 <= float(lon) <= 180.0:
            raise ValueError("lon must be a finite WGS84 longitude")
        query_lat, query_lon = float(lat), float(lon)
        actual_query_time = parse_query_time(query_time) if query_time is not None else self.query_time_utc
        estimate_started = time_module.perf_counter()
        excluded_ids = {str(station_id) for station_id in (exclude_station_ids or ())}
        values, excluded = self._station_values(query_lat, query_lon, actual_query_time, excluded_ids)
        values.sort(key=lambda item: (item.distance_km, item.station_id))
        preferred = [item for item in values if item.distance_km <= self.config.preferred_radius_km]
        within_max = [item for item in values if item.distance_km <= self.config.maximum_radius_km]
        selected = preferred if len(preferred) >= self.config.minimum_stations else within_max
        selected = selected[: self.config.maximum_stations]
        counts = {str(radius): sum(item.distance_km <= radius for item in values) for radius in (5, 10, 20, 30)}
        common_diagnostics = {
            "preferred_radius_km": self.config.preferred_radius_km,
            "maximum_radius_km": self.config.maximum_radius_km,
            "candidate_count_preferred_radius": len(preferred),
            "candidate_count_maximum_radius": len(within_max),
            "effective_station_counts_within_km": counts,
            "excluded_variable_or_invalid_observation_count": excluded["variable"] + excluded["invalid"],
            "stations_without_usable_temporal_value": excluded["no_temporal_value"],
        }
        if len(selected) < self.config.minimum_stations:
            result = WindEstimate(
                query_lat, query_lon, actual_query_time, None, None, None, None, None,
                len(selected), tuple(selected),
                selected[0].distance_km if selected else None,
                selected[-1].distance_km if selected else None,
                max((item.temporal_offset_minutes for item in selected), default=None),
                "high", "STALE", None, 0.0 if selected else None, "INSUFFICIENT_STATIONS", common_diagnostics,
            )
            if self.metrics is not None:
                self.metrics["wind_estimate_seconds"] = self.metrics.get("wind_estimate_seconds", 0.0) + (time_module.perf_counter() - estimate_started)
            return result
        idw_started = time_module.perf_counter()
        weights = _idw_weights(selected, self.config.idw_power, self.config.idw_epsilon_km)
        total = sum(weights)
        selected = tuple(
            StationWindUse(**{**asdict(item), "weight": round(weight / total, 9)})
            for item, weight in zip(selected, weights)
        )
        u, v, disagreement = interpolate_vectors(selected, self.config.idw_power, self.config.idw_epsilon_km)
        if self.metrics is not None:
            self.metrics["idw_computation_seconds"] = self.metrics.get("idw_computation_seconds", 0.0) + (time_module.perf_counter() - idw_started)
        speed, wind_to, wind_from = directions_from_vector(u, v)
        nearest = selected[0].distance_km
        farthest = selected[-1].distance_km
        spatial = _spatial_uncertainty(nearest, farthest, len(selected), disagreement, self.config.minimum_stations)
        temporal = _temporal_uncertainty(selected)
        calm_count = sum(item.wind_status.casefold() == "calm" for item in selected)
        if calm_count >= math.ceil(len(selected) / 2):
            quality = "CALM / LOW_DIRECTION_CONFIDENCE"
        elif spatial == "high" or temporal == "STALE":
            quality = "HIGH_UNCERTAINTY"
        elif spatial == "medium" or temporal == "NEAREST_ONLY":
            quality = "MEDIUM_UNCERTAINTY"
        else:
            quality = "GOOD"
        confidence = _quality_confidence(selected, spatial, temporal, disagreement, self.config.maximum_stations)
        common_diagnostics.update({
            "calm_station_count": calm_count,
            "directional_station_count": len(selected) - calm_count,
            "vector_disagreement_mps": round(disagreement, 6),
            "confidence_note": "confidence is a deterministic data/interpolation quality score, not a probability",
        })
        result = WindEstimate(
            query_lat, query_lon, actual_query_time,
            round(u, 6), round(v, 6), round(speed, 6),
            None if wind_to is None else round(wind_to, 6),
            None if wind_from is None else round(wind_from, 6),
            len(selected), selected, round(nearest, 6), round(farthest, 6),
            round(max(item.temporal_offset_minutes for item in selected), 6),
            spatial, temporal, round(disagreement, 6), confidence, quality, common_diagnostics,
        )
        if self.metrics is not None:
            self.metrics["wind_estimate_seconds"] = self.metrics.get("wind_estimate_seconds", 0.0) + (time_module.perf_counter() - estimate_started)
        return result

    def estimates_for_grid(self, points: Iterable[tuple[float, float]]) -> list[WindEstimate]:
        return [self.estimate(lat, lon) for lat, lon in points]


def load_wind_snapshot(
    database_path: Path = DEFAULT_DATABASE,
    query_time: datetime | str | None = None,
    config: WindConfig = WindConfig(),
    *,
    metrics: dict[str, float] | None = None,
    temporal_cache_enabled: bool = True,
) -> WindFieldSnapshot:
    if query_time is None:
        connection = _open_read_only(database_path)
        try:
            value = connection.execute("SELECT max(observation_time_utc) FROM weather_observation").fetchone()[0]
        except Exception as exc:
            raise WindFieldError(f"READ_ONLY_QUERY_FAILED: could not resolve latest weather time: {exc}") from exc
        finally:
            connection.close()
        if value is None:
            raise ValueError("no weather observations available")
        query = utc_datetime(value)
    else:
        query = parse_query_time(query_time)
    return WindFieldSnapshot(_read_snapshot_range(database_path, query, query, config, metrics), query, config, metrics=metrics, temporal_cache_enabled=temporal_cache_enabled)


def load_wind_snapshot_range(
    database_path: Path = DEFAULT_DATABASE,
    start_time: datetime | str | None = None,
    end_time: datetime | str | None = None,
    config: WindConfig = WindConfig(),
    *,
    metrics: dict[str, float] | None = None,
    temporal_cache_enabled: bool = True,
) -> WindFieldSnapshot:
    """Load one immutable read-only snapshot covering a trace time range."""

    if start_time is None or end_time is None:
        raise ValueError("snapshot range requires start_time and end_time")
    start = parse_query_time(start_time)
    end = parse_query_time(end_time)
    return WindFieldSnapshot(
        _read_snapshot_range(database_path, start, end, config, metrics),
        start,
        config,
        requested_start_utc=start,
        requested_end_utc=end,
        metrics=metrics,
        temporal_cache_enabled=temporal_cache_enabled,
    )


def get_wind(
    lat: float,
    lon: float,
    time: datetime | str | None = None,
    *,
    database_path: Path = DEFAULT_DATABASE,
    config: WindConfig = WindConfig(),
    exclude_station_ids: Iterable[str] | None = None,
    snapshot: WindFieldSnapshot | None = None,
) -> WindEstimate:
    """Estimate the wind vector at a WGS84 location and timezone-aware time."""

    if snapshot is not None:
        return snapshot.estimate(lat, lon, query_time=time, exclude_station_ids=exclude_station_ids)
    field = load_wind_snapshot(database_path, time, config)
    return field.estimate(lat, lon, exclude_station_ids=exclude_station_ids)


def estimate_to_dict(estimate: WindEstimate) -> dict[str, Any]:
    def convert(value: Any) -> Any:
        if isinstance(value, datetime):
            return iso_utc(value)
        if isinstance(value, tuple):
            return [convert(item) for item in value]
        if isinstance(value, list):
            return [convert(item) for item in value]
        if isinstance(value, dict):
            return {key: convert(item) for key, item in value.items()}
        return value
    return convert(asdict(estimate))


def grid_points(core_bbox: dict[str, float], spacing_km: float = 1.0) -> list[tuple[float, float]]:
    if spacing_km <= 0:
        raise ValueError("grid spacing must be positive")
    center_lat = (core_bbox["south"] + core_bbox["north"]) / 2.0
    lat_step = spacing_km / 111.195
    lon_step = spacing_km / (111.195 * max(0.01, math.cos(math.radians(center_lat))))
    points: list[tuple[float, float]] = []
    lat = core_bbox["south"]
    while lat <= core_bbox["north"] + 1e-10:
        lon = core_bbox["west"]
        while lon <= core_bbox["east"] + 1e-10:
            points.append((round(lat, 8), round(lon, 8)))
            lon += lon_step
        lat += lat_step
    return points


def build_summary(
    field: WindFieldSnapshot,
    core_estimate: WindEstimate,
    grid: Sequence[WindEstimate] = (),
    *,
    database_path: Path,
    analysis_scope: str = "Core center wind estimate and Core Zone grid",
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "analysis": {
            "analysis_time_utc": iso_utc(field.query_time_utc),
            "scope": analysis_scope,
            "database_time_span_utc": {
                "start": iso_utc(field.database_start_utc),
                "end": iso_utc(field.database_end_utc),
            },
        },
        "database": {"path": str(database_path), "read_only": True},
        "core_center_estimate": estimate_to_dict(core_estimate),
        "stations_selected": estimate_to_dict(core_estimate)["stations_used"],
        "interpolation_parameters": asdict(field.config),
        "quality_diagnostics": {
            "core_center": estimate_to_dict(core_estimate)["diagnostics"],
            "grid_point_count": len(grid),
            "grid_quality_categories": {category: sum(item.quality_category == category for item in grid) for category in sorted({item.quality_category for item in grid})},
        },
    }


__all__ = [
    "WindConfig", "RawWindObservation", "TemporalWindValue", "StationWindUse", "WindEstimate",
    "WindFieldError", "WindFieldSnapshot", "vector_from_wind_from", "directions_from_vector",
    "temporal_select", "interpolate_vectors", "load_wind_snapshot", "load_wind_snapshot_range", "get_wind", "estimate_to_dict",
    "grid_points", "build_summary", "haversine_km",
]
