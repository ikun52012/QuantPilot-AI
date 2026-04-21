"""
Signal Server - User Router
User-facing routes for dashboard, settings, and trading.
"""
import json
import copy
from datetime import datetime, timezone, timedelta
from typing import Optional

from fastapi import APIRouter, Request, Response, HTTPException, Depends, Query
from pydantic import BaseModel, Field
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, update

from core.database import (
    get_db, get_user_by_id, get_user_active_subscription,
    TradeModel, PositionModel, PaymentModel, SubscriptionModel,
)
from core.auth import get_current_user
from core.config import settings
from core.security import encrypt_settings_payload, decrypt_settings_payload
from core import runtime_settings


router = APIRouter(prefix="/api", tags=["user"])


# ─────────────────────────────────────────────
# Request Models
# ─────────────────────────────────────────────

class UserSettingsRequest(BaseModel):
    exchange: str = Field(default="binance", max_length=40)
    api_key: str = Field(default="", max_length=300)
    api_secret: str = Field(default="", max_length=300)
    password: str = Field(default="", max_length=300)
    live_trading: bool = False
    sandbox_mode: bool = False


class AISettingsRequest(BaseModel):
    provider: str = Field(default="deepseek", max_length=40)
    api_key: str = Field(default="", max_length=500)
    temperature: float = Field(default=0.3, ge=0, le=2)
    max_tokens: int = Field(default=1000, ge=100, le=4000)
    custom_system_prompt: str = Field(default="", max_length=12000)
    custom_provider_enabled: bool = False
    custom_provider_name: str = Field(default="custom", max_length=80)
    custom_provider_model: str = Field(default="", max_length=160)
    custom_provider_api_url: str = Field(default="", max_length=500)


class TelegramSettingsRequest(BaseModel):
    bot_token: str = Field(default="", max_length=300)
    chat_id: str = Field(default="", max_length=120)


class RiskSettingsRequest(BaseModel):
    max_position_pct: float = Field(default=10.0, ge=0.1, le=100.0)
    max_daily_trades: int = Field(default=10, ge=1, le=10000)
    max_daily_loss_pct: float = Field(default=5.0, ge=0.1, le=100.0)
    exit_management_mode: str = Field(default="ai", max_length=20)
    ai_risk_profile: str = Field(default="balanced", max_length=40)
    custom_stop_loss_pct: float = Field(default=1.5, ge=0.1, le=100.0)
    ai_exit_system_prompt: str = Field(default="", max_length=12000)


class TakeProfitSettingsRequest(BaseModel):
    num_levels: int = Field(default=1, ge=1, le=4)
    tp1_pct: float = Field(default=2.0, gt=0, le=200.0)
    tp2_pct: float = Field(default=4.0, gt=0, le=200.0)
    tp3_pct: float = Field(default=6.0, gt=0, le=200.0)
    tp4_pct: float = Field(default=10.0, gt=0, le=200.0)
    tp1_qty: float = Field(default=25.0, ge=0.0, le=100.0)
    tp2_qty: float = Field(default=25.0, ge=0.0, le=100.0)
    tp3_qty: float = Field(default=25.0, ge=0.0, le=100.0)
    tp4_qty: float = Field(default=25.0, ge=0.0, le=100.0)


class TrailingStopSettingsRequest(BaseModel):
    mode: str = Field(default="none", max_length=40)
    trail_pct: float = Field(default=1.0, ge=0.1, le=100.0)
    activation_profit_pct: float = Field(default=1.0, ge=0.0, le=100.0)
    trailing_step_pct: float = Field(default=0.5, ge=0.0, le=100.0)


def _is_admin(user: dict) -> bool:
    return user.get("role") == "admin"


def _require_admin(user: dict) -> None:
    if not _is_admin(user):
        raise HTTPException(403, "Admin access required")


