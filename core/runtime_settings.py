"""
Runtime admin settings.

Admin-facing settings are persisted in the database and applied to the
in-process configuration object so changes survive restart and take effect
without rebuilding the container.
"""
from __future__ import annotations

import json
from typing import Any

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from core.config import settings
from core.database import get_admin_setting, set_admin_setting
from core.security import decrypt_settings_payload, encrypt_settings_payload


EXCHANGE_KEY = "runtime_exchange"
AI_KEY = "runtime_ai"
TELEGRAM_KEY = "runtime_telegram"
RISK_KEY = "runtime_risk"
TAKE_PROFIT_KEY = "runtime_take_profit"
TRAILING_STOP_KEY = "runtime_trailing_stop"


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
        return settings.ai.openai_api_key
    if provider == "anthropic":
        return settings.ai.anthropic_api_key
    if provider == "deepseek":
        return settings.ai.deepseek_api_key
    if provider == "openrouter":
        return settings.ai.openrouter_api_key
    return settings.ai.custom_provider_api_key


def _normalize_ai_provider(provider: Any, default: str | None = None) -> str:
    value = str(provider or default or settings.ai.provider).lower().strip()
    allowed = {"openai", "anthropic", "deepseek", "openrouter", "custom"}
    return value if value in allowed else settings.ai.provider


async def _load_encrypted_dict(session: AsyncSession, key: str) -> dict[str, Any]:
    raw = await get_admin_setting(session, key, "")
    if not raw:
        return {}
    try:
        payload = json.loads(raw)
        if isinstance(payload, dict):
            decrypted = decrypt_settings_payload(payload)
            return decrypted if isinstance(decrypted, dict) else {}
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

    ai = runtime.get("ai") or {}
    if ai:
        settings.ai.provider = _normalize_ai_provider(ai.get("provider"))
        api_key = str(ai.get("api_key") or "")
        if settings.ai.provider == "openai":
            settings.ai.openai_api_key = api_key
        elif settings.ai.provider == "anthropic":
            settings.ai.anthropic_api_key = api_key
        elif settings.ai.provider == "deepseek":
            settings.ai.deepseek_api_key = api_key
        elif settings.ai.provider == "openrouter":
            settings.ai.openrouter_api_key = api_key
        else:
            settings.ai.custom_provider_api_key = api_key
        settings.ai.temperature = _to_float(ai.get("temperature"), settings.ai.temperature, 0, 2)
        settings.ai.max_tokens = _to_int(ai.get("max_tokens"), settings.ai.max_tokens, 100, 4000)
        settings.ai.custom_system_prompt = str(ai.get("custom_system_prompt") or "")
        settings.ai.custom_provider_enabled = _to_bool(ai.get("custom_provider_enabled"), settings.ai.custom_provider_enabled)
        settings.ai.custom_provider_name = str(ai.get("custom_provider_name") or settings.ai.custom_provider_name)
        settings.ai.custom_provider_model = str(ai.get("custom_provider_model") or "")
        settings.ai.custom_provider_api_url = str(ai.get("custom_provider_api_url") or "")
        settings.ai.openrouter_enabled = _to_bool(ai.get("openrouter_enabled"), settings.ai.openrouter_enabled)
        settings.ai.openrouter_model = str(ai.get("openrouter_model") or settings.ai.openrouter_model)
        settings.ai.openrouter_site_url = str(ai.get("openrouter_site_url") or settings.ai.openrouter_site_url)
        settings.ai.openrouter_app_name = str(ai.get("openrouter_app_name") or settings.ai.openrouter_app_name)
        if "voting_enabled" in ai:
            settings.ai.voting_enabled = _to_bool(ai.get("voting_enabled"), settings.ai.voting_enabled)
        if ai.get("voting_models"):
            models = ai.get("voting_models")
            if isinstance(models, list):
                settings.ai.voting_models = models
            elif isinstance(models, str):
                try:
                    settings.ai.voting_models = json.loads(models)
                except Exception as e:
                    logger.debug(f"[RuntimeSettings] Failed to parse voting_models: {e}")
        if ai.get("voting_weights"):
            weights = ai.get("voting_weights")
            if isinstance(weights, dict):
                settings.ai.voting_weights = weights
            elif isinstance(weights, str):
                try:
                    settings.ai.voting_weights = json.loads(weights)
                except Exception as e:
                    logger.debug(f"[RuntimeSettings] Failed to parse voting_weights: {e}")
        if ai.get("voting_strategy"):
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

    take_profit = runtime.get("take_profit") or {}
    if take_profit:
        settings.take_profit.num_levels = _to_int(take_profit.get("num_levels"), settings.take_profit.num_levels, 1, 4)
        for attr in ("tp1_pct", "tp2_pct", "tp3_pct", "tp4_pct", "tp1_qty", "tp2_qty", "tp3_qty", "tp4_qty"):
            setattr(settings.take_profit, attr, _to_float(take_profit.get(attr), getattr(settings.take_profit, attr), 0, 200))

    trailing_stop = runtime.get("trailing_stop") or {}
    if trailing_stop:
        mode = str(trailing_stop.get("mode") or settings.trailing_stop.mode)
        allowed = {"none", "moving", "breakeven_on_tp1", "step_trailing", "profit_pct_trailing"}
        settings.trailing_stop.mode = mode if mode in allowed else "none"
        settings.trailing_stop.trail_pct = _to_float(trailing_stop.get("trail_pct"), settings.trailing_stop.trail_pct, 0.1, 100)
        settings.trailing_stop.activation_profit_pct = _to_float(
            trailing_stop.get("activation_profit_pct"), settings.trailing_stop.activation_profit_pct, 0, 100
        )
        settings.trailing_stop.trailing_step_pct = _to_float(
            trailing_stop.get("trailing_step_pct"), settings.trailing_stop.trailing_step_pct, 0, 100
        )


