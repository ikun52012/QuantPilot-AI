"""
QuantPilot AI - Enhanced Market Data Fetcher
Fetches advanced market data from free public APIs:
- Macro events calendar (economic indicators)
- Liquidation heatmap
- Long/Short ratio
- CVD/Delta divergence
- Basis (spot vs futures price)
- Fear & Greed Index
- Volatility regime detection
"""
import asyncio
import os
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any, TypeVar, cast

import aiohttp
from loguru import logger

from core.utils.datetime import utcnow

_cache_ttl = 300
T = TypeVar("T")

_cache: dict[str, tuple[float, object]] = {}
_cache_lock = asyncio.Lock()
_cache_inflight: dict[str, asyncio.Task] = {}

MACRO_EVENTS_CACHE_KEY = "macro_events"
LIQUIDATION_CACHE_KEY = "liquidation_heatmap"
FEAR_GREED_CACHE_KEY = "fear_greed_index"


async def _fetch_with_cache(key: str, fetcher: Callable[[], Awaitable[T]], ttl: int = _cache_ttl) -> T:
    """Fetch data with cache and one in-flight request per cache key."""
    now = time.time()
    async with _cache_lock:
        if key in _cache:
            cached_time, cached_data = _cache[key]
            if now - cached_time < ttl:
                return cast(T, cached_data)
        task = _cache_inflight.get(key)
        if task is None:
            async def _fetch_and_store() -> T:
                try:
                    data = await fetcher()
                    async with _cache_lock:
                        if data is not None:
                            _cache[key] = (time.time(), data)
                    return data
                finally:
                    async with _cache_lock:
                        if _cache_inflight.get(key) is asyncio.current_task():
                            _cache_inflight.pop(key, None)

            task = asyncio.create_task(_fetch_and_store())
            _cache_inflight[key] = task

    return cast(T, await asyncio.shield(task))


def _base_asset(symbol: str) -> str:
    """Normalize TradingView/ccxt symbols to a base asset for public APIs."""
    value = str(symbol or "").upper().strip().replace(" ", "")
    if ":" in value:
        value = value.split(":", 1)[0]
    for suffix in (".P", "PERP"):
        if value.endswith(suffix):
            value = value[:-len(suffix)]
            break
    value = value.replace("/", "").replace("-", "").replace("_", "")
    for quote in ("USDT", "USDC", "BUSD", "USD"):
        if value.endswith(quote) and len(value) > len(quote):
            return value[:-len(quote)]
    return value


def _binance_usdt_symbol(symbol: str) -> str:
    base = _base_asset(symbol)
    return f"{base}USDT" if base else ""


def _okx_swap_inst_id(symbol: str) -> str:
    base = _base_asset(symbol)
    return f"{base}-USDT-SWAP" if base else ""


async def fetch_macro_events_calendar() -> dict[str, list[dict[str, Any]]]:
    """
    Fetch macro economic events calendar.
    Free sources:
    - FXStreet (partial free)
    - Investing.com (via scraper - limited)
    - FMP (Financial Modeling Prep - free tier)

    Returns dict with event categories and their schedules.
    """
    async def _fetch() -> dict[str, list[dict[str, Any]]]:
        events: dict[str, list[dict[str, Any]]] = {
            "high_impact": [],
            "medium_impact": [],
            "crypto_specific": [],
        }

        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as session:
                fmp_api_key = os.getenv("FMP_API_KEY", "")
                if fmp_api_key:
                    url = "https://financialmodelingprep.com/api/v3/economic_calendar"
                    headers = {"apikey": fmp_api_key}
                    async with session.get(url, headers=headers) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            now = utcnow()
                            for event in data:
                                event_time = datetime.fromisoformat(str(event.get("date") or "").replace("Z", "+00:00"))
                                impact = event.get("impact", "").lower()
                                if abs((event_time - now).total_seconds()) < 3600:
                                    if impact == "high":
                                        events["high_impact"].append({
                                            "event": event.get("event", ""),
                                            "date": event_time.isoformat(),
                                            "impact": impact,
                                            "country": event.get("country", ""),
                                        })
                                    elif impact == "medium":
                                        events["medium_impact"].append({
                                            "event": event.get("event", ""),
                                            "date": event_time.isoformat(),
                                            "impact": impact,
                                        })

                hardcoded_crypto_events = _get_hardcoded_crypto_events()
                events["crypto_specific"] = hardcoded_crypto_events

        except Exception as e:
            logger.warning(f"[EnhancedData] Failed to fetch macro events: {e}")
            events["crypto_specific"] = _get_hardcoded_crypto_events()

        return events

    return await _fetch_with_cache(MACRO_EVENTS_CACHE_KEY, _fetch, ttl=3600)


def _get_hardcoded_crypto_events() -> list[dict[str, Any]]:
    """Hardcoded major crypto events that we know about."""
    now = utcnow()
    events: list[dict[str, Any]] = []

    known_events = [
        {"name": "BTC Halving", "approximate_date": "2024-04-20", "impact": "high"},
        {"name": "ETH Upgrade", "approximate_date": "2024-03-13", "impact": "high"},
    ]

    for event in known_events:
        try:
            event_date = datetime.fromisoformat(event["approximate_date"]).replace(tzinfo=UTC)
            days_diff = abs((event_date - now).days)
            if days_diff <= 7:
                events.append({
                    "event": event["name"],
                    "date": event_date.isoformat(),
                    "impact": event["impact"],
                    "days_until": days_diff,
                })
        except (ValueError, TypeError, AttributeError):
            logger.debug("[EnhancedMarketData] Failed to parse macro event date")
        except Exception as e:
            logger.debug(f"[EnhancedMarketData] Unexpected error parsing macro event: {e}")

    return events


async def check_macro_event_risk() -> tuple[bool, str | None]:
    """
    Check if there's a high-impact macro event in the next 30 minutes.
    Returns (is_safe, reason_if_blocked)
    """
    events = await fetch_macro_events_calendar()
    now = utcnow()

    for event in events.get("high_impact", []):
        try:
            event_time = datetime.fromisoformat(event.get("date", "").replace("Z", "+00:00"))
            time_diff = (event_time - now).total_seconds()
            if -1800 <= time_diff <= 1800:
                return False, f"High-impact event '{event.get('event')}' at {event_time.strftime('%H:%M')} UTC"
        except (ValueError, TypeError, AttributeError):
            logger.debug("[EnhancedMarketData] Failed to parse event time for risk check")
        except Exception as e:
            logger.debug(f"[EnhancedMarketData] Unexpected error in macro risk check: {e}")

    for event in events.get("crypto_specific", []):
        days_until = event.get("days_until", 999)
        if days_until <= 1:
            return False, f"Major crypto event '{event.get('event')}' in {days_until} day(s)"

    return True, None


async def fetch_liquidation_heatmap(symbol: str) -> dict[str, Any]:
    """
    Fetch liquidation heatmap data.
    Free sources:
    - Binance public API (liquidation orders)
    - Coinglass (limited free tier)

    Returns levels where large liquidations exist.
    """
    async def _fetch() -> dict[str, Any]:
        base = _base_asset(symbol)
        binance_symbol = _binance_usdt_symbol(symbol)
        heatmap: dict[str, Any] = {
            "long_liquidations": [],
            "short_liquidations": [],
            "total_long_liq_usd": 0,
            "total_short_liq_usd": 0,
            "nearest_liq_level": None,
            "nearest_liq_distance_pct": None,
        }

        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as session:
                coinglass_url = f"https://open-api.coinglass.com/api/liquidation_heat_map?symbol={base}&interval=1h"
                api_key = os.getenv("COINGLASS_API_KEY", "")
                if api_key:
                    headers = {"coinglass-api-Key": api_key}
                    async with session.get(coinglass_url, headers=headers) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            if data.get("success"):
                                for level in data.get("data", []):
                                    price = level.get("price", 0)
                                    liq_usd = level.get("liquidationUsd", 0)
                                    side = level.get("side", "").lower()
                                    if side == "long":
                                        heatmap["long_liquidations"].append({"price": price, "usd": liq_usd})
                                        heatmap["total_long_liq_usd"] += liq_usd
                                    elif side == "short":
                                        heatmap["short_liquidations"].append({"price": price, "usd": liq_usd})
                                        heatmap["total_short_liq_usd"] += liq_usd

                binance_url = f"https://fapi.binance.com/fapi/v1/forceOrders?symbol={binance_symbol}&limit=100"
                try:
                    async with session.get(binance_url) as resp:
                        if resp.status == 200:
                            orders = await resp.json()
                            for order in orders:
                                price = float(order.get("price", 0))
                                qty = float(order.get("origQty", 0))
                                side = order.get("side", "").lower()
                                liq_usd = price * qty
                                if side == "sell":
                                    heatmap["long_liquidations"].append({"price": price, "usd": liq_usd})
                                elif side == "buy":
                                    heatmap["short_liquidations"].append({"price": price, "usd": liq_usd})
                except (TimeoutError, aiohttp.ClientError, OSError) as e:
                    logger.debug(f"[EnhancedMarketData] Liquidation API error for {symbol}: {e}")
                except Exception as e:
                    logger.debug(f"[EnhancedMarketData] Unexpected error fetching liquidation for {symbol}: {e}")

        except Exception as e:
            logger.warning(f"[EnhancedData] Failed to fetch liquidation heatmap for {symbol}: {e}")

        current_price_val = 0
        try:
            import market_data as _md
            ctx = getattr(_md, '_market_context_cache', {}).get(_binance_usdt_symbol(symbol))
            if ctx:
                current_price_val = float(ctx.get('current_price', 0) or 0)
        except Exception:
            pass

        if current_price_val <= 0:
            return heatmap

        nearest_long = None
        nearest_short = None
        nearest_long_dist = float('inf')
        nearest_short_dist = float('inf')
        for liq in heatmap["long_liquidations"]:
            p = float(liq.get("price", 0) or 0)
            if p > 0:
                d = abs(current_price_val - p) / current_price_val * 100
                if d < nearest_long_dist:
                    nearest_long_dist = d
                    nearest_long = p
        for liq in heatmap["short_liquidations"]:
            p = float(liq.get("price", 0) or 0)
            if p > 0:
                d = abs(current_price_val - p) / current_price_val * 100
                if d < nearest_short_dist:
                    nearest_short_dist = d
                    nearest_short = p

        if nearest_long is not None and nearest_long_dist <= nearest_short_dist:
            heatmap["nearest_liq_level"] = nearest_long
            heatmap["nearest_liq_distance_pct"] = round(nearest_long_dist, 4)
        elif nearest_short is not None:
            heatmap["nearest_liq_level"] = nearest_short
            heatmap["nearest_liq_distance_pct"] = round(nearest_short_dist, 4)

        return heatmap

    return await _fetch_with_cache(f"{LIQUIDATION_CACHE_KEY}:{symbol}", _fetch, ttl=60)


