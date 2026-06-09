"""
QuantPilot AI - Pre-Filter (Rule-Based Layer) v5.0
Institutional-grade multi-check engine: fast, rule-based checks BEFORE calling the AI.

v5.0 Upgrades:
- Dynamic threshold profiles (HIGH_VOLATILITY, LOW_VOLUME) now auto-applied
- Circuit breaker / kill switch for extreme conditions
- Multi-timeframe confirmation (MTF)
- Signal velocity & block rate throttle
- Position concentration / portfolio heat check
- Signal source consistency check
- Relative volume drop detection
- Bucketed signal memory with time-based eviction
- All 18 audit findings resolved
"""
import asyncio
import json
import os
import threading
import time
from collections import deque
from datetime import timedelta
from pathlib import Path
from typing import Any

from loguru import logger
from sqlalchemy.exc import SQLAlchemyError

from core.account_risk import check_account_loss_limits
from core.config import settings
from core.utils.common import position_symbol_key
from core.utils.datetime import utcnow
from models import MarketContext, PreFilterResult, SignalDirection, TradingViewSignal
from trade_logger import get_recent_trade_results_async, get_today_pnl_async

# Filter statistics and block history
_filter_stats_lock = asyncio.Lock()
_filter_stats: dict[str, dict[str, int]] = {}
_filter_stats_buffer: dict[str, dict[str, int]] = {}
_filter_stats_last_flush: float = 0.0
_filter_stats_last_cleanup: float = 0.0
_STATS_FILE = "data/filter_stats.json"
_STATS_FLUSH_INTERVAL = 5.0
_STATS_CLEANUP_INTERVAL = 86400.0
_STATS_MAX_ENTRIES_PER_CHECK = 200

_block_history: deque[dict[str, Any]] = deque(maxlen=500)
_CIRCUIT_BREAKERS: dict[str, float] = {}
_CIRCUIT_LOCK = asyncio.Lock()


def _load_filter_stats() -> dict[str, dict[str, int]]:
    try:
        if os.path.exists(_STATS_FILE):
            with open(_STATS_FILE, encoding="utf-8") as f:
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
    return {}


def _save_filter_stats(stats: dict[str, dict[str, int]]) -> None:
    try:
        os.makedirs("data", exist_ok=True)
        stats_file = Path(_STATS_FILE)
        # Write with atomic swap to prevent corruption
        tmp_file = stats_file.with_suffix(".tmp")
        with open(tmp_file, "w", encoding="utf-8") as f:
            json.dump(stats, f, indent=2)
        tmp_file.replace(stats_file)
    except (OSError, PermissionError, TypeError, ValueError):
        pass


def _cleanup_filter_stats() -> None:
    global _filter_stats_last_cleanup
    now = time.time()
    if now - _filter_stats_last_cleanup < _STATS_CLEANUP_INTERVAL:
        return
    _filter_stats_last_cleanup = now
    for check_name in list(_filter_stats.keys()):
        tickers = _filter_stats[check_name]
        if len(tickers) > _STATS_MAX_ENTRIES_PER_CHECK:
            sorted_tickers = sorted(tickers.items(), key=lambda item: item[1], reverse=True)
            _filter_stats[check_name] = dict(sorted_tickers[:_STATS_MAX_ENTRIES_PER_CHECK])


def _flush_filter_stats() -> None:
    global _filter_stats, _filter_stats_buffer
    if not _filter_stats_buffer:
        return
    if not _filter_stats:
        _filter_stats = _load_filter_stats()
    for check_name, tickers in _filter_stats_buffer.items():
        current = _filter_stats.setdefault(check_name, {})
        for ticker, count in tickers.items():
            current[ticker] = current.get(ticker, 0) + count
    _filter_stats_buffer = {}
    _cleanup_filter_stats()
    _save_filter_stats(_filter_stats)


async def _record_filter_block(check_name: str, ticker: str) -> None:
    global _filter_stats_last_flush
    key = position_symbol_key(ticker).upper() or ticker.upper()
    async with _filter_stats_lock:
        bucket = _filter_stats_buffer.setdefault(check_name, {})
        bucket[key] = bucket.get(key, 0) + 1
        now = time.time()
        if now - _filter_stats_last_flush >= _STATS_FLUSH_INTERVAL:
            _flush_filter_stats()
            _filter_stats_last_flush = now
    _block_history.append({"ticker": key, "check": check_name, "timestamp": time.time()})


async def get_filter_stats() -> dict[str, dict[str, int]]:
    async with _filter_stats_lock:
        if not _filter_stats:
            _filter_stats.update(_load_filter_stats())
        merged = {check_name: dict(ticker_counts) for check_name, ticker_counts in _filter_stats.items()}
        for check_name, ticker_counts in _filter_stats_buffer.items():
            current = merged.setdefault(check_name, {})
            for ticker, count in ticker_counts.items():
                current[ticker] = current.get(ticker, 0) + count
        return merged


async def reset_filter_stats() -> None:
    global _filter_stats, _filter_stats_buffer, _filter_stats_last_flush, _filter_stats_last_cleanup
    async with _filter_stats_lock:
        _filter_stats = {}
        _filter_stats_buffer = {}
        _filter_stats_last_flush = 0.0
        _filter_stats_last_cleanup = 0.0
        _block_history.clear()
        _save_filter_stats({})


