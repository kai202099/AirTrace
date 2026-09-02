"""NASA FIRMS Area API access, VIIRS parsing, and deterministic hotspot groups."""

from __future__ import annotations

import csv
import io
import math
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterable, Mapping

import requests

from airtrace.data.moenv import haversine_km

FIRMS_BASE_URL = "https://firms.modaps.eosdis.nasa.gov/api/area/csv"
DEFAULT_SOURCES = ("VIIRS_NOAA20_NRT", "VIIRS_NOAA21_NRT")


@dataclass(frozen=True)
class FireDetection:
    latitude: float
    longitude: float
    acq_date: str
    acq_time: str
    acquisition_time_utc: datetime
    satellite: str
    instrument: str
    confidence: str
    frp: float | None
    bright_ti4: float | None
    bright_ti5: float | None
    scan: float | None
    track: float | None
    daynight: str
    source: str = ""
    fire_group_id: str | None = None


def _value(row: Mapping[str, Any], name: str, default: Any = None) -> Any:
    wanted = name.casefold()
    for key, value in row.items():
        if str(key).strip().casefold() == wanted:
            return value
    return default


def _float(value: Any) -> float | None:
    try:
        result = float(str(value).strip())
        return result if math.isfinite(result) else None
    except (TypeError, ValueError):
        return None


def parse_acquisition_time(acq_date: Any, acq_time: Any) -> datetime | None:
    try:
        day = datetime.strptime(str(acq_date).strip(), "%Y-%m-%d").date()
        digits = str(acq_time).strip().split(".", 1)[0].zfill(4)
        hour, minute = int(digits[:2]), int(digits[2:4])
        if hour > 23 or minute > 59:
            return None
        return datetime(day.year, day.month, day.day, hour, minute, tzinfo=timezone.utc)
    except (TypeError, ValueError, IndexError):
        return None


def parse_detection(raw: Mapping[str, Any], *, source: str = "") -> FireDetection | None:
    lat, lon = _float(_value(raw, "latitude")), _float(_value(raw, "longitude"))
    acq_date, acq_time = _value(raw, "acq_date"), _value(raw, "acq_time")
    acquired = parse_acquisition_time(acq_date, acq_time)
    if lat is None or lon is None or acquired is None:
        return None
    return FireDetection(
        latitude=lat, longitude=lon, acq_date=str(acq_date).strip(), acq_time=str(acq_time).strip(),
        acquisition_time_utc=acquired, satellite=str(_value(raw, "satellite", "")).strip(),
        instrument=str(_value(raw, "instrument", "")).strip(), confidence=str(_value(raw, "confidence", "")).strip(),
        frp=_float(_value(raw, "frp")), bright_ti4=_float(_value(raw, "bright_ti4")),
        bright_ti5=_float(_value(raw, "bright_ti5")), scan=_float(_value(raw, "scan")),
        track=_float(_value(raw, "track")), daynight=str(_value(raw, "daynight", "")).strip(), source=source,
    )


class FirmsError(RuntimeError):
    pass


class FirmsClient:
    """Small bounded client for the FIRMS CSV Area API."""

    def __init__(self, map_key: str, *, timeout_seconds: float = 30.0) -> None:
        if not str(map_key).strip():
            raise FirmsError("FIRMS_MAP_KEY is not set")
        self.map_key = str(map_key).strip()
        self.timeout_seconds = timeout_seconds
        self.session = requests.Session()
        self.session.headers.update({"Accept": "text/csv", "User-Agent": "AirTrace-FIRMS/1.0"})

    def fetch_area(self, source: str, bbox: Mapping[str, float] | tuple[float, float, float, float], query: str | int) -> list[dict[str, str]]:
        if isinstance(bbox, Mapping):
            # FIRMS order is west,south,east,north. Do not reuse config N/S/W/E order.
            bbox_text = ",".join(str(bbox[key]) for key in ("west", "south", "east", "north"))
        else:
            bbox_text = ",".join(str(value) for value in bbox)
        url = f"{FIRMS_BASE_URL}/{self.map_key}/{source}/{bbox_text}/{query}"
        try:
            response = self.session.get(url, timeout=self.timeout_seconds)
            response.raise_for_status()
        except requests.RequestException as exc:
            raise FirmsError(f"FIRMS request failed: {type(exc).__name__}") from exc
        text = response.text
        if text.lstrip().startswith("Invalid") or text.lstrip().startswith("Error"):
            raise FirmsError(text.strip()[:300])
        return list(csv.DictReader(io.StringIO(text)))

    def fetch_detections(self, source: str, bbox: Mapping[str, float] | tuple[float, float, float, float], query: str | int) -> list[FireDetection]:
        result = []
        for row in self.fetch_area(source, bbox, query):
            detection = parse_detection(row, source=source)
            if detection is not None:
                result.append(detection)
        return result


