from unittest.mock import AsyncMock

import pytest

from core import runtime_settings
from core.config import AIConfig
from pre_filter import get_thresholds


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


def test_runtime_status_reports_provider_specific_ai_keys(monkeypatch):
    monkeypatch.setattr(runtime_settings.settings.ai, "openai_api_key", "")
    monkeypatch.setattr(runtime_settings.settings.ai, "anthropic_api_key", "")
    monkeypatch.setattr(runtime_settings.settings.ai, "deepseek_api_key", "deep-key")
    monkeypatch.setattr(runtime_settings.settings.ai, "mistral_api_key", "")
    monkeypatch.setattr(runtime_settings.settings.ai, "openrouter_api_key", "")
    monkeypatch.setattr(runtime_settings.settings.ai, "custom_provider_api_key", "")

    status = runtime_settings.runtime_status()

    assert status["ai_api_configured"] is True
    assert status["openai_api_configured"] is False
    assert status["deepseek_api_configured"] is True


def test_apply_runtime_settings_allows_empty_voting_collections(monkeypatch):
    monkeypatch.setattr(runtime_settings.settings.ai, "voting_models", ["openai/gpt-5.5"])
    monkeypatch.setattr(runtime_settings.settings.ai, "voting_weights", {"openai/gpt-5.5": 1.0})
    monkeypatch.setattr(runtime_settings.settings.ai, "voting_strategy", "weighted")

    runtime_settings.apply_runtime_settings(
        {
            "ai": {
                "voting_enabled": False,
                "voting_models": [],
                "voting_weights": {},
                "voting_strategy": "consensus",
            }
        }
    )

    assert runtime_settings.settings.ai.voting_enabled is False
    assert runtime_settings.settings.ai.voting_models == []
    assert runtime_settings.settings.ai.voting_weights == {}
    assert runtime_settings.settings.ai.voting_strategy == "consensus"


@pytest.mark.asyncio
async def test_save_ai_settings_allows_clearing_strings_and_voting(monkeypatch):
    class _FakeSession:
        pass

    monkeypatch.setattr(runtime_settings.settings.ai, "custom_provider_name", "custom")
    monkeypatch.setattr(runtime_settings.settings.ai, "custom_provider_model", "old-model")
    monkeypatch.setattr(runtime_settings.settings.ai, "custom_provider_api_url", "https://old.example")
    monkeypatch.setattr(runtime_settings.settings.ai, "openrouter_model", "openai/gpt-5.5")
    monkeypatch.setattr(runtime_settings.settings.ai, "openrouter_site_url", "https://old.site")
    monkeypatch.setattr(runtime_settings.settings.ai, "openrouter_app_name", "Old App")
    monkeypatch.setattr(runtime_settings.settings.ai, "mistral_model", "mistral-large-latest")
    monkeypatch.setattr(runtime_settings.settings.ai, "openai_model", "gpt-5.5")
    monkeypatch.setattr(runtime_settings.settings.ai, "anthropic_model", "claude-opus-4-7")
    monkeypatch.setattr(runtime_settings.settings.ai, "deepseek_model", "deepseek-v4-pro")
    monkeypatch.setattr(runtime_settings.settings.ai, "voting_models", ["openai/gpt-5.5"])
    monkeypatch.setattr(runtime_settings.settings.ai, "voting_weights", {"openai/gpt-5.5": 1.0})

    monkeypatch.setattr(runtime_settings, "_load_encrypted_dict", AsyncMock(return_value={}))
    monkeypatch.setattr(runtime_settings, "_save_encrypted_dict", AsyncMock())

    updated = await runtime_settings.save_ai_settings(
        _FakeSession(),
        {
            "provider": "openrouter",
            "api_key": "",
            "custom_provider_name": "",
            "custom_provider_model": "",
            "custom_provider_api_url": "",
            "openrouter_model": "",
            "openrouter_site_url": "",
            "openrouter_app_name": "",
            "mistral_model": "",
            "openai_model": "",
            "anthropic_model": "",
            "deepseek_model": "",
            "voting_models": [],
            "voting_weights": {},
            "voting_strategy": "consensus",
        },
    )

    assert updated["voting_models"] == []
    assert updated["voting_weights"] == {}
    assert updated["custom_provider_model"] == ""
    assert updated["custom_provider_api_url"] == ""
    assert updated["openrouter_model"] == ""
    assert updated["openrouter_site_url"] == ""
    assert updated["openrouter_app_name"] == ""
    assert updated["mistral_model"] == ""
    assert updated["openai_model"] == ""
    assert updated["anthropic_model"] == ""
    assert updated["deepseek_model"] == ""


