"""Contract tests for the thin dashboard API.

These tests intentionally exercise the existing fixture artifacts rather than
re-running the analysis pipeline.
"""

from __future__ import annotations

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from airtrace.api import app as api_module  # noqa: E402
from airtrace.api.app import app  # noqa: E402


@pytest.fixture()
def client() -> TestClient:
    return TestClient(app)


def test_status_and_live_expose_recorder_health(client: TestClient) -> None:
    status = client.get("/api/status")
    live = client.get("/api/live")
    assert status.status_code == 200
    assert live.status_code == 200
    assert "freshness" in status.json()["pm25"]
    assert live.json()["sensors"]["counts"]["total"] >= 0


def test_runs_and_synthetic_run_detail_are_manifest_backed(client: TestClient) -> None:
    runs = client.get("/api/runs")
    detail = client.get("/api/runs/synthetic_full_event")
    assert runs.status_code == 200
    assert any(item["synthetic_validation"] for item in runs.json()["runs"])
    assert detail.status_code == 200
    assert detail.json()["manifest"]["produced_artifacts"]["manifest"] == "manifest.json"
    assert detail.json()["incidents"][0]["incident"]["trace"]["status"] == "TRACE_COMPLETE"
    incident = detail.json()["incidents"][0]
    assert {row["station_id"] for row in incident["membership"]} == {"a0", "a1"}
    assert incident["incident"]["trace"]["wind_diagnostics"]["map_arrows"][0]["u_east_mps"] == 1.0


def test_analyze_rejects_naive_timestamps(client: TestClient) -> None:
    response = client.post("/api/analyze", json={"start": "2026-09-03T05:44:00", "end": "2026-09-03T05:45:00", "analysis_zone": "core"})
    assert response.status_code == 422


def test_optional_evidence_status_is_preserved(client: TestClient) -> None:
    response = client.get("/api/incidents/AIRTRACE_20260902203000_001")
    assert response.status_code == 200
    evidence = response.json()["incident"]["evidence"]
    assert evidence["firms_status"] == "NOT_QUERIED_MISSING_KEY"
    assert evidence["cems_status"] == "CEMS_CONTEXT_UNAVAILABLE"


def test_db_lock_is_graceful(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(api_module, "_db_read", lambda path, operation: (None, "DB_LOCKED"))
    payload = api_module._sensor_state(api_module.datetime.now(api_module.UTC))
    assert payload["status"] == "DB_LOCKED"
    assert payload["sensors"] == []

