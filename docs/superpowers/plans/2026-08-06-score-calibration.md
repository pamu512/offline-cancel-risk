# Score Calibration — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Per head×market Platt/isotonic calibration on pattern cohort \(S\), shadow-by-default, so apply mode uses one calibrated \(p\) for flags, EAR, and published scores.

**Architecture:** Offline `run_calibration_fit` joins assessments↔labels, filters \(S\) via `scores_raw`, fits Platt or isotonic by support, gates on holdout ECE, stores params in `CalibratorStore`. Assess applies after baselines / before thresholds; `scores_raw` stays uncalibrated.

**Tech Stack:** Python 3.11+, SQLite, scikit-learn (`LogisticRegression`, `IsotonicRegression`), existing control-plane audit / patterns / holdout_split.

## Global Constraints

- Fit on pattern cohort \(S\) only (`in_pattern_cohort` + `scores_raw`)
- Per head × market only — no region/global fallback
- Default `calibration.mode: shadow`; apply via policy or API
- `scores_raw` never calibrated
- No new clusterer; no threshold-tuner objective change
- Do not invent amount-weighted labels or online EWMA bins

## File map

| File | Role |
|---|---|
| `src/offline_cancel_risk/scoring/calibration.py` | ECE, Platt/isotonic fit+predict, `calibration_cfg` |
| `src/offline_cancel_risk/control_plane/calibrate.py` | `CalibratorStore`, `CalibrationRunStore`, `run_calibration_fit` |
| `src/offline_cancel_risk/pipeline/score_build.py` | Hook after baselines |
| `src/offline_cancel_risk/pipeline/context.py` + `assess.py` | `calibrators` on context |
| `src/offline_cancel_risk/api/schemas.py` | `calibration_meta` on `AssessmentResult` |
| `src/offline_cancel_risk/api/routes.py` + `main.py` + `settings.py` + queues | DI + API |
| `src/offline_cancel_risk/control_plane/loop.py` | Optional `on_tick` |
| `config/policy.default.yaml` | `calibration:` block |
| `tests/test_score_calibration.py` | Unit + fit + assess shadow/apply |
| `docs/OPS.md` + `docs/MANUAL.md` | Ops path |

---

### Task 1: Calibration math + store

**Files:**
- Create: `src/offline_cancel_risk/scoring/calibration.py`
- Create: `src/offline_cancel_risk/control_plane/calibrate.py` (store classes only in this task; fit runner in Task 2)
- Test: `tests/test_score_calibration.py`

**Interfaces:**
- Produces:
  - `calibration_cfg(policy: dict) -> dict` with keys `mode`, `on_tick`, `min_labeled`, `platt_max_n`, `holdout_fraction`, `max_ece`, `cooldown_minutes`, `ece_bins`
  - `expected_calibration_error(probs: list[float], labels: list[int], *, n_bins: int) -> float`
  - `fit_calibrator(xs: list[float], ys: list[int], *, platt_max_n: int) -> dict` → `{"method": "platt"|"isotonic", "params": {...}, "support": int}`
  - `predict_calibrated(model: dict, x: float) -> float` clipped to `[0,1]`
  - `CalibratorStore(sqlite_path)`: `upsert(...)`, `get(region, city, head) -> dict|None`, `list_market(region, city) -> list`

- [ ] **Step 1: Write failing tests for math + store**

```python
# tests/test_score_calibration.py
from pathlib import Path
import numpy as np

from offline_cancel_risk.scoring.calibration import (
    expected_calibration_error,
    fit_calibrator,
    predict_calibrated,
    calibration_cfg,
)
from offline_cancel_risk.control_plane.calibrate import CalibratorStore
from offline_cancel_risk.settings import load_policy


def test_ece_perfect_is_near_zero():
    probs = [0.1, 0.1, 0.9, 0.9]
    labels = [0, 0, 1, 1]
    assert expected_calibration_error(probs, labels, n_bins=2) < 0.05


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
```

- [ ] **Step 2: Run tests — expect import/fail**

```bash
.venv/bin/python -m pytest tests/test_score_calibration.py -v --tb=short
```

Expected: FAIL (modules missing)

- [ ] **Step 3: Implement `scoring/calibration.py`**

```python
# Key behaviors:
# - calibration_cfg reads policy["calibration"] with defaults from spec
# - ECE: equal-width bins on [0,1]; skip empty bins; weighted abs(acc-conf)
# - Platt: sklearn LogisticRegression; store coef_/intercept_ as lists
# - Isotonic: IsotonicRegression(out_of_bounds="clip"); store X_thresholds_/y_thresholds_
# - predict: reconstruct estimator or apply closed form; clip to [0,1]
# - fit_calibrator: if len(xs) < platt_max_n → platt else isotonic
# - Require both classes present in ys for Platt; if single-class, raise ValueError
#   (fit runner will catch and reject)
```

- [ ] **Step 4: Implement `CalibratorStore` in `control_plane/calibrate.py`**

```python
# Table calibrators(
#   region_code, city_code, head PRIMARY KEY,
#   method, params_json, ece, support, updated_at
# )
# upsert / get / list_market — mirror EntityBaselineStore style
```

