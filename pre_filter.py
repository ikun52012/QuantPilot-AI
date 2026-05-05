"""
QuantPilot AI - Pre-Filter (Rule-Based Layer)
Fast, free, rule-based checks BEFORE calling the AI.
Enhanced v4: 29 intelligent checks with configurable thresholds, weighted scoring,
dynamic thresholds per ticker, and blocking statistics.
"""
import asyncio
import json
import os
import threading
import time
from collections import deque
from datetime import timedelta
from typing import Any

from loguru import logger
from sqlalchemy.exc import SQLAlchemyError

from core.account_risk import check_account_loss_limits
from core.utils.common import position_symbol_key
from core.utils.datetime import utcnow
from models import MarketContext, PreFilterResult, SignalDirection, TradingViewSignal
from trade_logger import get_recent_trade_results_async, get_today_pnl_async

_filter_stats_lock = threading.Lock()
_filter_stats: dict[str, dict[str, int]] = {}
_filter_stats_buffer: dict[str, dict[str, int]] = {}
_filter_stats_last_flush: float = 0.0
_STATS_FILE = "data/filter_stats.json"
_STATS_FLUSH_INTERVAL = 5.0
_MAX_RECENT_SIGNALS = 1000


def _load_filter_stats() -> dict[str, dict[str, int]]:
    """Load filter statistics from disk."""
    try:
        import os
        if os.path.exists(_STATS_FILE):
            with open(_STATS_FILE) as f:
                raw = json.load(f)
                if not isinstance(raw, dict):
                    return {}
                loaded: dict[str, dict[str, int]] = {}
                for check_name, ticker_counts in raw.items():
                    if not isinstance(check_name, str) or not isinstance(ticker_counts, dict):
                        continue
                    normalized_counts: dict[str, int] = {}
                    for ticker, count in ticker_counts.items():
                        if not isinstance(ticker, str):
                            continue
                        try:
                            key = position_symbol_key(ticker).upper() or ticker.upper()
                            normalized_counts[key] = normalized_counts.get(key, 0) + int(count)
                        except (TypeError, ValueError):
                            continue
                    loaded[check_name] = normalized_counts
                return loaded
    except (FileNotFoundError, PermissionError, OSError, json.JSONDecodeError, TypeError, ValueError):
        pass
    except Exception:
        pass
    return {}


def _save_filter_stats(stats: dict[str, dict[str, int]]) -> None:
    """Save filter statistics to disk."""
    try:
        import os
        os.makedirs("data", exist_ok=True)
        with open(_STATS_FILE, "w") as f:
            json.dump(stats, f, indent=2)
    except (OSError, PermissionError, TypeError, ValueError):
        pass
    except Exception:
        pass


def _record_filter_block(check_name: str, ticker: str) -> None:
    """Record that a filter blocked a signal (buffered writes)."""
    global _filter_stats_buffer, _filter_stats_last_flush

    with _filter_stats_lock:
        if check_name not in _filter_stats_buffer:
            _filter_stats_buffer[check_name] = {}

        key = position_symbol_key(ticker).upper() or ticker.upper()
        _filter_stats_buffer[check_name][key] = _filter_stats_buffer[check_name].get(key, 0) + 1

        now = time.time()
        if now - _filter_stats_last_flush >= _STATS_FLUSH_INTERVAL:
            _flush_filter_stats()
            _filter_stats_last_flush = now


def _flush_filter_stats() -> None:
    """Flush buffered stats to disk."""
    global _filter_stats, _filter_stats_buffer

    if not _filter_stats_buffer:
        return

    if not _filter_stats:
        _filter_stats = _load_filter_stats()

    for check_name, tickers in _filter_stats_buffer.items():
        if check_name not in _filter_stats:
            _filter_stats[check_name] = {}
        for ticker, count in tickers.items():
            _filter_stats[check_name][ticker] = _filter_stats[check_name].get(ticker, 0) + count

    _save_filter_stats(_filter_stats)
    _filter_stats_buffer = {}


def get_filter_stats() -> dict[str, dict[str, int]]:
    """Return current filter blocking statistics."""
    with _filter_stats_lock:
        merged = {check_name: dict(ticker_counts) for check_name, ticker_counts in _filter_stats.items()}
        for check_name, ticker_counts in _filter_stats_buffer.items():
            current = merged.setdefault(check_name, {})
            for ticker, count in ticker_counts.items():
                current[ticker] = current.get(ticker, 0) + count
        return merged


def reset_filter_stats() -> None:
    """Reset all filter statistics."""
    global _filter_stats, _filter_stats_buffer, _filter_stats_last_flush
    with _filter_stats_lock:
        _filter_stats = {}
        _filter_stats_buffer = {}
        _filter_stats_last_flush = 0.0
        _save_filter_stats({})