class FilterThresholds:
    """Configurable thresholds for pre-filter checks with dynamic profile support."""

    DEFAULT_THRESHOLDS: dict[str, Any] = {
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
        "volatility_regime_extreme_multiplier": 2.0,
        "position_reduce_on_loss_pct": 50.0,
        "dynamic_cooldown_enabled": True,
        "data_completeness_soft_fail_count": 5,
        "max_same_direction_positions": 5,
        "max_correlated_exposure_pct": 50.0,
        "max_live_missing_data_checks": 0,
        "block_live_on_risk_check_error": True,
        "low_liquidity_hour_start": 21,
        "low_liquidity_hour_end": 1,
        "low_liquidity_weekend_vol_min": 5_000_000,
        "low_liquidity_spread_max": 0.05,
        "block_rate_threshold": 5,
        "block_rate_window_seconds": 600,
        "block_rate_throttle_seconds": 300,
        "circuit_breaker_max_blocks": 10,
        "circuit_breaker_window_seconds": 300,
        "circuit_breaker_cooldown_seconds": 900,
        "volume_drop_pct_max": 70.0,
        "volume_lookback_days": 7,
        "mtf_require_htf_alignment": True,
        "signal_velocity_max_per_minute": 3.0,
        "signal_velocity_window_seconds": 300,
        "position_concentration_soft_limit": 3,
        "position_concentration_hard_limit": 6,
        "vwap_deviation_pct_max": 2.0,
        "vwap_lookback_candles": 24,
        "oi_divergence_threshold_pct": 5.0,
        "oi_price_stall_threshold_pct": 1.0,
        "exchange_reserves_flow_threshold_usd": 100_000_000,
        "funding_steepening_threshold_multiplier": 2.0,
        "exchange_price_discrepancy_pct_max": 2.0,
        "exchange_price_min_exchanges": 2,
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

    def __new__(cls) -> "FilterThresholds":
        if cls._instance is None:
            with cls._instance_lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._custom_thresholds = {}
                    cls._instance._load_from_env()
        return cls._instance

    def _load_from_env(self) -> None:
        env_mappings = {
            "OI_CHANGE_THRESHOLD_PCT": "oi_change_pct_max",
            "CORRELATED_THRESHOLD_PCT": "correlated_asset_change_max",
            "WHALE_THRESHOLD_USD": "whale_threshold_usd",
        }
        for env_key, threshold_key in env_mappings.items():
            env_value = os.getenv(env_key)
            if not env_value:
                continue
            try:
                self._custom_thresholds[threshold_key] = float(env_value)
            except (ValueError, TypeError):
                pass

    def get(self, key: str, ticker: str = "") -> Any:
        ticker_upper = ticker.upper().strip()
        if ticker_upper not in self.DYNAMIC_THRESHOLDS:
            ticker_upper = position_symbol_key(ticker).upper() or ticker_upper
        if key in self._custom_thresholds:
            return self._custom_thresholds[key]
        if ticker_upper in self.DYNAMIC_THRESHOLDS and key in self.DYNAMIC_THRESHOLDS[ticker_upper]:
            return self.DYNAMIC_THRESHOLDS[ticker_upper][key]
        return self.DEFAULT_THRESHOLDS.get(key)

    def get_with_profile(self, key: str, ticker: str = "", atr_pct: float | None = None, volume_24h: float = 0) -> Any:
        ticker_upper = ticker.upper().strip()
        if ticker_upper not in self.DYNAMIC_THRESHOLDS:
            ticker_upper = position_symbol_key(ticker).upper() or ticker_upper
        if key in self._custom_thresholds:
            return self._custom_thresholds[key]
        if ticker_upper in self.DYNAMIC_THRESHOLDS and key in self.DYNAMIC_THRESHOLDS[ticker_upper]:
            return self.DYNAMIC_THRESHOLDS[ticker_upper][key]
        if atr_pct is not None:
            profile = self.DYNAMIC_THRESHOLDS.get("HIGH_VOLATILITY", {})
            if atr_pct > float(profile.get("atr_pct_max", 20.0)) and key in profile:
                return profile[key]
        if volume_24h > 0:
            profile = self.DYNAMIC_THRESHOLDS.get("LOW_VOLUME", {})
            if volume_24h < float(profile.get("volume_24h_min", 500_000)) and key in profile:
                return profile[key]
        return self.DEFAULT_THRESHOLDS.get(key)

    def get_regime_multiplier(self, regime: str) -> float:
        if regime == "extreme_volatility":
            return float(self.get("volatility_regime_extreme_multiplier", "") or 0)
        return float(self.get("volatility_regime_multiplier", "") or 0)

    def set_custom(self, key: str, value: Any) -> None:
        with self._instance_lock:
            self._custom_thresholds[key] = value

    def clear_custom(self, key: str | None = None) -> None:
        with self._instance_lock:
            if key:
                self._custom_thresholds.pop(key, None)
            else:
                self._custom_thresholds.clear()

    def load_from_dict(self, data: dict[str, Any]) -> None:
        with self._instance_lock:
            for key, value in data.items():
                if key in self.DEFAULT_THRESHOLDS:
                    self._custom_thresholds[key] = value

    def reload_from_dict(self, data: dict[str, Any] | None = None) -> None:
        with self._instance_lock:
            self._custom_thresholds = {}
            self._load_from_env()
            for key, value in (data or {}).items():
                if key in self.DEFAULT_THRESHOLDS:
                    self._custom_thresholds[key] = value

    def to_dict(self) -> dict[str, Any]:
        with self._instance_lock:
            result = dict(self.DEFAULT_THRESHOLDS)
            result.update(self._custom_thresholds)
            return result

    @classmethod
    def instance(cls) -> "FilterThresholds":
        return cls()


def get_thresholds() -> FilterThresholds:
    return FilterThresholds.instance()


FILTER_WEIGHTS: dict[str, float] = {
    "daily_trade_limit": 10.0,
    "daily_loss_limit": 10.0,
    "account_daily_loss_limit": 10.0,
    "cooldown": 5.0,
    "block_rate": 7.0,
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
    "position_concentration": 8.0,
    "signal_consistency": 6.0,
    "volume_drop": 6.0,
    "mtf_confirmation": 7.0,
    "signal_velocity": 5.0,
    "circuit_breaker": 10.0,
    "vwap_deviation": 7.0,
    "oi_price_divergence": 8.0,
    "exchange_reserves": 6.0,
    "funding_term_structure": 5.0,
    "exchange_price_discrepancy": 6.0,
    "live_data_quality": 10.0,
}


def calculate_filter_score(checks: dict[str, dict]) -> float:
    active_weight = 0.0
    earned_weight = 0.0
    for check_name, check_data in checks.items():
        if check_data.get("disabled", False):
            continue
        weight = FILTER_WEIGHTS.get(check_name, 5.0)
        active_weight += weight
        if check_data.get("passed", True):
            earned_weight += weight
        elif check_data.get("soft_fail", False):
            earned_weight += weight * 0.5
    return (earned_weight / active_weight) * 100.0 if active_weight > 0 else 100.0


# ════════════════════════════════════════════════════════════════════════════════
# Signal Memory (Bucketed, Time-Based Eviction)
# ════════════════════════════════════════════════════════════════════════════════
_state_lock = asyncio.Lock()
# FIX #15: Bucketed storage by (user_id, ticker_key) for O(1) lookups
_signal_buckets: dict[str, deque[dict[str, Any]]] = {}
_MAX_BUCKET_SIZE = 200
_SIGNAL_MAX_AGE_SECONDS = 3600  # 1 hour max retention per signal
_daily_trade_count: int = 0
_daily_trade_date: str = ""
_daily_pnl: float = 0.0


def _bucket_key(user_id: str | None, ticker: str) -> str:
    uid = user_id or "admin"
    tk = position_symbol_key(ticker).upper() or ticker.upper()
    return f"{uid}:{tk}"


async def _evict_stale_signals() -> None:
    """TIME-BASED EVICTION (#13): Remove signals older than _SIGNAL_MAX_AGE_SECONDS."""
    cutoff = utcnow() - timedelta(seconds=_SIGNAL_MAX_AGE_SECONDS)
    async with _state_lock:
        for key in list(_signal_buckets.keys()):
            bucket = _signal_buckets[key]
            while bucket and bucket[0]["timestamp"] < cutoff:
                bucket.popleft()
            if not bucket:
                del _signal_buckets[key]


async def _append_signal(signal: TradingViewSignal, user_id: str | None, passed: bool) -> None:
    """FIX #6: Record ALL signals (passed or blocked) for accurate saturation tracking."""
    key = _bucket_key(user_id, signal.ticker)
    async with _state_lock:
        if key not in _signal_buckets:
            _signal_buckets[key] = deque(maxlen=_MAX_BUCKET_SIZE)
        _signal_buckets[key].append({
            "user_id": user_id or "admin",
            "ticker": signal.ticker,
            "ticker_key": position_symbol_key(signal.ticker),
            "direction": signal.direction,
            "timestamp": utcnow(),
            "passed": passed,
        })
    await _evict_stale_signals()


def reset_daily_counters():
    global _daily_trade_count, _daily_trade_date, _daily_pnl
    _daily_trade_count = 0
    _daily_trade_date = utcnow().strftime("%Y-%m-%d")
    _daily_pnl = 0.0


async def increment_trade_count():
    global _daily_trade_count, _daily_trade_date
    async with _state_lock:
        today = utcnow().strftime("%Y-%m-%d")
        if today != _daily_trade_date:
            reset_daily_counters()
        _daily_trade_count += 1


async def clear_signal_memory() -> None:
    """Clear all signal memory (buckets). Used by tests."""
    global _signal_buckets
    async with _state_lock:
        _signal_buckets.clear()


# Backward-compat: expose signal injection for tests that relied on _recent_signals.append()
async def _inject_signal(user_id: str | None, ticker: str, direction, timestamp=None, passed: bool = True) -> None:
    """Inject a synthetic signal into memory (for test fixtures)."""
    key = _bucket_key(user_id, ticker)
    async with _state_lock:
        if key not in _signal_buckets:
            _signal_buckets[key] = deque(maxlen=_MAX_BUCKET_SIZE)
        _signal_buckets[key].append({
            "user_id": user_id or "admin",
            "ticker": ticker,
            "ticker_key": position_symbol_key(ticker),
            "direction": direction,
            "timestamp": timestamp or utcnow(),
            "passed": passed,
        })


async def update_daily_pnl(pnl: float):
    global _daily_pnl, _daily_trade_date
    async with _state_lock:
        today = utcnow().strftime("%Y-%m-%d")
        if today != _daily_trade_date:
            reset_daily_counters()
        _daily_pnl += float(pnl or 0)


# ════════════════════════════════════════════════════════════════════════════════
# Helper Functions (cooldown, saturation, block rate, circuit breaker)
# ════════════════════════════════════════════════════════════════════════════════
async def _check_cooldown(signal: TradingViewSignal, cooldown_seconds: int = 300, user_id: str | None = None) -> bool:
    """Check if we received a similar signal recently (O(1) bucketed)."""
    cutoff = utcnow() - timedelta(seconds=cooldown_seconds)
    key = _bucket_key(user_id, signal.ticker)
    async with _state_lock:
        bucket = _signal_buckets.get(key, deque())
        for s in bucket:
            if s["timestamp"] > cutoff and s["direction"] == signal.direction and s.get("passed", True):
                return False
    return True


async def _count_recent_same_direction(signal: TradingViewSignal, window_minutes: int = 60, user_id: str | None = None) -> int:
    """Count same-direction signals in window (FIX #6: counts all signals, not just passed)."""
    cutoff = utcnow() - timedelta(minutes=window_minutes)
    key = _bucket_key(user_id, signal.ticker)
    async with _state_lock:
        bucket = _signal_buckets.get(key, deque())
        return sum(
            1 for s in bucket
            if s["timestamp"] > cutoff and s["direction"] == signal.direction
        )


async def _count_recent_opposite_direction(signal: TradingViewSignal, window_minutes: int = 60, user_id: str | None = None) -> int:
    """Count opposite-direction signals in window."""
    cutoff = utcnow() - timedelta(minutes=window_minutes)
    opposite = SignalDirection.SHORT if signal.direction == SignalDirection.LONG else SignalDirection.LONG
    key = _bucket_key(user_id, signal.ticker)
    async with _state_lock:
        bucket = _signal_buckets.get(key, deque())
        return sum(
            1 for s in bucket
            if s["timestamp"] > cutoff and s["direction"] == opposite
        )


# FIX #11: Block rate throttle
def _check_block_rate_throttle(ticker_key: str, thresholds: FilterThresholds) -> tuple[bool, str | None]:
    """Check if this ticker has been blocked too many times recently."""
    threshold = int(thresholds.get("block_rate_threshold", ""))
    window = int(thresholds.get("block_rate_window_seconds", ""))

    cutoff = time.time() - window
    recent_blocks = [b for b in _block_history if b["ticker"] == ticker_key and b["timestamp"] > cutoff]

    if len(recent_blocks) >= threshold:
        return False, f"Block rate throttle: {len(recent_blocks)} blocks in {window}s for {ticker_key}"
    return True, None


# FIX: Circuit breaker / kill switch (#INSTITUTIONAL)
async def _check_circuit_breaker(ticker_key: str, thresholds: FilterThresholds) -> tuple[bool, str | None]:
    """Circuit breaker: if too many blocks in short window, kill trading for cooldown."""
    max_blocks = int(thresholds.get("circuit_breaker_max_blocks", ""))
    window = int(thresholds.get("circuit_breaker_window_seconds", ""))
    cooldown_secs = int(thresholds.get("circuit_breaker_cooldown_seconds", ""))

    now = time.time()

    async with _CIRCUIT_LOCK:
        if ticker_key in _CIRCUIT_BREAKERS:
            if now < _CIRCUIT_BREAKERS[ticker_key]:
                remaining = int(_CIRCUIT_BREAKERS[ticker_key] - now)
                return False, f"Circuit breaker active for {ticker_key}: {remaining}s remaining"
            else:
                del _CIRCUIT_BREAKERS[ticker_key]

        cutoff = now - window
        recent_blocks = [b for b in _block_history if b["ticker"] == ticker_key and b["timestamp"] > cutoff]

        if len(recent_blocks) >= max_blocks:
            _CIRCUIT_BREAKERS[ticker_key] = now + cooldown_secs
            return False, f"Circuit breaker tripped for {ticker_key}: {len(recent_blocks)} blocks in {window}s"

    return True, None


# Signal velocity tracker (#INSTITUTIONAL)
async def _check_signal_velocity(ticker_key: str, thresholds: FilterThresholds, user_id: str | None = None) -> tuple[bool, float, str | None]:
    """Check if signals are arriving too fast (momentum exhaustion risk)."""
    window = int(thresholds.get("signal_velocity_window_seconds", ""))
    max_per_minute = float(thresholds.get("signal_velocity_max_per_minute", ""))

    cutoff = utcnow() - timedelta(seconds=window)
    key = _bucket_key(user_id, ticker_key)
    async with _state_lock:
        bucket = _signal_buckets.get(key, deque())
        recent_count = sum(1 for s in bucket if s["timestamp"] > cutoff)

    minutes = window / 60.0
    velocity = recent_count / minutes if minutes > 0 else 0

    if velocity > max_per_minute:
        return False, velocity, f"Signal velocity too high: {velocity:.1f}/min (max {max_per_minute}/min)"
    return True, velocity, None


# FIX #17: Signal source consistency
async def _check_signal_consistency(ticker_key: str, signal: TradingViewSignal, user_id: str | None) -> tuple[bool, str | None]:
    """Check if same strategy is sending conflicting signals in short window."""
    cutoff = utcnow() - timedelta(seconds=60)
    opposite = SignalDirection.SHORT if signal.direction == SignalDirection.LONG else SignalDirection.LONG
    key = _bucket_key(user_id, ticker_key)
    async with _state_lock:
        bucket = _signal_buckets.get(key, deque())
        for s in bucket:
            if s["timestamp"] > cutoff and s["direction"] == opposite:
                return False, f"Signal conflict: {signal.direction.value} vs {opposite.value} within 60s"
    return True, None


# Multi-Timeframe Confirmation (#INSTITUTIONAL)
def _check_mtf_confirmation(
    signal: TradingViewSignal,
    market: MarketContext,
    thresholds: FilterThresholds,
) -> tuple[bool, dict[str, Any]]:
    """Check if higher timeframe structure supports the signal direction.

    Uses EMA alignment on multiple timeframes as a proxy for HTF confirmation.
    """
    result = {"htf_aligned": False, "htf_timeframe": None, "htf_trend": "neutral"}
    require_htf = bool(thresholds.get("mtf_require_htf_alignment", ""))

    ohlcv_4h = getattr(market, "_ohlcv_4h", None) or []
    ohlcv_1h = getattr(market, "_ohlcv_1h", None) or []

    is_long = signal.direction in (SignalDirection.LONG,)
    is_short = signal.direction in (SignalDirection.SHORT,)

    # Use 4H data as HTF if available
    htf_data = ohlcv_4h if len(ohlcv_4h) >= 10 else (ohlcv_1h if len(ohlcv_1h) >= 10 else [])
    if len(htf_data) < 10:
        result["note"] = "Insufficient HTF data"
        return not require_htf, result

    htf_label = "4h" if len(ohlcv_4h) >= 10 else "1h"
    result["htf_timeframe"] = htf_label

    closes = [c[4] for c in htf_data[-10:]]
    first_close = closes[0]
    last_close = closes[-1]
    if first_close <= 0:
        result["note"] = "Invalid HTF close data"
        return not require_htf, result

    htf_change = (last_close - first_close) / first_close * 100
    if htf_change > 1.0:
        result["htf_trend"] = "bullish"
        result["htf_aligned"] = is_long
    elif htf_change < -1.0:
        result["htf_trend"] = "bearish"
        result["htf_aligned"] = is_short
    else:
        result["htf_trend"] = "neutral"
        result["htf_aligned"] = True  # neutral doesn't conflict

    if not result["htf_aligned"] and require_htf:
        return False, result

    return True, result


# FIX #18: Relative volume drop detection
def _check_volume_drop(market: MarketContext, thresholds: FilterThresholds) -> tuple[bool, str | None]:
    """Check if current volume has dropped significantly vs. recent average.

    Uses the _volume_history attribute if available on market context.
    """
    vol_max_drop = float(thresholds.get("volume_drop_pct_max", ""))
    vol_history = getattr(market, "_volume_history", None) or []

    if len(vol_history) < 24:
        return True, None  # Not enough data

    recent_avg = sum(vol_history[-24:]) / len(vol_history[-24:])
    if recent_avg <= 0:
        return True, None

    current_vol = market.volume_24h
    vol_drop_pct = ((recent_avg - current_vol) / recent_avg) * 100

    if vol_drop_pct > vol_max_drop:
        return False, f"Volume drop {vol_drop_pct:.1f}% vs {vol_max_drop}% threshold (avg: ${recent_avg:,.0f})"

    return True, None


# FIX #16: Position concentration / portfolio heat check
async def _check_position_concentration(
    ticker_key: str,
    signal: TradingViewSignal,
    thresholds: FilterThresholds,
    user_id: str | None = None,
    db_session=None,
) -> tuple[bool, dict[str, Any]]:
    """Check if too many positions are already open in the same direction."""
    result = {"long_positions": 0, "short_positions": 0, "long_notional": 0.0, "short_notional": 0.0, "note": ""}

    if db_session is None:
        result["note"] = "No DB session available - skip"
        return True, result

    soft_limit = int(thresholds.get("position_concentration_soft_limit", ""))
    hard_limit = int(thresholds.get("position_concentration_hard_limit", ""))

    try:
        from sqlalchemy import select

        from core.database import PositionModel

        stmt = select(PositionModel).where(PositionModel.status.in_(["open", "pending"]))
        if user_id:
            stmt = stmt.where(PositionModel.user_id == user_id)
        else:
            stmt = stmt.where(PositionModel.user_id.is_(None))
        result_set = await db_session.execute(stmt)
        positions = list(result_set.scalars().all())

        is_long = signal.direction in (SignalDirection.LONG,)

        for pos in positions:
            pos_dir = str(getattr(pos, "direction", "long") or "long").lower()
            entry = float(getattr(pos, "entry_price", 0) or 0)
            qty = float(getattr(pos, "remaining_quantity", 0) or 0) or float(getattr(pos, "quantity", 0) or 0)
            notional = entry * qty if entry > 0 and qty > 0 else 0

            if pos_dir == "long":
                result["long_positions"] += 1
                result["long_notional"] += notional
            elif pos_dir == "short":
                result["short_positions"] += 1
                result["short_notional"] += notional

        current_same_dir = result["long_positions"] if is_long else result["short_positions"]

        if current_same_dir >= hard_limit:
            return False, result
        if current_same_dir >= soft_limit:
            return True, result  # Soft limit, passed but noted

    except (SQLAlchemyError, OSError, TypeError, ValueError, AttributeError) as e:
        logger.debug(f"[PreFilter] Position concentration check skipped: {e}")
        result["note"] = f"Skip: {e}"
    except Exception as e:
        logger.warning(f"[PreFilter] Position concentration check unexpected error: {e}")
        result["note"] = f"Error: {e}"

    return True, result


# ════════════════════════════════════════════════════════════════════════════════
# Main Pre-Filter Engine
# ════════════════════════════════════════════════════════════════════════════════
async def count_today_executed_trades_async(user_id: str | None = None) -> int:
    """Count today's executed trades from the async database.

    P0-3 FIX: On database failure, returns max_daily_trades to BLOCK the trade.
    FIX #2: Excludes asyncio.CancelledError from broad except.
    """
    from core.database import count_today_executed_trades, db_manager

    try:
        async with db_manager.async_session_factory() as session:
            return await count_today_executed_trades(session, user_id)
    except asyncio.CancelledError:
        raise
    except SQLAlchemyError as e:
        logger.error(
            f"[PreFilter] CRITICAL: Database count failed for daily_trade_limit. "
            f"User={user_id}, error={e}. BLOCKING trade to prevent limit bypass."
        )
        return 999999
    except (OSError, ConnectionError, TimeoutError) as e:
        logger.error(
            f"[PreFilter] CRITICAL: Network error in daily trade count. "
            f"User={user_id}, error={e}. BLOCKING trade to prevent limit bypass."
        )
        return 999999
    except Exception:
        logger.exception(
            f"[PreFilter] CRITICAL: Unexpected error in daily trade count. "
            f"User={user_id}. BLOCKING trade to prevent limit bypass."
        )
        return 999999


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
    db_session=None,
) -> PreFilterResult:
    """
    Run institutional-grade rule-based checks on the incoming signal (async version).

    Args:
        use_scoring: If True, use weighted scoring instead of hard pass/fail
        min_pass_score: Minimum score (0-100) required to pass when scoring mode enabled
        db_session: Optional SQLAlchemy async session for position queries

    Returns PreFilterResult with pass/fail, score, and detailed reasons.
    """
    thresholds = get_thresholds()
    checks: dict[str, dict[str, Any]] = {}
    reasons: list[str] = []
    soft_fail_reasons: list[str] = []
    ticker = signal.ticker.upper()
    ticker_key = position_symbol_key(signal.ticker).upper() or ticker.upper()
    disabled = {str(item).strip().lower() for item in (disabled_checks or []) if str(item).strip()}

    # 鈹€鈹€ v5.2: Pipeline latency tracking 鈹€鈹€
    _pipeline_start = time.perf_counter()

    # ═══ v5.2: Evaluate past blocked signal outcomes ═══
    try:
        evaluate_blocked_outcomes(current_prices={ticker_key: market.current_price} if market.current_price > 0 else None)
    except (ValueError, TypeError, KeyError, NameError):
        pass  # Never let feedback loop affect trade decisions

    async def record_filter_block(check_name: str) -> None:
        if check_name.lower() not in disabled:
            await _record_filter_block(check_name, ticker)

    # Market data availability flags
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

    # At market-level: resolve dynamic profile
    atr_for_profile = float(market.atr_pct) if market.atr_pct is not None else None
    vol_for_profile = float(market.volume_24h) if market.volume_24h > 0 else 0.0

    # 鈹€鈹€ Check 1: Daily trade limit 鈹€鈹€
    daily_count_snapshot = await count_today_executed_trades_async(user_id=user_id)
    daily_ok = True if max_daily_trades <= 0 else daily_count_snapshot < max_daily_trades
    checks["daily_trade_limit"] = {
        "passed": daily_ok,
        "current": daily_count_snapshot,
        "max": max_daily_trades,
    }
    if not daily_ok:
        reasons.append(f"Daily trade limit reached ({daily_count_snapshot}/{max_daily_trades})")
        await record_filter_block("daily_trade_limit")

    # 鈹€鈹€ Check 2: Daily loss limit 鈹€鈹€
    account_equity = float(getattr(settings.risk, "account_equity_usdt", 10000.0) or 10000.0)
    if account_equity <= 0:
        account_equity = 10000.0

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
        await record_filter_block("daily_loss_limit")

    # 鈹€鈹€ Check 3: Account-level loss limit 鈹€鈹€
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
        await record_filter_block("account_daily_loss_limit")

    # 鈹€鈹€ Check 4: Circuit breaker / kill switch (NEW v5.0) 鈹€鈹€
    cb_ok, cb_reason = await _check_circuit_breaker(ticker_key, thresholds)
    checks["circuit_breaker"] = {
        "passed": cb_ok,
        "reason": cb_reason,
    }
    if not cb_ok:
        reasons.append(cb_reason)
        await record_filter_block("circuit_breaker")

    # ── Check 5: Block rate throttle (NEW v5.0) ──
    throttle_ok, throttle_reason = _check_block_rate_throttle(ticker_key, thresholds)
    checks["block_rate"] = {
        "passed": throttle_ok,
        "reason": throttle_reason,
    }
    if not throttle_ok:
        reasons.append(throttle_reason)
        await record_filter_block("block_rate")

    # 鈹€鈹€ Check 6: Duplicate signal cooldown (Dynamic) 鈹€鈹€
    # FIX #1: fetch recent_results ONCE for both cooldown and consecutive_loss
    base_cooldown = int(thresholds.get_with_profile("cooldown_seconds", ticker))
    dynamic_enabled = bool(thresholds.get_with_profile("dynamic_cooldown_enabled", ticker))

    try:
        recent_results = await get_recent_trade_results_async(limit=5, user_id=user_id, ticker=ticker)
    except asyncio.CancelledError:
        raise
    except (SQLAlchemyError, ConnectionError, TimeoutError, OSError, ValueError, TypeError, AttributeError):
        recent_results = []

    if dynamic_enabled and recent_results:
        win_multiplier = float(thresholds.get_with_profile("cooldown_win_multiplier", ticker))
        loss_multiplier = float(thresholds.get_with_profile("cooldown_loss_multiplier", ticker))
        last_pnl = recent_results[0].get("pnl_pct", 0) if recent_results else 0
        if last_pnl > 0:
            cooldown_secs = int(base_cooldown * win_multiplier)
        elif last_pnl < 0:
            cooldown_secs = int(base_cooldown * loss_multiplier)
        else:
            cooldown_secs = base_cooldown
    else:
        cooldown_secs = base_cooldown

    cooldown_ok = await _check_cooldown(signal, cooldown_seconds=cooldown_secs, user_id=user_id)
    checks["cooldown"] = {
        "passed": cooldown_ok,
        "cooldown_seconds": cooldown_secs,
        "base_cooldown": base_cooldown,
        "dynamic_enabled": dynamic_enabled,
    }
    if not cooldown_ok:
        reasons.append(f"Duplicate signal within {cooldown_secs}s cooldown (dynamic)")
        await record_filter_block("cooldown")

    # 鈹€鈹€ Check 7: Price sanity check 鈹€鈹€
    price_ok = True
    price_deviation_max = float(thresholds.get_with_profile("price_deviation_pct_max", ticker))
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
            await record_filter_block("price_sanity")
    elif not has_price_data:
        checks["price_sanity"] = {"passed": True, "missing_data": True, "note": "No price data available"}
        missing_data_checks.append("price_sanity")

    # 鈹€鈹€ Check 8: Extreme volatility guard 鈹€鈹€
    vol_ok = True
    atr_max = float(thresholds.get_with_profile("atr_pct_max", ticker, atr_pct=atr_for_profile))
    if has_atr_data:
        vol_ok = market.atr_pct < atr_max
        checks["volatility_guard"] = {
            "passed": vol_ok,
            "atr_pct": market.atr_pct,
            "threshold": atr_max,
        }
        if not vol_ok:
            reasons.append(f"Extreme volatility: ATR% = {market.atr_pct:.2f}% > {atr_max}%")
            await record_filter_block("volatility_guard")
    elif not has_atr_data:
        checks["volatility_guard"] = {"passed": True, "missing_data": True, "note": "No ATR data available"}
        missing_data_checks.append("volatility_guard")

    # 鈹€鈹€ Check 9: Spread check 鈹€鈹€
    spread_ok = True
    spread_max = float(thresholds.get_with_profile("spread_pct_max", ticker, volume_24h=vol_for_profile))
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

    # 鈹€鈹€ Check 10: Volume sanity 鈹€鈹€
    volume_ok = True
    volume_min = float(thresholds.get_with_profile("volume_24h_min", ticker, volume_24h=vol_for_profile))
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

    # 鈹€鈹€ Check 11: Relative volume drop (NEW v5.0) 鈹€鈹€
    vol_drop_ok, vol_drop_reason = _check_volume_drop(market, thresholds)
    checks["volume_drop"] = {
        "passed": vol_drop_ok,
        "reason": vol_drop_reason,
    }
    if not vol_drop_ok:
        soft_fail_reasons.append(vol_drop_reason)
        checks["volume_drop"]["soft_fail"] = True

    # 鈹€鈹€ Check 12: Large sudden move guard 鈹€鈹€
    sudden_move_ok = True
    move_max = float(thresholds.get_with_profile("price_change_1h_max", ticker, atr_pct=atr_for_profile))
    if market.price_change_1h != 0:
        sudden_move_ok = abs(market.price_change_1h) < move_max
        checks["sudden_move"] = {
            "passed": sudden_move_ok,
            "price_change_1h": market.price_change_1h,
            "threshold": move_max,
        }
        if not sudden_move_ok:
            reasons.append(f"Sudden move: {market.price_change_1h:+.2f}% in 1h")
            await record_filter_block("sudden_move")

    # 鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺?    # ENHANCED CHECKS (v3+)
    # 鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺?
    # 鈹€鈹€ Check 13: VWAP Deviation (P0 鈥?institutional benchmark) 鈹€鈹€
    vwap_ok = True
    vwap_dev_max = float(thresholds.get("vwap_deviation_pct_max", ""))
    vwap_data = None
    ohlcv_1h_vwap = getattr(market, "_ohlcv_1h", None) or []
    if len(ohlcv_1h_vwap) >= 24 and market.current_price > 0:
        try:
            from enhanced_market_data import calculate_vwap_deviation
            vwap_data = await calculate_vwap_deviation(ohlcv_1h_vwap, market.current_price)
            vwap_dev = vwap_data.get("deviation_pct")
            vwap_dir = vwap_data.get("direction")

            if vwap_dev is not None:
                is_long = signal.direction in (SignalDirection.LONG,)
                is_short = signal.direction in (SignalDirection.SHORT,)
                # Long entry below VWAP = favorable; above VWAP by too much = chasing
                if is_long and vwap_dir == "above_vwap" and vwap_dev > vwap_dev_max:
                    vwap_ok = False
                # Short entry above VWAP = favorable; below VWAP by too much = chasing
                elif is_short and vwap_dir == "below_vwap" and vwap_dev > vwap_dev_max:
                    vwap_ok = False

            checks["vwap_deviation"] = {
                "passed": vwap_ok,
                "vwap": vwap_data.get("vwap"),
                "deviation_pct": vwap_dev,
                "direction": vwap_dir,
                "threshold": vwap_dev_max,
            }
            if not vwap_ok:
                soft_fail_reasons.append(f"VWAP deviation {vwap_dev:.2f}% {vwap_dir} (soft fail)")
                checks["vwap_deviation"]["soft_fail"] = True
        except (ImportError, ModuleNotFoundError, NameError):
            checks["vwap_deviation"] = {"passed": True, "note": "VWAP module not available"}
        except (ValueError, TypeError, ZeroDivisionError, IndexError, KeyError):
            checks["vwap_deviation"] = {"passed": True, "note": "VWAP calculation error"}
    else:
        checks["vwap_deviation"] = {"passed": True, "missing_data": True, "note": "Need 24 1h candles for VWAP"}
        if len(ohlcv_1h_vwap) < 24:
            missing_data_checks.append("vwap_deviation")

    # 鈹€鈹€ Check 14: RSI Extreme Guard 鈹€鈹€
    rsi_ok = True
    rsi_long_max = float(thresholds.get_with_profile("rsi_long_max", ticker))
    rsi_short_min = float(thresholds.get_with_profile("rsi_short_min", ticker))
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
            await record_filter_block("rsi_extreme")
    elif not has_rsi_data:
        checks["rsi_extreme"] = {"passed": True, "missing_data": True, "note": "No RSI data available"}
        missing_data_checks.append("rsi_extreme")

    # 鈹€鈹€ Check 14b: Funding Rate Guard 鈹€鈹€
    funding_ok = True
    funding_threshold = float(thresholds.get_with_profile("funding_rate_threshold", ticker))
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

    # 鈹€鈹€ Check 15: Orderbook Imbalance Guard 鈹€鈹€
    ob_ok = True
    ob_long_min = float(thresholds.get_with_profile("orderbook_long_min", ticker))
    ob_short_max = float(thresholds.get_with_profile("orderbook_short_max", ticker))
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
            await record_filter_block("orderbook_imbalance")
    elif not has_orderbook_data:
        checks["orderbook_imbalance"] = {"passed": True, "missing_data": True, "note": "No orderbook data available"}
        missing_data_checks.append("orderbook_imbalance")

    # 鈹€鈹€ Check 16: Weekend / Low Liquidity Hours Guard 鈹€鈹€
    # FIX #9: Configurable low-liquidity hours
    time_ok = True
    now_utc = utcnow()
    is_weekend = now_utc.weekday() >= 5
    low_hour_start = int(thresholds.get("low_liquidity_hour_start", ""))
    low_hour_end = int(thresholds.get("low_liquidity_hour_end", ""))

    if low_hour_start > low_hour_end:
        is_low_liq_hour = now_utc.hour >= low_hour_start or now_utc.hour < low_hour_end
    else:
        is_low_liq_hour = low_hour_start <= now_utc.hour < low_hour_end

    weekend_vol_min = float(thresholds.get("low_liquidity_weekend_vol_min", ""))
    liq_spread_max = float(thresholds.get("low_liquidity_spread_max", ""))

    if is_weekend and market.volume_24h > 0:
        if market.volume_24h < weekend_vol_min:
            time_ok = False

    if is_low_liq_hour and market.bid_ask_spread > liq_spread_max:
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

    # 鈹€鈹€ Check 17: Consecutive Loss Protection (Smart) 鈹€鈹€
    # FIX #1: Uses the same recent_results already fetched for cooldown
    # FIX #2: Excludes asyncio.CancelledError
    consec_ok = True
    consec_max = int(thresholds.get("consecutive_loss_max", ticker))
    position_reduce_pct = float(thresholds.get("position_reduce_on_loss_pct", ticker))
    consec_losses = 0

    if recent_results:
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
        reasons.append(f"{consec_max} consecutive losses 鈥?cooling off, suggest {position_suggestion}")
        await record_filter_block("consecutive_loss")
    elif consec_losses >= 2:
        soft_fail_reasons.append(f"{consec_losses} recent losses 鈥?suggest reduce position by {position_reduce_pct}%")
        checks["consecutive_loss"]["soft_fail"] = True

    # 鈹€鈹€ Check 18: Same-Direction Signal Saturation 鈹€鈹€
    saturation_ok = True
    saturation_max = int(thresholds.get("signal_saturation_max", ticker))
    same_dir_count = await _count_recent_same_direction(signal, window_minutes=60, user_id=user_id)
    opposite_dir_count = await _count_recent_opposite_direction(signal, window_minutes=60, user_id=user_id)

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

    # 鈹€鈹€ Check 19: Signal velocity (NEW v5.0) 鈹€鈹€
    vel_ok, velocity, vel_reason = await _check_signal_velocity(ticker_key, thresholds, user_id=user_id)
    checks["signal_velocity"] = {
        "passed": vel_ok,
        "velocity_per_min": round(velocity, 2),
        "reason": vel_reason,
    }
    if not vel_ok:
        soft_fail_reasons.append(vel_reason)
        checks["signal_velocity"]["soft_fail"] = True

    # 鈹€鈹€ Check 20: Signal source consistency (NEW v5.0) 鈹€鈹€
    consist_ok, consist_reason = await _check_signal_consistency(ticker_key, signal, user_id)
    checks["signal_consistency"] = {
        "passed": consist_ok,
        "reason": consist_reason,
    }
    if not consist_ok:
        soft_fail_reasons.append(consist_reason)
        checks["signal_consistency"]["soft_fail"] = True

    # 鈹€鈹€ Check 21: EMA Trend Alignment 鈹€鈹€
    ema_ok = True
    ema_diff_min = float(thresholds.get_with_profile("ema_diff_pct_min", ticker))
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

    # 鈹€鈹€ Check 22: Multi-Timeframe Confirmation (NEW v5.0) 鈹€鈹€
    mtf_ok, mtf_data = _check_mtf_confirmation(signal, market, thresholds)
    checks["mtf_confirmation"] = {
        "passed": mtf_ok,
        **mtf_data,
    }
    if not mtf_ok:
        soft_fail_reasons.append(f"HTF {mtf_data.get('htf_trend')} conflicts with {signal.direction.value} (soft fail)")
        checks["mtf_confirmation"]["soft_fail"] = True

    # 鈹€鈹€ Check 23: Market Structure (SMC) Validation 鈹€鈹€
    # FIX #3: Log warnings on failures
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
                await record_filter_block("market_structure")
    except ImportError as e:
        logger.warning(f"[PreFilter] SMC analyzer import failed: {e}")
        checks["market_structure"] = {"passed": True, "note": f"Import skip: {e}"}
    except asyncio.CancelledError:
        raise
    except Exception as e:
        logger.warning(f"[PreFilter] Market structure check failed: {e}")
        checks["market_structure"] = {"passed": True, "note": f"Error: {e}"}

    # 鈹€鈹€ Check 24: Open Interest Change 鈹€鈹€
    oi_ok = True
    oi_max = float(thresholds.get_with_profile("oi_change_pct_max", ticker))
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

    # 鈹€鈹€ Check 25: OI-Price Divergence (P0 鈥?smart money detection) 鈹€鈹€
    oi_div_ok = True
    if has_oi_data and market.price_change_1h != 0:
        try:
            from enhanced_market_data import check_oi_price_divergence
            oi_div_data = await check_oi_price_divergence(
                oi_change_pct=market.open_interest_change_pct,
                price_change_1h=market.price_change_1h,
                price_change_4h=market.price_change_4h,
                oi_change_threshold=float(thresholds.get("oi_divergence_threshold_pct", "")),
                price_stall_threshold=float(thresholds.get("oi_price_stall_threshold_pct", "")),
            )
            div_type = oi_div_data.get("divergence_type")
            is_bearish = oi_div_data.get("is_bearish", False)
            is_bullish = oi_div_data.get("is_bullish", False)

            is_long = signal.direction in (SignalDirection.LONG,)
            is_short = signal.direction in (SignalDirection.SHORT,)

            if is_long and is_bearish:
                oi_div_ok = False
            elif is_short and is_bullish:
                oi_div_ok = False

            checks["oi_price_divergence"] = {
                "passed": oi_div_ok,
                "divergence_type": div_type,
                "is_bearish": is_bearish,
                "is_bullish": is_bullish,
                "note": oi_div_data.get("note"),
            }
            if not oi_div_ok:
                soft_fail_reasons.append(f"OI-Price divergence: {oi_div_data.get('note', div_type)}")
                checks["oi_price_divergence"]["soft_fail"] = True
        except Exception:
            checks["oi_price_divergence"] = {"passed": True, "note": "OI divergence check skip"}
    elif not has_oi_data:
        checks["oi_price_divergence"] = {"passed": True, "missing_data": True, "note": "No OI data"}
        missing_data_checks.append("oi_price_divergence")
    else:
        checks["oi_price_divergence"] = {"passed": True, "note": "No price change data"}

    # 鈹€鈹€ Check 26: Correlated Assets Check 鈹€鈹€
    correlated_ok = True
    corr_max = float(thresholds.get("correlated_asset_change_max", ticker))
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

    # 鈹€鈹€ Check 26b: Whale Activity 鈹€鈹€
    whale_ok = True
    whale_data = getattr(market, "_whale_activity", None) or {}
    whale_threshold = float(thresholds.get_with_profile("whale_threshold_usd", ticker, volume_24h=vol_for_profile))

    if whale_data:
        large_transfers_1h = whale_data.get("large_transfers_1h", 0)
        net_flow = whale_data.get("net_flow_24h", 0)

        is_long = signal.direction in (SignalDirection.LONG,)
        is_short = signal.direction in (SignalDirection.SHORT,)

        if is_long and net_flow < -whale_threshold:
            whale_ok = False
        elif is_short and net_flow > whale_threshold:
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

    # 鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺?    # ENHANCED CHECKS (v4+) - External Market Data
    # 鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺?
    async def _enhanced_call(name: str, coro):
        if is_check_degraded(name):
            close = getattr(coro, "close", None)
            if callable(close):
                close()
            return TimeoutError(f"{name} degraded 鈥?skipped to protect pipeline latency")
        try:
            return await asyncio.wait_for(coro, timeout=6.0)
        except TimeoutError:
            logger.warning(f"[PreFilter] Enhanced check {name} timed out")
            return TimeoutError(f"{name} timed out")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            return exc

    try:
        from enhanced_market_data import (
            calculate_directional_volume_delta,
            calculate_funding_term_structure,
            check_exchange_price_discrepancy,
            check_macro_event_risk,
            detect_volatility_regime,
            fetch_basis_data,
            fetch_exchange_reserves,
            fetch_fear_greed_index,
            fetch_liquidation_heatmap,
            fetch_long_short_ratio,
        )

        ohlcv_1h = getattr(market, "_ohlcv_1h", None) or []
        ohlcv_4h = getattr(market, "_ohlcv_4h", None) or []

        async def _safe_dvd():
            if len(ohlcv_1h) >= 20:
                return await calculate_directional_volume_delta(ohlcv_1h)
            return None

        async def _safe_volatility():
            if len(ohlcv_1h) >= 100:
                return await detect_volatility_regime(ohlcv_1h, thresholds=thresholds)
            return None

        # FIX #14: All enhanced data fetched in parallel
        results = await asyncio.gather(
            _enhanced_call("macro_events", check_macro_event_risk()),
            _enhanced_call("liquidation_heatmap", fetch_liquidation_heatmap(ticker)),
            _enhanced_call("long_short_ratio", fetch_long_short_ratio(ticker)),
            _enhanced_call("basis_check", fetch_basis_data(ticker)),
            _enhanced_call("fear_greed", fetch_fear_greed_index()),
            _enhanced_call("exchange_reserves", fetch_exchange_reserves(ticker.replace("USDT", ""))),
            _enhanced_call("funding_term", calculate_funding_term_structure(ticker, market.funding_rate)),
            _enhanced_call("price_discrepancy", check_exchange_price_discrepancy(ticker, market.current_price)),
            _safe_dvd(),
            _safe_volatility(),
            return_exceptions=True,
        )
        (macro_result, liq_result, ls_result, basis_result, fg_result,
         reserve_result, funding_term_result, price_disc_result,
         dvd_result_raw, vol_result_raw) = results

        dvd_result = None if dvd_result_raw is None or isinstance(dvd_result_raw, Exception) else dvd_result_raw
        vol_result = None if vol_result_raw is None or isinstance(vol_result_raw, Exception) else vol_result_raw
    except asyncio.CancelledError:
        raise
    except (ImportError, Exception) as e:
        macro_result = liq_result = ls_result = basis_result = fg_result = e
        reserve_result = funding_term_result = price_disc_result = dvd_result = vol_result = None

    # 鈹€鈹€ Check 27: Macro Events Risk 鈹€鈹€
    if isinstance(macro_result, Exception) and not isinstance(macro_result, asyncio.CancelledError):
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
            await record_filter_block("macro_events")

    # 鈹€鈹€ Check 28: Liquidation Heatmap 鈹€鈹€
    liq_ok = True
    liq_distance_min = float(thresholds.get("liquidation_distance_pct_min", ticker))
    if isinstance(liq_result, Exception) and not isinstance(liq_result, asyncio.CancelledError):
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

    # 鈹€鈹€ Check 29: Long/Short Ratio Extreme 鈹€鈹€
    ls_ok = True
    ls_high = float(thresholds.get("long_short_ratio_extreme_high", ticker))
    ls_low = float(thresholds.get("long_short_ratio_extreme_low", ticker))
    if isinstance(ls_result, Exception) and not isinstance(ls_result, asyncio.CancelledError):
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

    # 鈹€鈹€ Check 30: Basis (Spot vs Futures) 鈹€鈹€
    basis_ok = True
    basis_max = float(thresholds.get("basis_pct_max", ticker))
    if isinstance(basis_result, Exception) and not isinstance(basis_result, asyncio.CancelledError):
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

    # 鈹€鈹€ Check 31: Fear & Greed Index 鈹€鈹€
    fg_ok = True
    fg_threshold = float(thresholds.get("fear_greed_extreme_threshold", ticker))
    if isinstance(fg_result, Exception) and not isinstance(fg_result, asyncio.CancelledError):
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

    # 鈹€鈹€ Check 32: Directional Volume Delta (formerly CVD - renamed for accuracy) 鈹€鈹€
    dvd_ok = True
    dvd_threshold = float(thresholds.get("cvd_divergence_threshold", ticker))
    if dvd_result is None:
        checks["cvd_divergence"] = {"passed": True, "missing_data": True, "note": "Need at least 20 1h candles"}
        missing_data_checks.append("cvd_divergence")
    elif isinstance(dvd_result, Exception) and not isinstance(dvd_result, asyncio.CancelledError):
        checks["cvd_divergence"] = {"passed": True, "note": f"Skip: {dvd_result}"}
        unavailable_data_checks.append("cvd_divergence")
    else:
        dvd_data = dvd_result
        divergence = dvd_data.get("divergence")
        strength = dvd_data.get("strength", 0)
        div_type = dvd_data.get("type")

        if divergence and strength > dvd_threshold:
            is_long = signal.direction in (SignalDirection.LONG,)
            is_short = signal.direction in (SignalDirection.SHORT,)

            if is_long and div_type == "bearish":
                dvd_ok = False
            elif is_short and div_type == "bullish":
                dvd_ok = False

        checks["cvd_divergence"] = {
            "passed": dvd_ok,
            "divergence_type": div_type,
            "strength": strength,
            "price_change_pct": dvd_data.get("price_change_pct"),
            "threshold": dvd_threshold,
            "note": "Directional Volume Delta (proxy for CVD, estimated from OHLCV)",
        }
        if not dvd_ok:
            soft_fail_reasons.append(f"DVD divergence: {div_type} ({strength:.1f}%)")
            checks["cvd_divergence"]["soft_fail"] = True

    # 鈹€鈹€ Check 33: Volatility Regime 鈹€鈹€
    # FIX #10: Configurable regime thresholds
    regime_ok = True
    if vol_result is None:
        ohlcv_1h = getattr(market, "_ohlcv_1h", None) or []
        if len(ohlcv_1h) < 100:
            checks["volatility_regime"] = {"passed": True, "missing_data": True, "note": "Need at least 100 1h candles"}
            missing_data_checks.append("volatility_regime")
        else:
            checks["volatility_regime"] = {"passed": True, "note": "No volatility regime data"}
    elif isinstance(vol_result, Exception) and not isinstance(vol_result, asyncio.CancelledError):
        checks["volatility_regime"] = {"passed": True, "note": f"Skip: {vol_result}"}
        unavailable_data_checks.append("volatility_regime")
    else:
        regime_data = vol_result
        regime = regime_data.get("regime")
        suggestion = regime_data.get("suggestion")

        if regime == "extreme_volatility":
            regime_ok = False
        elif regime == "high_volatility" and market.atr_pct and market.atr_pct > thresholds.get_regime_multiplier("high_volatility") * regime_data.get("avg_atr_pct", 5):
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

    # 鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺?    # NEW v5.1 CHECKS 鈥?Institutional-Grade Indicators
    # 鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺?
    # 鈹€鈹€ Check 34: Exchange Reserve Flow (P1) 鈹€鈹€
    reserve_ok = True
    if isinstance(reserve_result, Exception) and not isinstance(reserve_result, asyncio.CancelledError):
        checks["exchange_reserves"] = {"passed": True, "note": f"Skip: {reserve_result}"}
        unavailable_data_checks.append("exchange_reserves")
    else:
        reserve_data = reserve_result
        is_accumulation = reserve_data.get("is_accumulation", False)
        is_distribution = reserve_data.get("is_distribution", False)
        flow_direction = reserve_data.get("flow_direction", "unknown")

        is_long = signal.direction in (SignalDirection.LONG,)
        is_short = signal.direction in (SignalDirection.SHORT,)

        if is_long and is_distribution:
            reserve_ok = False
        elif is_short and is_accumulation:
            reserve_ok = False

        checks["exchange_reserves"] = {
            "passed": reserve_ok,
            "net_flow_24h": reserve_data.get("net_flow_24h"),
            "flow_direction": flow_direction,
            "is_accumulation": is_accumulation,
            "is_distribution": is_distribution,
            "source": reserve_data.get("source"),
        }
        if not reserve_ok:
            soft_fail_reasons.append(f"Exchange reserves: {flow_direction} against {signal.direction.value}")
            checks["exchange_reserves"]["soft_fail"] = True

    # 鈹€鈹€ Check 35: Funding Rate Term Structure (P1) 鈹€鈹€
    fterm_ok = True
    if isinstance(funding_term_result, Exception) and not isinstance(funding_term_result, asyncio.CancelledError):
        checks["funding_term_structure"] = {"passed": True, "note": f"Skip: {funding_term_result}"}
        unavailable_data_checks.append("funding_term_structure")
    else:
        ft_data = funding_term_result
        is_steepening = ft_data.get("is_steepening", False)
        ft_trend = ft_data.get("trend", "stable")
        curr_funding = ft_data.get("current_funding", 0)

        is_long = signal.direction in (SignalDirection.LONG,)
        is_short = signal.direction in (SignalDirection.SHORT,)

        if is_long and is_steepening and curr_funding > 0:
            fterm_ok = False
        elif is_short and is_steepening and curr_funding < 0:
            fterm_ok = False

        checks["funding_term_structure"] = {
            "passed": fterm_ok,
            "current_funding": curr_funding,
            "funding_8h_ago": ft_data.get("funding_8h_ago"),
            "funding_24h_ago": ft_data.get("funding_24h_ago"),
            "trend": ft_trend,
            "is_steepening": is_steepening,
            "note": ft_data.get("note"),
        }
        if not fterm_ok:
            soft_fail_reasons.append(f"Funding term structure steepening: {ft_data.get('note', ft_trend)}")
            checks["funding_term_structure"]["soft_fail"] = True

    # 鈹€鈹€ Check 36: Multi-Exchange Price Discrepancy (P2) 鈹€鈹€
    price_disc_ok = True
    disc_max = float(thresholds.get("exchange_price_discrepancy_pct_max", ""))
    if isinstance(price_disc_result, Exception) and not isinstance(price_disc_result, asyncio.CancelledError):
        checks["exchange_price_discrepancy"] = {"passed": True, "note": f"Skip: {price_disc_result}"}
        unavailable_data_checks.append("exchange_price_discrepancy")
    else:
        disc_data = price_disc_result
        max_disc = disc_data.get("max_discrepancy_pct", 0)
        is_concerning = disc_data.get("is_concerning", False)

        if max_disc > disc_max or is_concerning:
            price_disc_ok = False

        checks["exchange_price_discrepancy"] = {
            "passed": price_disc_ok,
            "max_discrepancy_pct": max_disc,
            "exchanges_checked": disc_data.get("exchanges_checked", []),
            "prices": disc_data.get("prices", {}),
            "threshold": disc_max,
            "note": disc_data.get("note"),
        }
        if not price_disc_ok:
            soft_fail_reasons.append(f"Exchange price discrepancy: {max_disc:.2f}% across exchanges")
            checks["exchange_price_discrepancy"]["soft_fail"] = True

    # 鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺?    # NEW v5.0 CHECKS
    # 鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺?
    # 鈹€鈹€ Check 37: Position concentration / portfolio heat 鈹€鈹€
    conc_ok, conc_data = await _check_position_concentration(
        ticker_key, signal, thresholds, user_id=user_id, db_session=db_session
    )
    checks["position_concentration"] = {
        "passed": conc_ok,
        **conc_data,
    }
    if not conc_ok:
        reasons.append(
            f"Position concentration limit: {conc_data.get('long_positions', 0)}L/{conc_data.get('short_positions', 0)}S positions"
        )
        await record_filter_block("position_concentration")
    elif conc_data.get("long_positions", 0) + conc_data.get("short_positions", 0) > 0:
        # Include for AI context even when passing
        checks["position_concentration"]["active"] = True

    # 鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺?    # Data completeness & live quality
    # 鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺?
    # 鈹€鈹€ Check 38: Market Data Completeness 鈹€鈹€
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
        await record_filter_block("data_completeness")

    # 鈹€鈹€ Check 39: Live trading data quality gate 鈹€鈹€
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
        await record_filter_block("live_data_quality")

    # 鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺?    # Final Verdict
    # 鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺?
    for name in disabled:
        for check_name, check in checks.items():
            if check_name.lower() == name:
                check["disabled"] = True
                check["passed"] = True

    score = calculate_filter_score(checks)

    hard_fail_count = sum(1 for c in checks.values() if not c.get("passed", True) and not c.get("disabled", False) and not c.get("soft_fail", False))

    if use_scoring:
        min_score = min_pass_score if min_pass_score is not None else float(thresholds.get("min_pass_score", ticker) or 0.0)
        all_passed = score >= min_score and hard_fail_count == 0
    else:
        all_passed = hard_fail_count == 0

    total_checks = len(checks)
    passed_count = sum(1 for c in checks.values() if c.get("passed", True) or c.get("disabled", False))

    all_reasons = (reasons if not all_passed else []) + soft_fail_reasons

    # FIX #6: Record ALL signals (passed or blocked) for accurate saturation tracking
    await _append_signal(signal, user_id, all_passed)

    if all_passed:
        logger.info(
            f"[PreFilter] PASSED score={score:.1f} ({passed_count}/{total_checks}) "
            f"- {signal.ticker} {signal.direction.value}"
        )
    else:
        logger.warning(
            f"[PreFilter] BLOCKED score={score:.1f} ({passed_count}/{total_checks}) "
            f"- {signal.ticker} {signal.direction.value}: {'; '.join(reasons)}"
        )

    final_reason = "; ".join(all_reasons) if all_reasons else f"All {total_checks} checks passed"

    # 鈹€鈹€ v5.2: Pipeline latency recording 鈹€鈹€
    _pipeline_duration = time.perf_counter() - _pipeline_start
    record_pipeline_latency(_pipeline_duration)

    # 鈹€鈹€ v5.2: Record blocked signal for feedback loop outcome evaluation 鈹€鈹€
    if not all_passed:
        _tracker_record_blocked_signal(
            ticker=ticker_key,
            direction=signal.direction.value,
            entry_price=signal.price,
            blocked_by=[c for c, v in checks.items() if not v.get("passed", True) and not v.get("disabled", False)],
            soft_fails=[c for c, v in checks.items() if v.get("soft_fail", False)],
        )

    return PreFilterResult(
        passed=all_passed,
        reason=final_reason,
        checks=checks,
        score=score,
    )