- [ ] **Step 5: Re-run tests — expect PASS**

```bash
.venv/bin/python -m pytest tests/test_score_calibration.py -v --tb=short
```

- [ ] **Step 6: Commit**

```bash
git add src/offline_cancel_risk/scoring/calibration.py \
  src/offline_cancel_risk/control_plane/calibrate.py \
  tests/test_score_calibration.py
git commit -m "Add calibration math and CalibratorStore."
```

---

### Task 2: `run_calibration_fit`

**Files:**
- Modify: `src/offline_cancel_risk/control_plane/calibrate.py`
- Modify: `tests/test_score_calibration.py`

**Interfaces:**
- Consumes: `calibration_cfg`, `fit_calibrator`, `predict_calibrated`, `expected_calibration_error`, `CalibratorStore`, `holdout_split`, `in_pattern_cohort`, `resolve_scores`, `PolicyAuditLog`
- Produces:
  - `CalibrationRunStore` with `record(...)`, `latest(region, city) -> dict|None`
  - `@dataclass CalibrationFitContext` with `base_policy`, `audit`, `calibrators`, `assessments`, `feedback`, `region_code`, `city_code`, `mode_override`, `run_store`
  - `run_calibration_fit(ctx: CalibrationFitContext) -> dict` report with `decision`, `reason`, `heads: {head: {...}}`

- [ ] **Step 1: Write failing fit-runner tests**

```python
def _assess(oid: str, raw: float) -> dict:
    scores = {"cancelled_offline": raw, "cancel_abuse": 0.1, "selective_theft": 0.1}
    return {
        "order_display_id": oid,
        "region_code": "PH",
        "city_code": "MNL",
        "scores": scores,
        "scores_raw": scores,
    }


def test_fit_rejects_insufficient_pattern_labels(tmp_path: Path):
    # few pairs below min_labeled → decision rejected
    ...


def test_fit_persists_when_ece_ok(tmp_path: Path):
    # Build ~40 separable S pairs (raw >= 0.85), labels match high/low
    # mode shadow still persists calibrator (fit always writes store when gates pass;
    #   shadow only affects assess apply — per spec fit persists calibrator)
    # assert store.get(...) is not None and report["decision"] in {"fitted", "shadow"}


def test_fit_rejects_high_ece(tmp_path: Path, monkeypatch):
    # Force ECE above max_ece via monkeypatch expected_calibration_error → 0.5
    # assert no upsert / prior unchanged


def test_fit_respects_cooldown(tmp_path: Path):
    # Prior audit apply/fit success within cooldown → reject cooldown
```

Clarification for implementer (spec): successful fit **always upserts** the calibrator when ECE/cooldown gates pass. `mode` (`shadow`/`apply`/`off`) is an **assess-time** switch, not a fit write suppress. `mode=off` skips fit entirely. Report `decision` = `fitted` | `rejected` | `skipped`.

- [ ] **Step 2: Run — expect FAIL**

```bash
.venv/bin/python -m pytest tests/test_score_calibration.py -k fit -v --tb=short
```

- [ ] **Step 3: Implement `run_calibration_fit`**

Logic outline:

```python
def run_calibration_fit(ctx: CalibrationFitContext) -> dict:
    cfg = calibration_cfg(ctx.base_policy)
    mode = (ctx.mode_override or cfg["mode"]).lower()
    if mode == "off":
        return {"decision": "skipped", "reason": "mode_off", ...}
    # join + filter S per head using resolve_scores(..., blend=None) + in_pattern_cohort
    # require len(pairs) >= max(cfg["min_labeled"], learning.min_pattern_support)
    # holdout_split on feedback-shaped list
    # fit on train xs/ys; predict holdout; ECE
    # cooldown via audit.last_apply_at(..., head=f"cal::{head}") OR scan after_json for calibration
    #   Prefer: audit action="apply" actor="calibrator" and after contains head key
    #   Extend audit.last_apply_at similarly to dbscan head="dbscan" if needed
    # upsert CalibratorStore; audit append; CalibrationRunStore.record
```

For cooldown detection: store `after={"calibration": {"cancelled_offline": {...}}}` and extend `PolicyAuditLog.last_apply_at` to treat `head` starting with `cal::` or match `calibration` dict keys — simplest: `head="calibration"` and put all heads under `after["calibration"]`, matching dbscan pattern (`head=="dbscan"`).

- [ ] **Step 4: Tests PASS**

```bash
.venv/bin/python -m pytest tests/test_score_calibration.py -v --tb=short
```

- [ ] **Step 5: Commit**

```bash
git add src/offline_cancel_risk/control_plane/calibrate.py \
  src/offline_cancel_risk/control_plane/audit.py \
  tests/test_score_calibration.py
git commit -m "Add run_calibration_fit with ECE and cooldown gates."
```

---

### Task 3: Assess wire + API + policy + docs

**Files:**
- Modify: `pipeline/context.py`, `pipeline/assess.py`, `pipeline/score_build.py`
- Modify: `api/schemas.py`, `api/routes.py`, `main.py`, `settings.py`
- Modify: `worker/queue.py`, `worker/sqlite_queue.py`, `control_plane/loop.py`
- Modify: `config/policy.default.yaml`
- Modify: `docs/OPS.md`, `docs/MANUAL.md`
- Modify: `tests/test_score_calibration.py` (assess + API cases)

