# DBSCAN market retune — Design Spec

**Date:** 2026-08-05  
**Status:** Approved  
**Project:** offline-cancel-risk

## 1. Problem

GPS noise and ping density vary by market. Fixed `clustering_radius_m` (eps) and `min_pts` refs under- or over-form stops. Learning objective wants **precision on pattern cohort \(S\)**, not a new clusterer.

## 2. Goals

- Full hybrid loop: **assess-time GPS cache** → offline grid over `(clustering_radius_m, min_pts)` → re-assess → Precision_S / Recall_S on labeled pattern cohort → shadow report or auto-apply overlay.
- Keep **v5 per-trip DBSCAN** only.
- Default **`dbscan_retune.mode: shadow`**. When **`apply`**, write overlay automatically if gates pass (tuner-style).

### Non-goals

- New clustering algorithms  
- Cross-order / multi-driver geo-clustering  
- Deep trajectory models  
- Retuning drop_off discounts / radii tiers (only eps + min_pts refs)

## 3. GPS replay cache

`AssessGpsCache` (SQLite, `OCR_ASSESS_GPS_CACHE_PATH`):

| Column | Purpose |
|---|---|
| order_display_id + assessment_generation | Key |
| region_code, city_code | Market filter |
| request_json | AssessRequest snapshot for replay |
| points_json | GpsPoint list |
| recorded_at | Retention |

Write after successful non-empty GPS window in geometry. Retention: `dbscan_retune.cache_retention_days` (default 30). Prune on write / retune start.

## 4. Search

Grid ∩ guardrails (`dbscan.clustering_radius_m`, `dbscan.min_pts`):

```yaml
dbscan_retune:
  mode: shadow  # shadow | apply | off
  on_tick: false
  cooldown_minutes: 1440
  min_labeled: 15
  min_recall_lift: 0.01
  cache_retention_days: 30
  holdout_fraction: 0.3
  grid:
    clustering_radius_m: [30, 40, 50, 60, 80]
    min_pts: [5, 7, 9, 11, 15]
```

For each candidate: resolved policy + trial `dbscan.min_pts` / `clustering_radius_m` → `assess_order` with `FakeGpsClient(cached points)` → join labels → pattern metrics on `cancelled_offline` (primary). Autoscale / dwell factors still apply on top of retuned refs.

## 5. Objective & gates

Same as threshold tuner on \(S\) for `cancelled_offline`:

1. Labeled + cached support ≥ `min_labeled` / `learning.min_pattern_support`  
2. Holdout Precision_S ≥ `learning.target_precision`  
3. Holdout Recall_S ≥ `learning.min_pattern_recall`  
4. Maximize holdout Recall_S (tie: higher precision)  
5. Apply only if Recall_S lifts ≥ `min_recall_lift` vs current policy and cooldown allows  
6. Hardgates on projected flag volume  

## 6. Modes

| Mode | Behavior |
|---|---|
| `shadow` (default) | Search + persist run + audit; **no** overlay |
| `apply` | Gates pass → `save_overlay` + audit (`dbscan_retuner`) |
| `off` | No-op |

## 7. API / ops

- `POST /v1/tuning/dbscan-retune` `{region_code, city_code, mode?}`  
- `GET /v1/tuning/dbscan-retune/latest?region_code=&city_code=`  
- Optional tick when `on_tick: true`  
- Run history in control-plane DB  

## 8. Acceptance

- Cache round-trip; empty GPS not stored  
- Synthetic labeled pack selects known-better eps/min_pts  
- Shadow never writes overlay; apply does within guardrails  
- Insufficient labels / missing cache → reject with reason  
- Cooldown respected  

## 9. Docs

OPS + MANUAL: shadow → review → set `apply` / call with mode apply.