# 鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺?# v5.2 鈥?Filter Performance Feedback Loop
# Tracks per-check precision by evaluating blocked signal outcomes.
# PERF-OPTIMIZATION: These locks are used in synchronous helper functions. In a full async refactor,
# they should be converted to asyncio.Lock and the functions made async.
# NOTE: Current implementation is thread-safe for the existing sync usage pattern.
_PERF_LOCK = threading.RLock()
_OUTCOME_WINDOW_SECONDS = 3600  # Check outcomes after 1 hour
_MAX_PENDING_OUTCOMES = 500

_pending_outcomes: deque[dict[str, Any]] = deque(maxlen=_MAX_PENDING_OUTCOMES)
_check_performance: dict[str, dict[str, float]] = {}  # check_name -> {tp, fp, tn, fn, precision, sample_count}
_PERFORMANCE_FILE = "data/filter_performance.json"


def _load_performance() -> dict[str, dict[str, float]]:
    try:
        if os.path.exists(_PERFORMANCE_FILE):
            with open(_PERFORMANCE_FILE, encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, dict):
                    return {
                        k: {"tp": float(v.get("tp", 0)), "fp": float(v.get("fp", 0)),
                            "tn": float(v.get("tn", 0)), "fn": float(v.get("fn", 0)),
                            "precision": float(v.get("precision", 0)), "sample_count": float(v.get("sample_count", 0))}
                        for k, v in data.items()
                    }
    except (OSError, json.JSONDecodeError, ValueError, TypeError):
        pass
    return {}


