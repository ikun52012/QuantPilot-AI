"""
Backtest API Router.
Provides endpoints for running backtests and retrieving results.
"""
import asyncio
import json
from datetime import datetime, timezone, timedelta
from typing import Optional

from fastapi import APIRouter, HTTPException, Depends, BackgroundTasks
from pydantic import BaseModel, Field

from loguru import logger

from backtest.engine import BacktestEngine, BacktestConfig
from backtest.strategies import SMCTrendStrategy, AIAssistantStrategy, SimpleTrendFollowStrategy
from core.auth import require_admin as get_current_admin
from market_data import fetch_ohlcv_history


router = APIRouter(prefix="/api/backtest", tags=["Backtest"])


class BacktestRequest(BaseModel):
    ticker: str = Field(default="BTCUSDT", description="Trading pair symbol")
    timeframe: str = Field(default="1h", description="Timeframe: 1m, 5m, 15m, 1h, 4h, 1d")
    days: int = Field(default=30, ge=7, le=365, description="Historical days to backtest")
    strategy: str = Field(default="simple_trend", description="Strategy: simple_trend, smc_trend, ai_assistant")
    initial_capital: float = Field(default=10000.0, ge=100, description="Starting capital in USDT")
    position_size_pct: float = Field(default=10.0, ge=1, le=100, description="Position size as % of capital")
    leverage: float = Field(default=1.0, ge=1, le=125, description="Leverage multiplier")
    fee_pct: float = Field(default=0.04, description="Trading fee percentage")
    slippage_pct: float = Field(default=0.01, description="Slippage percentage")
    stop_loss_pct: float = Field(default=2.0, ge=0, description="Stop loss percentage")
    trailing_mode: str = Field(default="none", description="Trailing stop mode: none, moving, breakeven_on_tp1, step_trailing, profit_pct_trailing")
    trailing_pct: float = Field(default=1.5, description="Trailing percentage")
    trailing_activation_pct: float = Field(default=0.5, description="Activation profit % for trailing")
    multi_tp_enabled: bool = Field(default=False, description="Enable multiple take profit levels")
    tp_levels: list[dict] = Field(default=[{"price_pct": 3.0, "qty_pct": 100}], description="TP level configurations")
    max_positions: int = Field(default=3, ge=1, le=10, description="Maximum concurrent positions")
    max_daily_loss_pct: float = Field(default=5.0, description="Max daily loss percentage")
    max_drawdown_pct: float = Field(default=20.0, description="Max drawdown percentage")
    strategy_params: dict = Field(default={}, description="Strategy-specific parameters")


class BacktestResultResponse(BaseModel):
    status: str
    trades: list[dict]
    equity_curve: list[dict]
    metrics: dict
    config: dict
    signals: dict
    execution_time_ms: float


_backtest_results_cache: dict[str, dict] = {}
_backtest_tasks: dict[str, asyncio.Task] = {}
_BACKTEST_CACHE_TTL = 3600  # 1 hour TTL
_BACKTEST_CACHE_MAX_SIZE = 100  # Max 100 cached results


def _cleanup_backtest_cache():
    """Clean up expired and oversized backtest cache."""
    now = datetime.now(timezone.utc)
    expired_keys = []

    for task_id, cached in _backtest_results_cache.items():
        completed_at = cached.get("completed_at")
        if completed_at:
            completed_time = datetime.fromisoformat(completed_at.replace("Z", "+00:00"))
            if (now - completed_time).total_seconds() > _BACKTEST_CACHE_TTL:
                expired_keys.append(task_id)

    for key in expired_keys:
        del _backtest_results_cache[key]
        if key in _backtest_tasks:
            del _backtest_tasks[key]

    # Enforce max size
    if len(_backtest_results_cache) > _BACKTEST_CACHE_MAX_SIZE:
        sorted_keys = sorted(
            _backtest_results_cache.keys(),
            key=lambda k: _backtest_results_cache[k].get("completed_at", ""),
        )
        for key in sorted_keys[:len(_backtest_results_cache) - _BACKTEST_CACHE_MAX_SIZE]:
            del _backtest_results_cache[key]
            if key in _backtest_tasks:
                del _backtest_tasks[key]


