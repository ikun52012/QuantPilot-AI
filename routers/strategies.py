"""
DCA and Grid Strategy API Router.
Provides endpoints for managing automated trading strategies.
"""
import asyncio
import json
from dataclasses import asdict
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from loguru import logger
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.auth import get_current_user
from core.auth import require_admin as get_current_admin
from core.config import settings
from core.database import StrategyStateModel, get_db
from core.utils.datetime import utcnow
from strategies.dca import DCAConfig, DCAEngine, DCAEntry, DCAPosition
from strategies.grid import GridConfig, GridEngine, GridLevel, GridPosition

router = APIRouter(prefix="/api/strategies", tags=["Strategies"])


dca_engine = DCAEngine()
grid_engine = GridEngine()


class DCAConfigRequest(BaseModel):
    ticker: str = Field(default="BTCUSDT", description="Trading pair")
    direction: str = Field(default="long", description="Direction: long or short")
    initial_capital_usdt: float = Field(default=1000.0, ge=50, description="Initial capital")
    max_entries: int = Field(default=5, ge=2, le=10, description="Maximum DCA entries")
    entry_spacing_pct: float = Field(default=2.0, ge=0.5, le=10, description="Entry spacing percentage")
    sizing_method: str = Field(default="fixed", description="Sizing: fixed, martingale, geometric, fibonacci")
    sizing_multiplier: float = Field(default=1.5, ge=1.0, le=3.0, description="Size multiplier for progressive sizing")
    stop_loss_pct: float = Field(default=10.0, ge=0, le=50, description="Stop loss percentage")
    take_profit_pct: float = Field(default=5.0, ge=0, le=30, description="Take profit percentage")
    activation_loss_pct: float = Field(default=1.0, ge=0.5, le=5, description="Loss % to trigger DCA")
    max_total_capital_usdt: float = Field(default=5000.0, description="Maximum total capital")
    mode: str = Field(default="average_down", description="Mode: average_down, average_up")
    leverage: float = Field(default=1.0, ge=1, le=125)
    paper_mode: bool = Field(default=True, description="Paper trading mode")
    auto_start: bool = Field(default=False, description="Auto start on creation")


class GridConfigRequest(BaseModel):
    ticker: str = Field(default="BTCUSDT", description="Trading pair")
    upper_price: float = Field(default=0, ge=0, description="Upper price boundary (0 = auto)")
    lower_price: float = Field(default=0, ge=0, description="Lower price boundary (0 = auto)")
    grid_count: int = Field(default=10, ge=5, le=50, description="Number of grid levels")
    total_capital_usdt: float = Field(default=1000.0, ge=100, description="Total capital")
    quantity_per_grid: float = Field(default=0, ge=0, description="Fixed quantity per grid (0 = auto)")
    grid_spacing_pct: float = Field(default=1.0, ge=0.5, le=5, description="Grid spacing percentage")
    spacing_mode: str = Field(default="arithmetic", description="Spacing: arithmetic or geometric")
    stop_loss_pct: float = Field(default=0, ge=0, description="Stop loss (out of range)")
    leverage: float = Field(default=1.0, ge=1, le=125)
    auto_replenish: bool = Field(default=True, description="Auto extend grid")
    mode: str = Field(default="neutral", description="Grid mode: neutral, long, short")
    paper_mode: bool = Field(default=True)


def _user_id(user: dict) -> str:
    return str(user.get("sub") or user.get("id") or "")


def _json_default(value):
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _loads_dict(raw: str) -> dict:
    try:
        data = json.loads(raw or "{}")
        return data if isinstance(data, dict) else {}
    except (TypeError, json.JSONDecodeError):
        return {}


def _parse_datetime(value):
    if isinstance(value, datetime) or value is None:
        return value
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return utcnow()


def _filter_dataclass(cls, data: dict) -> dict:
    fields = getattr(cls, "__dataclass_fields__", {})
    return {key: value for key, value in data.items() if key in fields}


def _restore_dca(row: StrategyStateModel) -> None:
    if row.id in dca_engine.positions and row.id in dca_engine.configs:
        return

    config_data = _loads_dict(row.config_json)
    state_data = _loads_dict(row.state_json)
    config = DCAConfig(**_filter_dataclass(DCAConfig, config_data))
    entries = []
    for raw_entry in state_data.get("entries", []):
        entry_data = _filter_dataclass(DCAEntry, dict(raw_entry or {}))
        entry_data["entry_time"] = _parse_datetime(entry_data.get("entry_time"))
        entries.append(DCAEntry(**entry_data))

    position_data = _filter_dataclass(DCAPosition, state_data)
    position_data["entries"] = entries
    for key in ("started_at", "updated_at", "closed_at"):
        if key in position_data:
            position_data[key] = _parse_datetime(position_data.get(key))
    dca_engine.configs[row.id] = config
    dca_engine.positions[row.id] = DCAPosition(**position_data)


