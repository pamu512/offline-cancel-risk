from pathlib import Path
from offline_cancel_risk.settings import load_policy, get_settings

def test_load_policy_has_v5_seeds():
    policy = load_policy(Path("config/policy.default.yaml"))
    assert policy["dbscan"]["min_pts"] == 7
    assert policy["dbscan"]["immediate_dp_radius"] == 150
    assert policy["dbscan"]["confidence_threshold"] == 0.75
    assert policy["gps"]["min_window_h"] == 3
    assert policy["gps"]["max_window_h"] == 24
    assert policy["learning"]["target_precision"] == 0.98
    assert policy["learning"]["pattern_strata"]["cancelled_offline"]["score_min"] == 0.85
    assert policy["baselines"]["mode"] == "shadow"
    assert policy["baselines"]["pair_window_n"] == 8

def test_settings_policy_path_exists():
    s = get_settings()
    assert Path(s.policy_path).exists()
