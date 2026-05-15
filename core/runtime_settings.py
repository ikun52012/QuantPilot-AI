"""
Runtime admin settings.

Admin-facing settings are persisted in the database and applied to the
in-process configuration object so changes survive restart and take effect
without rebuilding the container.
"""
from __future__ import annotations

import json
import os
from typing import Any

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from core.config import settings
from core.database import get_admin_setting, set_admin_setting
from core.security import decrypt_settings_payload, encrypt_settings_payload, mask_secret
from core.utils.common import first_valid, normalize_limit_timeout_overrides

EXCHANGE_KEY = "runtime_exchange"
AI_KEY = "runtime_ai"
TELEGRAM_KEY = "runtime_telegram"
RISK_KEY = "runtime_risk"
TAKE_PROFIT_KEY = "runtime_take_profit"
TRAILING_STOP_KEY = "runtime_trailing_stop"
ORDER_EXECUTION_KEY = "runtime_order_execution"


def _to_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _to_int(value: Any, default: int, min_value: int | None = None, max_value: int | None = None) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    if min_value is not None:
        parsed = max(min_value, parsed)
    if max_value is not None:
        parsed = min(max_value, parsed)
    return parsed


def _to_float(value: Any, default: float, min_value: float | None = None, max_value: float | None = None) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        parsed = default
    if min_value is not None:
        parsed = max(min_value, parsed)
    if max_value is not None:
        parsed = min(max_value, parsed)
    return parsed


def _public_secret_configured(value: str) -> bool:
    return bool(str(value or "").strip())


def _current_ai_key(provider: str) -> str:
    provider = (provider or settings.ai.provider).lower().strip()
    if provider == "openai":
        return str(settings.ai.openai_api_key or "")
    if provider == "anthropic":
        return str(settings.ai.anthropic_api_key or "")
    if provider == "deepseek":
        return str(settings.ai.deepseek_api_key or "")
    if provider == "mistral":
        return str(settings.ai.mistral_api_key or "")
    if provider == "openrouter":
        return str(settings.ai.openrouter_api_key or "")
    return str(settings.ai.custom_provider_api_key or "")


def _normalize_ai_provider(provider: Any, default: str | None = None) -> str:
    value = str(provider or default or settings.ai.provider).lower().strip()
    allowed = {"openai", "anthropic", "deepseek", "mistral", "openrouter", "custom"}
    return value if value in allowed else settings.ai.provider


def _coalesce_str(*values: Any, default: str = "") -> str:
    value = first_valid(*values)
    if value is None:
        return default
    return str(value)


async def _load_encrypted_dict(session: AsyncSession, key: str) -> dict[str, Any]:
    raw = await get_admin_setting(session, key, "")
    if not raw:
        return {}
    try:
        payload = json.loads(raw)
        if isinstance(payload, dict):
            decrypted = decrypt_settings_payload(payload)
            return decrypted if isinstance(decrypted, dict) else {}
    except json.JSONDecodeError:
        return {}
    except Exception:
        return {}
    return {}


async def _save_encrypted_dict(session: AsyncSession, key: str, data: dict[str, Any]) -> None:
    await set_admin_setting(session, key, json.dumps(encrypt_settings_payload(data)))


async def load_admin_runtime_settings(session: AsyncSession) -> dict[str, dict[str, Any]]:
    """Load persisted runtime settings from the database."""
    return {
        "exchange": await _load_encrypted_dict(session, EXCHANGE_KEY),
        "ai": await _load_encrypted_dict(session, AI_KEY),
        "telegram": await _load_encrypted_dict(session, TELEGRAM_KEY),
        "risk": await _load_encrypted_dict(session, RISK_KEY),
        "take_profit": await _load_encrypted_dict(session, TAKE_PROFIT_KEY),
        "trailing_stop": await _load_encrypted_dict(session, TRAILING_STOP_KEY),
        "order_execution": await _load_encrypted_dict(session, ORDER_EXECUTION_KEY),
    }


