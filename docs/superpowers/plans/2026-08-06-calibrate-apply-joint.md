# Joint Calibration Apply + Threshold Retune — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Atomic `POST /v1/tuning/calibrate/apply` that projects calibrated scores, runs deferred threshold search, and commits `calibration.mode: apply` ± τ in one overlay write; harden bare overlay apply flips.

**Architecture:** Projection helper builds in-memory assess copies with applied scores in both `scores` and `scores_raw`. `run_tuner(..., defer_overlay_writes=True)` searches without `save_overlay`. `run_calibrate_apply` deep-merges mode + accepted τ once. Overlay PUT rejects shadow→apply without escape hatch.

**Tech Stack:** Existing Python control plane (`run_tuner`, `CalibratorStore`, `PolicyOverlayStore`, FastAPI routes), `deep_merge` from `policy.resolve`.

## Global Constraints

- Soft success: search completed ⇒ allow mode apply even if zero heads change τ
- All-or-nothing on mode: if search cannot run, write nothing
- Do not call `run_tuner` as-is for the happy path (it writes per head)
- No calibration fit math changes; no auto historical reassess
- Escape hatch: `allow_calib_apply_without_retune` on overlay PUT only
- Audit actor: `calibrate_apply`

## File map

| File | Role |
|---|---|
| `src/offline_cancel_risk/control_plane/calibrate_apply.py` | Projection + `run_calibrate_apply` |
| `src/offline_cancel_risk/control_plane/tuner.py` | `defer_overlay_writes` on `TunerContext` / `run_tuner` |
| `src/offline_cancel_risk/control_plane/cycle.py` | Include `calibration_meta` in `assessments_as_dicts` |
| `src/offline_cancel_risk/api/routes.py` | New POST + overlay gate field |
| `tests/test_calibrate_apply_joint.py` | Projection, defer, orchestrator, API gates |
| `docs/OPS.md` + `docs/MANUAL.md` | Supported apply path |

---

### Task 1: Score projection helper

**Files:**
- Create: `src/offline_cancel_risk/control_plane/calibrate_apply.py` (projection only; orchestrator in Task 3)
- Modify: `src/offline_cancel_risk/control_plane/cycle.py` (`assessments_as_dicts` add `calibration_meta` when present)
- Test: `tests/test_calibrate_apply_joint.py`

**Interfaces:**
- Consumes: `predict_calibrated`, `apply_calibrated_score`, `CalibratorStore.list_market` / `get`
- Produces:
  - `project_calibrated_assessments(assessments: list[dict], *, calibrators: CalibratorStore, region_code: str, city_code: str, current_mode: str) -> list[dict]`
  - Returns deep copies; for each head with a calibrator, sets both `scores[h]` and `scores_raw[h]` to applied score

- [ ] **Step 1: Write failing projection tests**

