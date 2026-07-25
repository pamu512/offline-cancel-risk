# Phase 2+ Control Plane Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add model sideload (joblib + ONNX), shadow scoring on the shared assess pipeline, `promotion_ready` gates, auto-canary (5%/24h defaults) with rollback, plus ops simulator, $ value reports, detection upgrades, and OSS/prod packaging.

**Architecture:** Model registry stores champion/shadow/canary roles. `assess_order` always computes rule scores, blends champion for serving flags, records challenger shadow scores, and optionally serves canary traffic. A metrics/gate worker flips `promotion_ready` and drives canary→promote/rollback. Simulators and value reports read stored scores; Docker/auth/queue are config-switched adapters.

**Tech Stack:** Existing MVP stack + `onnxruntime`, `joblib`, `scikit-learn` (already present), optional `redis` for queue later

**Spec:** `docs/superpowers/specs/2026-07-25-phase2-control-plane-design.md`

## Global Constraints

- Serving flags come from champion, except canary cohort when canary active.
- Shadow scores never mutate flags outside canary.
- Bundle formats: `joblib` and `onnx` via `model.json` `format` field.
- Canary defaults: `canary_pct=5`, `canary_hours=24`.
- No enforcement actions (payout/suspend).
- TDD; frequent commits; keep CSV demo working every phase.
- Auth required on `/v1/models*` when `OCR_AUTH_REQUIRED=1`.

---

## Phase 2a — Registry + loaders + shadow

### Task 1: Model bundle schema + joblib/ONNX loaders

**Files:**
- Create: `src/offline_cancel_risk/models/bundle.py`
- Create: `src/offline_cancel_risk/models/loaders.py`
- Create: `tests/test_model_bundle.py`
- Modify: `pyproject.toml` (add `onnxruntime`, `joblib` if missing)

**Interfaces:**
- Produces: `load_bundle(path: Path) -> ModelHandle` with `.model_id`, `.format`, `.predict(features: dict[str, float]) -> dict[str, float]`
- Rejects checksum/schema/format errors with typed exceptions

- [ ] **Step 1: Write failing tests** for joblib bundle load, onnx bundle load (tiny fixture), checksum fail, schema drift fail

- [ ] **Step 2: Run tests — expect FAIL**

- [ ] **Step 3: Implement bundle validation + loaders**

`model.json` fields as in spec. For ONNX tests, create a minimal 3-output identity/linear model fixture under `tests/fixtures/models/`.

- [ ] **Step 4: Pass + commit**

```bash
pytest tests/test_model_bundle.py -v
git commit -m "feat: add joblib and ONNX model bundle loaders"
```

### Task 2: Model registry (SQLite) + roles

**Files:**
- Create: `src/offline_cancel_risk/models/registry.py`
- Test: `tests/test_model_registry.py`

**Interfaces:**
- `Registry.sideload(bundle_path) -> ModelRecord`
- `Registry.set_role(model_id, role)` where role ∈ `champion|shadow|canary|retired|failed_canary`
- `Registry.get_champion()`, `list_shadow()`, `get_canary()`

- [ ] TDD sideload copies bundle into `data/models/{id}/`, registers metadata
- [ ] Only one champion; sideload defaults to `shadow`
- [ ] Commit: `feat: add model registry with champion/shadow roles`

### Task 3: Wire shadow into assess pipeline

**Files:**
- Modify: `src/offline_cancel_risk/pipeline/assess.py`
- Modify: `src/offline_cancel_risk/scoring/blend.py` (actually use ml weights)
- Modify: `src/offline_cancel_risk/api/schemas.py` (optional `shadow_scores`, `model_roles`)
- Test: `tests/test_shadow_assess.py`

- [ ] Champion blends into serving scores/flags
- [ ] Each shadow model records `shadow_scores[model_id]` without affecting flags
- [ ] `model_version` becomes champion id (or `none` if rules-only champion)
- [ ] Commit: `feat: shadow challenger scores on shared assess pipeline`

### Task 4: Shadow metrics store

**Files:**
- Create: `src/offline_cancel_risk/models/metrics.py`
- Test: `tests/test_shadow_metrics.py`

- [ ] Persist per-order champion vs shadow scores + labels if present
- [ ] Aggregate FP$/catch proxies for gates
- [ ] Commit: `feat: persist shadow comparison metrics`

---

## Phase 2b — Promote-ready + canary

### Task 5: Promote gates → `promotion_ready`

**Files:**
- Create: `src/offline_cancel_risk/models/gates.py`
- Create: `config/promote_gates.default.yaml`
- Test: `tests/test_promote_gates.py`

- [ ] Implement gates from spec defaults
- [ ] `evaluate_promotion(challenger_id) -> PromotionStatus`
- [ ] Publish `promotion_ready` row/event when status flips 0→1
- [ ] Commit: `feat: flag promotion_ready when shadow gates pass`

