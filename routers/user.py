"""
Signal Server - User Router
User-facing routes for dashboard, settings, and trading.
"""
import copy
import json
import uuid
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from loguru import logger
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core import runtime_settings
from core.auth import get_current_user
from core.config import settings
from core.database import (
    PositionModel,
    TradeModel,
    get_db,
    get_user_active_subscription,
    get_user_by_id,
)
from core.request_utils import public_base_url
from core.security import decrypt_settings_payload, encrypt_settings_payload
from core.utils.common import normalize_limit_timeout_overrides, position_symbol_key
from core.utils.datetime import utcnow

router = APIRouter(prefix="/api", tags=["user"])


def _simplify_symbol(ticker: str) -> str:
    """Simplify ticker name by removing USDT, USD, PERP, .P suffixes."""
    ticker = str(ticker or "").upper().strip()
    for suffix in ["USDT.P", "USDT", "USD.P", "USD", "PERP", ".P", "_P"]:
        if ticker.endswith(suffix):
            ticker = ticker[:-len(suffix)]
            break
    return ticker or ticker


def _status_text(status: str) -> str:
    """Convert status code to human-readable text."""
    status_map = {
        "pending": "挂单中",
        "open": "持仓中",
        "closed": "已平仓",
        "cancelled": "已取消",
    }
    return status_map.get(str(status or "").lower(), status or "未知")


def _loads_list(json_str: str) -> list:
    """Parse JSON list safely."""
    try:
        data = json.loads(str(json_str or "[]"))
        return data if isinstance(data, list) else []
    except Exception:
        return []


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
    market_type: str = Field(default="contract", max_length=20)
    default_order_type: str = Field(default="limit", max_length=20)
    stop_loss_order_type: str = Field(default="market", max_length=20)
    limit_timeout_overrides: dict[str, int] = Field(default_factory=dict)

    @field_validator("exchange")
    @classmethod
    def _validate_exchange(cls, v: str) -> str:
        allowed = {"binance", "okx", "bybit", "bitget", "gate", "coinbase"}
        normalized = v.lower().strip()
        if normalized not in allowed:
            raise ValueError(f"exchange must be one of: {', '.join(sorted(allowed))}")
        return normalized

    @field_validator("market_type")
    @classmethod
    def _validate_market_type(cls, v: str) -> str:
        normalized = v.lower().strip()
        if normalized not in {"spot", "contract"}:
            raise ValueError("market_type must be 'spot' or 'contract'")
        return normalized

    @field_validator("default_order_type")
    @classmethod
    def _validate_default_order_type(cls, v: str) -> str:
        normalized = v.lower().strip()
        if normalized not in {"market", "limit"}:
            raise ValueError("default_order_type must be 'market' or 'limit'")
        return normalized

    @field_validator("stop_loss_order_type")
    @classmethod
    def _validate_stop_loss_order_type(cls, v: str) -> str:
        normalized = v.lower().strip()
        if normalized != "market":
            raise ValueError("stop_loss_order_type must be 'market'")
        return normalized


