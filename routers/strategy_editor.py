"""
Strategy Editor Router - Visual strategy configuration.
Provides drag-and-drop style strategy editing interface.
"""
import json
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel, Field
from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.auth import get_current_user
from core.database import get_db, StrategyStateModel


router = APIRouter(prefix="/api/strategy-editor", tags=["Strategy Editor"])


class StrategyTemplate(BaseModel):
    name: str
    category: str
    description: str
    config: dict
    is_public: bool = False


class StrategyConfig(BaseModel):
    strategy_id: str = ""
    name: str
    ticker: str = "BTCUSDT"
    direction: str = "long"
    entry_conditions: list[dict] = []
    exit_conditions: list[dict] = []
    risk_management: dict = {}
    tp_levels: list[dict] = []
    trailing_stop: dict = {}


_STRATEGY_TEMPLATES = [
    {
        "id": "ema_cross",
        "name": "EMA Cross",
        "category": "trend",
        "description": "EMA crossover strategy",
        "config": {
            "entry_conditions": [
                {"type": "ema_cross", "fast_period": 12, "slow_period": 26, "direction": "above"},
            ],
            "exit_conditions": [
                {"type": "ema_cross", "fast_period": 12, "slow_period": 26, "direction": "below"},
            ],
            "risk_management": {
                "stop_loss_pct": 2.0,
                "take_profit_pct": 4.0,
                "position_size_pct": 10.0,
            },
        },
    },
    {
        "id": "rsi_reversal",
        "name": "RSI Reversal",
        "category": "reversal",
        "description": "RSI overbought/oversold reversal",
        "config": {
            "entry_conditions": [
                {"type": "rsi", "threshold": 30, "direction": "oversold"},
            ],
            "exit_conditions": [
                {"type": "rsi", "threshold": 70, "direction": "overbought"},
            ],
            "risk_management": {
                "stop_loss_pct": 1.5,
                "take_profit_pct": 3.0,
            },
        },
    },
    {
        "id": "smc_fvg",
        "name": "SMC FVG",
        "category": "smart_money",
        "description": "Fair Value Gap detection",
        "config": {
            "entry_conditions": [
                {"type": "fvg", "lookback": 5, "min_gap_pct": 0.5},
            ],
            "exit_conditions": [
                {"type": "order_block", "threshold": 0.5},
            ],
            "risk_management": {
                "stop_loss_pct": 2.0,
                "take_profit_pct": 6.0,
            },
        },
    },
    {
        "id": "multi_tp",
        "name": "Multi-TP Strategy",
        "category": "risk_management",
        "description": "Multiple take profit levels",
        "config": {
            "entry_conditions": [],
            "tp_levels": [
                {"price_pct": 2.0, "qty_pct": 25},
                {"price_pct": 4.0, "qty_pct": 25},
                {"price_pct": 6.0, "qty_pct": 25},
                {"price_pct": 8.0, "qty_pct": 25},
            ],
            "trailing_stop": {
                "mode": "breakeven_on_tp1",
            },
        },
    },
    {
        "id": "dca_basic",
        "name": "DCA Basic",
        "category": "dca",
        "description": "Dollar Cost Average strategy",
        "config": {
            "entry_conditions": [],
            "dca_config": {
                "max_entries": 5,
                "entry_spacing_pct": 2.0,
                "sizing_method": "fixed",
            },
        },
    },
    {
        "id": "grid_neutral",
        "name": "Neutral Grid",
        "category": "grid",
        "description": "Neutral grid trading",
        "config": {
            "entry_conditions": [],
            "grid_config": {
                "grid_count": 10,
                "grid_spacing_pct": 1.0,
                "mode": "neutral",
            },
        },
    },
]

_USER_STRATEGIES = {}


def _user_id(user: dict) -> str:
    return str(user.get("sub") or user.get("id") or "")


def _loads_dict(raw: str) -> dict:
    try:
        data = json.loads(raw or "{}")
        return data if isinstance(data, dict) else {}
    except (TypeError, json.JSONDecodeError):
        return {}


def _strategy_payload(strategy_id: str, config: StrategyConfig, user_id: str, existing: Optional[dict] = None) -> dict:
    existing = existing or {}
    return {
        "strategy_id": strategy_id,
        "name": config.name,
        "ticker": config.ticker,
        "direction": config.direction,
        "entry_conditions": config.entry_conditions,
        "exit_conditions": config.exit_conditions,
        "risk_management": config.risk_management,
        "tp_levels": config.tp_levels,
        "trailing_stop": config.trailing_stop,
        "user_id": user_id,
        "created_at": existing.get("created_at") or datetime.now(timezone.utc).isoformat(),
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "is_active": bool(existing.get("is_active", False)),
    }


