#!/usr/bin/env python3
"""Deterministic known-source validation for Backward Particle Tracing v1."""

from __future__ import annotations

import json
import math
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from airtrace.analysis.backtrace import BacktraceConfig, trace_event  # noqa: E402

UTC = timezone.utc
T0 = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
SOURCE = (25.060, 121.450)


class SyntheticWind:
    def __init__(self, residual_scale: float = 0.0):
        self.residual_scale = residual_scale

    def __call__(self, lat, lon, at):
        # Eastward wind: receivers are east of the source and observed later.
        return {"u_east_mps": 4.0, "v_north_mps": 0.0, "nearest_station_km": 1.0, "quality_category": "GOOD"}


def make_case():
    memberships = []
    # 4 m/s for 15 minutes = 3.6 km.  Receptors are downwind of S.
    for index, (lat_offset, minutes) in enumerate(((0.0001, 15), (0.0002, 15), (0.00015, 16))):
        at = T0 + timedelta(minutes=minutes)
        memberships.append({"event_id": "synthetic", "time_bin": at.isoformat().replace("+00:00", "Z"), "station_id": f"r{index}", "role": "seed", "anomaly_score": 8.0, "lat": SOURCE[0] + lat_offset, "lon": SOURCE[1] + 0.036})
    return {"event_id": "synthetic", "latest_centroid": {"lat": SOURCE[0], "lon": SOURCE[1]}}, memberships


def run(scale: float) -> dict:
    event, memberships = make_case()
    result = trace_event(event, memberships, BacktraceConfig(
        particles_per_receptor=40, maximum_backtrace_minutes=20, random_seed=42,
        residual_csv_path=(ROOT / "does-not-exist.csv" if scale == 0 else ROOT / "reports" / "wind_validation" / "wind_validation_samples.csv"), wind_getter=SyntheticWind(scale),
        peak_threshold_fraction=0.5,
    ))
    region = result["candidate_source_regions"][0] if result["candidate_source_regions"] else None
    centroid_error = None if region is None else math.hypot((region["centroid"]["lat"] - SOURCE[0]) * 110.54, (region["centroid"]["lon"] - SOURCE[1]) * 111.32)
    covered = bool(region and region["bounds"]["south"] <= SOURCE[0] <= region["bounds"]["north"] and region["bounds"]["west"] <= SOURCE[1] <= region["bounds"]["east"])
    grid = result["source_evidence_grid"]
    # Localization error is distance to the reported region (zero means S is
    # covered); centroid error is retained because a corridor centroid alone
    # is not a complete localization metric.
    return {"residual_mode": "zero" if scale == 0 else "empirical_like", "source_covered_by_top_region": covered, "localization_error_km": 0.0 if covered else centroid_error, "centroid_error_km": None if centroid_error is None else round(centroid_error, 6), "region_area_km2": None if region is None else region["area_km2"], "occupied_evidence_cell_count": len(grid), "occupied_evidence_area_km2": round(len(grid) * 250.0 * 250.0 / 1_000_000, 6), "regions": result["candidate_source_regions"], "particle_stats": result["particle_stats"]}


def main() -> int:
    output = {"schema_version": 1, "source": SOURCE, "zero_residual": run(0.0), "realistic_residual": run(1.0), "note": "Synthetic only; no data was written to recorder databases."}
    path = ROOT / "reports" / "source_trace" / "synthetic_validation.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(output, ensure_ascii=False, indent=2))
    print(f"Output: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
