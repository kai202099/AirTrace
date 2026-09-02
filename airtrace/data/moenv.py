"""Small, defensive client and parser for MOENV v2 open-data APIs."""

from __future__ import annotations

import json
import math
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import requests
import truststore
from requests import Response
from requests.exceptions import RequestException

truststore.inject_into_ssl()


MOENV_DATASET = "AQX_P_432"
MOENV_API_URL = f"https://data.moenv.gov.tw/api/v2/{MOENV_DATASET.lower()}"
REQUEST_TIMEOUT_SECONDS = 30.0
MAX_RETRIES = 3
RETRY_BACKOFF_SECONDS = 1.0
MAX_RETRY_BACKOFF_SECONDS = 8.0
MAX_PAGES = 100
EARTH_RADIUS_KM = 6371.0088


class MoenvError(RuntimeError):
    """An expected MOENV API or schema error."""


@dataclass(frozen=True)
class ReferenceAirRecord:
    site_id: str
    site_name: str
    county: str
    lat: float | None
    lon: float | None
    publish_time_utc: datetime | None
    aqi: float | None
    status: str
    primary_pollutant: str
    pm25_ugm3: float | None
    pm25_avg_ugm3: float | None
    pm10_ugm3: float | None
    pm10_avg_ugm3: float | None
    so2_ppb: float | None
    so2_avg_ppb: float | None
    no2_ppb: float | None
    nox_ppb: float | None
    no_ppb: float | None
    co_ppm: float | None
    co_8hr_ppm: float | None
    o3_ppb: float | None
    o3_8hr_ppb: float | None
    wind_speed_mps: float | None
    wind_direction_deg: float | None
    quality_flags: tuple[str, ...]


