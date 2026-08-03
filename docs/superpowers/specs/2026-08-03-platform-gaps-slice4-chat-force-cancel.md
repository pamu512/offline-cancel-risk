# Platform Gaps Slice 4 — Force-Cancel / Chat Ingest

**Status:** Done  
**Order:** 4 of 5

## Math (assessed)

No chat NLP in this service. Downstream posts structured flags + optional `signal_score ∈ [0,1]`.

**Instant risk** (max, not sum — same discipline as device integrity):

\[
f = \max\begin{cases}
0.90 & \text{rider\_forced\_cancel}\\
0.85 & \text{cash\_offline\_suggested}\\
0.70 & \text{persuasion\_suspected}\\
0 & \text{else}
\end{cases}
\qquad
R = \mathrm{clip}_{[0,1]}\big(\max(r_{\mathrm{vendor}}, f)\big)
\]

**Fire** if \(R \ge \tau\) (default \(0.55\)). Abuse bonus \(\beta \cdot R\).

**Stall combo:** if fire **and** (`no_progress` ∨ `wrong_direction`), add flat `force_cancel_with_stall` bonus — matches cancel-to-collect / fee-abuse pattern.

**Repeat:** driver signal events in lookback \(\ge n_{\min}\) → `repeat_force_cancel`.

## Ingest

- Assess field `chat_signals: {persuasion_suspected, cash_offline_suggested, rider_forced_cancel, signal_score}`
- `POST /v1/chat-signals` keyed by `order_display_id` (async before/after assess)
- Keyword / model lists stay Downstream

## Non-goals

Chat NLP, transcript storage, phone-call ASR.
