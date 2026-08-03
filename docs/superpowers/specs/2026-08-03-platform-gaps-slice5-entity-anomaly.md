# Platform Gaps Slice 5 — Entity Anomaly Watch (Light)

**Status:** Done  
**Order:** 5 of 5

## Math (assessed)

Not full Uber Risk Entity Watch. Rolling **robust z-scores** (MAD) on a few features vs **self history** and **city/region peer cohort**.

For feature value \(x\) and reference sample \(S\) (\(|S|\ge n_{\min}\)):

\[
\mathrm{MAD} = \mathrm{median}_i\big(|S_i - \mathrm{median}(S)|\big)
\qquad
z = 0.6745\cdot\frac{x - \mathrm{median}(S)}{\max(\mathrm{MAD},\,\varepsilon)}
\]

**Fire** when \(z \ge \tau\) (default \(3.0\)):

- `anomaly_self` — \(S\) = entity’s own prior values
- `anomaly_peer` — \(S\) = other entities in same cohort (`city:` → `region:` → `global`)

**Features (default):** `accept_cancel_rate`, `cancel_rate`, `cancel_abuse` (pre-anomaly abuse score).

**Mode:** `shadow` (default) → reasons + evidence only; `apply` → flat abuse bonus; `off` → skip.

## Non-goals

Full unsupervised platform, auto-ban, embeddings, merchant cohorts.
