# Learning Objective Realignment — Design Spec

**Date:** 2026-08-03  
**Status:** Approved  
**Project:** `~/Projects/offline-cancel-risk`  
**Depends on:** feedback sampler, label metrics, constrained tuner

## 1. Problem

The control plane optimized **global F1** and **uncertainty-heavy labeling**. That chases the long tail (novel / exotic cases) and under-samples the recurring behavioral patterns that dominate labelable mass. Scores are uncalibrated cutoffs, not probabilities; F1 treats precision and recall symmetrically and surplus `min_recall` forces recall into the last ~2%.

## 2. Goals

- Optimize for **stable detection of common behavioral patterns** (~98% of labelable mass).
- **Primary metric:** precision on a **pattern cohort** \(S\) ≥ `target_precision` (default 0.98).
- **Secondary:** maximize recall on \(S\) subject to that floor.
- Retarget the sampler so most tickets are **pattern-mass** examples, not boundary-only.
- Soften surplus operating-point recall so supply mode cannot force a 2% chase.
- Keep per-trip DBSCAN stop features as-is (no new clustering stack).

### Non-goals

- Novel / adversarial fraud detection.
- Latency / realtime optimizations.
- Full score calibration (Platt/isotonic) — follow-on after pattern precision is stable.
- Cross-order geo-clustering or deep trajectory models.
- Rewriting DBSCAN v5 core.

## 3. Interpretation of “98%”

Not “98% recall of all fraud in the wild.”  
**High precision on known pattern strata** (aim ~0.98 on cohort \(S\)) with acceptable recall **on that cohort**. Explicitly do not optimize recall on the complement until \(S\) is stable.

## 4. Pattern cohort \(S\)

Configured under `policy.learning`:

```yaml
learning:
  target_precision: 0.98
  min_pattern_support: 15
  min_pattern_recall: 0.35
  pattern_mass_fraction: 0.7
  blend_search_min_support: 40
  pattern_strata:
    cancelled_offline:
      score_min: 0.85
    cancel_abuse:
      score_min: 0.70
    selective_theft:
      score_min: 0.70
```

An assessment is in \(S\) for head \(h\) when its (blended) score for \(h\) ≥ `score_min`, or when `reason_any` matches any assessment reason (optional list on the stratum).

**Metrics on \(S\):** join labeled feedback to assessments, restrict to pairs in \(S\), then compute precision / recall / F1 / support as today.

## 5. Sampler

- New reason: `pattern_mass` (priority between disagreement and uncertainty).
- Batch mix: aim ~`pattern_mass_fraction` of daily quota from clear pattern strata; remainder from disagreement / uncertainty / bias / coverage.
- Inline: after disagreement, prefer `pattern_mass` over uncertainty when a head matches strata.

## 6. Tuner objective

For each head:

1. Require train pattern support ≥ `min_pattern_support`.
2. Grid-search thresholds (blend/routing only if support ≥ `blend_search_min_support`).
3. Candidate must satisfy holdout \(\mathrm{Precision}_S \ge\) `target_precision` and \(\mathrm{Recall}_S \ge\) `min_pattern_recall`, plus hardgates / guardrails.
4. Among candidates, maximize holdout \(\mathrm{Recall}_S\) (ties: higher precision).
5. Apply only if holdout pattern recall lifts by ≥ `tuner_min_f1_lift` (reused as lift epsilon on the primary metric) and cooldown allows.

Global operating-point P/R bands remain a soft ops constraint; surplus `min_recall` is lowered so it cannot override the precision-on-\(S\) goal.

## 7. GPS clustering

Existing per-trip DBSCAN remains the pattern feature generator. No new clustering in this change. Optional later: market-retune `eps` / `min_pts` from labeled pattern cohort only.

## 8. Acceptance

- Spec + `learning` policy section present.
- Sampler emits majority `pattern_mass` under a mixed candidate pool.
- Tuner prefers high-\(\tau\) that hits target precision on \(S\) even when global F1 is lower.
- OPS documents pattern precision vs global F1.
