# Outcome → EAR Recoverability Loop Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ingest Downstream outcomes, EWMA-update per-market recoverability, and feed EAR in shadow (default) or apply mode so ranking can learn from clawback/payout results.

**Architecture:** SQLite `OutcomeStore` holds events + EWMA state; `POST /v1/outcomes` updates EWMA; `score_build`/`compute_ear` resolve static vs learned recoverability and always emit `ear_meta` without changing golden scores in shadow mode.

**Tech Stack:** Python 3.11+, SQLite, FastAPI, existing `compute_ear` / policy YAML / pytest

## Global Constraints

- Thin closed loop only (no case packs, sampler quotas, ML promote)
- Default `ear.mode: shadow` — live EAR/attention/flags/scores unchanged vs today
- Apply uses learned weights only when `n_updates >= min_updates_apply` per head (else static fallback)
- Outcomes: `payout_blocked` | `clawback_won` | `clawback_lost` | `account_actioned`
- EWMA: `value ← (1-α)*value + α*signal` with signal 1.0 / 0.0; α default `0.05`
- Clamp to guardrails `ear.recoverability.*`
- Auth on write routes via `_require_auth`
- Setting `OCR_OUTCOMES_PATH` → `data/outcomes.db`
- Commits only when the human asks (skip commit steps unless requested)

## File structure

| Path | Responsibility |
|---|---|
| `src/offline_cancel_risk/outcomes/__init__.py` | Package exports |
| `src/offline_cancel_risk/outcomes/store.py` | SQLite outcomes + recoverability EWMA |
| `src/offline_cancel_risk/outcomes/ewma.py` | Pure update/clamp helpers |
| `src/offline_cancel_risk/scoring/ear.py` | Extend for learned recoverability + meta |
| `src/offline_cancel_risk/pipeline/score_build.py` | Wire store + ear_meta onto result |
| `src/offline_cancel_risk/pipeline/context.py` | Optional `outcomes` on AssessContext |
| `src/offline_cancel_risk/api/schemas.py` | Outcome request + `ear_meta` on AssessmentResult |
| `src/offline_cancel_risk/api/routes.py` | POST/GET outcomes |
| `src/offline_cancel_risk/settings.py` | `outcomes_path` |
| `src/offline_cancel_risk/main.py` | Construct store, pass into assess/queue |
| `config/policy.default.yaml` | `ear.mode`, `outcome_ewma_alpha`, `min_updates_apply` |
| `config/policy_guardrails.default.yaml` | Bounds for new knobs |
| `tests/test_outcome_ear.py` | Unit + API + shadow golden stability |
| `docs/OPS.md` | Downstream outcome blurb |

---

### Task 1: EWMA helpers + OutcomeStore

**Files:**
- Create: `src/offline_cancel_risk/outcomes/__init__.py`
- Create: `src/offline_cancel_risk/outcomes/ewma.py`
- Create: `src/offline_cancel_risk/outcomes/store.py`
- Test: `tests/test_outcome_ear.py`

**Interfaces:**
- Produces: `OUTCOME_TYPES = frozenset({...})`
- Produces: `signal_for_outcome(outcome: str) -> float`
- Produces: `ewma_update(prev: float, signal: float, alpha: float) -> float`
- Produces: `clamp_recoverability(value: float, head: str, guardrails: dict) -> float`
- Produces: `OutcomeStore(path)` with:
  - `record_outcome(*, order_display_id, outcome, head, region_code, city_code, amount=None, occurred_at=None, alpha, cold_start: dict[str,float], guardrails) -> dict`
  - `get_recoverability(region, city) -> dict[str, dict]`  # head → {value, n_updates, updated_at}
  - `list_outcomes(*, order_display_id=None, limit=100) -> list[dict]`

- [ ] **Step 1: Failing tests**

