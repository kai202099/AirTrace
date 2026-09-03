"""Helpers for keeping serialized AirTrace artifacts portable and public-safe."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]

# These keys are used by reports and manifests for filesystem locations. Keep
# the list intentionally narrow so ordinary diagnostic strings are unchanged.
PATH_KEYS = frozenset({
    "path",
    "database_path",
    "region_config_path",
    "residual_csv_path",
    "trace_path",
    "facilities_db",
    "cems_db",
    "blind_replay_report",
    "known_events_config",
    "cache_path",
})


def public_path(value: str | Path) -> str:
    """Return a repository-relative POSIX path without exposing local paths."""

    raw = str(value)
    if "://" in raw:
        return raw
    candidate = Path(raw)
    if not candidate.is_absolute():
        return candidate.as_posix()
    try:
        return candidate.resolve().relative_to(REPOSITORY_ROOT.resolve()).as_posix()
    except ValueError:
        name = candidate.name or "unknown"
        return f"external/{name}"


def sanitize_public_paths(value: Any) -> Any:
    """Recursively sanitize known path-valued fields in a JSON-like value."""

    if isinstance(value, Mapping):
        return {
            str(key): public_path(item) if str(key) in PATH_KEYS and isinstance(item, (str, Path))
            else sanitize_public_paths(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [sanitize_public_paths(item) for item in value]
    if isinstance(value, tuple):
        return [sanitize_public_paths(item) for item in value]
    return value


__all__ = ["PATH_KEYS", "REPOSITORY_ROOT", "public_path", "sanitize_public_paths"]