def apply_runtime_settings(runtime: dict[str, dict[str, Any]]) -> None:
    """Apply loaded runtime settings to the process-wide settings object."""
    exchange = runtime.get("exchange") or {}
    if exchange:
        settings.exchange.name = str(exchange.get("name") or exchange.get("exchange") or settings.exchange.name).lower().strip()
        settings.exchange.api_key = str(exchange.get("api_key") or "")
        settings.exchange.api_secret = str(exchange.get("api_secret") or "")
        settings.exchange.password = str(exchange.get("password") or "")
        settings.exchange.live_trading = _to_bool(exchange.get("live_trading"), settings.exchange.live_trading)
        settings.exchange.sandbox_mode = _to_bool(exchange.get("sandbox_mode"), settings.exchange.sandbox_mode)
        settings.exchange.market_type = str(exchange.get("market_type") or settings.exchange.market_type).lower().strip()
        settings.exchange.default_order_type = str(exchange.get("default_order_type") or settings.exchange.default_order_type).lower().strip()
        settings.exchange.stop_loss_order_type = str(exchange.get("stop_loss_order_type") or settings.exchange.stop_loss_order_type).lower().strip()
        settings.exchange.limit_timeout_overrides = normalize_limit_timeout_overrides(
            exchange.get("limit_timeout_overrides") if "limit_timeout_overrides" in exchange else settings.exchange.limit_timeout_overrides
        )

    ai = runtime.get("ai") or {}
    if ai:
        settings.ai.provider = _normalize_ai_provider(ai.get("provider"))
        api_key = str(ai.get("api_key") or "").strip()
        if api_key:
            if settings.ai.provider == "openai":
                settings.ai.openai_api_key = api_key
            elif settings.ai.provider == "anthropic":
                settings.ai.anthropic_api_key = api_key
            elif settings.ai.provider == "deepseek":
                settings.ai.deepseek_api_key = api_key
            elif settings.ai.provider == "mistral":
                settings.ai.mistral_api_key = api_key
            elif settings.ai.provider == "openrouter":
                settings.ai.openrouter_api_key = api_key
            else:
                settings.ai.custom_provider_api_key = api_key

        # Also explicitly set custom_provider_api_key if provided separately
        custom_api_key = str(ai.get("custom_provider_api_key") or "")
        if custom_api_key:
            settings.ai.custom_provider_api_key = custom_api_key

        settings.ai.temperature = _to_float(ai.get("temperature"), settings.ai.temperature, 0, 2)
        settings.ai.max_tokens = _to_int(ai.get("max_tokens"), settings.ai.max_tokens, 100, 4000)
        settings.ai.custom_system_prompt = str(ai.get("custom_system_prompt") or "")
        settings.ai.custom_provider_enabled = _to_bool(ai.get("custom_provider_enabled"), settings.ai.custom_provider_enabled)
        settings.ai.custom_provider_name = _coalesce_str(ai.get("custom_provider_name"), settings.ai.custom_provider_name, default="custom")
        settings.ai.custom_provider_model = _coalesce_str(ai.get("custom_provider_model"), default="")
        settings.ai.custom_provider_api_url = _coalesce_str(ai.get("custom_provider_api_url"), default="")
        settings.ai.openrouter_enabled = _to_bool(ai.get("openrouter_enabled"), settings.ai.openrouter_enabled)
        settings.ai.openrouter_model = _coalesce_str(ai.get("openrouter_model"), settings.ai.openrouter_model)
        settings.ai.openrouter_site_url = _coalesce_str(ai.get("openrouter_site_url"), settings.ai.openrouter_site_url)
        settings.ai.openrouter_app_name = _coalesce_str(ai.get("openrouter_app_name"), settings.ai.openrouter_app_name)
        settings.ai.mistral_api_key = _coalesce_str(ai.get("mistral_api_key"), settings.ai.mistral_api_key)
        settings.ai.mistral_model = _coalesce_str(ai.get("mistral_model"), settings.ai.mistral_model)
        settings.ai.openai_model = _coalesce_str(ai.get("openai_model"), settings.ai.openai_model)
        settings.ai.anthropic_model = _coalesce_str(ai.get("anthropic_model"), settings.ai.anthropic_model)
        settings.ai.deepseek_model = _coalesce_str(ai.get("deepseek_model"), settings.ai.deepseek_model)
        if "voting_enabled" in ai:
            settings.ai.voting_enabled = _to_bool(ai.get("voting_enabled"), settings.ai.voting_enabled)
        if "voting_models" in ai:
            models = ai.get("voting_models")
            if isinstance(models, list):
                settings.ai.voting_models = models
            elif isinstance(models, str):
                try:
                    settings.ai.voting_models = json.loads(models)
                except Exception as e:
                    logger.debug(f"[RuntimeSettings] Failed to parse voting_models: {e}")
        if "voting_weights" in ai:
            weights = ai.get("voting_weights")
            if isinstance(weights, dict):
                settings.ai.voting_weights = weights
            elif isinstance(weights, str):
                try:
                    settings.ai.voting_weights = json.loads(weights)
                except Exception as e:
                    logger.debug(f"[RuntimeSettings] Failed to parse voting_weights: {e}")
        if "voting_strategy" in ai:
            strategy = str(ai.get("voting_strategy") or "weighted").lower().strip()
            if strategy in {"weighted", "consensus", "best_confidence"}:
                settings.ai.voting_strategy = strategy

    telegram = runtime.get("telegram") or {}
    if telegram:
        settings.telegram.bot_token = str(telegram.get("bot_token") or "")
        settings.telegram.chat_id = str(telegram.get("chat_id") or "")

    risk = runtime.get("risk") or {}
    if risk:
        settings.risk.max_position_pct = _to_float(risk.get("max_position_pct"), settings.risk.max_position_pct, 0.1, 100)
        settings.risk.max_daily_trades = _to_int(risk.get("max_daily_trades"), settings.risk.max_daily_trades, 1, 10000)
        settings.risk.max_daily_loss_pct = _to_float(risk.get("max_daily_loss_pct"), settings.risk.max_daily_loss_pct, 0.1, 100)
        mode = str(risk.get("exit_management_mode") or settings.risk.exit_management_mode)
        settings.risk.exit_management_mode = mode if mode in {"ai", "custom"} else "ai"
        profile = str(risk.get("ai_risk_profile") or settings.risk.ai_risk_profile)
        settings.risk.ai_risk_profile = profile if profile in {"conservative", "balanced", "aggressive"} else "balanced"
        settings.risk.custom_stop_loss_pct = _to_float(risk.get("custom_stop_loss_pct"), settings.risk.custom_stop_loss_pct, 0.1, 100)
        settings.risk.ai_exit_system_prompt = str(risk.get("ai_exit_system_prompt") or "")
        # Position sizing settings
        sizing_mode = str(risk.get("position_sizing_mode") or settings.risk.position_sizing_mode)
        settings.risk.position_sizing_mode = sizing_mode if sizing_mode in {"percentage", "fixed", "risk_ratio"} else "percentage"
        settings.risk.fixed_position_size_usdt = _to_float(risk.get("fixed_position_size_usdt"), settings.risk.fixed_position_size_usdt, 1, 1000000)
        settings.risk.risk_per_trade_pct = _to_float(risk.get("risk_per_trade_pct"), settings.risk.risk_per_trade_pct, 0.1, 100)
        settings.risk.account_equity_usdt = _to_float(risk.get("account_equity_usdt"), settings.risk.account_equity_usdt, 100, 10000000)
        margin_mode = str(risk.get("margin_mode") or settings.risk.margin_mode).lower().strip()
        settings.risk.margin_mode = margin_mode if margin_mode in {"cross", "isolated"} else "cross"

    take_profit = runtime.get("take_profit") or {}
    if take_profit:
        settings.take_profit.num_levels = _to_int(take_profit.get("num_levels"), settings.take_profit.num_levels, 1, 4)
        for attr in ("tp1_pct", "tp2_pct", "tp3_pct", "tp4_pct", "tp1_qty", "tp2_qty", "tp3_qty", "tp4_qty"):
            setattr(settings.take_profit, attr, _to_float(take_profit.get(attr), getattr(settings.take_profit, attr), 0, 200))

    trailing_stop = runtime.get("trailing_stop") or {}
    if trailing_stop:
        mode = str(trailing_stop.get("mode") or settings.trailing_stop.mode)
        allowed = {"none", "auto", "moving", "breakeven_on_tp1", "step_trailing", "profit_pct_trailing"}
        settings.trailing_stop.mode = mode if mode in allowed else "none"
        settings.trailing_stop.trail_pct = _to_float(trailing_stop.get("trail_pct"), settings.trailing_stop.trail_pct, 0.1, 100)
        settings.trailing_stop.activation_profit_pct = _to_float(
            trailing_stop.get("activation_profit_pct"), settings.trailing_stop.activation_profit_pct, 0, 100
        )
        settings.trailing_stop.trailing_step_pct = _to_float(
            trailing_stop.get("trailing_step_pct"), settings.trailing_stop.trailing_step_pct, 0, 100
        )
        # Apply buffer settings for breakeven and step trailing
        if "breakeven_buffer_pct" in trailing_stop:
            settings.trailing_stop.breakeven_buffer_pct = _to_float(
                trailing_stop.get("breakeven_buffer_pct"), 0.2, 0, 1.0
            )
        if "step_buffer_pct" in trailing_stop:
            settings.trailing_stop.step_buffer_pct = _to_float(
                trailing_stop.get("step_buffer_pct"), 0.3, 0, 2.0
            )


