"""Supply/demand forecast store for market operating points."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _norm(code: str | None) -> str:
    return (code or "").strip().upper()


class SupplyForecastStore:
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
                CREATE TABLE IF NOT EXISTS supply_forecast (
                  region_code TEXT NOT NULL,
                  city_code TEXT NOT NULL,
                  period_start TEXT NOT NULL,
                  period_end TEXT NOT NULL,
                  forecast_supply REAL NOT NULL,
                  forecast_demand REAL NOT NULL,
                  source TEXT NOT NULL,
                  ingested_at TEXT NOT NULL,
                  PRIMARY KEY (region_code, city_code, period_start, period_end)
                )
                """
            )
            conn.commit()

    def upsert(self, rows: list[dict[str, Any]]) -> int:
        ingested = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        n = 0
        with self._connect() as conn:
            for row in rows:
                supply = float(row["forecast_supply"])
                demand = float(row["forecast_demand"])
                if supply < 0 or demand <= 0:
                    raise ValueError("forecast_supply >= 0 and forecast_demand > 0 required")
                conn.execute(
                    """
                    INSERT INTO supply_forecast(
                      region_code, city_code, period_start, period_end,
                      forecast_supply, forecast_demand, source, ingested_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(region_code, city_code, period_start, period_end)
                    DO UPDATE SET
                      forecast_supply=excluded.forecast_supply,
                      forecast_demand=excluded.forecast_demand,
                      source=excluded.source,
                      ingested_at=excluded.ingested_at
                    """,
                    (
                        _norm(row.get("region_code")),
                        _norm(row.get("city_code")),
                        str(row["period_start"]),
                        str(row["period_end"]),
                        supply,
                        demand,
                        str(row.get("source") or "unknown"),
                        ingested,
                    ),
                )
                n += 1
            conn.commit()
        return n

    def active(
        self, region_code: str, city_code: str, at_ts: str
    ) -> dict[str, Any] | None:
        region = _norm(region_code)
        city = _norm(city_code)
        with self._connect() as conn:
            if city:
                row = conn.execute(
                    """
                    SELECT * FROM supply_forecast
                    WHERE region_code=? AND city_code=?
                      AND period_start <= ? AND period_end > ?
                    ORDER BY ingested_at DESC LIMIT 1
                    """,
                    (region, city, at_ts, at_ts),
                ).fetchone()
                if row is not None:
                    return dict(row)
            row = conn.execute(
                """
                SELECT * FROM supply_forecast
                WHERE region_code=? AND city_code=''
                  AND period_start <= ? AND period_end > ?
                ORDER BY ingested_at DESC LIMIT 1
                """,
                (region, at_ts, at_ts),
            ).fetchone()
        return dict(row) if row is not None else None

    def list_all(self, *, limit: int = 100) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM supply_forecast
                ORDER BY ingested_at DESC LIMIT ?
                """,
                (int(limit),),
            ).fetchall()
        return [dict(r) for r in rows]
