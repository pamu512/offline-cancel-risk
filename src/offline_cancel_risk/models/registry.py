"""SQLite-backed model registry with champion/shadow/canary roles."""

from __future__ import annotations

import json
import shutil
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from offline_cancel_risk.models.bundle import BundleError, ModelHandle, load_bundle

Role = Literal["champion", "shadow", "canary", "retired", "failed_canary"]


@dataclass(frozen=True)
class ModelRecord:
    model_id: str
    format: str
    role: Role
    bundle_path: str
    feature_schema_version: str
    created_at: str
    meta_json: str


class ModelRegistry:
    def __init__(self, sqlite_path: Path | str, models_root: Path | str) -> None:
        self._sqlite_path = Path(sqlite_path)
        self._models_root = Path(models_root)
        self._sqlite_path.parent.mkdir(parents=True, exist_ok=True)
        self._models_root.mkdir(parents=True, exist_ok=True)
        self._init_db()
        self._handles: dict[str, ModelHandle] = {}

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._sqlite_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS models (
                  model_id TEXT PRIMARY KEY,
                  format TEXT NOT NULL,
                  role TEXT NOT NULL,
                  bundle_path TEXT NOT NULL,
                  feature_schema_version TEXT NOT NULL,
                  created_at TEXT NOT NULL,
                  meta_json TEXT NOT NULL
                )
                """
            )
            conn.commit()

    def sideload(self, bundle_path: Path | str, *, role: Role = "shadow") -> ModelRecord:
        src = Path(bundle_path)
        handle = load_bundle(src)
        dest = self._models_root / handle.model_id
        if src.resolve() != dest.resolve():
            if dest.exists():
                shutil.rmtree(dest)
            shutil.copytree(src, dest)
        # Re-load from dest so checksum/paths are canonical
        handle = load_bundle(dest)
        created = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        meta = (dest / "model.json").read_text(encoding="utf-8")
        if role == "champion":
            self._clear_role("champion")
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO models(
                  model_id, format, role, bundle_path, feature_schema_version,
                  created_at, meta_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(model_id) DO UPDATE SET
                  format=excluded.format,
                  role=excluded.role,
                  bundle_path=excluded.bundle_path,
                  feature_schema_version=excluded.feature_schema_version,
                  created_at=excluded.created_at,
                  meta_json=excluded.meta_json
                """,
                (
                    handle.model_id,
                    handle.format,
                    role,
                    str(dest),
                    handle.feature_schema_version,
                    created,
                    meta,
                ),
            )
            conn.commit()
        self._handles[handle.model_id] = handle
        return self.get(handle.model_id)

    def _clear_role(self, role: Role) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE models SET role=? WHERE role=?",
                ("retired", role),
            )
            conn.commit()

    def set_role(self, model_id: str, role: Role) -> ModelRecord:
        if role in {"champion", "canary"}:
            self._clear_role(role)
        with self._connect() as conn:
            cur = conn.execute(
                "UPDATE models SET role=? WHERE model_id=?",
                (role, model_id),
            )
            if cur.rowcount == 0:
                raise BundleError(f"unknown model_id: {model_id}")
            conn.commit()
        return self.get(model_id)

    def get(self, model_id: str) -> ModelRecord:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM models WHERE model_id=?", (model_id,)
            ).fetchone()
        if row is None:
            raise BundleError(f"unknown model_id: {model_id}")
        return ModelRecord(**dict(row))

    def list_models(self) -> list[ModelRecord]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM models ORDER BY created_at DESC"
            ).fetchall()
        return [ModelRecord(**dict(r)) for r in rows]

    def get_champion(self) -> ModelRecord | None:
        return self._get_by_role("champion")

    def get_canary(self) -> ModelRecord | None:
        return self._get_by_role("canary")

    def list_shadow(self) -> list[ModelRecord]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM models WHERE role=? ORDER BY created_at DESC",
                ("shadow",),
            ).fetchall()
        return [ModelRecord(**dict(r)) for r in rows]

    def _get_by_role(self, role: Role) -> ModelRecord | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM models WHERE role=? LIMIT 1", (role,)
            ).fetchone()
        return ModelRecord(**dict(row)) if row else None

    def get_handle(self, model_id: str) -> ModelHandle:
        if model_id in self._handles:
            return self._handles[model_id]
        rec = self.get(model_id)
        handle = load_bundle(rec.bundle_path)
        self._handles[model_id] = handle
        return handle

    def predict(
        self, model_id: str, features: dict[str, float]
    ) -> dict[str, float]:
        return self.get_handle(model_id).predict(features)

    def roles_snapshot(self) -> dict[str, str]:
        return {m.model_id: m.role for m in self.list_models()}
