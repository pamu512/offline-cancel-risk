from offline_cancel_risk.control_plane.metrics import compute_label_metrics


def test_compute_f1_offline_head():
    assessments = [
        {
            "order_display_id": "A",
            "region_code": "PH",
            "city_code": "MNL",
            "scores": {
                "cancelled_offline": 0.9,
                "cancel_abuse": 0.1,
                "selective_theft": 0.1,
            },
        },
        {
            "order_display_id": "B",
            "region_code": "PH",
            "city_code": "MNL",
            "scores": {
                "cancelled_offline": 0.2,
                "cancel_abuse": 0.1,
                "selective_theft": 0.1,
            },
        },
    ]
    feedback = [
        {
            "order_display_id": "A",
            "labels": {
                "cancelled_offline": 1,
                "cancel_abuse": 0,
                "selective_theft": 0,
            },
        },
        {
            "order_display_id": "B",
            "labels": {
                "cancelled_offline": 0,
                "cancel_abuse": 0,
                "selective_theft": 0,
            },
        },
    ]
    rows = compute_label_metrics(
        assessments,
        feedback,
        thresholds={
            "cancelled_offline": 0.75,
            "cancel_abuse": 0.75,
            "selective_theft": 0.75,
        },
        region_code="PH",
        city_code="MNL",
    )
    offline = next(r for r in rows if r["head"] == "cancelled_offline")
    assert offline["tp"] == 1 and offline["tn"] == 1
    assert offline["f1"] == 1.0
    assert offline["support"] == 2
