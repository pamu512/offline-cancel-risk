# Entity Baseline Gate — Design Spec

**Date:** 2026-08-03  
**Status:** Approved  
**Project:** `~/Projects/offline-cancel-risk`  
**Depends on:** assess pipeline, blended scores + thresholds, SQLite publishers pattern  
**Aligns with:** learning-objective realignment (pattern precision); this gate is entity-level FP dampening in the below-threshold band — not a tuner precision weight

## 1. Problem

Some drivers (and optionally users / driver×user pairs) are **consistently under** flag thresholds — their cancel behavior is a stable low-risk baseline. Relative bumps above that personal baseline (but still below the market threshold) are often noise. Treating them like cold-start risk overstates flags.

We need to:

1. **Store** rolling + EWMA behavior baselines for warehouse / Downstream pull.
2. **Optionally discount** assess-time scores (multiply) when above-baseline consistency fires **inside the below-threshold band**, with explicit reason codes — without sheltering absolute threshold crossings.

## 2. Goals

- Track baselines for **driver always**; **user** and **driver×user pair** only when `user_id` is present.
- Per head, with **per-head** enable/mode/discount.
- Arm baseline when **under-threshold consistent** (window **OR** EWMA).
- Activate discount only when **above-baseline consistent** (window **AND** EWMA) **and** current raw score is in `(baseline + ε, armed_thr)`.
- Persist **`scores_raw`** (pre-discount) and discounted `scores` on assessments; baselines update from **raw** only.
- Default **`mode: shadow`** (record + reasons/meta, no multiply) until markets flip to `apply`.
- SQLite + pull API with **`updated_since` cursor**; no JSONL stream in this slice.
- Time-decay samples older than `max_age_days`.
- Pair uses smaller `pair_window_n`; backoff discount signals pair → driver → user.

### Non-goals

- JSONL baseline stream.
- Merchant / device baselines.
- Calibrated probability / Platt scaling.
- Changing the pattern-cohort tuner objective.
- Downstream enforcement actions.

## 3. Entity keys

| Kind   | When              | Key form                     | Window size        |
|--------|-------------------|------------------------------|--------------------|
| driver | always            | `driver:{driver_id}`         | `window_n`         |
| user   | `user_id` present | `user:{user_id}`             | `window_n`         |
| pair   | `user_id` present | `pair:{driver_id}:{user_id}` | `pair_window_n`    |

## 4. Consistency rules

```yaml
baselines:
  mode: shadow   # shadow | apply  (global default; head may override)
  window_n: 20
  pair_window_n: 8
  under_fraction: 0.9
  ewma_alpha: 0.2
  ewma_delta: 0.05
  min_ewma_samples: 10
  above_epsilon: 0.1
  above_fraction: 0.9
  discount: 0.85
  refresh_epsilon: 0.05
  max_age_days: 90
  heads:
    cancelled_offline: {enabled: true}
    cancel_abuse: {enabled: true, discount: 0.90}
    selective_theft: {enabled: true, mode: shadow}  # precision-fragile
```

After blend, for each relevant entity×head with **raw** score `s` and live market threshold `thr`:

1. Append `{s, t}` to ring buffer; drop points older than `max_age_days`; trim to kind window size.
2. Update EWMA on retained points’ latest sample.
3. **Under-consistent (OR — soft arm):** window fraction `< thr` ≥ `under_fraction` with full window, **or** EWMA `< thr − ewma_delta` with `samples ≥ min_ewma_samples`.
4. When under-consistent: candidate baseline = mean of window scores `< thr` (else EWMA). **Arm** if unset; **refresh** only if `candidate ≤ baseline + refresh_epsilon` (elevated-but-under-thr windows must not raise the floor). Persist **`armed_thr = thr`** at arm/refresh.
5. **Above-consistent (AND — hard activate):** baseline set, and window fraction `> baseline + above_epsilon` ≥ `above_fraction`, **and** EWMA `> baseline + above_epsilon` (with min samples).
6. `discount_active` for entity×head iff above-consistent **and** head enabled.

Evaluate under/above against **`armed_thr`** once baseline exists (not live thr), so tuner thr moves do not flip history. New arms use current thr.

## 5. Assess-time application

Hook **after** blend, **before** thresholds / EAR:

1. Update store with **raw** scores.
2. Per head, collect active kinds among driver / user / pair (if present).
3. **Backoff:** if pair not above-consistent (sparse), still consider driver then user.
4. **Band gate:** apply multiply only if `baseline_ref + above_epsilon < s < armed_thr_ref` where refs come from the chosen entity row (prefer pair if active, else driver, else user). If `s ≥ armed_thr` (or live thr if no armed): **no multiply**.
5. Modes:
   - `shadow`: write `baseline_meta` + reason codes `baseline_discount:*` / `baseline_shadow:*`; **do not** change scores.
   - `apply`: `score = clip01(s * head_discount)`; reasons `baseline_discount:*`.
6. Persist `scores_raw` = pre-discount blend; `scores` = post (equal in shadow).
7. Flags / EAR / attention from `scores`.

`baseline_meta` example:

```json
"baseline_meta": {
  "cancelled_offline": {
    "applied": ["driver"],
    "multiplier": 0.85,
    "mode": "shadow",
    "band_eligible": true
  }
}
```

## 6. Storage

`OCR_ENTITY_BASELINES_PATH` → `data/entity_baselines.db`.

Table `entity_baselines`: entity_key, entity_kind, driver_id, user_id, head, samples, ewma, baseline, armed_thr, under_consistent, above_consistent, discount_active, window_json, region_code, city_code, updated_at.  
PK `(entity_key, head)`.

`window_json`: `[{"s": 0.2, "t": "2026-08-01T12:00:00Z"}, ...]`.

## 7. API

- `GET /v1/baselines?entity_kind=&driver_id=&user_id=&head=&discount_active=&updated_since=&limit=`  
  Sort `(updated_at ASC, entity_key ASC, head ASC)`. `updated_since` is exclusive lower bound ISO timestamp.
- `GET /v1/baselines/{entity_key}` — all heads for key.

Auth: control-route `_require_auth`.

## 8. Learning / metrics note

Label metrics and pattern tuner should prefer **`scores_raw`** when present so discounts do not distort learning joins. Assess publishes both.

## 9. Tests

- EWMA + ring + max_age drop
- Under OR arms baseline + armed_thr; above AND activates
- Band: no multiply when `s ≥ thr`; multiply (apply mode) when in band
- Shadow: reasons/meta, scores unchanged
- scores_raw preserved
- No user_id → driver only; pair_window_n smaller
- Per-head theft stays shadow when global apply
- GET with updated_since cursor
- mode off / head disabled → no writes affecting scores

## 10. Acceptance

All ten improvements from review are in this spec and implemented: band limit, scores_raw, shadow default, updated_since, AND for discount / OR for arm, armed_thr freeze, pair window + backoff, per-head policy, time decay, language clarified (FP dampener in band).
