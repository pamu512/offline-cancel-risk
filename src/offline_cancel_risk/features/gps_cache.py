"""Assess-time GPS replay cache for DBSCAN market retune."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from offline_cancel_risk.domain.models import GpsPoint


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat().replace("+00:00", "Z")


def points_to_json(points: list[GpsPoint]) -> str:
    return json.dumps(
        [
            {
                "lat": p.lat,
                "lon": p.lon,
                "ts": p.ts,
                "speed_mps": p.speed_mps,
                "heading_deg": p.heading_deg,
            }
            for p in points
        ]
    )


def points_from_json(raw: str) -> list[GpsPoint]:
    data = json.loads(raw)
    out: list[GpsPoint] = []
    for item in data:
        out.append(
            GpsPoint(
                lat=float(item["lat"]),
                lon=float(item["lon"]),
                ts=str(item["ts"]),
                speed_mps=(
                    float(item["speed_mps"])
                    if item.get("speed_mps") is not None
                    else None
                ),
                heading_deg=(
                    float(item["heading_deg"])
                    if item.get("heading_deg") is not None
                    else None
                ),
            )
        )
    return out


class AssessGpsCache:
    def __init__(self, sqlite_path: Path | str) -> None:
        self._path = Path(sqlite_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._init()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS assess_gps_cache (
                  order_display_id TEXT NOT NULL,
                  assessment_generation INTEGER NOT NULL,
                  region_code TEXT NOT NULL,
                  city_code TEXT NOT NULL,
                  request_json TEXT NOT NULL,
                  points_json TEXT NOT NULL,
                  recorded_at TEXT NOT NULL,
                  PRIMARY KEY (order_display_id, assessment_generation)
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_gps_cache_market_ts
                ON assess_gps_cache(region_code, city_code, recorded_at)
                """
            )
            conn.commit()

    def put(
        self,
        *,
        order_display_id: str,
        assessment_generation: int,
        region_code: str,
        city_code: str,
        request: dict[str, Any],
        points: list[GpsPoint],
    ) -> bool:
        """Store non-empty tracks. Returns False if skipped (empty)."""
        if not points:
            return False
        oid = (order_display_id or "").strip()
        if not oid:
            return False
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO assess_gps_cache(
                  order_display_id, assessment_generation, region_code, city_code,
                  request_json, points_json, recorded_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(order_display_id, assessment_generation) DO UPDATE SET
                  region_code=excluded.region_code,
                  city_code=excluded.city_code,
                  request_json=excluded.request_json,
                  points_json=excluded.points_json,
                  recorded_at=excluded.recorded_at
                """,
                (
                    oid,
                    int(assessment_generation),
                    (region_code or "").strip().upper(),
                    (city_code or "").strip().upper(),
                    json.dumps(request),
                    points_to_json(points),
                    _iso(_utc_now()),
                ),
            )
            conn.commit()
        return True

    def get(
        self, order_display_id: str, assessment_generation: int
    ) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM assess_gps_cache
                WHERE order_display_id=? AND assessment_generation=?
                """,
                (order_display_id.strip(), int(assessment_generation)),
            ).fetchone()
        if row is None:
            return None
        return {
            "order_display_id": row["order_display_id"],
            "assessment_generation": int(row["assessment_generation"]),
            "region_code": row["region_code"],
            "city_code": row["city_code"],
            "request": json.loads(row["request_json"]),
            "points": points_from_json(row["points_json"]),
            "recorded_at": row["recorded_at"],
        }

    def latest_for_market(
        self, region_code: str, city_code: str = ""
    ) -> list[dict[str, Any]]:
        """Latest generation per order in market."""
        region = region_code.strip().upper()
        city = (city_code or "").strip().upper()
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT c.* FROM assess_gps_cache c
                INNER JOIN (
                  SELECT order_display_id, MAX(assessment_generation) AS g
                  FROM assess_gps_cache
                  WHERE region_code=? AND (? = '' OR city_code=?)
                  GROUP BY order_display_id
                ) t
                ON c.order_display_id = t.order_display_id
                 AND c.assessment_generation = t.g
                """,
                (region, city, city),
            ).fetchall()
        out: list[dict[str, Any]] = []
        for row in rows:
            out.append(
                {
                    "order_display_id": row["order_display_id"],
                    "assessment_generation": int(row["assessment_generation"]),
                    "region_code": row["region_code"],
                    "city_code": row["city_code"],
                    "request": json.loads(row["request_json"]),
                    "points": points_from_json(row["points_json"]),
                    "recorded_at": row["recorded_at"],
                }
            )
        return out

    def prune(self, retention_days: int) -> int:
        if retention_days <= 0:
            return 0
        cutoff = _utc_now() - timedelta(days=int(retention_days))
        with self._connect() as conn:
            cur = conn.execute(
                "DELETE FROM assess_gps_cache WHERE recorded_at < ?",
                (_iso(cutoff),),
            )
            conn.commit()
            return int(cur.rowcount)
