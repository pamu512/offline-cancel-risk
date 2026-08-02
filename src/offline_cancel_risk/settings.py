from functools import lru_cache
from pathlib import Path
import yaml
from pydantic_settings import BaseSettings, SettingsConfigDict

# settings.py lives at src/offline_cancel_risk/settings.py → repo root is parents[2]
ROOT = Path(__file__).resolve().parents[2]

class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="OCR_")
    policy_path: str = str(ROOT / "config" / "policy.default.yaml")
    policy_guardrails_path: str = str(
        ROOT / "config" / "policy_guardrails.default.yaml"
    )
    policy_overlays_path: str = str(ROOT / "data" / "policy_overlays.db")
    promote_gates_path: str = str(ROOT / "config" / "promote_gates.default.yaml")
    sqlite_path: str = str(ROOT / "data" / "assessments.db")
    stream_path: str = str(ROOT / "data" / "risk_events.jsonl")
    models_sqlite_path: str = str(ROOT / "data" / "models.db")
    models_root: str = str(ROOT / "data" / "models")
    shadow_metrics_path: str = str(ROOT / "data" / "shadow_metrics.db")
    canary_sqlite_path: str = str(ROOT / "data" / "canary.db")
    control_plane_sqlite_path: str = str(ROOT / "data" / "control_plane.db")
    label_tickets_path: str = str(ROOT / "data" / "label_tickets.db")
    label_tickets_stream_path: str = str(ROOT / "data" / "label_tickets.jsonl")
    driver_chains_path: str = str(ROOT / "data" / "driver_chains.db")
    operating_point_path: str = str(
        ROOT / "config" / "operating_point.default.yaml"
    )
    tuner_min_labeled: int = 30
    tuner_cooldown_minutes: int = 60
    tuner_min_f1_lift: float = 0.01
    metrics_debounce_seconds: float = 30.0
    # 0 disables periodic control-plane tick (metrics/tune/sample)
    control_plane_tick_seconds: float = 0.0
    gps_base_url: str = ""  # empty by default; tenants set OCR_GPS_BASE_URL
    gps_api_key: str = ""
    sync_assess: bool = False
    auth_required: bool = False
    api_keys: str = ""  # comma-separated when auth_required

@lru_cache
def get_settings() -> Settings:
    return Settings()

def load_policy(path: Path | str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    if not isinstance(data, dict):
        raise ValueError("policy must be a mapping")
    return data
