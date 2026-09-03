from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient

from airtrace.api import app as api_module
from airtrace.api.app import app
from airtrace.provenance import is_synthetic_manifest


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "fixtures" / "demo" / "synthetic_full_event"
LOCAL_PATH = re.compile(
    r"(?<![A-Za-z0-9])[A-Za-z]:[\\/](?![\\/])(?=[^\\/\s])"
    r"|(?:^|[\\/])Users[\\/]|(?:^|[\\/])home[\\/]",
    re.IGNORECASE,
)


def test_curated_fixture_has_no_local_path_leakage() -> None:
    for path in FIXTURE.rglob("*"):
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8")
        assert not LOCAL_PATH.search(text), path
        assert str(ROOT) not in text, path


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (r"C:\Users\Example\AirTrace", True),
        ("C:/Users/Example/AirTrace", True),
        ("https://example.com", False),
        ("http://example.com", False),
        ("arbitrary text containing s:/", False),
    ],
)
def test_windows_absolute_path_detection(value: str, expected: bool) -> None:
    assert bool(LOCAL_PATH.search(value)) is expected


def test_synthetic_provenance_survives_fixture_directory_rename(tmp_path: Path) -> None:
    manifest = json.loads((FIXTURE / "manifest.json").read_text(encoding="utf-8"))
    assert is_synthetic_manifest(manifest)

    renamed = tmp_path / "renamed-demo"
    renamed.mkdir()
    shutil.copy2(FIXTURE / "analysis_summary.json", renamed / "analysis_summary.json")
    index = api_module._run_index(manifest, renamed)

    assert index["synthetic_validation"] is True


def test_api_manifest_has_public_paths_and_synthetic_provenance() -> None:
    response = TestClient(app).get("/api/runs/synthetic_full_event")
    assert response.status_code == 200
    payload = response.json()
    manifest_text = json.dumps(payload["manifest"], ensure_ascii=False)
    assert not LOCAL_PATH.search(manifest_text)
    assert payload["manifest"]["provenance"]["type"] == "synthetic_validation"
    assert payload["manifest"]["produced_artifacts"]["incidents_detail"][0]["incident"].count("\\") == 0