def _confidence_score(value: str) -> float:
    text = str(value or "").strip().casefold()
    if text in {"nominal", "n", "high", "h"}:
        return 1.0
    if text in {"low", "l"}:
        return 0.4
    try:
        number = float(text.rstrip("%"))
        return max(0.0, min(number / 100.0 if number > 1 else number, 1.0))
    except ValueError:
        return 0.5


def group_detections(detections: Iterable[FireDetection], *, spatial_km: float = 1.0, temporal_hours: float = 6.0) -> list[dict[str, Any]]:
    """Group adjacent pixels and cross-satellite observations without deleting detections."""
    rows = sorted(list(detections), key=lambda item: (item.acquisition_time_utc, item.latitude, item.longitude, item.satellite))
    parent = list(range(len(rows)))
    def find(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index
    def union(first: int, second: int) -> None:
        left, right = find(first), find(second)
        if left != right:
            parent[right] = left
    for i, first in enumerate(rows):
        for j in range(i + 1, len(rows)):
            if (rows[j].acquisition_time_utc - first.acquisition_time_utc).total_seconds() > temporal_hours * 3600:
                break
            if haversine_km((first.longitude, first.latitude), (rows[j].longitude, rows[j].latitude)) <= spatial_km:
                union(i, j)
    components: dict[int, list[FireDetection]] = {}
    for index, row in enumerate(rows):
        components.setdefault(find(index), []).append(row)
    groups = []
    for rank, component in enumerate(sorted(components.values(), key=lambda items: (items[0].acquisition_time_utc, items[0].latitude, items[0].longitude)), 1):
        lats = [row.latitude for row in component]
        lons = [row.longitude for row in component]
        frps = [row.frp for row in component if row.frp is not None]
        groups.append({
            "fire_group_id": f"fire-group-{rank:03d}",
            "centroid": {"lat": round(sum(lats) / len(lats), 6), "lon": round(sum(lons) / len(lons), 6)},
            "first_acquisition_time_utc": min(row.acquisition_time_utc for row in component).isoformat().replace("+00:00", "Z"),
            "last_acquisition_time_utc": max(row.acquisition_time_utc for row in component).isoformat().replace("+00:00", "Z"),
            "detection_count": len(component),
            "satellites": sorted({row.satellite for row in component if row.satellite}),
            "instruments": sorted({row.instrument for row in component if row.instrument}),
            "max_frp": max(frps) if frps else None,
            "median_frp": sorted(frps)[len(frps) // 2] if frps else None,
            "confidence_summary": {"max": max(_confidence_score(row.confidence) for row in component), "values": sorted({row.confidence for row in component})},
            "detections": [detection_to_dict(row) for row in component],
        })
    return groups


def detection_to_dict(row: FireDetection) -> dict[str, Any]:
    result = asdict(row)
    result["acquisition_time_utc"] = row.acquisition_time_utc.isoformat().replace("+00:00", "Z")
    return result


__all__ = ["DEFAULT_SOURCES", "FIRMS_BASE_URL", "FireDetection", "FirmsClient", "FirmsError", "detection_to_dict", "group_detections", "parse_acquisition_time", "parse_detection"]
