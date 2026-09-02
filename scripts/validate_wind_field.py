#!/usr/bin/env python3
"""Run Wind Field Validation v1 against the read-only weather database."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from airtrace.analysis.anomaly import load_pilot_config  # noqa: E402
from airtrace.analysis.wind import DEFAULT_DATABASE, WindConfig  # noqa: E402
from airtrace.analysis.wind_validation import (  # noqa: E402
    markdown_summary,
    run_validation,
    write_samples_csv,
    write_validation_map,
)

DEFAULT_CONFIG = ROOT / "config" / "pilot_region.json"
DEFAULT_OUTPUT_DIR = ROOT / "reports" / "wind_validation"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--preferred-radius-km", type=float, default=15.0)
    parser.add_argument("--maximum-radius-km", type=float, default=30.0)
    parser.add_argument("--minimum-stations", type=int, default=3)
    parser.add_argument("--maximum-stations", type=int, default=8)
    parser.add_argument("--maximum-temporal-distance-minutes", type=float, default=15.0)
    parser.add_argument("--idw-power", type=float, default=2.0)
    return parser.parse_args()


def print_report(payload: dict, output_dir: Path) -> None:
    summary = payload["summary"]
    vector = summary["vector"]
    speed = summary["speed"]
    direction = summary["direction"]
    proxy = summary["core_center"]["empirical_error_proxy"]
    print("AirTrace Wind Field Validation v1")
    print(summary["validation_status"])
    print(f"Validation span: {summary['validation_span_utc']['start']} → {summary['validation_span_utc']['end']}")
    print(f"Stations evaluated: {summary['station_count_evaluated']} (within 30 km: {summary['station_count_within_30km']})")
    print(f"Validation samples: {summary['sample_count']}")
    print(f"Valid prediction coverage: {summary['valid_prediction_count']} / {summary['sample_count']} ({summary['prediction_coverage_pct']:.2f}%)")
    print(f"Vector median / RMSE / P90: {vector['median_mps']} / {vector['rmse_mps']} / {vector['p90_mps']} m/s")
    print(f"Speed MAE / P90: {speed['mae_mps']} / {speed['p90_mps']} m/s")
    print(f"Direction median / P90: {direction['median_deg']} / {direction['p90_deg']}° (n={direction['count']})")
    print("Current production diagnostic: IDW power=2, preferred radius=15 km, maximum radius=30 km")
    for item in summary["parameter_diagnostic"]:
        if item["idw_power"] == 2.0 and item["preferred_radius_km"] == 15.0:
            print(f"  current row: vector RMSE={item['vector_rmse_mps']} m/s, median={item['vector_median_error_mps']} m/s, direction median={item['direction_median_error_deg']}°, coverage={item['valid_prediction_coverage_pct']:.2f}%")
            break
    print(f"Quality diagnostic: {summary['quality_diagnostic']['note']}")
    print(f"Core-center empirical error proxy: n={proxy['sample_count']}, vector median/P90={proxy['metrics']['vector']['median_mps']}/{proxy['metrics']['vector']['p90_mps']} m/s, direction median/P90={proxy['metrics']['direction']['median_deg']}/{proxy['metrics']['direction']['p90_deg']}°")
    print(f"Samples CSV: {output_dir / 'wind_validation_samples.csv'}")
    print(f"Summary JSON: {output_dir / 'wind_validation_summary.json'}")
    print(f"Map: {output_dir / 'latest_wind_validation_map.html'}")
    print(f"README: {output_dir / 'README.md'}")


def main() -> int:
    args = parse_args()
    try:
        pilot = load_pilot_config(args.config)
        core = pilot["core_bbox"]
        center_lat = (float(core["south"]) + float(core["north"])) / 2.0
        center_lon = (float(core["west"]) + float(core["east"])) / 2.0
        config = WindConfig(
            preferred_radius_km=args.preferred_radius_km,
            maximum_radius_km=args.maximum_radius_km,
            minimum_stations=args.minimum_stations,
            maximum_stations=args.maximum_stations,
            maximum_temporal_distance_minutes=args.maximum_temporal_distance_minutes,
            idw_power=args.idw_power,
        )
        payload = run_validation(args.database, center_lat=center_lat, center_lon=center_lon, base_config=config)
        args.output_dir.mkdir(parents=True, exist_ok=True)
        write_samples_csv(args.output_dir / "wind_validation_samples.csv", payload["samples"])
        write_validation_map(args.output_dir / "latest_wind_validation_map.html", payload, pilot)
        (args.output_dir / "wind_validation_summary.json").write_text(
            json.dumps({key: value for key, value in payload.items() if key != "samples"}, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
            encoding="utf-8",
        )
        (args.output_dir / "README.md").write_text(markdown_summary(payload), encoding="utf-8")
        print_report(payload, args.output_dir)
    except (OSError, KeyError, ValueError, RuntimeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