def _save_performance(perf: dict) -> None:
    try:
        os.makedirs("data", exist_ok=True)
        perf_file = Path(_PERFORMANCE_FILE)
        # Write with atomic swap to prevent corruption
        tmp_file = perf_file.with_suffix(".tmp")
        with open(tmp_file, "w", encoding="utf-8") as f:
            json.dump(perf, f, indent=2)
        tmp_file.replace(perf_file)
    except (OSError, PermissionError):
        pass


_check_performance = _load_performance()


def _tracker_record_blocked_signal(
    ticker: str,
    direction: str,
    entry_price: float,
    blocked_by: list[str],
    soft_fails: list[str],
) -> None:
    """Record a blocked signal for later outcome evaluation."""
    with _PERF_LOCK:
        _pending_outcomes.append({
            "ticker": ticker,
            "direction": direction,
            "entry_price": entry_price,
            "blocked_by": blocked_by,
            "soft_fails": soft_fails,
            "blocked_at": time.time(),
            "evaluated": False,
        })


def evaluate_blocked_outcomes(
    current_prices: dict[str, float] | None = None,
    market_data: Any | None = None,
) -> dict[str, Any]:
    """
    Evaluate outcomes of previously blocked signals.

    For each blocked signal older than _OUTCOME_WINDOW_SECONDS:
    - Compare entry price to current price
    - If signal would have lost money 鈫?True Positive (correct block)
    - If signal would have made money 鈫?False Positive (wrong block)
    - Update per-check precision

    Returns evaluation summary.
    """
    global _check_performance

    now = time.time()
    evaluated = 0
    outcome_summary: dict[str, Any] = {"evaluated": 0, "new_tp": 0, "new_fp": 0}

    with _PERF_LOCK:
        for entry in list(_pending_outcomes):
            if entry.get("evaluated"):
                continue
            age = now - entry.get("blocked_at", 0)
            if age < _OUTCOME_WINDOW_SECONDS:
                continue

            ticker = entry.get("ticker", "")
            direction = entry.get("direction", "")
            entry_price = entry.get("entry_price", 0)

            # Get current price from provided data or from market context
            current_price = 0.0
            if current_prices and ticker in current_prices:
                current_price = float(current_prices[ticker])
            elif market_data and hasattr(market_data, "current_price"):
                current_price = float(market_data.current_price or 0)

            if current_price <= 0 or entry_price <= 0:
                continue

            pnl_pct = (current_price - entry_price) / entry_price * 100
            if direction == "short":
                pnl_pct = -pnl_pct

            would_have_lost = pnl_pct < -0.5
            would_have_won = pnl_pct > 0.5

            # Update per-check stats
            for check_name in entry.get("blocked_by", []):
                if check_name not in _check_performance:
                    _check_performance[check_name] = {"tp": 0, "fp": 0, "tn": 0, "fn": 0, "precision": 0, "sample_count": 0}

                perf = _check_performance[check_name]
                if would_have_lost:
                    perf["tp"] += 1  # True Positive: correctly blocked a losing trade
                elif would_have_won:
                    perf["fp"] += 1  # False Positive: incorrectly blocked a winning trade

                perf["sample_count"] += 1
                tp, fp = perf["tp"], perf["fp"]
                perf["precision"] = round((tp / (tp + fp)) * 100, 2) if (tp + fp) > 0 else 0

                if would_have_lost:
                    outcome_summary["new_tp"] += 1
                elif would_have_won:
                    outcome_summary["new_fp"] += 1

            entry["evaluated"] = True
            evaluated += 1

        outcome_summary["evaluated"] = evaluated

        # Persist periodically
        if evaluated > 0:
            _save_performance(_check_performance)

    return outcome_summary


