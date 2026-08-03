"""Storage / publisher ports used by assess and control plane.

SQLite remains the default implementation. Set OCR_DATABASE_URL to use Postgres
for the assessments table (shared idempotency across replicas).
"""

from __future__ import annotations

from typing import Any, Protocol

from offline_cancel_risk.api.schemas import AssessmentResult


class StreamPublisher(Protocol):
    def publish(self, result: AssessmentResult) -> None: ...


class AssessmentStore(Protocol):
    """Assessments + feedback + ledger (idempotency cache)."""

    def upsert(self, result: AssessmentResult) -> None: ...

    def get(
        self,
        order_display_id: str,
        policy_hash: str,
        model_version: str,
        generation: int,
    ) -> AssessmentResult | None: ...

    def latest(self, order_display_id: str) -> AssessmentResult | None: ...

    def next_generation(self, order_display_id: str) -> int: ...

    def mark_prior_provisional(
        self, order_display_id: str, *, before_generation: int
    ) -> int: ...

    def list_generations(self, order_display_id: str) -> list[AssessmentResult]: ...

    def upsert_feedback(self, order_display_id: str, labels: dict[str, Any]) -> None: ...

    def list_feedback(self) -> list[dict[str, Any]]: ...

    def list_latest_assessments(self) -> list[AssessmentResult]: ...


# Feature / entity stores (assess side-channels). SQLite impls today.
class DeviceIntegrityPort(Protocol):
    def get(self, device_id: str) -> dict[str, Any] | None: ...

    def upsert(
        self,
        *,
        device_id: str,
        ewma_risk: float,
        instant_risk: float,
        flags: dict[str, Any],
        driver_id: int | None,
        user_id: int | None,
    ) -> dict[str, Any]: ...


class DeviceGraphPort(Protocol):
    def observe(
        self,
        *,
        device_id: str,
        driver_id: int | None,
        user_id: int | None,
        event_ts: str | None = None,
    ) -> None: ...

    def evaluate(
        self,
        *,
        device_id: str | None,
        driver_id: int | None,
        user_id: int | None,
        as_of: str | None,
        policy: dict[str, Any],
    ) -> dict[str, Any]: ...


class ChatSignalPort(Protocol):
    def get(self, order_display_id: str) -> dict[str, Any] | None: ...

    def upsert(
        self,
        *,
        order_display_id: str,
        driver_id: int | None,
        user_id: int | None,
        flags: dict[str, Any],
        risk: float,
        event_ts: str | None = None,
    ) -> dict[str, Any]: ...

    def driver_signal_count(
        self,
        driver_id: int,
        *,
        as_of: str | None = None,
        window_minutes: int = 10080,
        min_risk: float = 0.55,
    ) -> int: ...


class EntityAnomalyPort(Protocol):
    def record(
        self,
        *,
        entity_key: str,
        feature: str,
        value: float,
        cohort_key: str,
        order_display_id: str | None,
        event_ts: str,
    ) -> None: ...

    def recent_entity_values(
        self,
        entity_key: str,
        feature: str,
        *,
        limit: int,
        exclude_order_id: str | None = None,
    ) -> list[float]: ...

    def recent_peer_values(
        self,
        cohort_key: str,
        feature: str,
        *,
        limit: int,
        exclude_entity_key: str,
        exclude_order_id: str | None = None,
    ) -> list[float]: ...
