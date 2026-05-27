"""Unified OHLCV provider used by the automatic market scanner.

This module is deliberately scanner-scoped. The normal signal execution path
continues to use market_data.fetch_market_context so we do not fork the trading
pipeline's market snapshot behaviour.
"""
import asyncio
import time
from datetime import datetime
from typing import Any

import httpx
from loguru import logger
from pydantic import BaseModel, Field

from commodity_data import get_yfinance_symbol, is_special_commodity
from core.config import settings
from core.utils.datetime import utcnow
from market_data import (
    _calculate_atr,
    _calculate_ema,
    _calculate_rsi,
    _calculate_volume_profile,
    _calculate_vwap,
    _market_data_exchange_ids,
    fetch_market_context,
    fetch_ohlcv_history,
)


class NormalizedCandle(BaseModel):
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0


class SymbolMapping(BaseModel):
    watch_symbol: str
    data_symbol: str
    exchange_symbol: str
    exchange_name: str | None = None
    market_type: str | None = None
    mapped_asset: bool = False
    data_source: str = "ccxt"
    data_source_policy: str = "fallback"
    source_exchange: str | None = None
    source_market_type: str | None = None
    source_symbol: str | None = None
    target_exchange: str | None = None
    target_market_type: str | None = None
    actual_data_source: str = ""
    tradable: bool = True
    tradability_reason: str = ""
    universe_source: str = "manual"
    liquidity_tier: str = "unknown"
    quote_volume: float = 0.0


class OHLCVBundle(BaseModel):
    mapping: SymbolMapping
    current_price: float = 0.0
    price_change_24h: float = 0.0
    volume_24h: float = 0.0
    volume_change_pct: float = 0.0
    high_24h: float = 0.0
    low_24h: float = 0.0
    timeframes: dict[str, list[NormalizedCandle]] = Field(default_factory=dict)
    indicators: dict[str, dict[str, Any]] = Field(default_factory=dict)
    data_quality: dict[str, Any] = Field(default_factory=dict)
    oi_change_pct: float | None = None
    oi_current: float | None = None
    funding_rate: float | None = None
    orderbook_imbalance: float | None = None
    long_short_ratio: float | None = None

    @property
    def candles(self) -> dict[str, list[NormalizedCandle]]:
        return self.timeframes

    @property
    def primary_timeframe(self) -> str:
        configured = self.data_quality.get("primary_timeframe")
        if configured:
            return str(configured)
        if self.timeframes:
            return next(iter(self.timeframes.keys()))
        return (settings.scanner.timeframes or ["1h"])[0]

    @property
    def quality_passed(self) -> bool:
        return bool(self.data_quality.get("passed", False))

    @property
    def quality_reasons(self) -> list[str]:
        return list(self.data_quality.get("reasons") or [])

    @property
    def bid_ask_spread_pct(self) -> float:
        try:
            return float(self.data_quality.get("spread_pct") or 0.0)
        except (TypeError, ValueError):
            return 0.0


def timeframe_to_seconds(timeframe: str) -> int:
    tf = str(timeframe or "1h").lower().strip()
    unit = tf[-1:]
    try:
        value = int(tf[:-1]) if unit in {"m", "h", "d"} else int(tf)
    except ValueError:
        return 3600
    if unit == "m" or unit.isdigit():
        return value * 60
    if unit == "h":
        return value * 3600
    if unit == "d":
        return value * 86400
    return 3600


def _dt_from_timestamp(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value
    if isinstance(value, (int, float)):
        raw = float(value)
        if raw > 10_000_000_000:
            raw = raw / 1000.0
        return datetime.utcfromtimestamp(raw)
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).replace(tzinfo=None)
        except ValueError:
            return utcnow()
    return utcnow()


def _optional_float(value: Any) -> float | None:
    try:
        parsed = float(value)
        return parsed if parsed == parsed else None
    except (TypeError, ValueError):
        return None


def _normalize_market_type(value: Any, default: str = "contract") -> str:
    normalized = str(value or default or "contract").lower().strip()
    if normalized in {"future", "futures", "swap", "linear", "inverse"}:
        return "contract"
    if normalized == "spot":
        return "spot"
    return "contract"


def _normalize_data_policy(value: Any) -> str:
    normalized = str(value or settings.scanner.data_source_policy or "fallback").lower().strip()
    return normalized if normalized in {"strict", "fallback", "confirm"} else "fallback"


