from typing import Any


def resolve_recoverability(
    policy: dict[str, Any],
    learned: dict[str, dict] | None,
) -> tuple[dict[str, float], dict[str, Any]]:
    ear_cfg = policy.get("ear") or {}
    mode = str(ear_cfg.get("mode", "shadow"))
    static = {
        k: float(v) for k, v in (ear_cfg.get("recoverability") or {}).items()
    }
    min_updates = int(ear_cfg.get("min_updates_apply", 5))

    full_learned = dict(static)
    if learned:
        for head, info in learned.items():
            if head in full_learned:
                full_learned[head] = float(info["value"])

    meta: dict[str, Any] = {
        "mode": mode,
        "recoverability_static": dict(static),
        "recoverability_learned": full_learned,
    }

    if mode == "apply":
        live = {}
        for head, static_val in static.items():
            head_info = (learned or {}).get(head)
            if (
                head_info is not None
                and int(head_info.get("n_updates", 0)) >= min_updates
            ):
                live[head] = float(head_info["value"])
            else:
                live[head] = static_val
    else:
        live = dict(static)

    return live, meta


def ear_shadow_delta_report(
    policy: dict[str, Any],
    learned: dict[str, dict] | None,
) -> dict[str, Any]:
    """Compare static vs learned recoverability; flag heads ready for apply."""
    ear_cfg = policy.get("ear") or {}
    mode = str(ear_cfg.get("mode", "shadow"))
    min_updates = int(ear_cfg.get("min_updates_apply", 5))
    static = {
        k: float(v) for k, v in (ear_cfg.get("recoverability") or {}).items()
    }
    heads: dict[str, Any] = {}
    # Only heads that have received outcomes gate market readiness (skip cold heads).
    updated_heads = 0
    updated_ready = 0
    for head, static_val in static.items():
        info = (learned or {}).get(head) or {}
        n = int(info.get("n_updates", 0))
        learned_val = float(info["value"]) if "value" in info else static_val
        ready = n >= min_updates
        if n > 0:
            updated_heads += 1
            if ready:
                updated_ready += 1
        heads[head] = {
            "static": static_val,
            "learned": learned_val,
            "delta": learned_val - static_val,
            "n_updates": n,
            "apply_ready": ready,
            "would_use_if_apply": learned_val if ready else static_val,
        }
    market_ready = updated_heads > 0 and updated_ready == updated_heads
    return {
        "mode": mode,
        "min_updates_apply": min_updates,
        "market_apply_ready": market_ready,
        "updated_heads": updated_heads,
        "recommendation": (
            "consider_apply"
            if market_ready and mode == "shadow"
            else ("ok_apply" if mode == "apply" else "wait_for_updates")
        ),
        "heads": heads,
    }


def compute_ear(
    scores: dict[str, float],
    order_value: float,
    policy: dict[str, Any],
    *,
    recoverability: dict[str, float] | None = None,
) -> tuple[dict[str, float], float]:
    ear_cfg = policy.get("ear") or {}
    if recoverability is None:
        recoverability = ear_cfg.get("recoverability") or {}
    attention_weights = ear_cfg.get("attention_weights") or {}

    ear: dict[str, float] = {}
    attention = 0.0
    for head, score in scores.items():
        rec = float(recoverability.get(head, 0.0))
        ear_value = float(order_value) * float(score) * rec
        ear[head] = ear_value
        attention += float(attention_weights.get(head, 0.0)) * ear_value
    return ear, attention
