from offline_cancel_risk.features.replacement import evaluate_replacement


def test_valid_via_gps_path_only():
    v = evaluate_replacement(
        original_reached_destination=False,
        replacement_placed_delay_minutes=9999,
        route_similarity=0.0,
        has_replacement=True,
        policy={"max_place_delay_minutes": 180, "route_similarity_min": 0.7},
    )
    assert v.valid is True
    assert "gps" in v.paths_passed


def test_valid_via_timing_only():
    v = evaluate_replacement(
        original_reached_destination=True,
        replacement_placed_delay_minutes=30,
        route_similarity=0.0,
        has_replacement=True,
        policy={"max_place_delay_minutes": 180, "route_similarity_min": 0.7},
    )
    assert v.valid is True
    assert "timing" in v.paths_passed


def test_invalid_replacement_all_paths_fail():
    v = evaluate_replacement(
        original_reached_destination=True,
        replacement_placed_delay_minutes=9999,
        route_similarity=0.1,
        has_replacement=True,
        policy={"max_place_delay_minutes": 180, "route_similarity_min": 0.7},
    )
    assert v.valid is False
    assert "invalid_replacement" in v.reason_codes


def test_no_replacement_reason():
    v = evaluate_replacement(
        original_reached_destination=False,
        replacement_placed_delay_minutes=None,
        route_similarity=None,
        has_replacement=False,
        policy={"max_place_delay_minutes": 180, "route_similarity_min": 0.7},
    )
    assert v.valid is False
    assert "no_replacement" in v.reason_codes