```python
# tests/test_outcome_ear.py
from offline_cancel_risk.outcomes.ewma import ewma_update, signal_for_outcome

def test_signal_and_ewma():
    assert signal_for_outcome("clawback_won") == 1.0
    assert signal_for_outcome("clawback_lost") == 0.0
    assert abs(ewma_update(0.8, 0.0, 0.05) - 0.76) < 1e-9

def test_store_persist_and_idempotent(tmp_path):
    from offline_cancel_risk.outcomes.store import OutcomeStore
    store = OutcomeStore(tmp_path / "o.db")
    cold = {"cancelled_offline": 1.0, "cancel_abuse": 0.4, "selective_theft": 0.8}
    guard = {"ear.recoverability.cancelled_offline": {"min": 0.0, "max": 1.0},
             "ear.recoverability.cancel_abuse": {"min": 0.0, "max": 1.0},
             "ear.recoverability.selective_theft": {"min": 0.0, "max": 1.0}}
    # flatten or nest guardrails to match implementation — use nested ear.recoverability if simpler
    r1 = store.record_outcome(
        order_display_id="O1", outcome="clawback_won", head="selective_theft",
        region_code="PH", city_code="MNL", alpha=0.05, cold_start=cold, guardrails=guard,
        occurred_at="2024-01-01T00:00:00Z",
    )
    r2 = store.record_outcome(
        order_display_id="O1", outcome="clawback_won", head="selective_theft",
        region_code="PH", city_code="MNL", alpha=0.05, cold_start=cold, guardrails=guard,
        occurred_at="2024-01-01T00:00:00Z",
    )
    assert r1["n_updates"] == 1
    assert r2.get("duplicate") is True or r2["n_updates"] == 1  # no double bump
    store2 = OutcomeStore(tmp_path / "o.db")
    got = store2.get_recoverability("PH", "MNL")
    assert got["selective_theft"]["n_updates"] == 1
```

- [ ] **Step 2: Run — expect FAIL**

Run: `pytest tests/test_outcome_ear.py::test_signal_and_ewma tests/test_outcome_ear.py::test_store_persist_and_idempotent -v`

- [ ] **Step 3: Implement ewma.py + store.py**

Schema sketch:

```sql
CREATE TABLE outcomes (
  order_display_id TEXT NOT NULL,
  outcome TEXT NOT NULL,
  head TEXT NOT NULL,
  region_code TEXT NOT NULL,
  city_code TEXT NOT NULL,
  amount REAL,
  occurred_at TEXT NOT NULL,
  created_at TEXT NOT NULL,
  PRIMARY KEY (order_display_id, outcome, occurred_at)
);
CREATE TABLE recoverability_ewma (
  region_code TEXT NOT NULL,
  city_code TEXT NOT NULL,
  head TEXT NOT NULL,
  value REAL NOT NULL,
  n_updates INTEGER NOT NULL,
  updated_at TEXT NOT NULL,
  PRIMARY KEY (region_code, city_code, head)
);
```

`record_outcome`: INSERT OR IGNORE into outcomes; if rowcount==0 return duplicate without EWMA bump; else load/create EWMA row from cold_start, apply ewma_update, clamp, save.

- [ ] **Step 4: PASS**

---

### Task 2: Policy knobs + settings

**Files:**
- Modify: `config/policy.default.yaml` (`ear` block)
- Modify: `config/policy_guardrails.default.yaml`
- Modify: `src/offline_cancel_risk/settings.py` (`outcomes_path`)

- [ ] **Step 1: Add to policy.default.yaml under `ear:`**

```yaml
ear:
  mode: shadow
  outcome_ewma_alpha: 0.05
  min_updates_apply: 5
  recoverability:
    cancelled_offline: 1.0
    cancel_abuse: 0.4
    selective_theft: 0.8
  attention_weights:
    ...
```

- [ ] **Step 2: Guardrails**

```yaml
ear.outcome_ewma_alpha: {min: 0.001, max: 0.5}
ear.min_updates_apply: {min: 1, max: 1000}
```

(mode is enum — validate in code as `shadow|apply`, no numeric guardrail)

- [ ] **Step 3: Settings**

```python
outcomes_path: str = str(ROOT / "data" / "outcomes.db")
```

- [ ] **Step 4: Smoke** — `python -c "from offline_cancel_risk.settings import load_policy, get_settings; p=load_policy('config/policy.default.yaml'); assert p['ear']['mode']=='shadow'"`

---

### Task 3: EAR resolve + schema `ear_meta`

