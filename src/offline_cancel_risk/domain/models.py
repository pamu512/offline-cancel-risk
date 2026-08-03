from dataclasses import dataclass


@dataclass(frozen=True)
class GpsPoint:
    lat: float
    lon: float
    ts: str  # ISO-8601 or "%Y-%m-%d %H:%M:%S"
    speed_mps: float | None = None
    # Device course over ground, degrees [0, 360). None if GPS provider omitted it.
    heading_deg: float | None = None