async def fetch_long_short_ratio(symbol: str) -> dict[str, Any]:
    """
    Fetch long/short ratio from multiple sources.
    Free sources:
    - Binance: fapi.binance.com/fapi/v1/globalLongShortAccountRatio
    - Coinglass: limited free tier

    Returns ratio data with history.
    """
    async def _fetch() -> dict[str, Any]:
        ratio_data: dict[str, Any] = {
            "current_ratio": None,
            "long_accounts_pct": None,
            "short_accounts_pct": None,
            "history_1h": [],
            "is_extreme_long": False,
            "is_extreme_short": False,
        }

        try:
            base = _base_asset(symbol)
            binance_symbol = _binance_usdt_symbol(symbol)

            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as session:
                binance_url = f"https://fapi.binance.com/fapi/v1/globalLongShortAccountRatio?symbol={binance_symbol}&period=1h&limit=24"
                async with session.get(binance_url) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        if data and len(data) > 0:
                            latest = data[-1]
                            ratio = float(latest.get("longShortRatio", 1.0))
                            ratio_data["current_ratio"] = ratio
                            ratio_data["long_accounts_pct"] = ratio / (ratio + 1) * 100
                            ratio_data["short_accounts_pct"] = 1 / (ratio + 1) * 100
                            ratio_data["history_1h"] = [float(d.get("longShortRatio", 1.0)) for d in data[-12:]]
                            ratio_data["is_extreme_long"] = ratio > 2.5
                            ratio_data["is_extreme_short"] = ratio < 0.4

                coinglass_url = f"https://open-api.coinglass.com/api/long_short_ratio?symbol={base}&interval=1h"
                api_key = os.getenv("COINGLASS_API_KEY", "")
                if api_key:
                    headers = {"coinglass-api-Key": api_key}
                    async with session.get(coinglass_url, headers=headers) as resp:
                        if resp.status == 200:
                            cg_data = await resp.json()
                            if cg_data.get("success") and cg_data.get("data"):
                                ratio_data["coinglass_ratio"] = cg_data["data"][0].get("ratio")

        except Exception as e:
            logger.warning(f"[EnhancedData] Failed to fetch long/short ratio for {symbol}: {e}")

        return ratio_data

    return await _fetch_with_cache(f"long_short_ratio:{symbol}", _fetch, ttl=60)


async def fetch_basis_data(symbol: str) -> dict[str, Any]:
    """
    Fetch basis (spot vs futures price difference).
    Free sources:
    - Binance spot + futures public APIs

    Returns basis percentage and historical trend.
    """
    async def _fetch() -> dict[str, Any]:
        basis_data: dict[str, Any] = {
            "basis_pct": None,
            "spot_price": None,
            "futures_price": None,
            "is_high_positive": False,
            "is_high_negative": False,
        }

        try:
            spot_symbol = _binance_usdt_symbol(symbol)
            futures_symbol = spot_symbol

            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as session:
                spot_url = f"https://api.binance.com/api/v3/ticker/price?symbol={spot_symbol}"
                futures_url = f"https://fapi.binance.com/fapi/v1/ticker/price?symbol={futures_symbol}"

                spot_resp, futures_resp = await asyncio.gather(
                    session.get(spot_url),
                    session.get(futures_url),
                )

                if spot_resp.status == 200 and futures_resp.status == 200:
                    spot_data = await spot_resp.json()
                    futures_data = await futures_resp.json()

                    spot_price = float(spot_data.get("price", 0))
                    futures_price = float(futures_data.get("price", 0))

                    if spot_price > 0:
                        basis_pct = (futures_price - spot_price) / spot_price * 100
                        basis_data["basis_pct"] = basis_pct
                        basis_data["spot_price"] = spot_price
                        basis_data["futures_price"] = futures_price
                        basis_data["is_high_positive"] = basis_pct > 0.5
                        basis_data["is_high_negative"] = basis_pct < -0.5

        except Exception as e:
            logger.warning(f"[EnhancedData] Failed to fetch basis for {symbol}: {e}")

        return basis_data

    return await _fetch_with_cache(f"basis:{symbol}", _fetch, ttl=30)


async def fetch_fear_greed_index() -> dict[str, Any]:
    """
    Fetch Crypto Fear & Greed Index.
    Free source: alternative.me API

    Returns current index value and classification.
    """
    async def _fetch() -> dict[str, Any]:
        fg_data: dict[str, Any] = {
            "value": None,
            "classification": None,
            "is_extreme_fear": False,
            "is_extreme_greed": False,
        }

        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as session:
                url = "https://api.alternative.me/fng/?limit=1"
                async with session.get(url) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        if data.get("data") and len(data["data"]) > 0:
                            latest = data["data"][0]
                            value = int(latest.get("value", 50))
                            classification = latest.get("value_classification", "Neutral")
                            fg_data["value"] = value
                            fg_data["classification"] = classification
                            fg_data["is_extreme_fear"] = value <= 20
                            fg_data["is_extreme_greed"] = value >= 80

        except Exception as e:
            logger.warning(f"[EnhancedData] Failed to fetch Fear & Greed Index: {e}")

        return fg_data

    return await _fetch_with_cache(FEAR_GREED_CACHE_KEY, _fetch, ttl=3600)


async def calculate_directional_volume_delta(ohlcv_data: list[list[float]], lookback: int = 20) -> dict[str, Any]:
    """
    Calculate Directional Volume Delta (DVD) - estimated from OHLCV data.

    NOTE: This is a proxy estimation, NOT true Cumulative Volume Delta (CVD).
    True CVD requires order-flow data (active/passive trade flags) which is
    not available from standard OHLCV candles. This implementation correlates
    price direction with volume as an approximation.

    Uses local OHLCV data - no external API needed.

    Returns divergence status and strength.
    """
    if len(ohlcv_data) < lookback:
        return {"divergence": None, "strength": 0, "type": None}

    closes = [c[4] for c in ohlcv_data[-lookback:]]
    volumes = [c[5] for c in ohlcv_data[-lookback:]]

    price_change = (closes[-1] - closes[0]) / closes[0] * 100 if closes[0] > 0 else 0

    cvd = 0.0
    for i in range(1, len(closes)):
        if closes[i] > closes[i-1]:
            cvd += volumes[i]
        elif closes[i] < closes[i-1]:
            cvd -= volumes[i]

    cvd_change_pct = cvd / sum(volumes) * 100 if sum(volumes) > 0 else 0

    divergence_data: dict[str, Any] = {
        "price_change_pct": price_change,
        "cvd_change_pct": cvd_change_pct,
        "divergence": None,
        "strength": 0,
        "type": None,
    }

    if price_change > 2 and cvd_change_pct < -10:
        divergence_data["divergence"] = True
        divergence_data["strength"] = abs(cvd_change_pct)
        divergence_data["type"] = "bearish"
    elif price_change < -2 and cvd_change_pct > 10:
        divergence_data["divergence"] = True
        divergence_data["strength"] = abs(cvd_change_pct)
        divergence_data["type"] = "bullish"

    return divergence_data


async def calculate_cvd_divergence(ohlcv_data: list[list[float]], lookback: int = 20) -> dict[str, Any]:
    """
    Backward-compatible alias for calculate_directional_volume_delta.
    DEPRECATED: Use calculate_directional_volume_delta instead.
    """
    return await calculate_directional_volume_delta(ohlcv_data, lookback)


