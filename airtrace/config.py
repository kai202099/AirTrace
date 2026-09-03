"""Application configuration bootstrap.

Environment variables supplied by the process take precedence over the
repository-local ``.env``.  Secrets are intentionally exposed only to the
Python process and never included in API payloads or persisted artifacts.
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
REPOSITORY_ENV_PATH = REPOSITORY_ROOT / ".env"


def load_repository_env(dotenv_path: Path | None = None) -> Path:
    """Load the deterministic repository ``.env`` once per bootstrap path."""

    path = Path(dotenv_path or REPOSITORY_ENV_PATH)
    load_dotenv(dotenv_path=path, override=False)
    return path


def get_firms_map_key() -> str:
    """Return the process-local FIRMS key without logging or serializing it."""

    load_repository_env()
    return os.environ.get("FIRMS_MAP_KEY", "").strip()


def firms_map_key_configured() -> bool:
    """Return only whether a non-empty FIRMS key is available."""

    return bool(get_firms_map_key())


# Application imports are the bootstrap boundary for API and pipeline code.
load_repository_env()


__all__ = [
    "REPOSITORY_ENV_PATH",
    "REPOSITORY_ROOT",
    "firms_map_key_configured",
    "get_firms_map_key",
    "load_repository_env",
]
