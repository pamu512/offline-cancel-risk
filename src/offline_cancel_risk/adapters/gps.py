from __future__ import annotations

import csv
from datetime import datetime
from pathlib import Path
from typing import Protocol

import httpx

from offline_cancel_risk.domain.models import GpsPoint
from offline_cancel_risk.timeutil import parse_ts


def _optional_float(item: dict, *keys: str) -> float | None:
    for key in keys:
        if key in item and item[key] is not None and item[key] != "":
            return float(item[key])
    return None


def map_gps_json(items: list[dict]) -> list[GpsPoint]:
    """Map a JSON list of GPS records to GpsPoint. Keep path/schema mapping here."""
    out: list[GpsPoint] = []
    for item in items:
        speed = _optional_float(item, "speed_mps", "speed")
        heading = _optional_float(item, "heading_deg", "heading", "course")
        out.append(
            GpsPoint(
                lat=float(item["lat"]),
                lon=float(item["lon"]),
                ts=str(item["ts"]),
                speed_mps=speed,
                heading_deg=heading,
            )
        )
    return out


class GpsClient(Protocol):
    async def fetch_track(
        self, driver_id: int, start: datetime, end: datetime
    ) -> list[GpsPoint]: ...


class FakeGpsClient:
    def __init__(self, points: list[GpsPoint]) -> None:
        self._points = points

    async def fetch_track(
        self, driver_id: int, start: datetime, end: datetime
    ) -> list[GpsPoint]:
        del driver_id  # fake store is single-driver
        return [
            p
            for p in self._points
            if start <= parse_ts(p.ts) <= end
        ]


class CsvGpsClient:
    """Local-file GPS source: driver_id,lat,lon,ts[,speed_mps]. No network."""

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._rows: list[tuple[int, GpsPoint]] | None = None

    def _load(self) -> list[tuple[int, GpsPoint]]:
        if self._rows is not None:
            return self._rows
        rows: list[tuple[int, GpsPoint]] = []
        with self._path.open(newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                speed_raw = row.get("speed_mps")
                speed = (
                    None
                    if speed_raw is None or speed_raw == ""
                    else float(speed_raw)
                )
                heading_raw = row.get("heading_deg") or row.get("heading")
                heading = (
                    None
                    if heading_raw is None or heading_raw == ""
                    else float(heading_raw)
                )
                rows.append(
                    (
                        int(row["driver_id"]),
                        GpsPoint(
                            lat=float(row["lat"]),
                            lon=float(row["lon"]),
                            ts=row["ts"],
                            speed_mps=speed,
                            heading_deg=heading,
                        ),
                    )
                )
        self._rows = rows
        return rows

    async def fetch_track(
        self, driver_id: int, start: datetime, end: datetime
    ) -> list[GpsPoint]:
        return [
            point
            for did, point in self._load()
            if did == driver_id and start <= parse_ts(point.ts) <= end
        ]


class HttpGpsClient:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._client = client

    async def fetch_track(
        self, driver_id: int, start: datetime, end: datetime
    ) -> list[GpsPoint]:
        url = f"{self._base_url}/v1/drivers/{driver_id}/gps"
        params = {"start": start.isoformat(), "end": end.isoformat()}
        headers = {"X-API-Key": self._api_key}
        if self._client is not None:
            resp = await self._client.get(url, params=params, headers=headers)
            resp.raise_for_status()
            data = resp.json()
        else:
            async with httpx.AsyncClient() as client:
                resp = await client.get(url, params=params, headers=headers)
                resp.raise_for_status()
                data = resp.json()
        if not isinstance(data, list):
            raise ValueError("GPS response must be a JSON list")
        return map_gps_json(data)
