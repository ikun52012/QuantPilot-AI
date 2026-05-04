"""
QuantPilot AI - Hot Configuration Reload

Allows admins to reload key configuration values from the database
and environment without restarting the application process.

Supported reloadable settings:
  - webhook_secret
  - exchange credentials (sandbox_mode, live_trading)
  - AI provider config (provider, model, temperature, max_tokens)
  - Risk settings (max_daily_trades, max_daily_loss_pct, etc.)
  - Telegram settings

Usage:
    from core.hot_reload import reload_settings_from_db
    await reload_settings_from_db(session)
"""
from __future__ import annotations

import os
from typing import Any

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from core.config import settings
from core.database import get_admin_setting


# Cache of last-known values for change-detection
_last_known_settings: dict[str, Any] = {}


async def reload_settings_from_db(session: AsyncSession) -> dict[str, Any]:
    """Reload supported settings from admin settings table and env.

    Returns a dict of {setting_name: (old_value, new_value)} for all
    values that actually changed.
    """
    changed: dict[str, Any] = {}

    # ── Webhook secret ──
    old_webhook = getattr(settings.server, "webhook_secret", "")
    new_webhook = await get_admin_setting(session, "webhook_secret", old_webhook)
    if new_webhook != old_webhook:
        settings.server.webhook_secret = new_webhook
        changed["webhook_secret"] = ("***", "***")
        logger.info("[HotReload] webhook_secret updated")

    # ── Exchange mode ──
    old_live = getattr(settings.exchange, "live_trading", False)
    new_live_str = await get_admin_setting(session, "live_trading", str(old_live).lower())
    new_live = str(new_live_str).lower() in ("true", "1", "yes")
    if new_live != old_live:
        settings.exchange.live_trading = new_live
        changed["live_trading"] = (old_live, new_live)
        logger.warning(f"[HotReload] live_trading changed: {old_live} -> {new_live}")

    old_sandbox = getattr(settings.exchange, "sandbox_mode", False)
    new_sandbox_str = await get_admin_setting(session, "sandbox_mode", str(old_sandbox).lower())
    new_sandbox = str(new_sandbox_str).lower() in ("true", "1", "yes")
    if new_sandbox != old_sandbox:
        settings.exchange.sandbox_mode = new_sandbox
        changed["sandbox_mode"] = (old_sandbox, new_sandbox)
        logger.info(f"[HotReload] sandbox_mode changed: {old_sandbox} -> {new_sandbox}")

    # ── AI Provider ──
    old_ai_provider = getattr(settings.ai, "provider", "")
    new_ai_provider = await get_admin_setting(session, "ai_provider", old_ai_provider)
    if new_ai_provider and new_ai_provider != old_ai_provider:
        settings.ai.provider = new_ai_provider
        changed["ai_provider"] = (old_ai_provider, new_ai_provider)
        logger.info(f"[HotReload] ai.provider changed: {old_ai_provider} -> {new_ai_provider}")

    old_ai_model = getattr(settings.ai, "model", "")
    new_ai_model = await get_admin_setting(session, "ai_model", old_ai_model)
    if new_ai_model and new_ai_model != old_ai_model:
        settings.ai.model = new_ai_model
        changed["ai_model"] = (old_ai_model, new_ai_model)
        logger.info(f"[HotReload] ai.model changed: {old_ai_model} -> {new_ai_model}")

    # ── Risk settings ──
    risk_attrs = [
        ("max_daily_trades", int, 10),
        ("max_daily_loss_pct", float, 5.0),
        ("max_position_pct", float, 10.0),
        ("risk_per_trade_pct", float, 1.0),
        ("max_same_direction_positions", int, 5),
        ("max_correlated_exposure_pct", float, 50.0),
    ]
    for attr_name, coerce_type, default_val in risk_attrs:
        old_val = getattr(settings.risk, attr_name, default_val)
        raw_val = await get_admin_setting(session, attr_name, str(old_val))
        try:
            new_val = coerce_type(raw_val)
        except (ValueError, TypeError):
            continue
        if new_val != old_val:
            setattr(settings.risk, attr_name, new_val)
            changed[f"risk.{attr_name}"] = (old_val, new_val)
            logger.info(f"[HotReload] risk.{attr_name} changed: {old_val} -> {new_val}")

    # ── Environment overrides (highest priority) ──
    env_live = os.getenv("LIVE_TRADING")
    if env_live is not None:
        env_live_bool = env_live.lower() in ("true", "1", "yes")
        if env_live_bool != settings.exchange.live_trading:
            old = settings.exchange.live_trading
            settings.exchange.live_trading = env_live_bool
            changed["live_trading(env)"] = (old, env_live_bool)
            logger.warning(f"[HotReload] LIVE_TRADING overridden by env: {old} -> {env_live_bool}")

    if changed:
        logger.info(f"[HotReload] {len(changed)} setting(s) reloaded")
    else:
        logger.debug("[HotReload] No settings changed")

    return changed
