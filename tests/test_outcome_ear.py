from offline_cancel_risk.outcomes.ewma import ewma_update, signal_for_outcome


def test_signal_and_ewma():
    assert signal_for_outcome("clawback_won") == 1.0
    assert signal_for_outcome("clawback_lost") == 0.0
    assert abs(ewma_update(0.8, 0.0, 0.05) - 0.76) < 1e-9


def test_store_persist_and_idempotent(tmp_path):
    from offline_cancel_risk.outcomes.store import OutcomeStore

    store = OutcomeStore(tmp_path / "o.db")
    cold = {"cancelled_offline": 1.0, "cancel_abuse": 0.4, "selective_theft": 0.8}
    guard = {
        "ear.recoverability.cancelled_offline": {"min": 0.0, "max": 1.0},
        "ear.recoverability.cancel_abuse": {"min": 0.0, "max": 1.0},
        "ear.recoverability.selective_theft": {"min": 0.0, "max": 1.0},
    }
    r1 = store.record_outcome(
        order_display_id="O1",
        outcome="clawback_won",
        head="selective_theft",
        region_code="PH",
        city_code="MNL",
        alpha=0.05,
        cold_start=cold,
        guardrails=guard,
        occurred_at="2024-01-01T00:00:00Z",
    )
    r2 = store.record_outcome(
        order_display_id="O1",
        outcome="clawback_won",
        head="selective_theft",
        region_code="PH",
        city_code="MNL",
        alpha=0.05,
        cold_start=cold,
        guardrails=guard,
        occurred_at="2024-01-01T00:00:00Z",
    )
    assert r1["n_updates"] == 1
    assert r2.get("duplicate") is True or r2["n_updates"] == 1
    store2 = OutcomeStore(tmp_path / "o.db")
    got = store2.get_recoverability("PH", "MNL")
    assert got["selective_theft"]["n_updates"] == 1
