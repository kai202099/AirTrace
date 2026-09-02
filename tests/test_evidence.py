from datetime import datetime, timezone

from airtrace.analysis.evidence import match_source_evidence
from airtrace.data.cems import cems_annotations, join_diagnostics, parse_cems_record
from airtrace.data.firms import FireDetection, group_detections, parse_acquisition_time
from airtrace.data.facilities import parse_facility_record


def trace(*, manual=False):
    return {
        "status": "TRACE_COMPLETE",
        "event": {"event_id": "e", "manual_diagnostic": manual, "note": "MANUAL DIAGNOSTIC — NOT DETECTED EVENT" if manual else ""},
        "receptor_seeds": [{"station_id": "R1", "seed_time_utc": "2026-09-02T13:26:00Z", "lat": 25.06, "lon": 121.45}],
        "source_evidence_grid": [
            {"center_lat": 25.06, "center_lon": 121.45, "source_evidence_score": 1.0, "receptor_support_fraction": 1.0, "receptor_support_count": 1},
            {"center_lat": 25.061, "center_lon": 121.45, "source_evidence_score": 0.5, "receptor_support_fraction": 1.0, "receptor_support_count": 1},
        ],
        "candidate_source_regions": [{"centroid": {"lat": 25.06, "lon": 121.45}}],
    }


def facility(ems, lat=25.06, lon=121.45):
    return {"ems_no": ems, "facility_name": ems, "lat": lat, "lon": lon, "industry_name": "test", "is_air_regulated": True}


def fire(lat=25.06, lon=121.45, when=None, satellite="NOAA-20"):
    return FireDetection(lat, lon, "2026-09-02", "1328", when or datetime(2026, 9, 2, 13, 28, tzinfo=timezone.utc), satellite, "VIIRS", "nominal", 42.0, 300.0, 280.0, 1.0, 1.0, "D")


def test_facility_at_source_ranks_above_distractor_and_uses_local_metrics():
    report = match_source_evidence(trace(), [facility("TRUE"), facility("DIST", 25.061, 121.452)])
    assert [row["ems_no"] for row in report["facility_matches"]] == ["TRUE", "DIST"]
    assert report["facility_matches"][0]["local_max_score"] == 1.0
    assert report["facility_matches"][0]["evidence_score"] > report["facility_matches"][1]["evidence_score"]


def test_no_facility_at_source_allows_no_strong_match():
    report = match_source_evidence(trace(), [facility("FAR", 25.08, 121.45)])
    assert report["facility_match_status"] == "NO STRONG FACILITY MATCH"
    assert report["facility_matches"][0]["evidence_score"] == 0.0


def test_cems_does_not_change_core_facility_score_and_is_annotation_only():
    cems = [{"cno": "TRUE", "ems_no": "TRUE", "measurement_time_utc": datetime(2026, 9, 2, 13, 30, tzinfo=timezone.utc), "pollutant_name": "SOx", "value": 10.0, "standard": 20.0, "unit": "ppm", "data_status": "valid", "data_code": "Code2-raw"}]
    without = match_source_evidence(trace(), [facility("TRUE")])
    with_cems = match_source_evidence(trace(), [facility("TRUE")], cems_context=cems)
    assert with_cems["facility_matches"][0]["evidence_score"] == without["facility_matches"][0]["evidence_score"]
    assert with_cems["cems_annotations"][0]["value_standard_ratio"] == 0.5
    assert with_cems["facility_matches"][0]["cems_available"] is True


def test_manual_trace_keeps_manual_label():
    report = match_source_evidence(trace(manual=True), [facility("TRUE")])
    assert report["trace_type"] == "MANUAL DIAGNOSTIC — NOT DETECTED EVENT"
    assert report["manual_diagnostic_notice"] == "MANUAL DIAGNOSTIC — NOT DETECTED EVENT"


def test_firms_grouping_merges_adjacent_cross_satellite_pixels():
    groups = group_detections([fire(), fire(25.0605, 121.4502, satellite="NOAA-21")])
    assert len(groups) == 1
    assert groups[0]["detection_count"] == 2
    assert set(groups[0]["satellites"]) == {"NOAA-20", "NOAA-21"}


def test_firms_temporal_offset_lowers_evidence():
    near = match_source_evidence(trace(), [], [fire()])
    far = match_source_evidence(trace(), [], [fire(when=datetime(2026, 9, 4, 13, 28, tzinfo=timezone.utc))])
    assert near["fire_matches"][0]["evidence_score"] > far["fire_matches"][0]["evidence_score"]


def test_firms_spatially_distant_hotspot_has_low_spatial_evidence():
    report = match_source_evidence(trace(), [], [fire(25.08, 121.47)])
    assert report["fire_matches"][0]["spatial_evidence"] == 0.0


def test_no_fire_returns_explicit_empty_result():
    report = match_source_evidence(trace(), [], [])
    assert report["fire_match_status"] == "NO FIRMS HOTSPOT DETECTED"


def test_firms_time_and_coordinate_parsing_are_utc():
    assert parse_acquisition_time("2026-09-02", "1328") == datetime(2026, 9, 2, 13, 28, tzinfo=timezone.utc)


def test_cems_join_is_exact_or_normalized_but_not_fuzzy():
    cems = [{"cno": " abc 123 "}, {"cno": "company-name"}]
    facilities = [{"ems_no": "ABC123"}, {"ems_no": "company-name-2"}]
    diagnostic = join_diagnostics(cems, facilities)
    assert diagnostic["exact_match_count"] == 0
    assert diagnostic["normalized_match_count"] == 1
    assert diagnostic["unmatched_cems_cno_count"] == 2


def test_cems_invalid_numeric_is_null_and_code_is_preserved():
    row = parse_cems_record({"CNO": "A", "M_Value": "--", "Std": "bad", "M_Date": "2026-09-02 21:30", "Code2": "CAL"})
    assert row.value is None and row.standard is None and row.data_code == "CAL"
    annotation = cems_annotations([row.__dict__], [{"ems_no": "A", "facility_name": "A"}], row.measurement_time_utc)
    assert annotation[0]["data_code"] == "CAL"
    assert annotation[0]["valid_for_supporting_context"] is False
