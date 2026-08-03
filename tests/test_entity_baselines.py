from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from offline_cancel_risk.baselines.gate import (
    apply_baselines,
    band_eligible,
    prune_window,
)
from offline_cancel_risk.baselines.store import EntityBaselineStore
from offline_cancel_risk.control_plane.metrics import resolve_scores
from offline_cancel_risk.main import create_app
from offline_cancel_risk.settings import Settings, load_policy


def _cfg(**overrides) -> dict:
    base = {
        "mode": "apply",
        "window_n": 5,
        "pair_window_n": 3,
        "under_fraction": 0.8,
        "ewma_alpha": 0.3,
        "ewma_delta": 0.05,
        "min_ewma_samples": 5,
        "above_epsilon": 0.1,
        "above_fraction": 0.8,
        "discount": 0.85,
        "refresh_epsilon": 0.05,
        "max_age_days": 90,
        "heads": {
            "cancelled_offline": {"enabled": True},
            "cancel_abuse": {"enabled": True, "discount": 0.9},
            "selective_theft": {"enabled": True, "mode": "shadow"},
        },
    }
    base.update(overrides)
    return {"baselines": base, "thresholds": {
        "cancelled_offline": 0.75,
        "cancel_abuse": 0.75,
        "selective_theft": 0.75,
    }}


def _feed(
    store: EntityBaselineStore,
    policy: dict,
    *,
    score: float,
    n: int,
    driver_id: int = 1,
    user_id: int | None = None,
    start: datetime | None = None,
) -> dict[str, float]:
    thr = policy["thresholds"]
    t0 = start or datetime(2026, 8, 1, tzinfo=timezone.utc)
    scores = {
        "cancelled_offline": score,
        "cancel_abuse": 0.1,
        "selective_theft": 0.1,
    }
    last = scores
    for i in range(n):
        ts = (t0 + timedelta(hours=i)).isoformat().replace("+00:00", "Z")
        last, _, _ = apply_baselines(
            store,
            scores=scores,
            thresholds=thr,
            policy=policy,
            driver_id=driver_id,
            user_id=user_id,
            assessed_at=ts,
        )
    return last


def test_prune_window_drops_old_and_trims():
    now = datetime(2026, 8, 1, tzinfo=timezone.utc)
    window = [
        {"s": 0.1, "t": (now - timedelta(days=100)).isoformat().replace("+00:00", "Z")},
        {"s": 0.2, "t": (now - timedelta(days=1)).isoformat().replace("+00:00", "Z")},
        {"s": 0.3, "t": now.isoformat().replace("+00:00", "Z")},
        {"s": 0.4, "t": now.isoformat().replace("+00:00", "Z")},
    ]
    kept = prune_window(window, max_age_days=90, max_len=2, now=now)
    assert len(kept) == 2
    assert kept[0]["s"] == 0.3


def test_band_eligible():
    assert band_eligible(0.5, baseline=0.2, armed_thr=0.75, eps=0.1)
    assert not band_eligible(0.8, baseline=0.2, armed_thr=0.75, eps=0.1)
    assert not band_eligible(0.25, baseline=0.2, armed_thr=0.75, eps=0.1)


def test_apply_discounts_in_band(tmp_path: Path):
    store = EntityBaselineStore(tmp_path / "b.db")
    policy = _cfg(mode="apply")
    _feed(store, policy, score=0.2, n=5)
    out = _feed(store, policy, score=0.5, n=5)
    assert out["cancelled_offline"] == pytest.approx(0.5 * 0.85)
    row = store.get("driver:1", "cancelled_offline")
    assert row is not None
    assert row["baseline"] is not None
    assert row["armed_thr"] == 0.75
    assert row["discount_active"] is True


def test_no_discount_at_or_above_threshold(tmp_path: Path):
    store = EntityBaselineStore(tmp_path / "b.db")
    policy = _cfg(mode="apply")
    _feed(store, policy, score=0.2, n=5)
    out = _feed(store, policy, score=0.8, n=5)
    assert out["cancelled_offline"] == pytest.approx(0.8)


def test_shadow_does_not_change_scores(tmp_path: Path):
    store = EntityBaselineStore(tmp_path / "b.db")
    policy = _cfg(mode="shadow")
    _feed(store, policy, score=0.2, n=5)
    out, reasons, meta = apply_baselines(
        store,
        scores={
            "cancelled_offline": 0.5,
            "cancel_abuse": 0.1,
            "selective_theft": 0.1,
        },
        thresholds=policy["thresholds"],
        policy=policy,
        driver_id=1,
        user_id=None,
        assessed_at="2026-08-02T00:00:00Z",
    )
    # Need above-consistent window — feed 4 more at 0.5 first via _feed partial
    _feed(store, policy, score=0.5, n=4)
    out, reasons, meta = apply_baselines(
        store,
        scores={
            "cancelled_offline": 0.5,
            "cancel_abuse": 0.1,
            "selective_theft": 0.1,
        },
        thresholds=policy["thresholds"],
        policy=policy,
        driver_id=1,
        user_id=None,
        assessed_at="2026-08-03T00:00:00Z",
    )
    assert out["cancelled_offline"] == pytest.approx(0.5)
    assert any(r.startswith("baseline_shadow:") for r in reasons)
    assert meta["cancelled_offline"]["mode"] == "shadow"