def _load_user_settings(db_user) -> dict:
    try:
        raw = json.loads(db_user.settings_json or "{}")
        settings_data = decrypt_settings_payload(raw)
        return settings_data if isinstance(settings_data, dict) else {}
    except Exception:
        return {}


def _loads_list(value) -> list:
    if isinstance(value, list):
        return value
    try:
        parsed = json.loads(value or "[]")
        return parsed if isinstance(parsed, list) else []
    except Exception:
        return []


async def _save_user_settings(db: AsyncSession, db_user, settings_data: dict) -> None:
    db_user.settings_json = json.dumps(encrypt_settings_payload(settings_data))
    await db.commit()


async def _save_user_exchange(db: AsyncSession, db_user, req: UserSettingsRequest) -> None:
    current = _load_user_settings(db_user)
    current_exchange = current.get("exchange") or {}
    current["exchange"] = {
        "name": req.exchange.lower().strip(),
        "exchange": req.exchange.lower().strip(),
        "api_key": req.api_key or current_exchange.get("api_key", ""),
        "api_secret": req.api_secret or current_exchange.get("api_secret", ""),
        "password": req.password or current_exchange.get("password", ""),
        "live_trading": bool(req.live_trading),
        "sandbox_mode": bool(req.sandbox_mode),
    }
    await _save_user_settings(db, db_user, current)


def _global_exchange_config() -> dict:
    return {
        "exchange": settings.exchange.name,
        "api_key": settings.exchange.api_key,
        "api_secret": settings.exchange.api_secret,
        "password": settings.exchange.password,
        "live_trading": settings.exchange.live_trading,
        "sandbox_mode": settings.exchange.sandbox_mode,
    }


async def _exchange_config_for_user(db: AsyncSession, user: dict) -> dict:
    if _is_admin(user):
        return _global_exchange_config()

    db_user = await get_user_by_id(db, user["sub"])
    if not db_user:
        return {"live_trading": False}
    settings_data = _load_user_settings(db_user)
    exchange = settings_data.get("exchange") or {}
    return {
        "exchange": exchange.get("name") or exchange.get("exchange") or settings.exchange.name,
        "api_key": exchange.get("api_key") or "",
        "api_secret": exchange.get("api_secret") or "",
        "password": exchange.get("password") or "",
        "live_trading": bool(exchange.get("live_trading")),
        "sandbox_mode": bool(exchange.get("sandbox_mode")),
    }


# ─────────────────────────────────────────────
# Status & Info
# ─────────────────────────────────────────────

