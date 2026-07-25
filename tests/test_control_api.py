from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from offline_cancel_risk.adapters.gps import FakeGpsClient
from offline_cancel_risk.main import create_app
from offline_cancel_risk.settings import Settings


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        sync_assess=True,
        sqlite_path=str(tmp_path / "assessments.db"),
        stream_path=str(tmp_path / "risk_events.jsonl"),
        policy_path=str(Path("config/policy.default.yaml").resolve()),
        policy_guardrails_path=str(
            Path("config/policy_guardrails.default.yaml").resolve()
        ),
        policy_overlays_path=str(tmp_path / "overlays.db"),
        control_plane_sqlite_path=str(tmp_path / "control_plane.db"),
        operating_point_path=str(
            Path("config/operating_point.default.yaml").resolve()
        ),
        tuner_min_labeled=2,
        tuner_cooldown_minutes=0,
    )


@pytest.mark.asyncio
async def test_forecast_hardgate_tune_audit_flow(tmp_path: Path):
    app = create_app(gps_client=FakeGpsClient([]), settings=_settings(tmp_path))
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as ac:
        r = await ac.put(
            "/v1/supply/forecast",
            json={
                "rows": [
                    {
                        "region_code": "PH",
                        "city_code": "MNL",
                        "period_start": "2020-01-01T00:00:00Z",
                        "period_end": "2099-01-01T00:00:00Z",
                        "forecast_supply": 120.0,
                        "forecast_demand": 100.0,
                        "source": "driver_ops",
                    }
                ]
            },
        )
        assert r.status_code == 200, r.text
        assert r.json()["upserted"] == 1

        hg = await ac.put(
            "/v1/enforcement/hardgates",
            json={
                "region_code": "PH",
                "city_code": "MNL",
                "window": "hour",
                "max_enforcements": 100,
            },
        )
        assert hg.status_code == 200, hg.text

        tune = await ac.post(
            "/v1/tuning/run",
            json={"region_code": "PH", "city_code": "MNL"},
        )
        assert tune.status_code == 200, tune.text
        assert "decisions" in tune.json()
        assert "metrics" in tune.json()

        metrics = await ac.get(
            "/v1/metrics/labels",
            params={"region_code": "PH", "city_code": "MNL"},
        )
        assert metrics.status_code == 200

        cb = await ac.post(
            "/v1/enforcement/clawback",
            json={
                "region_code": "PH",
                "city_code": "MNL",
                "ttl_minutes": 15,
                "reason": "supply_dip",
            },
        )
        assert cb.status_code == 200
        assert "until_ts" in cb.json()

        audit = await ac.get("/v1/audit/policy", params={"limit": 50})
        assert audit.status_code == 200
        actions = {row["action"] for row in audit.json()}
        assert "forecast_ingest" in actions
        assert "hardgate_ingest" in actions
        assert "clawback_signal" in actions