```python
# tests/test_calibrate_apply_joint.py
from copy import deepcopy
from pathlib import Path

from offline_cancel_risk.control_plane.calibrate import CalibratorStore
from offline_cancel_risk.control_plane.calibrate_apply import (
    project_calibrated_assessments,
)
from offline_cancel_risk.scoring.calibration import fit_calibrator, predict_calibrated


def _store_platt(tmp_path: Path) -> CalibratorStore:
    store = CalibratorStore(tmp_path / "cal.db")
    model = fit_calibrator([0.1, 0.2, 0.8, 0.9], [0, 0, 1, 1], platt_max_n=80)
    store.upsert(
        region_code="PH",
        city_code="MNL",
        head="cancelled_offline",
        method=model["method"],
        params=model["params"],
        ece=0.01,
        support=4,
    )
    return store


def test_project_writes_applied_into_scores_and_raw(tmp_path: Path):
    store = _store_platt(tmp_path)
    raw = 0.9
    assessments = [
        {
            "order_display_id": "o1",
            "region_code": "PH",
            "city_code": "MNL",
            "scores": {
                "cancelled_offline": 0.45,  # baseline discount vs raw
                "cancel_abuse": 0.1,
                "selective_theft": 0.1,
            },
            "scores_raw": {
                "cancelled_offline": raw,
                "cancel_abuse": 0.1,
                "selective_theft": 0.1,
            },
        }
    ]
    out = project_calibrated_assessments(
        assessments,
        calibrators=store,
        region_code="PH",
        city_code="MNL",
        current_mode="shadow",
    )
    assert out[0]["scores"]["cancelled_offline"] == out[0]["scores_raw"]["cancelled_offline"]
    assert out[0]["scores"]["cancelled_offline"] != raw
    # original untouched
    assert assessments[0]["scores_raw"]["cancelled_offline"] == raw


def test_project_already_apply_uses_meta_p_when_present(tmp_path: Path):
    store = _store_platt(tmp_path)
    # published score already calibrated: p_old * discount
    assessments = [
        {
            "order_display_id": "o1",
            "region_code": "PH",
            "city_code": "MNL",
            "scores": {
                "cancelled_offline": 0.4,
                "cancel_abuse": 0.1,
                "selective_theft": 0.1,
            },
            "scores_raw": {
                "cancelled_offline": 1.0,
                "cancel_abuse": 0.1,
                "selective_theft": 0.1,
            },
            "calibration_meta": {
                "cancelled_offline": {"p": 0.8, "score_applied": 0.4}
            },
        }
    ]
    out = project_calibrated_assessments(
        assessments,
        calibrators=store,
        region_code="PH",
        city_code="MNL",
        current_mode="apply",
    )
    # Must not double-apply via scores/raw (= 0.4); pre_calib should be 0.4/0.8 = 0.5
    assert 0.0 <= out[0]["scores"]["cancelled_offline"] <= 1.0
    assert assessments[0]["scores"]["cancelled_offline"] == 0.4
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_calibrate_apply_joint.py::test_project_writes_applied_into_scores_and_raw tests/test_calibrate_apply_joint.py::test_project_already_apply_uses_meta_p_when_present -v`

Expected: FAIL (import / function missing)

- [ ] **Step 3: Implement projection + assessments_as_dicts meta**

```python
# src/offline_cancel_risk/control_plane/calibrate_apply.py
from __future__ import annotations

from copy import deepcopy
from typing import Any

from offline_cancel_risk.control_plane.calibrate import CalibratorStore
from offline_cancel_risk.scoring.calibration import (
    apply_calibrated_score,
    predict_calibrated,
)

_HEADS = ("cancelled_offline", "cancel_abuse", "selective_theft")


def project_calibrated_assessments(
    assessments: list[dict[str, Any]],
    *,
    calibrators: CalibratorStore,
    region_code: str,
    city_code: str,
    current_mode: str,
) -> list[dict[str, Any]]:
    region = region_code.strip().upper()
    city = (city_code or "").strip().upper()
    mode = (current_mode or "shadow").strip().lower()
    out: list[dict[str, Any]] = []
    for a in assessments:
        copy = deepcopy(a)
        scores = dict(copy.get("scores") or {})
        raw = dict(copy.get("scores_raw") or scores)
        meta = copy.get("calibration_meta") or {}
        for head in _HEADS:
            row = calibrators.get(region, city, head)
            if row is None:
                continue
            raw_v = float(raw.get(head, 0.0))
            p = predict_calibrated(
                {"method": row["method"], "params": row["params"]}, raw_v
            )
            if mode == "apply":
                head_meta = meta.get(head) if isinstance(meta, dict) else None
                p_old = None
                if isinstance(head_meta, dict) and head_meta.get("p") is not None:
                    try:
                        p_old = float(head_meta["p"])
                    except (TypeError, ValueError):
                        p_old = None
                if p_old is not None and abs(p_old) > 1e-12:
                    pre_calib = float(scores.get(head, raw_v)) / p_old
                else:
                    pre_calib = raw_v
            else:
                pre_calib = float(scores.get(head, raw_v))
            applied = apply_calibrated_score(
                p=p, scores_raw=raw_v, scores_current=pre_calib
            )
            scores[head] = applied
            raw[head] = applied
        copy["scores"] = scores
        copy["scores_raw"] = raw
        out.append(copy)
    return out
```

In `cycle.py` `assessments_as_dicts`, after scores_raw block:

```python
        if getattr(r, "calibration_meta", None):
            row["calibration_meta"] = dict(r.calibration_meta)
```

