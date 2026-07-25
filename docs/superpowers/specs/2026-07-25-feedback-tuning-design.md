# Feedback Tuning Control Plane — Design Spec

**Date:** 2026-07-25  
**Status:** Draft for review  
**Project:** `~/Projects/offline-cancel-risk`  
**Depends on:** Phase 2a/2b model control plane; market policy overlays + guardrails  
**Approach:** Hybrid of auto-apply overlays inside hardgates (B) + continuous supply-aware operating point (C)

## 1. Problem

Labeled review feedback already lands in SQLite via `POST /v1/feedback`, and ops can ingest city/region policy overlays within risk guardrails. Missing pieces:

1. Join labels to stored soft scores and **continuously track precision / recall / F1** per risk head (and market).
2. **Suggest and auto-apply** threshold (and related policy) changes that improve constrained F1.
3. Keep tuning inside **supply-aware precision/recall hardgates** so enforcement does not starve supply at peak, and can be more aggressive when supply exceeds demand.
4. Ingest **local ops enforcement volume hardgates** (hour / day / week).
5. Keep an **append-only audit log** of every ingest, suggestion, apply, and reject.

Downstream owns real enforcement and clawback. This service never calls payout/suspend APIs.

## 2. Goals

- Close the ML feedback loop: labels → metrics → constrained search → overlay apply → audit.
- Map Driver Ops / Platform supply–demand **forecast** into a continuous precision/recall operating point per market.
- Respect local ops **hardgates** on enforcement volume.
- Expose ingest + read APIs for Product FE / platform systems (no FE owned here).
- Reuse existing `policy_overlays`, `policy_guardrails`, and assess `region_code` / `city_code`.

### Non-goals

- Product tuning UI.
- Downstream enforcement execution or clawback execution.
- Retraining model weights in this slice (champion/shadow/canary remains separate); this slice tunes **policy thresholds / overlay knobs**.
- Changing GPS feature math; simulator replays from stored soft scores only.

## 3. Ownership

| Concern | Owner |
|---|---|
| Forecast supply/demand by market | Driver Ops / Platform S&D (ingested here) |
| Local enforcement volume caps | Local ops (ingested here) |
| Label reviews (quota) | Downstream review tooling → `POST /v1/feedback` |
| Threshold / overlay apply inside gates | **This service (tuner)** |
| Enforce / claw back actions | **Downstream** |
| Product FE for overlays / hardgates | Product |

## 4. Architecture

```
supply forecast (S&D) ──┐
enforcement hardgates ──┼─► control ingest ─► SQLite control tables
review labels (feedback)┘         │
                                  ▼
assessments (scores) ──► metrics job ─► label_metrics (P/R/F1)
                                  │
                                  ▼
                         operating point (C)
                         supply_ratio → P/R targets
                                  │
                                  ▼
                         tuner (B under C)
                         search thresholds in
                         guardrails ∩ P/R gates ∩ volume caps
                                  │
                    ┌─────────────┴─────────────┐
                    ▼                           ▼
            policy_overlays              policy_audit_log
            (auto-apply)                 (append-only)
                    │
                    ▼
                 assess_order (existing resolve)
```

## 5. Data model

### 5.1 `supply_forecast`

| Column | Type | Notes |
|---|---|---|
| region_code | TEXT | PK part |
| city_code | TEXT | PK part; `''` = region-wide |
| period_start | TEXT | ISO UTC |
| period_end | TEXT | ISO UTC |
| forecast_supply | REAL | ≥ 0 |
| forecast_demand | REAL | > 0 |
| source | TEXT | e.g. `driver_ops`, `platform_snd` |
| ingested_at | TEXT | ISO UTC |

Primary key: `(region_code, city_code, period_start, period_end)`.

`supply_ratio = clamp(forecast_supply / forecast_demand, ratio_min, ratio_max)`.

### 5.2 `enforcement_hardgates`

| Column | Type | Notes |
|---|---|---|
| region_code | TEXT | |
| city_code | TEXT | |
| window | TEXT | `hour` \| `day` \| `week` |
| max_enforcements | INTEGER | hard cap |
| heads | TEXT | JSON list or `["*"]` |
| updated_at | TEXT | |
| actor | TEXT | who set it |

Primary key: `(region_code, city_code, window)`.

