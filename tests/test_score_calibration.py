from pathlib import Path

import numpy as np

from offline_cancel_risk.control_plane.calibrate import CalibratorStore
from offline_cancel_risk.scoring.calibration import (
    calibration_cfg,
    expected_calibration_error,
    fit_calibrator,
    predict_calibrated,
)
from offline_cancel_risk.settings import load_policy


def test_ece_perfect_is_near_zero():
    probs = [0.1, 0.1, 0.9, 0.9]
    labels = [0, 0, 1, 1]
    assert expected_calibration_error(probs, labels, n_bins=2) <= 0.1


def test_fit_picks_platt_below_threshold():
    rng = np.random.default_rng(0)
    xs = rng.uniform(0, 1, 40).tolist()
    ys = [1 if x > 0.5 else 0 for x in xs]
    model = fit_calibrator(xs, ys, platt_max_n=80)
    assert model["method"] == "platt"
    assert 0.0 <= predict_calibrated(model, 0.9) <= 1.0


def test_fit_picks_isotonic_at_or_above_threshold():
    rng = np.random.default_rng(1)
    xs = rng.uniform(0, 1, 100).tolist()
    ys = [1 if x > 0.4 else 0 for x in xs]
    model = fit_calibrator(xs, ys, platt_max_n=80)
    assert model["method"] == "isotonic"


def test_calibrator_store_roundtrip(tmp_path: Path):
    store = CalibratorStore(tmp_path / "cal.db")
    store.upsert(
        region_code="PH",
        city_code="MNL",
        head="cancelled_offline",
        method="platt",
        params={"coef": [2.0], "intercept": [-1.0]},
        ece=0.02,
        support=40,
    )
    row = store.get("PH", "MNL", "cancelled_offline")
    assert row is not None
    assert row["method"] == "platt"
    assert row["ece"] == 0.02
