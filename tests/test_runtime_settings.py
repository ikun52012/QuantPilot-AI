from core import runtime_settings
from core.config import AIConfig


def test_ai_config_reads_provider_api_keys_from_env(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "anth-key")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "deep-key")

    cfg = AIConfig.from_env()

    assert cfg.anthropic_api_key == "anth-key"
    assert cfg.deepseek_api_key == "deep-key"


def test_runtime_settings_accepts_mistral_provider(monkeypatch):
    monkeypatch.setattr(runtime_settings.settings.ai, "mistral_api_key", "mistral-key")

    assert runtime_settings._normalize_ai_provider("mistral") == "mistral"
    assert runtime_settings._current_ai_key("mistral") == "mistral-key"
