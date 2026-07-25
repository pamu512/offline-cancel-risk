import hashlib
import json
from typing import Any


def policy_hash(policy: dict[str, Any]) -> str:
    canonical = json.dumps(policy, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def apply_thresholds(
    scores: dict[str, float],
    policy: dict[str, Any],
) -> dict[str, int]:
    thresholds = policy.get("thresholds") or {}
    flags: dict[str, int] = {}
    for head, score in scores.items():
        threshold = float(thresholds.get(head, 1.0))
        flags[head] = 1 if float(score) >= threshold else 0
    return flags
