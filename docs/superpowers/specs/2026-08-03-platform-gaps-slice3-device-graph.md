# Platform Gaps Slice 3 — Multi-Account / Device Graph

**Status:** Done  
**Order:** 3 of 5

## Math (assessed)

Bipartite edges from assess ingest (Downstream `device_id` + `driver_id` / `user_id`). No community detection — counts in a lookback window.

| Metric | Definition | Why |
|---|---|---|
| **Drivers/device** | \(\lvert\{d : (device, driver\,d)\}\rvert\) | Shared phone across courier accounts |
| **Users/device** | \(\lvert\{u : (device, user\,u)\}\rvert\) | Multi-account customer / mule phone |
| **Devices/driver** | \(\lvert\{v : (v, driver)\}\rvert\) | Device hopping to evade integrity |
| **Shared pair** | device has edges to **both** this driver and this user | Collusion / same-device ring |

**Support:** fire count signals only if device (or driver) edge sightings \(\ge n_{\min}\) in window — avoids one-off ID collisions.

**Abuse triggers:**

- `multi_account_device`: drivers/device \(\ge \tau_d\)
- `multi_user_device`: users/device \(\ge \tau_u\)
- `device_hopping`: devices/driver \(\ge \tau_v\)
- `shared_device_pair`: shared pair true (and optional min sightings each side)

Bonuses are flat (policy), same style as marketplace signals.

## Ingest

- Assess: observe `(device_id → driver_id)` automatically.
- `POST /v1/device-graph/edges`: Downstream posts user↔device (and extra driver) links for multi-user / shared-pair.

## Non-goals

Full graph embeddings, phone/email identity resolution, slice-2 integrity SDK.
