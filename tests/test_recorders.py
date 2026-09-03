from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]


def load_script(name: str):
    path = ROOT / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"airtrace_{name}", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeConnection:
    def __init__(self) -> None:
        self.closed = False

    def execute(self, *args, **kwargs):
        return self

    def begin(self) -> None:
        return None

    def commit(self) -> None:
        return None

    def rollback(self) -> None:
        return None

    def close(self) -> None:
        self.closed = True


def test_pm25_connection_opens_after_fetch_and_closes(monkeypatch, tmp_path: Path) -> None:
    recorder = load_script("record_pm25")
    connections: list[FakeConnection] = []
    fetch_connect_counts: list[int] = []

    def connect(*args, **kwargs):
        connection = FakeConnection()
        connections.append(connection)
        return connection

    class Region:
        region_id = "test"
        name = "Test"
        bbox = {}
        timezone = "UTC"

    monkeypatch.setattr(recorder.duckdb, "connect", connect)
    monkeypatch.setattr(recorder.RegionConfig, "load", lambda path: Region())
    monkeypatch.setattr(recorder, "ApiClient", lambda *args, **kwargs: object())
    def write_snapshot(*args, **kwargs):
        assert not connections
        return tmp_path / "raw.json.gz"

    monkeypatch.setattr(recorder, "write_raw_snapshot", write_snapshot)
    monkeypatch.setattr(recorder, "ensure_schema", lambda connection: None)
    monkeypatch.setattr(recorder, "upsert_stations", lambda connection, rows, now: None)
    monkeypatch.setattr(recorder, "insert_observations", lambda connection, rows, now: (1, 0))
    monkeypatch.setattr(recorder, "save_poll_health", lambda connection, started, completed, values: None)

    quality = {
        "invalid_coordinate": 0,
        "invalid_pm25": 0,
        "invalid_timestamp": 0,
        "missing_observation": 0,
        "future_ahead_seconds": [],
    }

    def fetch(client, region, started):
        fetch_connect_counts.append(len(connections))
        return ([{"observation": object(), "freshness_status": "fresh"}], {"quality": quality, "things_pages": 1, "api_reported_count": 1, "nested_latest_used": False, "fallback_problems": [], "things": []})

    monkeypatch.setattr(recorder, "fetch_pm25_records", fetch)
    assert recorder.run_once(tmp_path / "config.json", tmp_path / "pm25.duckdb", tmp_path / "raw", 1.0)
    assert fetch_connect_counts == [0]
    assert connections and all(connection.closed for connection in connections)


def test_weather_connection_opens_after_fetch_and_closes(monkeypatch, tmp_path: Path) -> None:
    recorder = load_script("record_weather")
    connections: list[FakeConnection] = []
    fetch_connect_counts: list[int] = []

    def connect(*args, **kwargs):
        connection = FakeConnection()
        connections.append(connection)
        return connection

    summary = SimpleNamespace(
        stations_received=0,
        valid_stations=0,
        observations_received=0,
        invalid_coordinates=0,
        invalid_wind=0,
        calm=0,
        variable=0,
    )

    class Client:
        def __init__(self, *args, **kwargs):
            pass

        def fetch(self):
            fetch_connect_counts.append(len(connections))
            return {}

    monkeypatch.setattr(recorder.duckdb, "connect", connect)
    monkeypatch.setattr(recorder, "CwaClient", Client)
    monkeypatch.setattr(recorder, "parse_response", lambda payload: ([], summary))
    def write_snapshot(*args, **kwargs):
        assert not connections
        return tmp_path / "raw.json.gz"

    monkeypatch.setattr(recorder, "write_raw_snapshot", write_snapshot)
    monkeypatch.setattr(recorder, "ensure_schema", lambda connection: None)
    monkeypatch.setattr(recorder, "upsert_stations", lambda connection, rows, seen_at: None)
    monkeypatch.setattr(recorder, "insert_observations", lambda connection, rows, ingested_at: (0, 0))
    monkeypatch.setattr(recorder, "save_poll_health", lambda connection, started, completed, values: None)

    assert recorder.run_once(tmp_path / "weather.duckdb", tmp_path / "raw", 1.0)
    assert fetch_connect_counts == [0]
    assert connections and all(connection.closed for connection in connections)


def test_pm25_failure_health_connection_also_closes(monkeypatch, tmp_path: Path) -> None:
    recorder = load_script("record_pm25")
    connection = FakeConnection()
    class Region:
        region_id = "test"
        name = "Test"
        bbox = {}
        timezone = "UTC"

    monkeypatch.setattr(recorder.duckdb, "connect", lambda *args, **kwargs: connection)
    monkeypatch.setattr(recorder.RegionConfig, "load", lambda path: Region())
    monkeypatch.setattr(recorder, "ApiClient", lambda *args, **kwargs: object())
    monkeypatch.setattr(recorder, "fetch_pm25_records", lambda *args, **kwargs: (_ for _ in ()).throw(ValueError("network down")))
    monkeypatch.setattr(recorder, "ensure_schema", lambda connection: None)
    monkeypatch.setattr(recorder, "save_poll_health", lambda connection, started, completed, values: None)
    assert not recorder.run_once(tmp_path / "config.json", tmp_path / "pm25.duckdb", tmp_path / "raw", 1.0)
    assert connection.closed


def test_weather_failure_health_connection_also_closes(monkeypatch, tmp_path: Path) -> None:
    recorder = load_script("record_weather")
    connection = FakeConnection()
    monkeypatch.setattr(recorder.duckdb, "connect", lambda *args, **kwargs: connection)
    monkeypatch.setattr(recorder, "CwaClient", lambda *args, **kwargs: (_ for _ in ()).throw(ValueError("network down")))
    monkeypatch.setattr(recorder, "ensure_schema", lambda connection: None)
    monkeypatch.setattr(recorder, "save_poll_health", lambda connection, started, completed, values: None)
    assert not recorder.run_once(tmp_path / "weather.duckdb", tmp_path / "raw", 1.0)
    assert connection.closed
