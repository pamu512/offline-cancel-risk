"""High-level policy resolution for assess + ops APIs."""

from __future__ import annotations

from typing import Any

from offline_cancel_risk.policy.overlays import PolicyOverlayStore
from offline_cancel_risk.policy.resolve import (
    GuardrailError,
    resolve_policy,
    validate_overlay,
)


def resolved_policy_for_market(
    base: dict[str, Any],
    overlays: PolicyOverlayStore,
    *,
    region_code: str | None,
    city_code: str | None,
) -> dict[str, Any]:
    region = (region_code or "").strip().upper()
    city = (city_code or "").strip().upper()
    region_overlay = overlays.get(region, "") if region else None
    city_overlay = overlays.get(region, city) if region and city else None
    return resolve_policy(
        base, region_overlay=region_overlay, city_overlay=city_overlay
    )


def save_overlay(
    overlays: PolicyOverlayStore,
    guardrails: dict[str, Any],
    *,
    region_code: str,
    city_code: str,
    overlay: dict[str, Any],
) -> dict[str, Any]:
    region = region_code.strip().upper()
    city = (city_code or "").strip().upper()
    if not region:
        raise GuardrailError("region_code is required")
    validate_overlay(overlay, guardrails)
    overlays.upsert(region, city, overlay)
    return {"region_code": region, "city_code": city, "overlay": overlay}
