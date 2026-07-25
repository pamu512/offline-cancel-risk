# Offline Cancel Risk Platform — Design Spec

**Date:** 2026-07-25  
**Status:** Draft for review  
**Project:** `~/Projects/offline-cancel-risk`  
**Source baseline:** `offline_multistop_v5_doc` (DBSCAN multistop offline confidence, ~87% accuracy / ~90% precision on 182 manual reviews)

## 1. Problem

Cancelled logistics orders can mask:

1. **Cancelled offline / off-platform completion** — trip fulfilled outside the platform (revenue leakage).
2. **Cancel abuse** — cancel/reassign games that create operational fraud without necessarily completing offline.
3. **Selective theft** — food / high-value goods disappear; next driver reports no order.

These risks are **independent**. Theft is not a subtype of offline. Offline is not implied by abuse.

The v5 notebook proved GPS stop clustering (DBSCAN + radius-tier confidence + round-trip rules) beats naive “points near dropoff” SQL, but it remains a one-off Excel/notebook workflow with hardcoded paths and a single offline score.

## 2. Goals

### Primary users (priority order)

1. Ops / logistics managers — tunable thresholds, $ at risk, market slices.
2. Fraud / risk investigators — rare dispute packs, not a full review queue.
3. Data / eng — async microservice, stream + table features for downstream.
4. Generic logistics tenants — multi-tenant adapters (later phases).

### Product goals

- Async assessment only (never realtime on cancel click).
- Produce risk **features** for downstream; this service does **not** enforce actions (no payout block, suspend, etc.).
- Three independent soft scores + versioned policy flags + attention driven by **expected revenue at risk**.
- Automate human toil for detection; retain a **small predetermined label quota** for ML feedback (bias / uncertainty / disagreement aware).
- Absolute ceiling capability set: trajectory twin, entity graph, multi-signal fusion, adversarial gates, causal replacement, foundation-model track, late re-assessment, immutable ledger.

### Non-goals (all phases)

- Full investigator product UI beyond label tickets, threshold simulator, and dispute case packs.
- Owning / rewriting the LBS GPS platform (integrate existing API).
- Downstream business actions / enforcement inside this service.

**Terminology:** *MVP ship* = Phase 0–1 (rules + contracts + ledger). *Platform complete* = through Phase 4 (absolute ceiling). The output schema is stable from MVP so later phases do not break downstream consumers.

## 3. Success metrics

| Metric | Target |
|---|---|
| Rule-path parity | On historical labeled set (incl. v5 182-review style set), offline-style detection within documented delta of ~87% acc / ~90% prec; dwell+sequence must reduce pass-through FPs vs point-only v5 |
| Contract completeness | Every successful assess writes identical schema to stream + table; idempotent on `(order_display_id, policy_hash, model_version, assessment_generation)` |
| GPS windowing | Expand 3h→24h only on sparse points or coverage gaps, then inconclusive scores; window always recorded |
| Risk independence | Unit fixtures prove theft high + offline low (and inverse) are possible |
| Degradation | GPS down → `gps_unavailable` reasons, no crash; ML down → rule-only |
| Calibration | Per-head expected calibration error (ECE) ≤ 0.05 on holdout after Phase 2; retune if exceeded |
| Shadow promote | Challenger promotes only if metrics improve at fixed FP$ budget |
| Feedback quota | Sampler emits `daily_review_quota` (±1) with required strata mix |
| Late evidence | Re-assessment creates new `assessment_generation`; prior generations retained |
| Adversarial gate | Attack suite must not regress on promote |
| No actions | Service never calls enforcement APIs |

## 4. Architecture

```
order.cancelled (queue) ──┐
batch / assess API ───────┼─► Assessment Worker
label feedback API ───────┘      │
                                 ├─ Order / lineage / context fetch
                                 ├─ GPS adapter (existing LBS API)
                                 │     3h → expand → 24h
                                 ├─ Feature stack
                                 │     trajectory twin, v5 DBSCAN, dwell/speed,
                                 │     lineage, entity graph, multi-signal, spoof checks
                                 ├─ Scorer
                                 │     rule_scores + ml_scores → blend → soft scores
                                 ├─ Policy
                                 │     thresholds → flags; $ at risk → attention_score
                                 └─ Publish
                                       ├─ Risk event stream
                                       ├─ Warehouse / DB table
                                       └─ Label-sample stream (quota only)
```

### Components

