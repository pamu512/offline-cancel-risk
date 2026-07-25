from offline_cancel_risk.features.geo import haversine, parse_latlong


def test_haversine_zero():
    assert haversine(1.0, 2.0, 1.0, 2.0) == 0.0


def test_haversine_known_distance_approx():
    # ~111.2km per degree latitude
    d = haversine(0.0, 0.0, 1.0, 0.0)
    assert 110_000 < d < 112_500


def test_parse_latlong():
    assert parse_latlong("1.0|2.0,3.0|4.0") == [(1.0, 2.0), (3.0, 4.0)]