# ─────────────────────────────────────────────
# Configurable Thresholds
# ─────────────────────────────────────────────
class FilterThresholds:
    """Configurable thresholds for pre-filter checks."""

    DEFAULT_THRESHOLDS = {
        "atr_pct_max": 15.0,
        "spread_pct_max": 0.1,
        "volume_24h_min": 1_000_000,
        "price_change_1h_max": 8.0,
        "rsi_long_max": 80,
        "rsi_short_min": 20,
        "funding_rate_threshold": 0.0005,
        "orderbook_long_min": 0.4,
        "orderbook_short_max": 2.5,
        "signal_saturation_max": 3,
        "ema_diff_pct_min": 1.0,
        "consecutive_loss_max": 3,
        "cooldown_seconds": 300,
        "cooldown_win_multiplier": 0.5,
        "cooldown_loss_multiplier": 2.0,
        "price_deviation_pct_max": 2.0,
        "oi_change_pct_max": 15.0,
        "correlated_asset_change_max": 5.0,
        "whale_threshold_usd": 1_000_000,
        "min_pass_score": 0.0,
        "liquidation_distance_pct_min": 1.0,
        "long_short_ratio_extreme_high": 2.5,
        "long_short_ratio_extreme_low": 0.4,
        "basis_pct_max": 0.5,
        "fear_greed_extreme_threshold": 20,
        "cvd_divergence_threshold": 15.0,
        "volatility_regime_multiplier": 1.5,
        "position_reduce_on_loss_pct": 50.0,
        "dynamic_cooldown_enabled": True,
        "data_completeness_soft_fail_count": 5,
    }

    DYNAMIC_THRESHOLDS: dict[str, dict[str, Any]] = {
        "BTCUSDT": {"atr_pct_max": 10.0, "volume_24h_min": 50_000_000, "spread_pct_max": 0.05, "whale_threshold_usd": 5_000_000},
        "ETHUSDT": {"atr_pct_max": 12.0, "volume_24h_min": 20_000_000, "spread_pct_max": 0.05, "whale_threshold_usd": 3_000_000},
        "SOLUSDT": {"atr_pct_max": 15.0, "volume_24h_min": 5_000_000},
        "HIGH_VOLATILITY": {"atr_pct_max": 20.0, "price_change_1h_max": 12.0},
        "LOW_VOLUME": {"volume_24h_min": 500_000, "spread_pct_max": 0.15},
    }

    _instance: "FilterThresholds | None" = None
    _instance_lock = threading.Lock()
    _custom_thresholds: dict[str, Any] = {}

    def __new__(cls) -> "FilterThresholds":
        if cls._instance is None:
            with cls._instance_lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._custom_thresholds = {}
                    cls._instance._load_from_env()
        return cls._instance

    def _load_from_env(self) -> None:
        """Load thresholds from environment variables on first instantiation."""
        env_mappings = {
            "OI_CHANGE_THRESHOLD_PCT": "oi_change_pct_max",
            "CORRELATED_THRESHOLD_PCT": "correlated_asset_change_max",
            "WHALE_THRESHOLD_USD": "whale_threshold_usd",
        }
        for env_key, threshold_key in env_mappings.items():
            env_value = os.getenv(env_key)
            if env_value:
                try:
                    self._custom_thresholds[threshold_key] = float(env_value)
                except (ValueError, TypeError):
                    pass
                except Exception:
                    pass

    def get(self, key: str, ticker: str = "") -> Any:
        """Get threshold value, applying dynamic adjustments."""
        ticker_upper = ticker.upper().strip()
        if ticker_upper not in self.DYNAMIC_THRESHOLDS:
            ticker_upper = position_symbol_key(ticker).upper() or ticker_upper

        if key in self._custom_thresholds:
            return self._custom_thresholds[key]

        base_value = self.DEFAULT_THRESHOLDS.get(key)

        if ticker_upper in self.DYNAMIC_THRESHOLDS:
            ticker_overrides = self.DYNAMIC_THRESHOLDS[ticker_upper]
            if key in ticker_overrides:
                return ticker_overrides[key]

        return base_value

    def set_custom(self, key: str, value: Any) -> None:
        """Set a custom threshold override."""
        with self._instance_lock:
            self._custom_thresholds[key] = value

    def clear_custom(self, key: str | None = None) -> None:
        """Clear custom threshold(s)."""
        with self._instance_lock:
            if key:
                self._custom_thresholds.pop(key, None)
            else:
                self._custom_thresholds.clear()

    def load_from_dict(self, data: dict[str, Any]) -> None:
        """Load thresholds from a dictionary."""
        with self._instance_lock:
            for key, value in data.items():
                if key in self.DEFAULT_THRESHOLDS:
                    self._custom_thresholds[key] = value

    def reload_from_dict(self, data: dict[str, Any] | None = None) -> None:
        """Reload custom thresholds from env plus persisted overrides."""
        with self._instance_lock:
            self._custom_thresholds = {}
            self._load_from_env()
            for key, value in (data or {}).items():
                if key in self.DEFAULT_THRESHOLDS:
                    self._custom_thresholds[key] = value

    def to_dict(self) -> dict[str, Any]:
        """Return all thresholds as a dictionary."""
        with self._instance_lock:
            result = dict(self.DEFAULT_THRESHOLDS)
            result.update(self._custom_thresholds)
            return result

    @classmethod
    def instance(cls) -> "FilterThresholds":
        return cls()


def get_thresholds() -> FilterThresholds:
    """Get the global thresholds instance."""
    return FilterThresholds.instance()


# ─────────────────────────────────────────────
# Weighted Scoring System
# ─────────────────────────────────────────────
FILTER_WEIGHTS: dict[str, float] = {
    "daily_trade_limit": 10.0,
    "daily_loss_limit": 10.0,
    "cooldown": 5.0,
    "price_sanity": 8.0,
    "volatility_guard": 8.0,
    "spread": 6.0,
    "volume": 5.0,
    "sudden_move": 7.0,
    "rsi_extreme": 6.0,
    "funding_rate": 5.0,
    "orderbook_imbalance": 7.0,
    "market_hours": 4.0,
    "consecutive_loss": 9.0,
    "signal_saturation": 5.0,
    "ema_alignment": 6.0,
    "market_structure": 8.0,
    "oi_change": 6.0,
    "correlated_assets": 4.0,
    "whale_activity": 5.0,
    "macro_events": 10.0,
    "liquidation_heatmap": 7.0,
    "long_short_ratio": 6.0,
    "cvd_divergence": 7.0,
    "basis_check": 5.0,
    "fear_greed": 4.0,
    "volatility_regime": 6.0,
    "data_completeness": 6.0,
}


def calculate_filter_score(checks: dict[str, dict]) -> float:
    """Calculate weighted score from filter results. Returns 0-100."""
    active_weight = 0.0
    earned_weight = 0.0

    for check_name, check_data in checks.items():
        weight = FILTER_WEIGHTS.get(check_name, 5.0)
        if check_data.get("disabled", False):
            continue
        active_weight += weight
        if check_data.get("passed", True):
            earned_weight += weight
        elif check_data.get("soft_fail", False):
            earned_weight += weight * 0.5

    return (earned_weight / active_weight) * 100.0 if active_weight > 0 else 100.0


_state_lock = threading.Lock()
_recent_signals: deque[dict[str, Any]] = deque(maxlen=_MAX_RECENT_SIGNALS)
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
    """
    Update the in-memory daily PnL counter.

    Note: This is now used as a secondary cache. Primary source is
    always the database via get_today_pnl_async() which queries
    closed trades from TradeModel.
    """
    global _daily_pnl, _daily_trade_date
    with _state_lock:
        today = utcnow().strftime("%Y-%m-%d")
        if today != _daily_trade_date:
            reset_daily_counters()
        _daily_pnl += float(pnl or 0)