| Component | Responsibility |
|---|---|
| `assess-api` | `POST /v1/assess`, batch, job status, latest result, feedback ingest, health |
| `assess-worker` | Queue consumer, batch runner, re-assessment on late evidence |
| `gps-adapter` | Client for existing LBS API; adaptive window; gap/sparsity metrics |
| `features/*` | Twin, DBSCAN v5, lineage, graph, multi-signal, spoof |
| `scoring/*` | Rules, ML, blend, policy, $ at risk |
| `feedback/*` | Bias/uncertainty/disagreement sampler + label store |
| `publishers/*` | Stream + table + label tickets |
| `control-plane` | Threshold simulator, shadow compare, promote gates, drift monitors |
| `policy-config` | Versioned YAML/JSON knobs |
| `models/` | Artifacts or object-store references |

## 5. Data contracts

### 5.1 Assess input (queue or API)

Minimum (enrichment allowed from order service):

- `order_id` / `order_display_id`
- `driver_id` (cancelling / last assigned)
- `cancel_ts`, assign/accept timestamps
- Stop list (`latlong` or structured stops), `path_point_num`
- Post-cancel `order_status` (still active?)
- `category`, `order_value`, currency
- `replacement_order_id?` + replacement metadata (placed_at, stops, status)
- `reassign_cancel_events[]?` (timestamps, actors)
- `next_driver_no_order?` (structured outcome)
- Optional multi-signal payloads: app integrity, POD/seal, IoT

### 5.2 GPS window policy

1. Start with **3 hours** around lifecycle anchor (assign→cancel + buffer).
2. Expand toward **24 hours** if too few points **or** max gap exceeds config.
3. If still weak after max window: assess with `gps_sparse` / `gps_gaps`; scores may remain mid-band (inconclusive for strict thresholds).
4. Persist `{start, end, expanded, point_count, max_gap_minutes}` on every result.

### 5.3 Output schema (stream + table)

```json
{
  "order_display_id": "string",
  "driver_id": 0,
  "scores": {
    "cancelled_offline": 0.0,
    "cancel_abuse": 0.0,
    "selective_theft": 0.0
  },
  "flags": {
    "cancelled_offline": 0,
    "cancel_abuse": 0,
    "selective_theft": 0
  },
  "expected_revenue_at_risk": {
    "cancelled_offline": 0.0,
    "cancel_abuse": 0.0,
    "selective_theft": 0.0,
    "total": 0.0
  },
  "attention_score": 0.0,
  "reasons": ["string"],
  "rule_scores": {},
  "ml_scores": {},
  "gps_window": {},
  "lineage_id": "string",
  "assessment_generation": 1,
  "provisional": true,
  "policy_hash": "string",
  "model_version": "string|none",
  "twin_version": "string",
  "graph_version": "string",
  "feature_vector_ref": "string",
  "assessed_at": "ISO-8601"
}
```

Soft scores are the source of truth. Flags are policy projections. Retuning thresholds can replay flags from stored scores/features without re-fetching GPS when features are complete.

## 6. Risk logic

### 6.1 `cancelled_offline`

Evidence includes:

- Trajectory twin likelihood vs expected route.
- v5-style per-stop confidence (DBSCAN clusters vs noise; 150/400/800m bands + discounts), upgraded with **dwell + speed**.
- **Sequence match**: pickup → mid-stops → drop order, not only mean stop confidence.
- Round-trip rules from v5 (e.g. single pickup visit halves final drop confidence) retained as features/reasons.
- Replacement handling (see 6.4).
- No replacement after cancel → offline candidate lift.

### 6.2 `cancel_abuse`

Evidence (confirmed):

- Driver cancels/drops but order remains active.
- Multiple cancels/reassigns on same order in a short window.
- Same driver repeatedly in cancel→reassign chains.
- Customer/merchant cancel after driver already near destination (GPS).

Explicitly **not** primary abuse evidence: next driver “no order” (that feeds theft).

### 6.3 `selective_theft`

Evidence (confirmed):

- Food delivery category.
- High-value order (tunable amount / category list).
- Next driver reports no order / missing goods.

May co-occur with offline or abuse but must remain independently scored.

### 6.4 Replacement validity (OR paths)

A replacement may suppress/reduce offline if **any** path holds:

1. **GPS path:** original driver did not reach original destination (stop/twin logic).
2. **Timing path:** replacement placed within configured window after cancel.
3. **Route path:** replacement covers same/similar pickup + key drops.

If a replacement exists but **fails all paths**:

