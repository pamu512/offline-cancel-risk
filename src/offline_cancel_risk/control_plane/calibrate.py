"""Score calibrator persistence (fit runner added in Task 2)."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class CalibratorStore:
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
                CREATE TABLE IF NOT EXISTS calibrators (
                  region_code TEXT NOT NULL,
                  city_code TEXT NOT NULL,
                  head TEXT NOT NULL,
                  method TEXT NOT NULL,
                  params_json TEXT NOT NULL,
                  ece REAL NOT NULL,
                  support INTEGER NOT NULL,
                  updated_at TEXT NOT NULL,
                  PRIMARY KEY (region_code, city_code, head)
                )
                """
            )
            conn.commit()

    def upsert(
        self,
        *,
        region_code: str,
        city_code: str,
        head: str,
        method: str,
        params: dict[str, Any],
        ece: float,
        support: int,
    ) -> None:
        region_code = (region_code or "").strip().upper()
        city_code = (city_code or "").strip().upper()
        updated = _utc_now_iso()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO calibrators(
                  region_code, city_code, head, method, params_json,
                  ece, support, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(region_code, city_code, head) DO UPDATE SET
                  method=excluded.method,
                  params_json=excluded.params_json,
                  ece=excluded.ece,
                  support=excluded.support,
                  updated_at=excluded.updated_at
                """,
                (
                    region_code,
                    city_code,
                    head,
                    method,
                    json.dumps(params),
                    float(ece),
                    int(support),
                    updated,
                ),
            )
            conn.commit()

    def get(self, region: str, city: str, head: str) -> dict[str, Any] | None:
        region_code = (region or "").strip().upper()
        city_code = (city or "").strip().upper()
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM calibrators
                WHERE region_code=? AND city_code=? AND head=?
                """,
                (region_code, city_code, head),
            ).fetchone()
        return self._row_to_dict(row) if row else None

    def list_market(self, region: str, city: str = "") -> list[dict[str, Any]]:
        region_code = (region or "").strip().upper()
        city_code = (city or "").strip().upper()
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM calibrators
                WHERE region_code=? AND city_code=?
                ORDER BY head
                """,
                (region_code, city_code),
            ).fetchall()
        return [self._row_to_dict(r) for r in rows]

    @staticmethod
    def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
        d = dict(row)
        d["params"] = json.loads(d.pop("params_json") or "{}")
        return d
