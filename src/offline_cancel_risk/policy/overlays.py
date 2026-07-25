"""Persist region/city policy overlays for ops tuning."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class PolicyOverlayStore:
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
                CREATE TABLE IF NOT EXISTS policy_overlays (
                  region_code TEXT NOT NULL,
                  city_code TEXT NOT NULL,
                  overlay_json TEXT NOT NULL,
                  updated_at TEXT NOT NULL,
                  PRIMARY KEY (region_code, city_code)
                )
                """
            )
            conn.commit()

    def get(self, region_code: str, city_code: str = "") -> dict[str, Any] | None:
        region_code = (region_code or "").strip().upper()
        city_code = (city_code or "").strip().upper()
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT overlay_json FROM policy_overlays
                WHERE region_code=? AND city_code=?
                """,
                (region_code, city_code),
            ).fetchone()
        if row is None:
            return None
        data = json.loads(row["overlay_json"])
        return data if isinstance(data, dict) else None

    def upsert(
        self, region_code: str, city_code: str, overlay: dict[str, Any]
    ) -> None:
        region_code = (region_code or "").strip().upper()
        city_code = (city_code or "").strip().upper()
        updated = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO policy_overlays(region_code, city_code, overlay_json, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(region_code, city_code) DO UPDATE SET
                  overlay_json=excluded.overlay_json,
                  updated_at=excluded.updated_at
                """,
                (region_code, city_code, json.dumps(overlay), updated),
            )
            conn.commit()

    def list_keys(self) -> list[dict[str, str]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT region_code, city_code, updated_at
                FROM policy_overlays
                ORDER BY region_code, city_code
                """
            ).fetchall()
        return [dict(r) for r in rows]

    def delete(self, region_code: str, city_code: str = "") -> bool:
        region_code = (region_code or "").strip().upper()
        city_code = (city_code or "").strip().upper()
        with self._connect() as conn:
            cur = conn.execute(
                "DELETE FROM policy_overlays WHERE region_code=? AND city_code=?",
                (region_code, city_code),
            )
            conn.commit()
            return cur.rowcount > 0