class MoenvClient:
    """Bounded-retry MOENV client; the API key is kept out of diagnostics."""

    retryable_statuses = {408, 429, 500, 502, 503, 504}

    def __init__(
        self,
        api_key: str,
        *,
        dataset: str = MOENV_DATASET,
        timeout_seconds: float = REQUEST_TIMEOUT_SECONDS,
        max_retries: int = MAX_RETRIES,
        page_limit: int = 1000,
        max_pages: int = MAX_PAGES,
    ) -> None:
        if not api_key.strip():
            raise MoenvError("MOENV_API_KEY is not set")
        if timeout_seconds <= 0:
            raise MoenvError("timeout must be positive")
        if max_retries < 0:
            raise MoenvError("max_retries must not be negative")
        if page_limit <= 0 or page_limit > 1000:
            raise MoenvError("page_limit must be between 1 and 1000")
        if max_pages <= 0:
            raise MoenvError("max_pages must be positive")
        normalized_dataset = str(dataset).strip().upper()
        if not normalized_dataset or not normalized_dataset.replace("_", "").isalnum():
            raise MoenvError("dataset must be a non-empty API dataset identifier")
        self.api_key = api_key.strip()
        self.dataset = normalized_dataset
        self.api_url = f"https://data.moenv.gov.tw/api/v2/{self.dataset.lower()}"
        self.timeout_seconds = timeout_seconds
        self.max_retries = max_retries
        self.page_limit = page_limit
        self.max_pages = max_pages
        self.session = requests.Session()
        self.session.headers.update(
            {"Accept": "application/json", "User-Agent": "AirTrace-MOENV/1.0"}
        )

    @staticmethod
    def _retry_delay(response: Response, attempt: int) -> float:
        retry_after = response.headers.get("Retry-After")
        if retry_after:
            try:
                return min(max(float(retry_after), 0.0), MAX_RETRY_BACKOFF_SECONDS)
            except ValueError:
                pass
        return min(RETRY_BACKOFF_SECONDS * (2**attempt), MAX_RETRY_BACKOFF_SECONDS)

    def fetch_page(self, offset: int, limit: int | None = None) -> Any:
        page_limit = limit or self.page_limit
        params = {"format": "json", "offset": offset, "limit": page_limit, "api_key": self.api_key}
        last_error: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                response = self.session.get(
                    self.api_url, params=params, timeout=self.timeout_seconds
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
                if isinstance(payload, list):
                    # A bad item is isolated to that item; the remaining station
                    # records must still be available for this ingest.
                    return [item for item in payload if isinstance(item, dict)]
                if not isinstance(payload, dict):
                    raise MoenvError("MOENV response was neither a JSON object nor record array")
                if str(payload.get("success", "true")).casefold() == "false":
                    message = payload.get("message")
                    raise MoenvError(
                        f"MOENV API returned success=false{': ' + str(message) if message else ''}"
                    )
                result = payload.get("result")
                if not isinstance(result, dict):
                    raise MoenvError("MOENV response is missing result object")
                records = result.get("records")
                if not isinstance(records, list):
                    raise MoenvError("MOENV response result is missing records list")
                return payload
            except MoenvError:
                raise
            except RequestException as exc:
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
                raise MoenvError(f"MOENV returned invalid JSON: {exc}") from exc
        detail = str(last_error) if last_error else "unknown request failure"
        raise MoenvError(f"MOENV request failed after retries: {detail}") from last_error

    def iter_pages(self, start_offset: int = 0):
        """Yield ``(records, raw_page)`` without retaining a large catalogue."""
        if start_offset < 0:
            raise MoenvError("start_offset must not be negative")
        offset = start_offset
        previous_signature: str | None = None
        for page_number in range(self.max_pages):
            payload = self.fetch_page(offset, self.page_limit)
            if isinstance(payload, list):
                page_records = payload
            else:
                page_records = payload["result"]["records"]
            if not page_records:
                break
            signature = json.dumps(page_records, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            if signature == previous_signature:
                raise MoenvError("MOENV pagination repeated the same page")
            previous_signature = signature
            yield [item for item in page_records if isinstance(item, dict)], payload
            if len(page_records) < self.page_limit:
                break
            offset += len(page_records)
        else:
            raise MoenvError(f"MOENV pagination exceeded {self.max_pages} pages")

    def fetch_all(self) -> tuple[list[dict[str, Any]], list[Any]]:
        """Return all records and the successful page responses used to obtain them."""
        records: list[dict[str, Any]] = []
        pages: list[Any] = []
        for page_records, payload in self.iter_pages():
            records.extend(page_records)
            pages.append(payload)
        return records, pages


def _field(record: dict[str, Any], name: str) -> Any:
    wanted = name.casefold()
    for key, value in record.items():
        if str(key).casefold() == wanted:
            return value
    return None


def _text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def parse_number(
    value: Any,
    *,
    minimum: float | None = None,
    maximum: float | None = None,
) -> tuple[float | None, bool]:
    """Return (number, invalid); empty/missing markers are NULL but not invalid."""
    if value is None:
        return None, False
    if isinstance(value, bool):
        return None, True
    text = str(value).strip()
    if not text or text.casefold() in {"-", "--", "na", "n/a", "null", "none", "x"}:
        return None, False
    try:
        number = float(text)
    except (TypeError, ValueError):
        return None, True
    if not math.isfinite(number):
        return None, True
    if minimum is not None and number < minimum:
        return None, True
    if maximum is not None and number > maximum:
        return None, True
    return number, False


def parse_timestamp_utc(value: Any, *, source_timezone: str = "Asia/Taipei") -> datetime | None:
    """Parse an aware timestamp or a MOENV local Taiwan timestamp into UTC."""
    text = _text(value)
    if not text:
        return None
    normalized = text[:-1] + "+00:00" if text.endswith("Z") else text
    parsed: datetime | None = None
    try:
        parsed = datetime.fromisoformat(normalized.replace("/", "-"))
    except ValueError:
        for pattern in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S"):
            try:
                parsed = datetime.strptime(text, pattern)
                break
            except ValueError:
                continue
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        try:
            parsed = parsed.replace(tzinfo=ZoneInfo(source_timezone))
        except ZoneInfoNotFoundError:
            return None
    return parsed.astimezone(timezone.utc)


def parse_record(raw: dict[str, Any], *, source_timezone: str = "Asia/Taipei") -> ReferenceAirRecord | None:
    site_id = _text(_field(raw, "SiteId"))
    if not site_id:
        return None
    flags: list[str] = []

    lat, lat_invalid = parse_number(_field(raw, "Latitude"), minimum=-90, maximum=90)
    lon, lon_invalid = parse_number(_field(raw, "Longitude"), minimum=-180, maximum=180)
    if lat_invalid or lon_invalid or lat is None or lon is None:
        lat, lon = None, None
        flags.append("invalid_coordinate")

    def number(name: str, *, minimum: float | None = None, maximum: float | None = None) -> float | None:
        value, invalid = parse_number(_field(raw, name), minimum=minimum, maximum=maximum)
        if invalid:
            flags.append(f"invalid_{name.casefold().replace('.', '')}")
        return value

    publish_time = parse_timestamp_utc(_field(raw, "publishtime"), source_timezone=source_timezone)
    if publish_time is None:
        flags.append("invalid_publish_timestamp")

    return ReferenceAirRecord(
        site_id=site_id,
        site_name=_text(_field(raw, "SiteName")),
        county=_text(_field(raw, "County")),
        lat=lat,
        lon=lon,
        publish_time_utc=publish_time,
        aqi=number("AQI", minimum=0),
        status=_text(_field(raw, "Status")),
        primary_pollutant=_text(_field(raw, "Pollutant")),
        pm25_ugm3=number("PM2.5", minimum=0),
        pm25_avg_ugm3=number("PM2.5_AVG", minimum=0),
        pm10_ugm3=number("PM10", minimum=0),
        pm10_avg_ugm3=number("PM10_AVG", minimum=0),
        so2_ppb=number("SO2", minimum=0),
        so2_avg_ppb=number("SO2_AVG", minimum=0),
        no2_ppb=number("NO2", minimum=0),
        nox_ppb=number("NOx", minimum=0),
        no_ppb=number("NO", minimum=0),
        co_ppm=number("CO", minimum=0),
        co_8hr_ppm=number("CO_8hr", minimum=0),
        o3_ppb=number("O3", minimum=0),
        o3_8hr_ppb=number("O3_8hr", minimum=0),
        wind_speed_mps=number("WIND_SPEED", minimum=0),
        wind_direction_deg=number("WIND_DIREC", minimum=0, maximum=360),
        quality_flags=tuple(dict.fromkeys(flags)),
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
