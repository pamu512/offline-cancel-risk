from __future__ import annotations

from dataclasses import dataclass

from offline_cancel_risk.adapters.publishers import TablePublisher
from offline_cancel_risk.api.schemas import AssessmentResult


@dataclass(frozen=True)
class IdempotencyKey:
    order_display_id: str
    policy_hash: str
    model_version: str
    assessment_generation: int


def make_idempotency_key(
    order_display_id: str,
    policy_hash: str,
    model_version: str,
    assessment_generation: int,
) -> IdempotencyKey:
    return IdempotencyKey(
        order_display_id=order_display_id,
        policy_hash=policy_hash,
        model_version=model_version,
        assessment_generation=assessment_generation,
    )


def lookup_cached(table: TablePublisher, key: IdempotencyKey) -> AssessmentResult | None:
    return table.get(
        key.order_display_id,
        key.policy_hash,
        key.model_version,
        key.assessment_generation,
    )
