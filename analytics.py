"""
Signal Server - Analytics Module (Enhanced)
Performance analytics and trade statistics.
"""
import json
from datetime import datetime, timezone, timedelta
from typing import Optional
from collections import defaultdict

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func

from core.database import TradeModel


async def calculate_performance(
    session: AsyncSession,
    days: int = 30,
    user_id: Optional[str] = None,
) -> dict:
    """
    Calculate comprehensive performance metrics.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)

    # Build query
    query = select(TradeModel).where(TradeModel.timestamp >= cutoff)
    if user_id:
        query = query.where(TradeModel.user_id == user_id)

    result = await session.execute(query.order_by(TradeModel.timestamp))
    trades = result.scalars().all()

    if not trades:
        return _empty_performance()

    # Calculate metrics
    total_trades = len(trades)
    executed_trades = [t for t in trades if t.execute]
    closed_trades = [t for t in executed_trades if _is_closed_trade(t)]
    open_trades = len(executed_trades) - len(closed_trades)

    # PnL calculations
    pnls = [t.pnl_pct for t in closed_trades if t.pnl_pct is not None]
    total_pnl = sum(pnls) if pnls else 0

    # Win/Loss
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    breakeven = [p for p in pnls if p == 0]

    win_rate = (len(wins) / len(pnls) * 100) if pnls else 0

    # Average win/loss
    avg_win = sum(wins) / len(wins) if wins else 0
    avg_loss = sum(losses) / len(losses) if losses else 0

    # Risk/Reward
    risk_reward = abs(avg_win / avg_loss) if avg_loss != 0 else 0

    # Profit factor
    gross_profit = sum(wins) if wins else 0
    gross_loss = abs(sum(losses)) if losses else 0
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else 0

    # Drawdown
    equity_curve = _calculate_equity_curve(pnls)
    max_drawdown = _calculate_max_drawdown(equity_curve)

    # Sharpe Ratio (simplified, assuming 0% risk-free rate)
    if len(pnls) > 1:
        import statistics
        std = statistics.stdev(pnls) if len(pnls) > 1 else 0
        avg_pnl = sum(pnls) / len(pnls)
        sharpe = (avg_pnl / std * (252 ** 0.5)) if std > 0 else 0
    else:
        sharpe = 0

    # Sortino Ratio
    negative_returns = [p for p in pnls if p < 0]
    if negative_returns:
        import statistics
        downside_std = statistics.stdev(negative_returns) if len(negative_returns) > 1 else 0
        avg_pnl = sum(pnls) / len(pnls)
        sortino = (avg_pnl / downside_std * (252 ** 0.5)) if downside_std > 0 else 0
    else:
        sortino = sharpe

    # Consecutive wins/losses
    max_consec_wins, max_consec_losses = _calculate_consecutive(pnls)

    # Best/worst trades
    best_trade = max(pnls) if pnls else 0
    worst_trade = min(pnls) if pnls else 0

    # AI stats
    ai_stats = await _calculate_ai_stats(trades)

    return {
        "total_trades": total_trades,
        "executed_trades": len(executed_trades),
        "closed_trades": len(closed_trades),
        "open_trades": open_trades,
        "total_pnl_pct": round(total_pnl, 2),
        "win_rate": round(win_rate, 1),
        "winning_trades": len(wins),
        "losing_trades": len(losses),
        "breakeven_trades": len(breakeven),
        "avg_win_pct": round(avg_win, 2),
        "avg_loss_pct": round(avg_loss, 2),
        "risk_reward_ratio": round(risk_reward, 2),
        "profit_factor": round(profit_factor, 2),
        "max_drawdown_pct": round(max_drawdown, 2),
        "sharpe_ratio": round(sharpe, 2),
        "sortino_ratio": round(sortino, 2),
        "best_trade_pct": round(best_trade, 2),
        "worst_trade_pct": round(worst_trade, 2),
        "max_consecutive_wins": max_consec_wins,
        "max_consecutive_losses": max_consec_losses,
        "equity_curve": equity_curve,
        "ai_stats": ai_stats,
    }


async def get_daily_pnl(
    session: AsyncSession,
    days: int = 30,
    user_id: Optional[str] = None,
) -> list[dict]:
    """Get daily PnL breakdown."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)

    query = select(TradeModel).where(TradeModel.timestamp >= cutoff)
    if user_id:
        query = query.where(TradeModel.user_id == user_id)

    result = await session.execute(query)
    trades = result.scalars().all()

    # Group by day
    daily_pnl = defaultdict(float)
    for trade in trades:
        if trade.pnl_pct is not None and _is_closed_trade(trade):
            day = trade.timestamp.strftime("%Y-%m-%d")
            daily_pnl[day] += trade.pnl_pct

    # Fill missing days
    all_days = []
    for i in range(days):
        day = (datetime.now(timezone.utc) - timedelta(days=i)).strftime("%Y-%m-%d")
        all_days.append(day)

    return [
        {"date": day, "pnl": round(daily_pnl.get(day, 0), 2)}
        for day in sorted(all_days)
    ]


