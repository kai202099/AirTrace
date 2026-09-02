#!/usr/bin/env python3
"""Deterministic Backtrace v1.1 production-path performance benchmark."""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import psutil

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from airtrace.analysis.backtrace import BacktraceConfig, trace_event  # noqa: E402

ANCHOR = (25.068571, 121.450769)
TRACE_TIME = "2026-09-02T21:30:00+08:00"
OFFSETS = (
    (0.0000, 0.0000), (0.0030, 0.0000), (-0.0030, 0.0000),
    (0.0000, 0.0040), (0.0000, -0.0040), (0.0030, 0.0040),
    (-0.0030, -0.0040), (0.0040, -0.0030), (-0.0040, 0.0030),
    (0.0040, 0.0040),
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spatial-m", type=float, choices=(250.0, 500.0), default=500.0)
    parser.add_argument("--output", type=Path, default=ROOT / "reports" / "performance" / "backtrace_benchmark.json")
    return parser.parse_args()


class _MemorySampler:
    def __init__(self) -> None:
        self.stop = threading.Event()
        self.peak_working_set = 0
        self.thread = threading.Thread(target=self._run, daemon=True)

    def _working_set(self) -> int:
        return int(psutil.Process(os.getpid()).memory_info().rss)

    def _run(self) -> None:
        while not self.stop.is_set():
            self.peak_working_set = max(self.peak_working_set, self._working_set())
            self.stop.wait(0.05)

    def __enter__(self) -> "_MemorySampler":
        self.thread.start()
        return self

    def __exit__(self, *_: object) -> None:
        self.stop.set()
        self.thread.join(timeout=1)
        self.peak_working_set = max(self.peak_working_set, self._working_set())


def benchmark(receptor_count: int, spatial_m: float) -> dict[str, object]:
    lat, lon = ANCHOR
    memberships = []
    for index, (dlat, dlon) in enumerate(OFFSETS[:receptor_count]):
        memberships.append({
            "event_id": "performance-benchmark",
            "time_bin": TRACE_TIME,
            "station_id": f"benchmark-receptor-{index:02d}",
            "role": "seed" if index == 0 else "support",
            "anomaly_score": 8.0,
            "lat": lat + dlat,
            "lon": lon + dlon,
        })
    event = {
        "event_id": "performance-benchmark",
        "latest_centroid": {"lat": lat, "lon": lon},
        "manual_diagnostic": True,
    }
    config = BacktraceConfig(
        database_path=ROOT / "data" / "weather.duckdb",
        region_config_path=ROOT / "config" / "pilot_region.json",
        residual_csv_path=ROOT / "reports" / "wind_validation" / "wind_validation_samples.csv",
        particles_per_receptor=200,
        random_seed=42,
        dt_seconds=60,
        maximum_backtrace_minutes=60,
        grid_cell_m=250.0,
        wind_cache_enabled=True,
        wind_cache_spatial_m=spatial_m,
        wind_cache_temporal_seconds=60,
        wind_cache_quantized=True,
        profile=True,
    )
    with _MemorySampler() as memory:
        wall_started = time.perf_counter()
        result = trace_event(event, memberships, config)
        wall_seconds = time.perf_counter() - wall_started
    profile = result.pop("_performance_profile")
    profile["wall_runtime_seconds"] = round(wall_seconds, 6)
    profile["peak_working_set_mb"] = round(memory.peak_working_set / 1_000_000, 3)
    profile["status"] = result["status"]
    profile["termination_counts"] = result["particle_stats"]["termination_counts"]
    return profile


def main() -> int:
    args = parse_args()
    output = {
        "schema_version": 1,
        "generated_at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "trace": {"anchor": ANCHOR, "time": TRACE_TIME, "particles_per_receptor": 200, "steps": 60, "wind_cache_spatial_m": args.spatial_m},
        "benchmarks": {str(count): benchmark(count, args.spatial_m) for count in (1, 5, 10)},
        "notes": [
            "Production airtrace.analysis.wind path backed by read-only data/weather.duckdb.",
            "Peak memory is sampled Windows process working set for one trace.",
            "Synthetic receptor coordinates are only a deterministic load shape; this is not source attribution.",
        ],
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    for count, profile in output["benchmarks"].items():
        print(f"{count} receptors: {profile['wall_runtime_seconds']:.3f}s, {profile['particle_steps_per_second']:.1f} particle-steps/s, cache hit {profile['wind_cache']['hit_rate']:.3%}, DB queries {profile['db_query_count']}")
    print(f"Benchmark: {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
