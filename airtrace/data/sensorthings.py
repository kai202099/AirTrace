"""Small shared client and parser for the Environmental SensorThings API."""

from __future__ import annotations

import json
import math
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote, urljoin

import requests
from requests import Response
from requests.exceptions import RequestException


API_BASE_URL = "https://sta.colife.org.tw/STA_AirQuality_EPAIoT/v1.0/"
PM25_DATASTREAM_NAME = "PM2.5"
PAGE_SIZE = 100
REQUEST_TIMEOUT_SECONDS = 30.0
MAX_RETRIES = 4
RETRY_BACKOFF_SECONDS = 1.0
MAX_RETRY_BACKOFF_SECONDS = 8.0
MAX_COLLECTION_PAGES = 1_000


class SensorThingsError(RuntimeError):
    """An expected source/API failure."""


@dataclass(frozen=True)
class RegionConfig:
    region_id: str
    name: str
    bbox: dict[str, float]
    timezone: str

    @classmethod
    def load(cls, path: Path) -> "RegionConfig":
        with path.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
        bbox = payload["context_bbox"]
        required = ("north", "south", "west", "east")
        if any(key not in bbox for key in required):
            raise SensorThingsError("region config is missing context_bbox values")
        result = {key: float(bbox[key]) for key in required}
        if not (
            result["south"] <= result["north"]
            and result["west"] <= result["east"]
        ):
            raise SensorThingsError("region context_bbox is not ordered")
        if payload.get("timezone") != "Asia/Taipei":
            raise SensorThingsError("region timezone must be Asia/Taipei")
        return cls(
            region_id=str(payload["region_id"]),
            name=str(payload["name"]),
            bbox=result,
            timezone=str(payload["timezone"]),
        )