async def apply_persisted_admin_settings(session: AsyncSession) -> dict[str, dict[str, Any]]:
    runtime = await load_admin_runtime_settings(session)
    apply_runtime_settings(runtime)

    try:
        from core.database import get_admin_setting

        # Load AI provider from admin_settings (saved separately in ai_config.py)
        ai_provider_raw = await get_admin_setting(session, "ai_provider", "")
        if ai_provider_raw:
            provider = _normalize_ai_provider(ai_provider_raw)
            if provider:
                settings.ai.provider = provider
                logger.debug(f"[RuntimeSettings] Loaded AI provider from admin_settings: {provider}")

        # Also load individual AI keys that may be saved separately
        mistral_api_key = await get_admin_setting(session, "mistral_api_key", "")
        if mistral_api_key:
            settings.ai.mistral_api_key = mistral_api_key

        mistral_model = await get_admin_setting(session, "mistral_model", "")
        if mistral_model:
            settings.ai.mistral_model = mistral_model

        openai_api_key = await get_admin_setting(session, "openai_api_key", "")
        if openai_api_key:
            settings.ai.openai_api_key = openai_api_key

        openai_model = await get_admin_setting(session, "openai_model", "")
        if openai_model:
            settings.ai.openai_model = openai_model

        anthropic_api_key = await get_admin_setting(session, "anthropic_api_key", "")
        if anthropic_api_key:
            settings.ai.anthropic_api_key = anthropic_api_key

        anthropic_model = await get_admin_setting(session, "anthropic_model", "")
        if anthropic_model:
            settings.ai.anthropic_model = anthropic_model

        deepseek_api_key = await get_admin_setting(session, "deepseek_api_key", "")
        if deepseek_api_key:
            settings.ai.deepseek_api_key = deepseek_api_key

        deepseek_model = await get_admin_setting(session, "deepseek_model", "")
        if deepseek_model:
            settings.ai.deepseek_model = deepseek_model

        openrouter_api_key = await get_admin_setting(session, "openrouter_api_key", "")
        if openrouter_api_key:
            settings.ai.openrouter_api_key = openrouter_api_key

        openrouter_model = await get_admin_setting(session, "openrouter_model", "")
        if openrouter_model:
            settings.ai.openrouter_model = openrouter_model

        voting_enabled_raw = await get_admin_setting(session, "ai_voting_enabled", "")
        if voting_enabled_raw:
            settings.ai.voting_enabled = _to_bool(json.loads(voting_enabled_raw) if voting_enabled_raw.startswith("{") or voting_enabled_raw.startswith("[") else voting_enabled_raw)

        voting_models_raw = await get_admin_setting(session, "ai_voting_models", "")
        if voting_models_raw:
            try:
                models = json.loads(voting_models_raw)
                if isinstance(models, list):
                    settings.ai.voting_models = models
            except Exception as e:
                logger.debug(f"[RuntimeSettings] Failed to parse voting_models_raw: {e}")

        voting_weights_raw = await get_admin_setting(session, "ai_voting_weights", "")
        if voting_weights_raw:
            try:
                weights = json.loads(voting_weights_raw)
                if isinstance(weights, dict):
                    settings.ai.voting_weights = weights
            except Exception as e:
                logger.debug(f"[RuntimeSettings] Failed to parse voting_weights_raw: {e}")

        voting_strategy_raw = await get_admin_setting(session, "ai_voting_strategy", "")
        if voting_strategy_raw:
            strategy = str(voting_strategy_raw).lower().strip()
            if strategy in {"weighted", "consensus", "best_confidence"}:
                settings.ai.voting_strategy = strategy

        # Reload AI settings saved via /api/admin/ai/provider-config
        # These are saved directly to admin_settings but need reload on restart
        openrouter_enabled_raw = await get_admin_setting(session, "openrouter_enabled", "")
        if openrouter_enabled_raw:
            try:
                enabled = json.loads(openrouter_enabled_raw)
                if isinstance(enabled, bool):
                    settings.ai.openrouter_enabled = enabled
            except json.JSONDecodeError:
                logger.debug("[RuntimeSettings] Invalid openrouter_enabled JSON")
            except Exception as e:
                logger.debug(f"[RuntimeSettings] Failed to load openrouter_enabled: {e}")

        custom_provider_enabled_raw = await get_admin_setting(session, "custom_ai_provider_enabled", "")
        if custom_provider_enabled_raw:
            try:
                enabled = json.loads(custom_provider_enabled_raw)
                if isinstance(enabled, bool):
                    settings.ai.custom_provider_enabled = enabled
            except json.JSONDecodeError:
                logger.debug("[RuntimeSettings] Invalid custom_ai_provider_enabled JSON")
            except Exception as e:
                logger.debug(f"[RuntimeSettings] Failed to load custom_ai_provider_enabled: {e}")

        custom_provider_name_raw = await get_admin_setting(session, "custom_ai_provider_name", "")
        if custom_provider_name_raw:
            settings.ai.custom_provider_name = str(custom_provider_name_raw).strip()

        custom_provider_model_raw = await get_admin_setting(session, "custom_ai_model", "")
        if custom_provider_model_raw:
            settings.ai.custom_provider_model = str(custom_provider_model_raw).strip()

        custom_provider_api_url_raw = await get_admin_setting(session, "custom_ai_api_url", "")
        if custom_provider_api_url_raw:
            settings.ai.custom_provider_api_url = str(custom_provider_api_url_raw).strip()

        custom_provider_api_key_raw = await get_admin_setting(session, "custom_ai_api_key", "")
        if custom_provider_api_key_raw:
            settings.ai.custom_provider_api_key = str(custom_provider_api_key_raw).strip()

        ai_temperature_raw = await get_admin_setting(session, "ai_temperature", "")
        if ai_temperature_raw:
            try:
                temp = float(ai_temperature_raw)
                if 0 <= temp <= 2:
                    settings.ai.temperature = temp
            except (ValueError, TypeError):
                logger.debug("[RuntimeSettings] Invalid ai_temperature value")
            except Exception as e:
                logger.debug(f"[RuntimeSettings] Failed to load ai_temperature: {e}")

        ai_max_tokens_raw = await get_admin_setting(session, "ai_max_tokens", "")
        if ai_max_tokens_raw:
            try:
                tokens = int(ai_max_tokens_raw)
                if 100 <= tokens <= 4000:
                    settings.ai.max_tokens = tokens
            except (ValueError, TypeError):
                logger.debug("[RuntimeSettings] Invalid ai_max_tokens value")
            except Exception as e:
                logger.debug(f"[RuntimeSettings] Failed to load ai_max_tokens: {e}")

        external_keys_raw = await get_admin_setting(session, "external_api_keys", "")
        if external_keys_raw:
            try:
                from core.security import decrypt_settings_payload, set_secure_api_key
                keys_data = json.loads(external_keys_raw)
                decrypted = decrypt_settings_payload(keys_data)
                if isinstance(decrypted, dict):
                    for key_name, key_value in decrypted.items():
                        if key_value:
                            set_secure_api_key(key_name, key_value)
            except Exception as e:
                logger.debug(f"[RuntimeSettings] Failed to load external API keys: {e}")

        enhanced_filters_raw = await get_admin_setting(session, "enhanced_filters", "")
        if enhanced_filters_raw:
            try:
                ef_settings = json.loads(enhanced_filters_raw)
                if isinstance(ef_settings, dict):
                    os.environ["ENHANCED_FILTERS_ENABLED"] = str(ef_settings.get("enhanced_filters_enabled", True)).lower()
                    if ef_settings.get("whale_threshold_usd"):
                        os.environ["WHALE_THRESHOLD_USD"] = str(ef_settings.get("whale_threshold_usd"))
                    if ef_settings.get("correlated_threshold_pct"):
                        os.environ["CORRELATED_THRESHOLD_PCT"] = str(ef_settings.get("correlated_threshold_pct"))
                    if ef_settings.get("oi_change_threshold_pct"):
                        os.environ["OI_CHANGE_THRESHOLD_PCT"] = str(ef_settings.get("oi_change_threshold_pct"))
            except Exception as e:
                logger.debug(f"[RuntimeSettings] Failed to parse enhanced_filters_raw: {e}")

        prefilter_thresholds_raw = await get_admin_setting(session, "prefilter_thresholds", "")
        try:
            prefilter_thresholds = json.loads(prefilter_thresholds_raw) if prefilter_thresholds_raw else {}
            if not isinstance(prefilter_thresholds, dict):
                prefilter_thresholds = {}
        except Exception as e:
            logger.debug(f"[RuntimeSettings] Failed to parse prefilter_thresholds: {e}")
            prefilter_thresholds = {}

        from pre_filter import get_thresholds

        get_thresholds().reload_from_dict(prefilter_thresholds)
    except Exception as e:
        logger.debug(f"[RuntimeSettings] Failed to apply persisted admin settings: {e}")

    return runtime


