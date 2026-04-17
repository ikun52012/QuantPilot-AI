"""
OpenClaw Signal Server - Pre-Filter (Rule-Based Layer)
Fast, free, rule-based checks BEFORE calling the AI.
Filters out 60-70% of low-quality signals instantly.
"""
from datetime import datetime, timedelta
from loguru import logger
from models import TradingViewSignal, MarketContext, PreFilterResult
from trade_logger import get_today_pnl

# ─────────────────────────────────────────────
# In-memory state for tracking
# ─────────────────────────────────────────────
_recent_signals: list[dict] = []
_daily_trade_count: int = 0
_daily_trade_date: str = ""
_daily_pnl: float = 0.0


def reset_daily_counters():
    """Reset daily counters at midnight."""
    global _daily_trade_count, _daily_trade_date, _daily_pnl
    _daily_trade_count = 0
    _daily_trade_date = datetime.utcnow().strftime("%Y-%m-%d")
    _daily_pnl = 0.0


def increment_trade_count():
    global _daily_trade_count, _daily_trade_date
    today = datetime.utcnow().strftime("%Y-%m-%d")
    if today != _daily_trade_date:
        reset_daily_counters()
    _daily_trade_count += 1


def update_daily_pnl(pnl: float):
    # No-op: daily PnL is now derived directly from trade logs via get_today_pnl().
    # Kept for backward compatibility.
    pass


def run_pre_filter(
    signal: TradingViewSignal,
    market: MarketContext,
    max_daily_trades: int = 10,
    max_daily_loss_pct: float = 5.0,
) -> PreFilterResult:
    """
    Run fast rule-based checks on the incoming signal.
    Returns PreFilterResult with pass/fail and reasons.
    """
    global _daily_trade_count, _daily_trade_date

    checks = {}
    reasons = []

    # ── Check 1: Daily trade limit ──
    today = datetime.utcnow().strftime("%Y-%m-%d")
    if today != _daily_trade_date:
        reset_daily_counters()

    daily_ok = _daily_trade_count < max_daily_trades
    checks["daily_trade_limit"] = {
        "passed": daily_ok,
        "current": _daily_trade_count,
        "max": max_daily_trades,
    }
    if not daily_ok:
        reasons.append(f"Daily trade limit reached ({_daily_trade_count}/{max_daily_trades})")

    # ── Check 2: Daily loss limit ──
    current_pnl = get_today_pnl()
    loss_ok = current_pnl > -max_daily_loss_pct
    checks["daily_loss_limit"] = {
        "passed": loss_ok,
        "current_pnl": current_pnl,
        "max_loss": max_daily_loss_pct,
    }
    if not loss_ok:
        reasons.append(f"Daily loss limit reached ({current_pnl:.2f}% / -{max_daily_loss_pct}%)")

    # ── Check 3: Duplicate signal cooldown ──
    cooldown_ok = _check_cooldown(signal, cooldown_seconds=300)
    checks["cooldown"] = {"passed": cooldown_ok}
    if not cooldown_ok:
        reasons.append("Duplicate signal within 5-minute cooldown")

    # ── Check 4: Price sanity check ──
    price_ok = True
    if market.current_price > 0 and signal.price > 0:
        price_diff = abs(signal.price - market.current_price) / market.current_price * 100
        price_ok = price_diff < 2.0     # signal price shouldn't deviate >2% from current
        checks["price_sanity"] = {
            "passed": price_ok,
            "signal_price": signal.price,
            "market_price": market.current_price,
            "diff_pct": round(price_diff, 4),
        }
        if not price_ok:
            reasons.append(f"Signal price deviates {price_diff:.2f}% from market price")

    # ── Check 5: Extreme volatility guard ──
    vol_ok = True
    if market.atr_pct is not None:
        vol_ok = market.atr_pct < 15.0  # skip if ATR% > 15% (extremely volatile)
        checks["volatility_guard"] = {
            "passed": vol_ok,
            "atr_pct": market.atr_pct,
        }
        if not vol_ok:
            reasons.append(f"Extreme volatility: ATR% = {market.atr_pct:.2f}%")

    # ── Check 6: Spread check ──
    spread_ok = True
    if market.bid_ask_spread > 0:
        spread_ok = market.bid_ask_spread < 0.1     # spread < 0.1%
        checks["spread"] = {
            "passed": spread_ok,
            "spread_pct": market.bid_ask_spread,
        }
        if not spread_ok:
            reasons.append(f"Spread too wide: {market.bid_ask_spread:.4f}%")

    # ── Check 7: Volume sanity ──
    volume_ok = True
    if market.volume_24h > 0:
        volume_ok = market.volume_24h > 1_000_000   # min $1M 24h volume
        checks["volume"] = {
            "passed": volume_ok,
            "volume_24h": market.volume_24h,
        }
        if not volume_ok:
            reasons.append(f"Low 24h volume: ${market.volume_24h:,.0f}")

    # ── Check 8: Large sudden move guard ──
    sudden_move_ok = True
    if market.price_change_1h != 0:
        sudden_move_ok = abs(market.price_change_1h) < 8.0     # >8% in 1h = skip
        checks["sudden_move"] = {
            "passed": sudden_move_ok,
            "price_change_1h": market.price_change_1h,
        }
        if not sudden_move_ok:
            reasons.append(f"Sudden move: {market.price_change_1h:+.2f}% in 1h")

    # ── Final verdict ──
    all_passed = all(c.get("passed", True) for c in checks.values())

    if all_passed:
        # Record this signal
        _recent_signals.append({
            "ticker": signal.ticker,
            "direction": signal.direction,
            "timestamp": datetime.utcnow(),
        })
        logger.info(f"[PreFilter] ✅ PASSED - {signal.ticker} {signal.direction}")
    else:
        logger.warning(f"[PreFilter] ❌ BLOCKED - {signal.ticker} {signal.direction}: {'; '.join(reasons)}")

    return PreFilterResult(
        passed=all_passed,
        reason="; ".join(reasons) if reasons else "All checks passed",
        checks=checks,
    )


def _check_cooldown(signal: TradingViewSignal, cooldown_seconds: int = 300) -> bool:
    """Check if we received a similar signal recently."""
    cutoff = datetime.utcnow() - timedelta(seconds=cooldown_seconds)
    # Clean old entries
    global _recent_signals
    _recent_signals = [s for s in _recent_signals if s["timestamp"] > cutoff]

    for s in _recent_signals:
        if s["ticker"] == signal.ticker and s["direction"] == signal.direction:
            return False
    return True
