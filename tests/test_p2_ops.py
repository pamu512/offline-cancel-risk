"""P2: HTTP stream fan-out, downstream fill, dwell factor nudges."""

from __future__ import annotations

from offline_cancel_risk.adapters.http_stream import FanoutStreamPublisher, HttpStreamPublisher
from offline_cancel_risk.adapters.stream_factory import make_stream_publisher
from offline_cancel_risk.api.schemas import (
    AssessmentResult,
    ExpectedRevenueAtRisk,
    ThreeHeadFlags,
    ThreeHeadMlScores,
    ThreeHeadScores,
)
from offline_cancel_risk.ops.downstream_fill import downstream_intel_fill_report
from offline_cancel_risk.ops.dwell_factor_nudge import dwell_factor_nudge_report
from offline_cancel_risk.settings import Settings, load_policy


def _result(
    oid: str,
    *,
    offline_flag: int,
    place: str = "unknown",
    device: bool = False,
    chat: bool = False,
) -> AssessmentResult:
    return AssessmentResult(
        order_display_id=oid,
        driver_id=1,
        scores=ThreeHeadScores(
            cancelled_offline=0.9 if offline_flag else 0.1,
            cancel_abuse=0.0,
            selective_theft=0.0,
        ),
        flags=ThreeHeadFlags(
            cancelled_offline=offline_flag,
            cancel_abuse=0,
            selective_theft=0,
        ),
        expected_revenue_at_risk=ExpectedRevenueAtRisk(
            cancelled_offline=0.0,
            cancel_abuse=0.0,
            selective_theft=0.0,
            total=0.0,
        ),
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
            "downstream_device_risk": device,
            "downstream_chat_signals": chat,
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


def test_make_stream_publisher_jsonl_only(tmp_path):
    s = Settings(stream_path=str(tmp_path / "e.jsonl"), stream_url="")
    pub = make_stream_publisher(s)
    assert type(pub).__name__ == "JsonlStreamPublisher"


def test_make_stream_publisher_fanout(tmp_path):
    s = Settings(
        stream_path=str(tmp_path / "e.jsonl"),
        stream_url="http://example.test/hook",
    )
    pub = make_stream_publisher(s)
    assert isinstance(pub, FanoutStreamPublisher)


def test_http_stream_posts(monkeypatch):
    calls: list[dict] = []

    def _post(url, content=None, headers=None, timeout=None):
        calls.append({"url": url, "headers": headers, "content": content})

        class Resp:
            status_code = 204

        return Resp()

    monkeypatch.setattr("offline_cancel_risk.adapters.http_stream.httpx.post", _post)
    pub = HttpStreamPublisher("http://bus.test/risk", api_key="k")
    pub.publish(_result("ORD-1", offline_flag=1))
    assert calls and calls[0]["url"] == "http://bus.test/risk"
    assert calls[0]["headers"]["X-API-Key"] == "k"


def test_fanout_continues_after_child_error():
    class Boom:
        def publish(self, result):
            raise RuntimeError("boom")

    class Ok:
        def __init__(self):
            self.n = 0

        def publish(self, result):
            self.n += 1

    ok = Ok()
    FanoutStreamPublisher([Boom(), ok]).publish(_result("ORD-2", offline_flag=0))
    assert ok.n == 1


def test_downstream_fill_rates():
    rep = downstream_intel_fill_report(
        [
            _result("a", offline_flag=0, device=True, chat=True),
            _result("b", offline_flag=0, device=True, chat=False),
            _result("c", offline_flag=0),
        ]
    )
    assert rep["device_fill_rate"] == 2 / 3
    assert rep["chat_fill_rate"] == 1 / 3


def test_dwell_factor_nudge_suggests_raise_on_fps():
    policy = load_policy("config/policy.default.yaml")
    assessments = [
        _result(f"fp-{i}", offline_flag=1, place="apartment") for i in range(12)
    ]
    feedback = [
        {"order_display_id": f"fp-{i}", "labels": {"cancelled_offline": 0}}
        for i in range(12)
    ]
    rep = dwell_factor_nudge_report(
        assessments=assessments,
        feedback=feedback,
        policy=policy,
        min_support=10,
        min_fill_rate=0.2,
    )
    assert rep["ready"] is True
    assert any(
        s["place_class"] == "apartment" and s["action"] == "raise_factor"
        for s in rep["suggestions"]
    )


def test_dwell_factor_nudge_waits_on_low_fill():
    policy = load_policy("config/policy.default.yaml")
    assessments = [_result(f"u-{i}", offline_flag=1, place="unknown") for i in range(12)]
    feedback = [
        {"order_display_id": f"u-{i}", "labels": {"cancelled_offline": 0}}
        for i in range(12)
    ]
    rep = dwell_factor_nudge_report(
        assessments=assessments,
        feedback=feedback,
        policy=policy,
        min_support=5,
        min_fill_rate=0.2,
    )
    assert rep["ready"] is False
    assert rep["reason"] == "place_fill_rate_below_min"