def _row_to_strategy(row: StrategyStateModel) -> dict:
    strategy = _loads_dict(row.state_json)
    strategy.setdefault("strategy_id", row.id)
    strategy.setdefault("name", row.name)
    strategy.setdefault("ticker", row.ticker)
    strategy.setdefault("user_id", row.user_id)
    strategy.setdefault("is_active", row.status == "active")
    return strategy


async def _get_strategy_row(db: AsyncSession, strategy_id: str, user_id: str) -> StrategyStateModel:
    row = await db.get(StrategyStateModel, strategy_id)
    if not row or row.strategy_type != "custom" or row.user_id != user_id or row.status == "deleted":
        raise HTTPException(404, f"Strategy {strategy_id} not found")
    return row


async def _save_strategy_row(
    db: AsyncSession,
    strategy_id: str,
    user_id: str,
    payload: dict,
    status: str = "draft",
) -> StrategyStateModel:
    row = await db.get(StrategyStateModel, strategy_id)
    if not row:
        row = StrategyStateModel(id=strategy_id, user_id=user_id, strategy_type="custom")
        db.add(row)
    row.user_id = user_id
    row.strategy_type = "custom"
    row.ticker = payload.get("ticker", "")
    row.name = payload.get("name", "")
    row.status = status
    row.config_json = json.dumps({
        "entry_conditions": payload.get("entry_conditions", []),
        "exit_conditions": payload.get("exit_conditions", []),
        "risk_management": payload.get("risk_management", {}),
        "tp_levels": payload.get("tp_levels", []),
        "trailing_stop": payload.get("trailing_stop", {}),
    }, ensure_ascii=False, default=str)
    row.state_json = json.dumps(payload, ensure_ascii=False, default=str)
    row.updated_at = datetime.now(timezone.utc)
    await db.flush()
    return row


@router.get("/templates")
async def list_strategy_templates(
    category: Optional[str] = None,
    user: dict = Depends(get_current_user),
):
    """List available strategy templates."""
    templates = _STRATEGY_TEMPLATES

    if category:
        templates = [t for t in templates if t.get("category") == category]

    return {
        "templates": templates,
        "categories": ["trend", "reversal", "smart_money", "risk_management", "dca", "grid"],
    }


@router.get("/template/{template_id}")
async def get_strategy_template(
    template_id: str,
    user: dict = Depends(get_current_user),
):
    """Get a specific strategy template."""
    template = next((t for t in _STRATEGY_TEMPLATES if t.get("id") == template_id), None)

    if not template:
        raise HTTPException(404, f"Template {template_id} not found")

    return template


