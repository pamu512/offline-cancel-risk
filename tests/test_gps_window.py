import pytest
from datetime import datetime, timezone, timedelta

from offline_cancel_risk.adapters.gps import FakeGpsClient
from offline_cancel_risk.domain.models import GpsPoint
from offline_cancel_risk.pipeline.window import resolve_gps_window
from offline_cancel_risk.timeutil import parse_ts


POLICY = {
    "min_window_h": 3,
    "max_window_h": 24,
    "min_points": 20,
    "max_gap_minutes": 45,
}


def _point(ts: datetime) -> GpsPoint:
    return GpsPoint(1.0, 2.0, ts.isoformat(), 0.0)


@pytest.mark.asyncio
async def test_expands_when_too_few_points():
    async def loader(start, end):
        # return 5 points for 3h window, 40 points when window > 12h
        hours = (end - start).total_seconds() / 3600
        n = 5 if hours <= 3.1 else 40
        return [_point(start) for _ in range(n)]

    anchor = datetime(2024, 1, 1, 12, 0, tzinfo=timezone.utc)
    result = await resolve_gps_window(
        anchor_start=anchor - timedelta(hours=1),
        anchor_end=anchor,
        fetch=loader,
        policy=POLICY,
    )
    assert result.expanded is True
    assert result.point_count >= 20


@pytest.mark.asyncio
async def test_expands_on_large_gap():
    async def loader(start, end):
        hours = (end - start).total_seconds() / 3600
        points: list[GpsPoint] = []
        if hours <= 3.1:
            # Plenty of points, but a ~3h hole in the middle of the 3h window.
            t = start
            while t < start + timedelta(minutes=10):
                points.append(_point(t))
                t += timedelta(minutes=1)
            t = end - timedelta(minutes=10)
            while t <= end:
                points.append(_point(t))
                t += timedelta(minutes=1)
        else:
            # Dense coverage across the expanded window (no large gaps).
            t = start
            while t <= end:
                points.append(_point(t))
                t += timedelta(minutes=10)
        return points

    anchor = datetime(2024, 1, 1, 12, 0, tzinfo=timezone.utc)
    result = await resolve_gps_window(
        anchor_start=anchor - timedelta(hours=1),
        anchor_end=anchor,
        fetch=loader,
        policy=POLICY,
    )
    assert result.expanded is True
    assert result.point_count >= 20
    assert result.max_gap_minutes <= POLICY["max_gap_minutes"]


def test_parse_ts_z_and_offset_and_naive_are_utc_aware():
    z = parse_ts("2024-01-01T12:00:00Z")
    offset = parse_ts("2024-01-01T12:00:00+00:00")
    naive = parse_ts("2024-01-01 12:00:00")
    assert z.tzinfo is not None
    assert offset.tzinfo is not None
    assert naive.tzinfo is not None
    assert z == offset == naive
    assert z.utcoffset() == timedelta(0)


@pytest.mark.asyncio
async def test_fake_gps_client_accepts_iso_z_with_aware_window():
    """Regression: ISO-Z point ts vs aware start/end must not TypeError."""
    start = datetime(2024, 1, 1, 10, 0, tzinfo=timezone.utc)
    end = datetime(2024, 1, 1, 12, 0, tzinfo=timezone.utc)
    points = [
        GpsPoint(1.0, 2.0, "2024-01-01T11:00:00Z", 0.0),
        GpsPoint(1.0, 2.0, "2024-01-01T11:30:00+00:00", 0.0),
        GpsPoint(1.0, 2.0, "2024-01-01 09:00:00", 0.0),  # before window
    ]
    client = FakeGpsClient(points)
    got = await client.fetch_track(1, start, end)
    assert len(got) == 2

    async def fetch(s, e):
        return await client.fetch_track(1, s, e)

    result = await resolve_gps_window(
        anchor_start=start,
        anchor_end=end - timedelta(hours=1),
        fetch=fetch,
        policy={**POLICY, "min_points": 1, "max_gap_minutes": 999},
    )
    assert result.point_count >= 1