async def save_exchange_settings(session: AsyncSession, data: dict[str, Any], apply_immediately: bool = True) -> dict[str, Any]:
    current = await _load_encrypted_dict(session, EXCHANGE_KEY)
    updated = {
        "name": _coalesce_str(data.get("exchange"), data.get("name"), current.get("name"), settings.exchange.name).lower().strip(),
        "api_key": _coalesce_str(data.get("api_key"), current.get("api_key"), settings.exchange.api_key),
        "api_secret": _coalesce_str(data.get("api_secret"), current.get("api_secret"), settings.exchange.api_secret),
        "password": _coalesce_str(data.get("password"), current.get("password"), settings.exchange.password),
        "live_trading": _to_bool(data.get("live_trading"), _to_bool(current.get("live_trading"), settings.exchange.live_trading)),
        "sandbox_mode": _to_bool(data.get("sandbox_mode"), _to_bool(current.get("sandbox_mode"), settings.exchange.sandbox_mode)),
        "market_type": _coalesce_str(data.get("market_type"), current.get("market_type"), settings.exchange.market_type).lower().strip(),
        "default_order_type": _coalesce_str(data.get("default_order_type"), current.get("default_order_type"), settings.exchange.default_order_type).lower().strip(),
        "stop_loss_order_type": _coalesce_str(data.get("stop_loss_order_type"), current.get("stop_loss_order_type"), settings.exchange.stop_loss_order_type).lower().strip(),
        "limit_timeout_overrides": normalize_limit_timeout_overrides(
            data.get("limit_timeout_overrides")
            if "limit_timeout_overrides" in data
            else current.get("limit_timeout_overrides", settings.exchange.limit_timeout_overrides)
        ),
    }
    await _save_encrypted_dict(session, EXCHANGE_KEY, updated)
    if apply_immediately:
        apply_runtime_settings({"exchange": updated})
    return updated


