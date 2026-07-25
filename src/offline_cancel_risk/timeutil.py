from __future__ import annotations

from datetime import datetime, timezone


def parse_ts(ts: str) -> datetime:
    """Parse a timestamp string to timezone-aware UTC.

    Accepts ``Z``, ``+00:00`` (and other offsets), and naive datetimes
    (assumed UTC). Space-separated ``YYYY-MM-DD HH:MM:SS`` is supported.
    """
    s = ts.strip()
    if s.endswith(("Z", "z")):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.strptime(s, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)