def _restore_grid(row: StrategyStateModel) -> None:
    if row.id in grid_engine.positions and row.id in grid_engine.configs:
        return

    config_data = _loads_dict(row.config_json)
    state_data = _loads_dict(row.state_json)
    config = GridConfig(**_filter_dataclass(GridConfig, config_data))
    levels = []
    for raw_level in state_data.get("grid_levels", []):
        level_data = _filter_dataclass(GridLevel, dict(raw_level or {}))
        level_data["filled_at"] = _parse_datetime(level_data.get("filled_at"))
        levels.append(GridLevel(**level_data))

    position_data = _filter_dataclass(GridPosition, state_data)
    position_data["grid_levels"] = levels
    for key in ("started_at", "updated_at", "closed_at"):
        if key in position_data:
            position_data[key] = _parse_datetime(position_data.get(key))
    grid_engine.configs[row.id] = config
    grid_engine.positions[row.id] = GridPosition(**position_data)


async def _persist_strategy_state(
    db: AsyncSession,
    user_id: str,
    strategy_type: str,
    strategy_id: str,
    ticker: str,
    config,
    position,
) -> StrategyStateModel:
    row = await db.get(StrategyStateModel, strategy_id)
    if not row:
        row = StrategyStateModel(id=strategy_id, user_id=user_id, strategy_type=strategy_type)
        db.add(row)

    row.user_id = user_id
    row.strategy_type = strategy_type
    row.ticker = ticker
    row.status = getattr(position, "status", "active")
    row.config_json = json.dumps(asdict(config), ensure_ascii=False, default=_json_default)
    row.state_json = json.dumps(asdict(position), ensure_ascii=False, default=_json_default)
    row.updated_at = utcnow()
    await db.flush()
    return row


async def _load_strategy_state(
    db: AsyncSession,
    user_id: str,
    strategy_type: str,
    strategy_id: str,
) -> StrategyStateModel | None:
    row = await db.get(StrategyStateModel, strategy_id)
    if not row or row.strategy_type != strategy_type or row.user_id != user_id:
        return None
    if strategy_type == "dca":
        _restore_dca(row)
    elif strategy_type == "grid":
        _restore_grid(row)
    return row


async def _hydrate_user_strategies(db: AsyncSession, user_id: str, strategy_type: str) -> None:
    result = await db.execute(
        select(StrategyStateModel)
        .where(
            StrategyStateModel.user_id == user_id,
            StrategyStateModel.strategy_type == strategy_type,
            StrategyStateModel.status != "deleted",
        )
    )
    for row in result.scalars().all():
        if strategy_type == "dca":
            _restore_dca(row)
        elif strategy_type == "grid":
            _restore_grid(row)


def _list_dca_for_user(user_id: str) -> list[dict]:
    return [
        dca_engine.get_position_status(strategy_id)
        for strategy_id, config in dca_engine.configs.items()
        if config.user_id == user_id
        and dca_engine.positions.get(strategy_id)
        and dca_engine.positions[strategy_id].status == "active"
    ]


def _list_grid_for_user(user_id: str) -> list[dict]:
    return [
        grid_engine.get_grid_status(strategy_id)
        for strategy_id, config in grid_engine.configs.items()
        if config.user_id == user_id
        and grid_engine.positions.get(strategy_id)
        and grid_engine.positions[strategy_id].status == "active"
    ]


