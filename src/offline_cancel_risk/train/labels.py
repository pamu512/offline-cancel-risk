from __future__ import annotations

import numpy as np

from offline_cancel_risk.train.scenarios import HEADS


def teacher_labels_from_flags(flags: dict[str, int]) -> dict[str, int]:
    return {h: int(flags[h]) for h in HEADS}


def flip_labels(
    labels: dict[str, int],
    rng: np.random.Generator,
    *,
    flip_rate: float,
) -> dict[str, int]:
    if not 0.0 <= flip_rate <= 1.0:
        raise ValueError("flip_rate must be in [0, 1]")
    out = dict(labels)
    for h in HEADS:
        if rng.random() < flip_rate:
            out[h] = 1 - int(out[h])
    return out