def _candles_from_history(rows: list[dict[str, Any]]) -> list[NormalizedCandle]:
    candles: list[NormalizedCandle] = []
    for row in rows:
        try:
            candles.append(
                NormalizedCandle(
                    timestamp=_dt_from_timestamp(row.get("timestamp") or row.get("time") or row.get("datetime")),
                    open=float(row.get("open") or 0),
                    high=float(row.get("high") or 0),
                    low=float(row.get("low") or 0),
                    close=float(row.get("close") or 0),
                    volume=float(row.get("volume") or 0),
                )
            )
        except (TypeError, ValueError):
            continue
    return [c for c in candles if c.open > 0 and c.high > 0 and c.low > 0 and c.close > 0]


def _indicator_snapshot(candles: list[NormalizedCandle]) -> dict[str, Any]:
    closes = [c.close for c in candles]
    ohlcv = [
        [int(c.timestamp.timestamp() * 1000), c.open, c.high, c.low, c.close, c.volume]
        for c in candles
    ]
    current = closes[-1] if closes else 0.0
    atr = _calculate_atr(ohlcv, 14) if len(ohlcv) >= 15 else None
    atr_pct = (atr / current * 100.0) if atr and current > 0 else None
    volumes = [c.volume for c in candles[-24:]]
    avg_volume = (sum(volumes) / len(volumes)) if volumes else 0.0
    current_volume = volumes[-1] if volumes else 0.0
    volume_change_pct = ((current_volume - avg_volume) / avg_volume * 100.0) if avg_volume > 0 else 0.0
    volume_ratio = (current_volume / avg_volume) if avg_volume > 0 else None
    macd = _calculate_macd(closes)
    adx_val = _calculate_adx(ohlcv, 14) if len(ohlcv) >= 16 else None
    vwap_result = _calculate_vwap(ohlcv, 24) if len(ohlcv) >= 24 else {"vwap": None, "distance_pct": None}
    poc_result = _calculate_volume_profile(ohlcv, lookback=96) if len(ohlcv) >= 24 else {"poc": None, "value_area_high": None, "value_area_low": None}
    return {
        "ema_fast": _calculate_ema(closes, 20) if len(closes) >= 20 else None,
        "ema_slow": _calculate_ema(closes, 50) if len(closes) >= 50 else None,
        "rsi": _calculate_rsi(closes, 14) if len(closes) >= 15 else None,
        "ema20": _calculate_ema(closes, 20) if len(closes) >= 20 else None,
        "ema50": _calculate_ema(closes, 50) if len(closes) >= 50 else None,
        "ema200": _calculate_ema(closes, 200) if len(closes) >= 200 else None,
        "rsi14": _calculate_rsi(closes, 14) if len(closes) >= 15 else None,
        "atr": atr,
        "atr_pct": atr_pct,
        "volume_change_pct": volume_change_pct,
        "volume_ratio": volume_ratio,
        "macd": macd.get("macd"),
        "macd_signal": macd.get("signal"),
        "macd_hist": macd.get("hist"),
        "adx": adx_val,
        "vwap": vwap_result.get("vwap"),
        "vwap_distance_pct": vwap_result.get("distance_pct"),
        "volume_profile_poc": poc_result.get("poc"),
        "value_area_high": poc_result.get("value_area_high"),
        "value_area_low": poc_result.get("value_area_low"),
        "market_regime": _classify_regime(adx_val, atr_pct),
    }


def _classify_regime(adx_val: float | None, atr_pct_val: float | None) -> str:
    if adx_val is None:
        return "unknown"
    if adx_val >= 25:
        return "trending"
    if adx_val >= 20:
        return "transitional"
    return "ranging"


def _calculate_macd(closes: list[float]) -> dict[str, float | None]:
    if len(closes) < 35:
        return {"macd": None, "signal": None, "hist": None}
    macd_values: list[float] = []
    for idx in range(26, len(closes) + 1):
        fast = _calculate_ema(closes[:idx], 12)
        slow = _calculate_ema(closes[:idx], 26)
        if fast is None or slow is None:
            continue
        macd_values.append(fast - slow)
    if not macd_values:
        return {"macd": None, "signal": None, "hist": None}
    macd_line = macd_values[-1]
    signal = _calculate_ema(macd_values, 9) if len(macd_values) >= 9 else None
    hist = macd_line - signal if signal is not None else None
    return {"macd": macd_line, "signal": signal, "hist": hist}