- Emit reason `invalid_replacement`.
- Moderately boost `cancelled_offline`.
- Also boost `cancel_abuse` when reassignment/cancel pattern looks gamed.

**Causal layer (Phase 4):** estimate  
`P(legitimate_replacement | evidence)` vs `P(laundering_cancel | driver_already_completed)`  
and feed those posteriors into scores (heuristics remain fallback).

### 6.5 Score math

Per head `r ∈ {cancelled_offline, cancel_abuse, selective_theft}`:

```
rule_score[r] = f_r(features, reasons)          # deterministic, explainable
ml_score[r]   = model_r(features) or null
blend[r]      = w_rule[r]*rule + w_ml[r]*ml     # if ml null → rule only
score[r]      = calibrate(clip(blend[r]))       # Phase 2+
flag[r]       = 1 if score[r] >= threshold[r] else 0

EAR[r]        = order_value * score[r] * recoverability[r]
attention     = sum_r (w_attention[r] * EAR[r])  # $ at risk weighted
```

Heads are never forced to imply each other.

## 7. ML, feedback, and control plane

### 7.1 Models

- Three calibrated heads (or multi-output with independent calibration).
- Graph encoder path for entity-ring features.
- Foundation-model pretrain on historical cancel/GPS corpora; per-tenant / per-market fine-tune (Phase 4).
- Online path loads artifact by `model_version`; failure → rule-only.

### 7.2 Champion / challenger / shadow

- Challenger scores in shadow alongside champion.
- Promote only if constrained metrics improve at fixed **FP$ budget** (and adversarial suite does not regress).

### 7.3 Feedback loop (predetermined reviews)

Not a full review queue. Quota sampler selects labels per period:

- Stratify by score band and risk head.
- Oversample bias hotspots (elevated FP or FN slices).
- Oversample **model uncertainty** and **rule↔ML disagreement**.
- Enforce per-head min/max so one head cannot consume the budget.
- Emit to label-sample stream/table with `sampling_reason`.
- `POST /v1/feedback` upserts labels for training.

### 7.4 Threshold simulator

Ops adjusts weights/thresholds on historical scores and sees projected flag rates and FP$ before deploy. Policy changes are versioned (`policy_hash`).

### 7.5 Drift / bias monitors

Continuous slice metrics (city, category, value band). Monitors tilt next period’s review mix and alert control plane.

## 8. Excellence layers (all in scope)

| # | Capability | Phase |
|---|---|---|
| 1 | Expected revenue at risk + $-based attention | 1–2 |
| 2 | Trip digital twin / world-model residuals | 4 (hooks in 1) |
| 3 | Entity risk graph (driver–merchant–customer–device) | 4 (lineage graph in 1) |
| 4 | Multi-signal fusion (app, POD/seal, IoT, no-order) | 4 |
| 5 | Adversarial / spoof-resistant red-team gate | 3–4 |
| 6 | Immutable decision ledger + dispute case packs | 1 (ledger) / 4 (packs) |
| 7 | Causal replacement posteriors | 4 |
| 8 | Auto-ML ops control plane (simulator, FP$ optimize) | 3–4 |
| 9 | Logistics risk foundation model | 4 |
| 10 | Late evidence re-assessment (provisional→final) | 1 mechanism / 4 full signals |

Plus earlier upgrade package: dwell/speed, sequence match, lineage graph, audit hashes, backtest harness, market calibration, feature store, multi-tenant adapters.

## 9. API surface (MVP+)

- `POST /v1/assess` — enqueue single assessment  
- `POST /v1/assess:batch` — backfill / replay  
- `GET /v1/assess/{job_id}` — job status + result pointer  
- `GET /v1/orders/{order_display_id}/latest` — latest generation  
- `GET /v1/orders/{order_display_id}/generations` — provisional→final history  
- `POST /v1/feedback` — label upsert  
- `GET /v1/health`  
- Control-plane endpoints (Phase 3+): simulate policy, shadow diff, promote model (internal/authz)

## 10. Error handling

| Case | Behavior |
|---|---|
| GPS timeout / 5xx | Retry with backoff; then assess with `gps_unavailable` |
| Missing order context | Retryable fail; or `incomplete_context` terminal after N tries |
| ML missing / error | Rule-only; `model_version=none` |
| Duplicate events | Idempotent upsert; no double-publish for same identity key |
| Late GPS / no-order | New assessment generation; consumers see `provisional` then final |

## 11. Repo layout