async def save_ai_settings(session: AsyncSession, data: dict[str, Any]) -> dict[str, Any]:
    current = await _load_encrypted_dict(session, AI_KEY)
    provider = _normalize_ai_provider(first_valid(data.get("provider"), current.get("provider")))
    updated = {
        "provider": provider,
        "api_key": str(data.get("api_key")).strip() if "api_key" in data and str(data.get("api_key")).strip() else _coalesce_str(current.get("api_key"), _current_ai_key(provider)),
        "temperature": _to_float(data.get("temperature"), _to_float(current.get("temperature"), settings.ai.temperature), 0, 2),
        "max_tokens": _to_int(data.get("max_tokens"), _to_int(current.get("max_tokens"), settings.ai.max_tokens), 100, 4000),
        "custom_system_prompt": str(data.get("custom_system_prompt") if data.get("custom_system_prompt") is not None else current.get("custom_system_prompt", "")),
        "custom_provider_enabled": _to_bool(data.get("custom_provider_enabled"), _to_bool(current.get("custom_provider_enabled"), False)),
        "custom_provider_name": _coalesce_str(data.get("custom_provider_name"), current.get("custom_provider_name"), settings.ai.custom_provider_name, default="custom"),
        "custom_provider_model": _coalesce_str(data.get("custom_provider_model"), current.get("custom_provider_model"), default=""),
        "custom_provider_api_url": _coalesce_str(data.get("custom_provider_api_url"), current.get("custom_provider_api_url"), default=""),
        "custom_provider_api_key": str(data.get("custom_provider_api_key")).strip() if "custom_provider_api_key" in data and str(data.get("custom_provider_api_key")).strip() else _coalesce_str(current.get("custom_provider_api_key"), settings.ai.custom_provider_api_key),
        "openrouter_enabled": _to_bool(data.get("openrouter_enabled"), _to_bool(current.get("openrouter_enabled"), settings.ai.openrouter_enabled)),
        "openrouter_model": _coalesce_str(data.get("openrouter_model"), current.get("openrouter_model"), settings.ai.openrouter_model),
        "openrouter_site_url": _coalesce_str(data.get("openrouter_site_url"), current.get("openrouter_site_url"), settings.ai.openrouter_site_url),
        "openrouter_app_name": _coalesce_str(data.get("openrouter_app_name"), current.get("openrouter_app_name"), settings.ai.openrouter_app_name),
        "mistral_api_key": _coalesce_str(data.get("mistral_api_key"), current.get("mistral_api_key"), settings.ai.mistral_api_key),
        "mistral_model": _coalesce_str(data.get("mistral_model"), current.get("mistral_model"), settings.ai.mistral_model),
        "openai_model": _coalesce_str(data.get("openai_model"), current.get("openai_model"), settings.ai.openai_model),
        "anthropic_model": _coalesce_str(data.get("anthropic_model"), current.get("anthropic_model"), settings.ai.anthropic_model),
        "deepseek_model": _coalesce_str(data.get("deepseek_model"), current.get("deepseek_model"), settings.ai.deepseek_model),
        "voting_enabled": _to_bool(data.get("voting_enabled"), _to_bool(current.get("voting_enabled"), settings.ai.voting_enabled)),
        "voting_models": list(data.get("voting_models") if "voting_models" in data else current.get("voting_models", settings.ai.voting_models)),
        "voting_weights": dict(data.get("voting_weights") if "voting_weights" in data else current.get("voting_weights", settings.ai.voting_weights)),
        "voting_strategy": _coalesce_str(data.get("voting_strategy"), current.get("voting_strategy"), settings.ai.voting_strategy),
    }
    await _save_encrypted_dict(session, AI_KEY, updated)
    apply_runtime_settings({"ai": updated})
    return updated


