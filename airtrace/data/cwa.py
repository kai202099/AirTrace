"""CWA O-A0003-001 client and defensive weather normalisation."""

from __future__ import annotations

import math
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import truststore

truststore.inject_into_ssl()

import requests
from requests import Response
from requests.exceptions import RequestException


CWA_DATASET = "O-A0003-001"
CWA_API_URL = f"https://opendata.cwa.gov.tw/api/v1/rest/datastore/{CWA_DATASET}"
REQUEST_TIMEOUT_SECONDS = 30.0
MAX_RETRIES = 3
RETRY_BACKOFF_SECONDS = 1.0
MAX_RETRY_BACKOFF_SECONDS = 8.0
EARTH_RADIUS_KM = 6371.0088


class CwaError(RuntimeError):
    """An expected CWA API or schema failure."""


@dataclass(frozen=True)
class WeatherStation:
    station_id: str
    station_name: str
    lat: float | None
    lon: float | None
    altitude_m: float | None
    county: str
    township: str
    observation_time_utc: datetime | None
    wind_from_deg: float | None
    wind_speed_mps: float | None
    wind_u_east_mps: float | None
    wind_v_north_mps: float | None
    wind_status: str
    temperature_c: float | None
    relative_humidity_pct: float | None
    pressure_hpa: float | None
    precipitation_mm: float | None
    quality_flags: tuple[str, ...]


@dataclass(frozen=True)
class ParseSummary:
    stations_received: int
    valid_stations: int
    observations_received: int
    invalid_coordinates: int
    invalid_wind: int
    calm: int
    variable: int