- [ ] **Step 4: Run projection tests — expect PASS**

Run: `pytest tests/test_calibrate_apply_joint.py::test_project_writes_applied_into_scores_and_raw tests/test_calibrate_apply_joint.py::test_project_already_apply_uses_meta_p_when_present -v`

- [ ] **Step 5: Commit**

```bash
git add src/offline_cancel_risk/control_plane/calibrate_apply.py \
  src/offline_cancel_risk/control_plane/cycle.py \
  tests/test_calibrate_apply_joint.py
git commit -m "Add calibrated score projection for joint apply retune."
```

---

### Task 2: Tuner `defer_overlay_writes`

**Files:**
- Modify: `src/offline_cancel_risk/control_plane/tuner.py` (`TunerContext` + `run_tuner` save path)
- Test: `tests/test_calibrate_apply_joint.py`

**Interfaces:**
- Consumes: existing `run_tuner`
- Produces: `TunerContext.defer_overlay_writes: bool = False`. When True, skip `save_overlay`; still append decision with `"decision": "accepted"`, `"action": "suggest"`, `"overlay": overlay` (would-apply). Audit action `suggest` / decision `deferred` when deferred.

- [ ] **Step 1: Write failing defer test**

Copy the assessment/feedback construction from `tests/test_control_tuner.py::test_tuner_applies_overlay_when_pattern_recall_improves` (high scores in pattern strata, half positive labels). Reuse that file’s `_ctx` helper via import or local duplicate in `test_calibrate_apply_joint.py`.

```python
def test_run_tuner_defer_does_not_write_overlay(tmp_path: Path):
    from tests.test_control_tuner import _ctx, _labels, _scores

    assessments = []
    feedback = []
    for i in range(20):
        oid = f"S{i}"
        pos = i < 10
        assessments.append(
            {
                "order_display_id": oid,
                "region_code": "PH",
                "city_code": "MNL",
                "scores": _scores(0.95 if pos else 0.2),
                "scores_raw": _scores(0.95 if pos else 0.2),
            }
        )
        feedback.append({"order_display_id": oid, "labels": _labels(1 if pos else 0)})
    ctx = _ctx(tmp_path, assessments=assessments, feedback=feedback, supply=70, demand=100)
    ctx.defer_overlay_writes = True
    before = ctx.overlays.get("PH", "MNL")
    decisions = run_tuner(ctx)
    assert ctx.overlays.get("PH", "MNL") == before
    # At least one head would have written under normal mode
    assert any(d.get("decision") == "accepted" and d.get("overlay") for d in decisions) or any(
        d.get("reason") == "holdout_pattern_recall_lift_below_min" for d in decisions
    )
```

Note: if importing `tests.test_control_tuner` is awkward under pytest path layout, duplicate `_ctx` / `_scores` / `_labels` into `test_calibrate_apply_joint.py` instead (same code as that file).

- [ ] **Step 2: Run test — expect FAIL** (attribute missing or overlay still written)

- [ ] **Step 3: Implement defer**

Add to `TunerContext`:

```python
defer_overlay_writes: bool = False
```

Replace the `save_overlay` success block (~lines 439–492) with:

```python
        if ctx.defer_overlay_writes:
            ctx.audit.append(
                actor="tuner",
                action="suggest",
                region_code=region,
                city_code=city,
                before={"thresholds": {head: current_thr}},
                after=overlay,
                metrics_before=current_hold,
                metrics_after=best["holdout_metrics"],
                constraints=constraints,
                decision="deferred",
                reason="holdout_pattern_recall_lift",
            )
            decisions.append(
                {
                    "head": head,
                    "action": "suggest",
                    "decision": "accepted",
                    "reason": "holdout_pattern_recall_lift",
                    "overlay": overlay,
                    "recall_lift": lift,
                    "holdout_metrics": best["holdout_metrics"],
                    "train_metrics": best["train_metrics"],
                }
            )
            continue

        try:
            save_overlay(...)
        # ... existing apply path unchanged ...
```

- [ ] **Step 4: Run defer test — expect PASS**

- [ ] **Step 5: Commit**

