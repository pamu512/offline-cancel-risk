"""Persist per-device integrity EWMA and last-seen linkage."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class DeviceIntegrityStore:
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
                CREATE TABLE IF NOT EXISTS device_integrity (
                  device_id TEXT PRIMARY KEY,
                  ewma_risk REAL NOT NULL,
                  last_instant_risk REAL NOT NULL,
                  last_flags_json TEXT NOT NULL,
                  last_driver_id INTEGER,
                  last_user_id INTEGER,
                  sightings INTEGER NOT NULL,
                  updated_at TEXT NOT NULL
                )
                """
            )
            conn.commit()

    def get(self, device_id: str) -> dict[str, Any] | None:
        did = (device_id or "").strip()
        if not did:
            return None
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM device_integrity WHERE device_id=?", (did,)
            ).fetchone()
        if row is None:
            return None
        d = dict(row)
        d["last_flags"] = json.loads(d.pop("last_flags_json") or "{}")
        return d

    def upsert(
        self,
        *,
        device_id: str,
        ewma_risk: float,
        instant_risk: float,
        flags: dict[str, Any],
        driver_id: int | None,
        user_id: int | None,
    ) -> dict[str, Any]:
        did = (device_id or "").strip()
        if not did:
            raise ValueError("device_id required")
        prev = self.get(did)
        sightings = 1 + (int(prev["sightings"]) if prev else 0)
        row = {
            "device_id": did,
            "ewma_risk": float(ewma_risk),
            "last_instant_risk": float(instant_risk),
            "last_flags": dict(flags),
            "last_driver_id": driver_id,
            "last_user_id": user_id,
            "sightings": sightings,
            "updated_at": _utc_now_iso(),
        }
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO device_integrity(
                  device_id, ewma_risk, last_instant_risk, last_flags_json,
                  last_driver_id, last_user_id, sightings, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(device_id) DO UPDATE SET
                  ewma_risk=excluded.ewma_risk,
                  last_instant_risk=excluded.last_instant_risk,
                  last_flags_json=excluded.last_flags_json,
                  last_driver_id=excluded.last_driver_id,
                  last_user_id=excluded.last_user_id,
                  sightings=excluded.sightings,
                  updated_at=excluded.updated_at
                """,
                (
                    row["device_id"],
                    row["ewma_risk"],
                    row["last_instant_risk"],
                    json.dumps(row["last_flags"]),
                    row["last_driver_id"],
                    row["last_user_id"],
                    row["sightings"],
                    row["updated_at"],
                ),
            )
            conn.commit()
        return row