def get_check_performance() -> dict[str, dict[str, float]]:
    """Return per-check precision statistics."""
    with _PERF_LOCK:
        return {k: dict(v) for k, v in _check_performance.items()}


def get_weight_suggestions() -> dict[str, dict[str, Any]]:
    """
    Generate weight adjustment suggestions based on performance data.

    Low-precision checks (<40% precision) should have weight reduced.
    High-precision checks (>80% precision) should have weight increased.
    """
    suggestions: dict[str, dict[str, Any]] = {}
    with _PERF_LOCK:
        for check_name, perf in _check_performance.items():
            precision = perf.get("precision", 0)
            sample_count = perf.get("sample_count", 0)
            current_weight = FILTER_WEIGHTS.get(check_name, 5.0)

            if sample_count < 5:
                suggestions[check_name] = {"action": "insufficient_data", "current_weight": current_weight, "suggested_weight": current_weight}
                continue

            if precision >= 80:
                suggestions[check_name] = {
                    "action": "increase",
                    "current_weight": current_weight,
                    "suggested_weight": round(min(current_weight * 1.3, 16.0), 1),
                    "precision": precision,
                    "sample_count": int(sample_count),
                }
            elif precision < 30:
                suggestions[check_name] = {
                    "action": "decrease",
                    "current_weight": current_weight,
                    "suggested_weight": round(max(current_weight * 0.6, 2.0), 1),
                    "precision": precision,
                    "sample_count": int(sample_count),
                }
            else:
                suggestions[check_name] = {
                    "action": "maintain",
                    "current_weight": current_weight,
                    "suggested_weight": current_weight,
                    "precision": precision,
                    "sample_count": int(sample_count),
                }

    return suggestions


