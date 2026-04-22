"""
QuantPilot AI - Pre-Filter (Rule-Based Layer)
Fast, free, rule-based checks BEFORE calling the AI.
Enhanced v2: 14 intelligent filters that block 70-85% of low-quality signals.
"""
import json
import threading
import concurrent.futures
from datetime import datetime, timedelta
from loguru import logger
from models import TradingViewSignal, MarketContext, PreFilterResult, SignalDirection
from trade_logger import get_today_pnl, get_recent_trade_results
from core.utils.datetime import utcnow


# 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€
# In-memory state for tracking
# 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€
_state_lock = threading.Lock()
_recent_signals: list[dict] = []
_daily_trade_count: int = 0
_daily_trade_date: str = ""
_daily_pnl: float = 0.0


def reset_daily_counters():
    """Reset daily counters at midnight. Must be called with _state_lock held."""
    global _daily_trade_count, _daily_trade_date, _daily_pnl
    _daily_trade_count = 0
    _daily_trade_date = utcnow().strftime("%Y-%m-%d")
    _daily_pnl = 0.0


def increment_trade_count():
    global _daily_trade_count, _daily_trade_date
    with _state_lock:
        today = utcnow().strftime("%Y-%m-%d")
        if today != _daily_trade_date:
            reset_daily_counters()
        _daily_trade_count += 1


def update_daily_pnl(pnl: float):
    # No-op: daily PnL is now derived directly from trade logs via get_today_pnl().
    # Kept for backward compatibility.
    pass


async def count_today_executed_trades_async(user_id: str | None = None) -> int:
    """Count today's executed trades from the async database."""
    from core.database import db_manager, count_today_executed_trades

    try:
        async with db_manager.async_session_factory() as session:
            return await count_today_executed_trades(session, user_id)
    except Exception as e:
        logger.warning(f"[PreFilter] Database count failed, using in-memory fallback: {e}")
        with _state_lock:
            today = utcnow().strftime("%Y-%m-%d")
            if today != _daily_trade_date:
                reset_daily_counters()
            return _daily_trade_count


def _trade_is_closed(trade) -> bool:
    status = str(getattr(trade, "order_status", "") or "").lower()
    direction = str(getattr(trade, "direction", "") or "").lower()
    if direction.startswith("close_"):
        return True
    if status in {"closed", "paper_closed", "exchange_closed", "tp_hit", "sl_hit"}:
        return True
    try:
        payload = json.loads(getattr(trade, "payload_json", "") or "{}")
        return payload.get("position_event") == "closed" or bool(payload.get("close_reason"))
    except Exception:
        return False


async def get_today_pnl_async(user_id: str | None = None) -> float:
    """Return today's realised PnL from the database, falling back to JSON logs."""
    from core.database import db_manager, TradeModel
    from sqlalchemy import select

    today_start = utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    try:
        async with db_manager.async_session_factory() as session:
            query = select(TradeModel).where(
                TradeModel.timestamp >= today_start,
                TradeModel.execute == True,
            )
            if user_id:
                query = query.where(TradeModel.user_id == user_id)
            result = await session.execute(query)
            trades = result.scalars().all()
            return sum(float(t.pnl_pct or 0.0) for t in trades if _trade_is_closed(t))
    except Exception as e:
        logger.warning(f"[PreFilter] Database PnL failed, using JSON fallback: {e}")
        return get_today_pnl(user_id=user_id)


async def get_recent_trade_results_async(limit: int = 5, user_id: str | None = None) -> list[dict]:
    """Get recent realised trade results from the database, falling back to JSON logs."""
    from core.database import db_manager, TradeModel
    from sqlalchemy import select

    try:
        async with db_manager.async_session_factory() as session:
            query = select(TradeModel).where(TradeModel.execute == True)
            if user_id:
                query = query.where(TradeModel.user_id == user_id)
            query = query.order_by(TradeModel.timestamp.desc()).limit(max(limit * 4, limit))
            result = await session.execute(query)
            rows = []
            for trade in result.scalars().all():
                if not _trade_is_closed(trade):
                    continue
                rows.append({
                    "id": trade.id,
                    "timestamp": trade.timestamp.isoformat() if trade.timestamp else "",
                    "pnl_pct": float(trade.pnl_pct or 0.0),
                })
                if len(rows) >= limit:
                    break
            return rows
    except Exception as e:
        logger.warning(f"[PreFilter] Database recent results failed, using JSON fallback: {e}")
        return get_recent_trade_results(limit=limit, user_id=user_id)