```text
offline-cancel-risk/
  app/
    api/
    worker/
    gps/
    features/
    scoring/
      dbscan_v5.py
      rules.py
      ml.py
      blend.py
      policy.py
      ear.py
    feedback/
    publishers/
    control_plane/
  models/
  config/
    policy.default.yaml
  tests/
  scripts/
    backtest.py
    adversarial_suite.py
  docs/superpowers/specs/
```

## 12. Phased delivery

### Phase 0 — Skeleton

API, worker, GPS adapter, stream+table publishers, policy config, idempotent assess, health.

### Phase 1 — Detection core

- Port v5 DBSCAN + dwell/speed + sequence features.
- Order lineage graph; replacement OR validity; abuse/theft rule features.
- Rule scores, reasons, policy flags, EAR/attention (rule-based recoverability defaults).
- Audit: feature vector ref, config/model/twin/graph hashes.
- Backtest harness; late re-assessment generation mechanism.
- Ledger rows for every assessment.

### Phase 2 — ML + feedback

- Three-head models + calibration.
- Blend; rule-only fallback.
- Champion/challenger shadow path.
- Quota sampler: uncertainty + disagreement + bias strata.
- Feedback API → training tables → scheduled retrain.

### Phase 3 — Hardening / platform

- GPS spoof/teleport reason codes.
- Per-market/vertical calibration + threshold search.
- Drift monitors tilting quotas.
- Threshold simulator (FP$ constrained).
- Feature store (batch ≡ stream).
- Multi-tenant order/LBS adapters.
- Adversarial suite in CI promote path.

### Phase 4 — Excellence track (absolute ceiling)

- Full trajectory twin.
- Entity risk graph + graph encoder.
- Multi-signal fusion.
- Causal replacement posteriors.
- Foundation-model pretrain/fine-tune.
- Dispute case packs.
- Full late-signal fusion; adversarial promote gates hardened.

## 13. Configuration knobs (non-exhaustive)

- GPS: `min_window_h`, `max_window_h`, `min_points`, `max_gap_minutes`
- v5/twin: clustering radius, min pts, dwell/speed thresholds, stop radii/discounts
- Replacement: time window, route similarity tolerance
- Theft: `high_value_amount`, food category ids
- Blend weights per head; policy thresholds per head
- Attention / EAR recoverability weights
- Feedback: `daily_review_quota`, per-head min/max, strata bands
- Shadow promote: FP$ budget, minimum lift, adversarial max regression

## 14. Testing strategy

- Unit: haversine/parse, DBSCAN stop vs pass-through fixtures (v5 examples), replacement OR paths, independence of heads.
- Contract: output schema, idempotency, provisional→final generations.
- Backtest: historical cancels → slice report (replacement, round-trip, food, high-value).
- Adversarial: teleport, waypoint farming, cancel-near-dest, replacement laundering.
- Integration: GPS adapter mocks, queue→publish goldens.
- ML: calibration tests; shadow gate tests; sampler quota tests.

## 15. Open integration points (not blocked for scaffolding)

These are external bindings, not unspecified product behavior:

- Existing LBS GPS API contract/URL/auth — bind behind `gps-adapter` interface.
- Queue/bus technology and warehouse table DDL — bind behind publisher interfaces (default assumption: Kafka-compatible stream + SQL warehouse table).
- Initial numeric defaults for quotas, high-value threshold, blend weights — seed from v5 constants (`MIN_PTS=7`, radii 150/400/800, discounts 0.6/0.2, confidence 0.75) in `policy.default.yaml`; tune via simulator + labeled feedback.

## 16. Decisions log

| Decision | Choice |
|---|---|
| Audience priority | Ops → investigators → eng → generic tenants |
| Runtime | Async only |
| Ingestion | Queue + batch/API |
| GPS source | Existing LBS API |
| GPS window | Adaptive 3h→24h (sparse/gaps, then inconclusive) |
| Outputs | Soft scores + policy flags + $ attention; stream + table |
| Risk heads | Independent binaries via thresholds; theft ≠ offline |
| Enforcement | None; downstream only |
| Scorer | Deterministic rules + ML (hybrid from start) |
| Reviews | Predetermined bias/uncertainty quota for ML only |
| Scope ambition | Absolute ceiling; all excellence pillars in phased scope |
| Project path | `~/Projects/offline-cancel-risk` |

## 17. References

- Internal baseline notebook/PDF: Fraud detection in Long Haul Multistop Offline Orders (DBSCAN v5).
- Ester et al., DBSCAN (KDD 1996); Schubert et al., DBSCAN Revisited (TODS 2017).