def reset_check_performance() -> None:
    """Reset all performance tracking data."""
    global _check_performance
    with _PERF_LOCK:
        _check_performance = {}
        _pending_outcomes.clear()
        _save_performance({})


# 鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺?# v5.2 鈥?Prefilter Latency Monitor with Degradation
# Tracks per-check timing and auto-degrades slow checks.
# PERF-OPTIMIZATION: This lock is used in synchronous helper functions. In a full async refactor,
# it should be converted to asyncio.Lock and the functions made async.
# NOTE: Current implementation is thread-safe for the existing sync usage pattern.
_LATENCY_LOCK = threading.RLock()
_check_latency: dict[str, deque[float]] = {}  # check_name -> deque of recent durations
_LATENCY_WINDOW_SIZE = 20
_DEGRADATION_THRESHOLD_SECS = 3.0  # Auto-skip checks averaging > 3s
_DEGRADED_CHECKS: set[str] = set()  # Checks currently in degraded state
_pipeline_latency: deque[float] = deque(maxlen=100)  # Overall pipeline duration


class PrefilterTimer:
    """Context manager for timing a single check."""

    def __init__(self, check_name: str):
        self._name = check_name
        self._start = 0.0

    def __enter__(self):
        self._start = time.perf_counter()
        return self

    def __exit__(self, *args):
        duration = time.perf_counter() - self._start
        _record_check_latency(self._name, duration)

    async def __aenter__(self):
        self._start = time.perf_counter()
        return self

    async def __aexit__(self, *args):
        duration = time.perf_counter() - self._start
        # Run sync function in thread pool to avoid blocking event loop
        await asyncio.get_event_loop().run_in_executor(None, _record_check_latency, self._name, duration)