async def count_today_executed_trades_async(user_id: str | None = None) -> int:
    """Count today's executed trades from the async database."""
    from core.database import count_today_executed_trades, db_manager

    try:
        async with db_manager.async_session_factory() as session:
            return await count_today_executed_trades(session, user_id)
    except SQLAlchemyError as e:
        logger.warning(f"[PreFilter] Database count failed, using in-memory fallback: {e}")
        with _state_lock:
            today = utcnow().strftime("%Y-%m-%d")
            if today != _daily_trade_date:
                reset_daily_counters()
            return _daily_trade_count
    except Exception as e:
        logger.warning(f"[PreFilter] Database count failed, using in-memory fallback: {e}")
        with _state_lock:
            today = utcnow().strftime("%Y-%m-%d")
            if today != _daily_trade_date:
                reset_daily_counters()
            return _daily_trade_count


async def run_pre_filter_async(
    signal: TradingViewSignal,
    market: MarketContext,
    max_daily_trades: int = 10,
    max_daily_loss_pct: float = 5.0,
    user_id: str | None = None,
    disabled_checks: set[str] | list[str] | tuple[str, ...] | None = None,
    use_scoring: bool = False,
    min_pass_score: float | None = None,
    live_trading: bool = False,
    data_quality_mode: str | None = None,
    max_missing_data_checks: int | None = None,
) -> PreFilterResult:
    """
    Run fast rule-based checks on the incoming signal (async version).

    Args:
        use_scoring: If True, use weighted scoring instead of hard pass/fail
        min_pass_score: Minimum score (0-100) required to pass when scoring mode enabled

    Returns PreFilterResult with pass/fail, score, and detailed reasons.
    """
    global _daily_trade_count, _daily_trade_date

    thresholds = get_thresholds()
    checks: dict[str, dict[str, Any]] = {}
    reasons: list[str] = []
    soft_fail_reasons: list[str] = []
    ticker = signal.ticker.upper()

    has_price_data = market.current_price > 0
    has_volume_data = market.volume_24h > 0
    has_atr_data = market.atr_pct is not None
    has_rsi_data = market.rsi_1h is not None
    has_spread_data = market.bid_ask_spread > 0
    has_orderbook_data = market.orderbook_imbalance is not None
    has_funding_data = market.funding_rate is not None
    has_oi_data = market.open_interest_change_pct is not None
    has_ema_data = market.ema_fast is not None and market.ema_slow is not None

    missing_data_checks = []
    unavailable_data_checks = []

    # ── Check 1: Daily trade limit ──
    try:
        daily_count_snapshot = await count_today_executed_trades_async(user_id=user_id)
    except (SQLAlchemyError, ConnectionError, TimeoutError, OSError):
        with _state_lock:
            today = utcnow().strftime("%Y-%m-%d")
            if today != _daily_trade_date:
                reset_daily_counters()
            daily_count_snapshot = _daily_trade_count
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
        _record_filter_block("daily_trade_limit", ticker)

    account_equity = float(getattr(market, 'account_equity_usdt', 0) or 10000)
    current_pnl = await get_today_pnl_async(user_id=user_id, account_equity_usdt=account_equity)
    loss_ok = current_pnl > -max_daily_loss_pct
    checks["daily_loss_limit"] = {
        "passed": loss_ok,
        "current_pnl": current_pnl,
        "current_pnl_usdt": current_pnl * account_equity / 100.0,
        "account_equity": account_equity,
        "max_loss": max_daily_loss_pct,
    }
    if not loss_ok:
        pnl_usdt = current_pnl * account_equity / 100.0
        reasons.append(
            f"Daily loss limit reached ({current_pnl:.2f}% / {abs(pnl_usdt):.2f} USDT / {account_equity:.2f} USDT equity "
            f"/ -{max_daily_loss_pct}%)"
        )
        _record_filter_block("daily_loss_limit", ticker)

    loss_allowed, loss_reason = await check_account_loss_limits(
        user_id=user_id,
        account_equity_usdt=account_equity,
        max_daily_loss_pct=max_daily_loss_pct,
        max_total_loss_pct=None,
    )
    checks["account_daily_loss_limit"] = {
        "passed": loss_allowed,
        "reason": loss_reason,
    }
    if not loss_allowed:
        reasons.append(loss_reason)
        _record_filter_block("account_daily_loss_limit", ticker)

    # ── Check 3: Duplicate signal cooldown (Dynamic) ──
    base_cooldown = thresholds.get("cooldown_seconds", ticker)
    dynamic_enabled = thresholds.get("dynamic_cooldown_enabled", ticker)

    if dynamic_enabled:
        win_multiplier = thresholds.get("cooldown_win_multiplier", ticker)
        loss_multiplier = thresholds.get("cooldown_loss_multiplier", ticker)
        try:
            recent_results = await get_recent_trade_results_async(limit=5, user_id=user_id, ticker=ticker)
            if recent_results:
                last_pnl = recent_results[0].get("pnl_pct", 0) if recent_results else 0
                if last_pnl > 0:
                    cooldown_secs = int(base_cooldown * win_multiplier)
                elif last_pnl < 0:
                    cooldown_secs = int(base_cooldown * loss_multiplier)
                else:
                    cooldown_secs = base_cooldown
            else:
                cooldown_secs = base_cooldown
        except (SQLAlchemyError, ConnectionError, TimeoutError, OSError, ValueError, TypeError):
            cooldown_secs = base_cooldown
        except Exception:
            cooldown_secs = base_cooldown
    else:
        cooldown_secs = base_cooldown

    cooldown_ok = _check_cooldown(signal, cooldown_seconds=cooldown_secs, user_id=user_id)
    checks["cooldown"] = {
        "passed": cooldown_ok,
        "cooldown_seconds": cooldown_secs,
        "base_cooldown": base_cooldown,
        "dynamic_enabled": dynamic_enabled,
    }
    if not cooldown_ok:
        reasons.append(f"Duplicate signal within {cooldown_secs}s cooldown (dynamic)")
        _record_filter_block("cooldown", ticker)

    # ── Check 4: Price sanity check ──
    price_ok = True
    price_deviation_max = thresholds.get("price_deviation_pct_max", ticker)
    if has_price_data and signal.price > 0:
        price_diff = abs(signal.price - market.current_price) / market.current_price * 100
        price_ok = price_diff < price_deviation_max
        checks["price_sanity"] = {
            "passed": price_ok,
            "signal_price": signal.price,
            "market_price": market.current_price,
            "diff_pct": round(price_diff, 4),
            "threshold": price_deviation_max,
        }
        if not price_ok:
            reasons.append(f"Signal price deviates {price_diff:.2f}% from market")
            _record_filter_block("price_sanity", ticker)
    elif not has_price_data:
        checks["price_sanity"] = {"passed": True, "missing_data": True, "note": "No price data available"}
        missing_data_checks.append("price_sanity")

    # ── Check 5: Extreme volatility guard ──
    vol_ok = True
    atr_max = thresholds.get("atr_pct_max", ticker)
    if has_atr_data:
        vol_ok = market.atr_pct < atr_max
        checks["volatility_guard"] = {
            "passed": vol_ok,
            "atr_pct": market.atr_pct,
            "threshold": atr_max,
        }
        if not vol_ok:
            reasons.append(f"Extreme volatility: ATR% = {market.atr_pct:.2f}% > {atr_max}%")
            _record_filter_block("volatility_guard", ticker)
    elif not has_atr_data:
        checks["volatility_guard"] = {"passed": True, "missing_data": True, "note": "No ATR data available"}
        missing_data_checks.append("volatility_guard")

    # ── Check 6: Spread check ──
    spread_ok = True
    spread_max = thresholds.get("spread_pct_max", ticker)
    if has_spread_data:
        spread_ok = market.bid_ask_spread < spread_max
        checks["spread"] = {
            "passed": spread_ok,
            "spread_pct": market.bid_ask_spread,
            "threshold": spread_max,
        }
        if not spread_ok:
            soft_fail_reasons.append(f"Spread wide: {market.bid_ask_spread:.4f}% (soft fail)")
            checks["spread"]["soft_fail"] = True
    elif not has_spread_data:
        checks["spread"] = {"passed": True, "missing_data": True, "note": "No spread data available"}
        missing_data_checks.append("spread")

    # ── Check 7: Volume sanity ──
    volume_ok = True
    volume_min = thresholds.get("volume_24h_min", ticker)
    if has_volume_data:
        volume_ok = market.volume_24h > volume_min
        checks["volume"] = {
            "passed": volume_ok,
            "volume_24h": market.volume_24h,
            "threshold": volume_min,
        }
        if not volume_ok:
            soft_fail_reasons.append(f"Low volume: ${market.volume_24h:,.0f} (soft fail)")
            checks["volume"]["soft_fail"] = True
    elif not has_volume_data:
        checks["volume"] = {"passed": True, "missing_data": True, "note": "No volume data available"}
        missing_data_checks.append("volume")

    # ── Check 8: Large sudden move guard ──
    sudden_move_ok = True
    move_max = thresholds.get("price_change_1h_max", ticker)
    if market.price_change_1h != 0:
        sudden_move_ok = abs(market.price_change_1h) < move_max
        checks["sudden_move"] = {
            "passed": sudden_move_ok,
            "price_change_1h": market.price_change_1h,
            "threshold": move_max,
        }
        if not sudden_move_ok:
            reasons.append(f"Sudden move: {market.price_change_1h:+.2f}% in 1h")
            _record_filter_block("sudden_move", ticker)

    # ═══════════════════════════════════════════
    # ENHANCED CHECKS (v3)
    # ═══════════════════════════════════════════

    # ── Check 9: RSI Extreme Guard ──
    rsi_ok = True
    rsi_long_max = thresholds.get("rsi_long_max", ticker)
    rsi_short_min = thresholds.get("rsi_short_min", ticker)
    if has_rsi_data:
        is_long = signal.direction in (SignalDirection.LONG,)
        is_short = signal.direction in (SignalDirection.SHORT,)

        if is_long and market.rsi_1h > rsi_long_max:
            rsi_ok = False
        elif is_short and market.rsi_1h < rsi_short_min:
            rsi_ok = False

        checks["rsi_extreme"] = {
            "passed": rsi_ok,
            "rsi_1h": market.rsi_1h,
            "direction": signal.direction.value,
            "thresholds": {"long_max": rsi_long_max, "short_min": rsi_short_min},
        }
        if not rsi_ok:
            soft_fail_reasons.append(f"RSI extreme: {market.rsi_1h:.1f} conflicts with {signal.direction.value} (soft fail)")
            checks["rsi_extreme"]["soft_fail"] = True
            _record_filter_block("rsi_extreme", ticker)
    elif not has_rsi_data:
        checks["rsi_extreme"] = {"passed": True, "missing_data": True, "note": "No RSI data available"}
        missing_data_checks.append("rsi_extreme")

    # ── Check 10: Funding Rate Guard ──
    funding_ok = True
    funding_threshold = thresholds.get("funding_rate_threshold", ticker)
    if has_funding_data:
        is_long = signal.direction in (SignalDirection.LONG,)
        is_short = signal.direction in (SignalDirection.SHORT,)

        if is_long and market.funding_rate > funding_threshold:
            funding_ok = False
        elif is_short and market.funding_rate < -funding_threshold:
            funding_ok = False

        checks["funding_rate"] = {
            "passed": funding_ok,
            "funding_rate": market.funding_rate,
            "direction": signal.direction.value,
            "threshold": funding_threshold,
        }
        if not funding_ok:
            soft_fail_reasons.append(f"Funding rate extreme: {market.funding_rate*100:.4f}% (soft fail)")
            checks["funding_rate"]["soft_fail"] = True
    elif not has_funding_data:
        checks["funding_rate"] = {"passed": True, "missing_data": True, "note": "No funding rate data available"}
        missing_data_checks.append("funding_rate")

    # ── Check 11: Orderbook Imbalance Guard ──
    ob_ok = True
    ob_long_min = thresholds.get("orderbook_long_min", ticker)
    ob_short_max = thresholds.get("orderbook_short_max", ticker)
    if has_orderbook_data and market.orderbook_imbalance > 0:
        is_long = signal.direction in (SignalDirection.LONG,)
        is_short = signal.direction in (SignalDirection.SHORT,)

        if is_long and market.orderbook_imbalance < ob_long_min:
            ob_ok = False
        elif is_short and market.orderbook_imbalance > ob_short_max:
            ob_ok = False

        checks["orderbook_imbalance"] = {
            "passed": ob_ok,
            "imbalance_ratio": market.orderbook_imbalance,
            "direction": signal.direction.value,
            "thresholds": {"long_min": ob_long_min, "short_max": ob_short_max},
        }
        if not ob_ok:
            soft_fail_reasons.append(f"Orderbook imbalance {market.orderbook_imbalance:.2f} against {signal.direction.value} (soft fail)")
            checks["orderbook_imbalance"]["soft_fail"] = True
            _record_filter_block("orderbook_imbalance", ticker)
    elif not has_orderbook_data:
        checks["orderbook_imbalance"] = {"passed": True, "missing_data": True, "note": "No orderbook data available"}
        missing_data_checks.append("orderbook_imbalance")

    # ── Check 12: Weekend / Low Liquidity Hours Guard ──
    time_ok = True
    now_utc = utcnow()
    is_weekend = now_utc.weekday() >= 5
    is_low_liq_hour = now_utc.hour >= 21 or now_utc.hour < 1

    if is_weekend and market.volume_24h > 0:
        weekend_vol_ok = market.volume_24h > 5_000_000
        if not weekend_vol_ok:
            time_ok = False

    if is_low_liq_hour and market.bid_ask_spread > 0.05:
        time_ok = False

    checks["market_hours"] = {
        "passed": time_ok,
        "is_weekend": is_weekend,
        "is_low_liquidity_hour": is_low_liq_hour,
        "hour_utc": now_utc.hour,
        "day": now_utc.strftime("%A"),
    }
    if not time_ok:
        soft_fail_reasons.append("Low liquidity period (soft fail)")
        checks["market_hours"]["soft_fail"] = True

    # ── Check 13: Consecutive Loss Protection (Smart) ──
    consec_ok = True
    consec_max = thresholds.get("consecutive_loss_max", ticker)
    position_reduce_pct = thresholds.get("position_reduce_on_loss_pct", ticker)
    consec_losses = 0
    try:
        recent_results = await get_recent_trade_results_async(limit=5, user_id=user_id)
        consec_losses = sum(1 for r in recent_results[:consec_max] if r.get("pnl_pct", 0) < 0)

        if len(recent_results) >= consec_max:
            last_n = recent_results[:consec_max]
            if all(r.get("pnl_pct", 0) < 0 for r in last_n):
                consec_ok = False

        position_suggestion = "normal"
        if consec_losses >= 2:
            position_suggestion = f"reduce_by_{int(position_reduce_pct)}%"
        if consec_losses >= consec_max - 1:
            position_suggestion = "pause_or_minimal"

        checks["consecutive_loss"] = {
            "passed": consec_ok,
            "recent_results": len(recent_results),
            "consecutive_losses": consec_losses,
            "threshold": consec_max,
            "position_suggestion": position_suggestion,
            "reduce_pct": position_reduce_pct if consec_losses >= 2 else 0,
        }
        if not consec_ok:
            reasons.append(f"{consec_max} consecutive losses — cooling off, suggest {position_suggestion}")
            _record_filter_block("consecutive_loss", ticker)
        elif consec_losses >= 2:
            soft_fail_reasons.append(f"{consec_losses} recent losses — suggest reduce position by {position_reduce_pct}%")
            checks["consecutive_loss"]["soft_fail"] = True
    except (SQLAlchemyError, ConnectionError, TimeoutError, OSError, TypeError, ValueError, AttributeError):
        checks["consecutive_loss"] = {"passed": True, "note": "Could not check"}
    except Exception:
        checks["consecutive_loss"] = {"passed": True, "note": "Could not check"}

    # ── Check 14: Same-Direction Signal Saturation (with Reverse Detection) ──
    saturation_ok = True
    saturation_max = thresholds.get("signal_saturation_max", ticker)
    same_dir_count = _count_recent_same_direction(signal, window_minutes=60, user_id=user_id)
    opposite_dir_count = _count_recent_opposite_direction(signal, window_minutes=60, user_id=user_id)

    if same_dir_count >= saturation_max:
        saturation_ok = False

    reverse_signal_boost = False
    if opposite_dir_count >= saturation_max - 1 and same_dir_count < saturation_max:
        reverse_signal_boost = True

    checks["signal_saturation"] = {
        "passed": saturation_ok,
        "same_direction_last_hour": same_dir_count,
        "opposite_direction_last_hour": opposite_dir_count,
        "threshold": saturation_max,
        "reverse_signal_boost": reverse_signal_boost,
    }
    if not saturation_ok:
        soft_fail_reasons.append(f"Signal saturation: {same_dir_count} {signal.direction.value} in 1h (soft fail)")
        checks["signal_saturation"]["soft_fail"] = True
    elif reverse_signal_boost:
        soft_fail_reasons.append(f"Reverse signal opportunity: {opposite_dir_count} opposite signals recently")
        checks["signal_saturation"]["note"] = "reverse_opportunity"

    # ── Check 15: EMA Trend Alignment ──
    ema_ok = True
    ema_diff_min = thresholds.get("ema_diff_pct_min", ticker)
    if has_ema_data:
        is_long = signal.direction in (SignalDirection.LONG,)
        is_short = signal.direction in (SignalDirection.SHORT,)

        ema_bullish = market.ema_fast > market.ema_slow
        ema_bearish = market.ema_fast < market.ema_slow
        ema_diff_pct = abs(market.ema_fast - market.ema_slow) / market.ema_slow * 100 if market.ema_slow > 0 else 0

        if is_long and ema_bearish and ema_diff_pct > ema_diff_min:
            ema_ok = False
        elif is_short and ema_bullish and ema_diff_pct > ema_diff_min:
            ema_ok = False

        checks["ema_alignment"] = {
            "passed": ema_ok,
            "ema_fast": market.ema_fast,
            "ema_slow": market.ema_slow,
            "ema_diff_pct": round(ema_diff_pct, 4),
            "trend": "bullish" if ema_bullish else "bearish",
            "threshold": ema_diff_min,
        }
        if not ema_ok:
            soft_fail_reasons.append("EMA trend conflict (soft fail)")
            checks["ema_alignment"]["soft_fail"] = True
    elif not has_ema_data:
        checks["ema_alignment"] = {"passed": True, "missing_data": True, "note": "No EMA data available"}
        missing_data_checks.append("ema_alignment")

    # ── Check 16: Market Structure (SMC) Validation ──
    structure_ok = True
    try:
        ohlcv_4h = getattr(market, "_ohlcv_4h", None) or []
        ohlcv_1h = getattr(market, "_ohlcv_1h", None) or []

        if len(ohlcv_4h) >= 10 or len(ohlcv_1h) >= 10:
            from smc_analyzer import detect_market_structure

            htf_ohlcv = ohlcv_4h if len(ohlcv_4h) >= 10 else ohlcv_1h
            htf_label = "4h" if len(ohlcv_4h) >= 10 else "1h"
            structure = detect_market_structure(htf_ohlcv, htf_label)

            is_long = signal.direction in (SignalDirection.LONG,)
            is_short = signal.direction in (SignalDirection.SHORT,)

            if is_long and structure.trend == "bearish" and not structure.last_choch:
                structure_ok = False
            elif is_short and structure.trend == "bullish" and not structure.last_choch:
                structure_ok = False

            checks["market_structure"] = {
                "passed": structure_ok,
                "htf_trend": structure.trend,
                "timeframe": htf_label,
                "last_bos": structure.last_bos,
                "last_choch": structure.last_choch,
            }
            if not structure_ok:
                soft_fail_reasons.append(f"HTF structure {structure.trend} conflicts (no CHoCH) (soft fail)")
                checks["market_structure"]["soft_fail"] = True
                _record_filter_block("market_structure", ticker)
    except Exception as e:
        checks["market_structure"] = {"passed": True, "note": f"Skip: {e}"}

    # ── Check 17: Open Interest Change (NEW) ──
    oi_ok = True
    oi_max = thresholds.get("oi_change_pct_max", ticker)
    if has_oi_data:
        oi_ok = abs(market.open_interest_change_pct) < oi_max
        checks["oi_change"] = {
            "passed": oi_ok,
            "oi_change_pct": market.open_interest_change_pct,
            "threshold": oi_max,
            "note": "Large OI changes indicate potential squeeze or reversal",
        }
        if not oi_ok:
            soft_fail_reasons.append(f"OI change: {market.open_interest_change_pct:+.2f}% (soft fail)")
            checks["oi_change"]["soft_fail"] = True
    elif not has_oi_data:
        checks["oi_change"] = {"passed": True, "missing_data": True, "note": "No OI data available"}
        missing_data_checks.append("oi_change")

    # ── Check 18: Correlated Assets Check (NEW) ──
    correlated_ok = True
    corr_max = thresholds.get("correlated_asset_change_max", ticker)
    correlated_data = getattr(market, "_correlated_assets", None) or {}
    if correlated_data:
        btc_change = correlated_data.get("BTC_change_1h", 0)
        eth_change = correlated_data.get("ETH_change_1h", 0)

        is_long = signal.direction in (SignalDirection.LONG,)
        is_short = signal.direction in (SignalDirection.SHORT,)

        if is_long and (btc_change < -corr_max or eth_change < -corr_max):
            correlated_ok = False
        elif is_short and (btc_change > corr_max or eth_change > corr_max):
            correlated_ok = False

        checks["correlated_assets"] = {
            "passed": correlated_ok,
            "btc_change_1h": btc_change,
            "eth_change_1h": eth_change,
            "threshold": corr_max,
            "note": "Correlated market movement against signal direction",
        }
        if not correlated_ok:
            soft_fail_reasons.append("Correlated assets moving opposite (soft fail)")
            checks["correlated_assets"]["soft_fail"] = True

    # ── Check 19: Whale Activity / Large Transactions (NEW) ──
    whale_ok = True
    whale_data = getattr(market, "_whale_activity", None) or {}
    whale_threshold = thresholds.get("whale_threshold_usd", ticker)

    if whale_data:
        large_transfers_1h = whale_data.get("large_transfers_1h", 0)
        net_flow = whale_data.get("net_flow_24h", 0)

        is_long = signal.direction in (SignalDirection.LONG,)
        is_short = signal.direction in (SignalDirection.SHORT,)

        net_flow_threshold = whale_threshold

        if is_long and net_flow < -net_flow_threshold:
            whale_ok = False
        elif is_short and net_flow > net_flow_threshold:
            whale_ok = False

        checks["whale_activity"] = {
            "passed": whale_ok,
            "large_transfers_1h": large_transfers_1h,
            "net_flow_24h": net_flow,
            "threshold_used": whale_threshold,
            "note": "Large net flow against signal direction",
        }
        if not whale_ok:
            soft_fail_reasons.append(f"Whale flow opposite: ${abs(net_flow):,.0f} (threshold: ${whale_threshold:,.0f})")
            checks["whale_activity"]["soft_fail"] = True

    # ═══════════════════════════════════════════
    # ENHANCED CHECKS (v4) - New Market Data
    # ═══════════════════════════════════════════

    async def _enhanced_call(name: str, coro):
        try:
            return await asyncio.wait_for(coro, timeout=6.0)
        except asyncio.TimeoutError:
            logger.warning(f"[PreFilter] Enhanced check {name} timed out")
            return TimeoutError(f"{name} timed out")
        except Exception as exc:
            return exc

    try:
        from enhanced_market_data import (
            check_macro_event_risk,
            fetch_basis_data,
            fetch_fear_greed_index,
            fetch_liquidation_heatmap,
            fetch_long_short_ratio,
        )

        macro_result, liq_result, ls_result, basis_result, fg_result = await asyncio.gather(
            _enhanced_call("macro_events", check_macro_event_risk()),
            _enhanced_call("liquidation_heatmap", fetch_liquidation_heatmap(ticker)),
            _enhanced_call("long_short_ratio", fetch_long_short_ratio(ticker)),
            _enhanced_call("basis_check", fetch_basis_data(ticker)),
            _enhanced_call("fear_greed", fetch_fear_greed_index()),
        )
    except Exception as e:
        macro_result = liq_result = ls_result = basis_result = fg_result = e

    # ── Check 20: Macro Events Risk ──
    if isinstance(macro_result, Exception):
        checks["macro_events"] = {"passed": True, "note": f"Skip: {macro_result}"}
        unavailable_data_checks.append("macro_events")
    else:
        macro_ok, macro_reason = macro_result
        checks["macro_events"] = {
            "passed": macro_ok,
            "reason": macro_reason,
        }
        if not macro_ok:
            reasons.append(f"Macro event risk: {macro_reason}")
            _record_filter_block("macro_events", ticker)

    # ── Check 21: Liquidation Heatmap ──
    liq_ok = True
    liq_distance_min = thresholds.get("liquidation_distance_pct_min", ticker)
    if isinstance(liq_result, Exception):
        checks["liquidation_heatmap"] = {"passed": True, "note": f"Skip: {liq_result}"}
        unavailable_data_checks.append("liquidation_heatmap")
    else:
        liq_data = liq_result
        liq_data.get("nearest_liq_level")
        nearest_distance = liq_data.get("nearest_liq_distance_pct")
        total_liq = liq_data.get("total_long_liq_usd", 0) + liq_data.get("total_short_liq_usd", 0)

        if nearest_distance is not None and nearest_distance < liq_distance_min:
            if total_liq > 10_000_000:
                liq_ok = False

        checks["liquidation_heatmap"] = {
            "passed": liq_ok,
            "nearest_liq_distance_pct": nearest_distance,
            "total_liq_usd": liq_data.get("total_long_liq_usd", 0) + liq_data.get("total_short_liq_usd", 0),
            "threshold_distance": liq_distance_min,
        }
        if not liq_ok:
            soft_fail_reasons.append(f"Large liquidations nearby (${total_liq/1e6:.1f}M within {nearest_distance:.1f}%)")
            checks["liquidation_heatmap"]["soft_fail"] = True

    # ── Check 22: Long/Short Ratio Extreme ──
    ls_ok = True
    ls_high = thresholds.get("long_short_ratio_extreme_high", ticker)
    ls_low = thresholds.get("long_short_ratio_extreme_low", ticker)
    if isinstance(ls_result, Exception):
        checks["long_short_ratio"] = {"passed": True, "note": f"Skip: {ls_result}"}
        unavailable_data_checks.append("long_short_ratio")
    else:
        ls_data = ls_result
        current_ratio = ls_data.get("current_ratio")

        if current_ratio is not None:
            is_long = signal.direction in (SignalDirection.LONG,)
            is_short = signal.direction in (SignalDirection.SHORT,)

            if is_long and current_ratio > ls_high:
                ls_ok = False
            elif is_short and current_ratio < ls_low:
                ls_ok = False

        checks["long_short_ratio"] = {
            "passed": ls_ok,
            "current_ratio": current_ratio,
            "long_pct": ls_data.get("long_accounts_pct"),
            "short_pct": ls_data.get("short_accounts_pct"),
            "thresholds": {"high": ls_high, "low": ls_low},
        }
        if not ls_ok:
            soft_fail_reasons.append(f"Long/Short ratio extreme: {current_ratio:.2f} (soft fail)")
            checks["long_short_ratio"]["soft_fail"] = True

    # ── Check 24: Basis (Spot vs Futures) ──
    basis_ok = True
    basis_max = thresholds.get("basis_pct_max", ticker)
    if isinstance(basis_result, Exception):
        checks["basis_check"] = {"passed": True, "note": f"Skip: {basis_result}"}
        unavailable_data_checks.append("basis_check")
    else:
        basis_data = basis_result
        basis_pct = basis_data.get("basis_pct")

        if basis_pct is not None:
            basis_ok = abs(basis_pct) < basis_max

        checks["basis_check"] = {
            "passed": basis_ok,
            "basis_pct": basis_pct,
            "spot_price": basis_data.get("spot_price"),
            "futures_price": basis_data.get("futures_price"),
            "threshold": basis_max,
        }
        if not basis_ok:
            soft_fail_reasons.append(f"Basis abnormal: {basis_pct:.3f}% (soft fail)")
            checks["basis_check"]["soft_fail"] = True

    # ── Check 25: Fear & Greed Index ──
    fg_ok = True
    fg_threshold = thresholds.get("fear_greed_extreme_threshold", ticker)
    if isinstance(fg_result, Exception):
        checks["fear_greed"] = {"passed": True, "note": f"Skip: {fg_result}"}
        unavailable_data_checks.append("fear_greed")
    else:
        fg_data = fg_result
        fg_value = fg_data.get("value")
        fg_class = fg_data.get("classification")

        is_long = signal.direction in (SignalDirection.LONG,)
        is_short = signal.direction in (SignalDirection.SHORT,)

        if fg_value is not None:
            if fg_value <= fg_threshold and is_long:
                fg_ok = False
            elif fg_value >= 80 and is_short:
                fg_ok = False

        checks["fear_greed"] = {
            "passed": fg_ok,
            "value": fg_value,
            "classification": fg_class,
            "threshold": fg_threshold,
        }
        if not fg_ok:
            soft_fail_reasons.append(f"Fear & Greed extreme: {fg_value} ({fg_class})")
            checks["fear_greed"]["soft_fail"] = True

    # ── Check 23: CVD Divergence ──
    cvd_ok = True
    cvd_threshold = thresholds.get("cvd_divergence_threshold", ticker)
    try:
        ohlcv_1h = getattr(market, "_ohlcv_1h", None) or []
        if len(ohlcv_1h) >= 20:
            from enhanced_market_data import calculate_cvd_divergence
            cvd_data = await calculate_cvd_divergence(ohlcv_1h)
            divergence = cvd_data.get("divergence")
            strength = cvd_data.get("strength", 0)
            div_type = cvd_data.get("type")

            if divergence and strength > cvd_threshold:
                is_long = signal.direction in (SignalDirection.LONG,)
                is_short = signal.direction in (SignalDirection.SHORT,)

                if is_long and div_type == "bearish":
                    cvd_ok = False
                elif is_short and div_type == "bullish":
                    cvd_ok = False

            checks["cvd_divergence"] = {
                "passed": cvd_ok,
                "divergence_type": div_type,
                "strength": strength,
                "price_change_pct": cvd_data.get("price_change_pct"),
                "threshold": cvd_threshold,
            }
            if not cvd_ok:
                soft_fail_reasons.append(f"CVD divergence: {div_type} (${strength:.1f}%)")
                checks["cvd_divergence"]["soft_fail"] = True
        else:
            checks["cvd_divergence"] = {"passed": True, "missing_data": True, "note": "Need at least 20 1h candles"}
            missing_data_checks.append("cvd_divergence")
    except Exception as e:
        checks["cvd_divergence"] = {"passed": True, "note": f"Skip: {e}"}
        unavailable_data_checks.append("cvd_divergence")

    # ── Check 26: Volatility Regime ──
    regime_ok = True
    vol_multiplier = thresholds.get("volatility_regime_multiplier", ticker)
    try:
        ohlcv_1h = getattr(market, "_ohlcv_1h", None) or []
        if len(ohlcv_1h) >= 100:
            from enhanced_market_data import detect_volatility_regime
            regime_data = await detect_volatility_regime(ohlcv_1h)
            regime = regime_data.get("regime")
            suggestion = regime_data.get("suggestion")

            if regime == "extreme_volatility":
                regime_ok = False
            elif regime == "high_volatility" and market.atr_pct and market.atr_pct > vol_multiplier * regime_data.get("avg_atr_pct", 5):
                regime_ok = False

            checks["volatility_regime"] = {
                "passed": regime_ok,
                "regime": regime,
                "current_atr_pct": regime_data.get("current_atr_pct"),
                "avg_atr_pct": regime_data.get("avg_atr_pct"),
                "suggestion": suggestion,
            }
            if not regime_ok:
                soft_fail_reasons.append(f"Volatility regime: {regime} - {suggestion}")
                checks["volatility_regime"]["soft_fail"] = True
        else:
            checks["volatility_regime"] = {"passed": True, "missing_data": True, "note": "Need at least 100 1h candles"}
            missing_data_checks.append("volatility_regime")
    except Exception as e:
        checks["volatility_regime"] = {"passed": True, "note": f"Skip: {e}"}
        unavailable_data_checks.append("volatility_regime")

    # ── Check 27: Market Data Completeness ──
    missing_soft_fail_count = int(thresholds.get("data_completeness_soft_fail_count", ticker) or 5)
    data_complete_ok = len(missing_data_checks) < missing_soft_fail_count
    checks["data_completeness"] = {
        "passed": data_complete_ok,
        "missing_count": len(missing_data_checks),
        "unavailable_count": len(unavailable_data_checks),
        "soft_fail_threshold": missing_soft_fail_count,
        "missing_checks": missing_data_checks,
        "unavailable_checks": unavailable_data_checks,
    }
    if not data_complete_ok:
        soft_fail_reasons.append(
            f"Market data incomplete: {len(missing_data_checks)} checks missing ({', '.join(missing_data_checks[:6])})"
        )
        checks["data_completeness"]["soft_fail"] = True
        _record_filter_block("data_completeness", ticker)

    # ── Live trading data quality gate ──
    live_quality_mode = str(data_quality_mode or "warn").lower().strip()
    live_missing_limit = int(max_missing_data_checks if max_missing_data_checks is not None else 0)
    live_quality_issues = len(missing_data_checks) + len(unavailable_data_checks)
    live_quality_ok = True
    if live_trading and live_quality_mode == "fail_closed" and live_quality_issues > live_missing_limit:
        live_quality_ok = False
    checks["live_data_quality"] = {
        "passed": live_quality_ok,
        "live_trading": bool(live_trading),
        "mode": live_quality_mode,
        "issue_count": live_quality_issues,
        "max_allowed_issues": live_missing_limit,
        "missing_checks": missing_data_checks,
        "unavailable_checks": unavailable_data_checks,
    }
    if not live_quality_ok:
        reasons.append(
            f"Live data quality gate failed: {live_quality_issues} unavailable/missing checks "
            f"(max {live_missing_limit})"
        )
        _record_filter_block("live_data_quality", ticker)

    # ═══════════════════════════════════════════
    # Final Verdict
    # ═══════════════════════════════════════════

    disabled = {str(item).strip() for item in (disabled_checks or []) if str(item).strip()}
    for name in disabled:
        if name in checks:
            checks[name]["disabled"] = True
            checks[name]["passed"] = True

    score = calculate_filter_score(checks)

    hard_fail_count = sum(1 for c in checks.values() if not c.get("passed", True) and not c.get("disabled", False) and not c.get("soft_fail", False))

    if use_scoring:
        min_score = min_pass_score if min_pass_score is not None else thresholds.get("min_pass_score", ticker)
        all_passed = score >= min_score and hard_fail_count == 0
    else:
        all_passed = hard_fail_count == 0

    total_checks = len(checks)
    passed_count = sum(1 for c in checks.values() if c.get("passed", True) or c.get("disabled", False))

    all_reasons = reasons + soft_fail_reasons

    if all_passed:
        ticker_key = position_symbol_key(signal.ticker)
        with _state_lock:
            _recent_signals.append({
                "user_id": user_id or "admin",
                "ticker": signal.ticker,
                "ticker_key": ticker_key,
                "direction": signal.direction,
                "timestamp": utcnow(),
            })
        logger.info(
            f"[PreFilter] ✅ PASSED score={score:.1f} ({passed_count}/{total_checks}) "
            f"- {signal.ticker} {signal.direction.value}"
        )
    else:
        logger.warning(
            f"[PreFilter] ❌ BLOCKED score={score:.1f} ({passed_count}/{total_checks}) "
            f"- {signal.ticker} {signal.direction.value}: {'; '.join(reasons)}"
        )

    final_reason = "; ".join(all_reasons) if all_reasons else f"All {total_checks} checks passed"

    return PreFilterResult(
        passed=all_passed,
        reason=final_reason,
        checks=checks,
        score=score,
    )


