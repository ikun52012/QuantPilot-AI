"""
Signal Server - Performance Analytics
Calculates Sharpe ratio, Sortino ratio, max drawdown, win rate, profit factor, etc.
"""
import math
import time
from datetime import datetime, timedelta
from pathlib import Path
from trade_logger import get_trade_history, get_today_trades

# ─────────────────────────────────────────────
# Simple in-memory cache for performance calculations
# ─────────────────────────────────────────────
_CACHE_TTL = 60  # seconds
_performance_cache: dict[str, tuple[object, float]] = {}


def _get_cached(key: str) -> object | None:
    """Return cached value if still fresh, otherwise None."""
    entry = _performance_cache.get(key)
    if entry and (time.time() - entry[1]) < _CACHE_TTL:
        return entry[0]
    return None


def _set_cache(key: str, value: dict):
    _performance_cache[key] = (value, time.time())


def invalidate_performance_cache():
    """Call this after a new trade is logged to ensure fresh metrics."""
    _performance_cache.clear()


def calculate_performance(days: int = 30, user_id: str | None = None) -> dict:
    """
    Calculate comprehensive performance metrics from trade history.
    Results are cached for up to 60 seconds to avoid redundant computation.
    """
    cache_key = f"perf_{days}_{user_id or 'all'}"
    cached = _get_cached(cache_key)
    if cached is not None:
        return cached

    result = _calculate_performance_impl(days, user_id=user_id)
    _set_cache(cache_key, result)
    return result


def _calculate_performance_impl(days: int = 30, user_id: str | None = None) -> dict:
    trades = get_trade_history(days, user_id=user_id)
    executed = [t for t in trades if t.get("execute") and t.get("order_status") in ("filled", "simulated")]

    if not executed:
        return _empty_metrics()

    # Extract PnL data
    pnl_list = []
    equity_curve = []
    cumulative = 0.0

    for t in executed:
        pnl = t.get("pnl_pct", 0.0) or 0.0
        pnl_list.append(pnl)
        cumulative += pnl
        equity_curve.append({
            "timestamp": t.get("timestamp", ""),
            "pnl": round(pnl, 4),
            "cumulative_pnl": round(cumulative, 4),
        })

    # Basic stats
    total_trades = len(pnl_list)
    winning = [p for p in pnl_list if p > 0]
    losing = [p for p in pnl_list if p < 0]
    breakeven = [p for p in pnl_list if p == 0]

    win_count = len(winning)
    loss_count = len(losing)
    win_rate = (win_count / total_trades * 100) if total_trades > 0 else 0.0

    avg_win = (sum(winning) / win_count) if win_count > 0 else 0.0
    avg_loss = (sum(losing) / loss_count) if loss_count > 0 else 0.0
    total_pnl = sum(pnl_list)

    # Profit factor
    gross_profit = sum(winning) if winning else 0.0
    gross_loss = abs(sum(losing)) if losing else 0.0
    profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else float("inf") if gross_profit > 0 else 0.0

    # Expectancy (average PnL per trade)
    expectancy = total_pnl / total_trades if total_trades > 0 else 0.0

    # Risk/Reward ratio
    risk_reward = abs(avg_win / avg_loss) if avg_loss != 0 else float("inf") if avg_win > 0 else 0.0

    # Max drawdown
    max_dd, max_dd_duration = _calculate_max_drawdown(pnl_list)

    # Sharpe ratio (annualized, assuming daily returns)
    sharpe = _calculate_sharpe_ratio(pnl_list)

    # Sortino ratio (only penalizes downside volatility)
    sortino = _calculate_sortino_ratio(pnl_list)

    # Calmar ratio (return / max drawdown)
    calmar = (total_pnl / abs(max_dd)) if max_dd != 0 else 0.0

    # Consecutive wins/losses
    max_consec_wins, max_consec_losses = _calculate_consecutive(pnl_list)

    # Best/worst trade
    best_trade = max(pnl_list) if pnl_list else 0.0
    worst_trade = min(pnl_list) if pnl_list else 0.0

    # Average holding time (if available)
    avg_duration = _calculate_avg_duration(executed)

    # AI confidence correlation
    ai_stats = _calculate_ai_stats(executed)

    return {
        "period_days": days,
        "total_trades": total_trades,
        "winning_trades": win_count,
        "losing_trades": loss_count,
        "breakeven_trades": len(breakeven),
        "win_rate": round(win_rate, 2),
        "total_pnl_pct": round(total_pnl, 4),
        "avg_win_pct": round(avg_win, 4),
        "avg_loss_pct": round(avg_loss, 4),
        "best_trade_pct": round(best_trade, 4),
        "worst_trade_pct": round(worst_trade, 4),
        "profit_factor": round(profit_factor, 4) if profit_factor != float("inf") else "∞",
        "risk_reward_ratio": round(risk_reward, 4) if risk_reward != float("inf") else "∞",
        "expectancy_pct": round(expectancy, 4),
        "max_drawdown_pct": round(max_dd, 4),
        "max_drawdown_duration_trades": max_dd_duration,
        "sharpe_ratio": round(sharpe, 4),
        "sortino_ratio": round(sortino, 4),
        "calmar_ratio": round(calmar, 4),
        "max_consecutive_wins": max_consec_wins,
        "max_consecutive_losses": max_consec_losses,
        "avg_duration_minutes": avg_duration,
        "gross_profit_pct": round(gross_profit, 4),
        "gross_loss_pct": round(gross_loss, 4),
        "equity_curve": equity_curve,
        "ai_stats": ai_stats,
    }


