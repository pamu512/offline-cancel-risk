from dataclasses import dataclass


@dataclass(frozen=True)
class GpsPoint:
    lat: float
    lon: float
    ts: str  # ISO-8601 or "%Y-%m-%d %H:%M:%S"
    speed_mps: float | None = None