async def apply_persisted_admin_settings(session: AsyncSession) -> dict[str, dict[str, Any]]:
    runtime = await load_admin_runtime_settings(session)
    apply_runtime_settings(runtime)

    try:
        from core.database import get_admin_setting

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
    except Exception as e:
        logger.debug(f"[RuntimeSettings] Failed to apply persisted admin settings: {e}")

    return runtime


async def save_exchange_settings(session: AsyncSession, data: dict[str, Any]) -> dict[str, Any]:
    current = await _load_encrypted_dict(session, EXCHANGE_KEY)
    updated = {
        "name": str(data.get("exchange") or data.get("name") or current.get("name") or settings.exchange.name).lower().strip(),
        "api_key": str(data.get("api_key") or current.get("api_key") or settings.exchange.api_key or ""),
        "api_secret": str(data.get("api_secret") or current.get("api_secret") or settings.exchange.api_secret or ""),
        "password": str(data.get("password") or current.get("password") or settings.exchange.password or ""),
        "live_trading": _to_bool(data.get("live_trading"), _to_bool(current.get("live_trading"), settings.exchange.live_trading)),
        "sandbox_mode": _to_bool(data.get("sandbox_mode"), _to_bool(current.get("sandbox_mode"), settings.exchange.sandbox_mode)),
    }
    await _save_encrypted_dict(session, EXCHANGE_KEY, updated)
    apply_runtime_settings({"exchange": updated})
    return updated


async def save_ai_settings(session: AsyncSession, data: dict[str, Any]) -> dict[str, Any]:
    current = await _load_encrypted_dict(session, AI_KEY)
    provider = _normalize_ai_provider(data.get("provider") or current.get("provider"))
    updated = {
        "provider": provider,
        "api_key": str(data.get("api_key") or current.get("api_key") or _current_ai_key(provider) or ""),
        "temperature": _to_float(data.get("temperature"), _to_float(current.get("temperature"), settings.ai.temperature), 0, 2),
        "max_tokens": _to_int(data.get("max_tokens"), _to_int(current.get("max_tokens"), settings.ai.max_tokens), 100, 4000),
        "custom_system_prompt": str(data.get("custom_system_prompt") if data.get("custom_system_prompt") is not None else current.get("custom_system_prompt", "")),
        "custom_provider_enabled": _to_bool(data.get("custom_provider_enabled"), _to_bool(current.get("custom_provider_enabled"), False)),
        "custom_provider_name": str(data.get("custom_provider_name") or current.get("custom_provider_name") or settings.ai.custom_provider_name),
        "custom_provider_model": str(data.get("custom_provider_model") or current.get("custom_provider_model") or ""),
        "custom_provider_api_url": str(data.get("custom_provider_api_url") or current.get("custom_provider_api_url") or ""),
        "openrouter_enabled": _to_bool(data.get("openrouter_enabled"), _to_bool(current.get("openrouter_enabled"), settings.ai.openrouter_enabled)),
        "openrouter_model": str(data.get("openrouter_model") or current.get("openrouter_model") or settings.ai.openrouter_model),
        "openrouter_site_url": str(data.get("openrouter_site_url") or current.get("openrouter_site_url") or settings.ai.openrouter_site_url),
        "openrouter_app_name": str(data.get("openrouter_app_name") or current.get("openrouter_app_name") or settings.ai.openrouter_app_name),
        "voting_enabled": settings.ai.voting_enabled,
        "voting_models": settings.ai.voting_models,
        "voting_weights": settings.ai.voting_weights,
        "voting_strategy": settings.ai.voting_strategy,
    }
    await _save_encrypted_dict(session, AI_KEY, updated)
    apply_runtime_settings({"ai": updated})
    return updated


async def save_telegram_settings(session: AsyncSession, data: dict[str, Any]) -> dict[str, Any]:
    current = await _load_encrypted_dict(session, TELEGRAM_KEY)
    updated = {
        "bot_token": str(data.get("bot_token") or current.get("bot_token") or settings.telegram.bot_token or ""),
        "chat_id": str(data.get("chat_id") if data.get("chat_id") is not None else current.get("chat_id", settings.telegram.chat_id or "")),
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
    }
    await _save_encrypted_dict(session, TRAILING_STOP_KEY, updated)
    apply_runtime_settings({"trailing_stop": updated})
    return updated


def runtime_status() -> dict[str, Any]:
    """Return non-secret runtime status for the dashboard."""
    return {
        "exchange": settings.exchange.name,
        "live_trading": settings.exchange.live_trading,
        "exchange_sandbox_mode": settings.exchange.sandbox_mode,
        "exchange_api_configured": _public_secret_configured(settings.exchange.api_key),
        "exchange_password_configured": _public_secret_configured(settings.exchange.password),
        "ai_provider": settings.ai.provider,
        "ai_api_configured": _public_secret_configured(
            settings.ai.openai_api_key
            or settings.ai.anthropic_api_key
            or settings.ai.deepseek_api_key
            or settings.ai.openrouter_api_key
            or settings.ai.custom_provider_api_key
        ),
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
        "telegram": {
            "configured": bool(settings.telegram.bot_token and settings.telegram.chat_id),
            "bot_configured": _public_secret_configured(settings.telegram.bot_token),
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
        },
        "voting": {
            "enabled": settings.ai.voting_enabled,
            "models": settings.ai.voting_models,
            "weights": settings.ai.voting_weights,
            "strategy": settings.ai.voting_strategy,
        },
    }