def _calculate_adx(ohlcv: list[list[float]], period: int = 14) -> float | None:
    if len(ohlcv) < period + 2:
        return None
    tr_values: list[float] = []
    plus_dm: list[float] = []
    minus_dm: list[float] = []
    for idx in range(1, len(ohlcv)):
        high = float(ohlcv[idx][2])
        low = float(ohlcv[idx][3])
        prev_high = float(ohlcv[idx - 1][2])
        prev_low = float(ohlcv[idx - 1][3])
        prev_close = float(ohlcv[idx - 1][4])
        up_move = high - prev_high
        down_move = prev_low - low
        tr_values.append(max(high - low, abs(high - prev_close), abs(low - prev_close)))
        plus_dm.append(up_move if up_move > down_move and up_move > 0 else 0.0)
        minus_dm.append(down_move if down_move > up_move and down_move > 0 else 0.0)
    if len(tr_values) < period:
        return None
    dx_values: list[float] = []
    for idx in range(period, len(tr_values) + 1):
        tr_sum = sum(tr_values[idx - period:idx])
        if tr_sum <= 0:
            continue
        plus_di = 100.0 * sum(plus_dm[idx - period:idx]) / tr_sum
        minus_di = 100.0 * sum(minus_dm[idx - period:idx]) / tr_sum
        denom = plus_di + minus_di
        if denom <= 0:
            continue
        dx_values.append(100.0 * abs(plus_di - minus_di) / denom)
    if len(dx_values) < period:
        return sum(dx_values) / len(dx_values) if dx_values else None
    return sum(dx_values[-period:]) / period


def _candle_gap_ratio(candles: list[NormalizedCandle], timeframe: str) -> float:
    if len(candles) < 3:
        return 0.0
    expected = max(60, timeframe_to_seconds(timeframe))
    gaps = 0
    comparisons = 0
    recent = candles[-80:]
    for prev, cur in zip(recent[:-1], recent[1:], strict=False):
        delta = abs((cur.timestamp - prev.timestamp).total_seconds())
        comparisons += 1
        if delta > expected * 1.8:
            gaps += 1
    return (gaps / comparisons) if comparisons else 0.0


class _BoundedTTLCache:
    """Simple bounded TTL cache with FIFO eviction."""

    def __init__(self, maxsize: int = 500, ttl: float = 45.0) -> None:
        self.maxsize = maxsize
        self.ttl = ttl
        self._store: dict[Any, tuple[float, Any]] = {}

    def get(self, key: Any) -> Any | None:
        now = time.monotonic()
        entry = self._store.get(key)
        if entry is None:
            return None
        if now - entry[0] > self.ttl:
            self._store.pop(key, None)
            return None
        return entry[1]

    def set(self, key: Any, value: Any) -> None:
        now = time.monotonic()
        if len(self._store) >= self.maxsize and key not in self._store:
            oldest = next(iter(self._store))
            self._store.pop(oldest, None)
        self._store[key] = (now, value)

    def clear(self) -> None:
        self._store.clear()