### 5.3 `label_metrics`

Rolling snapshots (not a single mutable row):

| Column | Type | Notes |
|---|---|---|
| snapshot_id | TEXT | PK |
| region_code | TEXT | `''` = global |
| city_code | TEXT | |
| head | TEXT | `cancelled_offline` \| `cancel_abuse` \| `selective_theft` |
| precision | REAL | |
| recall | REAL | |
| f1 | REAL | |
| support | INTEGER | labeled count |
| tp, fp, fn, tn | INTEGER | |
| flag_rate | REAL | optional on labeled set |
| window_start / window_end | TEXT | label/assess window used |
| computed_at | TEXT | |

### 5.4 `policy_audit_log`

Append-only:

| Column | Type | Notes |
|---|---|---|
| audit_id | TEXT | PK |
| ts | TEXT | ISO UTC |
| actor | TEXT | `tuner` \| `ops_ingest` \| `manual_overlay` \| `system` |
| action | TEXT | `forecast_ingest` \| `hardgate_ingest` \| `metrics_snapshot` \| `suggest` \| `apply` \| `reject` \| `clawback_signal` |
| region_code | TEXT | |
| city_code | TEXT | |
| before_json | TEXT | nullable |
| after_json | TEXT | nullable |
| metrics_before_json | TEXT | nullable |
| metrics_after_json | TEXT | nullable |
| constraints_json | TEXT | operating point + hardgates + guardrails summary |
| decision | TEXT | `accepted` \| `rejected` \| `recorded` |
| reason | TEXT | machine-readable code + short message |

### 5.5 Reuse

- `feedback` — existing labels table.
- `assessments` — existing score store (`SqliteTablePublisher`).
- `policy_overlays` — existing city/region overlays.

## 6. Continuous operating point (C)

Config in `config/operating_point.default.yaml` (tunable, versioned with policy hash inputs where needed):

```yaml
ratio_min: 0.5
ratio_max: 2.0
# Anchors: low ratio = peak/tight supply; high = surplus
peak:      # supply_ratio <= peak.ratio
  ratio: 0.8
  min_precision: 0.85
  min_recall: 0.40
  max_precision: 1.0
  max_recall: 0.70
surplus:   # supply_ratio >= surplus.ratio
  ratio: 1.2
  min_precision: 0.70
  min_recall: 0.75
  max_precision: 0.95
  max_recall: 1.0
# Between anchors: piecewise-linear interpolation of the four bounds
fallback_when_no_forecast:
  min_precision: 0.75
  min_recall: 0.50
  max_precision: 1.0
  max_recall: 1.0
```

**Behavior**

- Resolve active forecast row for `(region, city)` overlapping “now” (city wins over region-wide).
- Compute `supply_ratio`; interpolate hardgate band for precision/recall.
- Peak intent: **higher precision, lower recall**. Surplus intent: **higher recall, lower precision**. Both bands are hardgated (min/max).

## 7. Metrics job

Triggers:

- Debounced after `POST /v1/feedback` (e.g. 30s coalesce).
- Periodic schedule (default hourly) via worker tick or CLI `scripts/compute_label_metrics.py`.

Algorithm (per head, optional market slice):

1. Load labels with binary per-head fields (e.g. `labels.cancelled_offline` ∈ {0,1}).
2. Join to latest non-provisional assessment scores for that order.
3. Apply **current** resolved thresholds to scores → predicted flags (replay; no GPS refetch).
4. Compute confusion matrix → P/R/F1; persist snapshot.
5. Audit `metrics_snapshot`.

Minimum support: do not tune a head/market until `support >= min_labeled` (config, default 30).

## 8. Tuner (B under C)

### 8.1 Search space

Default: per-head `thresholds.*` on a grid (or coarse binary search) inside `policy_guardrails.bounds`.

Optional later (same path): `routing.p1_attention_min`, blend weights — still guardrail-clamped.

### 8.2 Constraints (all must pass)

1. Guardrails validate overlay.
2. On labeled set (and/or holdout fold):  
   `precision ∈ [min_p, max_p]` and `recall ∈ [min_r, max_r]` from operating point.