def _record_check_latency(check_name: str, duration_secs: float) -> None:
    """Record latency for a single check and evaluate degradation."""
    global _DEGRADED_CHECKS
    with _LATENCY_LOCK:
        if check_name not in _check_latency:
            _check_latency[check_name] = deque(maxlen=_LATENCY_WINDOW_SIZE)
        _check_latency[check_name].append(duration_secs)

        recent = _check_latency[check_name]
        if len(recent) >= 5:
            avg = sum(recent) / len(recent)
            if avg > _DEGRADATION_THRESHOLD_SECS and check_name not in _DEGRADED_CHECKS:
                _DEGRADED_CHECKS.add(check_name)
                logger.warning(
                    f"[PrefilterLatency] DEGRADED check '{check_name}': "
                    f"avg {avg:.2f}s over {len(recent)} samples (threshold: {_DEGRADATION_THRESHOLD_SECS}s)"
                )


def record_pipeline_latency(duration_secs: float) -> None:
    """Record overall prefilter pipeline duration."""
    with _LATENCY_LOCK:
        _pipeline_latency.append(duration_secs)


def is_check_degraded(check_name: str) -> bool:
    """Check if a check is currently in degraded state."""
    with _LATENCY_LOCK:
        return check_name in _DEGRADED_CHECKS


