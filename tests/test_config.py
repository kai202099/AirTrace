from __future__ import annotations

import json

import pytest

import airtrace.config as config


def test_repository_env_loading_is_deterministic_and_process_env_wins(tmp_path, monkeypatch):
    env_path = tmp_path / ".env"
    env_path.write_text("FIRMS_MAP_KEY=file-secret\n", encoding="utf-8")
    monkeypatch.delenv("FIRMS_MAP_KEY", raising=False)

    assert config.load_repository_env(env_path) == env_path
    assert config.get_firms_map_key() == "file-secret"

    monkeypatch.setenv("FIRMS_MAP_KEY", "process-secret")
    assert config.get_firms_map_key() == "process-secret"


def test_missing_firms_key_is_reported_without_value(tmp_path, monkeypatch):
    monkeypatch.delenv("FIRMS_MAP_KEY", raising=False)
    missing = tmp_path / "missing.env"
    monkeypatch.setattr(config, "REPOSITORY_ENV_PATH", missing)

    assert config.get_firms_map_key() == ""
    assert config.firms_map_key_configured() is False


def test_cors_origins_are_configurable_without_wildcard_default(monkeypatch):
    monkeypatch.setenv("AIRTRACE_CORS_ORIGINS", "https://demo.example, http://localhost:5173")
    assert config.get_cors_origins() == ["https://demo.example", "http://localhost:5173"]

    monkeypatch.setenv("AIRTRACE_CORS_ORIGINS", " , ")
    assert config.get_cors_origins() == list(config.DEFAULT_CORS_ORIGINS)


def test_firms_secret_never_appears_in_status_or_manifest(monkeypatch):
    pytest.importorskip("fastapi")
    from airtrace.api import app as api_module

    secret = "unit-test-firms-secret"
    monkeypatch.setenv("FIRMS_MAP_KEY", secret)
    payload = api_module._status_payload()

    assert payload["firms"] == {"configured": True}
    assert secret not in json.dumps(payload, default=str)
