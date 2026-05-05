"""
QuantPilot AI - Trade Logger
Persists all trade decisions and results to JSON files.
Enhanced with async database support.
"""
import asyncio as _asyncio
import json
import threading
import uuid
import warnings
from datetime import timedelta
from pathlib import Path
from typing import Any

from loguru import logger

from core.utils.datetime import utcnow
from models import TradeDecision

LOGS_DIR = Path(__file__).parent / "trade_logs"
LOGS_DIR.mkdir(exist_ok=True)

# Thread lock to prevent concurrent write races on the daily JSON file
_file_lock = threading.Lock()


def _get_log_file() -> Path:
    """Get today's log file path."""
    date_str = utcnow().strftime("%Y-%m-%d")
    return LOGS_DIR / f"trades_{date_str}.json"


def _load_logs(path: Path) -> list[dict[str, Any]]:
    """Load trade logs from file."""
    if not path.exists():
        return []
    try:
        with open(path, encoding="utf-8") as f:
            loaded = json.load(f)
            if not isinstance(loaded, list):
                return []
            return [item for item in loaded if isinstance(item, dict)]
    except (OSError, json.JSONDecodeError):
        return []


def _save_logs(path: Path, logs: list[dict[str, Any]]) -> None:
    """Save trade logs to file."""
    tmp_path = path.with_suffix(".json.tmp")
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(logs, f, indent=2, default=str, ensure_ascii=False)
    tmp_path.replace(path)


def _filter_user(trades: list[dict[str, Any]], user_id: str | None = None) -> list[dict[str, Any]]:
    if user_id is None:
        return trades
    return [t for t in trades if t.get("user_id") == user_id]


async def log_trade_async(decision: TradeDecision, order_result: dict, user_id: str | None = None) -> str:
    """
    Log a trade decision and its execution result (async version).
    Returns the trade ID.
    """
    from core.database import db_manager, insert_trade_log_async

    trade_id = str(uuid.uuid4())

    entry = {
        "id": trade_id,
        "timestamp": utcnow().isoformat(),
        "user_id": user_id,
        "ticker": decision.ticker,
        "direction": decision.direction.value if decision.direction else "unknown",
        "execute": decision.execute,
        "entry_price": decision.entry_price,
        "stop_loss": decision.stop_loss,
        "take_profit": decision.take_profit,
        "take_profit_levels": [
            {"price": tp.price, "qty_pct": tp.qty_pct}
            for tp in decision.take_profit_levels
        ],
        "trailing_stop": decision.trailing_stop.mode.value if decision.trailing_stop else "none",
        "quantity": decision.quantity,
        "reason": decision.reason,
        "order_status": order_result.get("status", "unknown"),
        "order_details": order_result,
    }

    # Add AI analysis details
    if decision.ai_analysis:
        entry["ai"] = {
            "confidence": decision.ai_analysis.confidence,
            "recommendation": decision.ai_analysis.recommendation,
            "reasoning": decision.ai_analysis.reasoning,
            "risk_score": decision.ai_analysis.risk_score,
            "market_condition": decision.ai_analysis.market_condition,
            "warnings": decision.ai_analysis.warnings,
            "position_size_pct": decision.ai_analysis.position_size_pct,
            "recommended_leverage": decision.ai_analysis.recommended_leverage,
        }

    # Write to database
    try:
        async with db_manager.async_session_factory() as session:
            entry = await insert_trade_log_async(session, entry)
            await session.commit()
    except Exception as e:
        logger.error(f"[TradeLog] Database write failed: {e}")
        # Continue to write JSON mirror even if DB fails

    # Keep a JSON mirror for compatibility and manual inspection
    log_path = _get_log_file()
    try:
        with _file_lock:
            logs = _load_logs(log_path)
            logs.append(entry)
            _save_logs(log_path, logs)
    except Exception as e:
        logger.warning(f"[TradeLog] JSON mirror write skipped: {e}")

    logger.info(f"[TradeLog] Saved trade {trade_id} → {log_path.name}")
    return trade_id


def log_trade(decision: TradeDecision, order_result: dict, user_id: str | None = None) -> str:
    """
    DEPRECATED: Use log_trade_async() instead.

    When called from outside an event loop, runs log_trade_async via asyncio.run().
    When called from inside an event loop, spawns a daemon thread with its own
    event loop to avoid nested-loop deadlocks.  The coroutine is *not* awaited,
    so the returned trade_id is a best-effort placeholder.
    """
    warnings.warn(
        "log_trade() is deprecated and will be removed in v5.0. Use log_trade_async() instead.",
        DeprecationWarning,
        stacklevel=2,
    )

    try:
        _asyncio.get_running_loop()
    except RuntimeError:
        # No running event loop — safe to run synchronously
        return _asyncio.run(log_trade_async(decision, order_result, user_id))

    # We are inside a running event loop; schedule the coroutine on a
    # background thread and return a placeholder id so we never block
    # the main async context.
    result_container: dict[str, str] = {}
    error_container: dict[str, Exception] = {}

    def _runner() -> None:
        try:
            result_container["trade_id"] = _asyncio.run(
                log_trade_async(decision, order_result, user_id)
            )
        except Exception as exc:
            error_container["error"] = exc

    thread = threading.Thread(target=_runner, daemon=True)
    thread.start()
    thread.join(timeout=30)

    if thread.is_alive():
        logger.warning("[TradeLog] log_trade() thread timed out — trade may not be logged")
        return str(uuid.uuid4())

    if "error" in error_container:
        logger.warning(f"[TradeLog] log_trade() failed: {error_container['error']}")
        return str(uuid.uuid4())

    trade_id = result_container.get("trade_id")
    return trade_id if trade_id is not None else str(uuid.uuid4())


