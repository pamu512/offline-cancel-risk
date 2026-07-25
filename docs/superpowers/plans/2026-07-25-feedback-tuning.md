# Feedback Tuning Control Plane Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the labeled-feedback loop with supply-aware precision/recall operating points, hardgated threshold auto-apply to market overlays, and an append-only audit log — Downstream still owns enforcement.

**Architecture:** A `control_plane` package owns SQLite stores (forecast, hardgates, label metrics, audit), an operating-point resolver (supply_ratio → P/R bands), a metrics job (join assessments ↔ feedback → F1), and a tuner that searches thresholds inside guardrails ∩ operating-point ∩ volume caps then auto-applies via existing `PolicyOverlayStore`. FastAPI routes expose ingest/read; no Product FE.

**Tech Stack:** Python 3.11+, FastAPI, SQLite, PyYAML, pytest, existing `offline_cancel_risk.policy` + `scoring.policy.apply_thresholds`.

## Global Constraints

- Downstream owns enforce/clawback; this service never calls payout/suspend APIs.
- Auto-apply only through `save_overlay` + `policy_guardrails` validation.
- Peak (low supply_ratio) → higher min precision / lower min recall; surplus → higher min recall / lower min precision; both hardgated.
- Local ops hardgates cap projected flag volume per `hour`/`day`/`week`.
- Every forecast ingest, hardgate ingest, metrics snapshot, suggest, apply, reject, clawback signal is audited.
- Prefer one SQLite: `data/control_plane.db` for control-plane tables.
- Spec: `docs/superpowers/specs/2026-07-25-feedback-tuning-design.md`.

## File map

| File | Responsibility |
|---|---|
| `config/operating_point.default.yaml` | Peak/surplus P/R anchors |
| `src/offline_cancel_risk/control_plane/audit.py` | Append-only audit log |
| `src/offline_cancel_risk/control_plane/forecast.py` | Supply forecast store |
| `src/offline_cancel_risk/control_plane/hardgates.py` | Enforcement volume caps + clawback TTL |
| `src/offline_cancel_risk/control_plane/operating_point.py` | `supply_ratio` → P/R bounds |
| `src/offline_cancel_risk/control_plane/metrics.py` | Label join + confusion matrix |
| `src/offline_cancel_risk/control_plane/tuner.py` | Constrained threshold search + apply |
| `src/offline_cancel_risk/control_plane/__init__.py` | Public exports |
| `src/offline_cancel_risk/adapters/publishers.py` | Add `list_feedback`, `list_latest_assessments` |
| `src/offline_cancel_risk/settings.py` | Control-plane paths + tuner knobs |
| `src/offline_cancel_risk/main.py` | Wire stores onto `app.state` |
| `src/offline_cancel_risk/api/routes.py` | New control-plane routes |
| `scripts/compute_label_metrics.py` | CLI metrics |
| `scripts/run_tuner.py` | CLI tune cycle |
| `README.md` | Short control-plane section |
| Tests under `tests/test_control_*.py` | Per-task coverage |

---

### Task 1: Audit log store

**Files:**
- Create: `src/offline_cancel_risk/control_plane/__init__.py`
- Create: `src/offline_cancel_risk/control_plane/audit.py`
- Create: `tests/test_control_audit.py`
- Modify: `src/offline_cancel_risk/settings.py`

**Interfaces:**
- Produces: `PolicyAuditLog(sqlite_path)`, `append(...)`, `list_entries(*, limit=100, action=None) -> list[dict]`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_control_audit.py
from pathlib import Path
from offline_cancel_risk.control_plane.audit import PolicyAuditLog

def test_append_and_list(tmp_path: Path):
    log = PolicyAuditLog(tmp_path / "cp.db")
    aid = log.append(
        actor="tuner",
        action="suggest",
        region_code="PH",
        city_code="MNL",
        before={"thresholds": {"cancelled_offline": 0.75}},
        after={"thresholds": {"cancelled_offline": 0.8}},
        metrics_before={"f1": 0.7},
        metrics_after={"f1": 0.72},
        constraints={"min_precision": 0.8},
        decision="accepted",
        reason="f1_lift",
    )
    rows = log.list_entries(limit=10)
    assert len(rows) == 1
    assert rows[0]["audit_id"] == aid
    assert rows[0]["action"] == "suggest"
    assert rows[0]["region_code"] == "PH"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_control_audit.py -v`  
Expected: FAIL import / module missing

- [ ] **Step 3: Minimal implementation**

```python
# settings.py — add:
control_plane_sqlite_path: str = str(ROOT / "data" / "control_plane.db")
operating_point_path: str = str(ROOT / "config" / "operating_point.default.yaml")
tuner_min_labeled: int = 30
tuner_cooldown_minutes: int = 60
tuner_min_f1_lift: float = 0.01

