import json
from pathlib import Path

from offline_cancel_risk.eval.holdout import assert_floors, run_holdout_eval
from offline_cancel_risk.settings import ROOT


def test_holdout_meets_ci_floors(tmp_path: Path):
    out = tmp_path / "latest.json"
    report = run_holdout_eval(out_path=out)
    floors = json.loads(
        (ROOT / "docs" / "evals" / "floors.json").read_text(encoding="utf-8")
    )
    assert_floors(report, floors)
    assert report["order_count"] >= floors["min_order_count"]
    assert report["by_head"]["selective_theft"]["precision"] == 1.0
    assert report["beats_always_precision"]["selective_theft"] is True
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["order_count"] == report["order_count"]


def test_holdout_theft_beats_always_baseline():
    report = run_holdout_eval()
    model_p = report["by_head"]["selective_theft"]["precision"]
    always_p = report["baselines"]["always"]["selective_theft"]["precision"]
    assert model_p > always_p
