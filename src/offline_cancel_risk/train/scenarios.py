from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import numpy as np

from offline_cancel_risk.api.schemas import AssessRequest
from offline_cancel_risk.domain.models import GpsPoint

HEADS = ("cancelled_offline", "cancel_abuse", "selective_theft")

TEMPLATES: dict[str, tuple[int, int, int]] = {
    "theft_dwell": (0, 0, 1),
    "plain_offline": (1, 0, 0),
    "abuse_chain": (0, 1, 0),
    "clean_cancel": (0, 0, 0),
    "gps_sparse": (0, 0, 0),
}

DEFAULT_WEIGHTS: dict[str, float] = {
    "theft_dwell": 0.20,
    "plain_offline": 0.25,
    "abuse_chain": 0.15,
    "clean_cancel": 0.30,
    "gps_sparse": 0.10,
}

PICKUP = (14.5500, 121.0200)
DEST = (14.6500, 121.0800)


@dataclass(frozen=True)
class ScenarioRow:
    template: str
    request: AssessRequest
    points: list[GpsPoint]
    labels: dict[str, int]


def draw_template(rng: np.random.Generator, weights: dict[str, float] | None = None) -> str:
    w = weights or DEFAULT_WEIGHTS
    names = list(w.keys())
    p = np.array([w[n] for n in names], dtype=float)
    p = p / p.sum()
    return str(rng.choice(names, p=p))


def _cluster(
    base: datetime, n: int, lat: float, lon: float, *, jitter: float, speed: float
) -> list[GpsPoint]:
    pts: list[GpsPoint] = []
    for i in range(n):
        pts.append(
            GpsPoint(
                lat=lat + (i % 5) * jitter,
                lon=lon + (i % 3) * jitter,
                ts=(base + timedelta(minutes=i * 2)).strftime("%Y-%m-%d %H:%M:%S"),
                speed_mps=speed,
            )
        )
    return pts


def build_scenario(
    template: str,
    *,
    order_display_id: str,
    driver_id: int,
    rng: np.random.Generator,
) -> ScenarioRow:
    if template not in TEMPLATES:
        raise ValueError(f"unknown template: {template}")
    offline, abuse, theft = TEMPLATES[template]
    labels = {
        "cancelled_offline": offline,
        "cancel_abuse": abuse,
        "selective_theft": theft,
    }
    base = datetime(2024, 1, 1, 10, 0, 0)
    j = float(rng.uniform(0.000005, 0.00002))
    assign_ts = base.strftime("%Y-%m-%d %H:%M:%S")
    cancel_ts = (base + timedelta(hours=1, minutes=20)).strftime("%Y-%m-%d %H:%M:%S")
    latlong = f"{PICKUP[0]}|{PICKUP[1]},{DEST[0]}|{DEST[1]}"
    kwargs: dict[str, Any] = dict(
        order_display_id=order_display_id,
        driver_id=driver_id,
        cancel_ts=cancel_ts,
        assign_ts=assign_ts,
        latlong=latlong,
        path_point_num=2,
        order_status="CANCELLED",
        category="FOOD",
        order_value=800.0 if template == "theft_dwell" else 120.0,
        currency="PHP",
        next_driver_no_order=template in {"theft_dwell", "plain_offline"},
        region_code="PH",
        city_code="MNL",
    )
    if template == "theft_dwell":
        points = _cluster(base, 40, PICKUP[0], PICKUP[1], jitter=j, speed=0.2)
        kwargs["order_value"] = 800.0
        kwargs["category"] = "FOOD"
    elif template == "plain_offline":
        points = _cluster(base, 25, PICKUP[0], PICKUP[1], jitter=j, speed=0.3)
        kwargs["order_value"] = 50.0
    elif template == "abuse_chain":
        points = _cluster(base, 20, PICKUP[0], PICKUP[1], jitter=j, speed=1.0)
        kwargs["reassign_cancel_events"] = [
            {"ts": (base + timedelta(minutes=10)).strftime("%Y-%m-%d %H:%M:%S"), "driver_id": driver_id},
            {"ts": (base + timedelta(minutes=20)).strftime("%Y-%m-%d %H:%M:%S"), "driver_id": driver_id},
            {"ts": (base + timedelta(minutes=30)).strftime("%Y-%m-%d %H:%M:%S"), "driver_id": driver_id},
        ]
        kwargs["order_value"] = 200.0
    elif template == "clean_cancel":
        points = _cluster(base, 12, PICKUP[0] + 0.01, PICKUP[1] + 0.01, jitter=j, speed=5.0)
        kwargs["order_value"] = 80.0
        kwargs["next_driver_no_order"] = False
        cancel_ts = (base + timedelta(minutes=15)).strftime("%Y-%m-%d %H:%M:%S")
        kwargs["cancel_ts"] = cancel_ts
    else:  # gps_sparse
        points = _cluster(base, 2, PICKUP[0], PICKUP[1], jitter=j, speed=0.0)
        kwargs["order_value"] = 90.0
    req = AssessRequest(**kwargs)
    return ScenarioRow(template=template, request=req, points=points, labels=labels)