3. Projected flag volume from recent assessments ≤ hardgates for hour/day/week (use score replay at candidate threshold; if Downstream posts actual enforcement counts later, prefer those for clawback-aware tightening).
4. `support >= min_labeled`.
5. Cooldown: no auto-apply for same market/head within `tuner_cooldown_minutes` unless F1 lift ≥ `min_f1_lift`.

### 8.3 Objective

Maximize constrained F1 (primary). Tie-break: higher recall in surplus regime; higher precision in peak regime.

### 8.4 Apply

- Build overlay `{thresholds: {...}}` (city-level when city present, else region).
- `save_overlay` (existing validate + upsert).
- Audit `apply` with before/after + metrics + constraints.
- If no candidate beats current under constraints → audit `reject` with reason; leave overlay unchanged.

### 8.5 Clawback signal (ingest only)

Downstream may `POST /v1/enforcement/clawback` with market + reason + optional tighter temporary caps. Service:

- Records audit `clawback_signal`.
- Optionally tightens effective hardgates / operating point toward peak band for `clawback_ttl_minutes`.
- Does **not** call Downstream APIs.

## 9. API surface

All control routes honor existing `_require_auth` when `OCR_AUTH_REQUIRED=1`.

| Method | Path | Purpose |
|---|---|---|
| PUT | `/v1/supply/forecast` | Upsert forecast rows (batch allowed) |
| GET | `/v1/supply/forecast` | List/active forecast for market |
| PUT | `/v1/enforcement/hardgates` | Upsert local ops caps |
| GET | `/v1/enforcement/hardgates` | Read caps |
| POST | `/v1/enforcement/clawback` | Ingest clawback signal |
| GET | `/v1/metrics/labels` | Latest P/R/F1 snapshots |
| POST | `/v1/tuning/run` | Force metrics+tune cycle (ops/CI) |
| GET | `/v1/tuning/suggestions` | Recent suggest/apply/reject decisions |
| GET | `/v1/audit/policy` | Query audit log |
| PUT | `/v1/policy/overlays` | Existing manual override (also writes audit) |

Assess path unchanged except it already resolves overlays; no new assess fields required for v1 of this slice.

## 10. Package layout

```
src/offline_cancel_risk/
  control_plane/
    __init__.py
    forecast.py          # store + resolve active forecast
    hardgates.py         # enforcement caps store
    operating_point.py   # supply_ratio → P/R bounds
    metrics.py           # label join + P/R/F1
    tuner.py             # constrained search + apply
    audit.py             # append-only log
  api/routes.py          # new endpoints
scripts/
  compute_label_metrics.py
  run_tuner.py
config/
  operating_point.default.yaml
```

## 11. Config / settings

New `OCR_*` settings:

- `operating_point_path`
- `control_plane_sqlite_path` (or reuse assessments DB with separate tables — **prefer one SQLite** `data/control_plane.db` for forecast/hardgates/metrics/audit to avoid coupling publishers)
- `tuner_min_labeled` (default 30)
- `tuner_cooldown_minutes` (default 60)
- `tuner_min_f1_lift` (default 0.01)
- `metrics_debounce_seconds` (default 30)

## 12. Success metrics

| Check | Pass criteria |
|---|---|
| Metrics | After ≥ N labeled fixtures, F1 matches hand-computed confusion matrix |
| Operating point | Peak ratio yields higher `min_precision` than surplus; surplus higher `min_recall` |
| Hardgates | Candidate that would exceed hourly cap is rejected and audited |
| Auto-apply | Improving candidate inside all gates upserts overlay; `policy_hash` changes on next assess |
| Manual overlay | Still works; audited as `manual_overlay` |
| Auth | Control routes 401 when auth required and key missing |
| No enforcement | Suite proves no Downstream action clients added |

## 13. Phased delivery

| Slice | Scope |
|---|---|
| **T1** | Forecast + hardgates ingest stores, operating_point resolver, audit log, APIs |
| **T2** | Label metrics job + `GET /v1/metrics/labels` |
| **T3** | Tuner search + auto-apply + suggestions API + clawback ingest |
| **T4** | CLI scripts, README/OPS docs, integration test on CSV demo + synthetic labels |

## 14. Open defaults (seed, not blockers)

Numeric anchors in §6 and tuner mins are seeds; Production calibrates via Product/S&D. Guardrail file remains the absolute numeric ceiling for any overlay field.