async def save_telegram_settings(session: AsyncSession, data: dict[str, Any]) -> dict[str, Any]:
    current = await _load_encrypted_dict(session, TELEGRAM_KEY)
    updated = {
        "bot_token": _coalesce_str(data.get("bot_token"), current.get("bot_token"), settings.telegram.bot_token),
        "chat_id": _coalesce_str(data.get("chat_id"), current.get("chat_id"), settings.telegram.chat_id),
    }
    await _save_encrypted_dict(session, TELEGRAM_KEY, updated)
    apply_runtime_settings({"telegram": updated})
    return updated


async def save_risk_settings(session: AsyncSession, data: dict[str, Any]) -> dict[str, Any]:
    current = await _load_encrypted_dict(session, RISK_KEY)
    updated = {
        "max_position_pct": _to_float(data.get("max_position_pct"), _to_float(current.get("max_position_pct"), settings.risk.max_position_pct), 0.1, 100),
        "max_daily_trades": _to_int(data.get("max_daily_trades"), _to_int(current.get("max_daily_trades"), settings.risk.max_daily_trades), 1, 10000),
        "max_daily_loss_pct": _to_float(data.get("max_daily_loss_pct"), _to_float(current.get("max_daily_loss_pct"), settings.risk.max_daily_loss_pct), 0.1, 100),
        "exit_management_mode": str(data.get("exit_management_mode") or current.get("exit_management_mode") or settings.risk.exit_management_mode),
        "ai_risk_profile": str(data.get("ai_risk_profile") or current.get("ai_risk_profile") or settings.risk.ai_risk_profile),
        "custom_stop_loss_pct": _to_float(data.get("custom_stop_loss_pct"), _to_float(current.get("custom_stop_loss_pct"), settings.risk.custom_stop_loss_pct), 0.1, 100),
        "ai_exit_system_prompt": str(data.get("ai_exit_system_prompt") if data.get("ai_exit_system_prompt") is not None else current.get("ai_exit_system_prompt", "")),
        # Position sizing settings
        "position_sizing_mode": str(data.get("position_sizing_mode") or current.get("position_sizing_mode") or settings.risk.position_sizing_mode),
        "fixed_position_size_usdt": _to_float(data.get("fixed_position_size_usdt"), _to_float(current.get("fixed_position_size_usdt"), settings.risk.fixed_position_size_usdt), 1, 1000000),
        "risk_per_trade_pct": _to_float(data.get("risk_per_trade_pct"), _to_float(current.get("risk_per_trade_pct"), settings.risk.risk_per_trade_pct), 0.1, 100),
        "account_equity_usdt": _to_float(data.get("account_equity_usdt"), _to_float(current.get("account_equity_usdt"), settings.risk.account_equity_usdt), 100, 10000000),
        "margin_mode": str(data.get("margin_mode") or current.get("margin_mode") or settings.risk.margin_mode),
    }
    await _save_encrypted_dict(session, RISK_KEY, updated)
    apply_runtime_settings({"risk": updated})
    return updated


