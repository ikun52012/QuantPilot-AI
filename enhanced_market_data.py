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
from datetime import datetime, timezone
from typing import Any, TypeVar, cast

import aiohttp
from loguru import logger

from core.utils.datetime import utcnow

_cache_ttl = 300
T = TypeVar("T")

_cache: dict[str, tuple[float, object]] = {}
_cache_lock = asyncio.Lock()

MACRO_EVENTS_CACHE_KEY = "macro_events"
LIQUIDATION_CACHE_KEY = "liquidation_heatmap"
FEAR_GREED_CACHE_KEY = "fear_greed_index"


async def _fetch_with_cache(key: str, fetcher: Callable[[], Awaitable[T]], ttl: int = _cache_ttl) -> T:
    """Fetch data with cache to avoid repeated API calls."""
    now = time.time()
    async with _cache_lock:
        if key in _cache:
            cached_time, cached_data = _cache[key]
            if now - cached_time < ttl:
                return cast(T, cached_data)

    data = await fetcher()
    if data is not None:
        async with _cache_lock:
            _cache[key] = (time.time(), data)
    return data


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
                    url = f"https://financialmodelingprep.com/api/v3/economic_calendar?apikey={fmp_api_key}"
                    async with session.get(url) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            now = utcnow()
                            for event in data:
                                event_time = datetime.fromisoformat(event.get("date", "").replace("Z", "+00:00"))
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
            event_date = datetime.fromisoformat(event["approximate_date"]).replace(tzinfo=timezone.utc)
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
                except (aiohttp.ClientError, OSError, asyncio.TimeoutError) as e:
                    logger.debug(f"[EnhancedMarketData] Liquidation API error for {symbol}: {e}")
                except Exception as e:
                    logger.debug(f"[EnhancedMarketData] Unexpected error fetching liquidation for {symbol}: {e}")

        except Exception as e:
            logger.warning(f"[EnhancedData] Failed to fetch liquidation heatmap for {symbol}: {e}")

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


async def calculate_cvd_divergence(ohlcv_data: list[list[float]], lookback: int = 20) -> dict[str, Any]:
    """
    Calculate CVD (Cumulative Volume Delta) divergence.
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


async def detect_volatility_regime(ohlcv_data: list[list[float]], lookback: int = 100) -> dict[str, Any]:
    """
    Detect current volatility regime.
    Uses local OHLCV data - no external API needed.

    Returns regime classification and position sizing suggestion.
    """
    if len(ohlcv_data) < lookback:
        return {"regime": "unknown", "atr_pct": None, "suggestion": None}

    recent_atr: list[float] = []
    for i in range(max(0, len(ohlcv_data) - lookback), len(ohlcv_data) - 14):
        window = ohlcv_data[i:i+14]
        if len(window) >= 14:
            highs = [c[2] for c in window]
            lows = [c[3] for c in window]
            closes = [c[4] for c in window]
            tr_sum = 0.0
            for j in range(1, len(window)):
                tr = max(highs[j] - lows[j], abs(highs[j] - closes[j-1]), abs(lows[j] - closes[j-1]))
                tr_sum += tr
            atr = tr_sum / 13
            atr_pct = atr / closes[-1] * 100 if closes[-1] > 0 else 0
            recent_atr.append(atr_pct)

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

    if current_atr_pct < avg_atr_pct * 0.5:
        regime_data["regime"] = "low_volatility"
        regime_data["suggestion"] = "breakout_approach"
    elif current_atr_pct > avg_atr_pct * 2.0:
        regime_data["regime"] = "extreme_volatility"
        regime_data["suggestion"] = "pause_trading"
    elif current_atr_pct > avg_atr_pct * 1.5:
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


async def fetch_all_enhanced_data(symbol: str, ohlcv_data: list[list[float]] | None = None) -> dict[str, Any]:
    """
    Fetch all enhanced market data in parallel.
    Includes liquidity analysis.
    """
    from market_data import fetch_market_context

    # Get current price first
    market_ctx = await fetch_market_context(symbol)
    current_price = float(market_ctx.current_price or 0)

    results = await asyncio.gather(
        fetch_liquidation_heatmap(symbol),
        fetch_long_short_ratio(symbol),
        fetch_basis_data(symbol),
        fetch_fear_greed_index(),
        check_macro_event_risk(),
        analyze_liquidity_structure(symbol, current_price, ohlcv_data),
    )

    cvd_data = {}
    regime_data = {}
    if ohlcv_data and len(ohlcv_data) >= 20:
        cvd_data = await calculate_cvd_divergence(ohlcv_data)
        regime_data = await detect_volatility_regime(ohlcv_data)

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
    }
