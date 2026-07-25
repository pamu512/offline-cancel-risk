"""Append-only policy / tuning audit log."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4


class PolicyAuditLog:
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
                CREATE TABLE IF NOT EXISTS policy_audit_log (
                  audit_id TEXT PRIMARY KEY,
                  ts TEXT NOT NULL,
                  actor TEXT NOT NULL,
                  action TEXT NOT NULL,
                  region_code TEXT NOT NULL,
                  city_code TEXT NOT NULL,
                  before_json TEXT,
                  after_json TEXT,
                  metrics_before_json TEXT,
                  metrics_after_json TEXT,
                  constraints_json TEXT,
                  decision TEXT NOT NULL,
                  reason TEXT NOT NULL
                )
                """
            )
            conn.commit()

    def append(
        self,
        *,
        actor: str,
        action: str,
        region_code: str = "",
        city_code: str = "",
        before: dict[str, Any] | None = None,
        after: dict[str, Any] | None = None,
        metrics_before: dict[str, Any] | None = None,
        metrics_after: dict[str, Any] | None = None,
        constraints: dict[str, Any] | None = None,
        decision: str = "recorded",
        reason: str = "",
    ) -> str:
        audit_id = uuid4().hex
        ts = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO policy_audit_log(
                  audit_id, ts, actor, action, region_code, city_code,
                  before_json, after_json, metrics_before_json, metrics_after_json,
                  constraints_json, decision, reason
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    audit_id,
                    ts,
                    actor,
                    action,
                    (region_code or "").strip().upper(),
                    (city_code or "").strip().upper(),
                    json.dumps(before) if before is not None else None,
                    json.dumps(after) if after is not None else None,
                    json.dumps(metrics_before) if metrics_before is not None else None,
                    json.dumps(metrics_after) if metrics_after is not None else None,
                    json.dumps(constraints) if constraints is not None else None,
                    decision,
                    reason,
                ),
            )
            conn.commit()
        return audit_id

    def list_entries(
        self, *, limit: int = 100, action: str | None = None
    ) -> list[dict[str, Any]]:
        sql = "SELECT * FROM policy_audit_log"
        params: list[Any] = []
        if action:
            sql += " WHERE action = ?"
            params.append(action)
        sql += " ORDER BY ts DESC LIMIT ?"
        params.append(int(limit))
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        out: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            for key, src in (
                ("before", "before_json"),
                ("after", "after_json"),
                ("metrics_before", "metrics_before_json"),
                ("metrics_after", "metrics_after_json"),
                ("constraints", "constraints_json"),
            ):
                raw = item.pop(src)
                item[key] = json.loads(raw) if raw else None
            out.append(item)
        return out

    def last_apply_at(
        self, region_code: str, city_code: str, *, head: str | None = None
    ) -> str | None:
        region = (region_code or "").strip().upper()
        city = (city_code or "").strip().upper()
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT ts, after_json FROM policy_audit_log
                WHERE action='apply' AND region_code=? AND city_code=?
                ORDER BY ts DESC LIMIT 20
                """,
                (region, city),
            ).fetchall()
        for row in rows:
            if head is None:
                return str(row["ts"])
            after = json.loads(row["after_json"]) if row["after_json"] else {}
            thresholds = after.get("thresholds") or {}
            if head in thresholds:
                return str(row["ts"])
        return None