def get_daily_pnl(days: int = 30, user_id: str | None = None) -> list[dict]:
    """Get daily aggregated PnL for charting."""
    cache_key = f"daily_pnl_{days}_{user_id or 'all'}"
    cached = _get_cached(cache_key)
    if cached is not None:
        return cached
    trades = get_trade_history(days, user_id=user_id)
    daily = {}

    for t in trades:
        if not t.get("execute"):
            continue
        ts = t.get("timestamp", "")
        if not ts:
            continue
        date = ts[:10]  # YYYY-MM-DD
        if date not in daily:
            daily[date] = {"date": date, "trades": 0, "pnl": 0.0, "wins": 0, "losses": 0}
        daily[date]["trades"] += 1
        pnl = t.get("pnl_pct", 0.0) or 0.0
        daily[date]["pnl"] += pnl
        if pnl > 0:
            daily[date]["wins"] += 1
        elif pnl < 0:
            daily[date]["losses"] += 1

    result = sorted(daily.values(), key=lambda x: x["date"])
    # Add cumulative
    cum = 0.0
    for d in result:
        cum += d["pnl"]
        d["cumulative_pnl"] = round(cum, 4)
        d["pnl"] = round(d["pnl"], 4)

    _set_cache(cache_key, result)
    return result


def get_trade_distribution() -> dict:
    """Get trade distribution by ticker, direction, hour, etc."""
    trades = get_trade_history(30)
    executed = [t for t in trades if t.get("execute")]

    by_ticker = {}
    by_direction = {"long": 0, "short": 0, "close_long": 0, "close_short": 0}
    by_hour = {str(h).zfill(2): 0 for h in range(24)}
    by_ai_recommendation = {"execute": 0, "modify": 0, "reject": 0}

    for t in executed:
        # By ticker
        ticker = t.get("ticker", "unknown")
        by_ticker[ticker] = by_ticker.get(ticker, 0) + 1

        # By direction
        direction = t.get("direction", "").lower()
        if direction in by_direction:
            by_direction[direction] += 1

        # By hour
        ts = t.get("timestamp", "")
        if len(ts) >= 13:
            hour = ts[11:13]
            if hour in by_hour:
                by_hour[hour] += 1

        # By AI recommendation
        ai = t.get("ai", {})
        rec = ai.get("recommendation", "")
        if rec in by_ai_recommendation:
            by_ai_recommendation[rec] += 1

    return {
        "by_ticker": by_ticker,
        "by_direction": by_direction,
        "by_hour": by_hour,
        "by_ai_recommendation": by_ai_recommendation,
    }


# ─────────────────────────────────────────────
# Internal calculation functions
# ─────────────────────────────────────────────

def _calculate_sharpe_ratio(returns: list[float], risk_free_rate: float = 0.0) -> float:
    """Calculate annualized Sharpe ratio."""
    if len(returns) < 2:
        return 0.0
    mean_ret = sum(returns) / len(returns) - risk_free_rate
    std_dev = _std_dev(returns)
    if std_dev == 0:
        return 0.0
    # Annualize: assume ~365 trading days for crypto
    return (mean_ret / std_dev) * math.sqrt(365)