def get_latency_stats() -> dict[str, Any]:
    """Return comprehensive latency statistics."""
    with _LATENCY_LOCK:
        check_stats = {}
        for name, samples in _check_latency.items():
            if samples:
                check_stats[name] = {
                    "avg_secs": round(sum(samples) / len(samples), 4),
                    "max_secs": round(max(samples), 4),
                    "min_secs": round(min(samples), 4),
                    "sample_count": len(samples),
                    "degraded": name in _DEGRADED_CHECKS,
                }

        pipeline_stats = {}
        if _pipeline_latency:
            pipeline_stats = {
                "avg_secs": round(sum(_pipeline_latency) / len(_pipeline_latency), 4),
                "max_secs": round(max(_pipeline_latency), 4),
                "min_secs": round(min(_pipeline_latency), 4),
                "sample_count": len(_pipeline_latency),
            }

        return {
            "checks": check_stats,
            "pipeline": pipeline_stats,
            "degraded_checks": sorted(_DEGRADED_CHECKS),
            "degradation_threshold_secs": _DEGRADATION_THRESHOLD_SECS,
        }


def clear_degraded_checks() -> None:
    """Clear the degraded check list (for recovery)."""
    global _DEGRADED_CHECKS
    with _LATENCY_LOCK:
        _DEGRADED_CHECKS.clear()
        logger.info("[PrefilterLatency] Degraded checks cleared 鈥?all checks re-enabled")


def reset_latency_stats() -> None:
    """Reset all latency statistics."""
    global _check_latency, _pipeline_latency, _DEGRADED_CHECKS
    with _LATENCY_LOCK:
        _check_latency.clear()
        _pipeline_latency.clear()
        _DEGRADED_CHECKS.clear()