**Interfaces:**
- Consumes: `CalibratorStore.get`, `predict_calibrated`, `calibration_cfg`
- Produces: `AssessmentResult.calibration_meta: dict`; assess `scores` replaced only when `mode=="apply"`

- [ ] **Step 1: Failing assess/API tests**

```python
@pytest.mark.asyncio
async def test_assess_shadow_keeps_scores_writes_meta(tmp_path):
    # Seed calibrator; policy calibration.mode=shadow
    # assess_order with gps + calibrators=
    # assert result.scores ~= pre-cal path; calibration_meta[head]["p"] set; applied False


@pytest.mark.asyncio
async def test_assess_apply_replaces_scores_keeps_raw(tmp_path):
    # mode=apply; assert scores[head] == meta p; scores_raw unchanged from blend


def test_calibrate_api(tmp_path):
    # create_app with settings paths; POST /v1/tuning/calibrate
    # insufficient → rejected; GET latest returns report
```

- [ ] **Step 2: Run — expect FAIL**

```bash
.venv/bin/python -m pytest tests/test_score_calibration.py -v --tb=short
```

- [ ] **Step 3: Wire pipeline**

In `score_build.py` after baselines, before `apply_thresholds`:

```python
ctx.calibration_meta = {}
cfg = calibration_cfg(policy)
mode = cfg["mode"]
if ctx.calibrators is not None and mode != "off":
    region = (req.region_code or "").strip().upper()
    city = (req.city_code or "").strip().upper()
    for head in ("cancelled_offline", "cancel_abuse", "selective_theft"):
        row = ctx.calibrators.get(region, city, head)
        if row is None:
            ctx.calibration_meta[head] = {"applied": False, "skip_reason": "missing"}
            continue
        p = predict_calibrated(
            {"method": row["method"], "params": row["params"]},
            float(ctx.scores_raw[head]),
        )
        discounted = abs(float(ctx.scores[head]) - float(ctx.scores_raw[head])) > 1e-12
        meta = {
            "p": p,
            "method": row["method"],
            "mode": mode,
            "ece": row.get("ece"),
            "support": row.get("support"),
            "applied": False,
            "baseline_discounted": discounted,
        }
        if mode == "apply":
            ctx.scores[head] = p
            meta["applied"] = True
        ctx.calibration_meta[head] = meta
```

Pass `calibrators` through `AssessContext` / `assess_order` / queues / `main.py` like `baselines`.

Add `calibration_meta: dict = Field(default_factory=dict)` on `AssessmentResult`; publish in `publish` stage if needed (follow `baseline_meta` / `ear_meta` pattern).

- [ ] **Step 4: API + settings + policy**

```yaml
# config/policy.default.yaml
calibration:
  mode: shadow
  on_tick: false
  min_labeled: 30
  platt_max_n: 80
  holdout_fraction: 0.3
  max_ece: 0.05
  cooldown_minutes: 1440
  ece_bins: 10
```

```python
# settings.py
calibrators_path: str = str(ROOT / "data" / "calibrators.db")
```

Routes (mirror dbscan-retune):

- `POST /v1/tuning/calibrate` body `{region_code, city_code, mode?}` → `asyncio.to_thread(run_calibration_fit, ctx)`
- `GET /v1/tuning/calibrate/latest?region_code=&city_code=`

Loop: if `calibration.on_tick`, run fit per market after tune (filter `gps_cache`/`dbscan_retune_store`/`calibrators` out of `run_metrics_and_tune` kwargs).

- [ ] **Step 5: Docs**

OPS: new § after 4.6a — shadow fit → review ECE → `mode: apply` → **retune thresholds**.  
MANUAL §4.3a: one row that calibration is follow-on after Precision_S.

- [ ] **Step 6: Full suite**

```bash
.venv/bin/python -m pytest -q --tb=line
```

Expected: all previously green tests still pass; new calibration tests pass.

- [ ] **Step 7: Commit**

```bash
git add config/policy.default.yaml docs/OPS.md docs/MANUAL.md \
  src/offline_cancel_risk/ settings tests/test_score_calibration.py
git commit -m "Wire score calibration into assess, API, and docs."
```

---

## Spec coverage checklist

| Spec requirement | Task |
|---|---|
| Platt vs isotonic by `platt_max_n` | 1 |
| Fit on \(S\) via `scores_raw` | 2 |
| Per head×market store | 1–2 |
| ECE gate + cooldown | 2 |
| Shadow vs apply assess behavior | 3 |
| `scores_raw` unchanged | 3 |
| API + optional tick | 3 |
| OPS/MANUAL | 3 |
| No region fallback / no ML-only calib | Global constraints |

## Self-review notes

- Fit always persists calibrator when gates pass; `mode` is assess-only (clarified in Task 2 — matches “shadow fills meta” + “apply replaces scores”).
- Cooldown uses audit `after.calibration` like dbscan `after.dbscan`.
- No placeholders remaining.
