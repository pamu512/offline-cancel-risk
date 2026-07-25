import pytest
from datetime import datetime, timezone, timedelta

from offline_cancel_risk.domain.models import GpsPoint
from offline_cancel_risk.pipeline.window import resolve_gps_window


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
