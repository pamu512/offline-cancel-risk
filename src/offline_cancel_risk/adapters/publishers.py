from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

from offline_cancel_risk.api.schemas import AssessmentResult
from offline_cancel_risk.settings import get_settings

_SCHEMA = """
CREATE TABLE IF NOT EXISTS assessments (
  order_display_id TEXT,
  policy_hash TEXT,
  model_version TEXT,
  assessment_generation INTEGER,
  payload_json TEXT NOT NULL,
  assessed_at TEXT NOT NULL,
  PRIMARY KEY (order_display_id, policy_hash, model_version, assessment_generation)
);
CREATE TABLE IF NOT EXISTS ledger (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  order_display_id TEXT,
  policy_hash TEXT,
  model_version TEXT,
  assessment_generation INTEGER,
  payload_json TEXT NOT NULL,
  written_at TEXT NOT NULL
);
"""


class StreamPublisher(Protocol):
    def publish(self, result: AssessmentResult) -> None: ...


class TablePublisher(Protocol):
    def upsert(self, result: AssessmentResult) -> None: ...

    def get(
        self,
        order_display_id: str,
        policy_hash: str,
        model_version: str,
        generation: int,
    ) -> AssessmentResult | None: ...


class JsonlStreamPublisher:
    def __init__(self, stream_path: str | Path | None = None) -> None:
        self._path = Path(stream_path if stream_path is not None else get_settings().stream_path)

    def publish(self, result: AssessmentResult) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open("a", encoding="utf-8") as f:
            f.write(result.model_dump_json())
            f.write("\n")


class SqliteTablePublisher:
    def __init__(self, sqlite_path: str | Path | None = None) -> None:
        self._path = Path(sqlite_path if sqlite_path is not None else get_settings().sqlite_path)
        self._ensure_schema()

    def _connect(self) -> sqlite3.Connection:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        return sqlite3.connect(self._path)

    def _ensure_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(_SCHEMA)

    def upsert(self, result: AssessmentResult) -> None:
        payload = result.model_dump_json()
        key = (
            result.order_display_id,
            result.policy_hash,
            result.model_version,
            result.assessment_generation,
        )
        written_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO assessments (
                  order_display_id, policy_hash, model_version, assessment_generation,
                  payload_json, assessed_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(order_display_id, policy_hash, model_version, assessment_generation)
                DO UPDATE SET
                  payload_json = excluded.payload_json,
                  assessed_at = excluded.assessed_at
                """,
                (*key, payload, result.assessed_at),
            )
            conn.execute(
                """
                INSERT INTO ledger (
                  order_display_id, policy_hash, model_version, assessment_generation,
                  payload_json, written_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (*key, payload, written_at),
            )
            conn.commit()

    def get(
        self,
        order_display_id: str,
        policy_hash: str,
        model_version: str,
        generation: int,
    ) -> AssessmentResult | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT payload_json FROM assessments
                WHERE order_display_id = ? AND policy_hash = ? AND model_version = ?
                  AND assessment_generation = ?
                """,
                (order_display_id, policy_hash, model_version, generation),
            ).fetchone()
        if row is None:
            return None
        return AssessmentResult.model_validate_json(row[0])