@router.post("/run", response_model=BacktestResultResponse)
async def run_backtest(
    request: BacktestRequest,
    admin: dict = Depends(get_current_admin),
):
    """Run a backtest on historical data with specified strategy."""
    start_time = datetime.now(timezone.utc)

    try:
        ohlcv_data = await fetch_ohlcv_history(
            ticker=request.ticker,
            timeframe=request.timeframe,
            days=request.days,
        )

        if not ohlcv_data:
            raise HTTPException(400, f"No historical data available for {request.ticker}")

        logger.info(f"[Backtest] Loaded {len(ohlcv_data)} bars for {request.ticker}")

        strategy = _get_strategy(request.strategy, {**request.strategy_params, "ticker": request.ticker})
        if not strategy:
            raise HTTPException(400, f"Unknown strategy: {request.strategy}")

        config = BacktestConfig(
            initial_capital=request.initial_capital,
            position_size_pct=request.position_size_pct,
            leverage=request.leverage,
            fee_pct=request.fee_pct,
            slippage_pct=request.slippage_pct,
            stop_loss_pct=request.stop_loss_pct,
            trailing_mode=request.trailing_mode,
            trailing_pct=request.trailing_pct,
            trailing_activation_pct=request.trailing_activation_pct,
            multi_tp_enabled=request.multi_tp_enabled,
            tp_levels=request.tp_levels,
            max_positions=request.max_positions,
            max_daily_loss_pct=request.max_daily_loss_pct,
            max_drawdown_pct=request.max_drawdown_pct,
            strategy_name=request.strategy,
            timeframe=request.timeframe,
        )

        engine = BacktestEngine(config, strategy)
        engine.load_data(ohlcv_data)

        result = engine.run()

        execution_time = (datetime.now(timezone.utc) - start_time).total_seconds() * 1000

        return BacktestResultResponse(
            status="completed",
            trades=result.get("trades", []),
            equity_curve=result.get("equity_curve", []),
            metrics=result.get("metrics", {}),
            config=result.get("config", {}),
            signals=result.get("signals", {}),
            execution_time_ms=round(execution_time, 2),
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[Backtest] Failed: {e}")
        raise HTTPException(500, f"Backtest failed: {str(e)}")


@router.post("/run-async/{task_id}")
async def start_async_backtest(
    task_id: str,
    request: BacktestRequest,
    background_tasks: BackgroundTasks,
    admin: dict = Depends(get_current_admin),
):
    """Start a backtest asynchronously and return task ID."""
    if task_id in _backtest_tasks:
        raise HTTPException(400, f"Task {task_id} already running")

    async def _run_backtest_task():
        try:
            ohlcv_data = await fetch_ohlcv_history(
                ticker=request.ticker,
                timeframe=request.timeframe,
                days=request.days,
            )

            if not ohlcv_data:
                _backtest_results_cache[task_id] = {"status": "error", "error": "No data"}
                return

            strategy = _get_strategy(request.strategy, {**request.strategy_params, "ticker": request.ticker})
            config = BacktestConfig(
                initial_capital=request.initial_capital,
                position_size_pct=request.position_size_pct,
                leverage=request.leverage,
                fee_pct=request.fee_pct,
                slippage_pct=request.slippage_pct,
                stop_loss_pct=request.stop_loss_pct,
                trailing_mode=request.trailing_mode,
                trailing_pct=request.trailing_pct,
                trailing_activation_pct=request.trailing_activation_pct,
                multi_tp_enabled=request.multi_tp_enabled,
                tp_levels=request.tp_levels,
                max_positions=request.max_positions,
                max_daily_loss_pct=request.max_daily_loss_pct,
                max_drawdown_pct=request.max_drawdown_pct,
                strategy_name=request.strategy,
                timeframe=request.timeframe,
            )

            engine = BacktestEngine(config, strategy)
            engine.load_data(ohlcv_data)
            result = engine.run()

            _backtest_results_cache[task_id] = {
                "status": "completed",
                "result": result,
                "completed_at": datetime.now(timezone.utc).isoformat(),
            }

            # Clean up expired cache entries
            _cleanup_backtest_cache()

        except HTTPException as e:
            _backtest_results_cache[task_id] = {
                "status": "error",
                "error": e.detail,
                "status_code": e.status_code,
            }
        except Exception as e:
            _backtest_results_cache[task_id] = {"status": "error", "error": str(e)}

        finally:
            if task_id in _backtest_tasks:
                del _backtest_tasks[task_id]

    _backtest_tasks[task_id] = asyncio.create_task(_run_backtest_task())

    return {"task_id": task_id, "status": "running", "message": "Backtest started"}


@router.get("/result/{task_id}")
async def get_backtest_result(
    task_id: str,
    admin: dict = Depends(get_current_admin),
):
    """Get backtest result by task ID."""
    if task_id not in _backtest_results_cache:
        if task_id in _backtest_tasks:
            return {"task_id": task_id, "status": "running"}
        raise HTTPException(404, f"Task {task_id} not found")

    cached = _backtest_results_cache[task_id]

    return {
        "task_id": task_id,
        **cached,
    }


@router.get("/strategies")
async def list_strategies(admin: dict = Depends(get_current_admin)):
    """List available backtest strategies."""
    return {
        "strategies": [
            {
                "name": "simple_trend",
                "description": "Simple EMA trend following strategy",
                "params": {
                    "ema_period": {"type": "int", "default": 20, "description": "EMA period"},
                    "stop_loss_pct": {"type": "float", "default": 1.5},
                    "take_profit_pct": {"type": "float", "default": 3.0},
                },
            },
            {
                "name": "smc_trend",
                "description": "Smart Money Concepts strategy using FVG and Order Blocks",
                "params": {
                    "fvg_lookback": {"type": "int", "default": 5},
                    "ob_threshold": {"type": "float", "default": 0.5},
                    "swing_lookback": {"type": "int", "default": 3},
                    "risk_reward_min": {"type": "float", "default": 1.5},
                },
            },
            {
                "name": "ai_assistant",
                "description": "Multi-indicator strategy with EMA, RSI, and Volume",
                "params": {
                    "confidence_threshold_buy": {"type": "float", "default": 0.75},
                    "confidence_threshold_sell": {"type": "float", "default": 0.75},
                    "risk_reward_min": {"type": "float", "default": 2.0},
                    "cooldown_bars": {"type": "int", "default": 10},
                },
            },
        ],
    }


@router.delete("/result/{task_id}")
async def delete_backtest_result(
    task_id: str,
    admin: dict = Depends(get_current_admin),
):
    """Delete a cached backtest result."""
    if task_id in _backtest_tasks:
        _backtest_tasks[task_id].cancel()
        del _backtest_tasks[task_id]

    if task_id in _backtest_results_cache:
        del _backtest_results_cache[task_id]
        return {"status": "deleted", "task_id": task_id}

    raise HTTPException(404, f"Task {task_id} not found")


@router.get("/compare")
async def compare_strategies(
    ticker: str = "BTCUSDT",
    timeframe: str = "1h",
    days: int = 30,
    admin: dict = Depends(get_current_admin),
):
    """Run multiple strategies and compare results."""
    strategies = ["simple_trend", "smc_trend", "ai_assistant"]
    results = []

    for strategy_name in strategies:
        try:
            request = BacktestRequest(
                ticker=ticker,
                timeframe=timeframe,
                days=days,
                strategy=strategy_name,
            )

            ohlcv_data = await fetch_ohlcv_history(ticker=ticker, timeframe=timeframe, days=days)

            if not ohlcv_data:
                continue

            strategy = _get_strategy(strategy_name, {"ticker": ticker})
            config = BacktestConfig(
                initial_capital=10000.0,
                strategy_name=strategy_name,
                timeframe=timeframe,
            )

            engine = BacktestEngine(config, strategy)
            engine.load_data(ohlcv_data)
            result = engine.run()

            results.append({
                "strategy": strategy_name,
                "metrics": result.get("metrics", {}),
                "trades_count": len(result.get("trades", [])),
            })

        except Exception as e:
            logger.warning(f"[Backtest/Compare] Strategy {strategy_name} failed: {e}")

    return {
        "ticker": ticker,
        "timeframe": timeframe,
        "days": days,
        "comparison": results,
    }


def _get_strategy(name: str, params: dict) -> Optional[object]:
    strategies = {
        "simple_trend": SimpleTrendFollowStrategy,
        "smc_trend": SMCTrendStrategy,
        "ai_assistant": AIAssistantStrategy,
    }

    strategy_class = strategies.get(name)
    if not strategy_class:
        return None

    merged_params = {**params, "ticker": params.get("ticker", "BTCUSDT")}
    return strategy_class(merged_params)