@router.post("/create")
async def create_custom_strategy(
    config: StrategyConfig,
    user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Create a custom strategy configuration."""
    user_id = _user_id(user)
    strategy_id = config.strategy_id or f"custom_{user_id}_{datetime.now().strftime('%Y%m%d%H%M%S%f')}"
    strategy = _strategy_payload(strategy_id, config, user_id)

    _USER_STRATEGIES[strategy_id] = strategy
    await _save_strategy_row(db, strategy_id, user_id, strategy, status="draft")

    return {
        "status": "created",
        "strategy_id": strategy_id,
        "name": config.name,
    }


@router.get("/list")
async def list_user_strategies(
    user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """List user's saved strategies."""
    user_id = _user_id(user)
    result = await db.execute(
        select(StrategyStateModel)
        .where(
            StrategyStateModel.user_id == user_id,
            StrategyStateModel.strategy_type == "custom",
            StrategyStateModel.status != "deleted",
        )
        .order_by(StrategyStateModel.updated_at.desc())
    )
    user_strategies = [_row_to_strategy(row) for row in result.scalars().all()]

    return {
        "strategies": user_strategies,
        "count": len(user_strategies),
    }


@router.get("/{strategy_id}")
async def get_strategy_config(
    strategy_id: str,
    user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Get a saved strategy configuration."""
    row = await _get_strategy_row(db, strategy_id, _user_id(user))
    return _row_to_strategy(row)


@router.put("/{strategy_id}")
async def update_strategy_config(
    strategy_id: str,
    config: StrategyConfig,
    user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Update a saved strategy."""
    user_id = _user_id(user)
    row = await _get_strategy_row(db, strategy_id, user_id)
    existing = _row_to_strategy(row)
    strategy = _strategy_payload(strategy_id, config, user_id, existing)
    strategy["is_active"] = existing.get("is_active", False)
    _USER_STRATEGIES[strategy_id] = strategy
    await _save_strategy_row(db, strategy_id, user_id, strategy, status=row.status or "draft")

    return {"status": "updated", "strategy_id": strategy_id}


@router.delete("/{strategy_id}")
async def delete_strategy(
    strategy_id: str,
    user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Delete a saved strategy."""
    row = await _get_strategy_row(db, strategy_id, _user_id(user))
    row.status = "deleted"
    row.updated_at = datetime.now(timezone.utc)
    _USER_STRATEGIES.pop(strategy_id, None)

    return {"status": "deleted", "strategy_id": strategy_id}


@router.post("/{strategy_id}/activate")
async def activate_strategy(
    strategy_id: str,
    user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Activate a strategy for live execution."""
    row = await _get_strategy_row(db, strategy_id, _user_id(user))
    strategy = _row_to_strategy(row)
    strategy["is_active"] = True
    strategy["activated_at"] = datetime.now(timezone.utc).isoformat()
    _USER_STRATEGIES[strategy_id] = strategy
    await _save_strategy_row(db, strategy_id, _user_id(user), strategy, status="active")

    logger.info(f"[StrategyEditor] Strategy {strategy_id} activated by user {_user_id(user)}")

    return {
        "status": "activated",
        "strategy_id": strategy_id,
        "message": f"Strategy '{strategy.get('name')}' is now active",
    }


@router.post("/{strategy_id}/deactivate")
async def deactivate_strategy(
    strategy_id: str,
    user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Deactivate a strategy."""
    row = await _get_strategy_row(db, strategy_id, _user_id(user))
    strategy = _row_to_strategy(row)
    strategy["is_active"] = False
    strategy["deactivated_at"] = datetime.now(timezone.utc).isoformat()
    _USER_STRATEGIES[strategy_id] = strategy
    await _save_strategy_row(db, strategy_id, _user_id(user), strategy, status="draft")

    return {"status": "deactivated", "strategy_id": strategy_id}


@router.get("/indicators/list")
async def list_available_indicators(
    user: dict = Depends(get_current_user),
):
    """List available indicators for strategy building."""
    return {
        "indicators": [
            {"id": "ema", "name": "EMA", "params": ["period", "source"]},
            {"id": "rsi", "name": "RSI", "params": ["period", "threshold"]},
            {"id": "macd", "name": "MACD", "params": ["fast", "slow", "signal"]},
            {"id": "bb", "name": "Bollinger Bands", "params": ["period", "std_dev"]},
            {"id": "fvg", "name": "Fair Value Gap", "params": ["lookback", "min_gap_pct"]},
            {"id": "order_block", "name": "Order Block", "params": ["threshold"]},
            {"id": "volume", "name": "Volume", "params": ["threshold", "multiplier"]},
            {"id": "atr", "name": "ATR", "params": ["period"]},
            {"id": "support", "name": "Support Level", "params": ["touch_count"]},
            {"id": "resistance", "name": "Resistance Level", "params": ["touch_count"]},
        ],
    }


@router.get("/export/{strategy_id}")
async def export_strategy(
    strategy_id: str,
    user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Export strategy as JSON file."""
    row = await _get_strategy_row(db, strategy_id, _user_id(user))
    strategy = _row_to_strategy(row)

    return {
        "format": "json",
        "content": json.dumps(strategy, indent=2),
        "filename": f"{strategy.get('name', 'strategy')}.json",
    }


@router.post("/import")
async def import_strategy(
    strategy_json: str,
    user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Import strategy from JSON."""
    try:
        strategy = json.loads(strategy_json)

        user_id = _user_id(user)
        strategy_id = f"imported_{user_id}_{datetime.now().strftime('%Y%m%d%H%M%S%f')}"
        strategy["strategy_id"] = strategy_id
        strategy["user_id"] = user_id
        strategy["imported_at"] = datetime.now(timezone.utc).isoformat()
        strategy["is_active"] = bool(strategy.get("is_active", False))

        _USER_STRATEGIES[strategy_id] = strategy
        await _save_strategy_row(
            db,
            strategy_id,
            user_id,
            strategy,
            status="active" if strategy.get("is_active") else "draft",
        )

        return {
            "status": "imported",
            "strategy_id": strategy_id,
            "name": strategy.get("name"),
        }

    except json.JSONDecodeError:
        raise HTTPException(400, "Invalid JSON format")