def _calculate_sortino_ratio(returns: list[float], risk_free_rate: float = 0.0) -> float:
    """Calculate annualized Sortino ratio (only downside deviation)."""
    if len(returns) < 2:
        return 0.0
    mean_ret = sum(returns) / len(returns) - risk_free_rate
    downside = [r for r in returns if r < 0]
    if not downside:
        return float("inf") if mean_ret > 0 else 0.0
    downside_dev = math.sqrt(sum(d ** 2 for d in downside) / len(downside))
    if downside_dev == 0:
        return 0.0
    return (mean_ret / downside_dev) * math.sqrt(365)


def _calculate_max_drawdown(returns: list[float]) -> tuple[float, int]:
    """Calculate maximum drawdown and its duration in trades."""
    if not returns:
        return 0.0, 0

    cumulative = 0.0
    peak = 0.0
    max_dd = 0.0
    max_dd_dur = 0
    current_dur = 0

    for r in returns:
        cumulative += r
        if cumulative > peak:
            peak = cumulative
            current_dur = 0
        dd = cumulative - peak
        if dd < max_dd:
            max_dd = dd
        if cumulative < peak:
            current_dur += 1
            max_dd_dur = max(max_dd_dur, current_dur)

    return max_dd, max_dd_dur


def _calculate_consecutive(pnl_list: list[float]) -> tuple[int, int]:
    """Calculate max consecutive wins and losses."""
    max_wins = 0
    max_losses = 0
    current_wins = 0
    current_losses = 0

    for p in pnl_list:
        if p > 0:
            current_wins += 1
            current_losses = 0
            max_wins = max(max_wins, current_wins)
        elif p < 0:
            current_losses += 1
            current_wins = 0
            max_losses = max(max_losses, current_losses)
        else:
            current_wins = 0
            current_losses = 0

    return max_wins, max_losses


def _calculate_avg_duration(trades: list[dict]) -> float | None:
    """Calculate average trade duration in minutes."""
    durations = []
    for t in trades:
        start = t.get("timestamp")
        end = t.get("close_timestamp")
        if start and end:
            try:
                s = datetime.fromisoformat(start.replace("Z", "+00:00"))
                e = datetime.fromisoformat(end.replace("Z", "+00:00"))
                durations.append((e - s).total_seconds() / 60)
            except (ValueError, TypeError):
                pass
    if durations:
        return round(sum(durations) / len(durations), 1)
    return None


def _calculate_ai_stats(trades: list[dict]) -> dict:
    """Analyze AI prediction accuracy."""
    high_conf = [t for t in trades if t.get("ai", {}).get("confidence", 0) >= 0.7]
    low_conf = [t for t in trades if 0 < t.get("ai", {}).get("confidence", 0) < 0.5]

    def _win_rate(subset):
        if not subset:
            return 0.0
        wins = sum(1 for t in subset if (t.get("pnl_pct", 0) or 0) > 0)
        return round(wins / len(subset) * 100, 2)

    return {
        "high_confidence_trades": len(high_conf),
        "high_confidence_win_rate": _win_rate(high_conf),
        "low_confidence_trades": len(low_conf),
        "low_confidence_win_rate": _win_rate(low_conf),
        "avg_confidence": round(
            sum(t.get("ai", {}).get("confidence", 0) for t in trades) / len(trades), 4
        ) if trades else 0.0,
    }


def _std_dev(data: list[float]) -> float:
    """Calculate standard deviation."""
    if len(data) < 2:
        return 0.0
    mean = sum(data) / len(data)
    variance = sum((x - mean) ** 2 for x in data) / (len(data) - 1)
    return math.sqrt(variance)


def _empty_metrics() -> dict:
    """Return empty metrics when no data available."""
    return {
        "period_days": 0,
        "total_trades": 0,
        "winning_trades": 0,
        "losing_trades": 0,
        "breakeven_trades": 0,
        "win_rate": 0.0,
        "total_pnl_pct": 0.0,
        "avg_win_pct": 0.0,
        "avg_loss_pct": 0.0,
        "best_trade_pct": 0.0,
        "worst_trade_pct": 0.0,
        "profit_factor": 0.0,
        "risk_reward_ratio": 0.0,
        "expectancy_pct": 0.0,
        "max_drawdown_pct": 0.0,
        "max_drawdown_duration_trades": 0,
        "sharpe_ratio": 0.0,
        "sortino_ratio": 0.0,
        "calmar_ratio": 0.0,
        "max_consecutive_wins": 0,
        "max_consecutive_losses": 0,
        "avg_duration_minutes": None,
        "gross_profit_pct": 0.0,
        "gross_loss_pct": 0.0,
        "equity_curve": [],
        "ai_stats": {},
    }
