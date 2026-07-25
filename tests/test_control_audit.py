from pathlib import Path

from offline_cancel_risk.control_plane.audit import PolicyAuditLog


def test_append_and_list(tmp_path: Path):
    log = PolicyAuditLog(tmp_path / "cp.db")
    aid = log.append(
        actor="tuner",
        action="suggest",
        region_code="PH",
        city_code="MNL",
        before={"thresholds": {"cancelled_offline": 0.75}},
        after={"thresholds": {"cancelled_offline": 0.8}},
        metrics_before={"f1": 0.7},
        metrics_after={"f1": 0.72},
        constraints={"min_precision": 0.8},
        decision="accepted",
        reason="f1_lift",
    )
    rows = log.list_entries(limit=10)
    assert len(rows) == 1
    assert rows[0]["audit_id"] == aid
    assert rows[0]["action"] == "suggest"
    assert rows[0]["region_code"] == "PH"
    assert rows[0]["before"]["thresholds"]["cancelled_offline"] == 0.75