async def get_trade_distribution(
    session: AsyncSession,
    user_id: Optional[str] = None,
) -> dict:
    """Get trade distribution by ticker and direction."""
    query = select(TradeModel)
    if user_id:
        query = query.where(TradeModel.user_id == user_id)

    result = await session.execute(query)
    trades = result.scalars().all()

    by_ticker = defaultdict(lambda: {"long": 0, "short": 0, "pnl": 0})
    by_direction = {"long": 0, "short": 0}

    for trade in trades:
        if trade.ticker and trade.direction:
            direction = "long" if "long" in trade.direction.lower() else "short"
            by_ticker[trade.ticker][direction] += 1
            by_direction[direction] += 1
            if trade.pnl_pct and _is_closed_trade(trade):
                by_ticker[trade.ticker]["pnl"] += trade.pnl_pct

    return {
        "by_ticker": dict(by_ticker),
        "by_direction": by_direction,
    }


def _empty_performance() -> dict:
    """Return empty performance metrics."""
    return {
        "total_trades": 0,
        "executed_trades": 0,
        "closed_trades": 0,
        "open_trades": 0,
        "total_pnl_pct": 0,
        "win_rate": 0,
        "winning_trades": 0,
        "losing_trades": 0,
        "breakeven_trades": 0,
        "avg_win_pct": 0,
        "avg_loss_pct": 0,
        "risk_reward_ratio": 0,
        "profit_factor": 0,
        "max_drawdown_pct": 0,
        "sharpe_ratio": 0,
        "sortino_ratio": 0,
        "best_trade_pct": 0,
        "worst_trade_pct": 0,
        "max_consecutive_wins": 0,
        "max_consecutive_losses": 0,
        "equity_curve": [],
        "ai_stats": {},
    }


def _is_closed_trade(trade) -> bool:
    status = str(getattr(trade, "order_status", "") or "").lower()
    direction = str(getattr(trade, "direction", "") or "").lower()
    if direction.startswith("close_"):
        return True
    if status in {"closed", "paper_closed", "exchange_closed", "tp_hit", "sl_hit"}:
        return True
    try:
        payload = json.loads(trade.payload_json) if trade.payload_json else {}
        return payload.get("position_event") == "closed" or bool(payload.get("close_reason"))
    except Exception:
        return False


def _calculate_equity_curve(pnls: list[float]) -> list[dict]:
    """Calculate cumulative equity curve."""
    curve = []
    cumulative = 0
    for i, pnl in enumerate(pnls):
        cumulative += pnl
        curve.append({
            "trade": i + 1,
            "pnl": pnl,
            "cumulative_pnl": round(cumulative, 2),
        })
    return curve


def _calculate_max_drawdown(equity_curve: list[dict]) -> float:
    """Calculate maximum drawdown percentage."""
    if not equity_curve:
        return 0

    peak = 0
    max_dd = 0

    for point in equity_curve:
        cum_pnl = point["cumulative_pnl"]
        if cum_pnl > peak:
            peak = cum_pnl
        drawdown = peak - cum_pnl
        if drawdown > max_dd:
            max_dd = drawdown

    return max_dd


def _calculate_consecutive(pnls: list[float]) -> tuple[int, int]:
    """Calculate max consecutive wins and losses."""
    if not pnls:
        return 0, 0

    max_wins = 0
    max_losses = 0
    current_wins = 0
    current_losses = 0

    for pnl in pnls:
        if pnl > 0:
            current_wins += 1
            current_losses = 0
            max_wins = max(max_wins, current_wins)
        elif pnl < 0:
            current_losses += 1
            current_wins = 0
            max_losses = max(max_losses, current_losses)
        else:
            current_wins = 0
            current_losses = 0

    return max_wins, max_losses


async def _calculate_ai_stats(trades: list) -> dict:
    """Calculate AI performance statistics."""
    high_conf_trades = []
    low_conf_trades = []
    all_confidences = []

    for trade in trades:
        if not _is_closed_trade(trade):
            continue
        try:
            payload = json.loads(trade.payload_json) if trade.payload_json else {}
            analysis = payload.get("analysis", {})
            confidence = analysis.get("confidence")

            if confidence is not None:
                all_confidences.append(confidence)

                if confidence >= 0.7:
                    high_conf_trades.append(trade.pnl_pct or 0)
                elif confidence < 0.5:
                    low_conf_trades.append(trade.pnl_pct or 0)
        except (TypeError, json.JSONDecodeError, ValueError):
            pass

    def win_rate(trade_list):
        if not trade_list:
            return 0
        wins = sum(1 for p in trade_list if p > 0)
        return (wins / len(trade_list)) * 100

    return {
        "high_confidence_trades": len(high_conf_trades),
        "low_confidence_trades": len(low_conf_trades),
        "high_confidence_win_rate": win_rate(high_conf_trades),
        "low_confidence_win_rate": win_rate(low_conf_trades),
        "avg_confidence": sum(all_confidences) / len(all_confidences) if all_confidences else 0,
    }


# Cache invalidation
_performance_cache = {}
_cache_time = {}


def invalidate_performance_cache(user_id: Optional[str] = None):
    """Invalidate performance cache."""
    global _performance_cache, _cache_time
    key = user_id or "global"
    _performance_cache.pop(key, None)
    _cache_time.pop(key, None)
