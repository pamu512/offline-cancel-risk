import numpy as np


def haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371.0
    dlat = np.radians(lat2 - lat1)
    dlon = np.radians(lon2 - lon1)
    a = (
        np.sin(dlat / 2) ** 2
        + np.cos(np.radians(lat1)) * np.cos(np.radians(lat2)) * np.sin(dlon / 2) ** 2
    )
    return float(r * 2 * np.arctan2(np.sqrt(a), np.sqrt(1 - a)) * 1000)


def bearing_deg(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Initial bearing from point 1 → point 2, degrees [0, 360)."""
    φ1, φ2 = np.radians(lat1), np.radians(lat2)
    Δλ = np.radians(lon2 - lon1)
    y = np.sin(Δλ) * np.cos(φ2)
    x = np.cos(φ1) * np.sin(φ2) - np.sin(φ1) * np.cos(φ2) * np.cos(Δλ)
    θ = np.degrees(np.arctan2(y, x))
    return float((θ + 360.0) % 360.0)


def heading_error_deg(heading_deg: float, target_bearing_deg: float) -> float:
    """Smallest absolute angular difference in degrees [0, 180]."""
    d = abs(float(heading_deg) - float(target_bearing_deg)) % 360.0
    return float(min(d, 360.0 - d))


def parse_latlong(latlong_str: str) -> list[tuple[float, float]]:
    if not latlong_str or not latlong_str.strip():
        return []
    out: list[tuple[float, float]] = []
    for point in latlong_str.split(","):
        lat_s, lon_s = point.split("|")
        out.append((float(lat_s), float(lon_s)))
    return out
