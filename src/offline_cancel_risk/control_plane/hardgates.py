"""Local ops enforcement volume hardgates + clawback TTL."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


def _norm(code: str | None) -> str:
    return (code or "").strip().upper()


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_ts(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


class EnforcementHardgateStore:
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
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS enforcement_hardgates (
                  region_code TEXT NOT NULL,
                  city_code TEXT NOT NULL,
                  window TEXT NOT NULL,
                  max_enforcements INTEGER NOT NULL,
                  heads_json TEXT NOT NULL,
                  updated_at TEXT NOT NULL,
                  actor TEXT NOT NULL,
                  PRIMARY KEY (region_code, city_code, window)
                );
                CREATE TABLE IF NOT EXISTS clawback_state (
                  region_code TEXT NOT NULL,
                  city_code TEXT NOT NULL,
                  until_ts TEXT NOT NULL,
                  reason TEXT NOT NULL,
                  updated_at TEXT NOT NULL,
                  PRIMARY KEY (region_code, city_code)
                );
                """
            )
            conn.commit()

    def upsert(
        self,
        region_code: str,
        city_code: str,
        *,
        window: str,
        max_enforcements: int,
        heads: list[str] | None = None,
        actor: str = "ops",
    ) -> None:
        if window not in {"hour", "day", "week"}:
            raise ValueError("window must be hour|day|week")
        if max_enforcements < 0:
            raise ValueError("max_enforcements must be >= 0")
        updated = _utc_now().isoformat().replace("+00:00", "Z")
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO enforcement_hardgates(
                  region_code, city_code, window, max_enforcements,
                  heads_json, updated_at, actor
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(region_code, city_code, window) DO UPDATE SET
                  max_enforcements=excluded.max_enforcements,
                  heads_json=excluded.heads_json,
                  updated_at=excluded.updated_at,
                  actor=excluded.actor
                """,
                (
                    _norm(region_code),
                    _norm(city_code),
                    window,
                    int(max_enforcements),
                    json.dumps(heads if heads is not None else ["*"]),
                    updated,
                    actor,
                ),
            )
            conn.commit()

    def get(self, region_code: str, city_code: str) -> dict[str, dict[str, Any]]:
        region = _norm(region_code)
        city = _norm(city_code)
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM enforcement_hardgates
                WHERE region_code=? AND city_code=?
                """,
                (region, city),
            ).fetchall()
        out: dict[str, dict[str, Any]] = {}
        for row in rows:
            item = dict(row)
            item["heads"] = json.loads(item.pop("heads_json"))
            out[str(item["window"])] = item
        return out

    def record_clawback(
        self,
        region_code: str,
        city_code: str,
        *,
        ttl_minutes: int,
        reason: str,
    ) -> dict[str, str]:
        until = (_utc_now() + timedelta(minutes=int(ttl_minutes))).isoformat().replace(
            "+00:00", "Z"
        )
        updated = _utc_now().isoformat().replace("+00:00", "Z")
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO clawback_state(region_code, city_code, until_ts, reason, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(region_code, city_code) DO UPDATE SET
                  until_ts=excluded.until_ts,
                  reason=excluded.reason,
                  updated_at=excluded.updated_at
                """,
                (_norm(region_code), _norm(city_code), until, reason, updated),
            )
            conn.commit()
        return {
            "region_code": _norm(region_code),
            "city_code": _norm(city_code),
            "until_ts": until,
            "reason": reason,
        }

    def clawback_active(self, region_code: str, city_code: str, at: datetime | None = None) -> bool:
        at = at or _utc_now()
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT until_ts FROM clawback_state
                WHERE region_code=? AND city_code=?
                """,
                (_norm(region_code), _norm(city_code)),
            ).fetchone()
        if row is None:
            return False
        return _parse_ts(str(row["until_ts"])) > at

    def effective_caps(
        self,
        region_code: str,
        city_code: str,
        *,
        clawback_scale: float = 0.5,
    ) -> dict[str, dict[str, Any]]:
        """Return hardgates, halved while clawback TTL is active."""
        caps = self.get(region_code, city_code)
        if not self.clawback_active(region_code, city_code):
            return caps
        scale = max(0.0, min(1.0, float(clawback_scale)))
        out: dict[str, dict[str, Any]] = {}
        for window, row in caps.items():
            item = dict(row)
            item["max_enforcements"] = int(int(item["max_enforcements"]) * scale)
            item["clawback_scaled"] = True
            out[window] = item
        return out