async def detect_volatility_regime(ohlcv_data: list[list[float]], lookback: int = 100, thresholds: Any | None = None) -> dict[str, Any]:
    """
    Detect current volatility regime.
    Uses local OHLCV data - no external API needed.

    Args:
        ohlcv_data: OHLCV candles [timestamp, open, high, low, close, volume]
        lookback: Number of candles to analyze
        thresholds: Optional FilterThresholds instance for configurable volatility regime multipliers

    Returns regime classification and position sizing suggestion.
    """
    if len(ohlcv_data) < lookback:
        return {"regime": "unknown", "atr_pct": None, "suggestion": None}

    period = 14
    if len(ohlcv_data) < period + 1:
        return {"regime": "unknown", "atr_pct": None, "suggestion": None}

    true_ranges: list[float] = []
    for i in range(1, len(ohlcv_data)):
        high = float(ohlcv_data[i][2])
        low = float(ohlcv_data[i][3])
        prev_close = float(ohlcv_data[i - 1][4])
        true_ranges.append(max(high - low, abs(high - prev_close), abs(low - prev_close)))

    recent_atr: list[float] = []
    start_idx = max(period, len(ohlcv_data) - lookback)
    for end_idx in range(start_idx, len(ohlcv_data)):
        tr_window = true_ranges[end_idx - period:end_idx]
        close = float(ohlcv_data[end_idx][4])
        if len(tr_window) == period and close > 0:
            atr = sum(tr_window) / period
            recent_atr.append(atr / close * 100)

    if not recent_atr:
        return {"regime": "unknown", "atr_pct": None, "suggestion": None}

    current_atr_pct = recent_atr[-1] if recent_atr else 0
    avg_atr_pct = sum(recent_atr) / len(recent_atr)

    regime_data: dict[str, Any] = {
        "current_atr_pct": current_atr_pct,
        "avg_atr_pct": avg_atr_pct,
        "regime": "normal",
        "suggestion": "normal_position",
    }

    # FIX #10: Configurable regime thresholds via FilterThresholds
    extreme_mult = 2.0
    high_mult = 1.5
    if thresholds is not None:
        try:
            extreme_mult = float(thresholds.get("volatility_regime_extreme_multiplier", 2.0))
        except (TypeError, ValueError, AttributeError):
            pass
        try:
            high_mult = float(thresholds.get("volatility_regime_multiplier", 1.5))
        except (TypeError, ValueError, AttributeError):
            pass

    if current_atr_pct < avg_atr_pct * 0.5:
        regime_data["regime"] = "low_volatility"
        regime_data["suggestion"] = "breakout_approach"
    elif current_atr_pct > avg_atr_pct * extreme_mult:
        regime_data["regime"] = "extreme_volatility"
        regime_data["suggestion"] = "pause_trading"
    elif current_atr_pct > avg_atr_pct * high_mult:
        regime_data["regime"] = "high_volatility"
        regime_data["suggestion"] = "reduce_position"

    return regime_data


async def fetch_orderbook_data(symbol: str, exchange: str = "binance") -> dict[str, Any]:
    """
    Fetch order book data for liquidity analysis.

    Free sources:
    - Binance public API
    - OKX public API

    Returns order book with bids and asks.
    """
    orderbook_data: dict[str, Any] = {
        "bids": [],
        "asks": [],
        "timestamp": None,
        "spread_pct": 0.0,
    }

    binance_symbol = _binance_usdt_symbol(symbol)
    okx_inst_id = _okx_swap_inst_id(symbol)

    async def _fetch() -> dict[str, Any]:
        urls = {
            "binance": f"https://fapi.binance.com/fapi/v1/depth?symbol={binance_symbol}&limit=100",
            "okx": f"https://www.okx.com/api/v5/market/books?instId={okx_inst_id}",
        }

        for ex_name, url in urls.items():
            if ex_name != exchange:
                continue
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                        if resp.status == 200:
                            data = await resp.json()

                            if ex_name == "binance":
                                bids = data.get("bids", [])
                                asks = data.get("asks", [])
                                orderbook_data["bids"] = [
                                    {"price": float(b[0]), "amount": float(b[1])}
                                    for b in bids[:50]
                                ]
                                orderbook_data["asks"] = [
                                    {"price": float(a[0]), "amount": float(a[1])}
                                    for a in asks[:50]
                                ]
                                orderbook_data["timestamp"] = data.get("E")

                                if bids and asks:
                                    best_bid = float(bids[0][0])
                                    best_ask = float(asks[0][0])
                                    mid = (best_bid + best_ask) / 2
                                    if mid > 0:
                                        orderbook_data["spread_pct"] = (best_ask - best_bid) / mid * 100

                                return orderbook_data

                            elif ex_name == "okx":
                                books = data.get("data", [])
                                if books:
                                    book = books[0]
                                    bids = book.get("bids", [])
                                    asks = book.get("asks", [])
                                    orderbook_data["bids"] = [
                                        {"price": float(b[0]), "amount": float(b[4])}
                                        for b in bids[:50]
                                    ]
                                    orderbook_data["asks"] = [
                                        {"price": float(a[0]), "amount": float(a[4])}
                                        for a in asks[:50]
                                    ]
                                    orderbook_data["timestamp"] = book.get("ts")
                                    return orderbook_data

            except Exception as e:
                logger.warning(f"[EnhancedData] Failed to fetch orderbook from {ex_name}: {e}")

        return orderbook_data

    cache_key = f"orderbook_{symbol}"
    return await _fetch_with_cache(cache_key, _fetch, ttl=5)


async def fetch_recent_trades(symbol: str, exchange: str = "binance", limit: int = 100) -> list[dict[str, Any]]:
    """
    Fetch recent trades for sweep detection.

    Free sources:
    - Binance public API
    - OKX public API

    Returns list of recent trades.
    """
    binance_symbol = _binance_usdt_symbol(symbol)
    okx_inst_id = _okx_swap_inst_id(symbol)

    async def _fetch() -> list[dict[str, Any]]:
        trades: list[dict[str, Any]] = []
        urls = {
            "binance": f"https://fapi.binance.com/fapi/v1/trades?symbol={binance_symbol}&limit={limit}",
            "okx": f"https://www.okx.com/api/v5/market/trades?instId={okx_inst_id}&limit={limit}",
        }

        for ex_name, url in urls.items():
            if ex_name != exchange:
                continue
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                        if resp.status == 200:
                            data = await resp.json()

                            if ex_name == "binance":
                                trades = [
                                    {
                                        "price": float(t.get("p", 0)),
                                        "amount": float(t.get("q", 0)),
                                        "timestamp": float(t.get("T", 0)),
                                        "side": "buy" if t.get("m") is False else "sell",
                                    }
                                    for t in data
                                ]
                                return trades

                            elif ex_name == "okx":
                                trade_data = data.get("data", [])
                                trades = [
                                    {
                                        "price": float(t.get("px", 0)),
                                        "amount": float(t.get("sz", 0)),
                                        "timestamp": float(t.get("ts", 0)),
                                        "side": t.get("side", "buy"),
                                    }
                                    for t in trade_data
                                ]
                                return trades

            except Exception as e:
                logger.warning(f"[EnhancedData] Failed to fetch trades from {ex_name}: {e}")

        return trades

    cache_key = f"trades_{symbol}"
    return await _fetch_with_cache(cache_key, _fetch, ttl=5)


async def analyze_liquidity_structure(
    symbol: str,
    current_price: float,
    ohlcv_data: list[list[float]] | None = None,
) -> dict[str, Any]:
    """
    Perform complete liquidity analysis for a symbol.

    Combines:
    - Order book depth analysis
    - Recent trades for sweep detection
    - OHLCV for support/resistance levels

    Returns liquidity analysis data.
    """
    from liquidity_analyzer import analyze_liquidity, format_liquidity_for_ai

    orderbook = await fetch_orderbook_data(symbol)
    recent_trades = await fetch_recent_trades(symbol)

    analysis = analyze_liquidity(
        ticker=symbol,
        current_price=current_price,
        orderbook=orderbook,
        recent_trades=recent_trades,
        ohlcv=cast(Any, ohlcv_data),
    )

    return {
        "analysis": analysis,
        "formatted_text": format_liquidity_for_ai(analysis, "long", current_price),
        "orderbook": orderbook,
        "has_liquidity_data": bool(orderbook.get("bids") or orderbook.get("asks")),
    }


# 鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺?# VWAP Deviation (P0 鈥?institutional must-have)
# 鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺?
async def calculate_vwap_deviation(
    ohlcv_data: list[list[float]],
    current_price: float,
    lookback: int = 24,
) -> dict[str, Any]:
    """
    Calculate VWAP (Volume-Weighted Average Price) deviation.

    Uses (High + Low + Close) / 3 as typical price weighted by volume.
    VWAP is the institutional benchmark 鈥?price above VWAP on longs is favorable.

    Returns deviation percentage and direction.
    """
    if len(ohlcv_data) < lookback or current_price <= 0:
        return {"vwap": None, "deviation_pct": None, "direction": None, "note": "Insufficient data"}

    recent = ohlcv_data[-lookback:]
    cumulative_tpv = 0.0
    cumulative_vol = 0.0

    for candle in recent:
        high = float(candle[2]) if len(candle) > 2 else 0
        low = float(candle[3]) if len(candle) > 3 else 0
        close = float(candle[4]) if len(candle) > 4 else 0
        volume = float(candle[5]) if len(candle) > 5 else 0
        typical_price = (high + low + close) / 3.0
        cumulative_tpv += typical_price * volume
        cumulative_vol += volume

    if cumulative_vol <= 0:
        return {"vwap": None, "deviation_pct": None, "direction": None, "note": "Zero volume"}

    vwap = cumulative_tpv / cumulative_vol
    deviation_pct = (current_price - vwap) / vwap * 100

    return {
        "vwap": round(vwap, 8),
        "deviation_pct": round(deviation_pct, 4),
        "direction": "above_vwap" if deviation_pct > 0 else "below_vwap",
        "lookback_candles": lookback,
    }