async def save_take_profit_settings(session: AsyncSession, data: dict[str, Any]) -> dict[str, Any]:
    current = await _load_encrypted_dict(session, TAKE_PROFIT_KEY)
    updated = {
        "num_levels": _to_int(data.get("num_levels"), _to_int(current.get("num_levels"), settings.take_profit.num_levels), 1, 4),
        "tp1_pct": _to_float(data.get("tp1_pct"), _to_float(current.get("tp1_pct"), settings.take_profit.tp1_pct), 0.1, 200),
        "tp2_pct": _to_float(data.get("tp2_pct"), _to_float(current.get("tp2_pct"), settings.take_profit.tp2_pct), 0.1, 200),
        "tp3_pct": _to_float(data.get("tp3_pct"), _to_float(current.get("tp3_pct"), settings.take_profit.tp3_pct), 0.1, 200),
        "tp4_pct": _to_float(data.get("tp4_pct"), _to_float(current.get("tp4_pct"), settings.take_profit.tp4_pct), 0.1, 200),
        "tp1_qty": _to_float(data.get("tp1_qty"), _to_float(current.get("tp1_qty"), settings.take_profit.tp1_qty), 0, 100),
        "tp2_qty": _to_float(data.get("tp2_qty"), _to_float(current.get("tp2_qty"), settings.take_profit.tp2_qty), 0, 100),
        "tp3_qty": _to_float(data.get("tp3_qty"), _to_float(current.get("tp3_qty"), settings.take_profit.tp3_qty), 0, 100),
        "tp4_qty": _to_float(data.get("tp4_qty"), _to_float(current.get("tp4_qty"), settings.take_profit.tp4_qty), 0, 100),
    }
    await _save_encrypted_dict(session, TAKE_PROFIT_KEY, updated)
    apply_runtime_settings({"take_profit": updated})
    return updated


async def save_trailing_stop_settings(session: AsyncSession, data: dict[str, Any]) -> dict[str, Any]:
    current = await _load_encrypted_dict(session, TRAILING_STOP_KEY)
    updated = {
        "mode": str(data.get("mode") or current.get("mode") or settings.trailing_stop.mode),
        "trail_pct": _to_float(data.get("trail_pct"), _to_float(current.get("trail_pct"), settings.trailing_stop.trail_pct), 0.1, 100),
        "activation_profit_pct": _to_float(data.get("activation_profit_pct"), _to_float(current.get("activation_profit_pct"), settings.trailing_stop.activation_profit_pct), 0, 100),
        "trailing_step_pct": _to_float(data.get("trailing_step_pct"), _to_float(current.get("trailing_step_pct"), settings.trailing_stop.trailing_step_pct), 0, 100),
        "breakeven_buffer_pct": _to_float(data.get("breakeven_buffer_pct"), _to_float(current.get("breakeven_buffer_pct"), 0.2), 0, 1.0),
        "step_buffer_pct": _to_float(data.get("step_buffer_pct"), _to_float(current.get("step_buffer_pct"), 0.3), 0, 2.0),
    }
    await _save_encrypted_dict(session, TRAILING_STOP_KEY, updated)
    apply_runtime_settings({"trailing_stop": updated})
    return updated


async def save_order_execution_settings(session: AsyncSession, data: dict[str, Any]) -> dict[str, Any]:
    current = await _load_encrypted_dict(session, ORDER_EXECUTION_KEY)
    updated = {
        "auto_approve_failed_orders": _to_bool(data.get("auto_approve_failed_orders"), _to_bool(current.get("auto_approve_failed_orders"), False)),
        "auto_reject_failed_orders": _to_bool(data.get("auto_reject_failed_orders"), _to_bool(current.get("auto_reject_failed_orders"), False)),
        "auto_retry_leverage_errors": _to_bool(data.get("auto_retry_leverage_errors"), _to_bool(current.get("auto_retry_leverage_errors"), False)),
        "max_leverage_retry_attempts": _to_int(data.get("max_leverage_retry_attempts"), _to_int(current.get("max_leverage_retry_attempts"), 3), 1, 10),
        "leverage_retry_delay_secs": _to_int(data.get("leverage_retry_delay_secs"), _to_int(current.get("leverage_retry_delay_secs"), 5), 1, 60),
    }
    await _save_encrypted_dict(session, ORDER_EXECUTION_KEY, updated)
    return updated