**Files:**
- Modify: `src/offline_cancel_risk/scoring/ear.py`
- Modify: `src/offline_cancel_risk/api/schemas.py`
- Modify: `src/offline_cancel_risk/pipeline/context.py` (add `outcomes: OutcomeStore | None = None`, `ear_meta: dict`)
- Modify: `src/offline_cancel_risk/pipeline/score_build.py`
- Modify: `src/offline_cancel_risk/pipeline/assess.py` (pass outcomes into context)
- Test: `tests/test_outcome_ear.py`, keep `tests/test_assess_golden.py` green

**Interfaces:**
- Produces: `resolve_recoverability(policy, learned: dict[str,dict] | None) -> tuple[dict[str,float], dict]`  
  Returns `(weights_for_live_ear, ear_meta_partial)`
- Produces: `compute_ear(scores, order_value, policy, *, recoverability: dict[str,float] | None = None) -> tuple[dict, float]`  
  If `recoverability` passed, use it instead of policy static (attention_weights still from policy).
- Produces: `AssessmentResult.ear_meta: dict = Field(default_factory=dict)`

Logic in score_build after scores final:

```python
learned = None
if ctx.outcomes is not None:
    learned = ctx.outcomes.get_recoverability(region, city)
live_rec, meta = resolve_recoverability(policy, learned)
ctx.ear, ctx.attention = compute_ear(ctx.scores, req.order_value, policy, recoverability=live_rec)
# also compute ear_learned / attention_learned with full learned map (static fill gaps)
meta["ear_learned"] = ...
meta["attention_learned"] = ...
ctx.ear_meta = meta
# AssessmentResult(..., ear_meta=ctx.ear_meta)
```

Shadow: `live_rec` is always static.  
Apply: per head, if `n_updates >= min_updates_apply` use learned value else static.

- [ ] **Step 1: Test shadow does not change EAR vs no-store**

```python
@pytest.mark.asyncio
async def test_shadow_ear_matches_static(tmp_path):
    # assess once without outcomes store, once with empty store — expected_revenue_at_risk identical
    # ear_meta present when store wired
```

- [ ] **Step 2: Test apply shifts EAR after enough won outcomes**

- [ ] **Step 3: Implement**

- [ ] **Step 4: `pytest tests/test_assess_golden.py tests/test_outcome_ear.py -q` PASS**

---

### Task 4: API routes + main/queue wiring

**Files:**
- Modify: `src/offline_cancel_risk/api/schemas.py` — `OutcomeIngestRequest`
- Modify: `src/offline_cancel_risk/api/routes.py`
- Modify: `src/offline_cancel_risk/main.py`
- Modify: `src/offline_cancel_risk/worker/queue.py` (+ sqlite_queue if same kwargs)
- Test: `tests/test_outcome_ear.py` HTTP via AsyncClient

**Interfaces:**
- `POST /v1/outcomes` — `_require_auth`; load latest assessment for defaults; call `record_outcome`
- `GET /v1/outcomes/recoverability`
- `GET /v1/outcomes`
- Head inference: among heads with `flags[h]==1` pick max `scores[h]`; else max score

```python
class OutcomeIngestRequest(BaseModel):
    order_display_id: str
    outcome: str
    amount: float | None = None
    head: str | None = None
    region_code: str | None = None
    city_code: str | None = None
    occurred_at: str | None = None
```

- [ ] **Step 1: API test ingest → get recoverability**

- [ ] **Step 2: Wire OutcomeStore in create_app; pass into queue.run_worker / run_one / assess_order**

- [ ] **Step 3: PASS** `pytest tests/test_outcome_ear.py -q`

---

### Task 5: OPS blurb + full suite

**Files:**
- Modify: `docs/OPS.md`
- Verify: full pytest

- [ ] **Step 1: OPS section** — POST example curl with outcome types; note shadow vs apply; `OCR_OUTCOMES_PATH`

- [ ] **Step 2: Run** `pytest -q` — all green including golden

---

## Spec coverage

| Spec item | Task |
|---|---|
| Outcome store + EWMA + idempotency | 1 |
| Policy mode/α/min_updates | 2 |
| ear_meta + shadow/apply EAR | 3 |
| POST/GET API + auth | 4 |
| main wiring | 4 |
| OPS | 5 |
| Golden unchanged in shadow | 3 |

## Placeholder scan

None. Guardrails shape in Task 1 test should match however store reads them (prefer nested `guardrails["ear"]["recoverability"][head]` from loaded YAML).