### Task 6: Auto canary + rollback

**Files:**
- Create: `src/offline_cancel_risk/models/canary.py`
- Modify: `assess.py` (canary cohort serving)
- Test: `tests/test_canary.py`

- [ ] On `promotion_ready` + `auto_canary=true`, start canary with pct/hours from config
- [ ] Cohort: `stable_hash(order_display_id) % 100 < canary_pct`
- [ ] Continuous gate check; fail → rollback; success after hours → promote
- [ ] Commit: `feat: auto-canary with rollback and full promote`

### Task 7: Model control API

**Files:**
- Modify: `src/offline_cancel_risk/api/routes.py`
- Test: `tests/test_models_api.py`

- [ ] Implement `/v1/models` CRUD-ish endpoints from spec
- [ ] Auth hook when `OCR_AUTH_REQUIRED=1`
- [ ] Commit: `feat: add model sideload and canary control API`

---

## Phase 2c — Ops simulator + $ report

### Task 8: Threshold simulator

**Files:**
- Create: `src/offline_cancel_risk/control_plane/simulate.py`
- Create: `scripts/simulate_policy.py`
- Modify: routes (`POST /v1/simulate/policy`)
- Test: `tests/test_simulate_policy.py`

- [ ] Recompute flags from stored soft scores + candidate thresholds/weights
- [ ] Return flag rates + FP$ estimate
- [ ] Commit: `feat: add policy threshold simulator`

### Task 9: Value / FP$ report + Ops README

**Files:**
- Create: `scripts/value_report.py`
- Create: `docs/OPS.md`
- Test: `tests/test_value_report.py`

- [ ] Slice report: category, value band, replacement, gps_expanded
- [ ] Document scores, thresholds, promote/canary for ops
- [ ] Commit: `docs: add ops guide and value report CLI`

---

## Phase 2d — Detection upgrades

### Task 10: Sequence weight + late re-assess

**Files:**
- Modify: `scoring/rules.py`, `config/policy.default.yaml`
- Modify: `pipeline/assess.py`, `api/routes.py` (`POST /v1/assess/{id}/refresh`)
- Test: `tests/test_late_reassess.py`

- [ ] Configurable sequence weight in offline rule
- [ ] Refresh bumps `assessment_generation`, sets prior provisional
- [ ] Commit: `feat: sequence-weighted offline score and late re-assess`

### Task 11: Cross-order driver chains

**Files:**
- Create: `src/offline_cancel_risk/features/driver_history.py`
- Modify: `assess.py`, publishers/table
- Test: `tests/test_driver_chains.py`

- [ ] Persist cancel/reassign events by driver
- [ ] Feed rolling `driver_chain_count` into abuse features
- [ ] Commit: `feat: cross-order driver cancel chain features`

---

## Phase 2e — OSS / prod packaging

### Task 12: Docker Compose + adapter templates

**Files:**
- Create: `Dockerfile`, `docker-compose.yml`
- Create: `adapters/templates/README.md` + stub modules
- Modify: root `README.md`

- [ ] `docker compose up` serves API with mounted `data/models`
- [ ] Templates for Kafka cancels / HTTP GPS / WH sink
- [ ] Commit: `chore: add Docker Compose and adapter templates`

### Task 13: PyPI publish action + auth + queue switch

**Files:**
- Create: `.github/workflows/publish.yml`
- Modify: `settings.py`, `worker/queue.py`, `publishers`
- Test: auth middleware tests

- [ ] Tag `v*` → PyPI (OIDC trusted publishing placeholder)
- [ ] `OCR_AUTH_REQUIRED` + `OCR_API_KEYS`
- [ ] `OCR_QUEUE_BACKEND=memory|redis`
- [ ] Commit: `feat: add auth, queue backend switch, and PyPI workflow`

### Task 14: Phase 2 acceptance gate

- [ ] Full pytest green
- [ ] Sideload joblib + ONNX fixtures; shadow does not change flags
- [ ] Force metrics to trip `promotion_ready`; canary starts; abort rolls back
- [ ] Simulator + value report smoke
- [ ] `docker compose` smoke
- [ ] Tag `v0.2.0-phase2`
- [ ] Commit: `chore: phase 2 acceptance gate passed`

---

## Spec coverage map

| Spec area | Tasks |
|---|---|
| Bundle joblib+ONNX | 1 |
| Registry roles | 2 |
| Shadow on same pipeline | 3–4 |
| promotion_ready gates | 5 |
| Canary 5%/24h + rollback | 6–7 |
| Simulator + $ report | 8–9 |
| Detection upgrades | 10–11 |
| Docker/adapters/PyPI/auth/queue | 12–13 |
| Acceptance | 14 |

## Execution note

Implement **2a → 2b → 2c → 2d → 2e** in order. Do not start canary work before shadow metrics exist. Keep `examples/csv_demo` green after every task group.
