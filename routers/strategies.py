"""
DCA and Grid Strategy API Router.
Provides endpoints for managing automated trading strategies.
"""
import asyncio
import json
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, HTTPException, Depends, BackgroundTasks
from pydantic import BaseModel, Field
from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.auth import get_current_user, require_admin as get_current_admin
from core.database import get_db, StrategyStateModel
from strategies.dca import DCAEngine, DCAConfig, DCAEntry, DCAPosition
from strategies.grid import GridEngine, GridConfig, GridLevel, GridPosition


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
        return datetime.now(timezone.utc)


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
    row.updated_at = datetime.now(timezone.utc)
    await db.flush()
    return row


async def _load_strategy_state(
    db: AsyncSession,
    user_id: str,
    strategy_type: str,
    strategy_id: str,
) -> Optional[StrategyStateModel]:
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

        position = dca_engine.create_position(config, current_price)
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
        }

    except Exception as e:
        logger.error(f"[DCA/Create] Failed: {e}")
        raise HTTPException(500, f"Failed to create DCA strategy: {str(e)}")


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

        result = await dca_engine.check_and_execute(strategy_id, current_price)
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

    except Exception as e:
        logger.error(f"[DCA/Check] Failed: {e}")
        raise HTTPException(500, f"Failed to check DCA: {str(e)}")


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
        dca_engine.positions[strategy_id].closed_at = datetime.now(timezone.utc)
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

    except Exception as e:
        logger.error(f"[DCA/Close] Failed: {e}")
        raise HTTPException(500, f"Failed to close DCA: {str(e)}")


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

        position = grid_engine.create_grid(config, current_price)
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
        }

    except Exception as e:
        logger.error(f"[Grid/Create] Failed: {e}")
        raise HTTPException(500, f"Failed to create grid strategy: {str(e)}")


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

        result = await grid_engine.check_and_execute(strategy_id, current_price)
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

    except Exception as e:
        logger.error(f"[Grid/Check] Failed: {e}")
        raise HTTPException(500, f"Failed to check grid: {str(e)}")


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
        grid_engine.positions[strategy_id].closed_at = datetime.now(timezone.utc)
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

    except Exception as e:
        logger.error(f"[Grid/Close] Failed: {e}")
        raise HTTPException(500, f"Failed to close grid: {str(e)}")


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


@router.post("/monitor/start")
async def start_strategy_monitor(
    interval_seconds: int = 60,
    admin: dict = Depends(get_current_admin),
):
    """Start background monitoring for all active strategies."""
    async def _monitor_loop():
        while True:
            try:
                for strategy_id in list(dca_engine.positions.keys()):
                    position = dca_engine.positions.get(strategy_id)
                    if position and position.status == "active":
                        try:
                            from market_data import fetch_market_context
                            context = await fetch_market_context(position.ticker)
                            await dca_engine.check_and_execute(strategy_id, context.current_price)
                        except Exception as e:
                            logger.debug(f"[Monitor/DCA] Check failed for {strategy_id}: {e}")

                for strategy_id in list(grid_engine.positions.keys()):
                    position = grid_engine.positions.get(strategy_id)
                    if position and position.status == "active":
                        try:
                            from market_data import fetch_market_context
                            context = await fetch_market_context(position.ticker)
                            await grid_engine.check_and_execute(strategy_id, context.current_price)
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

    dca_engine._monitor_task = asyncio.create_task(_monitor_loop())
    grid_engine._monitor_task = dca_engine._monitor_task

    return {
        "status": "started",
        "interval_seconds": interval_seconds,
        "dca_active": len(dca_engine.list_active_positions()),
        "grid_active": len(grid_engine.list_active_grids()),
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