@pytest.mark.asyncio
async def test_save_exchange_settings_allows_clearing_credentials_and_empty_timeout_overrides(monkeypatch):
    class _FakeSession:
        pass

    monkeypatch.setattr(runtime_settings.settings.exchange, "api_key", "GLOBAL_KEY")
    monkeypatch.setattr(runtime_settings.settings.exchange, "api_secret", "GLOBAL_SECRET")
    monkeypatch.setattr(runtime_settings.settings.exchange, "password", "GLOBAL_PASSWORD")
    monkeypatch.setattr(runtime_settings.settings.exchange, "limit_timeout_overrides", {"1h": 3600})

    monkeypatch.setattr(
        runtime_settings,
        "_load_encrypted_dict",
        AsyncMock(
            return_value={
                "name": "okx",
                "api_key": "OLD_KEY",
                "api_secret": "OLD_SECRET",
                "password": "OLD_PASSWORD",
                "limit_timeout_overrides": {"1h": 7200},
            }
        ),
    )
    monkeypatch.setattr(runtime_settings, "_save_encrypted_dict", AsyncMock())

    updated = await runtime_settings.save_exchange_settings(
        _FakeSession(),
        {
            "api_key": "",
            "api_secret": "",
            "password": "",
            "limit_timeout_overrides": {},
        },
    )

    assert updated["api_key"] == ""
    assert updated["api_secret"] == ""
    assert updated["password"] == ""
    assert updated["limit_timeout_overrides"] == {}


@pytest.mark.asyncio
async def test_save_telegram_settings_allows_clearing_bot_token(monkeypatch):
    class _FakeSession:
        pass

    monkeypatch.setattr(runtime_settings.settings.telegram, "bot_token", "GLOBAL_TOKEN")
    monkeypatch.setattr(runtime_settings.settings.telegram, "chat_id", "GLOBAL_CHAT")

    monkeypatch.setattr(
        runtime_settings,
        "_load_encrypted_dict",
        AsyncMock(return_value={"bot_token": "OLD_TOKEN", "chat_id": "OLD_CHAT"}),
    )
    monkeypatch.setattr(runtime_settings, "_save_encrypted_dict", AsyncMock())

    updated = await runtime_settings.save_telegram_settings(
        _FakeSession(),
        {"bot_token": "", "chat_id": ""},
    )

    assert updated["bot_token"] == ""
    assert updated["chat_id"] == ""


def test_apply_runtime_settings_respects_empty_limit_timeout_overrides(monkeypatch):
    monkeypatch.setattr(runtime_settings.settings.exchange, "limit_timeout_overrides", {"1h": 3600})

    runtime_settings.apply_runtime_settings({"exchange": {"limit_timeout_overrides": {}}})

    assert runtime_settings.settings.exchange.limit_timeout_overrides == {}


@pytest.mark.asyncio
async def test_apply_persisted_admin_settings_reloads_prefilter_thresholds(monkeypatch):
    class _FakeSession:
        async def execute(self, statement):
            key = statement.compile().params.get("key_1")
            value = threshold_store.get(key)

            class _Result:
                def scalar_one_or_none(self_nonlocal):
                    if value is None:
                        return None
                    return type("_Setting", (), {"value": value})()

            return _Result()

    threshold_store = {
        "prefilter_thresholds": '{"min_pass_score": 65.0, "cooldown_seconds": 120}',
        "enhanced_filters": "",
        "ai_provider": "",
        "mistral_api_key": "",
        "mistral_model": "",
        "openai_api_key": "",
        "openai_model": "",
        "anthropic_api_key": "",
        "anthropic_model": "",
        "deepseek_api_key": "",
        "deepseek_model": "",
        "openrouter_api_key": "",
        "openrouter_model": "",
        "ai_voting_enabled": "",
        "ai_voting_models": "",
        "ai_voting_weights": "",
        "ai_voting_strategy": "",
        "external_api_keys": "",
    }

    thresholds = get_thresholds()
    thresholds.reload_from_dict({"min_pass_score": 10.0, "cooldown_seconds": 300})

    await runtime_settings.apply_persisted_admin_settings(_FakeSession())

    assert thresholds.get("min_pass_score") == 65.0
    assert thresholds.get("cooldown_seconds") == 120
