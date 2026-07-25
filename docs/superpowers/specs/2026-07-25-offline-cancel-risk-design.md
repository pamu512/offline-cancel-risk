# Offline Cancel Risk Platform — Design Spec

**Date:** 2026-07-25  
**Status:** Approved (reuse/OSS distribution addendum 2026-07-25)  
**Project:** `~/Projects/offline-cancel-risk`  
**Distribution:** Open-source installable toolkit — Git repo + PyPI package + CSV-only `examples/` demo  
**License (default):** Apache-2.0 (changeable before first public publish)  
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
3. Data / eng — async microservice **or** library embed, stream + table features for downstream.
4. Any logistics team — clone/pip-install, bring CSV or plug adapters (first-class, not an afterthought).

### Product goals

- **Reusable by anyone:** core has zero vendor lock-in (no hardcoded company table names, host paths, or proprietary IDs in the library).
- Ship as **(1)** public Git repo, **(2)** installable package `offline-cancel-risk`, **(3)** `examples/csv_demo` that runs end-to-end on sample CSVs with no external GPS/order APIs.
- Pluggable adapters from MVP: GPS, order-events, publishers (stream/table). Reference adapters included; tenants add their own.
- Async assessment only (never realtime on cancel click).
- Produce risk **features** for downstream; this toolkit does **not** enforce actions (no payout block, suspend, etc.).
- Three independent soft scores + versioned policy flags + attention driven by **expected revenue at risk**.
- Automate human toil for detection; retain a **small predetermined label quota** for ML feedback (bias / uncertainty / disagreement aware).
- Absolute ceiling capability set: trajectory twin, entity graph, multi-signal fusion, adversarial gates, causal replacement, foundation-model track, late re-assessment, immutable ledger.

### Non-goals (all phases)

- Full investigator product UI beyond label tickets, threshold simulator, and dispute case packs.
- Owning / rewriting any tenant’s LBS GPS platform (integrate via adapter).
- Downstream business actions / enforcement inside this toolkit.
- Bundling proprietary production datasets (examples use synthetic/sample CSVs only).

**Terminology:** *MVP ship* = Phase 0–1 (installable package + CSV demo + rules + contracts + ledger + adapter interfaces). *Platform complete* = through Phase 4 (absolute ceiling). The output schema is stable from MVP so later phases do not break downstream consumers.

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
| No actions | Toolkit never calls enforcement APIs |
| Reuse smoke | Fresh clone: `pip install -e ".[dev]"` + `python -m examples.csv_demo` produces scores from sample CSVs with no network calls |
| Adapter isolation | Core imports never reference a specific tenant SDK; tenant code lives under `adapters/` or external packages |

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
| `offline_cancel_risk` (library) | Installable core: schemas, features, scoring, pipeline, adapter protocols |
| `assess-api` | Optional FastAPI process: `POST /v1/assess`, batch, job status, latest, feedback, health |
| `assess-worker` | Queue consumer, batch runner, re-assessment on late evidence |
| `adapters.gps` | Protocol + `HttpGpsClient` + `CsvGpsClient` / `FakeGpsClient` |
| `adapters.events` | Protocol for cancel ingestion (queue / HTTP / CSV folder watcher) |
| `adapters.publishers` | Protocol + JSONL stream + SQLite table (Kafka/WH are optional bindings) |
| `features/*` | Twin, DBSCAN v5, lineage, graph, multi-signal, spoof |
| `scoring/*` | Rules, ML, blend, policy, $ at risk |
| `feedback/*` | Bias/uncertainty/disagreement sampler + label store |
| `control-plane` | Threshold simulator, shadow compare, promote gates, drift monitors |
| `examples/csv_demo` | Zero-network demo: sample orders+GPS CSVs → printed/JSON results |
| `policy-config` | Versioned YAML/JSON knobs (tenant overlays allowed) |
| `models/` | Artifacts or object-store references |

### Distribution model

1. **Git repository** — source of truth, Apache-2.0, `CONTRIBUTING.md`, CI, examples.
2. **PyPI package** `offline-cancel-risk` — `pip install offline-cancel-risk` exposes the library + CLI entry points.
3. **CSV demo** — `python -m examples.csv_demo` (or `ocr-csv-demo`) runs without credentials or network.
4. **Embed or serve** — tenants either import `assess_order(...)` in their jobs or run the bundled API/worker with their adapter config.

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
  LICENSE
  NOTICE
  CONTRIBUTING.md
  pyproject.toml          # package name: offline-cancel-risk
  README.md               # quickstart: pip + CSV demo + API
  src/offline_cancel_risk/
    api/
    worker/
    adapters/
      gps.py
      events.py
      publishers.py
    features/
    scoring/
    pipeline/
    feedback/
    control_plane/
  config/
    policy.default.yaml
  examples/
    csv_demo/
      __main__.py
      sample_orders.csv
      sample_gps.csv
      README.md
  adapters/               # optional tenant reference adapters (not imported by core)
  models/
  tests/
  scripts/
    backtest.py
    adversarial_suite.py
  docs/superpowers/specs/
```

## 12. Phased delivery

### Phase 0 — Skeleton (reusable package)

Installable `src/offline_cancel_risk` package, LICENSE/CONTRIBUTING, adapter protocols, CSV GPS/orders clients, JSONL+SQLite publishers, policy config, idempotent assess, optional API/worker, health, `examples/csv_demo` zero-network run.

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
- Additional production adapter bindings (Kafka, warehouse DDL templates) — core protocols already exist from Phase 0.
- Adversarial suite in CI promote path.
- PyPI publish automation (trusted publishing / tags).

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
| Distribution | OSS Git + PyPI package + CSV-only examples demo |
| License default | Apache-2.0 |
| Tenant integration | Adapter protocols from MVP; core has no vendor lock-in |

## 17. References

- Internal baseline notebook/PDF: Fraud detection in Long Haul Multistop Offline Orders (DBSCAN v5).
- Ester et al., DBSCAN (KDD 1996); Schubert et al., DBSCAN Revisited (TODS 2017).
