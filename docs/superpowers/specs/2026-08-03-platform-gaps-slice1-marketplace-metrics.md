# Platform Gaps Slice 1 — Marketplace Metrics

**Status:** Implementing  
**Order:** 1 of 5 (device integrity → graph → chat → anomaly next)

## Math (assessed)

| Metric | Definition | Why |
|---|---|---|
| **ACR** (accept→cancel rate) | \(C / \max(A,1)\) in window | Cancels/hour confuses volume with abuse; ratio to accepts matches platforms |
| **CR** (completion rate) | \(K / \max(K+C,1)\) | Terminal-outcome completion; stable if accept stream incomplete |
| **WCF** (with-cause fraction) | \(W / \max(C,1)\) | High ACR + low WCF = cancel abuse without cause |
| **Support gate** | apply ACR/CR/WCF flags only if \(A \ge n_{\min}\) or \(C \ge n_{\min}\) | Avoids small-sample explosions |

**Abuse triggers (AND/OR as configured):**

- `high_accept_cancel_rate`: \(A \ge n_{\min}\) and \(\mathrm{ACR} \ge \tau_{\mathrm{acr}}\)
- `low_completion_rate`: \((K+C) \ge n_{\min}\) and \(\mathrm{CR} \le \tau_{\mathrm{cr}}\)
- `cancel_without_cause_heavy`: \(C \ge n_{\min}\) and \(\mathrm{WCF} \le \tau_{\mathrm{wcf}}\) (and optionally \(\mathrm{ACR} \ge \tau_{\mathrm{acr}}\))

Legacy `cancels/hour` kept as secondary volume signal (`high_cancel_rate`).

## Ingest

On assess: record `accept` at `accepted_at` or `assign_ts`, `cancel` at `cancel_ts` with `cancel_with_cause` / `cancel_reason_code`.  
Optional `marketplace_events[]` for Downstream completes/accepts not tied to this assess.

## Non-goals

Wilson intervals, peer z-scores (slice 5), device graph (slice 3).
