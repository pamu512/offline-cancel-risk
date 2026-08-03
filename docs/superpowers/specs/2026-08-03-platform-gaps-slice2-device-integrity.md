# Platform Gaps Slice 2 — Device Integrity

**Status:** Done  
**Order:** 2 of 5

## Math (assessed)

Downstream posts binary integrity flags + optional vendor `risk_score ∈ [0,1]`.

**Instant risk** (avoid naive sum of booleans — that double-counts correlated signals):

\[
f = \max\begin{cases}
0.85 & \text{spoof\_suspected}\\
0.80 & \text{fake\_app}\\
0.75 & \text{emulator}\\
0.55 & \text{rooted}\\
0 & \text{else}
\end{cases}
\qquad
R_{\mathrm{inst}} = \mathrm{clip}_{[0,1]}\big(\max(r_{\mathrm{vendor}}, f)\big)
\]

**Persistence (EWMA)** so one-off blips don’t dominate, but repeat offenders stay hot:

\[
R_{t} = \alpha R_{\mathrm{inst}} + (1-\alpha) R_{t-1}
\]

**Fire** if \(R_{\mathrm{inst}} \ge \tau\) **or** \(R_t \ge \tau\) (default \(\tau=0.7\), \(\alpha=0.4\)).

**Score impact:** abuse bonus \(\beta \cdot R_{\mathrm{eff}}\) (not a flat +0.15 for every flag).  
**GPS impact:** if spoof/emulator, dampen stop confidence by \((1 - \delta R_{\mathrm{eff}})\).

## Non-goals

SDK installation, attestation crypto, multi-account graph (slice 3).
