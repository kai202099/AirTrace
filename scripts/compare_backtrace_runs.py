#!/usr/bin/env python3
"""Compare strict and quantized Backtrace evidence surfaces."""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reference", type=Path, default=ROOT / "reports" / "performance" / "strict_final" / "latest_source_trace.json")
    parser.add_argument("--optimized", type=Path, default=ROOT / "reports" / "performance" / "optimized_final" / "latest_source_trace.json")
    parser.add_argument("--output", type=Path, default=ROOT / "reports" / "performance" / "backtrace_accuracy_comparison.json")
    return parser.parse_args()


def haversine_km(first: tuple[float, float], second: tuple[float, float]) -> float:
    lon1, lat1 = map(math.radians, first)
    lon2, lat2 = map(math.radians, second)
    dlon, dlat = lon2 - lon1, lat2 - lat1
    a = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 6371.0088 * 2 * math.asin(min(1.0, math.sqrt(a)))


def load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def surface(payload: dict) -> dict[tuple[int, int], float]:
    return {tuple(item["cell_key"]): float(item["source_evidence_score"]) for item in payload.get("source_evidence_grid", [])}


def compare(reference: dict, optimized: dict) -> dict:
    ref_surface, opt_surface = surface(reference), surface(optimized)
    keys = sorted(set(ref_surface) | set(opt_surface))
    ref_values = [ref_surface.get(key, 0.0) for key in keys]
    opt_values = [opt_surface.get(key, 0.0) for key in keys]
    ref_mean = sum(ref_values) / max(1, len(ref_values))
    opt_mean = sum(opt_values) / max(1, len(opt_values))
    numerator = sum((a - ref_mean) * (b - opt_mean) for a, b in zip(ref_values, opt_values))
    denom = math.sqrt(sum((a - ref_mean) ** 2 for a in ref_values) * sum((b - opt_mean) ** 2 for b in opt_values))
    ref_top = reference.get("candidate_source_regions", [{}])[0] if reference.get("candidate_source_regions") else None
    opt_top = optimized.get("candidate_source_regions", [{}])[0] if optimized.get("candidate_source_regions") else None
    ref_peak = max(ref_surface, key=ref_surface.get) if ref_surface else None
    opt_peak = max(opt_surface, key=opt_surface.get) if opt_surface else None
    centroid_shift = None
    if ref_top and opt_top:
        centroid_shift = haversine_km(
            (ref_top["centroid"]["lon"], ref_top["centroid"]["lat"]),
            (opt_top["centroid"]["lon"], opt_top["centroid"]["lat"]),
        )
    ref_stats = reference.get("particle_stats", {})
    opt_stats = optimized.get("particle_stats", {})
    return {
        "reference_status": reference.get("status"),
        "optimized_status": optimized.get("status"),
        "top_region_centroid_shift_km": None if centroid_shift is None else round(centroid_shift, 9),
        "top_region_reference": None if ref_top is None else ref_top.get("centroid"),
        "top_region_optimized": None if opt_top is None else opt_top.get("centroid"),
        "evidence_grid_pearson_correlation": None if denom == 0 else round(numerator / denom, 9),
        "peak_cell_reference": ref_peak,
        "peak_cell_optimized": opt_peak,
        "peak_cell_same": ref_peak == opt_peak,
        "occupied_evidence_area_km2": {
            "reference": round(len(ref_surface) * 250.0 * 250.0 / 1_000_000, 6),
            "optimized": round(len(opt_surface) * 250.0 * 250.0 / 1_000_000, 6),
        },
        "termination_counts": {"reference": ref_stats.get("termination_counts", {}), "optimized": opt_stats.get("termination_counts", {})},
        "wind_estimate_requests": {"reference": reference.get("wind_diagnostics", {}).get("estimate_calls"), "optimized": optimized.get("wind_diagnostics", {}).get("estimate_calls")},
        "acceptance_checks": {
            "centroid_shift_preferably_below_0_5_km": centroid_shift is not None and centroid_shift < 0.5,
            "top_peak_cell_unchanged": ref_peak == opt_peak,
            "evidence_correlation_high": denom != 0 and numerator / denom >= 0.95,
        },
    }


def main() -> int:
    args = parse_args()
    result = {
        "schema_version": 1,
        "reference": str(args.reference),
        "optimized": str(args.optimized),
        "comparison": compare(load(args.reference), load(args.optimized)),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result["comparison"], ensure_ascii=False, indent=2))
    print(f"Comparison: {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
