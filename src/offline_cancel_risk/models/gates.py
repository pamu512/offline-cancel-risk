"""Promotion gates → promotion_ready signal."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from offline_cancel_risk.models.metrics import ShadowMetricsStore


@dataclass
class PromotionStatus:
    challenger_model_id: str
    champion_model_id: str
    promotion_ready: int
    promotion_blockers: list[str] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)
    recommended_action: str = "wait"


def evaluate_promotion(
    *,
    challenger_model_id: str,
    champion_model_id: str,
    store: ShadowMetricsStore,
    thresholds: dict[str, float],
    gates: dict[str, Any],
) -> PromotionStatus:
    rows = store.list_for_shadow(challenger_model_id)
    n = len(rows)
    min_n = int(gates.get("min_shadow_assessments", 500))
    blockers: list[str] = []
    if n < min_n:
        blockers.append(f"min_shadow_assessments:{n}<{min_n}")

    champ_fp = 0.0
    shadow_fp = 0.0
    champ_catch = 0.0
    shadow_catch = 0.0
    for row in rows:
        for head, thr in thresholds.items():
            c = row.champion_scores.get(head, 0.0) >= thr
            s = row.shadow_scores.get(head, 0.0) >= thr
            # proxy: flag without label → treat as FP cost; both flag → catch
            if c and not s:
                champ_fp += row.order_value
            if s and not c:
                shadow_fp += row.order_value
            if c:
                champ_catch += row.order_value
            if s:
                shadow_catch += row.order_value

    max_fp_lift = float(gates.get("max_fp_dollar_lift", 0.0))
    max_fp_abs = float(gates.get("max_fp_dollar_abs", 0.0))
    fp_cap = champ_fp * (1.0 + max_fp_lift) + max_fp_abs
    if n >= min_n and shadow_fp > fp_cap + 1e-9:
        blockers.append("fp_dollar_worse_than_champion")

    min_catch_lift = float(gates.get("min_catch_lift", 0.02))
    if n >= min_n:
        if champ_catch <= 0:
            if shadow_catch <= 0:
                blockers.append("catch_lift_insufficient")
        else:
            lift = (shadow_catch - champ_catch) / champ_catch
            if lift < min_catch_lift:
                blockers.append(f"catch_lift_insufficient:{lift:.4f}<{min_catch_lift}")

    # Label agreement when Downstream labels are present on shadow rows.
    labeled_n = 0
    agree_n = 0
    for row in rows:
        if not row.labels:
            continue
        labeled_n += 1
        head_ok = True
        for head, thr in thresholds.items():
            if head not in row.labels:
                continue
            truth = int(row.labels[head])
            pred = 1 if row.shadow_scores.get(head, 0.0) >= thr else 0
            if pred != truth:
                head_ok = False
                break
        if head_ok:
            agree_n += 1
    label_agreement = (agree_n / labeled_n) if labeled_n else None
    min_label_n = int(gates.get("min_labeled_shadow", 0))
    min_agree = gates.get("min_label_agreement")
    if min_label_n > 0 and labeled_n < min_label_n:
        blockers.append(f"min_labeled_shadow:{labeled_n}<{min_label_n}")
    if (
        min_agree is not None
        and labeled_n > 0
        and label_agreement is not None
        and float(label_agreement) < float(min_agree)
    ):
        blockers.append(
            f"label_agreement:{label_agreement:.4f}<{float(min_agree)}"
        )

    metrics = {
        "n": n,
        "champion_fp_dollar_proxy": champ_fp,
        "shadow_fp_dollar_proxy": shadow_fp,
        "champion_catch_dollar_proxy": champ_catch,
        "shadow_catch_dollar_proxy": shadow_catch,
        "labeled_n": labeled_n,
        "label_agreement": label_agreement,
    }
    ready = 1 if not blockers else 0
    return PromotionStatus(
        challenger_model_id=challenger_model_id,
        champion_model_id=champion_model_id,
        promotion_ready=ready,
        promotion_blockers=blockers,
        metrics=metrics,
        recommended_action="start_canary" if ready else "wait",
    )