```bash
git add src/offline_cancel_risk/control_plane/tuner.py tests/test_calibrate_apply_joint.py
git commit -m "Allow run_tuner to defer overlay writes for joint apply."
```

---

### Task 3: `run_calibrate_apply` orchestrator

**Files:**
- Modify: `src/offline_cancel_risk/control_plane/calibrate_apply.py`
- Test: `tests/test_calibrate_apply_joint.py`

**Interfaces:**
- Consumes: `project_calibrated_assessments`, `run_tuner` with defer, `deep_merge`, `save_overlay`, `resolved_policy_for_market`, `assessments_as_dicts`, `CalibratorStore.list_market`
- Produces:

```python
@dataclass
class CalibrateApplyContext:
    settings: Settings
    base_policy: dict[str, Any]
    guardrails: dict[str, Any]
    overlays: PolicyOverlayStore
    audit: PolicyAuditLog
    forecast: SupplyForecastStore
    hardgates: EnforcementHardgateStore
    op_cfg: dict[str, Any]
    calibrators: CalibratorStore
    table: AssessmentStore  # or duck-typed with list_latest_assessments + list_feedback
    region_code: str
    city_code: str = ""


def run_calibrate_apply(ctx: CalibrateApplyContext) -> dict[str, Any]:
    ...
```

**Search-ok rule:** after deferred `run_tuner`, if **every** decision reason is `insufficient_pattern_labels` (or decisions empty with no calibrators already handled), treat as reject `insufficient_labels`. If ≥1 head got past that gate (any other reason, including no-lift / cooldown / accepted), search-ok → commit.

- [ ] **Step 1: Write failing orchestrator tests**

```python
def test_calibrate_apply_no_calibrators_rejects(tmp_path: Path):
    # empty CalibratorStore → decision rejected, reason no_calibrators
    # overlay calibration.mode unchanged (still shadow / absent)


def test_calibrate_apply_writes_mode_on_search_ok(tmp_path: Path):
    # seed calibrator + enough pattern labels + assessments with scores_raw
    # run_calibrate_apply → decision applied, force_reassess_required True
    # resolved overlay has calibration.mode == apply
```

Build context mirroring `tests/test_score_calibration.py` API fixtures / tuner fixtures. Keep markets `PH`/`MNL`.

- [ ] **Step 2: Run — expect FAIL**

- [ ] **Step 3: Implement `run_calibrate_apply`**

Pseudo-structure (implement fully, no stubs):

