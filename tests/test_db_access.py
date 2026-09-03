from __future__ import annotations

from pathlib import Path

import pytest

from airtrace import db
from airtrace.analysis.wind import _open_read_only


class FakeReadConnection:
    def __init__(self) -> None:
        self.closed = False

    def execute(self, sql: str):
        assert sql == "SET TimeZone='UTC'"
        return self

    def close(self) -> None:
        self.closed = True


def test_read_only_retries_transient_writer_lock_and_closes(monkeypatch, tmp_path: Path) -> None:
    attempts = 0
    sleeps: list[float] = []
    connection = FakeReadConnection()

    def connect(path: str, read_only: bool):
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise RuntimeError("READ_ONLY_ACCESS_FAILED: file is already open by another process")
        return connection

    monkeypatch.setattr(db.duckdb, "connect", connect)
    monkeypatch.setattr(db.time_module, "sleep", sleeps.append)
    result = _open_read_only(tmp_path / "recorder.duckdb")
    assert result is connection
    assert attempts == 3
    assert sleeps == list(db.READ_ONLY_RETRY_DELAYS_SECONDS[:2])
    result.close()
    assert connection.closed


def test_read_only_exhaustion_is_bounded_and_actionable(monkeypatch, tmp_path: Path) -> None:
    sleeps: list[float] = []

    monkeypatch.setattr(db.duckdb, "connect", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("IO Error: Could not set lock; file is already open")))
    monkeypatch.setattr(db.time_module, "sleep", sleeps.append)
    with pytest.raises(db.ReadOnlyDatabaseBusyError, match=r"DB_LOCKED: recorder is still writing.*retry window 1\.25s exhausted"):
        db.connect_read_only(tmp_path / "recorder.duckdb")
    assert sleeps == list(db.READ_ONLY_RETRY_DELAYS_SECONDS)


def test_read_only_non_lock_error_is_not_retried(monkeypatch, tmp_path: Path) -> None:
    attempts = 0

    def connect(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        raise RuntimeError("schema is unreadable")

    monkeypatch.setattr(db.duckdb, "connect", connect)
    with pytest.raises(RuntimeError, match="schema is unreadable"):
        db.connect_read_only(tmp_path / "recorder.duckdb")
    assert attempts == 1