@router.post("/dca/create")
async def create_dca_strategy(
    request: DCAConfigRequest,
    user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Create a new DCA strategy position."""
    try:
        from market_data import fetch_market_context

        context = await fetch_market_context(request.ticker)
        current_price = context.current_price

        if current_price <= 0:
            raise HTTPException(400, f"Cannot get current price for {request.ticker}")

        config = DCAConfig(
            ticker=request.ticker,
            direction=request.direction,
            initial_capital_usdt=request.initial_capital_usdt,
            max_entries=request.max_entries,
            entry_spacing_pct=request.entry_spacing_pct,
            sizing_method=request.sizing_method,
            sizing_multiplier=request.sizing_multiplier,
            stop_loss_pct=request.stop_loss_pct,
            take_profit_pct=request.take_profit_pct,
            activation_loss_pct=request.activation_loss_pct,
            max_total_capital_usdt=request.max_total_capital_usdt,
            mode=request.mode,
            leverage=request.leverage,
            paper_mode=request.paper_mode,
            auto_start=request.auto_start,
            user_id=_user_id(user),
        )

        exchange_config = {
            "live_trading": not request.paper_mode,
            "sandbox_mode": settings.exchange.sandbox_mode,
            "exchange": settings.exchange.name,
            "api_key": settings.exchange.api_key,
            "api_secret": settings.exchange.api_secret,
            "password": settings.exchange.password,
        }

        position = await dca_engine.create_position_async(config, current_price, exchange_config)
        await _persist_strategy_state(db, config.user_id, "dca", position.config_id, position.ticker, config, position)

        return {
            "status": "created",
            "strategy_id": position.config_id,
            "ticker": position.ticker,
            "initial_entry_price": round(position.average_entry_price, 6),
            "initial_quantity": round(position.total_quantity, 6),
            "entries_remaining": position.entries_remaining,
            "stop_loss_price": round(position.stop_loss_price, 6),
            "take_profit_price": round(position.take_profit_price, 6),
            "paper_mode": request.paper_mode,
            "exchange_executed": not request.paper_mode,
        }

    except Exception as err:
        logger.error(f"[DCA/Create] Failed: {err}")
        raise HTTPException(500, f"Failed to create DCA strategy: {err}") from err


@router.get("/dca/status/{strategy_id}")
async def get_dca_status(
    strategy_id: str,
    user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Get DCA strategy status."""
    user_id = _user_id(user)
    row = await _load_strategy_state(db, user_id, "dca", strategy_id)
    if not row and dca_engine.configs.get(strategy_id, DCAConfig()).user_id != user_id:
        raise HTTPException(404, "Position not found")
    status = dca_engine.get_position_status(strategy_id)

    if "error" in status:
        raise HTTPException(404, status["error"])

    return status


@router.get("/dca/list")
async def list_dca_strategies(
    user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """List all active DCA strategies."""
    user_id = _user_id(user)
    await _hydrate_user_strategies(db, user_id, "dca")
    strategies = _list_dca_for_user(user_id)
    return {
        "active_count": len(strategies),
        "strategies": strategies,
    }


@router.post("/dca/check/{strategy_id}")
async def check_dca_strategy(
    strategy_id: str,
    user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Check and execute DCA strategy with current price."""
    try:
        from market_data import fetch_market_context

        row = await _load_strategy_state(db, _user_id(user), "dca", strategy_id)
        if not row:
            raise HTTPException(404, "Position not found")

        status = dca_engine.get_position_status(strategy_id)
        if "error" in status:
            raise HTTPException(404, status["error"])

        ticker = status.get("ticker", "BTCUSDT")
        context = await fetch_market_context(ticker)
        current_price = context.current_price

        config = dca_engine.configs.get(strategy_id)
        exchange_config = None
        if config and not config.paper_mode:
            exchange_config = {
                "live_trading": True,
                "sandbox_mode": settings.exchange.sandbox_mode,
                "exchange": settings.exchange.name,
                "api_key": settings.exchange.api_key,
                "api_secret": settings.exchange.api_secret,
                "password": settings.exchange.password,
            }

        result = await dca_engine.check_and_execute(strategy_id, current_price, exchange_config)
        await _persist_strategy_state(
            db,
            _user_id(user),
            "dca",
            strategy_id,
            ticker,
            dca_engine.configs[strategy_id],
            dca_engine.positions[strategy_id],
        )

        return {
            "strategy_id": strategy_id,
            "current_price": round(current_price, 6),
            **result,
            "updated_status": dca_engine.get_position_status(strategy_id),
        }

    except Exception as err:
        logger.error(f"[DCA/Check] Failed: {err}")
        raise HTTPException(500, f"Failed to check DCA: {err}") from err


@router.delete("/dca/close/{strategy_id}")
async def close_dca_strategy(
    strategy_id: str,
    user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Manually close a DCA strategy."""
    try:
        from market_data import fetch_market_context

        row = await _load_strategy_state(db, _user_id(user), "dca", strategy_id)
        if not row:
            raise HTTPException(404, "Position not found")

        status = dca_engine.get_position_status(strategy_id)
        if "error" in status:
            raise HTTPException(404, status["error"])

        ticker = status.get("ticker", "BTCUSDT")
        context = await fetch_market_context(ticker)
        current_price = context.current_price

        dca_engine.positions[strategy_id].status = "manual_close"
        dca_engine.positions[strategy_id].closed_at = utcnow()
        dca_engine.positions[strategy_id].close_reason = "manual"

        final_status = dca_engine.get_position_status(strategy_id)
        await _persist_strategy_state(
            db,
            _user_id(user),
            "dca",
            strategy_id,
            ticker,
            dca_engine.configs[strategy_id],
            dca_engine.positions[strategy_id],
        )

        return {
            "status": "closed",
            "strategy_id": strategy_id,
            "close_price": round(current_price, 6),
            "final_pnl_usdt": final_status.get("unrealized_pnl_usdt", 0),
            "entries_count": final_status.get("entries_count", 0),
        }

    except Exception as err:
        logger.error(f"[DCA/Close] Failed: {err}")
        raise HTTPException(500, f"Failed to close DCA: {err}") from err


@router.post("/grid/create")
async def create_grid_strategy(
    request: GridConfigRequest,
    user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Create a new grid trading strategy."""
    try:
        from market_data import fetch_market_context

        context = await fetch_market_context(request.ticker)
        current_price = context.current_price

        if current_price <= 0:
            raise HTTPException(400, f"Cannot get current price for {request.ticker}")

        config = GridConfig(
            ticker=request.ticker,
            upper_price=request.upper_price,
            lower_price=request.lower_price,
            grid_count=request.grid_count,
            total_capital_usdt=request.total_capital_usdt,
            quantity_per_grid=request.quantity_per_grid,
            grid_spacing_pct=request.grid_spacing_pct,
            spacing_mode=request.spacing_mode,
            stop_loss_pct=request.stop_loss_pct,
            leverage=request.leverage,
            auto_replenish=request.auto_replenish,
            mode=request.mode,
            paper_mode=request.paper_mode,
            user_id=_user_id(user),
        )

        exchange_config = {
            "live_trading": not request.paper_mode,
            "sandbox_mode": settings.exchange.sandbox_mode,
            "exchange": settings.exchange.name,
            "api_key": settings.exchange.api_key,
            "api_secret": settings.exchange.api_secret,
            "password": settings.exchange.password,
        }

        position = await grid_engine.create_grid_async(config, current_price, exchange_config)
        await _persist_strategy_state(db, config.user_id, "grid", position.config_id, position.ticker, config, position)

        return {
            "status": "created",
            "strategy_id": position.config_id,
            "ticker": position.ticker,
            "upper_price": round(position.upper_price, 6),
            "lower_price": round(position.lower_price, 6),
            "grid_levels": len(position.grid_levels),
            "pending_orders": position.pending_orders,
            "current_price": round(current_price, 6),
            "paper_mode": request.paper_mode,
            "exchange_executed": not request.paper_mode,
        }

    except Exception as err:
        logger.error(f"[Grid/Create] Failed: {err}")
        raise HTTPException(500, f"Failed to create grid strategy: {err}") from err


@router.get("/grid/status/{strategy_id}")
async def get_grid_status(
    strategy_id: str,
    user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Get grid strategy status."""
    user_id = _user_id(user)
    row = await _load_strategy_state(db, user_id, "grid", strategy_id)
    if not row and grid_engine.configs.get(strategy_id, GridConfig()).user_id != user_id:
        raise HTTPException(404, "Position not found")
    status = grid_engine.get_grid_status(strategy_id)

    if "error" in status:
        raise HTTPException(404, status["error"])

    return status


@router.get("/grid/list")
async def list_grid_strategies(
    user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """List all active grid strategies."""
    user_id = _user_id(user)
    await _hydrate_user_strategies(db, user_id, "grid")
    strategies = _list_grid_for_user(user_id)
    return {
        "active_count": len(strategies),
        "strategies": strategies,
    }


@router.post("/grid/check/{strategy_id}")
async def check_grid_strategy(
    strategy_id: str,
    user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Check and execute grid strategy with current price."""
    try:
        from market_data import fetch_market_context

        row = await _load_strategy_state(db, _user_id(user), "grid", strategy_id)
        if not row:
            raise HTTPException(404, "Position not found")

        status = grid_engine.get_grid_status(strategy_id)
        if "error" in status:
            raise HTTPException(404, status["error"])

        ticker = status.get("ticker", "BTCUSDT")
        context = await fetch_market_context(ticker)
        current_price = context.current_price

        config = grid_engine.configs.get(strategy_id)
        exchange_config = None
        if config and not config.paper_mode:
            exchange_config = {
                "live_trading": True,
                "sandbox_mode": settings.exchange.sandbox_mode,
                "exchange": settings.exchange.name,
                "api_key": settings.exchange.api_key,
                "api_secret": settings.exchange.api_secret,
                "password": settings.exchange.password,
            }

        result = await grid_engine.check_and_execute(strategy_id, current_price, exchange_config)
        await _persist_strategy_state(
            db,
            _user_id(user),
            "grid",
            strategy_id,
            ticker,
            grid_engine.configs[strategy_id],
            grid_engine.positions[strategy_id],
        )

        return {
            "strategy_id": strategy_id,
            "current_price": round(current_price, 6),
            **result,
            "updated_status": grid_engine.get_grid_status(strategy_id),
        }

    except Exception as err:
        logger.error(f"[Grid/Check] Failed: {err}")
        raise HTTPException(500, f"Failed to check grid: {err}") from err


@router.delete("/grid/close/{strategy_id}")
async def close_grid_strategy(
    strategy_id: str,
    user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Manually close a grid strategy."""
    try:
        from market_data import fetch_market_context

        row = await _load_strategy_state(db, _user_id(user), "grid", strategy_id)
        if not row:
            raise HTTPException(404, "Position not found")

        status = grid_engine.get_grid_status(strategy_id)
        if "error" in status:
            raise HTTPException(404, status["error"])

        ticker = status.get("ticker", "BTCUSDT")
        context = await fetch_market_context(ticker)
        current_price = context.current_price

        grid_engine.positions[strategy_id].status = "manual_close"
        grid_engine.positions[strategy_id].closed_at = utcnow()
        grid_engine.positions[strategy_id].close_reason = "manual"

        final_status = grid_engine.get_grid_status(strategy_id)
        await _persist_strategy_state(
            db,
            _user_id(user),
            "grid",
            strategy_id,
            ticker,
            grid_engine.configs[strategy_id],
            grid_engine.positions[strategy_id],
        )

        return {
            "status": "closed",
            "strategy_id": strategy_id,
            "close_price": round(current_price, 6),
            "final_pnl_usdt": final_status.get("realized_pnl_usdt", 0),
            "total_trades": final_status.get("total_trades", 0),
        }

    except Exception as err:
        logger.error(f"[Grid/Close] Failed: {err}")
        raise HTTPException(500, f"Failed to close grid: {err}") from err


@router.get("/overview")
async def get_strategies_overview(
    user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Get overview of all active strategies."""
    user_id = _user_id(user)
    await _hydrate_user_strategies(db, user_id, "dca")
    await _hydrate_user_strategies(db, user_id, "grid")
    dca_strategies = _list_dca_for_user(user_id)
    grid_strategies = _list_grid_for_user(user_id)
    return {
        "dca": {
            "active_count": len(dca_strategies),
            "total_pnl_usdt": sum(
                p.get("unrealized_pnl_usdt", 0)
                for p in dca_strategies
            ),
        },
        "grid": {
            "active_count": len(grid_strategies),
            "total_pnl_usdt": sum(
                p.get("realized_pnl_usdt", 0) + p.get("unrealized_pnl_usdt", 0)
                for p in grid_strategies
            ),
        },
    }


@router.get("/history")
async def get_strategy_history(
    strategy_type: str = "all",
    status: str = "all",
    user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """List all strategies including closed/historical ones with full details."""
    user_id = _user_id(user)

    query = select(StrategyStateModel).where(
        StrategyStateModel.user_id == user_id,
    )

    if strategy_type != "all":
        query = query.where(StrategyStateModel.strategy_type == strategy_type)

    if status != "all":
        query = query.where(StrategyStateModel.status == status)
    else:
        query = query.where(StrategyStateModel.status != "deleted")

    query = query.order_by(StrategyStateModel.updated_at.desc())

    result = await db.execute(query)
    rows = result.scalars().all()

    strategies = []
    for row in rows:
        config_data = _loads_dict(row.config_json)
        state_data = _loads_dict(row.state_json)

        strategy_info = {
            "id": row.id,
            "strategy_type": row.strategy_type,
            "ticker": row.ticker,
            "name": row.name or f"{row.strategy_type.upper()}_{row.id[:8]}",
            "status": row.status,
            "created_at": row.created_at.isoformat() if row.created_at else None,
            "updated_at": row.updated_at.isoformat() if row.updated_at else None,
            "config": config_data,
            "state": state_data,
        }

        if row.strategy_type == "dca":
            strategy_info["direction"] = config_data.get("direction", "long")
            strategy_info["entries_count"] = len(state_data.get("entries", []))
            strategy_info["average_entry_price"] = state_data.get("average_entry_price")
            strategy_info["unrealized_pnl_usdt"] = state_data.get("unrealized_pnl_usdt", 0)
            strategy_info["unrealized_pnl_pct"] = state_data.get("unrealized_pnl_pct", 0)
            strategy_info["total_invested_usdt"] = state_data.get("total_invested_usdt")
            strategy_info["stop_loss_price"] = state_data.get("stop_loss_price")
            strategy_info["take_profit_price"] = state_data.get("take_profit_price")
            strategy_info["close_reason"] = state_data.get("close_reason")
            strategy_info["closed_at"] = state_data.get("closed_at")
        elif row.strategy_type == "grid":
            strategy_info["mode"] = config_data.get("mode", "neutral")
            strategy_info["grid_count"] = config_data.get("grid_count", 0)
            strategy_info["upper_price"] = state_data.get("upper_price")
            strategy_info["lower_price"] = state_data.get("lower_price")
            strategy_info["total_trades"] = state_data.get("total_trades", 0)
            strategy_info["realized_pnl_usdt"] = state_data.get("realized_pnl_usdt", 0)
            strategy_info["unrealized_pnl_usdt"] = state_data.get("unrealized_pnl_usdt", 0)
            strategy_info["pending_orders"] = state_data.get("pending_orders", 0)
            strategy_info["close_reason"] = state_data.get("close_reason")
            strategy_info["closed_at"] = state_data.get("closed_at")

        strategies.append(strategy_info)

    return {
        "total_count": len(strategies),
        "strategies": strategies,
    }


@router.get("/detail/{strategy_id}")
async def get_strategy_detail(
    strategy_id: str,
    user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Get full detail of a specific strategy including config and runtime state."""
    user_id = _user_id(user)
    row = await db.get(StrategyStateModel, strategy_id)

    if not row or row.user_id != user_id:
        raise HTTPException(404, "Strategy not found")

    config_data = _loads_dict(row.config_json)
    state_data = _loads_dict(row.state_json)

    detail = {
        "id": row.id,
        "strategy_type": row.strategy_type,
        "ticker": row.ticker,
        "name": row.name or f"{row.strategy_type.upper()}_{row.id[:8]}",
        "status": row.status,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
        "config": config_data,
        "state": state_data,
    }

    if row.strategy_type == "dca":
        if strategy_id in dca_engine.positions:
            live_status = dca_engine.get_position_status(strategy_id)
            detail["live_status"] = live_status
        detail["entries"] = state_data.get("entries", [])
        detail["direction"] = config_data.get("direction", "long")
        detail["sizing_method"] = config_data.get("sizing_method", "fixed")
        detail["max_entries"] = config_data.get("max_entries", 5)
    elif row.strategy_type == "grid":
        if strategy_id in grid_engine.positions:
            live_status = grid_engine.get_grid_status(strategy_id)
            detail["live_status"] = live_status
        detail["grid_levels"] = state_data.get("grid_levels", [])
        detail["mode"] = config_data.get("mode", "neutral")
        detail["spacing_mode"] = config_data.get("spacing_mode", "arithmetic")

    return detail


@router.post("/monitor/start")
async def start_strategy_monitor(
    interval_seconds: int = 60,
    admin: dict = Depends(get_current_admin),
):
    """Start background monitoring for all active strategies."""
    async def _monitor_loop():
        while True:
            try:
                exchange_config = {
                    "live_trading": settings.exchange.live_trading,
                    "sandbox_mode": settings.exchange.sandbox_mode,
                    "exchange": settings.exchange.name,
                    "api_key": settings.exchange.api_key,
                    "api_secret": settings.exchange.api_secret,
                    "password": settings.exchange.password,
                }

                for strategy_id in list(dca_engine.positions.keys()):
                    position = dca_engine.positions.get(strategy_id)
                    config = dca_engine.configs.get(strategy_id)
                    if position and position.status == "active":
                        try:
                            from market_data import fetch_market_context
                            context = await fetch_market_context(position.ticker)
                            strategy_exchange_config = exchange_config if (config and not config.paper_mode) else None
                            await dca_engine.check_and_execute(strategy_id, context.current_price, strategy_exchange_config)
                        except Exception as e:
                            logger.debug(f"[Monitor/DCA] Check failed for {strategy_id}: {e}")

                for strategy_id in list(grid_engine.positions.keys()):
                    position = grid_engine.positions.get(strategy_id)
                    config = grid_engine.configs.get(strategy_id)
                    if position and position.status == "active":
                        try:
                            from market_data import fetch_market_context
                            context = await fetch_market_context(position.ticker)
                            strategy_exchange_config = exchange_config if (config and not config.paper_mode) else None
                            await grid_engine.check_and_execute(strategy_id, context.current_price, strategy_exchange_config)
                        except Exception as e:
                            logger.debug(f"[Monitor/Grid] Check failed for {strategy_id}: {e}")

                await asyncio.sleep(interval_seconds)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[Monitor] Loop error: {e}")
                await asyncio.sleep(interval_seconds * 2)

    if dca_engine._monitor_task or grid_engine._monitor_task:
        return {"status": "already_running"}

    task = asyncio.create_task(_monitor_loop())

    # BUG FIX: Add error callback so unhandled exceptions in the monitor loop
    # are logged instead of silently swallowed.
    def _on_monitor_done(t: asyncio.Task):
        try:
            exc = t.exception()
        except asyncio.CancelledError:
            logger.info("[Monitor] Strategy monitor task cancelled")
            return
        if exc:
            logger.error(f"[Monitor] Strategy monitor task crashed: {exc}")
            # Clear references so the monitor can be restarted
            dca_engine._monitor_task = None
            grid_engine._monitor_task = None

    task.add_done_callback(_on_monitor_done)
    dca_engine._monitor_task = task
    grid_engine._monitor_task = task

    return {
        "status": "started",
        "interval_seconds": interval_seconds,
        "dca_active": len(dca_engine.list_active_positions()),
        "grid_active": len(grid_engine.list_active_grids()),
        "live_trading": settings.exchange.live_trading,
        "sandbox_mode": settings.exchange.sandbox_mode,
    }


@router.post("/monitor/stop")
async def stop_strategy_monitor(
    admin: dict = Depends(get_current_admin),
):
    """Stop background strategy monitoring."""
    if dca_engine._monitor_task:
        dca_engine._monitor_task.cancel()
        dca_engine._monitor_task = None

    if grid_engine._monitor_task:
        grid_engine._monitor_task.cancel()
        grid_engine._monitor_task = None

    return {"status": "stopped"}


AI_DCA_CONFIG_PROMPT = """You are an expert cryptocurrency quantitative trading analyst specializing in DCA (Dollar Cost Average) strategy design.

Based on the current market conditions for the given ticker, generate optimal DCA configuration parameters.

Your analysis process:
1. Analyze current price volatility and trend direction
2. Assess appropriate entry spacing based on ATR and volatility
3. Determine optimal stop-loss distance based on support/resistance levels
4. Calculate take-profit targets based on expected price movement
5. Decide on DCA mode (average_down for bearish, average_up for bullish)
6. Determine max entries based on volatility and risk tolerance
7. Set activation loss threshold based on price swings

You MUST respond in valid JSON format with these exact fields:
{
    "direction": "long" | "short",
    "initial_capital_usdt": float (50-2000),
    "max_entries": int (2-10),
    "entry_spacing_pct": float (0.5-10.0),
    "sizing_method": "fixed" | "martingale" | "geometric" | "fibonacci",
    "sizing_multiplier": float (1.0-3.0),
    "stop_loss_pct": float (0-50),
    "take_profit_pct": float (0-30),
    "activation_loss_pct": float (0.5-5.0),
    "max_total_capital_usdt": float (500-20000),
    "mode": "average_down" | "average_up",
    "leverage": float (1-10),
    "reasoning": "Brief explanation of why these parameters suit current market conditions"
}

Key rules:
- High volatility (>5% 24h change): wider spacing (2-5%), fewer entries (3-5), larger stop-loss (15-25%)
- Low volatility (<2% 24h change): tighter spacing (1-2%), more entries (5-8), smaller stop-loss (5-10%)
- Trending up: prefer "long" direction, "average_up" mode, smaller activation loss
- Trending down: prefer "short" direction, "average_down" mode, larger activation loss
- Ranging market: prefer "long" direction, "average_down" mode, moderate parameters
- Extreme RSI (>75 or <25): reduce position size, increase stop-loss buffer
- High funding rate (>0.05%): cautious sizing, consider counter-trend

Respond ONLY with the JSON object, no other text."""


AI_GRID_CONFIG_PROMPT = """You are an expert cryptocurrency quantitative trading analyst specializing in Grid Trading strategy design.

Based on the current market conditions for the given ticker, generate optimal Grid configuration parameters.

Your analysis process:
1. Analyze current price volatility and trading range
2. Determine appropriate grid range based on support/resistance levels
3. Calculate optimal grid count based on volatility and capital
4. Choose spacing mode based on price distribution
5. Decide on grid mode based on market bias
6. Set grid spacing percentage based on ATR

You MUST respond in valid JSON format with these exact fields:
{
    "upper_price": float (current_price * 1.02 to 1.20),
    "lower_price": float (current_price * 0.80 to 0.98),
    "grid_count": int (5-50),
    "total_capital_usdt": float (100-5000),
    "grid_spacing_pct": float (0.5-5.0),
    "spacing_mode": "arithmetic" | "geometric",
    "mode": "neutral" | "long" | "short",
    "stop_loss_pct": float (0-20),
    "leverage": float (1-10),
    "reasoning": "Brief explanation of why these parameters suit current market conditions"
}

Key rules:
- High volatility (>5% 24h change): wider range (±10-15%), more grids (20-40), larger spacing (1.5-3%)
- Low volatility (<2% 24h change): tighter range (±3-5%), fewer grids (10-20), smaller spacing (0.5-1%)
- Trending up: prefer "long" mode, asymmetric range (higher upper bound)
- Trending down: prefer "short" mode, asymmetric range (lower lower bound)
- Ranging market: prefer "neutral" mode, symmetric range around current price
- High volume: can use more grids with smaller spacing
- Low volume: use fewer grids with larger spacing to avoid partial fills

Respond ONLY with the JSON object, no other text."""


class AIGenerateRequest(BaseModel):
    ticker: str = Field(default="BTCUSDT", description="Trading pair")
    strategy_type: str = Field(default="dca", description="Strategy type: dca or grid")
    risk_level: str = Field(default="medium", description="Risk level: low, medium, high")


@router.post("/ai/generate")
async def ai_generate_strategy_config(
    request: AIGenerateRequest,
    user: dict = Depends(get_current_user),
):
    """AI generates optimal DCA or Grid configuration based on current market conditions."""
    try:
        from ai_analyzer import (
            _call_anthropic,
            _call_custom,
            _call_deepseek,
            _call_mistral,
            _call_openai,
            _call_openrouter,
        )
        from market_data import fetch_market_context

        context = await fetch_market_context(request.ticker)
        current_price = context.current_price

        if current_price <= 0:
            raise HTTPException(400, f"Cannot get current price for {request.ticker}")

        risk_adjustment = {
            "low": {"capital_mult": 0.5, "sl_mult": 0.8, "entries_mult": 0.8},
            "medium": {"capital_mult": 1.0, "sl_mult": 1.0, "entries_mult": 1.0},
            "high": {"capital_mult": 1.5, "sl_mult": 1.2, "entries_mult": 1.2},
        }
        risk = risk_adjustment.get(request.risk_level, risk_adjustment["medium"])

        if request.strategy_type == "dca":
            system_prompt = AI_DCA_CONFIG_PROMPT
            user_prompt = f"""Generate DCA configuration for:

Ticker: {request.ticker}
Current Price: {current_price}
24h Price Change: {context.price_change_24h:+.4f}%
4h Price Change: {context.price_change_4h:+.4f}%
1h Price Change: {context.price_change_1h:+.4f}%
24h Volume: ${context.volume_24h:,.0f}
24h High: {context.high_24h}
24h Low: {context.low_24h}
RSI (1h): {context.rsi_1h if context.rsi_1h is not None else 'N/A'}
ATR%: {context.atr_pct if context.atr_pct is not None else 'N/A'}%
EMA Fast: {context.ema_fast if context.ema_fast is not None else 'N/A'}
EMA Slow: {context.ema_slow if context.ema_slow is not None else 'N/A'}
Funding Rate: {context.funding_rate if context.funding_rate is not None else 'N/A'}
Orderbook Imbalance: {context.orderbook_imbalance if context.orderbook_imbalance is not None else 'N/A'}

Risk Level: {request.risk_level}

Generate optimal DCA parameters for this market condition."""
        else:
            system_prompt = AI_GRID_CONFIG_PROMPT
            user_prompt = f"""Generate Grid Trading configuration for:

Ticker: {request.ticker}
Current Price: {current_price}
24h Price Change: {context.price_change_24h:+.4f}%
4h Price Change: {context.price_change_4h:+.4f}%
1h Price Change: {context.price_change_1h:+.4f}%
24h Volume: ${context.volume_24h:,.0f}
24h High: {context.high_24h}
24h Low: {context.low_24h}
RSI (1h): {context.rsi_1h if context.rsi_1h is not None else 'N/A'}
ATR%: {context.atr_pct if context.atr_pct is not None else 'N/A'}%
EMA Fast: {context.ema_fast if context.ema_fast is not None else 'N/A'}
EMA Slow: {context.ema_slow if context.ema_slow is not None else 'N/A'}
Funding Rate: {context.funding_rate if context.funding_rate is not None else 'N/A'}
Orderbook Imbalance: {context.orderbook_imbalance if context.orderbook_imbalance is not None else 'N/A'}

Risk Level: {request.risk_level}

Generate optimal Grid Trading parameters for this market condition."""

        provider = settings.ai.provider.lower()
        logger.info(f"[AI/Generate] Generating {request.strategy_type} config for {request.ticker} via {provider}")

        if provider == "openai":
            ai_response = await _call_openai(system_prompt, user_prompt)
        elif provider == "anthropic":
            ai_response = await _call_anthropic(system_prompt, user_prompt)
        elif provider == "deepseek":
            ai_response = await _call_deepseek(system_prompt, user_prompt)
        elif provider == "mistral":
            ai_response = await _call_mistral(system_prompt, user_prompt)
        elif provider == "openrouter":
            ai_response = await _call_openrouter(system_prompt, user_prompt)
        elif settings.ai.custom_provider_enabled and provider in {"custom", settings.ai.custom_provider_name.lower()}:
            ai_response = await _call_custom(system_prompt, user_prompt)
        else:
            raise HTTPException(400, f"AI provider '{provider}' is not configured or not supported")

        if not ai_response:
            raise HTTPException(500, f"AI provider '{provider}' returned empty response. Check API key and model configuration.")

        config_start = ai_response.find("{")
        config_end = ai_response.rfind("}") + 1
        if config_start == -1 or config_end == 0:
            raise HTTPException(500, "AI response did not contain valid JSON")

        config_json_str = ai_response[config_start:config_end]
        ai_config = json.loads(config_json_str)

        if request.strategy_type == "dca":
            ai_config["initial_capital_usdt"] = ai_config.get("initial_capital_usdt", 1000) * risk["capital_mult"]
            ai_config["max_total_capital_usdt"] = ai_config.get("max_total_capital_usdt", 5000) * risk["capital_mult"]
            ai_config["stop_loss_pct"] = ai_config.get("stop_loss_pct", 10) * risk["sl_mult"]
            ai_config["max_entries"] = int(ai_config.get("max_entries", 5) * risk["entries_mult"])
            ai_config["ticker"] = request.ticker
            ai_config["paper_mode"] = True
        else:
            ai_config["total_capital_usdt"] = ai_config.get("total_capital_usdt", 1000) * risk["capital_mult"]
            ai_config["stop_loss_pct"] = ai_config.get("stop_loss_pct", 5) * risk["sl_mult"]
            ai_config["grid_count"] = int(ai_config.get("grid_count", 15) * risk["entries_mult"])
            ai_config["ticker"] = request.ticker
            ai_config["paper_mode"] = True

        logger.info(f"[AI/Generate] Generated {request.strategy_type} config for {request.ticker}: {ai_config.get('reasoning', '')}")

        return {
            "status": "generated",
            "strategy_type": request.strategy_type,
            "ticker": request.ticker,
            "current_price": round(current_price, 6),
            "config": ai_config,
            "market_context": {
                "price_change_24h": round(context.price_change_24h, 4),
                "price_change_4h": round(context.price_change_4h, 4),
                "price_change_1h": round(context.price_change_1h, 4),
                "volume_24h": context.volume_24h,
                "rsi_1h": context.rsi_1h,
                "atr_pct": context.atr_pct,
                "funding_rate": context.funding_rate,
            },
        }

    except json.JSONDecodeError as err:
        logger.error(f"[AI/Generate] JSON parse error: {err}")
        raise HTTPException(500, f"AI response parsing failed: {err}") from err
    except Exception as err:
        logger.error(f"[AI/Generate] Failed: {err}")
        raise HTTPException(500, f"Failed to generate strategy config: {err}") from err
