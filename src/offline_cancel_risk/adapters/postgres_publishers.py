"""Postgres AssessmentStore — shared idempotency for multi-replica assess."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from offline_cancel_risk.api.schemas import AssessmentResult

_SCHEMA = """
CREATE TABLE IF NOT EXISTS assessments (
  order_display_id TEXT NOT NULL,
  policy_hash TEXT NOT NULL,
  model_version TEXT NOT NULL,
  assessment_generation INTEGER NOT NULL,
  payload_json TEXT NOT NULL,
  assessed_at TEXT NOT NULL,
  PRIMARY KEY (order_display_id, policy_hash, model_version, assessment_generation)
);
CREATE TABLE IF NOT EXISTS ledger (
  id BIGSERIAL PRIMARY KEY,
  order_display_id TEXT NOT NULL,
  policy_hash TEXT NOT NULL,
  model_version TEXT NOT NULL,
  assessment_generation INTEGER NOT NULL,
  payload_json TEXT NOT NULL,
  written_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS feedback (
  order_display_id TEXT PRIMARY KEY,
  labels TEXT NOT NULL,
  created_at TEXT NOT NULL
);
"""


class PostgresTablePublisher:
    """AssessmentStore backed by Postgres (psycopg3)."""

    def __init__(self, database_url: str) -> None:
        if not (database_url or "").strip():
            raise ValueError("database_url required")
        try:
            import psycopg  # noqa: F401
        except ImportError as exc:  # pragma: no cover - env dependent
            raise ImportError(
                "Postgres assessments require psycopg. Install with: "
                'pip install -e ".[pg]"'
            ) from exc
        self._url = database_url.strip()
        self._ensure_schema()

    def _connect(self):  # type: ignore[no-untyped-def]
        import psycopg

        return psycopg.connect(self._url)

    def _ensure_schema(self) -> None:
        with self._connect() as conn:
            conn.execute(_SCHEMA)
            conn.commit()

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
                ) VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (order_display_id, policy_hash, model_version, assessment_generation)
                DO UPDATE SET
                  payload_json = EXCLUDED.payload_json,
                  assessed_at = EXCLUDED.assessed_at
                """,
                (*key, payload, result.assessed_at),
            )
            conn.execute(
                """
                INSERT INTO ledger (
                  order_display_id, policy_hash, model_version, assessment_generation,
                  payload_json, written_at
                ) VALUES (%s, %s, %s, %s, %s, %s)
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
                WHERE order_display_id = %s AND policy_hash = %s AND model_version = %s
                  AND assessment_generation = %s
                """,
                (order_display_id, policy_hash, model_version, generation),
            ).fetchone()
        if row is None:
            return None
        return AssessmentResult.model_validate_json(row[0])

    def latest(self, order_display_id: str) -> AssessmentResult | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT payload_json FROM assessments
                WHERE order_display_id = %s
                ORDER BY assessment_generation DESC
                LIMIT 1
                """,
                (order_display_id,),
            ).fetchone()
        if row is None:
            return None
        return AssessmentResult.model_validate_json(row[0])

    def next_generation(self, order_display_id: str) -> int:
        latest = self.latest(order_display_id)
        if latest is None:
            return 1
        return int(latest.assessment_generation) + 1

    def mark_prior_provisional(
        self, order_display_id: str, *, before_generation: int
    ) -> int:
        updated = 0
        for result in self.list_generations(order_display_id):
            if result.assessment_generation >= before_generation:
                continue
            if result.provisional:
                continue
            patched = result.model_copy(update={"provisional": True})
            self.upsert(patched)
            updated += 1
        return updated

    def list_generations(self, order_display_id: str) -> list[AssessmentResult]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT payload_json FROM assessments
                WHERE order_display_id = %s
                ORDER BY assessment_generation ASC
                """,
                (order_display_id,),
            ).fetchall()
        return [AssessmentResult.model_validate_json(r[0]) for r in rows]

    def upsert_feedback(self, order_display_id: str, labels: dict[str, Any]) -> None:
        created_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO feedback (order_display_id, labels, created_at)
                VALUES (%s, %s, %s)
                ON CONFLICT (order_display_id) DO UPDATE SET
                  labels = EXCLUDED.labels,
                  created_at = EXCLUDED.created_at
                """,
                (order_display_id, json.dumps(labels), created_at),
            )
            conn.commit()

    def list_feedback(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT order_display_id, labels, created_at FROM feedback"
            ).fetchall()
        out: list[dict[str, Any]] = []
        for order_display_id, labels, created_at in rows:
            parsed = json.loads(labels)
            out.append(
                {
                    "order_display_id": order_display_id,
                    "labels": parsed if isinstance(parsed, dict) else {},
                    "created_at": created_at,
                }
            )
        return out

    def list_latest_assessments(self) -> list[AssessmentResult]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT payload_json FROM assessments a
                WHERE assessment_generation = (
                  SELECT MAX(b.assessment_generation) FROM assessments b
                  WHERE b.order_display_id = a.order_display_id
                )
                ORDER BY assessed_at DESC
                """
            ).fetchall()
        return [AssessmentResult.model_validate_json(r[0]) for r in rows]
