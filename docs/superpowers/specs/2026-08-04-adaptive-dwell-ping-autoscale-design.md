# Adaptive dwell + ping-gap autoscale + transit filter

**Date:** 2026-08-04  
**Status:** approved for implementation

## Problem

1. Fixed `dbscan.min_pts` / `drop_off_min_pts` bias dense (1s) vs sparse (30s) GPS.
2. Fixed `dwell.min_dwell_seconds` ignores place (apartment/commercial) and vehicle (walker→semi).
3. Low-speed crawl through a stop geofence (traffic / signals) can look like a delivery dwell.

## Goals

- Normalize stop presence to a target wall-clock dwell \(D\), then derive DBSCAN counts from median ping gap \(\tau\).
- Optional `place_class` / `vehicle_class` on assess (skippable; unknown → factor 1.0). Market/platform overlays tune base \(D\) and factor tables.
- Cheap transit rejection: tighter dwell radius + max displacement during a dwell run.

## Non-goals

- POI inference from lat/lon
- Road/traffic API
- Changing GPS SDK or Downstream enforcement

## Resolution

\[
D = \mathrm{clamp}(D_{base} \cdot f_{place} \cdot f_{vehicle},\; D_{min},\; D_{max})
\]

\[
\tau = \mathrm{median}(\Delta t_i)\quad\text{consecutive gaps} \le gps.max\_gap\_minutes
\]

\[
min\_pts = \mathrm{clamp}\big(\mathrm{round}(min\_pts_{ref}\cdot\frac{D}{D_{base}}\cdot\frac{\tau_{ref}}{\tau}),\; lo,\; hi\big)
\]

\[
drop\_off\_min\_pts = \mathrm{clamp}\big(\mathrm{round}(drop_{ref}\cdot\frac{D}{D_{base}}\cdot\frac{\tau_{ref}}{\tau}),\; lo,\; hi\big)
\]

At \(\tau=\tau_{ref}\) and \(D=D_{base}\), both recover the policy reference counts.

Clamp median gap before scaling: \(\tau = \mathrm{clamp}(\tau_{raw},\; gap\_seconds\_min,\; gap\_seconds\_max)\) (defaults 1–30s). Raw and clamped both appear in evidence.

If autoscale off, or fewer than `min_gap_samples` usable deltas: use policy counts; dwell still uses resolved \(D\).

## Cheap transit lever

Dwell mask (unchanged speed/duration rules) plus:

1. **`dwell.radius_m`** — default tighter than sequence match (e.g. 80m vs 150m).
2. **`dwell.max_run_displacement_m`** — if haversine(run_start_point, current) exceeds threshold while still “in dwell,” reset the run (crawling through ≠ parked).

## Policy knobs

```yaml
dbscan:
  autoscale_min_pts: true
  autoscale_ref_gap_seconds: 5
  # min_pts / drop_off_min_pts remain reference counts at (D_base, τ_ref)
dwell:
  min_dwell_seconds: 120          # D_base
  radius_m: 80
  max_run_displacement_m: 40
  min_gap_samples: 3
  place_factors:
    unknown: 1.0
    curb: 0.85
    residential: 1.0
    apartment: 1.35
    commercial: 1.5
  vehicle_factors:
    unknown: 1.0
    walker: 0.55
    cycle: 0.6
    two_wheel: 0.7
    van_pickup: 1.0
    large_4w: 1.25
    box_truck: 1.45
    semi: 1.6
```

Guardrails clamp factors, radii, displacement, and effective counts.

## API

Optional on `AssessRequest`:

- `place_class: str | None`
- `vehicle_class: str | None`

Unrecognized → treat as `unknown` (factor 1.0).

## Evidence

Attach under geometry/gps evidence: `dwell_target_s`, `median_gap_s`, `min_pts_effective`, `drop_off_min_pts_effective`, `place_class`, `vehicle_class`, `place_factor`, `vehicle_factor`.

## Precision notes

- Gap scaling fixes sampling bias; factors fix mode/place prior; displacement filter cuts mid-geofence crawls.
- Missing classes = market base \(D\) + gap scale only (safe rollout).
- Golden tests: fixed gaps / autoscale path covered by unit tests; holdout may shift slightly when autoscale on.

## Tests

- Gap 1s → higher min_pts than 30s for same \(D\)
- apartment × semi → higher \(D\) than walker × curb
- Missing classes → factors 1.0
- Crawl through radius with displacement > max → not dwell
- Stationary in tighter radius → dwell