async def estimate_orderbook_slippage(symbol: str, order_size_usdt: float, side: str = "buy") -> dict[str, Any]:
    """Estimate slippage for a given order size using orderbook depth."""
    ob_data = await fetch_orderbook_data(symbol)
    bids = ob_data.get("bids", [])
    asks = ob_data.get("asks", [])
    book = asks if side == "buy" else bids
    if not book or order_size_usdt <= 0:
        return {"slippage_bps": 0.0, "avg_fill_price": 0.0, "fillable_usdt": 0.0, "fillable_pct": 0.0}

    remaining = order_size_usdt
    total_cost = 0.0
    total_qty = 0.0
    for level in book:
        price = float(level.get("price", 0) or level.get(0, 0))
        size = float(level.get("quantity", 0) or level.get("size", 0) or level.get(1, 0))
        if price <= 0 or size <= 0:
            continue
        level_usdt = price * size
        fill = min(remaining, level_usdt)
        qty = fill / price
        total_cost += fill
        total_qty += qty
        remaining -= fill
        if remaining <= 0:
            break

    if total_qty <= 0:
        return {"slippage_bps": 0.0, "avg_fill_price": 0.0, "fillable_usdt": 0.0, "fillable_pct": 0.0}

    avg_price = total_cost / total_qty
    best_price = float(book[0].get("price", 0) or book[0].get(0, 0)) if book else 0
    if best_price <= 0:
        return {"slippage_bps": 0.0, "avg_fill_price": avg_price, "fillable_usdt": total_cost, "fillable_pct": 0.0}

    slippage_bps = abs(avg_price - best_price) / best_price * 10000
    fillable_pct = total_cost / order_size_usdt * 100
    return {
        "slippage_bps": round(slippage_bps, 2),
        "avg_fill_price": avg_price,
        "best_price": best_price,
        "fillable_usdt": round(total_cost, 2),
        "fillable_pct": round(min(fillable_pct, 100.0), 2),
        "order_size_usdt": order_size_usdt,
        "side": side,
    }


async def calculate_volume_zscore(ohlcv_data: list[list[float]], lookback: int = 20) -> dict[str, Any]:
    """Calculate volume z-score excluding current candle."""
    if len(ohlcv_data) < lookback + 1:
        return {"rvol": None, "volume_zscore": None, "note": "Insufficient data"}

    volumes = [float(row[5]) for row in ohlcv_data if len(row) > 5 and float(row[5]) > 0]
    if len(volumes) < lookback + 1:
        return {"rvol": None, "volume_zscore": None, "note": "Insufficient volume data"}

    current_vol = volumes[-1]
    hist_vols = volumes[-(lookback + 1):-1]
    avg_vol = sum(hist_vols) / len(hist_vols)
    if avg_vol <= 0:
        return {"rvol": None, "volume_zscore": None, "note": "Zero average volume"}

    std_vol = (sum((v - avg_vol) ** 2 for v in hist_vols) / len(hist_vols)) ** 0.5
    zscore = (current_vol - avg_vol) / std_vol if std_vol > 0 else 0.0
    rvol = current_vol / avg_vol

    return {
        "rvol": round(rvol, 3),
        "volume_zscore": round(zscore, 3),
        "current_volume": current_vol,
        "avg_volume": round(avg_vol, 2),
        "lookback": lookback,
    }


async def calculate_atr_percentile(ohlcv_data: list[list[float]], period: int = 14, lookback: int = 90) -> dict[str, Any]:
    """Calculate ATR as percentile of recent history."""
    if len(ohlcv_data) < period + lookback:
        return {"atr_pct": None, "atr_percentile": None, "note": "Insufficient data"}

    atrs: list[float] = []
    for i in range(period, len(ohlcv_data)):
        high = float(ohlcv_data[i][2])
        low = float(ohlcv_data[i][3])
        prev_close = float(ohlcv_data[i - 1][4])
        tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
        atrs.append(tr)

    if len(atrs) < lookback:
        lookback = len(atrs)

    recent_atrs = atrs[-lookback:]
    current_atr = recent_atrs[-1]
    sorted_atrs = sorted(recent_atrs)
    rank = sum(1 for a in sorted_atrs if a <= current_atr)
    percentile = rank / len(sorted_atrs) * 100

    last_close = float(ohlcv_data[-1][4])
    atr_pct = (current_atr / last_close * 100) if last_close > 0 else 0

    return {
        "atr_pct": round(atr_pct, 4),
        "atr_percentile": round(percentile, 1),
        "current_atr": round(current_atr, 6),
        "lookback_days": lookback,
    }


# 鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺?# OI-Price Divergence (P0 鈥?detects smart money distribution)
# 鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺?
async def check_oi_price_divergence(
    oi_change_pct: float | None,
    price_change_1h: float,
    price_change_4h: float = 0.0,
    oi_change_threshold: float = 5.0,
    price_stall_threshold: float = 1.0,
) -> dict[str, Any]:
    """
    Check for OI-Price divergence 鈥?a leading indicator of trend reversals.

    Patterns:
    - OI up + price flat/down 鈫?bearish divergence (distribution)
    - OI up + price up 鈫?healthy uptrend (accumulation)
    - OI down + price flat/up 鈫?bullish divergence (short covering)
    - OI down + price down 鈫?healthy downtrend (liquidation cascade)

    Returns divergence type and strength.
    """
    result: dict[str, Any] = {
        "divergence_type": None,
        "strength": 0.0,
        "is_bearish": False,
        "is_bullish": False,
        "note": None,
    }

    if oi_change_pct is None:
        result["note"] = "No OI data"
        return result

    abs_oi = abs(oi_change_pct)

    if oi_change_pct > oi_change_threshold and price_change_1h < -price_stall_threshold:
        result["divergence_type"] = "bearish_confirmed"
        result["strength"] = round(abs_oi + abs(price_change_1h), 2)
        result["is_bearish"] = True
        result["note"] = f"OI +{oi_change_pct:.1f}% with price {price_change_1h:.1f}% — aggressive distribution"
    elif oi_change_pct > oi_change_threshold and price_change_1h < price_stall_threshold:
        result["divergence_type"] = "bearish_divergence"
        result["strength"] = round(abs_oi, 2)
        result["is_bearish"] = True
        result["note"] = f"OI +{oi_change_pct:.1f}% while price only +{price_change_1h:.1f}% — distribution likely"
    elif oi_change_pct < -oi_change_threshold and price_change_1h > price_stall_threshold:
        result["divergence_type"] = "bullish_confirmed"
        result["strength"] = round(abs_oi + abs(price_change_1h), 2)
        result["is_bullish"] = True
        result["note"] = f"OI {oi_change_pct:.1f}% with price +{price_change_1h:.1f}% — aggressive short squeeze"
    elif oi_change_pct < -oi_change_threshold and price_change_1h > -price_stall_threshold:
        result["divergence_type"] = "bullish_divergence"
        result["strength"] = round(abs_oi, 2)
        result["is_bullish"] = True
        result["note"] = f"OI {oi_change_pct:.1f}% while price holds — short covering likely"

    return result


# 鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺?# Exchange Reserve Flow (P1 鈥?whale movement detection)
# 鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺?
async def fetch_exchange_reserves(base_asset: str = "BTC") -> dict[str, Any]:
    """
    Fetch exchange reserve flow data.

    Free source: CryptoQuant community API / Glassnode alternatives.
    Net outflow from exchanges = accumulation (bullish).
    Net inflow to exchanges = potential selling pressure (bearish).

    Falls back gracefully if API is unavailable.
    """
    async def _fetch() -> dict[str, Any]:
        reserve_data: dict[str, Any] = {
            "net_flow_24h": None,
            "total_reserves": None,
            "flow_direction": None,
            "is_accumulation": False,
            "is_distribution": False,
            "source": "unavailable",
        }

        base = str(base_asset or "BTC").upper().strip()

        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as session:
                # CryptoQuant-style free API (may require key in production)
                cq_url = f"https://api.cryptoquant.com/v1/{base.lower()}/exchange-reserves"
                cq_key = os.getenv("CRYPTOQUANT_API_KEY", "")
                if cq_key:
                    async with session.get(cq_url, headers={"Authorization": f"Bearer {cq_key}"}) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            reserve_data.update({
                                "net_flow_24h": data.get("net_flow_24h"),
                                "total_reserves": data.get("total_reserves"),
                                "source": "cryptoquant",
                            })

                # Fallback: Glassnode free tier
                if reserve_data["net_flow_24h"] is None:
                    gn_key = os.getenv("GLASSNODE_API_KEY", "")
                    if gn_key:
                        gn_url = "https://api.glassnode.com/v1/metrics/transactions/transfers_volume_to_exchanges_sum"
                        async with session.get(gn_url, headers={"X-API-KEY": gn_key}) as resp:
                            if resp.status == 200:
                                data = await resp.json()
                                reserve_data["net_flow_24h"] = data[-1].get("v") if data else None
                                reserve_data["source"] = "glassnode"

        except (TimeoutError, aiohttp.ClientError, OSError) as e:
            logger.debug(f"[EnhancedData] Exchange reserves fetch failed for {base}: {e}")
        except Exception as e:
            logger.debug(f"[EnhancedData] Unexpected error in exchange reserves for {base}: {e}")

        # Classify flow direction
        net_flow = reserve_data.get("net_flow_24h")
        if net_flow is not None:
            try:
                nf = float(net_flow)
                if nf < -100:
                    reserve_data["flow_direction"] = "outflow"
                    reserve_data["is_accumulation"] = True
                elif nf > 100:
                    reserve_data["flow_direction"] = "inflow"
                    reserve_data["is_distribution"] = True
                else:
                    reserve_data["flow_direction"] = "neutral"
            except (TypeError, ValueError):
                pass

        return reserve_data

    return await _fetch_with_cache(f"exchange_reserves:{base_asset}", _fetch, ttl=300)