def test_user_and_pair_entities(tmp_path: Path):
    store = EntityBaselineStore(tmp_path / "b.db")
    policy = _cfg(mode="shadow")
    _feed(store, policy, score=0.2, n=5, user_id=99)
    assert store.get("driver:1", "cancelled_offline") is not None
    assert store.get("user:99", "cancelled_offline") is not None
    assert store.get("pair:1:99", "cancelled_offline") is not None
    pair = store.get("pair:1:99", "cancelled_offline")
    assert pair is not None
    assert len(pair["window"]) <= 3  # pair_window_n


def test_theft_stays_shadow_when_global_apply(tmp_path: Path):
    store = EntityBaselineStore(tmp_path / "b.db")
    policy = _cfg(mode="apply")
    thr = policy["thresholds"]
    for i in range(5):
        apply_baselines(
            store,
            scores={
                "cancelled_offline": 0.2,
                "cancel_abuse": 0.2,
                "selective_theft": 0.2,
            },
            thresholds=thr,
            policy=policy,
            driver_id=1,
            user_id=None,
            assessed_at=f"2026-08-01T0{i}:00:00Z",
        )
    for i in range(5):
        out, reasons, meta = apply_baselines(
            store,
            scores={
                "cancelled_offline": 0.5,
                "cancel_abuse": 0.5,
                "selective_theft": 0.5,
            },
            thresholds=thr,
            policy=policy,
            driver_id=1,
            user_id=None,
            assessed_at=f"2026-08-03T0{i}:00:00Z",
        )
    assert out["selective_theft"] == pytest.approx(0.5)
    assert "selective_theft" in meta
    assert meta["selective_theft"]["mode"] == "shadow"
    assert any(r == "baseline_shadow:driver" for r in reasons)


def test_resolve_scores_prefers_scores_raw():
    assess = {
        "scores": {"cancelled_offline": 0.4, "cancel_abuse": 0.1, "selective_theft": 0.1},
        "scores_raw": {
            "cancelled_offline": 0.5,
            "cancel_abuse": 0.1,
            "selective_theft": 0.1,
        },
    }
    resolved = resolve_scores(assess, blend=None)
    assert resolved["cancelled_offline"] == 0.5


def test_updated_since_query(tmp_path: Path):
    store = EntityBaselineStore(tmp_path / "b.db")
    policy = _cfg(mode="shadow")
    _feed(store, policy, score=0.2, n=1)
    rows = store.query(head="cancelled_offline", limit=10)
    assert len(rows) == 1
    ts = rows[0]["updated_at"]
    assert store.query(head="cancelled_offline", updated_since=ts, limit=10) == []
    _feed(store, policy, score=0.25, n=1)
    later = store.query(head="cancelled_offline", updated_since=ts, limit=10)
    assert len(later) == 1


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        sync_assess=True,
        sqlite_path=str(tmp_path / "a.db"),
        stream_path=str(tmp_path / "r.jsonl"),
        policy_path=str(Path("config/policy.default.yaml").resolve()),
        policy_guardrails_path=str(
            Path("config/policy_guardrails.default.yaml").resolve()
        ),
        policy_overlays_path=str(tmp_path / "o.db"),
        control_plane_sqlite_path=str(tmp_path / "cp.db"),
        label_tickets_path=str(tmp_path / "tickets.db"),
        label_tickets_stream_path=str(tmp_path / "tickets.jsonl"),
        entity_baselines_path=str(tmp_path / "baselines.db"),
        operating_point_path=str(
            Path("config/operating_point.default.yaml").resolve()
        ),
    )


@pytest.mark.asyncio
async def test_baselines_api(tmp_path: Path):
    from offline_cancel_risk.adapters.gps import FakeGpsClient

    app = create_app(gps_client=FakeGpsClient([]), settings=_settings(tmp_path))
    store: EntityBaselineStore = app.state.baselines
    policy = load_policy("config/policy.default.yaml")
    policy = dict(policy)
    policy["baselines"] = _cfg(mode="shadow")["baselines"]
    # Seed via store API used by gate
    apply_baselines(
        store,
        scores={
            "cancelled_offline": 0.2,
            "cancel_abuse": 0.1,
            "selective_theft": 0.1,
        },
        thresholds=policy["thresholds"],
        policy=policy,
        driver_id=42,
        user_id=None,
        assessed_at="2026-08-01T00:00:00Z",
    )
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as ac:
        r = await ac.get("/v1/baselines", params={"driver_id": 42})
        assert r.status_code == 200
        assert len(r.json()) >= 1
        r2 = await ac.get("/v1/baselines/driver:42")
        assert r2.status_code == 200
        assert all(row["entity_key"] == "driver:42" for row in r2.json())
