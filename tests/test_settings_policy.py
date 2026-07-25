from pathlib import Path
from offline_cancel_risk.settings import load_policy, get_settings

def test_load_policy_has_v5_seeds():
    policy = load_policy(Path("config/policy.default.yaml"))
    assert policy["dbscan"]["min_pts"] == 7
    assert policy["dbscan"]["immediate_dp_radius"] == 150
    assert policy["dbscan"]["confidence_threshold"] == 0.75
    assert policy["gps"]["min_window_h"] == 3
    assert policy["gps"]["max_window_h"] == 24

def test_settings_policy_path_exists():
    s = get_settings()
    assert Path(s.policy_path).exists()