def count_today_executed_trades(user_id: str | None = None) -> int:
    """
    Synchronous wrapper for count_today_executed_trades_async.

    DEPRECATED: This function uses asyncio.run() in a thread pool which can cause issues.
    Prefer using count_today_executed_trades_async() directly in async contexts.
    """
    import asyncio
    import warnings

    warnings.warn(
        "count_today_executed_trades() is deprecated. Use count_today_executed_trades_async() instead.",
        DeprecationWarning,
        stacklevel=2
    )

    try:
        loop = asyncio.get_running_loop()
        with concurrent.futures.ThreadPoolExecutor() as executor:
            future = executor.submit(
                asyncio.run,
                count_today_executed_trades_async(user_id)
            )
            return future.result()
    except RuntimeError:
        return asyncio.run(count_today_executed_trades_async(user_id))


async def run_pre_filter_async(
    signal: TradingViewSignal,
    market: MarketContext,
    max_daily_trades: int = 10,
    max_daily_loss_pct: float = 5.0,
    user_id: str | None = None,
    disabled_checks: set[str] | list[str] | tuple[str, ...] | None = None,
) -> PreFilterResult:
    """
    Run 14 fast rule-based checks on the incoming signal (async version).
    Returns PreFilterResult with pass/fail and detailed reasons.
    """
    global _daily_trade_count, _daily_trade_date

    checks = {}
    reasons = []

    # 鈹€鈹€ Check 1: Daily trade limit 鈹€鈹€
    try:
        daily_count_snapshot = await count_today_executed_trades_async(user_id=user_id)
    except Exception:
        with _state_lock:
            today = utcnow().strftime("%Y-%m-%d")
            if today != _daily_trade_date:
                reset_daily_counters()
            daily_count_snapshot = _daily_trade_count

    daily_ok = True if max_daily_trades <= 0 else daily_count_snapshot < max_daily_trades
    checks["daily_trade_limit"] = {
        "passed": daily_ok,
        "current": daily_count_snapshot,
        "max": max_daily_trades,
    }
    if not daily_ok:
        reasons.append(f"Daily trade limit reached ({daily_count_snapshot}/{max_daily_trades})")

    # 鈹€鈹€ Check 2: Daily loss limit 鈹€鈹€
    current_pnl = await get_today_pnl_async(user_id=user_id)
    loss_ok = current_pnl > -max_daily_loss_pct
    checks["daily_loss_limit"] = {
        "passed": loss_ok,
        "current_pnl": current_pnl,
        "max_loss": max_daily_loss_pct,
    }
    if not loss_ok:
        reasons.append(f"Daily loss limit reached ({current_pnl:.2f}% / -{max_daily_loss_pct}%)")

    # 鈹€鈹€ Check 3: Duplicate signal cooldown 鈹€鈹€
    cooldown_ok = _check_cooldown(signal, cooldown_seconds=300, user_id=user_id)
    checks["cooldown"] = {"passed": cooldown_ok}
    if not cooldown_ok:
        reasons.append("Duplicate signal within 5-minute cooldown")

    # 鈹€鈹€ Check 4: Price sanity check 鈹€鈹€
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

    # 鈹€鈹€ Check 5: Extreme volatility guard 鈹€鈹€
    vol_ok = True
    if market.atr_pct is not None:
        vol_ok = market.atr_pct < 15.0  # skip if ATR% > 15% (extremely volatile)
        checks["volatility_guard"] = {
            "passed": vol_ok,
            "atr_pct": market.atr_pct,
            "threshold": 15.0,
        }
        if not vol_ok:
            reasons.append(f"Extreme volatility: ATR% = {market.atr_pct:.2f}%")

    # 鈹€鈹€ Check 6: Spread check 鈹€鈹€
    spread_ok = True
    if market.bid_ask_spread > 0:
        spread_ok = market.bid_ask_spread < 0.1     # spread < 0.1%
        checks["spread"] = {
            "passed": spread_ok,
            "spread_pct": market.bid_ask_spread,
            "threshold": 0.1,
        }
        if not spread_ok:
            reasons.append(f"Spread too wide: {market.bid_ask_spread:.4f}%")

    # 鈹€鈹€ Check 7: Volume sanity 鈹€鈹€
    volume_ok = True
    if market.volume_24h > 0:
        volume_ok = market.volume_24h > 1_000_000   # min $1M 24h volume
        checks["volume"] = {
            "passed": volume_ok,
            "volume_24h": market.volume_24h,
            "threshold": 1_000_000,
        }
        if not volume_ok:
            reasons.append(f"Low 24h volume: ${market.volume_24h:,.0f}")

    # 鈹€鈹€ Check 8: Large sudden move guard 鈹€鈹€
    sudden_move_ok = True
    if market.price_change_1h != 0:
        sudden_move_ok = abs(market.price_change_1h) < 8.0     # >8% in 1h = skip
        checks["sudden_move"] = {
            "passed": sudden_move_ok,
            "price_change_1h": market.price_change_1h,
            "threshold": 8.0,
        }
        if not sudden_move_ok:
            reasons.append(f"Sudden move: {market.price_change_1h:+.2f}% in 1h")

    # 鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺?
    # NEW ENHANCED CHECKS (v2)
    # 鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺?

    # 鈹€鈹€ Check 9: RSI Extreme Guard 鈹€鈹€
    rsi_ok = True
    if market.rsi_1h is not None:
        is_long = signal.direction in (SignalDirection.LONG,)
        is_short = signal.direction in (SignalDirection.SHORT,)

        if is_long and market.rsi_1h > 80:
            rsi_ok = False
        elif is_short and market.rsi_1h < 20:
            rsi_ok = False

        checks["rsi_extreme"] = {
            "passed": rsi_ok,
            "rsi_1h": market.rsi_1h,
            "direction": signal.direction.value,
            "note": "Long blocked if RSI>80, Short blocked if RSI<20",
        }
        if not rsi_ok:
            reasons.append(f"RSI extreme: {market.rsi_1h:.1f} conflicts with {signal.direction.value}")

    # 鈹€鈹€ Check 10: Funding Rate Guard 鈹€鈹€
    funding_ok = True
    if market.funding_rate is not None:
        is_long = signal.direction in (SignalDirection.LONG,)
        is_short = signal.direction in (SignalDirection.SHORT,)
        extreme_funding_threshold = 0.0005  # 0.05% when fundingRate is decimal form

        # Extremely positive funding (>0.05%) disfavors longs
        # Extremely negative funding (<-0.05%) disfavors shorts
        if is_long and market.funding_rate > extreme_funding_threshold:
            funding_ok = False
        elif is_short and market.funding_rate < -extreme_funding_threshold:
            funding_ok = False

        checks["funding_rate"] = {
            "passed": funding_ok,
            "funding_rate": market.funding_rate,
            "direction": signal.direction.value,
            "threshold": extreme_funding_threshold,
            "note": "Extreme funding rate conflicts with signal direction",
        }
        if not funding_ok:
            reasons.append(f"Extreme funding rate: {market.funding_rate * 100:.4f}% against {signal.direction.value}")

    # 鈹€鈹€ Check 11: Orderbook Imbalance Guard 鈹€鈹€
    ob_ok = True
    if market.orderbook_imbalance is not None and market.orderbook_imbalance > 0:
        is_long = signal.direction in (SignalDirection.LONG,)
        is_short = signal.direction in (SignalDirection.SHORT,)

        # Ratio < 0.4 means heavy sell pressure (bad for longs)
        # Ratio > 2.5 means heavy buy pressure (bad for shorts, usually traps)
        if is_long and market.orderbook_imbalance < 0.4:
            ob_ok = False
        elif is_short and market.orderbook_imbalance > 2.5:
            ob_ok = False

        checks["orderbook_imbalance"] = {
            "passed": ob_ok,
            "imbalance_ratio": market.orderbook_imbalance,
            "direction": signal.direction.value,
            "note": "Orderbook heavily against signal direction",
        }
        if not ob_ok:
            reasons.append(f"Orderbook imbalance {market.orderbook_imbalance:.2f} against {signal.direction.value}")

    # 鈹€鈹€ Check 12: Weekend / Low Liquidity Hours Guard 鈹€鈹€
    time_ok = True
    now_utc = utcnow()
    is_weekend = now_utc.weekday() >= 5  # Saturday=5, Sunday=6

    # Check for known low-liquidity hours (UTC 21:00-01:00)
    is_low_liq_hour = now_utc.hour >= 21 or now_utc.hour < 1

    if is_weekend and market.volume_24h > 0:
        # On weekends, require higher volume to compensate for lower liquidity
        weekend_vol_ok = market.volume_24h > 5_000_000
        if not weekend_vol_ok:
            time_ok = False

    if is_low_liq_hour and market.bid_ask_spread > 0.05:
        # During low liq hours, be stricter about spread
        time_ok = False

    checks["market_hours"] = {
        "passed": time_ok,
        "is_weekend": is_weekend,
        "is_low_liquidity_hour": is_low_liq_hour,
        "hour_utc": now_utc.hour,
        "day": now_utc.strftime("%A"),
    }
    if not time_ok:
        reasons.append(f"Low liquidity {'weekend' if is_weekend else 'hours'} detected")

    # 鈹€鈹€ Check 13: Consecutive Loss Protection 鈹€鈹€
    consec_ok = True
    try:
        recent_results = await get_recent_trade_results_async(limit=5, user_id=user_id)
        if len(recent_results) >= 3:
            # If last 3 trades were all losses, block the next one
            last_three = recent_results[:3]
            if all(r.get("pnl_pct", 0) < 0 for r in last_three):
                consec_ok = False

        checks["consecutive_loss"] = {
            "passed": consec_ok,
            "recent_results": len(recent_results),
            "note": "Blocks after 3 consecutive losses",
        }
        if not consec_ok:
            reasons.append("3 consecutive losing trades 鈥?cooling off")
    except Exception:
        checks["consecutive_loss"] = {"passed": True, "note": "Could not check (no history)"}

    # 鈹€鈹€ Check 14: Same-Direction Signal Saturation 鈹€鈹€
    saturation_ok = True
    same_dir_count = _count_recent_same_direction(signal, window_minutes=60, user_id=user_id)
    if same_dir_count >= 3:
        saturation_ok = False

    checks["signal_saturation"] = {
        "passed": saturation_ok,
        "same_direction_last_hour": same_dir_count,
        "threshold": 3,
        "note": "Blocks if 3+ same-direction signals in 1 hour",
    }
    if not saturation_ok:
        reasons.append(f"Signal saturation: {same_dir_count} {signal.direction.value} signals in 1h")

    # 鈹€鈹€ Check 15: EMA Trend Alignment 鈹€鈹€
    ema_ok = True
    if market.ema_fast is not None and market.ema_slow is not None:
        is_long = signal.direction in (SignalDirection.LONG,)
        is_short = signal.direction in (SignalDirection.SHORT,)

        ema_bullish = market.ema_fast > market.ema_slow
        ema_bearish = market.ema_fast < market.ema_slow

        # Only block if EMAs are strongly against the signal (>1% divergence)
        ema_diff_pct = abs(market.ema_fast - market.ema_slow) / market.ema_slow * 100 if market.ema_slow > 0 else 0

        if is_long and ema_bearish and ema_diff_pct > 1.0:
            ema_ok = False
        elif is_short and ema_bullish and ema_diff_pct > 1.0:
            ema_ok = False

        checks["ema_alignment"] = {
            "passed": ema_ok,
            "ema_fast": market.ema_fast,
            "ema_slow": market.ema_slow,
            "ema_diff_pct": round(ema_diff_pct, 4),
            "trend": "bullish" if ema_bullish else "bearish",
            "direction": signal.direction.value,
        }
        if not ema_ok:
            trend = "bullish" if ema_bullish else "bearish"
            reasons.append(f"EMA trend ({trend}) conflicts with {signal.direction.value} (diff={ema_diff_pct:.2f}%)")

    # 鈹€鈹€ Final verdict 鈹€鈹€
    disabled = {str(item).strip() for item in (disabled_checks or []) if str(item).strip()}
    for name in disabled:
        if name in checks:
            checks[name]["disabled"] = True
            checks[name]["passed"] = True
    all_passed = all(c.get("passed", True) for c in checks.values())
    total_checks = len(checks)
    passed_checks = sum(1 for c in checks.values() if c.get("passed", True))
    failed_names = [name for name, c in checks.items() if not c.get("passed", True)]

    if all_passed:
        # Record this signal (thread-safe)
        with _state_lock:
            _recent_signals.append({
                "user_id": user_id or "admin",
                "ticker": signal.ticker,
                "direction": signal.direction,
                "timestamp": utcnow(),
            })
        logger.info(f"[PreFilter] 鉁?PASSED ({passed_checks}/{total_checks}) - {signal.ticker} {signal.direction}")
    else:
        logger.warning(
            f"[PreFilter] 鉂?BLOCKED ({passed_checks}/{total_checks}) - "
            f"{signal.ticker} {signal.direction}: {'; '.join(reasons)}"
        )

    final_reason = "; ".join(reasons) if reasons and not disabled else "; ".join(failed_names)
    return PreFilterResult(
        passed=all_passed,
        reason=final_reason if final_reason else f"All {total_checks} checks passed",
        checks=checks,
    )


