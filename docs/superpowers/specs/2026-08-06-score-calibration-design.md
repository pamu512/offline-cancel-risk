# Score Calibration — Design Spec

**Date:** 2026-08-06  
**Status:** Approved  
**Project:** offline-cancel-risk  
**Depends on:** pattern-cohort learning (`policy.learning`), label metrics, entity baselines, control-plane audit patterns

## 1. Problem

Assess scores are soft rule/ML blend cutoffs, not probabilities. Thresholds, EAR, and ops intuition treat them as if \(\tau \approx 0.98\) meant ~98% precision. Learning objective deferred full calibration (Platt/isotonic) until Precision_S work landed; that follow-on is now in scope.

## 2. Goals

- Produce a **single calibrated score** \(p \in [0,1]\) per head used for **flags, EAR, and metrics** when apply mode is on.
- Fit **per head × market** (`region_code` / `city_code`) on **all labeled** pairs (full score support).
- **Auto method by support:** Platt (logistic) when \(n <\) `platt_max_n`; isotonic otherwise.
- **Fit on `scores_raw`** (pre-baseline); **apply after baselines, before thresholds**, preserving baseline discount: `score_applied = p · (score / scores_raw)`.
- Default **`calibration.mode: shadow`**; switch to **`apply`** via policy or API (EAR / DBSCAN retune pattern).
- Holdout **must be non-empty**. Gates: **quantile ECE** ≤ `max_ece` and **Brier** ≤ `max_brier`.

### Non-goals

- Calibrating the ML champion alone (rule-heavy blends must calibrate too)
- Online / EWMA bin calibrators
- Cross-market pooling or region fallback (explicitly deferred; thin markets skip)
- Amount-weighted labels
- New clustering / DBSCAN changes
- Changing threshold-tuner or sampler objectives
- Claiming sampler labels equal population probabilities (ops must treat \(p\) as label-conditional)
## 3. Architecture

```
assess:  blend → scores_raw → baselines → [calibrate meta / apply] → thresholds → EAR
fit:     assessments ⋈ labels → filter S → holdout → Platt|isotonic → CalibratorStore + audit
```

| Piece | Role |
|---|---|
| `CalibratorStore` | SQLite rows keyed by `(region, city, head)`: method, params JSON, ECE, support, `updated_at` |
| `run_calibration_fit` | Control-plane job; writes calibrators + audit; does **not** write policy overlays |
| Assess hook | After `apply_baselines`, before `apply_thresholds` |
| API | `POST /v1/tuning/calibrate`, `GET /v1/tuning/calibrate/latest` |
| Optional tick | `calibration.on_tick` + `OCR_CONTROL_PLANE_TICK_SECONDS` |

## 4. Policy

```yaml
calibration:
  mode: shadow          # shadow | apply | off
  on_tick: false
  min_labeled: 30       # min labeled pairs to fit
  platt_max_n: 80       # n < this → Platt; else isotonic
  holdout_fraction: 0.3
  max_ece: 0.05
  max_brier: 0.25
  cooldown_minutes: 1440
  ece_bins: 10
  ece_strategy: quantile  # quantile|equal
```

Settings: `OCR_CALIBRATORS_PATH` (default under `data/`, or sibling of control-plane DB — implementation picks one path; prefer dedicated `data/calibrators.db`).

## 5. Fit loop

Per head × market:

1. Join latest assessments ↔ feedback labels for that market (full score support).
2. If support < `min_labeled` (and `learning.min_pattern_support`) → reject; keep prior calibrator if any.
3. Deterministic holdout split (`holdout_fraction`). **Reject if holdout empty** (no train-ECE fallback).
4. Train input \(x =\) `scores_raw[head]`, target \(y =\) binary label for head.
5. Method: **Platt** if train \(n <\) `platt_max_n`, else **isotonic**.
6. Holdout **quantile ECE** and **Brier**; persist only if both ≤ configured maxima.
7. Respect `cooldown_minutes`; audit actor `calibrator`.
8. Record run history (decision, reason, method, ECE, Brier, support) for `GET .../latest`.

## 6. Assess apply

After baselines, for each head:

1. If a calibrator row exists: \(p_h = \mathrm{calib}_h(\texttt{scores_raw}[h])\).
2. `score_applied = apply_calibrated_score(p, scores_raw, scores)` preserves baseline discount.
3. Populate `calibration_meta[h]` with `{p, score_applied, method, mode, ece, support, applied, baseline_discounted, …}`.
4. **`mode: shadow` / `off` / missing:** leave post-baseline `scores` unchanged.
5. **`mode: apply` and calibrator present:** set `scores[h] = score_applied`.
6. **`scores_raw` never calibrated**.

## 7. Metrics & tuner interaction

- **Shadow:** label metrics / threshold tuner keep joining on `scores_raw` / uncalibrated path as today. Optional ECE reports may read `calibration_meta`.
- **Apply:** published `scores` are calibrated; tuner threshold search operates on calibrated scores for that market (ops must retune thresholds after flipping apply — document in OPS). Pattern strata `score_min` still refers to the score channel used for membership at fit time (`scores_raw`).

## 8. Acceptance

- Fit picks Platt vs isotonic by `platt_max_n`.
- Fit uses full labeled support (not S-only); empty holdout rejects.
- Thin labels / high quantile ECE / high Brier → reject; no overwrite.
- Shadow: live flags/EAR unchanged vs pre-calibration; meta present when fit exists.
- Apply: flags/EAR use `score_applied` (p × baseline discount); `scores_raw` unchanged.
- Cooldown honored; audit entries for accept/reject.
- OPS + MANUAL: shadow → review ECE+Brier → set `apply` / call API; warn to retune thresholds after apply.

## 9. Docs

- OPS § tuning: calibration subsection next to DBSCAN retune / auto-tuner.
- MANUAL learning blurb: calibration as follow-on after Precision_S.
