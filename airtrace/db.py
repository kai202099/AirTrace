"""Small, bounded helpers for reading recorder-owned DuckDB files."""

from __future__ import annotations

import time as time_module
from pathlib import Path

import duckdb


# The recorder normally holds its writer for only the final transaction. These
# short backoffs cover that hand-off without making replay wait indefinitely.
READ_ONLY_RETRY_DELAYS_SECONDS = (0.15, 0.35, 0.75)
READ_ONLY_MAX_WAIT_SECONDS = sum(READ_ONLY_RETRY_DELAYS_SECONDS)


class ReadOnlyDatabaseBusyError(RuntimeError):
    """A recorder still owns the database after the bounded retry window."""


def is_transient_lock_error(error: BaseException) -> bool:
    message = str(error).lower()
    return any(
        marker in message
        for marker in (
            "lock",
            "read_only_access_failed",
            "read only access failed",
            "already open",
            "another process",
            "being used",
        )
    )


def connect_read_only(database_path: Path) -> duckdb.DuckDBPyConnection:
    """Open a UTC read-only connection, retrying only transient writer locks."""

    last_error: Exception | None = None
    for attempt in range(len(READ_ONLY_RETRY_DELAYS_SECONDS) + 1):
        connection: duckdb.DuckDBPyConnection | None = None
        try:
            connection = duckdb.connect(str(database_path), read_only=True)
            connection.execute("SET TimeZone='UTC'")
            return connection
        except Exception as exc:
            last_error = exc
            if connection is not None:
                try:
                    connection.close()
                except Exception:
                    pass
            if not is_transient_lock_error(exc):
                raise
            if attempt < len(READ_ONLY_RETRY_DELAYS_SECONDS):
                time_module.sleep(READ_ONLY_RETRY_DELAYS_SECONDS[attempt])
    raise ReadOnlyDatabaseBusyError(
        f"DB_LOCKED: recorder is still writing {database_path}; "
        f"retry window {READ_ONLY_MAX_WAIT_SECONDS:.2f}s exhausted"
    ) from last_error

