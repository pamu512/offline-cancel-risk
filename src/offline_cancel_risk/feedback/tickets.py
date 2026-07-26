"""Label ticket store + JSONL stream for review quota sampling."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4


def utc_day_key(now: datetime | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    return now.strftime("%Y-%m-%d")


class LabelTicketStore:
    def __init__(
        self,
        sqlite_path: Path | str,
        stream_path: Path | str | None = None,
    ) -> None:
        self._path = Path(sqlite_path)
        self._stream = Path(stream_path) if stream_path is not None else None
        self._path.parent.mkdir(parents=True, exist_ok=True)
        if self._stream is not None:
            self._stream.parent.mkdir(parents=True, exist_ok=True)
        self._init()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS label_tickets (
                  ticket_id TEXT PRIMARY KEY,
                  order_display_id TEXT NOT NULL,
                  region_code TEXT NOT NULL,
                  city_code TEXT NOT NULL,
                  heads_json TEXT NOT NULL,
                  sampling_reason TEXT NOT NULL,
                  strata_json TEXT NOT NULL,
                  priority INTEGER NOT NULL,
                  status TEXT NOT NULL,
                  day_key TEXT NOT NULL,
                  created_at TEXT NOT NULL,
                  labeled_at TEXT
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_tickets_day
                ON label_tickets(day_key, status)
                """
            )
            conn.commit()

    def day_count(self, day_key: str | None = None) -> int:
        day = day_key or utc_day_key()
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT COUNT(*) AS n FROM label_tickets
                WHERE day_key=? AND status IN ('open', 'labeled')
                """,
                (day,),
            ).fetchone()
        return int(row["n"]) if row else 0

    def head_counts(self, day_key: str | None = None) -> dict[str, int]:
        day = day_key or utc_day_key()
        counts: dict[str, int] = {
            "cancelled_offline": 0,
            "cancel_abuse": 0,
            "selective_theft": 0,
        }
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT heads_json FROM label_tickets
                WHERE day_key=? AND status IN ('open', 'labeled')
                """,
                (day,),
            ).fetchall()
        for row in rows:
            for head in json.loads(row["heads_json"]):
                if head in counts:
                    counts[head] += 1
        return counts

    def has_open_or_labeled_today(
        self, order_display_id: str, day_key: str | None = None
    ) -> bool:
        day = day_key or utc_day_key()
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT 1 FROM label_tickets
                WHERE order_display_id=? AND day_key=?
                  AND status IN ('open', 'labeled')
                LIMIT 1
                """,
                (order_display_id, day),
            ).fetchone()
        return row is not None

    def create(
        self,
        *,
        order_display_id: str,
        region_code: str = "",
        city_code: str = "",
        heads: list[str],
        sampling_reason: str,
        strata: dict[str, Any] | None = None,
        priority: int = 10,
        day_key: str | None = None,
    ) -> dict[str, Any] | None:
        day = day_key or utc_day_key()
        if self.has_open_or_labeled_today(order_display_id, day):
            return None
        ticket_id = uuid4().hex
        created = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        payload = {
            "ticket_id": ticket_id,
            "order_display_id": order_display_id,
            "region_code": (region_code or "").strip().upper(),
            "city_code": (city_code or "").strip().upper(),
            "heads": list(heads),
            "sampling_reason": sampling_reason,
            "strata": strata or {},
            "priority": int(priority),
            "status": "open",
            "day_key": day,
            "created_at": created,
            "labeled_at": None,
        }
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO label_tickets(
                  ticket_id, order_display_id, region_code, city_code,
                  heads_json, sampling_reason, strata_json, priority,
                  status, day_key, created_at, labeled_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
                """,
                (
                    ticket_id,
                    order_display_id,
                    payload["region_code"],
                    payload["city_code"],
                    json.dumps(payload["heads"]),
                    sampling_reason,
                    json.dumps(payload["strata"]),
                    int(priority),
                    "open",
                    day,
                    created,
                ),
            )
            conn.commit()
        if self._stream is not None:
            with self._stream.open("a", encoding="utf-8") as f:
                f.write(json.dumps(payload, separators=(",", ":")))
                f.write("\n")
        return payload

    def mark_labeled(self, order_display_id: str) -> int:
        labeled_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        with self._connect() as conn:
            cur = conn.execute(
                """
                UPDATE label_tickets
                SET status='labeled', labeled_at=?
                WHERE order_display_id=? AND status='open'
                """,
                (labeled_at, order_display_id),
            )
            conn.commit()
            return int(cur.rowcount)

    def list_tickets(
        self,
        *,
        status: str | None = None,
        day_key: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        sql = "SELECT * FROM label_tickets WHERE 1=1"
        params: list[Any] = []
        if status:
            sql += " AND status=?"
            params.append(status)
        if day_key:
            sql += " AND day_key=?"
            params.append(day_key)
        sql += " ORDER BY priority DESC, created_at DESC LIMIT ?"
        params.append(int(limit))
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        out: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["heads"] = json.loads(item.pop("heads_json"))
            item["strata"] = json.loads(item.pop("strata_json"))
            out.append(item)
        return out
