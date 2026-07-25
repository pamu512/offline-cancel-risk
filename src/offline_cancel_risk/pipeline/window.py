from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Awaitable, Callable

from offline_cancel_risk.domain.models import GpsPoint

# ponytail: 5m post-cancel buffer; if policy grows a buffer key, read it there.
_SMALL_BUFFER = timedelta(minutes=5)


def _parse_ts(ts: str) -> datetime:
    try:
        return datetime.strptime(ts, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return datetime.fromisoformat(ts)


def _max_gap_minutes(points: list[GpsPoint], start: datetime, end: datetime) -> float:
    if not points:
        return (end - start).total_seconds() / 60.0
    times = sorted(_parse_ts(p.ts) for p in points)
    gaps = [(times[0] - start).total_seconds()]
    for a, b in zip(times, times[1:]):
        gaps.append((b - a).total_seconds())
    gaps.append((end - times[-1]).total_seconds())
    return max(gaps) / 60.0


@dataclass(frozen=True)
class GpsWindowResult:
    start: datetime
    end: datetime
    expanded: bool
    point_count: int
    max_gap_minutes: float
    points: list[GpsPoint]


async def resolve_gps_window(
    *,
    anchor_start: datetime,
    anchor_end: datetime,
    fetch: Callable[[datetime, datetime], Awaitable[list[GpsPoint]]],
    policy: dict[str, Any],
) -> GpsWindowResult:
    min_h = float(policy["min_window_h"])
    max_h = float(policy["max_window_h"])
    min_points = int(policy["min_points"])
    max_gap = float(policy["max_gap_minutes"])

    start = min(anchor_start, anchor_end - timedelta(hours=min_h))
    end = anchor_end + _SMALL_BUFFER

    points = await fetch(start, end)
    gap = _max_gap_minutes(points, start, end)
    needs_expand = len(points) < min_points or gap > max_gap

    if not needs_expand:
        return GpsWindowResult(
            start=start,
            end=end,
            expanded=False,
            point_count=len(points),
            max_gap_minutes=gap,
            points=points,
        )

    # Expand symmetrically toward max_window_h total span; stop there even if sparse.
    span = timedelta(hours=max_h)
    mid = start + (end - start) / 2
    start = mid - span / 2
    end = mid + span / 2
    points = await fetch(start, end)
    gap = _max_gap_minutes(points, start, end)
    return GpsWindowResult(
        start=start,
        end=end,
        expanded=True,
        point_count=len(points),
        max_gap_minutes=gap,
        points=points,
    )
