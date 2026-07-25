from scripts.compute_label_metrics import main as metrics_main
from scripts.run_tuner import main as tuner_main


def test_compute_label_metrics_main_empty(tmp_path, monkeypatch):
    monkeypatch.setenv("OCR_SQLITE_PATH", str(tmp_path / "a.db"))
    monkeypatch.setenv("OCR_CONTROL_PLANE_SQLITE_PATH", str(tmp_path / "cp.db"))
    monkeypatch.setenv("OCR_POLICY_OVERLAYS_PATH", str(tmp_path / "o.db"))
    from offline_cancel_risk.settings import get_settings

    get_settings.cache_clear()
    assert metrics_main([]) == 0
    get_settings.cache_clear()


def test_run_tuner_main_empty(tmp_path, monkeypatch):
    monkeypatch.setenv("OCR_SQLITE_PATH", str(tmp_path / "a.db"))
    monkeypatch.setenv("OCR_CONTROL_PLANE_SQLITE_PATH", str(tmp_path / "cp.db"))
    monkeypatch.setenv("OCR_POLICY_OVERLAYS_PATH", str(tmp_path / "o.db"))
    from offline_cancel_risk.settings import get_settings

    get_settings.cache_clear()
    assert tuner_main(["--region-code", "PH", "--city-code", "MNL"]) == 0
    get_settings.cache_clear()
