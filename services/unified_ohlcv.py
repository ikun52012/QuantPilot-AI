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
    _safe_fetch_open_interest,
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


class OHLCVBundle(BaseModel):
    mapping: SymbolMapping
    current_price: float = 0.0
    timeframes: dict[str, list[NormalizedCandle]] = Field(default_factory=dict)
    indicators: dict[str, dict[str, float | None]] = Field(default_factory=dict)
    data_quality: dict[str, Any] = Field(default_factory=dict)
    oi_change_pct: float | None = None
    oi_current: float | None = None

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


def _indicator_snapshot(candles: list[NormalizedCandle]) -> dict[str, float | None]:
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


class UnifiedOHLCVProvider:
    """Scanner-only OHLCV facade for crypto and commodity-like symbols."""

    def __init__(self) -> None:
        self._bundle_cache: dict[tuple[str, tuple[str, ...]], tuple[float, OHLCVBundle]] = {}

    def resolve_mapping(self, watch_symbol: str) -> SymbolMapping:
        watch = str(watch_symbol or "").upper().strip()
        raw_map = settings.scanner.symbol_map.get(watch) or settings.scanner.symbol_map.get(watch.replace("/", ""))
        if isinstance(raw_map, dict):
            exchange_symbol = str(raw_map.get("exchange_symbol") or watch).upper().strip()
            exchange_name = str(raw_map.get("exchange_name") or settings.exchange.name).lower().strip()
            market_type = str(
                raw_map.get("type") or raw_map.get("market_type")
                or settings.exchange.market_type
            ).lower().strip()
        else:
            exchange_symbol = watch
            exchange_name = settings.exchange.name
            market_type = settings.exchange.market_type

        commodity_type = is_special_commodity(watch)
        data_symbol = get_yfinance_symbol(watch) if commodity_type else watch
        return SymbolMapping(
            watch_symbol=watch,
            data_symbol=data_symbol,
            exchange_symbol=exchange_symbol,
            exchange_name=exchange_name,
            market_type=market_type,
            mapped_asset=exchange_symbol != watch,
            data_source="yfinance" if commodity_type else "ccxt",
        )

    async def get_bundle(self, watch_symbol: str, timeframes: list[str] | None = None) -> OHLCVBundle:
        mapping = self.resolve_mapping(watch_symbol)
        requested_tfs = timeframes or settings.scanner.timeframes
        cache_key = (mapping.watch_symbol, tuple(requested_tfs))
        ttl = max(0, int(settings.scanner.bundle_cache_ttl_secs))
        now_monotonic = time.monotonic()
        if ttl > 0:
            cached = self._bundle_cache.get(cache_key)
            if cached and now_monotonic - cached[0] <= ttl:
                return cached[1]
        timeframes_data: dict[str, list[NormalizedCandle]] = {}
        indicators: dict[str, dict[str, float | None]] = {}

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
        if mapping.data_source == "ccxt":
            try:
                ohlcv_price = current_price
                context = await fetch_market_context(mapping.exchange_symbol)
                context_price = float(context.current_price or current_price)
                if ohlcv_price > 0 and context_price > 0:
                    price_deviation_pct = abs(context_price - ohlcv_price) / ohlcv_price * 100.0
                current_price = context_price
                spread_pct = float(context.bid_ask_spread or 0.0)
                oi_change_pct = float(context.open_interest_change_pct) if context.open_interest_change_pct is not None else None
                oi_current = float(context.open_interest) if context.open_interest is not None else None
            except Exception as exc:
                logger.debug(f"[Scanner/OHLCV] market context unavailable for {mapping.exchange_symbol}: {exc}")

        bundle = OHLCVBundle(
            mapping=mapping,
            current_price=current_price,
            timeframes=timeframes_data,
            indicators=indicators,
            data_quality={},
            oi_change_pct=oi_change_pct,
            oi_current=oi_current,
        )
        bundle.data_quality = self._assess_quality(
            bundle,
            spread_pct=spread_pct,
            price_deviation_pct=price_deviation_pct,
        )
        if ttl > 0:
            self._bundle_cache[cache_key] = (now_monotonic, bundle)
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

    async def _fetch_candles(self, mapping: SymbolMapping, timeframe: str) -> list[NormalizedCandle]:
        if mapping.data_source == "yfinance":
            return await self._fetch_yfinance_candles(mapping.data_symbol, timeframe)
        rows = await fetch_ohlcv_history(
            mapping.exchange_symbol,
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
        if bundle.mapping.mapped_asset and bundle.mapping.exchange_symbol not in settings.scanner.live_symbol_whitelist:
            reasons.append("mapped_asset_live_disabled")

        return {
            "passed": not any(r not in {"mapped_asset_live_disabled"} for r in reasons),
            "reasons": reasons,
            "spread_pct": spread_pct,
            "price_deviation_pct": price_deviation_pct,
            "volume_ratio": volume_ratio,
            "candle_gap_ratio": gap_ratio,
            "primary_timeframe": primary_tf,
            "primary_candles": len(primary),
            "mapped_asset": bundle.mapping.mapped_asset,
        }


__all__ = [
    "NormalizedCandle",
    "SymbolMapping",
    "OHLCVBundle",
    "UnifiedOHLCVProvider",
    "timeframe_to_seconds",
]