class UnifiedOHLCVProvider:
    """Scanner-only OHLCV facade for crypto and commodity-like symbols."""

    def __init__(self) -> None:
        self._bundle_cache = _BoundedTTLCache(maxsize=500, ttl=45.0)

    def resolve_mapping(
        self,
        watch_symbol: str,
        *,
        exchange_symbol: str | None = None,
        target_exchange: str | None = None,
        target_market_type: str | None = None,
        source_exchange: str | None = None,
        source_market_type: str | None = None,
        source_symbol: str | None = None,
        data_source_policy: str | None = None,
        tradable: bool = True,
        tradability_reason: str = "",
        universe_source: str = "manual",
        liquidity_tier: str = "unknown",
        quote_volume: float = 0.0,
    ) -> SymbolMapping:
        watch = str(watch_symbol or "").upper().strip()
        raw_map = settings.scanner.symbol_map.get(watch) or settings.scanner.symbol_map.get(watch.replace("/", ""))
        if isinstance(raw_map, dict):
            mapped_exchange_symbol = str(raw_map.get("exchange_symbol") or watch).upper().strip()
            exchange_name = str(raw_map.get("exchange_name") or settings.exchange.name).lower().strip()
            market_type = str(
                raw_map.get("type") or raw_map.get("market_type")
                or settings.exchange.market_type
            ).lower().strip()
            mapped_source_exchange = str(raw_map.get("source_exchange") or exchange_name).lower().strip()
            mapped_source_market_type = str(raw_map.get("source_market_type") or market_type).lower().strip()
            mapped_source_symbol = str(raw_map.get("source_symbol") or raw_map.get("data_symbol") or watch).upper().strip()
        else:
            mapped_exchange_symbol = watch
            exchange_name = settings.exchange.name
            market_type = settings.exchange.market_type
            mapped_source_exchange = settings.scanner.source_exchange or exchange_name
            mapped_source_market_type = settings.scanner.source_market_type or market_type
            mapped_source_symbol = watch

        resolved_exchange_symbol = str(exchange_symbol or mapped_exchange_symbol or watch).upper().strip()
        resolved_target_exchange = str(target_exchange or exchange_name or settings.exchange.name).lower().strip()
        resolved_target_market_type = _normalize_market_type(target_market_type or market_type, settings.exchange.market_type)
        resolved_source_exchange = str(source_exchange or mapped_source_exchange or resolved_target_exchange).lower().strip()
        resolved_source_market_type = _normalize_market_type(source_market_type or mapped_source_market_type, resolved_target_market_type)
        resolved_source_symbol = str(source_symbol or mapped_source_symbol or resolved_exchange_symbol).upper().strip()

        commodity_type = is_special_commodity(watch)
        data_symbol = get_yfinance_symbol(watch) if commodity_type else watch
        return SymbolMapping(
            watch_symbol=watch,
            data_symbol=data_symbol,
            exchange_symbol=resolved_exchange_symbol,
            exchange_name=resolved_target_exchange,
            market_type=resolved_target_market_type,
            mapped_asset=resolved_exchange_symbol != watch,
            data_source="yfinance" if commodity_type else "ccxt",
            data_source_policy=_normalize_data_policy(data_source_policy),
            source_exchange=resolved_source_exchange,
            source_market_type=resolved_source_market_type,
            source_symbol=resolved_source_symbol,
            target_exchange=resolved_target_exchange,
            target_market_type=resolved_target_market_type,
            actual_data_source="yfinance" if commodity_type else "",
            tradable=bool(tradable),
            tradability_reason=str(tradability_reason or ""),
            universe_source=str(universe_source or settings.scanner.source_mode or "manual"),
            liquidity_tier=str(liquidity_tier or "unknown"),
            quote_volume=float(quote_volume or 0.0),
        )

    async def get_bundle(
        self,
        watch_symbol: str,
        timeframes: list[str] | None = None,
        **mapping_overrides: Any,
    ) -> OHLCVBundle:
        mapping = self.resolve_mapping(watch_symbol, **mapping_overrides)
        requested_tfs = timeframes or settings.scanner.timeframes
        cache_key = (
            mapping.watch_symbol,
            mapping.exchange_symbol,
            mapping.source_exchange,
            mapping.source_market_type,
            mapping.data_source_policy,
            tuple(requested_tfs),
        )
        ttl = max(0, int(settings.scanner.bundle_cache_ttl_secs))
        if ttl > 0:
            cached = self._bundle_cache.get(cache_key)
            if cached is not None:
                return cached
        timeframes_data: dict[str, list[NormalizedCandle]] = {}
        indicators: dict[str, dict[str, Any]] = {}

        for tf in requested_tfs:
            candles = await self._fetch_candles(mapping, tf)
            timeframes_data[tf] = candles
            indicators[tf] = _indicator_snapshot(candles)

        current_price = 0.0
        for tf in requested_tfs:
            if timeframes_data.get(tf):
                current_price = timeframes_data[tf][-1].close
                break

        spread_pct = 0.0
        price_deviation_pct = 0.0
        oi_change_pct: float | None = None
        oi_current: float | None = None
        funding_rate: float | None = None
        orderbook_imbalance: float | None = None
        long_short_ratio: float | None = None
        price_change_24h = 0.0
        volume_24h = 0.0
        volume_change_pct = 0.0
        high_24h = 0.0
        low_24h = 0.0
        market_context_available = False
        market_context_source = ""
        market_context_error = ""
        orderbook_bid_depth_usdt: float | None = None
        orderbook_ask_depth_usdt: float | None = None
        orderbook_top_bid_depth_usdt: float | None = None
        orderbook_top_ask_depth_usdt: float | None = None
        missing_microstructure: list[str] = []
        confirmation: dict[str, Any] | None = None
        if mapping.data_source == "ccxt":
            try:
                ohlcv_price = current_price
                source_ids = self._exchange_ids_for_mapping(mapping)
                cache_scope = self._cache_scope(mapping) if source_ids else None
                context_symbol = mapping.source_symbol or mapping.exchange_symbol
                if source_ids:
                    context = await fetch_market_context(
                        context_symbol,
                        exchange_ids=source_ids,
                        market_type=mapping.source_market_type,
                        cache_scope=cache_scope,
                    )
                else:
                    context = await fetch_market_context(context_symbol)
                market_context_source = str(getattr(context, "_market_data_source", "") or "")
                mapping.actual_data_source = market_context_source or (mapping.source_exchange or "")
                market_context_available = bool(market_context_source and float(context.current_price or 0.0) > 0)
                context_price = float(context.current_price or current_price)
                if ohlcv_price > 0 and context_price > 0:
                    price_deviation_pct = abs(context_price - ohlcv_price) / ohlcv_price * 100.0
                current_price = context_price
                spread_pct = float(context.bid_ask_spread or 0.0)
                oi_change_pct = float(context.open_interest_change_pct) if context.open_interest_change_pct is not None else None
                oi_current = float(context.open_interest) if context.open_interest is not None else None
                funding_rate = float(context.funding_rate) if context.funding_rate is not None else None
                orderbook_imbalance = float(context.orderbook_imbalance) if context.orderbook_imbalance is not None else None
                long_short_ratio = float(context.long_short_ratio) if context.long_short_ratio is not None else None
                price_change_24h = float(context.price_change_24h or 0.0)
                volume_24h = float(context.volume_24h or 0.0)
                volume_change_pct = float(context.volume_change_pct or 0.0)
                high_24h = float(context.high_24h or 0.0)
                low_24h = float(context.low_24h or 0.0)
                orderbook_bid_depth_usdt = _optional_float(getattr(context, "_orderbook_bid_depth_usdt", None))
                orderbook_ask_depth_usdt = _optional_float(getattr(context, "_orderbook_ask_depth_usdt", None))
                orderbook_top_bid_depth_usdt = _optional_float(getattr(context, "_orderbook_top_bid_depth_usdt", None))
                orderbook_top_ask_depth_usdt = _optional_float(getattr(context, "_orderbook_top_ask_depth_usdt", None))
                if funding_rate is None:
                    missing_microstructure.append("funding_rate")
                if orderbook_imbalance is None:
                    missing_microstructure.append("orderbook_imbalance")
                if oi_current is None:
                    missing_microstructure.append("open_interest")
                if volume_24h <= 0:
                    missing_microstructure.append("volume_24h")
                if orderbook_bid_depth_usdt is None or orderbook_ask_depth_usdt is None:
                    missing_microstructure.append("orderbook_depth")
            except Exception as exc:
                market_context_error = str(exc)
                logger.debug(f"[Scanner/OHLCV] market context unavailable for {mapping.exchange_symbol}: {exc}")
            if mapping.data_source_policy == "confirm":
                confirmation = await self._confirm_market_context(mapping, current_price, volume_24h)

        bundle = OHLCVBundle(
            mapping=mapping,
            current_price=current_price,
            price_change_24h=price_change_24h,
            volume_24h=volume_24h,
            volume_change_pct=volume_change_pct,
            high_24h=high_24h,
            low_24h=low_24h,
            timeframes=timeframes_data,
            indicators=indicators,
            data_quality={},
            oi_change_pct=oi_change_pct,
            oi_current=oi_current,
            funding_rate=funding_rate,
            orderbook_imbalance=orderbook_imbalance,
            long_short_ratio=long_short_ratio,
        )
        bundle.data_quality = self._assess_quality(
            bundle,
            spread_pct=spread_pct,
            price_deviation_pct=price_deviation_pct,
            market_context_available=market_context_available,
            market_context_source=market_context_source,
            market_context_error=market_context_error,
            confirmation=confirmation,
            missing_microstructure=missing_microstructure,
            orderbook_bid_depth_usdt=orderbook_bid_depth_usdt,
            orderbook_ask_depth_usdt=orderbook_ask_depth_usdt,
            orderbook_top_bid_depth_usdt=orderbook_top_bid_depth_usdt,
            orderbook_top_ask_depth_usdt=orderbook_top_ask_depth_usdt,
        )
        if ttl > 0:
            self._bundle_cache.set(cache_key, bundle)
        return bundle

    async def get_many(self, watchlist: list[str], timeframes: list[str] | None = None) -> list[OHLCVBundle]:
        results = await asyncio.gather(
            *(self.get_bundle(symbol, timeframes=timeframes) for symbol in watchlist),
            return_exceptions=True,
        )
        bundles: list[OHLCVBundle] = []
        for symbol, result in zip(watchlist, results, strict=False):
            if isinstance(result, Exception):
                logger.warning(f"[Scanner/OHLCV] bundle fetch failed for {symbol}: {result}")
                continue
            bundles.append(result)
        return bundles

    def _exchange_ids_for_mapping(self, mapping: SymbolMapping) -> list[str] | None:
        if mapping.data_source != "ccxt":
            return None
        primary = str(mapping.source_exchange or settings.exchange.name).lower().strip()
        policy = _normalize_data_policy(mapping.data_source_policy)
        source_type = _normalize_market_type(mapping.source_market_type, settings.exchange.market_type)
        default_type = _normalize_market_type(settings.exchange.market_type, "contract")
        if policy == "fallback" and primary == settings.exchange.name and source_type == default_type:
            return None
        include_fallbacks = policy == "fallback"
        return _market_data_exchange_ids(primary, include_fallbacks=include_fallbacks)

    @staticmethod
    def _cache_scope(mapping: SymbolMapping) -> str:
        return ":".join([
            "scanner",
            str(mapping.data_source_policy or "fallback"),
            str(mapping.source_exchange or settings.exchange.name),
            str(mapping.source_market_type or settings.exchange.market_type),
        ])

    async def _confirm_market_context(
        self,
        mapping: SymbolMapping,
        primary_price: float,
        primary_volume_24h: float,
    ) -> dict[str, Any]:
        primary = str(mapping.source_exchange or settings.exchange.name).lower().strip()
        candidates = [source for source in _market_data_exchange_ids(primary, include_fallbacks=True) if source != primary]
        if not candidates:
            return {"passed": False, "reason": "confirmation_unavailable", "source": ""}

        for exchange_id in candidates[:3]:
            try:
                context = await fetch_market_context(
                    mapping.source_symbol or mapping.exchange_symbol,
                    exchange_ids=[exchange_id],
                    market_type=mapping.source_market_type,
                    cache_scope=f"scanner-confirm:{exchange_id}:{mapping.source_market_type}",
                )
                confirm_price = float(context.current_price or 0.0)
                if confirm_price <= 0 or primary_price <= 0:
                    continue
                price_deviation_pct = abs(confirm_price - primary_price) / primary_price * 100.0
                confirm_volume = float(context.volume_24h or 0.0)
                volume_deviation_pct = 0.0
                if primary_volume_24h > 0 and confirm_volume > 0:
                    volume_deviation_pct = abs(confirm_volume - primary_volume_24h) / primary_volume_24h * 100.0
                max_deviation = float(settings.scanner.max_price_deviation_pct)
                max_volume_deviation = float(settings.scanner.confirm_max_volume_deviation_pct)
                confirm_spread_pct = float(context.bid_ask_spread or 0.0)
                spread_passed = confirm_spread_pct <= float(settings.scanner.max_spread_pct) if confirm_spread_pct > 0 else True
                volume_passed = volume_deviation_pct <= max_volume_deviation if volume_deviation_pct > 0 else True
                price_passed = price_deviation_pct <= max_deviation
                passed = price_passed and volume_passed and spread_passed
                reason = ""
                if not price_passed:
                    reason = "confirmation_price_deviation"
                elif not volume_passed:
                    reason = "confirmation_volume_deviation"
                elif not spread_passed:
                    reason = "confirmation_spread_too_wide"
                return {
                    "passed": passed,
                    "reason": reason,
                    "source": str(getattr(context, "_market_data_source", "") or exchange_id),
                    "price": confirm_price,
                    "price_deviation_pct": round(price_deviation_pct, 4),
                    "volume_24h": confirm_volume,
                    "volume_deviation_pct": round(volume_deviation_pct, 4),
                    "max_price_deviation_pct": max_deviation,
                    "max_volume_deviation_pct": max_volume_deviation,
                    "spread_pct": confirm_spread_pct,
                    "max_spread_pct": float(settings.scanner.max_spread_pct),
                }
            except Exception as exc:
                logger.debug(f"[Scanner/OHLCV] confirmation source {exchange_id} failed for {mapping.exchange_symbol}: {exc}")
        return {"passed": False, "reason": "confirmation_unavailable", "source": ""}

    async def _fetch_candles(self, mapping: SymbolMapping, timeframe: str) -> list[NormalizedCandle]:
        if mapping.data_source == "yfinance":
            return await self._fetch_yfinance_candles(mapping.data_symbol, timeframe)
        source_ids = self._exchange_ids_for_mapping(mapping)
        history_symbol = mapping.source_symbol or mapping.exchange_symbol
        if source_ids:
            rows = await fetch_ohlcv_history(
                history_symbol,
                timeframe=timeframe,
                days=self._days_for_timeframe(timeframe),
                exchange_ids=source_ids,
                market_type=mapping.source_market_type,
            )
        else:
            rows = await fetch_ohlcv_history(
                history_symbol,
                timeframe=timeframe,
                days=self._days_for_timeframe(timeframe),
            )
        return _candles_from_history(rows)

    @staticmethod
    def _days_for_timeframe(timeframe: str) -> int:
        seconds = timeframe_to_seconds(timeframe)
        candles_needed = 240
        return max(5, min(120, int((candles_needed * seconds) / 86400) + 2))

    async def _fetch_yfinance_candles(self, symbol: str, timeframe: str) -> list[NormalizedCandle]:
        interval = self._yf_interval(timeframe)
        range_value = "60d" if interval in {"1h", "90m"} else "30d"
        try:
            import yfinance

            ticker_obj = await asyncio.to_thread(yfinance.Ticker, symbol)
            hist = await asyncio.to_thread(lambda: ticker_obj.history(period=range_value, interval=interval))
            candles: list[NormalizedCandle] = []
            if not hist.empty:
                for idx, row in hist.iterrows():
                    candles.append(
                        NormalizedCandle(
                            timestamp=_dt_from_timestamp(idx.to_pydatetime() if hasattr(idx, "to_pydatetime") else idx),
                            open=float(row.get("Open") or 0),
                            high=float(row.get("High") or 0),
                            low=float(row.get("Low") or 0),
                            close=float(row.get("Close") or 0),
                            volume=float(row.get("Volume") or 0),
                        )
                    )
            return [c for c in candles if c.close > 0]
        except Exception as exc:
            logger.debug(f"[Scanner/OHLCV] yfinance package fetch failed for {symbol}: {exc}")

        return await self._fetch_yfinance_chart(symbol, interval, range_value)

    async def _fetch_yfinance_chart(self, symbol: str, interval: str, range_value: str) -> list[NormalizedCandle]:
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
        params = {"interval": interval, "range": range_value}
        headers = {"User-Agent": "Mozilla/5.0"}
        try:
            async with httpx.AsyncClient(timeout=15.0, headers=headers) as client:
                resp = await client.get(url, params=params)
                resp.raise_for_status()
            data = resp.json()
            result = (data.get("chart", {}).get("result") or [None])[0] or {}
            timestamps = result.get("timestamp") or []
            quote = ((result.get("indicators") or {}).get("quote") or [{}])[0]
            candles: list[NormalizedCandle] = []
            for idx, ts in enumerate(timestamps):
                try:
                    open_ = quote.get("open", [])[idx]
                    high = quote.get("high", [])[idx]
                    low = quote.get("low", [])[idx]
                    close = quote.get("close", [])[idx]
                    volume = quote.get("volume", [0])[idx] or 0
                    if None in (open_, high, low, close):
                        continue
                    candles.append(
                        NormalizedCandle(
                            timestamp=_dt_from_timestamp(ts),
                            open=float(open_),
                            high=float(high),
                            low=float(low),
                            close=float(close),
                            volume=float(volume),
                        )
                    )
                except (IndexError, TypeError, ValueError):
                    continue
            return candles
        except Exception as exc:
            logger.warning(f"[Scanner/OHLCV] Yahoo chart fetch failed for {symbol}: {exc}")
            return []

    @staticmethod
    def _yf_interval(timeframe: str) -> str:
        tf = str(timeframe or "1h").lower().strip()
        if tf in {"1m", "2m", "5m", "15m", "30m", "60m", "90m", "1h", "1d"}:
            return "60m" if tf == "1h" else tf
        if tf.endswith("h"):
            return "60m"
        return "60m"

    def _assess_quality(
        self,
        bundle: OHLCVBundle,
        spread_pct: float = 0.0,
        price_deviation_pct: float = 0.0,
        market_context_available: bool = False,
        market_context_source: str = "",
        market_context_error: str = "",
        confirmation: dict[str, Any] | None = None,
        missing_microstructure: list[str] | None = None,
        orderbook_bid_depth_usdt: float | None = None,
        orderbook_ask_depth_usdt: float | None = None,
        orderbook_top_bid_depth_usdt: float | None = None,
        orderbook_top_ask_depth_usdt: float | None = None,
    ) -> dict[str, Any]:
        reasons: list[str] = []
        now = utcnow()
        min_candles = 50
        configured_primary = (settings.scanner.timeframes or ["1h"])[0]
        primary_tf = (
            configured_primary
            if configured_primary in bundle.timeframes
            else next(iter(bundle.timeframes.keys()), configured_primary)
        )
        primary = (
            bundle.timeframes.get(primary_tf)
            or next(iter(bundle.timeframes.values()), [])
        )
        primary_indicator = (
            bundle.indicators.get(primary_tf)
            or next(iter(bundle.indicators.values()), {})
        )

        if len(primary) < min_candles:
            reasons.append("ohlcv_insufficient")
        if bundle.current_price <= 0:
            reasons.append("invalid_price")
        if primary:
            tf_seconds = timeframe_to_seconds(primary_tf)
            latest = primary[-1].timestamp
            age = max(0.0, time.mktime(now.timetuple()) - time.mktime(latest.timetuple()))
            if age > tf_seconds * 2.5:
                reasons.append("stale_candle")
        else:
            reasons.append("missing_primary_timeframe")
        if primary_indicator.get("atr_pct") is None:
            reasons.append("invalid_atr")
        volume_ratio = primary_indicator.get("volume_ratio")
        if volume_ratio is not None and volume_ratio < float(settings.scanner.min_volume_ratio):
            reasons.append("low_volume")
        gap_ratio = _candle_gap_ratio(primary, primary_tf) if primary else 0.0
        if bundle.mapping.data_source == "ccxt" and gap_ratio > float(settings.scanner.max_candle_gap_ratio):
            reasons.append("candle_gaps")
        if spread_pct > 0 and spread_pct > float(settings.scanner.max_spread_pct):
            reasons.append("wide_spread")
        if price_deviation_pct > float(settings.scanner.max_price_deviation_pct):
            reasons.append("price_source_deviation")
        if bundle.mapping.data_source == "ccxt" and not market_context_available:
            reasons.append("market_context_unavailable")
        if bundle.mapping.mapped_asset and bundle.mapping.exchange_symbol not in settings.scanner.live_symbol_whitelist:
            reasons.append("mapped_asset_live_disabled")
        if not bundle.mapping.tradable:
            reasons.append("not_tradable")
        if bundle.mapping.data_source_policy == "confirm" and not bool((confirmation or {}).get("passed")):
            reasons.append(str((confirmation or {}).get("reason") or "confirmation_unavailable"))

        return {
            "passed": len(reasons) == 0,
            "reasons": reasons,
            "source_mode": settings.scanner.source_mode,
            "data_source_policy": bundle.mapping.data_source_policy,
            "target_exchange": bundle.mapping.target_exchange or bundle.mapping.exchange_name,
            "target_market_type": bundle.mapping.target_market_type or bundle.mapping.market_type,
            "source_exchange": bundle.mapping.source_exchange,
            "source_market_type": bundle.mapping.source_market_type,
            "actual_data_source": bundle.mapping.actual_data_source or market_context_source,
            "tradable": bundle.mapping.tradable,
            "tradability_reason": bundle.mapping.tradability_reason,
            "universe_source": bundle.mapping.universe_source,
            "liquidity_tier": bundle.mapping.liquidity_tier,
            "quote_volume": bundle.mapping.quote_volume,
            "confirmation": confirmation or {},
            "spread_pct": spread_pct,
            "price_deviation_pct": price_deviation_pct,
            "volume_ratio": volume_ratio,
            "candle_gap_ratio": gap_ratio,
            "primary_timeframe": primary_tf,
            "primary_candles": len(primary),
            "mapped_asset": bundle.mapping.mapped_asset,
            "market_context_available": market_context_available if bundle.mapping.data_source == "ccxt" else None,
            "market_context_source": market_context_source,
            "market_context_error": market_context_error,
            "missing_microstructure": list(missing_microstructure or []),
            "orderbook_bid_depth_usdt": orderbook_bid_depth_usdt,
            "orderbook_ask_depth_usdt": orderbook_ask_depth_usdt,
            "orderbook_top_bid_depth_usdt": orderbook_top_bid_depth_usdt,
            "orderbook_top_ask_depth_usdt": orderbook_top_ask_depth_usdt,
        }


__all__ = [
    "NormalizedCandle",
    "SymbolMapping",
    "OHLCVBundle",
    "UnifiedOHLCVProvider",
    "timeframe_to_seconds",
]