class AISettingsRequest(BaseModel):
    provider: str = Field(default="deepseek", max_length=40)
    api_key: str = Field(default="", max_length=500)
    temperature: float = Field(default=0.3, ge=0, le=2)
    max_tokens: int = Field(default=1000, ge=100, le=4000)
    custom_system_prompt: str = Field(default="", max_length=12000)
    openrouter_enabled: bool = False
    openrouter_model: str = Field(default="", max_length=160)
    openrouter_site_url: str = Field(default="", max_length=500)
    openrouter_app_name: str = Field(default="QuantPilot AI", max_length=120)
    custom_provider_enabled: bool = False
    custom_provider_name: str = Field(default="custom", max_length=80)
    custom_provider_model: str = Field(default="", max_length=160)
    custom_provider_api_url: str = Field(default="", max_length=500)
    mistral_model: str = Field(default="mistral-large-latest", max_length=160)
    mistral_api_key: str = Field(default="", max_length=500)
    openai_model: str = Field(default="", max_length=160)
    anthropic_model: str = Field(default="", max_length=160)
    deepseek_model: str = Field(default="", max_length=160)
    voting_enabled: bool = False
    voting_models: list[str] = Field(default_factory=list)
    voting_weights: dict[str, float] = Field(default_factory=dict)
    voting_strategy: str = Field(default="weighted", max_length=40)

    @field_validator("voting_strategy")
    @classmethod
    def _validate_voting_strategy(cls, v: str) -> str:
        allowed = {"weighted", "consensus", "best_confidence"}
        if v not in allowed:
            raise ValueError(f"voting_strategy must be one of: {', '.join(sorted(allowed))}")
        return v


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
    position_sizing_mode: str = Field(default="percentage", max_length=20)
    fixed_position_size_usdt: float = Field(default=100.0, ge=1.0, le=1000000.0)
    risk_per_trade_pct: float = Field(default=1.0, ge=0.1, le=100.0)
    account_equity_usdt: float = Field(default=10000.0, ge=100.0, le=10000000.0)

    @field_validator("position_sizing_mode")
    @classmethod
    def _validate_sizing_mode(cls, v: str) -> str:
        allowed = {"percentage", "fixed", "risk_ratio"}
        if v not in allowed:
            raise ValueError(f"position_sizing_mode must be one of: {', '.join(sorted(allowed))}")
        return v

    @field_validator("exit_management_mode")
    @classmethod
    def _validate_exit_mode(cls, v: str) -> str:
        allowed = {"ai", "custom"}
        if v not in allowed:
            raise ValueError(f"exit_management_mode must be one of: {', '.join(sorted(allowed))}")
        return v

    @field_validator("ai_risk_profile")
    @classmethod
    def _validate_risk_profile(cls, v: str) -> str:
        allowed = {"conservative", "balanced", "aggressive"}
        if v not in allowed:
            raise ValueError(f"ai_risk_profile must be one of: {', '.join(sorted(allowed))}")
        return v


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

    @field_validator("mode")
    @classmethod
    def _validate_mode(cls, v: str) -> str:
        allowed = {"none", "auto", "moving", "breakeven_on_tp1", "step_trailing", "profit_pct_trailing"}
        if v not in allowed:
            raise ValueError(f"mode must be one of: {', '.join(sorted(allowed))}")
        return v


class OfflineTradeSyncRequest(BaseModel):
    id: str = Field(default="", max_length=80)
    ticker: str = Field(min_length=1, max_length=40)
    direction: str = Field(default="manual", max_length=20)
    timestamp: datetime | None = None
    entry_price: float | None = Field(default=None, ge=0)
    exit_price: float | None = Field(default=None, ge=0)
    quantity: float = Field(default=0.0, ge=0)
    pnl_pct: float = Field(default=0.0, ge=-1000, le=1000)
    execute: bool = False
    order_status: str = Field(default="offline_synced", max_length=30)
    payload: dict = Field(default_factory=dict)

    @field_validator("ticker")
    @classmethod
    def _normalize_ticker(cls, value: str) -> str:
        normalized = value.upper().strip()
        allowed = set("ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789/:._-")
        if not normalized or any(ch not in allowed for ch in normalized):
            raise ValueError("ticker contains unsupported characters")
        return normalized

    @field_validator("direction")
    @classmethod
    def _normalize_direction(cls, value: str) -> str:
        normalized = value.lower().strip()
        allowed = {"long", "short", "close_long", "close_short", "manual"}
        if normalized not in allowed:
            raise ValueError(f"direction must be one of: {', '.join(sorted(allowed))}")
        return normalized


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
    except Exception as e:
        logger.debug(f"[User] Failed to load user settings: {e}")
        return {}


def _loads_list(value) -> list:
    if isinstance(value, list):
        return value
    try:
        parsed = json.loads(value or "[]")
        return parsed if isinstance(parsed, list) else []
    except Exception as e:
        logger.debug(f"[User] Failed to parse list value: {e}")
        return []


def _present_str(value, fallback: str = "") -> str:
    if value is None:
        return fallback
    return str(value)


async def _save_user_settings(db: AsyncSession, db_user, settings_data: dict) -> None:
    db_user.settings_json = json.dumps(encrypt_settings_payload(settings_data))
    await db.commit()