def get_today_trades(user_id: str | None = None) -> list[dict]:
    """Get all trades from today."""
    return get_trade_history(1, user_id=user_id)


def get_today_pnl(user_id: str | None = None, account_equity_usdt: float = 0.0) -> float:
    """Return today's cumulative realised PnL percentage relative to account equity.

    FIX: Calculate PnL % relative to account equity, NOT by summing position-level %.

    Args:
        account_equity_usdt: Account equity in USDT (required for correct % calculation)

    Returns:
        PnL percentage relative to account equity (e.g., -1.5 means -1.5% of account)
    """
    trades = get_today_trades(user_id)
    total_pnl_usdt = sum(t.get("pnl_usdt", 0.0) or 0.0 for t in trades if t.get("execute"))

    if account_equity_usdt <= 0:
        return 0.0

    return (total_pnl_usdt / account_equity_usdt) * 100.0


def get_today_stats(user_id: str | None = None) -> dict[str, Any]:
    """Get today's trading statistics."""
    trades = get_today_trades(user_id)
    executed = [t for t in trades if t.get("execute")]
    rejected = [t for t in trades if not t.get("execute")]

    return {
        "total_signals": len(trades),
        "executed": len(executed),
        "rejected": len(rejected),
        "tickers": list({t.get("ticker", "") for t in executed}),
    }


def get_trade_history(days: int = 7, user_id: str | None = None) -> list[dict[str, Any]]:
    """Get trade history for the last N days from JSON files."""
    all_trades: list[dict[str, Any]] = []
    days = max(1, min(int(days), 365))
    for i in range(days):
        date = utcnow() - timedelta(days=i)
        date_str = date.strftime("%Y-%m-%d")
        path = LOGS_DIR / f"trades_{date_str}.json"
        trades = _load_logs(path)
        all_trades.extend(_filter_user(trades, user_id))

    all_trades.sort(key=lambda t: t.get("timestamp", ""), reverse=True)
    return all_trades


async def get_trade_history_async(days: int = 7, user_id: str | None = None) -> list[dict[str, Any]]:
    """Get trade history for the last N days from database (async)."""
    from core.database import db_manager, get_trade_logs_async

    try:
        async with db_manager.async_session_factory() as session:
            db_trades = await get_trade_logs_async(session, days, user_id)
    except Exception as e:
        logger.warning(f"[TradeLog] Database read failed, falling back to JSON: {e}")
        return get_trade_history(days, user_id)

    json_trades = get_trade_history(days, user_id)

    by_id = {t.get("id"): t for t in json_trades if t.get("id")}
    for trade in db_trades:
        trade_id = trade.get("id")
        if trade_id:
            by_id[trade_id] = trade

    merged = list(by_id.values())
    merged.sort(key=lambda t: t.get("timestamp", ""), reverse=True)
    return merged


def get_recent_trade_results(limit: int = 5, user_id: str | None = None) -> list[dict[str, Any]]:
    """Get the most recent executed trade results (for consecutive loss check)."""
    all_trades = get_trade_history(days=3, user_id=user_id)
    executed = [t for t in all_trades if t.get("execute")]
    executed.sort(key=lambda t: t.get("timestamp", ""), reverse=True)
    return executed[:limit]


async def get_today_pnl_async(user_id: str | None = None, account_equity_usdt: float = 0.0) -> float:
    """Return today's cumulative realised PnL percentage relative to account equity (async version).

    FIX: Calculate PnL % relative to account equity, NOT by summing position-level %.

    Args:
        account_equity_usdt: Account equity in USDT (required for correct % calculation)

    Returns:
        PnL percentage relative to account equity (e.g., -1.5 means -1.5% of account)
    """
    try:
        trades = await get_trade_history_async(1, user_id=user_id)
        total_pnl_usdt = sum(t.get("pnl_usdt", 0.0) or 0.0 for t in trades if t.get("execute"))

        if account_equity_usdt <= 0:
            return 0.0

        return (total_pnl_usdt / account_equity_usdt) * 100.0
    except Exception as e:
        logger.debug(f"[TradeLog] Async PnL fetch failed: {e}")
        return get_today_pnl(user_id, account_equity_usdt)


async def get_recent_trade_results_async(limit: int = 5, user_id: str | None = None, ticker: str | None = None) -> list[dict[str, Any]]:
    """Get the most recent executed trade results (async version)."""
    try:
        all_trades = await get_trade_history_async(days=3, user_id=user_id)
        executed = [t for t in all_trades if t.get("execute")]
        if ticker:
            from core.utils.common import position_symbol_key
            target_key = position_symbol_key(ticker)
            executed = [t for t in executed if position_symbol_key(t.get("ticker", "")) == target_key]
        executed.sort(key=lambda t: t.get("timestamp", ""), reverse=True)
        return executed[:limit]
    except Exception as e:
        logger.debug(f"[TradeLog] Async trade results fetch failed: {e}")
        return get_recent_trade_results(limit, user_id)


async def get_today_stats_async(user_id: str | None = None) -> dict[str, Any]:
    """Get today's trading statistics (async version)."""
    try:
        trades = await get_trade_history_async(1, user_id=user_id)
        executed = [t for t in trades if t.get("execute")]
        rejected = [t for t in trades if not t.get("execute")]

        return {
            "total_signals": len(trades),
            "executed": len(executed),
            "rejected": len(rejected),
            "tickers": list({t.get("ticker", "") for t in executed}),
        }
    except Exception as e:
        logger.debug(f"[TradeLog] Async stats fetch failed: {e}")
        return get_today_stats(user_id)
