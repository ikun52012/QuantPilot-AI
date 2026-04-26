"""
QuantPilot AI - Trade Logger
Persists all trade decisions and results to JSON files.
Enhanced with async database support.
"""
import json
import threading
import uuid
import concurrent.futures
from datetime import datetime, timedelta
from pathlib import Path
from loguru import logger
from models import TradeLog, TradeDecision
from core.utils.datetime import utcnow, utcnow_iso

LOGS_DIR = Path(__file__).parent / "trade_logs"
LOGS_DIR.mkdir(exist_ok=True)

# Thread lock to prevent concurrent write races on the daily JSON file
_file_lock = threading.Lock()


def _get_log_file() -> Path:
    """Get today's log file path."""
    date_str = utcnow().strftime("%Y-%m-%d")
    return LOGS_DIR / f"trades_{date_str}.json"


def _load_logs(path: Path) -> list[dict]:
    """Load trade logs from file."""
    if not path.exists():
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError):
        return []


def _save_logs(path: Path, logs: list[dict]):
    """Save trade logs to file."""
    tmp_path = path.with_suffix(".json.tmp")
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(logs, f, indent=2, default=str, ensure_ascii=False)
    tmp_path.replace(path)


def _filter_user(trades: list[dict], user_id: str | None = None) -> list[dict]:
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
    Kept as a thin shim for any remaining sync callers.
    """
    import asyncio
    import warnings

    warnings.warn(
        "log_trade() is deprecated and will be removed in v5.0. Use log_trade_async() instead.",
        DeprecationWarning,
        stacklevel=2,
    )

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(log_trade_async(decision, order_result, user_id))

    # Already inside an event loop — schedule as a task
    import concurrent.futures
    with concurrent.futures.ThreadPoolExecutor() as executor:
        future = executor.submit(asyncio.run, log_trade_async(decision, order_result, user_id))
        return future.result(timeout=30)


def get_today_trades(user_id: str | None = None) -> list[dict]:
    """Get all trades from today."""
    return get_trade_history(1, user_id=user_id)


def get_today_pnl(user_id: str | None = None) -> float:
    """Return today's cumulative realised PnL percentage from the trade log."""
    trades = get_today_trades(user_id)
    return sum(t.get("pnl_pct", 0.0) or 0.0 for t in trades if t.get("execute"))


def get_today_stats(user_id: str | None = None) -> dict:
    """Get today's trading statistics."""
    trades = get_today_trades(user_id)
    executed = [t for t in trades if t.get("execute")]
    rejected = [t for t in trades if not t.get("execute")]

    return {
        "total_signals": len(trades),
        "executed": len(executed),
        "rejected": len(rejected),
        "tickers": list(set(t.get("ticker", "") for t in executed)),
    }


def get_trade_history(days: int = 7, user_id: str | None = None) -> list[dict]:
    """Get trade history for the last N days from JSON files."""
    all_trades = []
    days = max(1, min(int(days), 365))
    for i in range(days):
        date = utcnow() - timedelta(days=i)
        date_str = date.strftime("%Y-%m-%d")
        path = LOGS_DIR / f"trades_{date_str}.json"
        trades = _load_logs(path)
        all_trades.extend(_filter_user(trades, user_id))

    # Sort by timestamp descending
    all_trades.sort(key=lambda t: t.get("timestamp", ""), reverse=True)
    return all_trades


async def get_trade_history_async(days: int = 7, user_id: str | None = None) -> list[dict]:
    """Get trade history for the last N days from database (async)."""
    from core.database import db_manager, get_trade_logs_async

    try:
        async with db_manager.async_session_factory() as session:
            db_trades = await get_trade_logs_async(session, days, user_id)
    except Exception as e:
        logger.warning(f"[TradeLog] Database read failed, falling back to JSON: {e}")
        return get_trade_history(days, user_id)

    # Merge with JSON logs for completeness
    json_trades = get_trade_history(days, user_id)

    # Deduplicate by ID
    by_id = {t.get("id"): t for t in json_trades if t.get("id")}
    for trade in db_trades:
        trade_id = trade.get("id")
        if trade_id:
            by_id[trade_id] = trade

    merged = list(by_id.values())
    merged.sort(key=lambda t: t.get("timestamp", ""), reverse=True)
    return merged


def get_recent_trade_results(limit: int = 5, user_id: str | None = None) -> list[dict]:
    """Get the most recent executed trade results (for consecutive loss check)."""
    all_trades = get_trade_history(days=3, user_id=user_id)
    executed = [t for t in all_trades if t.get("execute")]
    executed.sort(key=lambda t: t.get("timestamp", ""), reverse=True)
    return executed[:limit]


async def get_today_pnl_async(user_id: str | None = None) -> float:
    """Return today's cumulative realised PnL percentage (async version)."""
    try:
        trades = await get_trade_history_async(1, user_id=user_id)
        return sum(t.get("pnl_pct", 0.0) or 0.0 for t in trades if t.get("execute"))
    except Exception as e:
        logger.debug(f"[TradeLog] Async PnL fetch failed: {e}")
        return get_today_pnl(user_id)


async def get_recent_trade_results_async(limit: int = 5, user_id: str | None = None) -> list[dict]:
    """Get the most recent executed trade results (async version)."""
    try:
        all_trades = await get_trade_history_async(days=3, user_id=user_id)
        executed = [t for t in all_trades if t.get("execute")]
        executed.sort(key=lambda t: t.get("timestamp", ""), reverse=True)
        return executed[:limit]
    except Exception as e:
        logger.debug(f"[TradeLog] Async trade results fetch failed: {e}")
        return get_recent_trade_results(limit, user_id)


async def get_today_stats_async(user_id: str | None = None) -> dict:
    """Get today's trading statistics (async version)."""
    try:
        trades = await get_trade_history_async(1, user_id=user_id)
        executed = [t for t in trades if t.get("execute")]
        rejected = [t for t in trades if not t.get("execute")]

        return {
            "total_signals": len(trades),
            "executed": len(executed),
            "rejected": len(rejected),
            "tickers": list(set(t.get("ticker", "") for t in executed)),
        }
    except Exception as e:
        logger.debug(f"[TradeLog] Async stats fetch failed: {e}")
        return get_today_stats(user_id)