# 鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺?# Funding Rate Term Structure (P1 鈥?sentiment curve)
# 鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺?
async def calculate_funding_term_structure(
    symbol: str,
    current_funding_rate: float | None,
) -> dict[str, Any]:
    """
    Analyze funding rate term structure 鈥?how funding has evolved over time.

    Steepening funding (rising faster) = growing extreme sentiment = reversal risk.
    Flattening funding (returning to normal) = sentiment normalizing.

    Uses Binance funding rate history endpoint.
    """
    async def _fetch() -> dict[str, Any]:
        result: dict[str, Any] = {
            "current_funding": round(float(current_funding_rate or 0), 6),
            "funding_8h_ago": None,
            "funding_24h_ago": None,
            "trend": "stable",
            "is_steepening": False,
            "is_flattening": False,
            "note": None,
        }

        if current_funding_rate is None:
            result["note"] = "No current funding rate"
            return result

        # _binance_usdt_symbol is defined at module level, no import needed
        binance_symbol = _binance_usdt_symbol(symbol)
        if not binance_symbol:
            result["note"] = "Could not resolve symbol"
            return result

        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as session:
                url = f"https://fapi.binance.com/fapi/v1/fundingRate?symbol={binance_symbol}&limit=24"
                async with session.get(url) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        if data and len(data) >= 3:
                            # Most funding intervals are 8h, so 3 entries = ~24h
                            rates = []
                            for entry in data:
                                try:
                                    ft = datetime.fromtimestamp(
                                        float(entry.get("fundingTime", 0)) / 1000,
                                        tz=UTC,
                                    )
                                    fr = float(entry.get("fundingRate", 0))
                                    rates.append((ft, fr))
                                except (ValueError, TypeError, AttributeError):
                                    continue

                            rates.sort(key=lambda x: x[0])

                            if len(rates) >= 2:
                                result["funding_8h_ago"] = rates[-2][1]
                            most_recent_time = rates[-1][0]
                            target_24h = most_recent_time - timedelta(hours=24)
                            target_8h_ago = most_recent_time - timedelta(hours=8)
                            best_24h = None
                            best_8h = None
                            for ft, fr in rates:
                                if best_24h is None or abs((ft - target_24h).total_seconds()) < abs((best_24h[0] - target_24h).total_seconds()):
                                    best_24h = (ft, fr)
                                if best_8h is None or abs((ft - target_8h_ago).total_seconds()) < abs((best_8h[0] - target_8h_ago).total_seconds()):
                                    if abs((ft - target_8h_ago).total_seconds()) < 14400:
                                        best_8h = (ft, fr)
                            if best_24h:
                                result["funding_24h_ago"] = best_24h[1]
                            if best_8h and "funding_8h_ago" not in result:
                                result["funding_8h_ago"] = best_8h[1]

                            # Determine term structure trend
                            curr = result["current_funding"]
                            prev8 = result.get("funding_8h_ago")
                            prev24 = result.get("funding_24h_ago")

                            if prev8 is not None and prev24 is not None:
                                recent_slope = curr - prev8
                                older_slope = prev8 - prev24

                                if abs(recent_slope) > abs(older_slope) * 2.0:
                                    result["trend"] = "steepening"
                                    result["is_steepening"] = True
                                    result["note"] = (
                                        f"Funding {curr*100:.4f}%, steepening from "
                                        f"{prev8*100:.4f}% (8h) and {prev24*100:.4f}% (24h)"
                                    )
                                elif abs(recent_slope) < abs(older_slope) * 0.5:
                                    result["trend"] = "flattening"
                                    result["is_flattening"] = True
                                    result["note"] = "Funding rate normalizing 鈥?sentiment cooling"
                                else:
                                    result["trend"] = "stable"

        except (TimeoutError, aiohttp.ClientError, OSError) as e:
            logger.debug(f"[EnhancedData] Funding term structure fetch failed: {e}")
            result["note"] = f"Fetch failed: {e}"
        except Exception as e:
            logger.debug(f"[EnhancedData] Unexpected error in funding term structure: {e}")
            result["note"] = f"Error: {e}"

        return result

    return await _fetch_with_cache(f"funding_term:{symbol}", _fetch, ttl=300)


# 鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺?# Multi-Exchange Price Discrepancy (P2 鈥?liquidity fragmentation)
# 鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺?
async def check_exchange_price_discrepancy(
    symbol: str,
    reference_price: float,
) -> dict[str, Any]:
    """
    Check if the same asset trades at significantly different prices across exchanges.

    Large discrepancies indicate:
    - Liquidity fragmentation
    - Arbitrage activity
    - Exchange-specific issues
    - Extreme market conditions

    Returns discrepancy data across exchanges.
    """
    async def _fetch() -> dict[str, Any]:
        result: dict[str, Any] = {
            "max_discrepancy_pct": 0.0,
            "exchanges_checked": [],
            "is_concerning": False,
            "prices": {},
            "note": None,
        }

        if reference_price <= 0:
            result["note"] = "Invalid reference price"
            return result

        base = _base_asset(symbol)
        exchanges_to_check = [
            ("Binance", f"https://api.binance.com/api/v3/ticker/price?symbol={base}USDT"),
            ("OKX", f"https://www.okx.com/api/v5/market/ticker?instId={base}-USDT"),
            ("Bybit", f"https://api.bybit.com/v5/market/tickers?category=spot&symbol={base}USDT"),
            ("Gate.io", f"https://api.gateio.ws/api/v4/spot/tickers?currency_pair={base}_USDT"),
        ]

        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as session:
                async def _fetch_exchange(name: str, url: str) -> tuple[str, float | None]:
                    try:
                        async with session.get(url) as resp:
                            if resp.status != 200:
                                return name, None
                            data = await resp.json()

                            if name == "Binance":
                                return name, float(data.get("price", 0))
                            elif name == "OKX":
                                tickers = data.get("data", [])
                                return name, float(tickers[0].get("last", 0)) if tickers else None
                            elif name == "Bybit":
                                tickers = data.get("result", {}).get("list", [])
                                return name, float(tickers[0].get("lastPrice", 0)) if tickers else None
                            elif name == "Gate.io":
                                ticker = data[0] if isinstance(data, list) and data else data
                                return name, float(ticker.get("last", 0))
                            return name, None
                    except Exception:
                        return name, None

                tasks = [_fetch_exchange(name, url) for name, url in exchanges_to_check]
                results_list = await asyncio.gather(*tasks, return_exceptions=True)

                prices = {}
                for item in results_list:
                    if isinstance(item, tuple) and item[1] is not None:
                        name, price = item
                        if price > 0:
                            prices[name] = price
                            result["exchanges_checked"].append(name)

                result["prices"] = prices

                if len(prices) >= 2:
                    prices_list = list(prices.values())
                    max_price = max(prices_list)
                    min_price = min(prices_list)
                    if min_price > 0:
                        result["max_discrepancy_pct"] = round((max_price - min_price) / min_price * 100, 4)

                    if result["max_discrepancy_pct"] > 2.0:
                        result["is_concerning"] = True
                        result["note"] = (
                            f"Price discrepancy {result['max_discrepancy_pct']:.2f}% across {len(prices)} exchanges"
                        )

        except (TimeoutError, aiohttp.ClientError, OSError) as e:
            logger.debug(f"[EnhancedData] Exchange price check failed: {e}")
            result["note"] = f"Fetch failed: {e}"
        except Exception as e:
            logger.debug(f"[EnhancedData] Unexpected error in price discrepancy: {e}")
            result["note"] = f"Error: {e}"

        return result

    return await _fetch_with_cache(f"price_discrepancy:{symbol}", _fetch, ttl=60)


