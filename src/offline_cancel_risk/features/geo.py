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


def parse_latlong(latlong_str: str) -> list[tuple[float, float]]:
    if not latlong_str or not latlong_str.strip():
        return []
    out: list[tuple[float, float]] = []
    for point in latlong_str.split(","):
        lat_s, lon_s = point.split("|")
        out.append((float(lat_s), float(lon_s)))
    return out