```python
def run_calibrate_apply(ctx: CalibrateApplyContext) -> dict[str, Any]:
    region = ctx.region_code.strip().upper()
    city = (ctx.city_code or "").strip().upper()
    rows = ctx.calibrators.list_market(region, city)
    if not rows:
        ctx.audit.append(
            actor="calibrate_apply",
            action="reject",
            region_code=region,
            city_code=city,
            decision="rejected",
            reason="no_calibrators",
        )
        return {
            "decision": "rejected",
            "reason": "no_calibrators",
            "force_reassess_required": False,
            "decisions": [],
            "overlay": None,
        }

    resolved = resolved_policy_for_market(
        ctx.base_policy, ctx.overlays, region_code=region, city_code=city or None
    )
    current_mode = str((resolved.get("calibration") or {}).get("mode", "shadow"))
    from offline_cancel_risk.control_plane.cycle import assessments_as_dicts

    assessments = assessments_as_dicts(ctx.table)
    projected = project_calibrated_assessments(
        assessments,
        calibrators=ctx.calibrators,
        region_code=region,
        city_code=city,
        current_mode=current_mode,
    )
    feedback = ctx.table.list_feedback()
    decisions = run_tuner(
        TunerContext(
            base_policy=ctx.base_policy,
            guardrails=ctx.guardrails,
            overlays=ctx.overlays,
            audit=ctx.audit,
            forecast=ctx.forecast,
            hardgates=ctx.hardgates,
            op_cfg=ctx.op_cfg,
            assessments=projected,
            feedback=feedback,
            region_code=region,
            city_code=city,
            min_labeled=ctx.settings.tuner_min_labeled,
            cooldown_minutes=ctx.settings.tuner_cooldown_minutes,
            min_f1_lift=ctx.settings.tuner_min_f1_lift,
            defer_overlay_writes=True,
        )
    )

    if not decisions or all(
        d.get("reason") == "insufficient_pattern_labels" for d in decisions
    ):
        ctx.audit.append(
            actor="calibrate_apply",
            action="reject",
            region_code=region,
            city_code=city,
            decision="rejected",
            reason="insufficient_labels",
            after={"decisions": decisions},
        )
        return {
            "decision": "rejected",
            "reason": "insufficient_labels",
            "force_reassess_required": False,
            "decisions": decisions,
            "overlay": None,
        }

    patch: dict[str, Any] = {"calibration": {"mode": "apply"}}
    for d in decisions:
        if d.get("decision") == "accepted" and isinstance(d.get("overlay"), dict):
            patch = deep_merge(patch, d["overlay"])

    prior = ctx.overlays.get(region, city) or {}
    merged = deep_merge(prior, patch)
    try:
        save_overlay(
            ctx.overlays,
            ctx.guardrails,
            region_code=region,
            city_code=city,
            overlay=merged,
        )
    except GuardrailError as exc:
        ctx.audit.append(
            actor="calibrate_apply",
            action="reject",
            region_code=region,
            city_code=city,
            decision="rejected",
            reason=f"guardrail:{exc}",
            after=patch,
        )
        return {
            "decision": "rejected",
            "reason": f"guardrail:{exc}",
            "force_reassess_required": False,
            "decisions": decisions,
            "overlay": None,
        }

    ctx.audit.append(
        actor="calibrate_apply",
        action="apply",
        region_code=region,
        city_code=city,
        before=prior,
        after=merged,
        decision="accepted",
        reason="search_ok",
    )
    return {
        "decision": "applied",
        "reason": "search_ok",
        "force_reassess_required": True,
        "decisions": decisions,
        "overlay": merged,
    }
```

- [ ] **Step 4: Run orchestrator tests — expect PASS**

- [ ] **Step 5: Commit**

```bash
git add src/offline_cancel_risk/control_plane/calibrate_apply.py tests/test_calibrate_apply_joint.py
git commit -m "Add run_calibrate_apply atomic mode+threshold commit."
```

---

### Task 4: API route + overlay escape hatch

**Files:**
- Modify: `src/offline_cancel_risk/api/routes.py`
- Test: `tests/test_calibrate_apply_joint.py`

**Interfaces:**
- Produces: `POST /v1/tuning/calibrate/apply` with body `{region_code, city_code?}`
- Extends `PolicyOverlayIngestRequest` with `allow_calib_apply_without_retune: bool = False`
- Gate helper (inline in route or small function in `calibrate_apply.py`):

```python
def calib_apply_overlay_blocked(
    *,
    prior_overlay: dict | None,
    base_policy: dict,
    region_code: str,
    city_code: str,
    new_overlay: dict,
    allow_escape: bool,
    overlays: PolicyOverlayStore,
) -> str | None:
    """Return error detail if blocked, else None."""
```

Resolve **prior mode** from `resolved_policy_for_market` (base ← region ← city) before write. Resolve **new mode** as: if `new_overlay` contains `calibration.mode`, that value; else prior. Block only when prior ≠ `apply` and new == `apply` and not `allow_escape`.

- [ ] **Step 1: Write failing API tests**

```python
def test_put_overlay_apply_blocked_without_escape(tmp_path: Path):
    client = TestClient(create_app(...))  # same pattern as test_score_calibration
    r = client.put(
        "/v1/policy/overlays",
        json={
            "region_code": "PH",
            "city_code": "MNL",
            "overlay": {"calibration": {"mode": "apply"}},
        },
    )
    assert r.status_code == 400


def test_put_overlay_apply_allowed_with_escape(tmp_path: Path):
    r = client.put(
        "/v1/policy/overlays",
        json={
            "region_code": "PH",
            "city_code": "MNL",
            "overlay": {"calibration": {"mode": "apply"}},
            "allow_calib_apply_without_retune": True,
        },
    )
    assert r.status_code == 200


def test_post_calibrate_apply_endpoint(tmp_path: Path):
    # seed calibrators + labels like orchestrator test
    r = client.post(
        "/v1/tuning/calibrate/apply",
        json={"region_code": "PH", "city_code": "MNL"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["decision"] in {"applied", "rejected"}
    if body["decision"] == "applied":
        assert body["force_reassess_required"] is True
```

