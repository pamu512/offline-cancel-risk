"""P0/P1 thin-spot upgrades: GPS prod, presence fill, EAR shadow, transit slice."""

from __future__ import annotations

from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from offline_cancel_risk.adapters.gps import FakeGpsClient
from offline_cancel_risk.api.schemas import AssessmentResult, ThreeHeadFlags, ThreeHeadMlScores, ThreeHeadScores
from offline_cancel_risk.domain.models import GpsPoint
from offline_cancel_risk.eval.holdout import transit_dwell_cases
from offline_cancel_risk.features.dwell import dwell_stop_mask
from offline_cancel_risk.main import create_app
from offline_cancel_risk.ops.presence_fill import presence_fill_report
from offline_cancel_risk.scoring.ear import ear_shadow_delta_report
from offline_cancel_risk.settings import Settings, load_policy


def _settings(tmp_path: Path, **kwargs) -> Settings:
    base = dict(
        sync_assess=True,
        sqlite_path=str(tmp_path / "assessments.db"),
        stream_path=str(tmp_path / "risk_events.jsonl"),
        policy_path=str(Path("config/policy.default.yaml").resolve()),
        control_plane_sqlite_path=str(tmp_path / "control_plane.db"),
        assess_queue_path=str(tmp_path / "assess_queue.db"),
        label_tickets_path=str(tmp_path / "tickets.db"),
        label_tickets_stream_path=str(tmp_path / "tickets.jsonl"),
        outcomes_path=str(tmp_path / "outcomes.db"),
    )
    base.update(kwargs)
    return Settings(**base)


def test_prod_create_app_requires_gps_url_or_inject(tmp_path: Path):
    with pytest.raises(RuntimeError, match="OCR_GPS_BASE_URL"):
        create_app(
            settings=_settings(tmp_path, profile="prod", api_keys="k", gps_base_url="")
        )


def test_prod_create_app_ok_with_injected_gps(tmp_path: Path):
    app = create_app(
        gps_client=FakeGpsClient(
            [GpsPoint(1.0, 2.0, "2024-01-01 10:00:00", 0.0)]
        ),
        settings=_settings(tmp_path, profile="prod", api_keys="k", gps_base_url=""),
    )
    assert app is not None


@pytest.mark.asyncio
async def test_ready_503_when_empty_fake_gps(tmp_path: Path):
    app = create_app(
        gps_client=FakeGpsClient([]),
        settings=_settings(tmp_path, profile="prod", api_keys="k", gps_base_url=""),
    )
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as ac:
        r = await ac.get("/v1/ready")
        assert r.status_code == 503
        assert r.json()["detail"]["gps_configured"] is False


def test_presence_fill_report_rates():
    def _res(place: str, vehicle: str) -> AssessmentResult:
        return AssessmentResult(
            order_display_id="o",
            driver_id=1,
            scores=ThreeHeadScores(
                cancelled_offline=0.0, cancel_abuse=0.0, selective_theft=0.0
            ),
            flags=ThreeHeadFlags(
                cancelled_offline=0, cancel_abuse=0, selective_theft=0
            ),
            expected_revenue_at_risk={
                "cancelled_offline": 0.0,
                "cancel_abuse": 0.0,
                "selective_theft": 0.0,
                "total": 0.0,
            },
            attention_score=0.0,
            reasons=[],
            rule_scores=ThreeHeadScores(
                cancelled_offline=0.0, cancel_abuse=0.0, selective_theft=0.0
            ),
            ml_scores=ThreeHeadMlScores(
                cancelled_offline=None, cancel_abuse=None, selective_theft=None
            ),
            gps_window={
                "presence_place_class": place,
                "presence_vehicle_class": vehicle,
            },
            lineage_id="l",
            assessment_generation=1,
            provisional=False,
            policy_hash="p",
            model_version="none",
            twin_version="none",
            graph_version="none",
            feature_vector_ref="none",
            assessed_at="2024-01-01T00:00:00Z",
        )

    rep = presence_fill_report(
        [
            _res("apartment", "semi"),
            _res("unknown", "unknown"),
            _res("curb", "unknown"),
        ]
    )
    assert rep["n"] == 3
    assert rep["place_fill_rate"] == pytest.approx(2 / 3)
    assert rep["vehicle_fill_rate"] == pytest.approx(1 / 3)


def test_ear_shadow_delta_apply_ready():
    policy = load_policy("config/policy.default.yaml")
    learned = {
        "cancelled_offline": {"value": 0.9, "n_updates": 10},
        "cancel_abuse": {"value": 0.3, "n_updates": 10},
        "selective_theft": {"value": 0.7, "n_updates": 10},
    }
    rep = ear_shadow_delta_report(policy, learned)
    assert rep["market_apply_ready"] is True
    assert rep["recommendation"] == "consider_apply"
    assert rep["heads"]["cancelled_offline"]["delta"] == pytest.approx(-0.1)


def test_ear_shadow_ignores_cold_heads_for_market_ready():
    policy = load_policy("config/policy.default.yaml")
    # Only theft has outcomes; abuse/offline never updated — should not block.
    learned = {
        "selective_theft": {"value": 0.7, "n_updates": 10},
    }
    rep = ear_shadow_delta_report(policy, learned)
    assert rep["updated_heads"] == 1
    assert rep["market_apply_ready"] is True
    assert rep["heads"]["cancel_abuse"]["apply_ready"] is False


def test_transit_crawl_vs_stationary_dwell():
    cases = {c.strata[0]: c for c in transit_dwell_cases()}
    policy = {
        "min_dwell_seconds": 120,
        "max_speed_mps": 1.5,
        "radius_m": 80,
        "max_run_displacement_m": 40,
    }
    stop = (14.5500, 121.0200)
    assert dwell_stop_mask(cases["transit_crawl"].points, stop, policy) is False
    assert dwell_stop_mask(cases["stationary_dwell"].points, stop, policy) is True


def test_policy_abuse_apply_theft_shadow():
    policy = load_policy("config/policy.default.yaml")
    assert policy["anomaly"]["mode"] == "apply"
    assert policy["baselines"]["mode"] == "apply"
    assert policy["baselines"]["heads"]["selective_theft"]["mode"] == "shadow"