def run_pre_filter(
    signal: TradingViewSignal,
    market: MarketContext,
    max_daily_trades: int = 10,
    max_daily_loss_pct: float = 5.0,
    user_id: str | None = None,
    disabled_checks: set[str] | list[str] | tuple[str, ...] | None = None,
) -> PreFilterResult:
    """
    Synchronous wrapper for run_pre_filter_async.

    DEPRECATED: This function uses asyncio.run() in a thread pool which can cause issues.
    Prefer using run_pre_filter_async() directly in async contexts.

    NOTE: The services/signal_processor.py already uses run_pre_filter_async().
    """
    import asyncio
    import warnings

    warnings.warn(
        "run_pre_filter() is deprecated. Use run_pre_filter_async() instead.",
        DeprecationWarning,
        stacklevel=2
    )

    try:
        loop = asyncio.get_running_loop()
        with concurrent.futures.ThreadPoolExecutor() as executor:
            future = executor.submit(
                asyncio.run,
                run_pre_filter_async(signal, market, max_daily_trades, max_daily_loss_pct, user_id, disabled_checks)
            )
            return future.result()
    except RuntimeError:
        return asyncio.run(run_pre_filter_async(signal, market, max_daily_trades, max_daily_loss_pct, user_id, disabled_checks))


def _check_cooldown(signal: TradingViewSignal, cooldown_seconds: int = 300, user_id: str | None = None) -> bool:
    """Check if we received a similar signal recently (thread-safe)."""
    cutoff = utcnow() - timedelta(seconds=cooldown_seconds)
    scope = user_id or "admin"
    with _state_lock:
        global _recent_signals
        _recent_signals = [s for s in _recent_signals if s["timestamp"] > cutoff]
        for s in _recent_signals:
            if (
                s.get("user_id", "admin") == scope
                and s["ticker"] == signal.ticker
                and s["direction"] == signal.direction
            ):
                return False
    return True


def _count_recent_same_direction(signal: TradingViewSignal, window_minutes: int = 60, user_id: str | None = None) -> int:
    """Count how many signals of the same direction we received recently (thread-safe)."""
    cutoff = utcnow() - timedelta(minutes=window_minutes)
    scope = user_id or "admin"
    with _state_lock:
        return sum(
            1 for s in _recent_signals
            if s["timestamp"] > cutoff and s.get("user_id", "admin") == scope and s["direction"] == signal.direction
        )
