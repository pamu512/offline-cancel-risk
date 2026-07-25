"""Resolve base + region + city policy overlays within ops guardrails."""

from __future__ import annotations

import copy
from typing import Any


class GuardrailError(ValueError):
    """Overlay violates configured min/max guardrails."""


def deep_merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    out = copy.deepcopy(base)
    for key, value in overlay.items():
        if (
            key in out
            and isinstance(out[key], dict)
            and isinstance(value, dict)
        ):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


def _set_path(root: dict[str, Any], dotted: str, value: Any) -> None:
    parts = dotted.split(".")
    cur: dict[str, Any] = root
    for part in parts[:-1]:
        nxt = cur.get(part)
        if not isinstance(nxt, dict):
            nxt = {}
            cur[part] = nxt
        cur = nxt
    cur[parts[-1]] = value


def _get_path(root: dict[str, Any], dotted: str) -> Any:
    cur: Any = root
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def flatten_numeric_overrides(
    overlay: dict[str, Any], prefix: str = ""
) -> dict[str, float | int]:
    """Flatten nested overlay to dotted numeric paths."""
    out: dict[str, float | int] = {}
    for key, value in overlay.items():
        path = f"{prefix}.{key}" if prefix else key
        if isinstance(value, dict):
            out.update(flatten_numeric_overrides(value, path))
        elif isinstance(value, bool):
            continue
        elif isinstance(value, (int, float)):
            out[path] = value
    return out


def validate_overlay(
    overlay: dict[str, Any],
    guardrails: dict[str, Any],
) -> None:
    bounds: dict[str, Any] = guardrails.get("bounds", {})
    allow_lists: list[str] = list(guardrails.get("allow_lists", []))
    flat = flatten_numeric_overrides(overlay)

    # Reject unknown numeric paths not in bounds
    for path, value in flat.items():
        if path not in bounds:
            raise GuardrailError(f"param not tunable under guardrails: {path}")
        b = bounds[path]
        lo, hi = float(b["min"]), float(b["max"])
        v = float(value)
        if v < lo or v > hi:
            raise GuardrailError(
                f"{path}={v} outside guardrail [{lo}, {hi}]"
            )

    # Allow-list keys (e.g. food_categories) may appear; other non-numeric nested
    # keys under known parents are rejected if not in bounds/allow_lists.
    def _walk(node: dict[str, Any], prefix: str) -> None:
        for key, value in node.items():
            path = f"{prefix}.{key}" if prefix else key
            if isinstance(value, dict):
                _walk(value, path)
            elif isinstance(value, list):
                if path not in allow_lists:
                    raise GuardrailError(f"list param not allowed: {path}")
            elif isinstance(value, (int, float)) and not isinstance(value, bool):
                continue  # already validated via flat
            elif isinstance(value, str):
                raise GuardrailError(f"string overrides not allowed: {path}")

    _walk(overlay, "")


def resolve_policy(
    base: dict[str, Any],
    *,
    region_overlay: dict[str, Any] | None = None,
    city_overlay: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Merge default ← region ← city (city wins)."""
    resolved = copy.deepcopy(base)
    if region_overlay:
        resolved = deep_merge(resolved, region_overlay)
    if city_overlay:
        resolved = deep_merge(resolved, city_overlay)
    return resolved
