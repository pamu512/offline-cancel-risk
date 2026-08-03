"""Characterization: assess scores must stay bit-stable across refactors."""

from datetime import datetime, timedelta
from pathlib import Path

import pytest

from offline_cancel_risk.adapters.gps import FakeGpsClient
from offline_cancel_risk.adapters.publishers import JsonlStreamPublisher, SqliteTablePublisher
from offline_cancel_risk.api.schemas import AssessRequest
from offline_cancel_risk.domain.models import GpsPoint
from offline_cancel_risk.pipeline.assess import assess_order
from offline_cancel_risk.settings import load_policy

PICKUP = (14.5500, 121.0200)
DEST = (14.6500, 121.0800)

_GOLD = {
    "GOLD-THEFT": {
        "scores": {
            "cancel_abuse": 0.2,
            "cancelled_offline": 0.6,
            "selective_theft": 1.0,
        },
        "flags": {
            "cancel_abuse": 0,
            "cancelled_offline": 0,
            "selective_theft": 1,
        },
        "rule_scores": {
            "cancel_abuse": 0.2,
            "cancelled_offline": 0.6,
            "selective_theft": 1.0,
        },
        "reasons": [
            "cancel_after_pickup",
            "food_category",
            "high_value",
            "next_driver_no_order",
            "cancel_after_pickup",
            "no_replacement",
            "pickup_only",
            "heading_unavailable",
            "stage:at_merchant",
            "gps_sparse",
            "gps_gaps",
        ],
        "cancel_stage": "at_merchant",
        "provisional": True,
        "attention_score": 1292.8,
        "ear": {
            "cancel_abuse": 64.0,
            "cancelled_offline": 480.0,
            "selective_theft": 640.0,
            "total": 1184.0,
        },
    },
    "GOLD-PLAIN": {
        "scores": {
            "cancel_abuse": 0.2,
            "cancelled_offline": 0.6,
            "selective_theft": 0.0,
        },
        "flags": {
            "cancel_abuse": 0,
            "cancelled_offline": 0,
            "selective_theft": 0,
        },
        "rule_scores": {
            "cancel_abuse": 0.2,
            "cancelled_offline": 0.6,
            "selective_theft": 0.0,
        },
        "reasons": [
            "cancel_after_pickup",
            "no_replacement",
            "pickup_only",
            "heading_unavailable",
            "stage:at_merchant",
            "gps_sparse",
            "gps_gaps",
        ],
        "cancel_stage": "at_merchant",
        "provisional": True,
        "attention_score": 32.8,
        "ear": {
            "cancel_abuse": 4.0,
            "cancelled_offline": 30.0,
            "selective_theft": 0.0,
            "total": 34.0,
        },
    },
}


def _pickup_cluster(n: int = 40) -> list[GpsPoint]:
    base = datetime(2024, 1, 1, 10, 0, 0)
    points: list[GpsPoint] = []
    for i in range(n):
        points.append(
            GpsPoint(
                lat=PICKUP[0] + (i % 5) * 0.00001,
                lon=PICKUP[1] + (i % 3) * 0.00001,
                ts=(base + timedelta(minutes=i * 2)).strftime("%Y-%m-%d %H:%M:%S"),
                speed_mps=0.3,
            )
        )
    return points


def _req(order_id: str, *, theft: bool) -> AssessRequest:
    if theft:
        return AssessRequest(
            order_display_id=order_id,
            driver_id=42,
            cancel_ts="2024-01-01 11:20:00",
            assign_ts="2024-01-01 10:00:00",
            latlong=f"{PICKUP[0]}|{PICKUP[1]},{DEST[0]}|{DEST[1]}",
            path_point_num=2,
            order_status="CANCELLED",
            category="FOOD",
            order_value=800.0,
            currency="PHP",
            next_driver_no_order=True,
        )
    return AssessRequest(
        order_display_id=order_id,
        driver_id=7,
        cancel_ts="2024-01-01 11:20:00",
        assign_ts="2024-01-01 10:00:00",
        latlong=f"{PICKUP[0]}|{PICKUP[1]},{DEST[0]}|{DEST[1]}",
        path_point_num=2,
        order_status="CANCELLED",
        category="HAUL",
        order_value=50.0,
        currency="PHP",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("order_id,theft", [("GOLD-THEFT", True), ("GOLD-PLAIN", False)])
async def test_assess_golden_scores(tmp_path: Path, order_id: str, theft: bool):
    policy = load_policy(Path("config/policy.default.yaml"))
    req = _req(order_id, theft=theft)
    result = await assess_order(
        req,
        FakeGpsClient(_pickup_cluster()),
        policy,
        stream=JsonlStreamPublisher(stream_path=str(tmp_path / f"{order_id}.jsonl")),
        table=SqliteTablePublisher(sqlite_path=str(tmp_path / f"{order_id}.db")),
    )
    gold = _GOLD[order_id]
    assert result.scores.model_dump() == gold["scores"]
    assert result.flags.model_dump() == gold["flags"]
    assert result.rule_scores.model_dump() == gold["rule_scores"]
    assert result.reasons == gold["reasons"]
    assert result.cancel_stage == gold["cancel_stage"]
    assert result.provisional is gold["provisional"]
    assert result.attention_score == gold["attention_score"]
    assert result.expected_revenue_at_risk.model_dump() == gold["ear"]