def _check_cooldown(signal: TradingViewSignal, cooldown_seconds: int = 300, user_id: str | None = None) -> bool:
    """Check if we received a similar signal recently (thread-safe)."""
    cutoff = utcnow() - timedelta(seconds=cooldown_seconds)
    scope = user_id or "admin"
    target_key = position_symbol_key(signal.ticker)
    with _state_lock:
        recent = [s for s in _recent_signals if s["timestamp"] > cutoff]
        for s in recent:
            if (
                s.get("user_id", "admin") == scope
                and (s.get("ticker_key") or position_symbol_key(s.get("ticker", ""))) == target_key
                and s["direction"] == signal.direction
            ):
                return False
    return True


def _count_recent_same_direction(signal: TradingViewSignal, window_minutes: int = 60, user_id: str | None = None) -> int:
    """Count how many signals of the same direction we received recently (thread-safe)."""
    cutoff = utcnow() - timedelta(minutes=window_minutes)
    scope = user_id or "admin"
    target_key = position_symbol_key(signal.ticker)
    with _state_lock:
        return sum(
            1 for s in _recent_signals
            if (
                s["timestamp"] > cutoff
                and s.get("user_id", "admin") == scope
                and (s.get("ticker_key") or position_symbol_key(s.get("ticker", ""))) == target_key
                and s["direction"] == signal.direction
            )
        )


def _count_recent_opposite_direction(signal: TradingViewSignal, window_minutes: int = 60, user_id: str | None = None) -> int:
    """Count how many signals of the opposite direction we received recently (thread-safe)."""
    cutoff = utcnow() - timedelta(minutes=window_minutes)
    scope = user_id or "admin"
    target_key = position_symbol_key(signal.ticker)
    opposite_direction = SignalDirection.SHORT if signal.direction == SignalDirection.LONG else SignalDirection.LONG
    with _state_lock:
        return sum(
            1 for s in _recent_signals
            if (
                s["timestamp"] > cutoff
                and s.get("user_id", "admin") == scope
                and (s.get("ticker_key") or position_symbol_key(s.get("ticker", ""))) == target_key
                and s["direction"] == opposite_direction
            )
        )
