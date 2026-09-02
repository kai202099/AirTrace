"""Backward Monte Carlo particle tracing for explainable source evidence.

This module is deliberately an evidence-surface engine, not a source
attribution system.  It reuses the production wind interpolation path and
applies empirical vector residuals from wind validation as the primary
stochastic uncertainty.  Scores are relative within one event and are not
probabilities.
"""

from __future__ import annotations

import csv
import html
import json
import math
import random
import time
from collections import defaultdict, deque
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from airtrace.analysis.wind import (
    DEFAULT_DATABASE,
    WindConfig,
    WindEstimate,
    get_wind,
    load_wind_snapshot,
    load_wind_snapshot_range,
    parse_query_time,
)

UTC = timezone.utc
ROOT = Path(__file__).resolve().parents[2]
DEFAULT_REGION_CONFIG = ROOT / "config" / "pilot_region.json"
DEFAULT_RESIDUALS = ROOT / "reports" / "wind_validation" / "wind_validation_samples.csv"


@dataclass(frozen=True)
class BacktraceConfig:
    """Deterministic v1 tracing settings."""

    database_path: Path = DEFAULT_DATABASE
    region_config_path: Path = DEFAULT_REGION_CONFIG
    residual_csv_path: Path = DEFAULT_RESIDUALS
    particles_per_receptor: int = 200
    random_seed: int = 42
    dt_seconds: int = 60
    maximum_backtrace_minutes: int = 60
    grid_cell_m: float = 250.0
    domain_buffer_km: float = 10.0
    residual_correlation_minutes: int = 10
    seed_mode: str = "first"
    top_regions: int = 5
    peak_threshold_fraction: float = 0.60
    display_trajectory_limit: int = 120
    subgrid_diffusion_mps: float = 0.0
    wind_cache_enabled: bool = True
    wind_cache_spatial_m: float = 250.0
    wind_cache_temporal_seconds: int = 60
    # Exact-coordinate caching is the safe default; spatial quantization is an
    # explicit performance/accuracy tradeoff for benchmark or high-throughput use.
    wind_cache_quantized: bool = False
    profile: bool = False
    wind_config: WindConfig = field(default_factory=WindConfig)
    # Test/manual injection point.  None means the production path.
    wind_getter: Callable[..., Any] | None = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self.particles_per_receptor < 1:
            raise ValueError("particles_per_receptor must be positive")
        if self.dt_seconds < 1 or self.maximum_backtrace_minutes <= 0:
            raise ValueError("dt_seconds and maximum_backtrace_minutes must be positive")
        if self.grid_cell_m <= 0 or self.domain_buffer_km < 0:
            raise ValueError("grid_cell_m and domain_buffer_km must be non-negative/positive")
        if self.residual_correlation_minutes < 1:
            raise ValueError("residual_correlation_minutes must be positive")
        if self.seed_mode != "first":
            raise ValueError("v1 supports seed_mode='first' only")
        if not 0 < self.peak_threshold_fraction <= 1:
            raise ValueError("peak_threshold_fraction must be in (0, 1]")
        if self.wind_cache_spatial_m <= 0 or self.wind_cache_temporal_seconds < 1:
            raise ValueError("wind cache resolution must be positive")


def _config(value: BacktraceConfig | Mapping[str, Any] | None) -> BacktraceConfig:
    if value is None:
        return BacktraceConfig()
    if isinstance(value, BacktraceConfig):
        return value
    allowed = {item.name for item in BacktraceConfig.__dataclass_fields__.values()}
    data = {key: val for key, val in value.items() if key in allowed}
    if "database_path" in data:
        data["database_path"] = Path(data["database_path"])
    if "region_config_path" in data:
        data["region_config_path"] = Path(data["region_config_path"])
    if "residual_csv_path" in data:
        data["residual_csv_path"] = Path(data["residual_csv_path"])
    if isinstance(data.get("wind_config"), Mapping):
        data["wind_config"] = WindConfig(**data["wind_config"])
    return BacktraceConfig(**data)


def _finite(value: Any) -> bool:
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


def _time(value: Any) -> datetime:
    if isinstance(value, datetime):
        result = value
        if result.tzinfo is None:
            result = result.replace(tzinfo=UTC)
        return result.astimezone(UTC)
    return parse_query_time(str(value))


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _number(value: Any, digits: int = 6) -> float | None:
    return round(float(value), digits) if _finite(value) else None


class LocalMetricProjection:
    """Small-region WGS84 local metric projection.

    The Pilot Region is only about 13 km north-south and 12 km east-west, so
    this local tangent/equirectangular approximation keeps integration in
    metres with sub-metre scale error at this extent.  It is intentionally
    self-contained; no degree is ever treated as a metre.
    """

    def __init__(self, origin_lat: float, origin_lon: float) -> None:
        self.origin_lat = float(origin_lat)
        self.origin_lon = float(origin_lon)
        self._x_scale = 111_320.0 * math.cos(math.radians(self.origin_lat))
        self._y_scale = 110_540.0

    def project(self, lat: float, lon: float) -> tuple[float, float]:
        return ((float(lon) - self.origin_lon) * self._x_scale, (float(lat) - self.origin_lat) * self._y_scale)

    def inverse(self, x: float, y: float) -> tuple[float, float]:
        return (self.origin_lat + float(y) / self._y_scale, self.origin_lon + float(x) / self._x_scale)