@router.get("/status")
async def get_status(
    user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Get system status and configuration."""
    status = runtime_settings.runtime_status()
    return {
        **status,
        "version": settings.app_version,
    }


# ─────────────────────────────────────────────
# Positions
# ─────────────────────────────────────────────

@router.get("/positions")
async def get_positions(
    user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Get tracked open positions, scoped to the current user."""
    filters = [PositionModel.status == "open"]
    if not _is_admin(user):
        filters.append(PositionModel.user_id == user.get("sub"))

    result = await db.execute(
        select(PositionModel)
        .where(*filters)
        .order_by(PositionModel.opened_at.desc())
        .limit(200)
    )
    positions = result.scalars().all()

    return [
        {
            "id": p.id,
            "symbol": p.ticker,
            "side": p.direction,
            "contracts": p.remaining_quantity or p.quantity,
            "entryPrice": p.entry_price,
            "entry_price": p.entry_price,
            "markPrice": p.last_price,
            "mark_price": p.last_price,
            "stop_loss": p.stop_loss,
            "take_profit_levels": _loads_list(p.take_profit_json),
            "unrealizedPnl": 0,
            "unrealized_pnl": 0,
            "percentage": p.current_pnl_pct,
            "leverage": p.leverage,
            "mode": "exchange" if p.live_trading else "paper",
            "sandbox_mode": p.sandbox_mode,
            "opened_at": p.opened_at.isoformat() if p.opened_at else None,
            "updated_at": p.updated_at.isoformat() if p.updated_at else None,
        }
        for p in positions
    ]


@router.get("/balance")
async def get_balance(
    user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Get account balance from exchange."""
    from exchange import get_account_balance

    balance = await get_account_balance(await _exchange_config_for_user(db, user))
    return balance


# ─────────────────────────────────────────────
# Trade History
# ─────────────────────────────────────────────

@router.get("/trades")
async def get_trades(
    days: int = Query(1, ge=1, le=365),
    user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Get recent trades."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    filters = [TradeModel.timestamp >= cutoff]
    if not _is_admin(user):
        filters.append(TradeModel.user_id == user.get("sub"))

    result = await db.execute(
        select(TradeModel)
        .where(*filters)
        .order_by(TradeModel.timestamp.desc())
        .limit(100)
    )
    trades = result.scalars().all()

    return [
        {
            "id": t.id,
            "timestamp": t.timestamp.isoformat() if t.timestamp else None,
            "ticker": t.ticker,
            "direction": t.direction,
            "execute": t.execute,
            "order_status": t.order_status,
            "pnl_pct": t.pnl_pct,
        }
        for t in trades
    ]


@router.get("/history")
async def get_history(
    days: int = Query(30, ge=1, le=365),
    user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Get trade history with details."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    filters = [TradeModel.timestamp >= cutoff]
    if not _is_admin(user):
        filters.append(TradeModel.user_id == user.get("sub"))

    result = await db.execute(
        select(TradeModel)
        .where(*filters)
        .order_by(TradeModel.timestamp.desc())
    )
    trades = result.scalars().all()

    history = []
    for t in trades:
        payload = {}
        try:
            payload = json.loads(t.payload_json) if t.payload_json else {}
        except (TypeError, json.JSONDecodeError):
            pass

        signal = payload.get("signal", {})
        analysis = payload.get("analysis", {})
        order_details = payload.get("order_details") or payload.get("result") or {}

        history.append({
            "id": t.id,
            "timestamp": t.timestamp.isoformat() if t.timestamp else None,
            "ticker": t.ticker,
            "direction": t.direction,
            "entry_price": signal.get("price") or payload.get("entry_price") or order_details.get("entry_price"),
            "exit_price": payload.get("exit_price") or order_details.get("exit_price"),
            "stop_loss": analysis.get("suggested_stop_loss") or payload.get("stop_loss"),
            "take_profit": (
                analysis.get("suggested_take_profit")
                or analysis.get("suggested_tp1")
                or payload.get("take_profit")
            ),
            "close_reason": payload.get("close_reason"),
            "ai": {
                "confidence": analysis.get("confidence"),
                "recommendation": analysis.get("recommendation"),
                "reasoning": analysis.get("reasoning"),
                "recommended_leverage": analysis.get("recommended_leverage"),
            },
            "order_status": t.order_status,
            "pnl_pct": t.pnl_pct,
        })

    return history


# ─────────────────────────────────────────────
# Analytics
# ─────────────────────────────────────────────

@router.get("/performance")
@router.get("/user/performance")
async def get_performance(
    days: int = Query(30, ge=1, le=365),
    user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Get performance analytics."""
    from analytics import calculate_performance

    perf = await calculate_performance(db, days=days, user_id=None if _is_admin(user) else user.get("sub"))
    return perf


@router.get("/daily-pnl")
async def get_daily_pnl(
    days: int = Query(30, ge=1, le=365),
    user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Get daily PnL data."""
    from analytics import get_daily_pnl

    daily = await get_daily_pnl(db, days=days, user_id=None if _is_admin(user) else user.get("sub"))
    return daily


# ─────────────────────────────────────────────
# Settings
# ─────────────────────────────────────────────

@router.get("/settings")
@router.get("/user/settings")
async def get_user_settings(
    request: Request,
    user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Get user settings."""
    db_user = await get_user_by_id(db, user["sub"])
    if not db_user:
        raise HTTPException(404, "User not found")

    settings_data = _load_user_settings(db_user)

    webhook = settings_data.setdefault("webhook", {})
    if not webhook.get("secret"):
        from core.security import generate_webhook_secret, webhook_secret_hash
        webhook["secret"] = generate_webhook_secret()
        db_user.webhook_secret_hash = webhook_secret_hash(webhook["secret"])
        await _save_user_settings(db, db_user, settings_data)
    elif not db_user.webhook_secret_hash:
        from core.security import webhook_secret_hash
        db_user.webhook_secret_hash = webhook_secret_hash(webhook["secret"])
        await db.commit()

    response_data = copy.deepcopy(settings_data)

    exchange = response_data.setdefault("exchange", {})
    exchange.setdefault("name", exchange.get("exchange") or settings.exchange.name)
    exchange.setdefault("exchange", exchange.get("name") or settings.exchange.name)
    exchange["api_configured"] = bool(exchange.get("api_key") and exchange.get("api_secret"))
    exchange.pop("api_key", None)
    exchange.pop("api_secret", None)
    exchange.pop("password", None)

    webhook = response_data.setdefault("webhook", {})
    base_url = str(request.base_url).rstrip("/")
    webhook.setdefault("url", f"{base_url}/webhook")
    if webhook.get("secret"):
        webhook.setdefault("template", json.dumps({
            "secret": webhook.get("secret"),
            "ticker": "{{ticker}}",
            "exchange": "{{exchange}}",
            "direction": "long",
            "price": "{{close}}",
            "timeframe": "{{interval}}",
            "strategy": "{{strategy.order.comment}}",
            "message": "{{strategy.order.action}} {{ticker}} @ {{close}}",
        }, indent=2))

    subscription = await get_user_active_subscription(db, db_user.id)
    response_data["trade_controls"] = {
        "live_trading_allowed": bool(db_user.live_trading_allowed and subscription),
        "max_leverage": db_user.max_leverage or 20,
        "max_position_pct": db_user.max_position_pct or 10,
        "subscription_active": bool(subscription),
    }

    return response_data


@router.put("/settings")
@router.put("/user/settings")
async def update_user_settings(
    settings_data: dict,
    user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Update user settings."""
    db_user = await get_user_by_id(db, user["sub"])
    if not db_user:
        raise HTTPException(404, "User not found")

    # Load existing settings
    current = _load_user_settings(db_user)

    # Merge settings
    for key, value in settings_data.items():
        if isinstance(value, dict) and isinstance(current.get(key), dict):
            current[key].update(value)
        else:
            current[key] = value

    # Save encrypted
    await _save_user_settings(db, db_user, current)

    return {"status": "ok"}


@router.post("/settings/exchange")
@router.post("/user/settings/exchange")
async def save_exchange_settings(
    req: UserSettingsRequest,
    request: Request,
    user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Save exchange settings."""
    if _is_admin(user) and request.url.path.endswith("/api/settings/exchange"):
        await runtime_settings.save_exchange_settings(db, req.model_dump())
        await db.commit()
        return {"status": "ok"}

    db_user = await get_user_by_id(db, user["sub"])
    if not db_user:
        raise HTTPException(404, "User not found")

    await _save_user_exchange(db, db_user, req)

    return {"status": "ok"}


@router.post("/settings/take-profit")
@router.post("/user/settings/take-profit")
async def save_take_profit_settings(
    req: TakeProfitSettingsRequest,
    request: Request,
    user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Save take-profit settings."""
    if _is_admin(user) and request.url.path.endswith("/api/settings/take-profit"):
        await runtime_settings.save_take_profit_settings(db, req.model_dump())
        await db.commit()
        return {"status": "ok"}

    db_user = await get_user_by_id(db, user["sub"])
    if not db_user:
        raise HTTPException(404, "User not found")

    # Load existing settings
    current = _load_user_settings(db_user)

    # Update TP settings
    current["take_profit"] = {
        "num_levels": req.num_levels,
        "tp1_pct": req.tp1_pct,
        "tp2_pct": req.tp2_pct,
        "tp3_pct": req.tp3_pct,
        "tp4_pct": req.tp4_pct,
        "tp1_qty": req.tp1_qty,
        "tp2_qty": req.tp2_qty,
        "tp3_qty": req.tp3_qty,
        "tp4_qty": req.tp4_qty,
    }

    await _save_user_settings(db, db_user, current)

    return {"status": "ok"}


@router.post("/settings/ai")
async def save_ai_settings(
    req: AISettingsRequest,
    user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Persist admin AI runtime settings."""
    _require_admin(user)
    await runtime_settings.save_ai_settings(db, req.model_dump())
    await db.commit()
    return {"status": "ok"}


@router.post("/settings/telegram")
async def save_telegram_settings(
    req: TelegramSettingsRequest,
    user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Persist admin Telegram runtime settings."""
    _require_admin(user)
    await runtime_settings.save_telegram_settings(db, req.model_dump())
    await db.commit()
    return {"status": "ok"}


@router.post("/settings/risk")
async def save_risk_settings(
    req: RiskSettingsRequest,
    user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Persist admin risk runtime settings."""
    _require_admin(user)
    await runtime_settings.save_risk_settings(db, req.model_dump())
    await db.commit()
    return {"status": "ok"}


@router.post("/settings/trailing-stop")
async def save_trailing_stop_settings(
    req: TrailingStopSettingsRequest,
    user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Persist admin trailing-stop runtime settings."""
    _require_admin(user)
    await runtime_settings.save_trailing_stop_settings(db, req.model_dump())
    await db.commit()
    return {"status": "ok"}


# ─────────────────────────────────────────────
# Webhook Secret
# ─────────────────────────────────────────────

@router.get("/webhook-secret")
async def get_webhook_secret(
    user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Get user's webhook secret."""
    from core.security import generate_webhook_secret, webhook_secret_hash

    db_user = await get_user_by_id(db, user["sub"])
    if not db_user:
        raise HTTPException(404, "User not found")

    # Load settings to get secret
    settings_data = {}
    try:
        settings_data = json.loads(db_user.settings_json or "{}")
        settings_data = decrypt_settings_payload(settings_data)
    except (TypeError, json.JSONDecodeError):
        pass

    secret = (settings_data.get("webhook") or {}).get("secret", "")

    if not secret:
        secret = generate_webhook_secret()
        if "webhook" not in settings_data:
            settings_data["webhook"] = {}
        settings_data["webhook"]["secret"] = secret
        db_user.settings_json = json.dumps(encrypt_settings_payload(settings_data))
        db_user.webhook_secret_hash = webhook_secret_hash(secret)
        await db.commit()

    return {"secret": secret}


# ─────────────────────────────────────────────
# Test Connection
# ─────────────────────────────────────────────

@router.post("/test-connection")
async def test_connection(
    req: UserSettingsRequest,
    user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Test exchange connection."""
    from exchange import test_exchange_connection

    result = await test_exchange_connection(
        exchange_id=req.exchange,
        api_key=req.api_key,
        api_secret=req.api_secret,
        password=req.password,
        sandbox_mode=req.sandbox_mode,
    )

    return result


@router.post("/test-telegram")
async def test_telegram(
    user: dict = Depends(get_current_user),
):
    """Send a test Telegram notification using the current runtime settings."""
    if not settings.telegram.bot_token or not settings.telegram.chat_id:
        raise HTTPException(400, "Telegram bot token or chat ID is not configured")
    from notifier import send_telegram

    await send_telegram("✅ TradingView Signal Server Telegram test message")
    return {"status": "ok"}