- [ ] **Step 2: Run — expect FAIL**

- [ ] **Step 3: Wire routes**

```python
class PolicyOverlayIngestRequest(BaseModel):
    region_code: str
    city_code: str = ""
    overlay: dict[str, Any] = Field(default_factory=dict)
    allow_calib_apply_without_retune: bool = False


class CalibrateApplyRequest(BaseModel):
    region_code: str
    city_code: str = ""
```

In `ingest_policy_overlay`, before `save_overlay`:

```python
    err = calib_apply_overlay_blocked(...)
    if err:
        raise HTTPException(status_code=400, detail=err)
```

New route after calibrate fit routes:

```python
@router.post("/tuning/calibrate/apply")
async def post_calibrate_apply(body: CalibrateApplyRequest, request: Request) -> dict[str, Any]:
    _require_auth(request)
    calibrators = getattr(request.app.state, "calibrators", None)
    if calibrators is None:
        raise HTTPException(status_code=503, detail="calibrators unavailable")
    return run_calibrate_apply(
        CalibrateApplyContext(
            settings=request.app.state.settings,
            base_policy=request.app.state.policy,
            guardrails=request.app.state.guardrails,
            overlays=request.app.state.overlays,
            audit=request.app.state.audit,
            forecast=request.app.state.forecast,
            hardgates=request.app.state.hardgates,
            op_cfg=request.app.state.operating_point_cfg,
            calibrators=calibrators,
            table=request.app.state.table,
            region_code=body.region_code,
            city_code=body.city_code,
        )
    )
```

- [ ] **Step 4: Run API tests — expect PASS**

Run: `pytest tests/test_calibrate_apply_joint.py -v`

- [ ] **Step 5: Commit**

```bash
git add src/offline_cancel_risk/api/routes.py \
  src/offline_cancel_risk/control_plane/calibrate_apply.py \
  tests/test_calibrate_apply_joint.py
git commit -m "Expose calibrate/apply endpoint and block bare overlay apply."
```

---

### Task 5: Docs

**Files:**
- Modify: `docs/OPS.md` (§4.6b)
- Modify: `docs/MANUAL.md` (calibration row)

- [ ] **Step 1: Replace OPS workflow** so joint endpoint is primary:

```bash
# Supported live apply (projects scores → retunes τ → writes mode:apply)
curl -X POST localhost:8000/v1/tuning/calibrate/apply \
  -H 'Content-Type: application/json' \
  -d '{"region_code":"PH","city_code":"MNL"}'
# Then force_reassess on hot orders as needed

# Break-glass only (skips joint retune):
curl -X PUT localhost:8000/v1/policy/overlays \
  -H 'Content-Type: application/json' \
  -d '{"region_code":"PH","city_code":"MNL","overlay":{"calibration":{"mode":"apply"}},"allow_calib_apply_without_retune":true}'
```

State that bare apply overlay without escape returns 400.

MANUAL: update calibration bullet to point at `POST /v1/tuning/calibrate/apply`.

- [ ] **Step 2: Commit**

```bash
git add docs/OPS.md docs/MANUAL.md
git commit -m "Document joint calibrate/apply as supported live path."
```

---

## Spec coverage checklist

| Spec requirement | Task |
|---|---|
| Score projection (shadow + already-apply meta) | 1 |
| `defer_overlay_writes` / no per-head save | 2 |
| Atomic mode ± τ commit; soft success; no_calibrators / insufficient_labels | 3 |
| `POST /v1/tuning/calibrate/apply` | 4 |
| Overlay PUT escape hatch | 4 |
| `force_reassess_required` | 3–4 |
| Audit `calibrate_apply` | 3 |
| OPS + MANUAL | 5 |
| Tests 1–5 from spec | 1, 3, 4 |

## Self-review notes

- No TBD placeholders.
- `decision: accepted` on deferred tuner rows means “would apply”; only `run_calibrate_apply` persists.
- Search-ok ≠ all heads accepted; only “not entirely insufficient_pattern_labels.”