async def _save_user_exchange(db: AsyncSession, db_user, req: UserSettingsRequest) -> None:
    current = _load_user_settings(db_user)
    current_exchange = current.get("exchange") or {}
    current["exchange"] = {
        "name": req.exchange.lower().strip(),
        "exchange": req.exchange.lower().strip(),
        "api_key": _present_str(req.api_key, _present_str(current_exchange.get("api_key"), "")),
        "api_secret": _present_str(req.api_secret, _present_str(current_exchange.get("api_secret"), "")),
        "password": _present_str(req.password, _present_str(current_exchange.get("password"), "")),
        "live_trading": bool(req.live_trading),
        "sandbox_mode": bool(req.sandbox_mode),
        "market_type": req.market_type,
        "default_order_type": req.default_order_type,
        "stop_loss_order_type": req.stop_loss_order_type,
        "limit_timeout_overrides": normalize_limit_timeout_overrides(req.limit_timeout_overrides),
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
        "market_type": settings.exchange.market_type,
        "default_order_type": settings.exchange.default_order_type,
        "stop_loss_order_type": settings.exchange.stop_loss_order_type,
        "limit_timeout_overrides": normalize_limit_timeout_overrides(settings.exchange.limit_timeout_overrides),
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
        "exchange": exchange.get("name") if "name" in exchange else exchange.get("exchange") or settings.exchange.name,
        "api_key": str(exchange.get("api_key") if "api_key" in exchange else ""),
        "api_secret": str(exchange.get("api_secret") if "api_secret" in exchange else ""),
        "password": str(exchange.get("password") if "password" in exchange else ""),
        "live_trading": bool(exchange.get("live_trading")),
        "sandbox_mode": bool(exchange.get("sandbox_mode")),
        "market_type": exchange.get("market_type") if "market_type" in exchange else settings.exchange.market_type,
        "default_order_type": exchange.get("default_order_type") if "default_order_type" in exchange else settings.exchange.default_order_type,
        "stop_loss_order_type": exchange.get("stop_loss_order_type") if "stop_loss_order_type" in exchange else settings.exchange.stop_loss_order_type,
        "limit_timeout_overrides": normalize_limit_timeout_overrides(
            exchange.get("limit_timeout_overrides")
            if "limit_timeout_overrides" in exchange
            else settings.exchange.limit_timeout_overrides
        ),
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


@router.get("/trading-controls")
async def get_trading_controls(
    user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Expose current trading control state to the dashboard."""
    from core.trading_control import get_trading_control_state

    return await get_trading_control_state(db)


# ─────────────────────────────────────────────
# Positions
# ─────────────────────────────────────────────

@router.get("/positions")
async def get_positions(
    user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Get tracked open positions, scoped to the current user."""
    from exchange import get_open_positions

    filters = [PositionModel.status.in_(["open", "pending"])]
    if not _is_admin(user):
        filters.append(PositionModel.user_id == user.get("sub"))

    result = await db.execute(
        select(PositionModel)
        .where(*filters)
        .order_by(PositionModel.opened_at.desc())
        .limit(200)
    )
    positions = result.scalars().all()
    db_items = [
        {
            "id": p.id,
            "symbol": p.ticker,
            "symbol_short": _simplify_symbol(p.ticker),
            "side": p.direction,
            "contracts": p.remaining_quantity or p.quantity,
            "entryPrice": p.entry_price,
            "entry_price": p.entry_price,
            "markPrice": p.last_price,
            "mark_price": p.last_price,
            "stop_loss": p.stop_loss,
            "liquidationPrice": getattr(p, 'liquidation_price', None),
            "liquidation_price": getattr(p, 'liquidation_price', None),
            "margin": getattr(p, 'margin', None) or (p.entry_price * (p.remaining_quantity or p.quantity) / (p.leverage or 1)),
            "leverage": p.leverage,
            "take_profit_levels": _loads_list(p.take_profit_json),
            "unrealizedPnl": p.unrealized_pnl_usdt or 0 if p.status == "open" else 0,
            "unrealized_pnl": p.unrealized_pnl_usdt or 0 if p.status == "open" else 0,
            "percentage": p.current_pnl_pct if p.status == "open" else None,
            "pnl_pct": p.current_pnl_pct if p.status == "open" else None,
            "mode": "exchange" if p.live_trading else "paper",
            "sandbox_mode": p.sandbox_mode,
            "status": p.status,
            "status_text": _status_text(p.status),
            "opened_at": p.opened_at.isoformat() if p.opened_at else None,
            "updated_at": p.updated_at.isoformat() if p.updated_at else None,
            "source": "db",
        }
        for p in positions
    ]

    exchange_items = []
    exchange_config = await _exchange_config_for_user(db, user)
    if exchange_config.get("live_trading"):
        try:
            exchange_positions = await get_open_positions(exchange_config)

            def _position_key(symbol: str, side: str) -> str:
                return f"{position_symbol_key(symbol)}::{str(side or '').lower().strip()}"

            db_keys = {
                _position_key(str(item.get("symbol") or ""), str(item.get("side") or ""))
                for item in db_items
                if str(item.get("status") or "").lower() == "open"
            }
            for p in exchange_positions:
                symbol = str(p.get("symbol") or "")
                side = str(p.get("side") or "")
                key = _position_key(symbol, side)
                if key in db_keys:
                    continue
                exchange_items.append({
                    "id": f"exchange::{symbol}::{side}",
                    "symbol": symbol,
                    "side": side,
                    "contracts": p.get("contracts"),
                    "entryPrice": p.get("entryPrice"),
                    "entry_price": p.get("entry_price"),
                    "markPrice": p.get("markPrice"),
                    "mark_price": p.get("mark_price"),
                    "liquidationPrice": p.get("liquidationPrice"),
                    "liquidation_price": p.get("liquidation_price"),
                    "unrealizedPnl": p.get("unrealizedPnl") or p.get("unrealized_pnl") or 0,
                    "unrealized_pnl": p.get("unrealized_pnl") or p.get("unrealizedPnl") or 0,
                    "percentage": p.get("percentage"),
                    "leverage": p.get("leverage"),
                    "mode": "exchange",
                    "sandbox_mode": exchange_config.get("sandbox_mode"),
                    "status": "open",
                    "opened_at": None,
                    "updated_at": None,
                    "source": "exchange_live",
                })
        except Exception as exc:
            logger.warning(f"[User] Failed to fetch exchange positions: {exc}")

    return db_items + exchange_items


@router.get("/balance")
async def get_balance(
    user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Get account balance from exchange."""
    from exchange import get_account_balance

    balance = await get_account_balance(await _exchange_config_for_user(db, user))
    return balance


@router.get("/pending-orders")
async def get_pending_orders(
    user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Get open/pending orders from exchange."""
    from exchange import get_open_orders

    exchange_config = await _exchange_config_for_user(db, user)
    if not exchange_config.get("live_trading"):
        return []

    try:
        orders = await get_open_orders(exchange_config=exchange_config)
        return [
            {
                "id": o.get("id"),
                "symbol": o.get("symbol"),
                "side": o.get("side"),
                "type": o.get("type"),
                "price": o.get("price"),
                "amount": o.get("amount"),
                "filled": o.get("filled") or 0,
                "remaining": o.get("remaining") or 0,
                "status": o.get("status"),
                "timestamp": o.get("timestamp"),
                "datetime": o.get("datetime"),
            }
            for o in orders
        ]
    except Exception as e:
        logger.warning(f"[User] Failed to fetch pending orders: {e}")
        return []


@router.post("/cancel-order")
async def cancel_pending_order(
    order_id: str = Query(...),
    symbol: str = Query(...),
    user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Cancel a pending order on exchange."""
    from exchange import cancel_order

    exchange_config = await _exchange_config_for_user(db, user)
    if not exchange_config.get("live_trading"):
        raise HTTPException(400, "Not in live trading mode")

    result = await cancel_order(order_id, symbol, exchange_config)
    if result.get("status") != "cancelled":
        raise HTTPException(400, result.get("reason") or "Failed to cancel order")

    return {"status": "cancelled", "order_id": order_id}


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
    cutoff = utcnow() - timedelta(days=days)
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
    cutoff = utcnow() - timedelta(days=days)
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
            "entry_price": order_details.get("entry_price") or payload.get("entry_price") or signal.get("price"),
            "exit_price": payload.get("exit_price") or order_details.get("exit_price"),
            "stop_loss": analysis.get("suggested_stop_loss") or payload.get("stop_loss"),
            "take_profit": (
                analysis.get("suggested_take_profit")
                or analysis.get("suggested_tp1")
                or payload.get("take_profit")
            ),
            "take_profit_levels": (
                order_details.get("take_profit_orders")
                or payload.get("take_profit_levels")
                or [
                    {
                        "level": idx,
                        "price": analysis.get(f"suggested_tp{idx}"),
                        "qty_pct": analysis.get(f"tp{idx}_qty_pct"),
                    }
                    for idx in range(1, 5)
                    if analysis.get(f"suggested_tp{idx}")
                ]
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


@router.post("/user/trades/sync")
async def sync_offline_trade(
    req: OfflineTradeSyncRequest,
    user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Sync one authenticated user's offline cached trade note from the PWA service worker.

    These rows are scoped to the current user and marked as offline-synced so a
    stale service worker request does not 404 or create live exchange activity.
    """
    db_user = await get_user_by_id(db, user["sub"])
    if not db_user:
        raise HTTPException(404, "User not found")

    trade_id = req.id.strip() or None
    if trade_id:
        existing = await db.get(TradeModel, trade_id)
        if existing and existing.user_id == db_user.id:
            return {"status": "duplicate", "id": existing.id}
        if existing:
            trade_id = None

    payload = {
        **(req.payload or {}),
        "source": "pwa_offline_sync",
        "entry_price": req.entry_price,
        "exit_price": req.exit_price,
        "quantity": req.quantity,
        "pnl_pct": req.pnl_pct,
    }
    trade = TradeModel(
        id=trade_id or str(uuid.uuid4()),
        user_id=db_user.id,
        timestamp=req.timestamp or utcnow(),
        ticker=req.ticker,
        direction=req.direction,
        execute=bool(req.execute),
        order_status=req.order_status or "offline_synced",
        entry_price=req.entry_price,
        exit_price=req.exit_price,
        quantity=req.quantity,
        pnl_pct=req.pnl_pct,
        signal_source="offline",
        payload_json=json.dumps(payload, ensure_ascii=False, default=str),
    )
    db.add(trade)
    await db.flush()
    return {"status": "synced", "id": trade.id}


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
    exchange.setdefault("market_type", settings.exchange.market_type)
    exchange.setdefault("default_order_type", settings.exchange.default_order_type)
    exchange.setdefault("stop_loss_order_type", settings.exchange.stop_loss_order_type)
    exchange.setdefault(
        "limit_timeout_overrides",
        normalize_limit_timeout_overrides(settings.exchange.limit_timeout_overrides),
    )
    exchange["api_configured"] = bool(exchange.get("api_key") and exchange.get("api_secret"))
    exchange.pop("api_key", None)
    exchange.pop("api_secret", None)
    exchange.pop("password", None)

    webhook = response_data.setdefault("webhook", {})
    base_url = public_base_url(request)
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
        await runtime_settings.save_exchange_settings(db, req.model_dump(exclude_unset=True))
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
        await runtime_settings.save_take_profit_settings(db, req.model_dump(exclude_unset=True))
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

    # Validate total TP quantity sum <= 100%
    total_qty = sum([
        req.tp1_qty if req.num_levels >= 1 else 0,
        req.tp2_qty if req.num_levels >= 2 else 0,
        req.tp3_qty if req.num_levels >= 3 else 0,
        req.tp4_qty if req.num_levels >= 4 else 0,
    ])
    if total_qty > 100:
        raise HTTPException(400, f"Total TP close percentage ({total_qty}%) exceeds 100%")

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
    await runtime_settings.save_ai_settings(db, req.model_dump(exclude_unset=True))
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
    await runtime_settings.save_telegram_settings(db, req.model_dump(exclude_unset=True))
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
    await runtime_settings.save_risk_settings(db, req.model_dump(exclude_unset=True))
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
    await runtime_settings.save_trailing_stop_settings(db, req.model_dump(exclude_unset=True))
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
        market_type=req.market_type,
    )

    return result


@router.post("/test-telegram")
async def test_telegram(
    user: dict = Depends(get_current_user),
):
    """Send a test Telegram notification using the current runtime settings."""
    _require_admin(user)
    if not settings.telegram.bot_token or not settings.telegram.chat_id:
        raise HTTPException(400, "Telegram bot token or chat ID is not configured")
    from notifier import send_telegram

    await send_telegram("✅ QuantPilot AI Telegram test message")
    return {"status": "ok"}
