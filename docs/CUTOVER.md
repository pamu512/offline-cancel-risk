# Ownership split (not a live-GPS cutover)

This repo **produces scores**. It is not a live GPS product, LBS owner, or enforcement service.

| Owner | Owns | Does not own |
|---|---|---|
| **This repo** | Three-head risk scores, flags, EAR, reason/evidence payloads, shadow calibration math | GPS ingest, live tracking, payout/suspend/clawback |
| **Downstream** | GPS ingest (or injected tracks), order feeds, enforcement | Score math / policy thresholds in this toolkit |

Assess is offline/batch. A GPS adapter or CSV track is an input, not a live location stream. Missing GPS degrades scores (`gps_unavailable` / sparse) — it does not invent a live track.

`calibration.mode` stays **`shadow`** by default: fit may persist calibrators and write `calibration_meta`; live `scores` stay uncalibrated unless Downstream later changes policy. This doc does not promote apply.

See [README](../README.md) for the score contract and [MANUAL.md](MANUAL.md) to run the CSV demo with no network.