class ApiClient:
    """Polite JSON client with bounded retries for transient failures."""

    retryable_statuses = {429, 500, 502, 503, 504}

    def __init__(
        self,
        base_url: str = API_BASE_URL,
        timeout_seconds: float = REQUEST_TIMEOUT_SECONDS,
        max_retries: int = MAX_RETRIES,
    ) -> None:
        self.base_url = base_url.rstrip("/") + "/"
        self.timeout_seconds = timeout_seconds
        self.max_retries = max_retries
        self.session = requests.Session()
        self.session.headers.update(
            {"Accept": "application/json", "User-Agent": "AirTrace-PM25-Recorder/1.0"}
        )

    def get_json(self, url: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        request_url = append_query(url, params)
        last_error: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                response = self.session.get(request_url, timeout=self.timeout_seconds)
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
                    raise SensorThingsError("SensorThings response was not a JSON object")
                return payload
            except SensorThingsError:
                raise
            except RequestException as exc:
                last_error = exc
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
                raise SensorThingsError(f"SensorThings returned invalid JSON: {exc}") from exc
        detail = str(last_error) if last_error else "unknown request failure"
        raise SensorThingsError(f"SensorThings request failed after retries: {detail}") from last_error

    @staticmethod
    def _retry_delay(response: Response, attempt: int) -> float:
        retry_after = response.headers.get("Retry-After")
        if retry_after:
            try:
                return min(max(float(retry_after), 0.0), MAX_RETRY_BACKOFF_SECONDS)
            except ValueError:
                pass
        return min(RETRY_BACKOFF_SECONDS * (2**attempt), MAX_RETRY_BACKOFF_SECONDS)


def api_url(path: str, base_url: str = API_BASE_URL) -> str:
    return urljoin(base_url.rstrip("/") + "/", path.lstrip("/"))


def append_query(url: str, params: dict[str, Any] | None) -> str:
    if not params:
        return url
    safe_value_chars = "$'(),;/:="
    encoded = "&".join(
        f"{quote(str(key), safe='$')}={quote(str(value), safe=safe_value_chars)}"
        for key, value in params.items()
    )
    return f"{url}{'&' if '?' in url else '?'}{encoded}"


def fetch_collection(
    client: ApiClient,
    path_or_url: str,
    params: dict[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], int, int | None]:
    """Fetch all pages, following SensorThings @iot.nextLink."""

    next_url = path_or_url if path_or_url.startswith("http") else api_url(path_or_url, client.base_url)
    next_params = params
    rows: list[dict[str, Any]] = []
    page_count = 0
    reported_count: int | None = None
    while next_url:
        if page_count >= MAX_COLLECTION_PAGES:
            raise SensorThingsError(f"collection exceeded {MAX_COLLECTION_PAGES} pages")
        page = client.get_json(next_url, params=next_params)
        next_params = None
        page_count += 1
        if page_count == 1 and isinstance(page.get("@iot.count"), int):
            reported_count = page["@iot.count"]
        values = page.get("value", [])
        if values is None:
            values = []
        if not isinstance(values, list):
            raise SensorThingsError("SensorThings collection had a non-list value")
        rows.extend(item for item in values if isinstance(item, dict))
        candidate_next = page.get("@iot.nextLink")
        if not candidate_next:
            break
        if not isinstance(candidate_next, str):
            raise SensorThingsError("SensorThings @iot.nextLink was not a string")
        resolved = urljoin(next_url, candidate_next)
        if resolved == next_url:
            raise SensorThingsError("SensorThings @iot.nextLink did not advance")
        next_url = resolved
    return rows, page_count, reported_count


def normalized_key(value: Any) -> str:
    return "".join(ch for ch in str(value).casefold() if ch.isalnum())


def get_value(thing: dict[str, Any], *names: str, default: Any = "") -> Any:
    wanted = {normalized_key(name) for name in names}
    sources: list[dict[str, Any]] = []
    if isinstance(thing.get("properties"), dict):
        sources.append(thing["properties"])
    sources.append(thing)
    for source in sources:
        for key, value in source.items():
            if normalized_key(key) in wanted and value is not None:
                return value
    return default


def first_collection(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    if isinstance(value, dict):
        return [value]
    return []


def extract_point(value: Any) -> tuple[float, float] | None:
    if isinstance(value, dict):
        if "location" in value:
            return extract_point(value["location"])
        if "coordinates" in value:
            return extract_point(value["coordinates"])
        return None
    if isinstance(value, (tuple, list)) and len(value) >= 2:
        if isinstance(value[0], (list, tuple, dict)):
            return None
        try:
            longitude, latitude = float(value[0]), float(value[1])
        except (TypeError, ValueError):
            return None
        return (longitude, latitude) if math.isfinite(longitude) and math.isfinite(latitude) else None
    if isinstance(value, str):
        parts = value.strip().replace(",", " ").split()
        if len(parts) >= 2:
            try:
                longitude, latitude = float(parts[0]), float(parts[1])
            except ValueError:
                return None
            return (longitude, latitude) if math.isfinite(longitude) and math.isfinite(latitude) else None
    return None


def valid_point(point: tuple[float, float] | None) -> bool:
    return bool(point and -180 <= point[0] <= 180 and -90 <= point[1] <= 90)


def point_in_bbox(point: tuple[float, float] | None, bbox: dict[str, float]) -> bool:
    return bool(
        valid_point(point)
        and bbox["west"] <= point[0] <= bbox["east"]
        and bbox["south"] <= point[1] <= bbox["north"]
    )


def parse_utc_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if "/" in text:
        text = text.rsplit("/", 1)[-1]
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def iso_utc(value: datetime | None) -> str:
    if value is None:
        return ""
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def parse_pm25(value: Any) -> tuple[float | None, bool]:
    if value is None or isinstance(value, bool):
        return None, False
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None, False
    return (parsed, True) if math.isfinite(parsed) and parsed >= 0 else (None, False)


def datastream_id(datastream: dict[str, Any]) -> Any:
    return datastream.get("@iot.id", datastream.get("id", ""))


def thing_id(thing: dict[str, Any]) -> Any:
    return thing.get("@iot.id", thing.get("id", ""))


def station_id(thing: dict[str, Any]) -> str:
    value = get_value(thing, "stationID", "station_id")
    return str(value) if value not in (None, "") else f"thing:{thing_id(thing)}"


def latest_observation_from_expansion(datastream: dict[str, Any]) -> dict[str, Any] | None:
    observations = first_collection(datastream.get("Observations"))
    if not observations:
        return None
    parseable = [item for item in observations if parse_utc_timestamp(item.get("phenomenonTime"))]
    return max(
        parseable or observations,
        key=lambda item: parse_utc_timestamp(item.get("phenomenonTime"))
        or datetime.min.replace(tzinfo=timezone.utc),
    )


def observation_endpoint(datastream: dict[str, Any], base_url: str = API_BASE_URL) -> str | None:
    value = datastream_id(datastream)
    return None if value in (None, "") else api_url(
        f"Datastreams({quote(str(value), safe='')})/Observations", base_url
    )


def latest_observation_for_datastream(client: ApiClient, datastream: dict[str, Any]) -> dict[str, Any] | None:
    endpoint = observation_endpoint(datastream, client.base_url)
    if endpoint is None:
        return None
    page = client.get_json(endpoint, {"$orderby": "phenomenonTime desc", "$top": 1})
    values = page.get("value", [])
    items = [item for item in values if isinstance(item, dict)] if isinstance(values, list) else []
    return max(
        items,
        key=lambda item: parse_utc_timestamp(item.get("phenomenonTime"))
        or datetime.min.replace(tzinfo=timezone.utc),
    ) if items else None


def build_expand(include_latest: bool) -> str:
    if not include_latest:
        return "Locations,Datastreams($filter=name eq 'PM2.5')"
    return "Locations,Datastreams($filter=name eq 'PM2.5';$expand=Observations($orderby=phenomenonTime desc;$top=1))"


def load_candidate_things(client: ApiClient, region: RegionConfig) -> tuple[list[dict[str, Any]], bool, int, int | None, list[str]]:
    b = region.bbox
    spatial_filter = (
        "geo.intersects(Locations/location,geography'"
        f"POLYGON(({b['west']:.2f} {b['south']:.2f},{b['east']:.2f} {b['south']:.2f},"
        f"{b['east']:.2f} {b['north']:.2f},{b['west']:.2f} {b['north']:.2f},"
        f"{b['west']:.2f} {b['south']:.2f}))')"
    )
    problems: list[str] = []
    for use_spatial in (True, False):
        for include_latest in (True, False):
            params: dict[str, Any] = {"$top": PAGE_SIZE, "$expand": build_expand(include_latest)}
            if use_spatial:
                params["$filter"] = spatial_filter
            try:
                things, pages, reported = fetch_collection(client, "Things", params)
                return things, include_latest, pages, reported, problems
            except SensorThingsError as exc:
                route = f"{'spatial' if use_spatial else 'local'} query"
                route += "+latest expansion" if include_latest else "+metadata"
                problems.append(f"{route}: {exc}")
                print(f"  {route} unavailable; trying a bounded fallback", file=sys.stderr)
    raise SensorThingsError("could not load SensorThings Things collection (" + "; ".join(problems[-2:]) + ")")


def locations_for_thing(thing: dict[str, Any], client: ApiClient) -> list[dict[str, Any]]:
    locations = first_collection(thing.get("Locations"))
    link = thing.get("Locations@iot.navigationLink")
    if not locations and isinstance(link, str) and link:
        try:
            locations, _, _ = fetch_collection(client, link, {"$top": 10})
        except SensorThingsError:
            pass
    return locations


def datastreams_for_thing(thing: dict[str, Any], client: ApiClient) -> list[dict[str, Any]]:
    datastreams = first_collection(thing.get("Datastreams"))
    next_link = thing.get("Datastreams@iot.nextLink")
    if isinstance(next_link, str) and next_link:
        try:
            extra, _, _ = fetch_collection(client, next_link)
            datastreams.extend(extra)
        except SensorThingsError:
            pass
    if not datastreams:
        link = thing.get("Datastreams@iot.navigationLink")
        if isinstance(link, str) and link:
            try:
                datastreams, _, _ = fetch_collection(client, link, {"$filter": "name eq 'PM2.5'", "$top": PAGE_SIZE})
            except SensorThingsError:
                return []
    return [item for item in datastreams if str(item.get("name", "")).strip() == PM25_DATASTREAM_NAME]


def coordinates_for_thing(thing: dict[str, Any], datastream: dict[str, Any], client: ApiClient) -> tuple[float, float] | None:
    for location in locations_for_thing(thing, client):
        point = extract_point(location)
        if point is not None:
            return point
    return extract_point(datastream.get("observedArea"))


def metadata_row(thing: dict[str, Any], datastream: dict[str, Any], point: tuple[float, float] | None) -> dict[str, Any]:
    return {
        "thing_id": str(thing_id(thing)),
        "station_id": station_id(thing),
        "station_name": str(get_value(thing, "stationName", "station_name", default=thing.get("name", ""))),
        "datastream_id": str(datastream_id(datastream)),
        "lat": point[1] if point else None,
        "lon": point[0] if point else None,
        "city": str(get_value(thing, "city")),
        "township": str(get_value(thing, "township", "town")),
        "area_type": str(get_value(thing, "areaType", "area_type")),
        "area_description": str(get_value(thing, "areaDescription", "area_description", "area")),
        "is_outdoor": get_value(thing, "isOutdoor", "is_outdoor"),
        "is_mobile": get_value(thing, "isMobile", "is_mobile"),
        "project_name": str(get_value(thing, "projectName", "project_name")),
    }


def as_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().casefold()
    if text in {"true", "1", "yes", "y", "是"}:
        return True
    if text in {"false", "0", "no", "n", "否"}:
        return False
    return None


def fetch_pm25_records(client: ApiClient, region: RegionConfig, now_utc: datetime) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Fetch one latest PM2.5 observation per in-bbox datastream plus metadata."""

    things, include_latest, page_count, reported_count, problems = load_candidate_things(client, region)
    rows: list[dict[str, Any]] = []
    seen_datastream_ids: set[str] = set()
    quality: dict[str, Any] = {
        "invalid_coordinate": 0,
        "outside_bbox": 0,
        "missing_observation": 0,
        "invalid_pm25": 0,
        "invalid_timestamp": 0,
        "future_timestamp": 0,
        "future_ahead_seconds": [],
        "observation_request_error": 0,
    }
    for thing in things:
        for datastream in datastreams_for_thing(thing, client):
            ds_id = datastream_id(datastream)
            key = str(ds_id) if ds_id not in (None, "") else f"thing:{thing_id(thing)}"
            if key in seen_datastream_ids:
                continue
            seen_datastream_ids.add(key)
            if not include_latest and not datastream.get("Observations"):
                try:
                    datastream["_latest_observation"] = latest_observation_for_datastream(client, datastream)
                except SensorThingsError:
                    datastream["_latest_observation"] = None
                    quality["observation_request_error"] += 1
            point = coordinates_for_thing(thing, datastream, client)
            row = metadata_row(thing, datastream, point)
            if not valid_point(point):
                quality["invalid_coordinate"] += 1
                continue
            # WGS84 coordinates are the sole membership source of truth.  The
            # city/township fields below are retained only for display and
            # diagnostics because this API's metadata can disagree with them.
            if not point_in_bbox(point, region.bbox):
                quality["outside_bbox"] += 1
                continue
            observation = datastream.get("_latest_observation") or latest_observation_from_expansion(datastream)
            if observation is None:
                quality["missing_observation"] += 1
                row.update({"observation": None, "phenomenon_time_utc": None, "pm25_ugm3": None, "quality_flags": "missing_observation", "freshness_status": "missing"})
                rows.append(row)
                continue
            raw_time = observation.get("phenomenonTime")
            phenomenon_time = parse_utc_timestamp(raw_time)
            pm25, valid_pm25 = parse_pm25(observation.get("result"))
            flags: list[str] = []
            freshness = "invalid"
            if phenomenon_time is None:
                quality["invalid_timestamp"] += 1
                flags.append("invalid_timestamp")
            else:
                age_seconds = (now_utc - phenomenon_time).total_seconds()
                if age_seconds < 0:
                    ahead = -age_seconds
                    quality["future_timestamp"] += 1
                    quality["future_ahead_seconds"].append(ahead)
                    flags.append(f"future_timestamp_ahead_seconds={ahead:.3f}")
                    freshness = "clock_ahead" if ahead <= 300 else "suspicious_future_timestamp"
                elif age_seconds <= 600:
                    freshness = "fresh"
                elif age_seconds <= 1800:
                    freshness = "stale"
                else:
                    freshness = "offline"
            if not valid_pm25:
                quality["invalid_pm25"] += 1
                flags.append("invalid_pm25")
            row.update({
                "observation": observation,
                "phenomenon_time_utc": phenomenon_time,
                "pm25_ugm3": pm25,
                "quality_flags": ";".join(flags),
                "freshness_status": freshness,
            })
            rows.append(row)
    return rows, {
        "things": things,
        "things_pages": page_count,
        "api_reported_count": reported_count,
        "nested_latest_used": include_latest,
        "fallback_problems": problems,
        "quality": quality,
    }