# control_plane/audit.py — SQLite table policy_audit_log per spec §5.4;
# append generates uuid4 hex audit_id + UTC ts; JSON-serialize dict fields.
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_control_audit.py -v`  
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/offline_cancel_risk/control_plane tests/test_control_audit.py src/offline_cancel_risk/settings.py
git commit -m "feat: add append-only policy audit log store"
```

---

### Task 2: Forecast + hardgates + operating point

**Files:**
- Create: `config/operating_point.default.yaml`
- Create: `src/offline_cancel_risk/control_plane/forecast.py`
- Create: `src/offline_cancel_risk/control_plane/hardgates.py`
- Create: `src/offline_cancel_risk/control_plane/operating_point.py`
- Create: `tests/test_control_operating_point.py`

**Interfaces:**
- Consumes: `PolicyAuditLog.append` (optional on upsert)
- Produces:
  - `SupplyForecastStore.upsert(rows: list[dict])`, `.active(region, city, at_ts) -> dict | None`
  - `EnforcementHardgateStore.upsert(...)`, `.get(region, city) -> dict[str, dict]`
  - `EnforcementHardgateStore.record_clawback(region, city, *, ttl_minutes, reason)`
  - `resolve_operating_point(cfg, supply_ratio: float | None) -> dict` with keys `min_precision`, `min_recall`, `max_precision`, `max_recall`, `regime`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_control_operating_point.py
from pathlib import Path
from offline_cancel_risk.control_plane.forecast import SupplyForecastStore
from offline_cancel_risk.control_plane.hardgates import EnforcementHardgateStore
from offline_cancel_risk.control_plane.operating_point import resolve_operating_point
from offline_cancel_risk.settings import load_policy

def test_peak_has_higher_min_precision_than_surplus():
    cfg = load_policy("config/operating_point.default.yaml")
    peak = resolve_operating_point(cfg, 0.7)
    surplus = resolve_operating_point(cfg, 1.5)
    assert peak["min_precision"] > surplus["min_precision"]
    assert surplus["min_recall"] > peak["min_recall"]
    assert peak["regime"] == "peak"
    assert surplus["regime"] == "surplus"

def test_forecast_and_hardgates_roundtrip(tmp_path: Path):
    fs = SupplyForecastStore(tmp_path / "cp.db")
    fs.upsert([{
        "region_code": "PH", "city_code": "MNL",
        "period_start": "2026-07-25T00:00:00Z",
        "period_end": "2026-07-26T00:00:00Z",
        "forecast_supply": 80.0, "forecast_demand": 100.0,
        "source": "driver_ops",
    }])
    row = fs.active("PH", "MNL", "2026-07-25T12:00:00Z")
    assert row is not None
    assert abs(row["forecast_supply"] / row["forecast_demand"] - 0.8) < 1e-9
    hg = EnforcementHardgateStore(tmp_path / "cp.db")
    hg.upsert("PH", "MNL", window="hour", max_enforcements=10, heads=["*"], actor="ops")
    assert hg.get("PH", "MNL")["hour"]["max_enforcements"] == 10
```

- [ ] **Step 2: Run tests — expect FAIL**

Run: `pytest tests/test_control_operating_point.py -v`

- [ ] **Step 3: Implement**

`operating_point.default.yaml` — copy anchors from spec §6.  
`resolve_operating_point`: if `supply_ratio is None` use `fallback_when_no_forecast`; if `<= peak.ratio` return peak bounds; if `>= surplus.ratio` return surplus; else piecewise-linear interpolate the four bounds between anchors.  
Stores: schema per spec §5.1–5.2; normalize region/city uppercase; clawback sets temporary effective tighter caps or `clawback_until` timestamp read by tuner.

- [ ] **Step 4: Pass tests**

Run: `pytest tests/test_control_operating_point.py -v`  
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add config/operating_point.default.yaml src/offline_cancel_risk/control_plane tests/test_control_operating_point.py
git commit -m "feat: add supply forecast, hardgates, and operating-point resolver"
```

---

### Task 3: Publisher list helpers + label metrics

**Files:**
- Modify: `src/offline_cancel_risk/adapters/publishers.py`
- Create: `src/offline_cancel_risk/control_plane/metrics.py`
- Create: `tests/test_control_metrics.py`

