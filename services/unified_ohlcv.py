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
    }


class UnifiedOHLCVProvider:
    """Scanner-only OHLCV facade for crypto and commodity-like symbols."""

    def resolve_mapping(self, watch_symbol: str) -> SymbolMapping:
        watch = str(watch_symbol or "").upper().strip()
        raw_map = settings.scanner.symbol_map.get(watch) or settings.scanner.symbol_map.get(watch.replace("/", ""))
        if isinstance(raw_map, dict):
            exchange_symbol = str(raw_map.get("exchange_symbol") or watch).upper().strip()
            exchange_name = str(raw_map.get("exchange_name") or settings.exchange.name).lower().strip()
            market_type = str(raw_map.get("type") or raw_map.get("market_type") or settings.exchange.market_type).lower().strip()
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
        if mapping.data_source == "ccxt":
            try:
                context = await fetch_market_context(mapping.exchange_symbol)
                current_price = float(context.current_price or current_price)
                spread_pct = float(context.bid_ask_spread or 0.0)
            except Exception as exc:
                logger.debug(f"[Scanner/OHLCV] market context unavailable for {mapping.exchange_symbol}: {exc}")

        bundle = OHLCVBundle(
            mapping=mapping,
            current_price=current_price,
            timeframes=timeframes_data,
            indicators=indicators,
            data_quality={},
        )
        bundle.data_quality = self._assess_quality(bundle, spread_pct=spread_pct)
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
        rows = await fetch_ohlcv_history(mapping.exchange_symbol, timeframe=timeframe, days=self._days_for_timeframe(timeframe))
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

    def _assess_quality(self, bundle: OHLCVBundle, spread_pct: float = 0.0) -> dict[str, Any]:
        reasons: list[str] = []
        now = utcnow()
        min_candles = 50
        configured_primary = (settings.scanner.timeframes or ["1h"])[0]
        primary_tf = configured_primary if configured_primary in bundle.timeframes else next(iter(bundle.timeframes.keys()), configured_primary)
        primary = bundle.timeframes.get(primary_tf) or next(iter(bundle.timeframes.values()), [])
        primary_indicator = bundle.indicators.get(primary_tf) or next(iter(bundle.indicators.values()), {})

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
        _atr_pct = float(primary_indicator.get("atr_pct") or 0.0)
        if spread_pct > 0 and spread_pct > float(settings.scanner.max_spread_pct):
            reasons.append("wide_spread")
        if bundle.mapping.mapped_asset and bundle.mapping.exchange_symbol not in settings.scanner.live_symbol_whitelist:
            reasons.append("mapped_asset_live_disabled")

        return {
            "passed": not any(r not in {"mapped_asset_live_disabled"} for r in reasons),
            "reasons": reasons,
            "spread_pct": spread_pct,
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