@dataclass(frozen=True)
class _Extent:
    west: float
    east: float
    south: float
    north: float
    min_x: float
    max_x: float
    min_y: float
    max_y: float

    def contains(self, x: float, y: float, projection: LocalMetricProjection) -> bool:
        del projection
        return self.min_x <= x <= self.max_x and self.min_y <= y <= self.max_y


@dataclass(frozen=True)
class _Residual:
    u: float
    v: float
    distance_bucket: str
    quality: str


def distance_bucket(distance_km: float | None) -> str:
    if distance_km is None or not _finite(distance_km):
        return "unavailable"
    value = float(distance_km)
    if value <= 3:
        return "<= 3 km"
    if value <= 5:
        return "3–5 km"
    if value <= 10:
        return "5–10 km"
    return "> 10 km"


def load_validation_residuals(path: Path = DEFAULT_RESIDUALS) -> list[_Residual]:
    """Load finite actual-minus-predicted vector residuals."""

    if not path.exists():
        return []
    result: list[_Residual] = []
    with path.open("r", encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            if not all(_finite(row.get(key)) for key in ("actual_u", "actual_v", "predicted_u", "predicted_v")):
                continue
            result.append(_Residual(
                float(row["actual_u"]) - float(row["predicted_u"]),
                float(row["actual_v"]) - float(row["predicted_v"]),
                distance_bucket(float(row["nearest_station_km"]) if _finite(row.get("nearest_station_km")) else None),
                str(row.get("predicted_quality") or "").strip().upper(),
            ))
    return result


class _ResidualSampler:
    def __init__(self, rows: Sequence[_Residual], rng: random.Random) -> None:
        self.rows = list(rows)
        self.rng = rng
        self.usage: dict[str, int] = defaultdict(int)
        self._by_distance: dict[str, list[_Residual]] = defaultdict(list)
        self._by_quality: dict[str, list[_Residual]] = defaultdict(list)
        self._exact: dict[tuple[str, str], list[_Residual]] = defaultdict(list)
        for row in self.rows:
            self._by_distance[row.distance_bucket].append(row)
            self._by_quality[row.quality].append(row)
            self._exact[(row.distance_bucket, row.quality)].append(row)

    def sample(self, estimate: Any) -> tuple[float, float, str]:
        if not self.rows:
            self.usage["none"] += 1
            return 0.0, 0.0, "none"
        distance = distance_bucket(_get_value(estimate, "nearest_station_km"))
        quality = str(_get_value(estimate, "quality_category") or "").upper()
        exact = self._exact.get((distance, quality), [])
        if exact:
            source, pool = "exact_bucket", exact
        else:
            pool = self._by_distance.get(distance, [])
            if pool:
                source = "distance_fallback"
            else:
                pool = self._by_quality.get(quality, [])
                if pool:
                    source = "quality_fallback"
                else:
                    source, pool = "global_fallback", self.rows
        selected = self.rng.choice(pool)
        self.usage[source] += 1
        return selected.u, selected.v, source


def _get_value(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


class _WindResolver:
    def __init__(
        self,
        config: BacktraceConfig,
        projection: LocalMetricProjection,
        snapshot_start: datetime | None = None,
        snapshot_end: datetime | None = None,
    ) -> None:
        self.config = config
        self.projection = projection
        self.custom = config.wind_getter is not None
        self.snapshot_cache: dict[datetime, Any] = {}
        self.wind_cache: dict[tuple[int | float, int | float, datetime], Any] = {}
        self.snapshot: Any | None = None
        self.estimate_count = 0
        self.wind_computation_count = 0
        self.cache_hits = 0
        self.cache_misses = 0
        self.unavailable_count = 0
        self.quality_counts: dict[str, int] = defaultdict(int)
        self.arrows: list[dict[str, Any]] = []
        self.timings: dict[str, float] = defaultdict(float)
        if not self.custom and config.wind_cache_enabled and snapshot_start is not None and snapshot_end is not None:
            self.snapshot = load_wind_snapshot_range(
                config.database_path, snapshot_start, snapshot_end, config.wind_config, metrics=self.timings,
            )

    def _cache_key(self, x: float, y: float, at: datetime) -> tuple[int | float, int | float, datetime]:
        spatial = self.config.wind_cache_spatial_m
        seconds = self.config.wind_cache_temporal_seconds
        epoch = math.floor(at.timestamp() / seconds) * seconds
        if not self.config.wind_cache_quantized:
            return round(x, 6), round(y, 6), datetime.fromtimestamp(epoch, tz=UTC)
        return math.floor(x / spatial), math.floor(y / spatial), datetime.fromtimestamp(epoch, tz=UTC)

    def _cached_query(self, key: tuple[int | float, int | float, datetime]) -> tuple[float, float, datetime]:
        if self.config.wind_cache_quantized:
            x = (key[0] + 0.5) * self.config.wind_cache_spatial_m
            y = (key[1] + 0.5) * self.config.wind_cache_spatial_m
        else:
            x, y = float(key[0]), float(key[1])
        lat, lon = self.projection.inverse(x, y)
        return lat, lon, key[2]

    def estimate(self, lat: float, lon: float, at: datetime) -> Any:
        self.estimate_count += 1
        x, y = self.projection.project(lat, lon)
        cache_key = self._cache_key(x, y, at) if self.config.wind_cache_enabled else None
        if cache_key is not None and cache_key in self.wind_cache:
            self.cache_hits += 1
            estimate = self.wind_cache[cache_key]
        else:
            if cache_key is not None:
                self.cache_misses += 1
                query_lat, query_lon, query_time = self._cached_query(cache_key)
            else:
                query_lat, query_lon = lat, lon
                query_time = at if self.custom else at.replace(second=0, microsecond=0)
            computation_started = time.perf_counter()
            if self.custom:
                estimate = self.config.wind_getter(query_lat, query_lon, query_time)  # type: ignore[misc]
            elif self.snapshot is not None:
                estimate = get_wind(query_lat, query_lon, query_time, database_path=self.config.database_path, config=self.config.wind_config, snapshot=self.snapshot)
            else:
                # Strict/reference mode keeps v1's per-minute immutable snapshot behavior.
                snapshot = self.snapshot_cache.get(query_time)
                if snapshot is None:
                    snapshot = load_wind_snapshot(
                        self.config.database_path, query_time, self.config.wind_config,
                        metrics=self.timings, temporal_cache_enabled=False,
                    )
                    self.snapshot_cache[query_time] = snapshot
                estimate = get_wind(query_lat, query_lon, query_time, database_path=self.config.database_path, config=self.config.wind_config, snapshot=snapshot)
            self.timings["wind_computation_seconds"] += time.perf_counter() - computation_started
            self.wind_computation_count += 1
            if cache_key is not None:
                self.wind_cache[cache_key] = estimate
        u = _get_value(estimate, "u_east_mps")
        v = _get_value(estimate, "v_north_mps")
        quality = str(_get_value(estimate, "quality_category") or "UNKNOWN")
        self.quality_counts[quality] += 1
        if not _finite(u) or not _finite(v):
            self.unavailable_count += 1
        return estimate

    def performance(self, runtime_seconds: float, receptor_count: int, particle_count: int, total_steps: int) -> dict[str, Any]:
        return {
            "runtime_seconds": round(runtime_seconds, 6),
            "receptor_count": receptor_count,
            "particle_count": particle_count,
            "integration_steps": total_steps * particle_count,
            "particle_steps_per_second": round((total_steps * particle_count) / runtime_seconds, 3) if runtime_seconds > 0 else None,
            "wind_estimate_requests": self.estimate_count,
            "wind_computations": self.wind_computation_count,
            "wind_computations_avoided": self.estimate_count - self.wind_computation_count,
            "wind_cache": {
                "enabled": self.config.wind_cache_enabled,
                "spatial_resolution_m": self.config.wind_cache_spatial_m,
                "quantized": self.config.wind_cache_quantized,
                "temporal_resolution_seconds": self.config.wind_cache_temporal_seconds,
                "hits": self.cache_hits,
                "misses": self.cache_misses,
                "hit_rate": round(self.cache_hits / max(1, self.cache_hits + self.cache_misses), 9),
                "entries": len(self.wind_cache),
            },
            "temporal_cache": {
                "enabled": self.config.wind_cache_enabled and not self.custom,
                "hits": int(self.timings.get("temporal_cache_hits", 0.0)),
                "misses": int(self.timings.get("temporal_cache_misses", 0.0)),
                "entries": int(self.timings.get("temporal_cache_misses", 0.0)),
            },
            "weather_snapshot": {
                "mode": "trace_range" if self.snapshot is not None else "per_minute",
                "count": 1 if self.snapshot is not None else len(self.snapshot_cache),
                "observations": sum(len(rows) for rows in getattr(getattr(self.snapshot, "_snapshot", None), "observations_by_station", {}).values()) if self.snapshot is not None else None,
                "requested_start_utc": _iso(getattr(self.snapshot, "requested_start_utc", None)) if self.snapshot is not None else None,
                "requested_end_utc": _iso(getattr(self.snapshot, "requested_end_utc", None)) if self.snapshot is not None else None,
            },
            "timings_seconds": {key: round(value, 6) for key, value in sorted(self.timings.items())},
            "db_query_count": int(self.timings.get("duckdb_query_count", 0.0)),
        }


def _load_region(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def _extent(region: Mapping[str, Any], projection: LocalMetricProjection, buffer_km: float) -> _Extent:
    bbox = region["context_bbox"]
    corners = [projection.project(bbox["south"], bbox["west"]), projection.project(bbox["south"], bbox["east"]), projection.project(bbox["north"], bbox["west"]), projection.project(bbox["north"], bbox["east"])]
    min_x, max_x = min(item[0] for item in corners) - buffer_km * 1000, max(item[0] for item in corners) + buffer_km * 1000
    min_y, max_y = min(item[1] for item in corners) - buffer_km * 1000, max(item[1] for item in corners) + buffer_km * 1000
    south, west = projection.inverse(min_x, min_y)
    north, east = projection.inverse(max_x, max_y)
    return _Extent(west, east, south, north, min_x, max_x, min_y, max_y)


def _event_centroid(event: Mapping[str, Any], seeds: Sequence[Mapping[str, Any]]) -> tuple[float, float]:
    value = event.get("latest_centroid") or event.get("centroid")
    if isinstance(value, Mapping) and _finite(value.get("lat")) and _finite(value.get("lon")):
        return float(value["lat"]), float(value["lon"])
    if seeds:
        return (sum(float(item["lat"]) for item in seeds) / len(seeds), sum(float(item["lon"]) for item in seeds) / len(seeds))
    return 0.0, 0.0


def select_receptor_seeds(event: Mapping[str, Any], memberships: Iterable[Mapping[str, Any]], seed_mode: str = "first") -> list[dict[str, Any]]:
    """Select one first qualifying observation per unique station."""

    if seed_mode != "first":
        raise ValueError("v1 supports seed_mode='first' only")
    event_id = event.get("event_id")
    rows = [dict(row) for row in memberships if event_id is None or row.get("event_id") in (None, event_id)]
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        station_id = str(row.get("station_id") or "")
        if station_id and _finite(row.get("lat")) and _finite(row.get("lon")):
            groups[station_id].append(row)
    selected: list[dict[str, Any]] = []
    for station_id, items in sorted(groups.items()):
        items.sort(key=lambda row: (_time(row.get("time_bin") or row.get("observation_time_utc")), str(row.get("role") or "")))
        strong = [row for row in items if str(row.get("role") or "").casefold() in {"seed", "support"}]
        if not strong:
            continue
        seed = next((row for row in strong if str(row.get("role") or "").casefold() == "seed"), strong[0])
        selected.append({
            "station_id": station_id,
            "seed_time_utc": _iso(_time(seed.get("time_bin") or seed.get("observation_time_utc"))),
            "lat": float(seed["lat"]), "lon": float(seed["lon"]),
            "pm25": _number(seed.get("pm25", seed.get("raw_pm25"))),
            "anomaly_score": _number(seed.get("anomaly_score")),
            "temporal_excess": _number(seed.get("temporal_excess")),
            "spatial_excess": _number(seed.get("spatial_excess")),
            "role": str(seed.get("role") or "support"),
        })
    return selected


def _seed_weight(seed: Mapping[str, Any]) -> float:
    role_factor = 1.0 if str(seed.get("role") or "").casefold() == "seed" else 0.75
    # Bounded score influence avoids absolute PM2.5 dominating evidence.
    score_factor = 0.75 + 0.25 * min(max(float(seed.get("anomaly_score") or 0.0), 0.0) / 10.0, 1.0)
    return role_factor * score_factor


def _cell_key(x: float, y: float, cell_m: float) -> tuple[int, int]:
    return math.floor(x / cell_m), math.floor(y / cell_m)


def _cell_center(key: tuple[int, int], cell_m: float, projection: LocalMetricProjection) -> tuple[float, float]:
    x = (key[0] + 0.5) * cell_m
    y = (key[1] + 0.5) * cell_m
    lat, lon = projection.inverse(x, y)
    return lat, lon


def _bearing(first: tuple[float, float], second: tuple[float, float]) -> float:
    lat1, lon1, lat2, lon2 = map(math.radians, (first[0], first[1], second[0], second[1]))
    return math.degrees(math.atan2(math.sin(lon2 - lon1) * math.cos(lat2), math.cos(lat1) * math.sin(lat2) - math.sin(lat1) * math.cos(lat2) * math.cos(lon2 - lon1))) % 360


def _angular_difference(first: float | None, second: float | None) -> float | None:
    if first is None or second is None:
        return None
    return min(abs(first - second) % 360, abs(second - first) % 360)


def _movement_diagnostic(event: Mapping[str, Any], resolver: _WindResolver) -> dict[str, Any]:
    path = event.get("centroid_path") or []
    points = []
    for item in path:
        centroid = item.get("centroid") if isinstance(item, Mapping) else None
        if isinstance(centroid, Mapping) and _finite(centroid.get("lat")) and _finite(centroid.get("lon")):
            points.append((centroid, _time(item.get("time_bin") or item.get("time_utc") or event.get("last_seen_time_utc"))))
    if len(points) < 2:
        return {"status": "INSUFFICIENT_CENTROID_PATH", "observed_movement_bearing_deg": None, "wind_to_bearing_deg": None, "angular_difference_deg": None}
    first, last = points[0][0], points[-1][0]
    displacement = _haversine_km((float(first["lon"]), float(first["lat"])), (float(last["lon"]), float(last["lat"])))
    if displacement < 0.1:
        return {"status": "MOVEMENT_TOO_SMALL", "displacement_km": round(displacement, 6), "observed_movement_bearing_deg": None, "wind_to_bearing_deg": None, "angular_difference_deg": None}
    observed = _bearing((float(first["lat"]), float(first["lon"])), (float(last["lat"]), float(last["lon"])))
    vectors: list[tuple[float, float]] = []
    for centroid, at in points:
        try:
            estimate = resolver.estimate(float(centroid["lat"]), float(centroid["lon"]), at)
            u, v = _get_value(estimate, "u_east_mps"), _get_value(estimate, "v_north_mps")
            if _finite(u) and _finite(v) and math.hypot(float(u), float(v)) > 1e-9:
                vectors.append((float(u), float(v)))
        except Exception:
            continue
    if not vectors:
        wind_to = None
    else:
        u = sum(item[0] for item in vectors) / len(vectors)
        v = sum(item[1] for item in vectors) / len(vectors)
        wind_to = math.degrees(math.atan2(u, v)) % 360
    return {"status": "OK" if wind_to is not None else "WIND_UNAVAILABLE", "displacement_km": round(displacement, 6), "observed_movement_bearing_deg": round(observed, 6), "wind_to_bearing_deg": None if wind_to is None else round(wind_to, 6), "angular_difference_deg": _number(_angular_difference(observed, wind_to))}


def _haversine_km(first: tuple[float, float], second: tuple[float, float]) -> float:
    radius = 6371.0088
    lon1, lat1, lon2, lat2 = map(math.radians, (first[0], first[1], second[0], second[1]))
    dlon, dlat = lon2 - lon1, lat2 - lat1
    a = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 2 * radius * math.asin(min(1.0, math.sqrt(a)))


def _candidate_regions(cells: Sequence[dict[str, Any]], config: BacktraceConfig, event_centroid: tuple[float, float]) -> list[dict[str, Any]]:
    if not cells:
        return []
    peak = max(float(item["source_evidence_score"]) for item in cells)
    if peak <= 0:
        return []
    threshold = peak * config.peak_threshold_fraction
    eligible = {tuple(item["cell_key"]): item for item in cells if float(item["source_evidence_score"]) >= threshold}
    components: list[list[dict[str, Any]]] = []
    while eligible:
        key, item = next(iter(eligible.items()))
        del eligible[key]
        queue = deque([key])
        component = [item]
        while queue:
            current = queue.popleft()
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    neighbor = (current[0] + dx, current[1] + dy)
                    if neighbor in eligible:
                        component.append(eligible.pop(neighbor))
                        queue.append(neighbor)
        components.append(component)
    components.sort(key=lambda group: (-max(float(item["source_evidence_score"]) for item in group), -sum(float(item["source_evidence_score"]) for item in group), min(tuple(item["cell_key"]) for item in group)))
    regions = []
    for rank, group in enumerate(components[:config.top_regions], 1):
        score_total = sum(float(item["source_evidence_score"]) for item in group)
        lat = sum(float(item["center_lat"]) * float(item["source_evidence_score"]) for item in group) / score_total
        lon = sum(float(item["center_lon"]) * float(item["source_evidence_score"]) for item in group) / score_total
        ranges = [value for item in group for value in item.get("contributing_times_utc", [])]
        regions.append({
            "rank": rank, "candidate_source_region": True,
            "centroid": {"lat": round(lat, 6), "lon": round(lon, 6)},
            "area_km2": round(len(group) * config.grid_cell_m ** 2 / 1_000_000, 6),
            "peak_score": round(max(float(item["source_evidence_score"]) for item in group), 6),
            "mean_score": round(sum(float(item["source_evidence_score"]) for item in group) / len(group), 6),
            "receptor_support_count": max(int(item["receptor_support_count"]) for item in group),
            "receptor_support_fraction": round(max(float(item["receptor_support_fraction"]) for item in group), 6),
            "distance_to_event_centroid_km": round(_haversine_km((lon, lat), (event_centroid[1], event_centroid[0])), 6),
            "contributing_time_range_utc": {"start": min(ranges) if ranges else None, "end": max(ranges) if ranges else None},
            "bounds": {"south": round(min(float(item["center_lat"]) for item in group) - config.grid_cell_m / 110540.0 / 2, 6), "north": round(max(float(item["center_lat"]) for item in group) + config.grid_cell_m / 110540.0 / 2, 6), "west": round(min(float(item["center_lon"]) for item in group) - config.grid_cell_m / 111320.0 / 2, 6), "east": round(max(float(item["center_lon"]) for item in group) + config.grid_cell_m / 111320.0 / 2, 6)},
            "cell_count": len(group),
        })
    return regions


def _json_config(config: BacktraceConfig) -> dict[str, Any]:
    result = asdict(config)
    result["database_path"] = str(config.database_path)
    result["region_config_path"] = str(config.region_config_path)
    result["residual_csv_path"] = str(config.residual_csv_path)
    result["wind_config"] = asdict(config.wind_config)
    result.pop("wind_getter", None)
    return result


def trace_event(event: Mapping[str, Any], memberships: Iterable[Mapping[str, Any]], config: BacktraceConfig | Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Trace one detected event and return particles, evidence cells, and diagnostics."""

    trace_started = time.perf_counter()
    cfg = _config(config)
    seeds = select_receptor_seeds(event, memberships, cfg.seed_mode)
    if not seeds:
        return {
            "status": "NO_TRACEABLE_RECEPTORS", "event": dict(event), "trace_config": _json_config(cfg), "receptor_seeds": [],
            "particle_stats": {"receptor_count": 0, "particle_count": 0, "completed_particles": 0, "termination_counts": {}},
            "source_evidence_grid": [], "candidate_source_regions": [], "wind_diagnostics": {},
            "evidence_semantics": "0–1 evidence score is relative within this event, not calibrated probability.",
        }
    region = _load_region(cfg.region_config_path)
    center_lat = (float(region["context_bbox"]["south"]) + float(region["context_bbox"]["north"])) / 2
    center_lon = (float(region["context_bbox"]["west"]) + float(region["context_bbox"]["east"])) / 2
    projection = LocalMetricProjection(center_lat, center_lon)
    extent = _extent(region, projection, cfg.domain_buffer_km)
    total_steps = math.ceil(cfg.maximum_backtrace_minutes * 60 / cfg.dt_seconds)
    seed_times = [_time(seed["seed_time_utc"]) for seed in seeds]
    snapshot_start = min(seed_times) - timedelta(seconds=total_steps * cfg.dt_seconds)
    snapshot_end = max(seed_times)
    for item in event.get("centroid_path") or []:
        if isinstance(item, Mapping):
            value = item.get("time_bin") or item.get("time_utc")
            if value:
                try:
                    at = _time(value)
                    snapshot_start = min(snapshot_start, at)
                    snapshot_end = max(snapshot_end, at)
                except (TypeError, ValueError):
                    pass
    resolver = _WindResolver(cfg, projection, snapshot_start, snapshot_end)
    sampler = _ResidualSampler(load_validation_residuals(cfg.residual_csv_path), random.Random(cfg.random_seed))
    weights = [_seed_weight(seed) for seed in seeds]
    weight_total = sum(weights)
    weights = [value / weight_total for value in weights]
    rng = random.Random(cfg.random_seed)
    receptor_surfaces: list[dict[tuple[int, int], float]] = [defaultdict(float) for _ in seeds]
    cell_times: dict[tuple[int, int], list[str]] = defaultdict(list)
    all_trajectories: list[dict[str, Any]] = []
    display_trajectories: list[dict[str, Any]] = []
    termination_counts: dict[str, int] = defaultdict(int)
    wind_unavailable_details: dict[str, int] = defaultdict(int)
    block_seconds = cfg.residual_correlation_minutes * 60
    for receptor_index, (seed, receptor_weight) in enumerate(zip(seeds, weights)):
        x, y = projection.project(float(seed["lat"]), float(seed["lon"]))
        seed_at = _time(seed["seed_time_utc"])
        mass = receptor_weight / cfg.particles_per_receptor
        for particle_index in range(cfg.particles_per_receptor):
            px, py, current_time = x, y, seed_at
            path = [{"lat": round(float(seed["lat"]), 6), "lon": round(float(seed["lon"]), 6), "time_utc": _iso(current_time)}]
            residual: tuple[float, float, str] | None = None
            residual_sources: list[str] = []
            last_block = -1
            termination = "time_limit"
            for step in range(total_steps):
                block = (step * cfg.dt_seconds) // block_seconds
                projection_started = time.perf_counter()
                lat, lon = projection.inverse(px, py)
                if not extent.contains(px, py, projection):
                    resolver.timings["coordinate_projection_seconds"] += time.perf_counter() - projection_started
                    termination = "domain_limit"
                    break
                resolver.timings["coordinate_projection_seconds"] += time.perf_counter() - projection_started
                try:
                    estimate = resolver.estimate(lat, lon, current_time)
                except Exception as exc:
                    termination = "wind_unavailable"
                    wind_unavailable_details[type(exc).__name__] += 1
                    break
                u, v = _get_value(estimate, "u_east_mps"), _get_value(estimate, "v_north_mps")
                if not _finite(u) or not _finite(v):
                    termination = "wind_unavailable"
                    wind_unavailable_details[str(_get_value(estimate, "quality_category") or "UNKNOWN")] += 1
                    break
                if block != last_block:
                    residual_started = time.perf_counter()
                    residual = sampler.sample(estimate)
                    resolver.timings["residual_sampling_seconds"] += time.perf_counter() - residual_started
                    residual_sources.append(residual[2])
                    last_block = block
                du, dv = residual[0], residual[1]  # type: ignore[index]
                if cfg.subgrid_diffusion_mps > 0:
                    scale = cfg.subgrid_diffusion_mps * math.sqrt(cfg.dt_seconds)
                    du += rng.gauss(0.0, scale) / max(1.0, math.sqrt(cfg.dt_seconds))
                    dv += rng.gauss(0.0, scale) / max(1.0, math.sqrt(cfg.dt_seconds))
                # u/v are metres per second toward east/north.  Backward is -vector*dt.
                px -= (float(u) + du) * cfg.dt_seconds
                py -= (float(v) + dv) * cfg.dt_seconds
                current_time -= timedelta(seconds=cfg.dt_seconds)
                if not extent.contains(px, py, projection):
                    termination = "domain_limit"
                    break
                projection_started = time.perf_counter()
                next_lat, next_lon = projection.inverse(px, py)
                resolver.timings["coordinate_projection_seconds"] += time.perf_counter() - projection_started
                evidence_started = time.perf_counter()
                cell = _cell_key(px, py, cfg.grid_cell_m)
                receptor_surfaces[receptor_index][cell] += 1.0 / total_steps
                cell_times[cell].append(_iso(current_time) or "")
                path.append({"lat": round(next_lat, 6), "lon": round(next_lon, 6), "time_utc": _iso(current_time)})
                resolver.timings["evidence_deposition_seconds"] += time.perf_counter() - evidence_started
                if receptor_index == 0 and particle_index == 0 and step % 5 == 0:
                    resolver.arrows.append({"lat": round(lat, 6), "lon": round(lon, 6), "u_east_mps": round(float(u), 4), "v_north_mps": round(float(v), 4), "time_utc": _iso(current_time)})
            termination_counts[termination] += 1
            trajectory = {"receptor_index": receptor_index, "station_id": seed["station_id"], "particle_index": particle_index, "termination_reason": termination, "residual_sources_by_correlation_block": residual_sources, "points": path}
            all_trajectories.append(trajectory)
            if len(display_trajectories) < cfg.display_trajectory_limit:
                display_trajectories.append(trajectory)
    aggregate: dict[tuple[int, int], float] = defaultdict(float)
    for receptor_index, surface in enumerate(receptor_surfaces):
        for cell, density in surface.items():
            aggregate[cell] += density
    cells: list[dict[str, Any]] = []
    max_density = max(aggregate.values(), default=0.0)
    raw_cells: list[dict[str, Any]] = []
    for cell, density in sorted(aggregate.items()):
        support_count = sum(surface.get(cell, 0.0) > 0 for surface in receptor_surfaces)
        support_fraction = support_count / len(seeds)
        lat, lon = _cell_center(cell, cfg.grid_cell_m, projection)
        # Support factor makes multi-receptor agreement explicit and
        # deterministic; this is never interpreted as probability.
        normalized = density / max_density if max_density else 0.0
        raw_score = normalized * (0.25 + 0.75 * support_fraction)
        raw_cells.append({
            "cell_key": list(cell), "center_lat": round(lat, 6), "center_lon": round(lon, 6),
            "total_density": round(density, 9), "normalized_density": round(normalized, 9),
            "receptor_support_count": support_count, "receptor_support_fraction": round(support_fraction, 9),
            "_raw_score": raw_score,
            "contributing_times_utc": sorted(set(cell_times.get(cell, []))),
        })
    score_scale = max((float(item["_raw_score"]) for item in raw_cells), default=0.0)
    cells = []
    for item in raw_cells:
        item["source_evidence_score"] = round(float(item.pop("_raw_score")) / score_scale, 9) if score_scale else 0.0
        cells.append(item)
    centroid = _event_centroid(event, seeds)
    regions = _candidate_regions(cells, cfg, centroid)
    movement = _movement_diagnostic(event, resolver)
    performance = resolver.performance(time.perf_counter() - trace_started, len(seeds), len(all_trajectories), total_steps)
    payload = {
        "schema_version": 1,
        "status": "TRACE_COMPLETE" if resolver.estimate_count else "NO_TRACEABLE_WIND",
        "event": dict(event),
        "trace_config": _json_config(cfg),
        "coordinate_system": {"input_output": "WGS84 EPSG:4326", "integration": "local metric metres", "projection": "local WGS84 tangent/equirectangular; origin at context center"},
        "analysis_extent": {"west": extent.west, "east": extent.east, "south": extent.south, "north": extent.north, "buffer_km": cfg.domain_buffer_km},
        "receptor_seeds": [{**seed, "normalized_weight": round(weight, 9)} for seed, weight in zip(seeds, weights)],
        "wind_diagnostics": {"provider": "airtrace.analysis.wind.get_wind production path; cached read-only WindFieldSnapshot per UTC minute", "estimate_calls": resolver.estimate_count, "unavailable_estimates": resolver.unavailable_count, "quality_counts": dict(sorted(resolver.quality_counts.items())), "unavailable_details": dict(sorted(wind_unavailable_details.items())), "movement_consistency": movement, "map_arrows": resolver.arrows},
        "empirical_residual_policy": {"path": str(cfg.residual_csv_path), "sample_count": len(sampler.rows), "formula": "residual_u=actual_u-predicted_u; residual_v=actual_v-predicted_v", "primary_uncertainty": "sampled empirical vector residual", "distance_buckets": ["<= 3 km", "3–5 km", "5–10 km", "> 10 km"], "fallback_order": ["exact_bucket", "distance_fallback", "quality_fallback", "global_fallback"], "sampling_usage": dict(sorted(sampler.usage.items())), "temporal_correlation_minutes": cfg.residual_correlation_minutes, "subgrid_diffusion_default": "disabled"},
        "particle_stats": {"receptor_count": len(seeds), "particles_per_receptor": cfg.particles_per_receptor, "particle_count": len(all_trajectories), "completed_particles": termination_counts.get("time_limit", 0), "integration_steps_per_particle": total_steps, "integration_steps_attempted": resolver.estimate_count, "termination_counts": dict(sorted(termination_counts.items())), "display_trajectory_count": len(display_trajectories)},
        "particle_trajectories": all_trajectories,
        "display_trajectories": display_trajectories,
        "source_evidence_grid": cells,
        "candidate_source_regions": regions,
        "evidence_semantics": "0–1 evidence score is relative within this event, not calibrated probability. A candidate_source_region is not a polluter determination and does not establish legal attribution.",
    }
    if cfg.profile:
        payload["_performance_profile"] = performance
    return payload


def no_traceable_event_payload(reason: str = "NO TRACEABLE EVENT") -> dict[str, Any]:
    return {"schema_version": 1, "status": reason, "source_evidence_grid": [], "candidate_source_regions": [], "particle_trajectories": [], "display_trajectories": [], "evidence_semantics": "No detected event was available; no synthetic event was created."}


def write_trace_json(payload: Mapping[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def write_evidence_csv(payload: Mapping[str, Any], path: Path) -> None:
    fields = ["center_lat", "center_lon", "total_density", "normalized_density", "receptor_support_count", "receptor_support_fraction", "source_evidence_score"]
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows({field: row.get(field) for field in fields} for row in payload.get("source_evidence_grid", []))


def write_trace_map(payload: Mapping[str, Any], path: Path) -> None:
    event = payload.get("event") or {}
    region_path = Path(payload.get("trace_config", {}).get("region_config_path", DEFAULT_REGION_CONFIG))
    try:
        region = _load_region(region_path)
    except OSError:
        region = {"context_bbox": {"south": 25.0, "north": 25.12, "west": 121.4, "east": 121.52}, "core_bbox": {"south": 25.05, "north": 25.086, "west": 121.428, "east": 121.474}}
    grid = json.dumps(list(payload.get("source_evidence_grid", [])), ensure_ascii=False, separators=(",", ":"))
    trajectories = json.dumps(list(payload.get("display_trajectories", [])), ensure_ascii=False, separators=(",", ":"))
    arrows = json.dumps(payload.get("wind_diagnostics", {}).get("map_arrows", []), ensure_ascii=False, separators=(",", ":"))
    seeds = json.dumps(list(payload.get("receptor_seeds", [])), ensure_ascii=False, separators=(",", ":"))
    regions = json.dumps(list(payload.get("candidate_source_regions", [])), ensure_ascii=False, separators=(",", ":"))
    context = json.dumps(region["context_bbox"], separators=(",", ":"))
    core = json.dumps(region["core_bbox"], separators=(",", ":"))
    title = "AirTrace Backward Particle Tracing v1"
    notice = html.escape(str(payload.get("status") or "TRACE_COMPLETE"))
    page = f'''<!doctype html><html lang="zh-Hant"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{title}</title><link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"><style>body{{margin:0;font-family:system-ui,sans-serif;color:#172033}}header{{padding:12px 18px;border-bottom:1px solid #d9e0ea;background:#fff}}h1{{margin:0 0 4px;font-size:20px}}.note{{color:#586579;font-size:13px}}#map{{height:calc(100vh - 92px);min-height:560px}}</style></head><body><header><h1>{title}</h1><div class="note">{notice} · heatmap = relative source evidence, not pollution probability · candidate source regions are not legal attribution</div></header><div id="map"></div><script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script><script>const ctx={context},core={core},grid={grid},trajs={trajectories},arrows={arrows},seeds={seeds},regions={regions};const map=L.map('map',{{preferCanvas:true}}).setView([25.06,121.47],11);L.tileLayer('https://{{s}}.tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png',{{maxZoom:19,attribution:'&copy; OpenStreetMap contributors'}}).addTo(map);L.rectangle([[ctx.south,ctx.west],[ctx.north,ctx.east]],{{color:'#26364f',weight:2,fill:false,dashArray:'7 5'}}).bindPopup('Context Zone').addTo(map);L.rectangle([[core.south,core.west],[core.north,core.east]],{{color:'#8e2a86',weight:3,fill:false,dashArray:'8 4'}}).bindPopup('Core Zone').addTo(map);const max=Math.max(1,...grid.map(x=>x.source_evidence_score));grid.forEach(x=>{{const color='hsl('+(230-210*x.source_evidence_score/max)+',85%,50%)';L.circle([x.center_lat,x.center_lon],{{radius:125,color,fillColor:color,fillOpacity:.25+.55*x.source_evidence_score/max,weight:0}}).bindPopup('Relative source evidence: '+x.source_evidence_score.toFixed(3)+'<br>Receptor support: '+x.receptor_support_count+'/'+seeds.length).addTo(map)}});trajs.forEach(t=>{{L.polyline(t.points.map(p=>[p.lat,p.lon]),{{color:'#64748b',weight:1,opacity:.22}}).addTo(map)}});seeds.forEach(s=>{{L.circleMarker([s.lat,s.lon],{{radius:6,color:'#d97706',fillColor:'#fbbf24',fillOpacity:.9}}).bindPopup('Receptor '+s.station_id+'<br>'+s.role).addTo(map)}});arrows.forEach(a=>{{const scale=.00045;L.polyline([[a.lat,a.lon],[a.lat+a.v_north_mps*scale,a.lon+a.u_east_mps*scale]],{{color:'#0f766e',weight:2,opacity:.7}}).addTo(map)}});regions.forEach(r=>{{L.circleMarker([r.centroid.lat,r.centroid.lon],{{radius:10,color:'#b91c1c',fill:false,weight:3}}).bindPopup('Candidate source region #'+r.rank+'<br>Peak score: '+r.peak_score.toFixed(3)+'<br>Area: '+r.area_km2+' km²').addTo(map)}});map.fitBounds([[ctx.south,ctx.west],[ctx.north,ctx.east]],{{padding:[12,12]}});</script></body></html>'''
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(page, encoding="utf-8")


__all__ = ["BacktraceConfig", "LocalMetricProjection", "load_validation_residuals", "no_traceable_event_payload", "select_receptor_seeds", "trace_event", "write_evidence_csv", "write_trace_json", "write_trace_map"]