**Interfaces:**
- Produces:
  - `SqliteTablePublisher.list_feedback() -> list[dict]` with `order_display_id`, `labels`, `created_at`
  - `SqliteTablePublisher.list_latest_assessments() -> list[AssessmentResult]` (max generation per order)
  - `compute_label_metrics(assessments, feedback, *, thresholds, region_code='', city_code='') -> list[dict]` one per head with tp/fp/fn/tn/precision/recall/f1/support
  - `LabelMetricsStore(sqlite_path).save_snapshots(rows)`, `.latest(...) -> list[dict]`

- [ ] **Step 1: Write failing test**

```python
from offline_cancel_risk.control_plane.metrics import compute_label_metrics

def test_compute_f1_offline_head():
    assessments = [{
        "order_display_id": "A",
        "region_code": "PH",
        "city_code": "MNL",
        "scores": {"cancelled_offline": 0.9, "cancel_abuse": 0.1, "selective_theft": 0.1},
    }, {
        "order_display_id": "B",
        "region_code": "PH",
        "city_code": "MNL",
        "scores": {"cancelled_offline": 0.2, "cancel_abuse": 0.1, "selective_theft": 0.1},
    }]
    feedback = [
        {"order_display_id": "A", "labels": {"cancelled_offline": 1, "cancel_abuse": 0, "selective_theft": 0}},
        {"order_display_id": "B", "labels": {"cancelled_offline": 0, "cancel_abuse": 0, "selective_theft": 0}},
    ]
    rows = compute_label_metrics(
        assessments, feedback, thresholds={"cancelled_offline": 0.75, "cancel_abuse": 0.75, "selective_theft": 0.75},
        region_code="PH", city_code="MNL",
    )
    offline = next(r for r in rows if r["head"] == "cancelled_offline")
    assert offline["tp"] == 1 and offline["tn"] == 1
    assert offline["f1"] == 1.0
    assert offline["support"] == 2
```

- [ ] **Step 2: Run — expect FAIL**

Run: `pytest tests/test_control_metrics.py::test_compute_f1_offline_head -v`

- [ ] **Step 3: Implement**

Use `apply_thresholds` from `scoring.policy`. Skip labeled rows missing assessment. Filter by market when codes non-empty. Precision/recall: if denom 0 → 0.0; F1: if P+R==0 → 0.0. Persist snapshots with uuid + `computed_at`.

Add publisher helpers reading SQLite `feedback` and latest assessment per `order_display_id` (ORDER BY assessment_generation DESC, pick first per id).

- [ ] **Step 4: Pass**

Run: `pytest tests/test_control_metrics.py -v`

- [ ] **Step 5: Commit**

```bash
git add src/offline_cancel_risk/adapters/publishers.py src/offline_cancel_risk/control_plane/metrics.py tests/test_control_metrics.py
git commit -m "feat: compute per-head precision recall F1 from labeled feedback"
```

---

### Task 4: Constrained tuner + auto-apply

**Files:**
- Create: `src/offline_cancel_risk/control_plane/tuner.py`
- Create: `tests/test_control_tuner.py`

**Interfaces:**
- Consumes: `resolve_operating_point`, `LabelMetricsStore`/`compute_label_metrics`, `EnforcementHardgateStore`, `PolicyOverlayStore`, `save_overlay`, `PolicyAuditLog`, guardrails, base policy
- Produces: `run_tuner(ctx: TunerContext) -> list[dict]` decisions (`suggest`/`apply`/`reject` per head/market)

`TunerContext` fields: `base_policy`, `guardrails`, `overlays`, `audit`, `forecast`, `hardgates`, `op_cfg`, `assessments`, `feedback`, `settings` (min_labeled, cooldown, min_f1_lift), `region_code`, `city_code`.

- [ ] **Step 1: Write failing tests**

```python
def test_tuner_rejects_when_below_min_precision(tmp_path):
    # Arrange labeled set where low threshold gets high recall but precision < peak min
    # Active forecast with supply_ratio in peak band
    # run_tuner → decision reject reason contains below_min_precision or similar
    ...

def test_tuner_applies_overlay_when_f1_improves_inside_gates(tmp_path):
    # Arrange scores separable by threshold; surplus or mid regime with room
    # run_tuner → overlays.get(region, city) has new threshold; audit has action=apply
    ...

def test_tuner_rejects_when_hourly_hardgate_breached(tmp_path):
    # Hardgate hour max_enforcements=0; any positive flag projection → reject
    ...
```

Fill the three tests with concrete fixtures (reuse pattern from `tests/test_control_metrics.py` + forecast/hardgate stores). Projected volume = count of assessments in market with score≥candidate threshold for the head, compared to `max_enforcements` for `hour` (use full recent list as the hour window in tests).

- [ ] **Step 2: Run — expect FAIL**

Run: `pytest tests/test_control_tuner.py -v`