def runtime_status() -> dict[str, Any]:
    """Return non-secret runtime status for the dashboard."""
    return {
        "exchange": settings.exchange.name,
        "live_trading": settings.exchange.live_trading,
        "exchange_sandbox_mode": settings.exchange.sandbox_mode,
        "exchange_market_type": settings.exchange.market_type,
        "exchange_default_order_type": settings.exchange.default_order_type,
        "exchange_stop_loss_order_type": settings.exchange.stop_loss_order_type,
        "exchange_limit_timeout_overrides": normalize_limit_timeout_overrides(settings.exchange.limit_timeout_overrides),
        "exchange_api_configured": _public_secret_configured(settings.exchange.api_key),
        "exchange_api_key_masked": mask_secret(settings.exchange.api_key),
        "exchange_api_secret_masked": mask_secret(settings.exchange.api_secret),
        "exchange_password_configured": _public_secret_configured(settings.exchange.password),
        "exchange_password_masked": mask_secret(settings.exchange.password),
        "ai_provider": settings.ai.provider,
        "ai_api_configured": _public_secret_configured(
            settings.ai.openai_api_key
            or settings.ai.anthropic_api_key
            or settings.ai.deepseek_api_key
            or settings.ai.mistral_api_key
            or settings.ai.openrouter_api_key
            or settings.ai.custom_provider_api_key
        ),
        "openai_api_configured": _public_secret_configured(settings.ai.openai_api_key),
        "openai_api_key_masked": mask_secret(settings.ai.openai_api_key),
        "anthropic_api_configured": _public_secret_configured(settings.ai.anthropic_api_key),
        "anthropic_api_key_masked": mask_secret(settings.ai.anthropic_api_key),
        "deepseek_api_configured": _public_secret_configured(settings.ai.deepseek_api_key),
        "deepseek_api_key_masked": mask_secret(settings.ai.deepseek_api_key),
        "ai_temperature": settings.ai.temperature,
        "ai_max_tokens": settings.ai.max_tokens,
        "ai_custom_system_prompt": settings.ai.custom_system_prompt,
        "custom_provider_enabled": settings.ai.custom_provider_enabled,
        "custom_provider_name": settings.ai.custom_provider_name,
        "custom_provider_model": settings.ai.custom_provider_model,
        "custom_provider_url": settings.ai.custom_provider_api_url,
        "openrouter_enabled": settings.ai.openrouter_enabled,
        "openrouter_model": settings.ai.openrouter_model,
        "openrouter_api_configured": _public_secret_configured(settings.ai.openrouter_api_key),
        "openrouter_api_key_masked": mask_secret(settings.ai.openrouter_api_key),
        "mistral_model": settings.ai.mistral_model,
        "mistral_api_configured": _public_secret_configured(settings.ai.mistral_api_key),
        "mistral_api_key_masked": mask_secret(settings.ai.mistral_api_key),
        "custom_provider_api_configured": _public_secret_configured(settings.ai.custom_provider_api_key),
        "custom_provider_api_key_masked": mask_secret(settings.ai.custom_provider_api_key),
        "openai_model": settings.ai.openai_model,
        "anthropic_model": settings.ai.anthropic_model,
        "deepseek_model": settings.ai.deepseek_model,
        "telegram": {
            "configured": bool(settings.telegram.bot_token and settings.telegram.chat_id),
            "bot_configured": _public_secret_configured(settings.telegram.bot_token),
            "bot_token_masked": mask_secret(settings.telegram.bot_token),
            "chat_id": settings.telegram.chat_id,
        },
        "take_profit": {
            "num_levels": settings.take_profit.num_levels,
            "tp1_pct": settings.take_profit.tp1_pct,
            "tp2_pct": settings.take_profit.tp2_pct,
            "tp3_pct": settings.take_profit.tp3_pct,
            "tp4_pct": settings.take_profit.tp4_pct,
            "tp1_qty": settings.take_profit.tp1_qty,
            "tp2_qty": settings.take_profit.tp2_qty,
            "tp3_qty": settings.take_profit.tp3_qty,
            "tp4_qty": settings.take_profit.tp4_qty,
        },
        "trailing_stop": {
            "mode": settings.trailing_stop.mode,
            "trail_pct": settings.trailing_stop.trail_pct,
            "activation_profit_pct": settings.trailing_stop.activation_profit_pct,
            "trailing_step_pct": settings.trailing_stop.trailing_step_pct,
        },
        "risk": {
            "max_position_pct": settings.risk.max_position_pct,
            "max_daily_trades": settings.risk.max_daily_trades,
            "max_daily_loss_pct": settings.risk.max_daily_loss_pct,
            "exit_management_mode": settings.risk.exit_management_mode,
            "ai_risk_profile": settings.risk.ai_risk_profile,
            "custom_stop_loss_pct": settings.risk.custom_stop_loss_pct,
            "ai_exit_system_prompt": settings.risk.ai_exit_system_prompt,
            "position_sizing_mode": settings.risk.position_sizing_mode,
            "fixed_position_size_usdt": settings.risk.fixed_position_size_usdt,
            "risk_per_trade_pct": settings.risk.risk_per_trade_pct,
            "account_equity_usdt": settings.risk.account_equity_usdt,
            "margin_mode": settings.risk.margin_mode,
        },
        "voting": {
            "enabled": settings.ai.voting_enabled,
            "models": settings.ai.voting_models,
            "weights": settings.ai.voting_weights,
            "strategy": settings.ai.voting_strategy,
        },
    }


async def get_order_execution_settings(session: AsyncSession) -> dict[str, Any]:
    """Get order execution auto-approve/auto-reject settings."""
    return await _load_encrypted_dict(session, ORDER_EXECUTION_KEY)