# ─────────────────────────────────────────────
# Ichimoku Cloud
# ─────────────────────────────────────────────
def calculate_ichimoku(
    ohlcv_data: list[list[float]],
    tenkan_period: int = 9,
    kijun_period: int = 26,
    senkou_b_period: int = 52,
) -> dict[str, Any]:
    if len(ohlcv_data) < senkou_b_period + kijun_period:
        return {"tenkan": None, "kijun": None, "senkou_a": None, "senkou_b": None, "chikou": None, "cloud_position": None}

    def _midpoint(data: list[list[float]], period: int, offset: int = 0) -> float:
        start = max(0, len(data) - period - offset)
        end = len(data) - offset if offset > 0 else len(data)
        segment = data[start:end]
        if not segment:
            return 0.0
        highs = [float(c[2]) for c in segment if len(c) > 2]
        lows = [float(c[3]) for c in segment if len(c) > 3]
        if not highs or not lows:
            return 0.0
        return (max(highs) + min(lows)) / 2.0

    tenkan = _midpoint(ohlcv_data, tenkan_period)
    kijun = _midpoint(ohlcv_data, kijun_period)
    senkou_a = (tenkan + kijun) / 2.0
    senkou_b = _midpoint(ohlcv_data, senkou_b_period, kijun_period)
    chikou = float(ohlcv_data[-1][4]) if ohlcv_data else 0.0

    current_price = float(ohlcv_data[-1][4]) if ohlcv_data else 0.0
    cloud_top = max(senkou_a, senkou_b)
    cloud_bottom = min(senkou_a, senkou_b)

    if current_price > cloud_top:
        cloud_position = "above_cloud"
    elif current_price < cloud_bottom:
        cloud_position = "below_cloud"
    else:
        cloud_position = "in_cloud"

    return {
        "tenkan": round(tenkan, 8),
        "kijun": round(kijun, 8),
        "senkou_a": round(senkou_a, 8),
        "senkou_b": round(senkou_b, 8),
        "chikou": round(chikou, 8),
        "cloud_position": cloud_position,
    }


# ─────────────────────────────────────────────
# Supertrend
# ─────────────────────────────────────────────
def calculate_supertrend(
    ohlcv_data: list[list[float]],
    period: int = 10,
    multiplier: float = 3.0,
) -> dict[str, Any]:
    if len(ohlcv_data) < period + 1:
        return {"supertrend": None, "direction": None}

    atr_values: list[float] = []
    for i in range(1, len(ohlcv_data)):
        high = float(ohlcv_data[i][2])
        low = float(ohlcv_data[i][3])
        prev_close = float(ohlcv_data[i - 1][4])
        tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
        atr_values.append(tr)

    if len(atr_values) < period:
        return {"supertrend": None, "direction": None}

    atr = sum(atr_values[-period:]) / period
    last_close = float(ohlcv_data[-1][4])
    last_high = float(ohlcv_data[-1][2])
    last_low = float(ohlcv_data[-1][3])

    hl2 = (last_high + last_low) / 2.0
    basic_upper_band = hl2 + multiplier * atr
    basic_lower_band = hl2 - multiplier * atr

    prev_close = float(ohlcv_data[-2][4])
    prev_hl2 = (float(ohlcv_data[-2][2]) + float(ohlcv_data[-2][3])) / 2.0
    prev_atr = sum(atr_values[-(period + 1):-1]) / period if len(atr_values) >= period + 1 else atr
    prev_lower = prev_hl2 - multiplier * prev_atr
    prev_upper = prev_hl2 + multiplier * prev_atr

    lower_band = basic_lower_band if basic_lower_band > prev_lower or prev_close < prev_lower else prev_lower
    upper_band = basic_upper_band if basic_upper_band < prev_upper or prev_close > prev_upper else prev_upper

    if last_close <= upper_band:
        supertrend_val = upper_band
        direction = "bearish"
    else:
        supertrend_val = lower_band
        direction = "bullish"

    return {
        "supertrend": round(supertrend_val, 8),
        "direction": direction,
    }


# ─────────────────────────────────────────────
# RSI Divergence Detection
# ─────────────────────────────────────────────
def detect_rsi_divergence(
    ohlcv_data: list[list[float]],
    rsi_values: list[float] | None = None,
    lookback: int = 30,
) -> dict[str, Any]:
    if len(ohlcv_data) < lookback or (rsi_values is not None and len(rsi_values) < lookback):
        return {"divergence": None, "strength": 0.0, "type": None}

    closes = [float(c[4]) for c in ohlcv_data[-lookback:]]

    if rsi_values is not None:
        rsi = rsi_values[-lookback:]
    else:
        rsi = _calculate_rsi_simple(closes, 14)

    if len(rsi) < 5:
        return {"divergence": None, "strength": 0.0, "type": None}

    price_swings = _find_swing_points_simple(closes, 5)
    rsi_swings = _find_swing_points_simple(rsi, 5)

    result: dict[str, Any] = {"divergence": None, "strength": 0.0, "type": None}

    if len(price_swings.get("highs", [])) >= 2 and len(rsi_swings.get("highs", [])) >= 2:
        p_highs = price_swings["highs"][-2:]
        r_highs = rsi_swings["highs"][-2:]
        if p_highs[1] > p_highs[0] and r_highs[1] < r_highs[0]:
            strength = abs(p_highs[1] - p_highs[0]) / max(p_highs[0], 0.001) * 100
            result = {"divergence": "bearish", "strength": round(strength, 2), "type": "regular_bearish"}

    if result["divergence"] is None and len(price_swings.get("lows", [])) >= 2 and len(rsi_swings.get("lows", [])) >= 2:
        p_lows = price_swings["lows"][-2:]
        r_lows = rsi_swings["lows"][-2:]
        if p_lows[1] < p_lows[0] and r_lows[1] > r_lows[0]:
            strength = abs(p_lows[0] - p_lows[1]) / max(p_lows[1], 0.001) * 100
            result = {"divergence": "bullish", "strength": round(strength, 2), "type": "regular_bullish"}

    return result


# ─────────────────────────────────────────────
# MACD Divergence Detection
# ─────────────────────────────────────────────
def detect_macd_divergence(
    ohlcv_data: list[list[float]],
    macd_hist: list[float] | None = None,
    lookback: int = 40,
) -> dict[str, Any]:
    if len(ohlcv_data) < lookback:
        return {"divergence": None, "strength": 0.0, "type": None}

    closes = [float(c[4]) for c in ohlcv_data[-lookback:]]

    if macd_hist is not None:
        hist = macd_hist[-lookback:]
    else:
        ema12 = _calculate_ema(closes, 12)
        ema26 = _calculate_ema(closes, 26)
        macd_line = [a - b for a, b in zip(ema12, ema26, strict=True)]
        signal = _calculate_ema(macd_line, 9)
        hist = [m - s for m, s in zip(macd_line, signal, strict=True)]

    if len(hist) < 5:
        return {"divergence": None, "strength": 0.0, "type": None}

    price_swings = _find_swing_points_simple(closes, 5)
    hist_swings = _find_swing_points_simple(hist, 5)

    result: dict[str, Any] = {"divergence": None, "strength": 0.0, "type": None}

    if len(price_swings.get("highs", [])) >= 2 and len(hist_swings.get("highs", [])) >= 2:
        p_highs = price_swings["highs"][-2:]
        h_highs = hist_swings["highs"][-2:]
        if p_highs[1] > p_highs[0] and h_highs[1] < h_highs[0]:
            strength = abs(h_highs[0] - h_highs[1])
            result = {"divergence": "bearish", "strength": round(strength, 4), "type": "regular_bearish"}

    if result["divergence"] is None and len(price_swings.get("lows", [])) >= 2 and len(hist_swings.get("lows", [])) >= 2:
        p_lows = price_swings["lows"][-2:]
        h_lows = hist_swings["lows"][-2:]
        if p_lows[1] < p_lows[0] and h_lows[1] > h_lows[0]:
            strength = abs(h_lows[1] - h_lows[0])
            result = {"divergence": "bullish", "strength": round(strength, 4), "type": "regular_bullish"}

    return result


# ─────────────────────────────────────────────
# TTM Squeeze Detection
# ─────────────────────────────────────────────
def detect_ttm_squeeze(
    ohlcv_data: list[list[float]],
    bb_period: int = 20,
    bb_mult: float = 2.0,
    kc_period: int = 20,
    kc_mult: float = 1.5,
) -> dict[str, Any]:
    if len(ohlcv_data) < max(bb_period, kc_period) + 1:
        return {"squeeze_active": None, "squeeze_fired": None, "keltner_upper": None, "keltner_lower": None}

    closes = [float(c[4]) for c in ohlcv_data]
    highs = [float(c[2]) for c in ohlcv_data]
    lows = [float(c[3]) for c in ohlcv_data]

    bb_data = _calculate_bollinger_bands(closes, bb_period, bb_mult)
    kc_data = _calculate_keltner_channel(highs, lows, closes, kc_period, kc_mult)

    if bb_data["upper"] is None or kc_data["upper"] is None:
        return {"squeeze_active": None, "squeeze_fired": None, "keltner_upper": None, "keltner_lower": None}

    squeeze_active = bb_data["lower"] > kc_data["lower"] and bb_data["upper"] < kc_data["upper"]

    prev_bb = _calculate_bollinger_bands(closes[:-1], bb_period, bb_mult) if len(closes) > bb_period + 1 else None
    prev_kc = _calculate_keltner_channel(highs[:-1], lows[:-1], closes[:-1], kc_period, kc_mult) if len(closes) > kc_period + 1 else None
    prev_squeeze = False
    if prev_bb and prev_kc and prev_bb["lower"] is not None and prev_kc["upper"] is not None:
        prev_squeeze = prev_bb["lower"] > prev_kc["lower"] and prev_bb["upper"] < prev_kc["upper"]

    squeeze_fired = prev_squeeze and not squeeze_active

    return {
        "squeeze_active": squeeze_active,
        "squeeze_fired": squeeze_fired,
        "keltner_upper": round(kc_data["upper"], 8),
        "keltner_lower": round(kc_data["lower"], 8),
    }