- [ ] **Step 3: Implement `run_tuner`**

For each head in `cancelled_offline`, `cancel_abuse`, `selective_theft`:
1. Resolve forecast → ratio → operating point; if clawback active, bias toward peak band (use peak bounds).
2. Skip if labeled support `< tuner_min_labeled` → audit reject `insufficient_labels`.
3. Current threshold from `resolved_policy_for_market`.
4. Grid-search candidate thresholds from guardrail min→max step 0.01 (or 0.05 for speed).
5. For each candidate: metrics on labeled set; check P/R in band; check projected flags ≤ hardgates; track best F1 (tie-break: peak→precision, surplus→recall).
6. If best improves F1 by `>= tuner_min_f1_lift` vs current and cooldown OK → `save_overlay` + audit `apply`; else audit `reject` with reason.

Cooldown: read last `apply` audit for market/head within `tuner_cooldown_minutes`.

- [ ] **Step 4: Pass**

Run: `pytest tests/test_control_tuner.py -v`

- [ ] **Step 5: Commit**

```bash
git add src/offline_cancel_risk/control_plane/tuner.py tests/test_control_tuner.py
git commit -m "feat: add hardgated threshold tuner with auto-apply overlays"
```

---

### Task 5: API routes + app wiring

**Files:**
- Modify: `src/offline_cancel_risk/main.py`
- Modify: `src/offline_cancel_risk/api/routes.py`
- Create: `tests/test_control_api.py`

**Interfaces:**
- `app.state.audit`, `.forecast`, `.hardgates`, `.label_metrics`, `.operating_point_cfg`
- Routes per spec §9

- [ ] **Step 1: Write API tests**

```python
@pytest.mark.asyncio
async def test_forecast_hardgate_metrics_tune_audit_flow(tmp_path):
    # create_app with settings pointing control_plane + assessments dbs into tmp_path
    # PUT forecast, PUT hardgates, seed assessments+feedback via table helpers
    # POST /v1/tuning/run
    # GET /v1/metrics/labels → 200 with heads
    # GET /v1/audit/policy → contains recorded actions
    # POST /v1/enforcement/clawback → 200
```

- [ ] **Step 2: Run — expect FAIL**

Run: `pytest tests/test_control_api.py -v`

- [ ] **Step 3: Wire main + routes**

Instantiate stores from `settings.control_plane_sqlite_path`.  
`POST /v1/tuning/run` loads feedback + latest assessments from `app.state.table`, runs metrics save + `run_tuner`, returns decisions.  
Manual `PUT /v1/policy/overlays` also `audit.append(actor="manual_overlay", action="apply", ...)`.  
All new routes call `_require_auth`.

- [ ] **Step 4: Pass + full suite**

Run: `pytest tests/test_control_api.py tests/test_policy_overlays.py -q` then `pytest -q`  
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add src/offline_cancel_risk/main.py src/offline_cancel_risk/api/routes.py tests/test_control_api.py
git commit -m "feat: expose supply, hardgate, metrics, tuner, and audit APIs"
```

---

### Task 6: CLIs + README

**Files:**
- Create: `scripts/compute_label_metrics.py`
- Create: `scripts/run_tuner.py`
- Modify: `README.md`
- Create: `tests/test_control_scripts_smoke.py` (import/run main with tmp paths or subprocess `--help` if argparse)

- [ ] **Step 1: Smoke test that scripts import and `main()` returns 0 on empty DB**

- [ ] **Step 2: Implement CLIs** reading `OCR_*` / Settings; print JSON summary

- [ ] **Step 3: README section** documenting forecast ingest, hardgates, tuning run, audit — note Product owns FE, Downstream owns enforce

- [ ] **Step 4: `pytest -q` green**

- [ ] **Step 5: Commit**

```bash
git add scripts/compute_label_metrics.py scripts/run_tuner.py README.md tests/test_control_scripts_smoke.py
git commit -m "docs: add feedback-tuning CLIs and README control-plane section"
```

---

## Spec coverage checklist

| Spec section | Task |
|---|---|
| §5.4 Audit log | T1 |
| §5.1–5.2 Forecast + hardgates | T2 |
| §6 Operating point | T2 |
| §5.3 + §7 Metrics | T3 |
| §8 Tuner + clawback ingest | T4 + T5 |
| §9 APIs | T5 |
| §13 T4 CLIs/docs | T6 |
| Publisher join for assessments/feedback | T3 |

## Self-review notes

- No TBD placeholders in tasks.
- Types consistent: `resolve_operating_point` → tuner constraints; `save_overlay` for apply.
- Volume projection uses assessments list (test-simple hour window); documented in T4.