class CwaClient:
    """Small HTTP client with bounded retry and no API-key persistence."""

    retryable_statuses = {408, 429, 500, 502, 503, 504}

    def __init__(
        self,
        api_key: str,
        timeout_seconds: float = REQUEST_TIMEOUT_SECONDS,
        max_retries: int = MAX_RETRIES,
    ) -> None:
        if not api_key.strip():
            raise CwaError("CWA_API_KEY is not set")
        self.api_key = api_key.strip()
        self.timeout_seconds = timeout_seconds
        self.max_retries = max_retries
        self.session = requests.Session()
        self.session.headers.update(
            {"Accept": "application/json", "User-Agent": "AirTrace-Weather-Recorder/1.0"}
        )

    def fetch(self) -> dict[str, Any]:
        last_error: Exception | None = None
        params = {"Authorization": self.api_key, "format": "JSON"}
        for attempt in range(self.max_retries + 1):
            try:
                response = self.session.get(
                    CWA_API_URL, params=params, timeout=self.timeout_seconds
                )
                if response.status_code in self.retryable_statuses and attempt < self.max_retries:
                    delay = self._retry_delay(response, attempt)
                    print(
                        f"  transient HTTP {response.status_code}; retrying in {delay:.1f}s",
                        file=sys.stderr,
                    )
                    time.sleep(delay)
                    continue
                response.raise_for_status()
                payload = response.json()
                if not isinstance(payload, dict):
                    raise CwaError("CWA response was not a JSON object")
                if str(payload.get("success", "true")).casefold() == "false":
                    result = payload.get("result")
                    message = result.get("message") if isinstance(result, dict) else None
                    raise CwaError(f"CWA API returned success=false{': ' + str(message) if message else ''}")
                return payload
            except CwaError:
                raise
            except RequestException as exc:
                # Do not retain/print requests' URL-bearing exception: the
                # Authorization query parameter contains the API key.
                last_error = RuntimeError(f"{type(exc).__name__}: HTTPS request failed")
                status = getattr(getattr(exc, "response", None), "status_code", None)
                if status is not None and status not in self.retryable_statuses:
                    break
                if attempt >= self.max_retries:
                    break
                delay = min(RETRY_BACKOFF_SECONDS * (2**attempt), MAX_RETRY_BACKOFF_SECONDS)
                print(
                    f"  request error ({type(exc).__name__}); retrying in {delay:.1f}s",
                    file=sys.stderr,
                )
                time.sleep(delay)
            except ValueError as exc:
                raise CwaError(f"CWA returned invalid JSON: {exc}") from exc
        detail = str(last_error) if last_error else "unknown request failure"
        raise CwaError(f"CWA request failed after retries: {detail}") from last_error

    @staticmethod
    def _retry_delay(response: Response, attempt: int) -> float:
        retry_after = response.headers.get("Retry-After")
        if retry_after:
            try:
                return min(max(float(retry_after), 0.0), MAX_RETRY_BACKOFF_SECONDS)
            except ValueError:
                pass
        return min(RETRY_BACKOFF_SECONDS * (2**attempt), MAX_RETRY_BACKOFF_SECONDS)


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _as_list(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    if isinstance(value, dict):
        return [value]
    return []


def _number(value: Any, *, minimum: float | None = None, maximum: float | None = None) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(str(value).strip())
    except (TypeError, ValueError):
        return None
    if not math.isfinite(result):
        return None
    if minimum is not None and result < minimum:
        return None
    if maximum is not None and result > maximum:
        return None
    return result


def parse_timestamp_utc(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _coordinate(station: dict[str, Any]) -> tuple[float | None, float | None]:
    geo = _as_dict(station.get("GeoInfo"))
    candidates = _as_list(geo.get("Coordinates"))
    # WGS84 is an explicit selection rule. Never use the first coordinate
    # because the API commonly returns TWD67 before WGS84.
    for coordinate in candidates:
        if str(coordinate.get("CoordinateName", "")).strip().casefold() != "wgs84":
            continue
        lat = _number(coordinate.get("StationLatitude"), minimum=-90, maximum=90)
        lon = _number(coordinate.get("StationLongitude"), minimum=-180, maximum=180)
        if lat is not None and lon is not None:
            return lat, lon
    return None, None


def _weather_element(station: dict[str, Any]) -> dict[str, Any]:
    weather = station.get("WeatherElement")
    if isinstance(weather, dict):
        return weather
    # Be tolerant of an occasional list wrapper while keeping the expected
    # O-A0003 schema as the primary path.
    return _as_list(weather)[0] if _as_list(weather) else {}


def normalize_station(station: dict[str, Any]) -> WeatherStation:
    station_id = str(station.get("StationId", "")).strip()
    if not station_id:
        raise CwaError("station record is missing StationId")
    lat, lon = _coordinate(station)
    geo = _as_dict(station.get("GeoInfo"))
    obs_time = _as_dict(station.get("ObsTime")).get("DateTime")
    observation_time = parse_timestamp_utc(obs_time)
    element = _weather_element(station)
    direction_raw = element.get("WindDirection")
    speed_raw = element.get("WindSpeed")
    direction = _number(direction_raw, minimum=0, maximum=990)
    speed = _number(speed_raw, minimum=0)
    flags: list[str] = []
    if lat is None or lon is None:
        flags.append("invalid_wgs84_coordinate")
    if observation_time is None:
        flags.append("invalid_observation_timestamp")

    direction_text = str(direction_raw).strip().casefold() if direction_raw is not None else ""
    speed_text = str(speed_raw).strip().casefold() if speed_raw is not None else ""
    u: float | None = None
    v: float | None = None
    if direction is None or speed is None or direction_text in {"x", "-99", ""} or speed_text in {"x", "-99", ""}:
        wind_status = "invalid"
        flags.append("invalid_wind")
    elif direction == 990:
        wind_status = "variable"
        flags.append("variable_wind")
    elif direction == 0:
        if speed > 0:
            wind_status = "inconsistent"
            flags.extend(("inconsistent_calm_direction", "invalid_wind"))
        else:
            wind_status = "calm"
            flags.append("calm_wind")
    else:
        theta = math.radians(direction)
        u = -speed * math.sin(theta)
        v = -speed * math.cos(theta)
        wind_status = "valid"

    return WeatherStation(
        station_id=station_id,
        station_name=str(station.get("StationName", "")).strip(),
        lat=lat,
        lon=lon,
        altitude_m=_number(geo.get("StationAltitude")),
        county=str(geo.get("CountyName", "")).strip(),
        township=str(geo.get("TownName", "")).strip(),
        observation_time_utc=observation_time,
        wind_from_deg=direction,
        wind_speed_mps=speed,
        wind_u_east_mps=u,
        wind_v_north_mps=v,
        wind_status=wind_status,
        temperature_c=_number(element.get("AirTemperature")),
        relative_humidity_pct=_number(element.get("RelativeHumidity"), minimum=0, maximum=100),
        pressure_hpa=_number(element.get("AirPressure")),
        precipitation_mm=_number(_as_dict(element.get("Now")).get("Precipitation"), minimum=0),
        quality_flags=tuple(flags),
    )


def parse_response(payload: dict[str, Any]) -> tuple[list[WeatherStation], ParseSummary]:
    records_value = payload.get("records")
    if not isinstance(records_value, dict):
        raise CwaError("CWA response is missing records object")
    if "Station" not in records_value:
        raise CwaError("CWA response records is missing Station")
    raw_stations = _as_list(records_value["Station"])
    if not isinstance(records_value["Station"], (list, dict)):
        raise CwaError("CWA response Station is not a list or object")
    stations: list[WeatherStation] = []
    invalid_coordinates = invalid_wind = calm = variable = 0
    observations_received = 0
    for raw_station in raw_stations:
        try:
            station = normalize_station(raw_station)
        except CwaError:
            continue
        stations.append(station)
        if station.observation_time_utc is not None:
            observations_received += 1
        if station.lat is None or station.lon is None:
            invalid_coordinates += 1
        if station.wind_status in {"invalid", "inconsistent"}:
            invalid_wind += 1
        elif station.wind_status == "calm":
            calm += 1
        elif station.wind_status == "variable":
            variable += 1
    return stations, ParseSummary(
        stations_received=len(raw_stations),
        valid_stations=sum(station.lat is not None and station.lon is not None for station in stations),
        observations_received=observations_received,
        invalid_coordinates=invalid_coordinates,
        invalid_wind=invalid_wind,
        calm=calm,
        variable=variable,
    )


def iso_utc(value: datetime | None) -> str:
    if value is None:
        return ""
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def haversine_km(first: tuple[float, float], second: tuple[float, float]) -> float:
    lon1, lat1 = map(math.radians, first)
    lon2, lat2 = map(math.radians, second)
    dlon, dlat = lon2 - lon1, lat2 - lat1
    a = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(math.sqrt(min(1.0, a)))