# ─────────────────────────────────────────────
# Wyckoff Phase Detection (Accumulation / Distribution / Markup / Markdown)
# ─────────────────────────────────────────────
def detect_wyckoff_phase(
    ohlcv_data: list[list[float]],
    lookback: int = 60,
) -> dict[str, Any]:
    """Heuristic Wyckoff phase detection based on price range, volume profile and trend.

    Phases:
        accumulation - sideways price after downtrend, increasing volume on up bars
        distribution - sideways price after uptrend, increasing volume on down bars
        markup       - clear uptrend (HH/HL) with rising volume
        markdown     - clear downtrend (LH/LL) with rising volume on down bars
        neutral      - insufficient signal
    """
    if not ohlcv_data or len(ohlcv_data) < lookback:
        return {"phase": None, "confidence": 0.0, "notes": "insufficient_data"}

    rows = ohlcv_data[-lookback:]
    highs = [float(r[2]) for r in rows]
    lows = [float(r[3]) for r in rows]
    closes = [float(r[4]) for r in rows]
    volumes = [float(r[5]) for r in rows]

    if not closes or max(highs) <= 0:
        return {"phase": None, "confidence": 0.0, "notes": "invalid_data"}

    range_pct = (max(highs) - min(lows)) / max(closes[-1], 1e-9) * 100.0
    first_third_close = sum(closes[: lookback // 3]) / max(1, lookback // 3)
    last_third_close = sum(closes[-lookback // 3 :]) / max(1, lookback // 3)
    trend_change_pct = (last_third_close - first_third_close) / max(first_third_close, 1e-9) * 100.0

    up_bars_vol = sum(volumes[i] for i in range(1, len(closes)) if closes[i] > closes[i - 1])
    down_bars_vol = sum(volumes[i] for i in range(1, len(closes)) if closes[i] < closes[i - 1])
    total_vol = up_bars_vol + down_bars_vol
    up_vol_ratio = up_bars_vol / total_vol if total_vol > 0 else 0.5

    # Volume trend: compare last 1/3 average vol vs first 1/3 average vol
    early_vol = sum(volumes[: lookback // 3]) / max(1, lookback // 3)
    late_vol = sum(volumes[-lookback // 3 :]) / max(1, lookback // 3)
    vol_increasing = late_vol > early_vol * 1.15

    # Higher highs/lower lows count
    swing_highs = [highs[i] for i in range(2, len(highs) - 2)
                   if highs[i] > highs[i - 1] and highs[i] > highs[i + 1]]
    swing_lows = [lows[i] for i in range(2, len(lows) - 2)
                  if lows[i] < lows[i - 1] and lows[i] < lows[i + 1]]
    hh = sum(1 for i in range(1, len(swing_highs)) if swing_highs[i] > swing_highs[i - 1])
    lh = sum(1 for i in range(1, len(swing_highs)) if swing_highs[i] < swing_highs[i - 1])
    hl = sum(1 for i in range(1, len(swing_lows)) if swing_lows[i] > swing_lows[i - 1])
    ll = sum(1 for i in range(1, len(swing_lows)) if swing_lows[i] < swing_lows[i - 1])

    # Sideways = small trend change & limited HH/LL bias
    is_sideways = abs(trend_change_pct) < 3.0 and range_pct < 12.0

    phase = "neutral"
    confidence = 0.3
    notes = ""

    if is_sideways:
        # Look at the period BEFORE the sideways range (first half) for the prior trend
        prior_change = (closes[lookback // 2] - closes[0]) / max(closes[0], 1e-9) * 100.0
        if prior_change < -5.0 and up_vol_ratio > 0.55 and vol_increasing:
            phase = "accumulation"
            confidence = min(0.85, 0.5 + up_vol_ratio * 0.5)
            notes = f"sideways after downtrend ({prior_change:.1f}%), up-vol={up_vol_ratio:.2f}"
        elif prior_change > 5.0 and up_vol_ratio < 0.45 and vol_increasing:
            phase = "distribution"
            confidence = min(0.85, 0.5 + (1 - up_vol_ratio) * 0.5)
            notes = f"sideways after uptrend ({prior_change:.1f}%), down-vol={1 - up_vol_ratio:.2f}"
        else:
            phase = "neutral_range"
            confidence = 0.4
            notes = f"sideways, prior_change={prior_change:.1f}%"
    elif trend_change_pct > 5.0 and hh > lh and hl > ll:
        phase = "markup"
        confidence = min(0.9, 0.5 + min(trend_change_pct / 30.0, 0.4))
        notes = f"uptrend +{trend_change_pct:.1f}%, HH={hh} HL={hl}"
    elif trend_change_pct < -5.0 and lh > hh and ll > hl:
        phase = "markdown"
        confidence = min(0.9, 0.5 + min(abs(trend_change_pct) / 30.0, 0.4))
        notes = f"downtrend {trend_change_pct:.1f}%, LH={lh} LL={ll}"
    else:
        phase = "transition"
        confidence = 0.35
        notes = f"trend_change={trend_change_pct:.1f}%"

    return {
        "phase": phase,
        "confidence": round(confidence, 3),
        "trend_change_pct": round(trend_change_pct, 2),
        "range_pct": round(range_pct, 2),
        "up_volume_ratio": round(up_vol_ratio, 3),
        "volume_increasing": vol_increasing,
        "hh_count": hh,
        "lh_count": lh,
        "hl_count": hl,
        "ll_count": ll,
        "notes": notes,
    }


# ─────────────────────────────────────────────
# Pivot Points
# ─────────────────────────────────────────────
def calculate_pivot_points(
    ohlcv_data: list[list[float]],
) -> dict[str, Any]:
    if len(ohlcv_data) < 2:
        return {"r1": None, "r2": None, "s1": None, "s2": None, "pp": None}

    prev = ohlcv_data[-2]
    h = float(prev[2])
    low = float(prev[3])
    c = float(prev[4])

    pp = (h + low + c) / 3.0
    r1 = 2.0 * pp - low
    s1 = 2.0 * pp - h
    r2 = pp + (h - low)
    s2 = pp - (h - low)

    return {
        "r1": round(r1, 8),
        "r2": round(r2, 8),
        "s1": round(s1, 8),
        "s2": round(s2, 8),
        "pp": round(pp, 8),
    }


# ─────────────────────────────────────────────
# Williams %R and CCI
# ─────────────────────────────────────────────
def calculate_williams_r(
    ohlcv_data: list[list[float]],
    period: int = 14,
) -> float | None:
    if len(ohlcv_data) < period:
        return None
    recent = ohlcv_data[-period:]
    highs = [float(c[2]) for c in recent]
    lows = [float(c[3]) for c in recent]
    close = float(ohlcv_data[-1][4])
    hh = max(highs)
    ll = min(lows)
    if hh == ll:
        return -50.0
    return round((hh - close) / (hh - ll) * -100.0, 2)


def calculate_cci(
    ohlcv_data: list[list[float]],
    period: int = 20,
) -> float | None:
    if len(ohlcv_data) < period:
        return None
    tps: list[float] = []
    for c in ohlcv_data[-period:]:
        h = float(c[2])
        low = float(c[3])
        cl = float(c[4])
        tps.append((h + low + cl) / 3.0)
    mean = sum(tps) / len(tps)
    md = sum(abs(tp - mean) for tp in tps) / len(tps)
    if md == 0:
        return 0.0
    return round((tps[-1] - mean) / (0.015 * md), 2)


# ─────────────────────────────────────────────
# BTC Dominance
# ─────────────────────────────────────────────
async def fetch_btc_dominance() -> dict[str, Any]:
    async def _fetch() -> dict[str, Any]:
        result: dict[str, Any] = {"btc_dominance": None, "change_24h": None}
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as session:
                url = "https://api.coingecko.com/api/v3/global"
                async with session.get(url) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        gd = data.get("data", {})
                        result["btc_dominance"] = float(gd.get("market_cap_percentage", {}).get("btc", 0))
                        result["change_24h"] = float(gd.get("market_cap_change_percentage_24h_usd", 0))
        except Exception as e:
            logger.debug(f"[EnhancedData] BTC dominance fetch failed: {e}")
        return result
    return await _fetch_with_cache("btc_dominance", _fetch, ttl=600)


# ─────────────────────────────────────────────
# Active Trading Session Detection
# ─────────────────────────────────────────────
def detect_active_session() -> str:
    now = utcnow()
    hour = now.hour
    if 0 <= hour < 8:
        return "asian"
    elif 8 <= hour < 14:
        return "london"
    elif 14 <= hour < 21:
        return "new_york"
    return "off_hours"


# ─────────────────────────────────────────────
# MTF Momentum Alignment Score
# ─────────────────────────────────────────────
def calculate_mtf_momentum_alignment(
    rsi_1h: float | None = None,
    rsi_4h: float | None = None,
    rsi_15m: float | None = None,
    ema_fast: float | None = None,
    ema_slow: float | None = None,
    ema_200: float | None = None,
    current_price: float = 0.0,
    supertrend_direction: str | None = None,
    ichimoku_cloud_position: str | None = None,
) -> float:
    bullish_votes = 0.0
    total_votes = 0.0

    def _rsi_vote(rsi: float | None, weight: float) -> tuple[float, float]:
        if rsi is None:
            return 0.0, 0.0
        if rsi > 55:
            return weight, weight
        elif rsi < 45:
            return 0.0, weight
        return weight * 0.5, weight

    for rsi, w in [(rsi_15m, 1.0), (rsi_1h, 2.0), (rsi_4h, 3.0)]:
        bv, tv = _rsi_vote(rsi, w)
        bullish_votes += bv
        total_votes += tv

    if ema_fast is not None and ema_slow is not None:
        total_votes += 2.0
        if ema_fast > ema_slow:
            bullish_votes += 2.0

    if ema_200 is not None and current_price > 0:
        total_votes += 2.0
        if current_price > ema_200:
            bullish_votes += 2.0

    if supertrend_direction is not None:
        total_votes += 2.0
        if supertrend_direction == "bullish":
            bullish_votes += 2.0

    if ichimoku_cloud_position is not None:
        total_votes += 2.0
        if ichimoku_cloud_position == "above_cloud":
            bullish_votes += 2.0
        elif ichimoku_cloud_position == "in_cloud":
            bullish_votes += 1.0

    if total_votes == 0:
        return 0.5

    return round(bullish_votes / total_votes, 3)


# ─────────────────────────────────────────────
# Internal helper functions
# ─────────────────────────────────────────────
def _calculate_rsi_simple(closes: list[float], period: int = 14) -> list[float]:
    if len(closes) < period + 1:
        return []
    deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    gains = [max(0.0, d) for d in deltas]
    losses = [max(0.0, -d) for d in deltas]
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    rsi_values: list[float] = []
    if avg_loss == 0:
        rsi_values.append(100.0)
    else:
        rs = avg_gain / avg_loss
        rsi_values.append(100.0 - 100.0 / (1.0 + rs))
    for i in range(period, len(deltas)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        if avg_loss == 0:
            rsi_values.append(100.0)
        else:
            rs = avg_gain / avg_loss
            rsi_values.append(100.0 - 100.0 / (1.0 + rs))
    return rsi_values


def _calculate_ema(data: list[float], period: int) -> list[float]:
    if len(data) < period:
        return []
    multiplier = 2.0 / (period + 1)
    ema = [sum(data[:period]) / period]
    for i in range(period, len(data)):
        ema.append(data[i] * multiplier + ema[-1] * (1.0 - multiplier))
    return ema


def _find_swing_points_simple(data: list[float], lookback: int = 5) -> dict[str, list[float]]:
    highs: list[float] = []
    lows: list[float] = []
    if len(data) < lookback * 2 + 1:
        return {"highs": highs, "lows": lows}
    for i in range(lookback, len(data) - lookback):
        is_high = all(data[i] >= data[i - j] for j in range(1, lookback + 1)) and \
                  all(data[i] >= data[i + j] for j in range(1, lookback + 1))
        is_low = all(data[i] <= data[i - j] for j in range(1, lookback + 1)) and \
                 all(data[i] <= data[i + j] for j in range(1, lookback + 1))
        if is_high:
            highs.append(data[i])
        if is_low:
            lows.append(data[i])
    return {"highs": highs, "lows": lows}


def _calculate_bollinger_bands(closes: list[float], period: int = 20, mult: float = 2.0) -> dict[str, Any]:
    if len(closes) < period:
        return {"upper": None, "middle": None, "lower": None}
    recent = closes[-period:]
    mean = sum(recent) / period
    variance = sum((c - mean) ** 2 for c in recent) / period
    std = variance ** 0.5
    return {
        "upper": round(mean + mult * std, 8),
        "middle": round(mean, 8),
        "lower": round(mean - mult * std, 8),
    }


def _calculate_keltner_channel(
    highs: list[float], lows: list[float], closes: list[float],
    period: int = 20, mult: float = 1.5,
) -> dict[str, Any]:
    if len(closes) < period + 1:
        return {"upper": None, "middle": None, "lower": None}
    trs: list[float] = []
    for i in range(1, len(closes)):
        tr = max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1]))
        trs.append(tr)
    if len(trs) < period:
        return {"upper": None, "middle": None, "lower": None}
    atr = sum(trs[-period:]) / period
    ema = sum(closes[-period:]) / period
    return {
        "upper": round(ema + mult * atr, 8),
        "middle": round(ema, 8),
        "lower": round(ema - mult * atr, 8),
    }


# ─────────────────────────────────────────────
# Fetch All Enhanced Data (updated with new checks)
# 鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺?
async def fetch_all_enhanced_data(symbol: str, ohlcv_data: list[list[float]] | None = None) -> dict[str, Any]:
    """
    Fetch all enhanced market data in parallel.
    Includes liquidity analysis.
    """
    from market_data import fetch_market_context

    market_ctx = await fetch_market_context(symbol)
    current_price = float(market_ctx.current_price or 0)

    ohlcv_rows = ohlcv_data if ohlcv_data else []
    vol_zscore_coro = calculate_volume_zscore(ohlcv_rows) if len(ohlcv_rows) > 21 else asyncio.sleep(0)
    atr_pct_coro = calculate_atr_percentile(ohlcv_rows) if len(ohlcv_rows) > 104 else asyncio.sleep(0)
    from core.config import settings as _settings
    ob_slippage_coro = estimate_orderbook_slippage(symbol, float(getattr(_settings, 'scanner', None) and _settings.scanner.liquidity_order_size_usdt or 1000)) if True else asyncio.sleep(0)

    results = await asyncio.gather(
        fetch_liquidation_heatmap(symbol),
        fetch_long_short_ratio(symbol),
        fetch_basis_data(symbol),
        fetch_fear_greed_index(),
        check_macro_event_risk(),
        analyze_liquidity_structure(symbol, current_price, ohlcv_data),
        vol_zscore_coro,
        atr_pct_coro,
        ob_slippage_coro,
        return_exceptions=True,
    )

    # Handle exceptions in results
    processed_results = []
    for i, result in enumerate(results):
        if isinstance(result, Exception):
            logger.warning(f"[EnhancedData] Enhanced data fetch #{i} failed: {result}")
            processed_results.append({})
        else:
            processed_results.append(result)
    results = processed_results

    cvd_data = {}
    regime_data = {}
    ichimoku_data = {}
    supertrend_data = {}
    rsi_div_data = {}
    macd_div_data = {}
    ttm_squeeze_data = {}
    pivot_data = {}
    williams_r_data = {}
    cci_data = {}
    wyckoff_data = {}
    session_data = {}

    if ohlcv_data and len(ohlcv_data) >= 20:
        cvd_data = await calculate_cvd_divergence(ohlcv_data)
        regime_data = await detect_volatility_regime(ohlcv_data)

    if ohlcv_data and len(ohlcv_data) >= 52:
        ichimoku_data = calculate_ichimoku(ohlcv_data)

    if ohlcv_data and len(ohlcv_data) >= 11:
        supertrend_data = calculate_supertrend(ohlcv_data)

    if ohlcv_data and len(ohlcv_data) >= 30:
        rsi_div_data = detect_rsi_divergence(ohlcv_data)

    if ohlcv_data and len(ohlcv_data) >= 40:
        macd_div_data = detect_macd_divergence(ohlcv_data)

    if ohlcv_data and len(ohlcv_data) >= 21:
        ttm_squeeze_data = detect_ttm_squeeze(ohlcv_data)

    if ohlcv_data and len(ohlcv_data) >= 2:
        pivot_data = calculate_pivot_points(ohlcv_data)

    if ohlcv_data and len(ohlcv_data) >= 14:
        williams_r_data = calculate_williams_r(ohlcv_data)

    if ohlcv_data and len(ohlcv_data) >= 20:
        cci_data = calculate_cci(ohlcv_data)

    if ohlcv_data and len(ohlcv_data) >= 60:
        wyckoff_data = detect_wyckoff_phase(ohlcv_data)

    session_data = detect_active_session()

    btc_dom_data = {}
    try:
        btc_dom_data = await fetch_btc_dominance()
    except Exception:
        btc_dom_data = {"btc_dominance": None, "btc_dominance_change_24h": None}

    return {
        "liquidation_heatmap": results[0],
        "long_short_ratio": results[1],
        "basis": results[2],
        "fear_greed": results[3],
        "macro_event_safe": results[4][0],
        "macro_event_reason": results[4][1],
        "cvd_divergence": cvd_data,
        "volatility_regime": regime_data,
        "liquidity": results[5],
        "volume_zscore": results[6],
        "atr_percentile": results[7],
        "orderbook_slippage": results[8],
        "ichimoku": ichimoku_data,
        "supertrend": supertrend_data,
        "rsi_divergence": rsi_div_data,
        "macd_divergence": macd_div_data,
        "ttm_squeeze": ttm_squeeze_data,
        "pivot_points": pivot_data,
        "williams_r": williams_r_data,
        "cci": cci_data,
        "wyckoff": wyckoff_data,
        "session": session_data,
        "btc_dominance": btc_dom_data,
    }
