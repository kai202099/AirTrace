"""Durable artifact provenance markers shared by API and release fixtures."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


SYNTHETIC_VALIDATION = "synthetic_validation"


def is_synthetic_manifest(manifest: Mapping[str, Any]) -> bool:
    provenance = manifest.get("provenance")
    return isinstance(provenance, Mapping) and provenance.get("type") == SYNTHETIC_VALIDATION


__all__ = ["SYNTHETIC_VALIDATION", "is_synthetic_manifest"]
