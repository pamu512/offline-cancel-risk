# Phase 2+ Control Plane & Hardening — Design Addendum

**Date:** 2026-07-25  
**Status:** Approved for planning (sideload C+ONNX, promote-ready + canary)  
**Parent:** `docs/superpowers/specs/2026-07-25-offline-cancel-risk-design.md`  
**Builds on:** Phase 0–1 MVP (`v0.1.0-mvp`)

## 1. Goal

Extend the reusable toolkit so ops and eng can:

1. Sideload challenger models and **shadow** them on the same assess pipeline as the champion.
2. Automatically flag **`promotion_ready`**, run a **canary**, then promote or rollback.
3. Tune policy with a **threshold simulator** and prove value with **$ at risk / FP$ backtests**.
4. Improve detection (sequence, late re-assess, cross-order chains).
5. Harden OSS/prod packaging (Docker, adapters, auth, real queue, stream bindings).

## 2. Decisions log (this addendum)

| Decision | Choice |
|---|---|
| Model package | Directory bundle: artifact + `feature_schema.json` + `metrics_baseline.json` + checksum |
| Artifact formats | **joblib and ONNX** (`format` in `model.json`) |
| Shadow | Same pipeline; challenger scores recorded, does not serve flags until canary/promote |
| Promote signal | Service sets `promotion_ready=1` when gates pass |
| Promote action | **Canary then full** — not flag-only, not instant full auto |
| Canary defaults | `canary_pct=5`, `canary_hours=24` (configurable) |
| Canary failure | Auto-rollback to champion; clear `promotion_ready`; keep shadow metrics for forensics |
| Enforcement | Still none — scores/flags only |

## 3. Model bundle contract

```text
model_bundle/
  model.json              # id, format: joblib|onnx, heads, created_at, checksums
  model.joblib            # XOR with model.onnx (format selects)
  model.onnx              # optional if format=onnx
  feature_schema.json     # ordered feature names + dtypes; reject on drift
  metrics_baseline.json   # training/holdout baselines for gate comparison
```

`model.json` (minimum):

```json
{
  "model_id": "challenger_2026_07_25",
  "format": "onnx",
  "heads": ["cancelled_offline", "cancel_abuse", "selective_theft"],
  "feature_schema_version": "v1",
  "checksum_sha256": "...",
  "created_at": "2026-07-25T00:00:00Z"
}
```

Loader interface:

- `load_bundle(path) -> ModelHandle`
- `ModelHandle.predict(features: dict|array) -> dict[str, float]`
- Reject bundle if schema mismatch, checksum fail, missing head, or unsupported format.

## 4. Champion / shadow / canary / promote

```
assess(order)
  features = extract(...)
  rule_scores = rules(features)
  champion = blend(rule, champion_model.predict)
  flags = policy(champion)          # serving path
  for challenger in shadow_models:
      shadow = blend(rule, challenger.predict)
      record_shadow(order, challenger_id, shadow)  # not serving unless canary
  if canary_active and hash(order_id) % 100 < canary_pct:
      flags = policy(canary_model)  # serving canary
      record_canary(...)
  publish result (+ shadow_scores, model_roles)
```

### Promote gates → `promotion_ready`

All configurable; defaults:

| Gate | Default |
|---|---|
| Min shadow assessments | 500 |
| FP$ budget | not worse than champion (≤ +0%) |
| Catch / recall lift | ≥ +2% relative at that FP$ |
| ECE | ≤ champion ECE + 0.02 |
| Feature schema | identical version |
| Adversarial suite | no new failures |

When all pass: emit event + row:

```json
{
  "challenger_model_id": "...",
  "champion_model_id": "...",
  "promotion_ready": 1,
  "promotion_blockers": [],
  "metrics": {},
  "recommended_action": "start_canary"
}
```

### Canary

- On `promotion_ready`, **auto-start canary** (unless `auto_canary=false`).
- Defaults: 5% traffic, 24 hours.
- Re-evaluate gates continuously on canary traffic.
- Pass → full promote (challenger becomes champion; previous champion retained as rollback).
- Fail → rollback serving to champion; challenger returns to shadow or `failed_canary` state.

### API

| Method | Path | Purpose |
|---|---|---|
| POST | `/v1/models` | Sideload bundle (multipart or path ref) |
| GET | `/v1/models` | List models + roles |
| GET | `/v1/models/{id}` | Detail, metrics, `promotion_ready`, blockers |
| POST | `/v1/models/{id}/canary/start` | Manual canary start |
| POST | `/v1/models/{id}/canary/abort` | Abort → rollback |
| POST | `/v1/models/{id}/promote` | Force full promote (authz; optional escape hatch) |
| GET | `/v1/models/compare` | Champion vs challenger report |

Auth required on all model-control routes in prod mode.

## 5. Ops threshold simulator

- Input: historical assessment table (scores already stored) + candidate thresholds/weights.
- Output: projected flag rates, FP$, catch proxy (if labels), slice breakdown.
- No GPS re-fetch required when feature/score history exists.
- CLI + API: `POST /v1/simulate/policy`.

## 6. $ at risk backtest report

Extend `scripts/backtest.py` / add `scripts/value_report.py`:

- Per head and overall: flagged $, catch $, FP$, precision/recall if labels.
- Slices: category, value band, replacement present, gps_expanded.
- Markdown/JSON report for ops.

## 7. Detection upgrades

1. **Sequence weight** — increase offline rule blend weight on `sequence_score` (config).
2. **Late re-assess** — API/worker accepts evidence updates; bump `assessment_generation`; mark prior provisional.
3. **Cross-order chains** — store recent cancel/reassign events by `driver_id`; feed `driver_chain_count` beyond single-order events.

## 8. OSS / prod packaging

- `docker-compose.yml`: API + volume for models + SQLite/JSONL.
- Adapter templates under `adapters/templates/` (Kafka cancels, HTTP GPS, WH sink stubs).
- GitHub Action: publish to PyPI on tag `v*`.
- Examples: 2–3 anonymized case folders with expected scores.

Prod hardening:

- API key / bearer auth for assess + model control.
- Swap in-process queue for Redis/RQ or Kafka consumer (config switch).
- Publisher bindings for Kafka + SQL warehouse (keep JSONL/SQLite as defaults).
- Metrics: assess latency, shadow lag, promote_ready count, canary error rate.

## 9. Phased delivery

| Phase | Scope |
|---|---|
| **2a** | Model registry + joblib/ONNX loaders + shadow on assess + metrics store |
| **2b** | Promote gates + `promotion_ready` + auto canary + rollback |
| **2c** | Threshold simulator + $ value report + Ops README |
| **2d** | Detection upgrades (sequence, late re-assess, driver chains) |
| **2e** | Docker, adapter templates, PyPI action, auth, queue/publisher bindings |

## 10. Success metrics

- Sideload joblib **and** ONNX bundles; schema/checksum rejection works.
- Shadow never changes flags unless order is in canary cohort.
- `promotion_ready` flips only when all gates pass; canary auto-starts.
- Canary failure restores champion with zero manual steps.
- Simulator changes thresholds without re-scoring GPS.
- Value report runs on CSV demo + labeled fixture.
- `docker compose up` + CSV demo path documented.
- Full unit/integration suite green; no enforcement APIs added.

## 11. Open bindings

- Exact ONNX opset / runtime pin (`onnxruntime` version).
- Auth provider (static API keys first).
- Queue backend choice default: Redis if available, else continue in-process with warning in prod mode.
