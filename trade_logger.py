"""
OpenClaw Signal Server - Trade Logger
Persists all trade decisions and results to JSON files.
"""
import json
import uuid
from datetime import datetime
from pathlib import Path
from loguru import logger
from models import TradeLog, TradeDecision

LOGS_DIR = Path(__file__).parent / "trade_logs"
LOGS_DIR.mkdir(exist_ok=True)


def _get_log_file() -> Path:
    """Get today's log file path."""
    date_str = datetime.utcnow().strftime("%Y-%m-%d")
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
    with open(path, "w", encoding="utf-8") as f:
        json.dump(logs, f, indent=2, default=str, ensure_ascii=False)


def log_trade(decision: TradeDecision, order_result: dict) -> str:
    """
    Log a trade decision and its execution result.
    Returns the trade ID.
    """
    trade_id = str(uuid.uuid4())[:8]

    entry = {
        "id": trade_id,
        "timestamp": datetime.utcnow().isoformat(),
        "ticker": decision.ticker,
        "direction": decision.direction.value if decision.direction else "unknown",
        "execute": decision.execute,
        "entry_price": decision.entry_price,
        "stop_loss": decision.stop_loss,
        "take_profit": decision.take_profit,
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
        }

    # Save to today's log file
    log_path = _get_log_file()
    logs = _load_logs(log_path)
    logs.append(entry)
    _save_logs(log_path, logs)

    logger.info(f"[TradeLog] Saved trade {trade_id} → {log_path.name}")
    return trade_id


def get_today_trades() -> list[dict]:
    """Get all trades from today."""
    return _load_logs(_get_log_file())


def get_today_stats() -> dict:
    """Get today's trading statistics."""
    trades = get_today_trades()
    executed = [t for t in trades if t.get("execute")]
    rejected = [t for t in trades if not t.get("execute")]

    return {
        "total_signals": len(trades),
        "executed": len(executed),
        "rejected": len(rejected),
        "tickers": list(set(t.get("ticker", "") for t in executed)),
    }


def get_trade_history(days: int = 7) -> list[dict]:
    """Get trade history for the last N days."""
    all_trades = []
    for i in range(days):
        date = datetime.utcnow() - __import__("datetime").timedelta(days=i)
        date_str = date.strftime("%Y-%m-%d")
        path = LOGS_DIR / f"trades_{date_str}.json"
        trades = _load_logs(path)
        all_trades.extend(trades)
    return all_trades
