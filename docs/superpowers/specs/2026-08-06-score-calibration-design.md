# Score Calibration — Design Spec

**Date:** 2026-08-06  
**Status:** Approved  
**Project:** offline-cancel-risk  
**Depends on:** pattern-cohort learning (`policy.learning`), label metrics, entity baselines, control-plane audit patterns

## 1. Problem

Assess scores are soft rule/ML blend cutoffs, not probabilities. Thresholds, EAR, and ops intuition treat them as if \(\tau \approx 0.98\) meant ~98% precision. Learning objective deferred full calibration (Platt/isotonic) until Precision_S work landed; that follow-on is now in scope.

## 2. Goals

- Produce a **single calibrated score** \(p \in [0,1]\) per head used for **flags, EAR, and metrics** when apply mode is on.
- Fit **per head × market** (`region_code` / `city_code`) on the **pattern cohort \(S\)** only.
- **Auto method by support:** Platt (logistic) when \(n <\) `platt_max_n`; isotonic otherwise.
- **Fit on `scores_raw`** (pre-baseline); **apply after baselines, before thresholds**.
- Default **`calibration.mode: shadow`**; switch to **`apply`** via policy or API (EAR / DBSCAN retune pattern).
- Holdout **ECE ≤ `max_ece`** (default 0.05) to persist a calibrator.

### Non-goals

- Calibrating the ML champion alone (rule-heavy blends must calibrate too)
- Online / EWMA bin calibrators
- Cross-market pooling or region fallback (explicitly deferred; thin markets skip)
- Amount-weighted labels
- New clustering / DBSCAN changes
- Changing threshold-tuner or sampler objectives

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
  min_labeled: 30       # min pattern-cohort pairs to fit
  platt_max_n: 80       # n < this → Platt; else isotonic
  holdout_fraction: 0.3
  max_ece: 0.05
  cooldown_minutes: 1440
  ece_bins: 10
```

Settings: `OCR_CALIBRATORS_PATH` (default under `data/`, or sibling of control-plane DB — implementation picks one path; prefer dedicated `data/calibrators.db`).

## 5. Fit loop

Per head × market:

1. Join latest assessments ↔ feedback labels for that market.
2. Restrict to pattern cohort \(S\) using existing `in_pattern_cohort`, with scores resolved preferentially from **`scores_raw`** (same preference as label metrics).
3. If \(|S| <\) `min_labeled` → reject; keep prior calibrator if any.
4. Deterministic holdout split (`holdout_fraction`, same hash style as tuner).
5. Train input \(x =\) `scores_raw[head]`, target \(y =\) binary label for head.
6. Method: **Platt** if train \(n <\) `platt_max_n`, else **isotonic** (sklearn `LogisticRegression` / `IsotonicRegression`; clip predictions to \([0,1]\)).
7. Compute holdout ECE with `ece_bins` equal-width bins on \([0,1]\). Persist only if ECE ≤ `max_ece`.
8. Respect `cooldown_minutes` between successful writes; audit actor `calibrator`.
9. Record run history (decision, reason, method, ECE, support) for `GET .../latest`.

## 6. Assess apply

After baselines, for each head:

1. If a calibrator row exists: \(p_h = \mathrm{calib}_h(\texttt{scores_raw}[h])\) (fit domain = raw).
2. Always populate `calibration_meta[h]` with `{p, method, mode, ece, support, applied: bool, skip_reason?}`.
3. **`mode: shadow` / `off` / missing calibrator:** leave post-baseline `scores` unchanged (identity for live path).
4. **`mode: apply` and calibrator present:** set `scores[h] = p_h`. Flags and EAR consume calibrated `scores`.
5. **`scores_raw` never calibrated** — permanent pre-discount blend for learning joins and future fits.

**Domain note:** Fit uses raw; apply replaces post-baseline `scores` with \(p\) from raw. When baseline discount is identity (common), domains match. When a discount fired, meta records that fact; v1 accepts this (no re-discount of \(p\)).

## 7. Metrics & tuner interaction

- **Shadow:** label metrics / threshold tuner keep joining on `scores_raw` / uncalibrated path as today. Optional ECE reports may read `calibration_meta`.
- **Apply:** published `scores` are calibrated; tuner threshold search operates on calibrated scores for that market (ops must retune thresholds after flipping apply — document in OPS). Pattern strata `score_min` still refers to the score channel used for membership at fit time (`scores_raw`).

## 8. Acceptance

- Fit picks Platt vs isotonic by `platt_max_n`.
- Thin \(S\) or ECE above gate → reject; no overwrite.
- Shadow: live flags/EAR unchanged vs pre-calibration; meta present when fit exists.
- Apply: flags/EAR/metrics use \(p\); `scores_raw` unchanged.
- Cooldown honored; audit entries for accept/reject.
- OPS + MANUAL: shadow → review ECE → set `apply` / call API; warn to retune thresholds after apply.

## 9. Docs

- OPS § tuning: calibration subsection next to DBSCAN retune / auto-tuner.
- MANUAL learning blurb: calibration as follow-on after Precision_S.
