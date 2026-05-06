"""
QuantPilot AI - Market Data Fetcher
Fetches real-time market data from the exchange via ccxt.
Includes WebSocket streaming for real-time updates (Optimization 6).
ENHANCED: Improved handling for perpetual contracts (.P tickers) and low liquidity assets.
"""
import asyncio
import json
import os
import time
from collections import OrderedDict
from datetime import datetime, timezone
from typing import Any, cast

from loguru import logger

from core.config import settings
from core.security import get_secure_api_key
from exchange import _CCXT_AVAILABLE, _get_or_create_exchange, _resolve_symbol, ccxt
from models import MarketContext

_MARKET_CACHE_TTL = 60
_MARKET_CACHE_MAX_SIZE = 500
_PUBLIC_MARKET_DATA_FALLBACKS = ("binance", "okx", "bybit", "bitget", "gate", "coinbase")
_market_cache: OrderedDict[str, tuple[float, MarketContext]] = OrderedDict()
_market_cache_locks: dict[str, asyncio.Lock] = {}
_market_cache_locks_guard = asyncio.Lock()

# WebSocket streaming state (Optimization 6)
_ws_connections: dict[str, Any] = {}
_ws_data_buffer: dict[str, dict] = {}
_ws_subscribers: dict[str, list[asyncio.Queue]] = {}
_ws_manager_task: asyncio.Task | None = None


# ─────────────────────────────────────────────
# ENHANCED: Known low liquidity / niche tickers
# ─────────────────────────────────────────────

# Tickers that often have insufficient OHLCV data
KNOWN_LOW_LIQUIDITY_TICKERS = {
    # New or niche perpetuals on Binance
    "APE", "DYDX", "GMX", "RDNT", "TRB", "BLUR", "ARB", "OP", "SSV", "MAGIC",
    "GLMR", "CELO", "GAL", "APT", "INJ", "SUI", "SEI", "WLD", "YGG", "ILV",
    "IMX", "RNDR", "FET", "AGIX", "QNT", "HFT", "HOOK", "AMB", "ALGO",
    # Commodities and stocks on Binance (often low OHLCV)
    "XAU", "XAG", "BTCST", "BNBDOWN", "BNBUP", "BTCDOWN", "BTCUP",
}

# Minimum OHLCV candles for different ticker types
MIN_OHLCV_STANDARD = 15  # Standard requirement for most tickers
MIN_OHLCV_LOW_LIQUIDITY = 7  # Fallback minimum for low liquidity tickers


def _is_low_liquidity_ticker(ticker: str) -> bool:
    """Detect if ticker is known to have low liquidity/insufficient data."""
    ticker_clean = ticker.upper().replace(".P", "").replace("PERP", "").replace("USDT", "").replace("USD", "")
    return ticker_clean in KNOWN_LOW_LIQUIDITY_TICKERS


def _get_min_ohlcv_requirement(ticker: str) -> int:
    """Get minimum OHLCV candles required based on ticker liquidity."""
    if _is_low_liquidity_ticker(ticker):
        return MIN_OHLCV_LOW_LIQUIDITY
    return MIN_OHLCV_STANDARD


# ─────────────────────────────────────────────
# OHLCV Data Cleaning (P2-10: Data validation)
# ─────────────────────────────────────────────
def _clean_ohlcv_data(ohlcv: list[list[Any]], max_candles: int | None = None) -> list[list[float]]:
    """Clean and validate OHLCV data.

    P2-10: Remove duplicates, invalid prices, validate high>=low, sort by timestamp.

    Args:
        ohlcv: Raw OHLCV data from exchange
        max_candles: Optional limit on number of candles to return

    Returns:
        Cleaned OHLCV data as list of [timestamp, open, high, low, close, volume]
    """
    if not ohlcv:
        return []

    cleaned = []
    seen_timestamps = set()

    for candle in ohlcv:
        try:
            # Extract values with validation
            ts = float(candle[0])
            open_p = float(candle[1])
            high_p = float(candle[2])
            low_p = float(candle[3])
            close_p = float(candle[4])
            volume = float(candle[5]) if len(candle) >= 6 else 0.0

            # Skip duplicates
            if ts in seen_timestamps:
                continue

            # Skip invalid prices (must be positive)
            if any(p <= 0 for p in [open_p, high_p, low_p, close_p]):
                continue

            # Skip invalid volume (must be non-negative)
            if volume < 0:
                continue

            # Validate high >= low (fix if violated)
            if high_p < low_p:
                high_p = max(open_p, close_p)
                low_p = min(open_p, close_p)

            # Validate high >= open and high >= close
            high_p = max(high_p, open_p, close_p)

            # Validate low <= open and low <= close
            low_p = min(low_p, open_p, close_p)

            seen_timestamps.add(ts)
            cleaned.append([ts, open_p, high_p, low_p, close_p, volume])
        except (ValueError, TypeError, IndexError):
            continue

    # Sort by timestamp (ascending order)
    cleaned.sort(key=lambda x: x[0])

    # Apply max_candles limit (keep most recent)
    if max_candles and len(cleaned) > max_candles:
        cleaned = cleaned[-max_candles:]

    return cleaned


def _calculate_vwap(ohlcv: list[list[float]], lookback: int = 24) -> dict[str, float | None]:
    """Calculate rolling VWAP from OHLCV candles."""
    candles = [c for c in ohlcv[-lookback:] if len(c) >= 6 and c[5] > 0]
    if not candles:
        return {"vwap": None, "distance_pct": None}

    total_pv = 0.0
    total_volume = 0.0
    for candle in candles:
        typical_price = (float(candle[2]) + float(candle[3]) + float(candle[4])) / 3.0
        volume = float(candle[5])
        total_pv += typical_price * volume
        total_volume += volume

    if total_volume <= 0:
        return {"vwap": None, "distance_pct": None}

    vwap = total_pv / total_volume
    current_price = float(candles[-1][4])
    distance_pct = ((current_price - vwap) / vwap * 100.0) if vwap > 0 else None
    return {
        "vwap": round(vwap, 8),
        "distance_pct": round(distance_pct, 4) if distance_pct is not None else None,
    }


def _calculate_volume_profile(
    ohlcv: list[list[float]],
    lookback: int = 96,
    bins: int = 24,
    value_area_pct: float = 0.70,
) -> dict[str, Any]:
    """Build a simple volume profile with POC and value area from recent candles."""
    candles = [c for c in ohlcv[-lookback:] if len(c) >= 6 and c[5] > 0]
    if len(candles) < 3:
        return {"poc": None, "value_area_high": None, "value_area_low": None, "high_volume_nodes": [], "low_volume_nodes": []}

    lows = [float(c[3]) for c in candles]
    highs = [float(c[2]) for c in candles]
    low = min(lows)
    high = max(highs)
    if high <= low:
        return {"poc": None, "value_area_high": None, "value_area_low": None, "high_volume_nodes": [], "low_volume_nodes": []}

    bin_count = max(4, min(int(bins), 80))
    step = (high - low) / bin_count
    volumes = [0.0 for _ in range(bin_count)]

    for candle in candles:
        typical_price = (float(candle[2]) + float(candle[3]) + float(candle[4])) / 3.0
        idx = int((typical_price - low) / step)
        idx = max(0, min(bin_count - 1, idx))
        volumes[idx] += float(candle[5])

    total_volume = sum(volumes)
    if total_volume <= 0:
        return {"poc": None, "value_area_high": None, "value_area_low": None, "high_volume_nodes": [], "low_volume_nodes": []}

    def midpoint(index: int) -> float:
        return low + (index + 0.5) * step

    poc_idx = max(range(bin_count), key=lambda i: volumes[i])
    ranked = sorted(range(bin_count), key=lambda i: volumes[i], reverse=True)
    selected: list[int] = []
    running_volume = 0.0
    for idx in ranked:
        selected.append(idx)
        running_volume += volumes[idx]
        if running_volume >= total_volume * value_area_pct:
            break

    nonzero = [idx for idx in range(bin_count) if volumes[idx] > 0]
    high_volume_nodes = [round(midpoint(idx), 8) for idx in ranked[:3] if volumes[idx] > 0]
    low_volume_nodes = [round(midpoint(idx), 8) for idx in sorted(nonzero, key=lambda i: volumes[i])[:3]]

    return {
        "poc": round(midpoint(poc_idx), 8),
        "value_area_high": round(max(midpoint(idx) for idx in selected), 8),
        "value_area_low": round(min(midpoint(idx) for idx in selected), 8),
        "high_volume_nodes": high_volume_nodes,
        "low_volume_nodes": low_volume_nodes,
        "lookback_bars": len(candles),
    }


def _calculate_session_levels(ohlcv: list[list[float]]) -> dict[str, float | None]:
    """Calculate current and previous UTC session high/low levels."""
    candles = [c for c in ohlcv if len(c) >= 5]
    if not candles:
        return {"session_high": None, "session_low": None, "prior_session_high": None, "prior_session_low": None}

    def session_key(candle: list[float]) -> str:
        return datetime.fromtimestamp(float(candle[0]) / 1000.0, tz=timezone.utc).strftime("%Y-%m-%d")

    latest_key = session_key(candles[-1])
    current_session = [c for c in candles if session_key(c) == latest_key]
    if len(current_session) < 3:
        current_session = candles[-24:]

    prior_sessions = sorted({session_key(c) for c in candles if session_key(c) != latest_key})
    prior_session = [c for c in candles if prior_sessions and session_key(c) == prior_sessions[-1]]

    return {
        "session_high": round(max(float(c[2]) for c in current_session), 8) if current_session else None,
        "session_low": round(min(float(c[3]) for c in current_session), 8) if current_session else None,
        "prior_session_high": round(max(float(c[2]) for c in prior_session), 8) if prior_session else None,
        "prior_session_low": round(min(float(c[3]) for c in prior_session), 8) if prior_session else None,
    }


def _detect_liquidity_sweep(ohlcv: list[list[float]], lookback: int = 20) -> dict[str, Any]:
    """Detect recent stop-run sweeps around prior swing highs/lows."""
    candles = [c for c in ohlcv if len(c) >= 5]
    if len(candles) < 5:
        return {"type": "none", "swept_level": None, "strength": 0.0, "recent_high": None, "recent_low": None}

    window = candles[-(lookback + 1):]
    prior = window[:-1]
    last = window[-1]
    recent_high = max(float(c[2]) for c in prior)
    recent_low = min(float(c[3]) for c in prior)
    last_high = float(last[2])
    last_low = float(last[3])
    last_close = float(last[4])
    candle_range = max(last_high - last_low, 0.0)

    sweep_type = "none"
    swept_level: float | None = None
    strength = 0.0
    if last_high > recent_high and last_close < recent_high:
        sweep_type = "bearish_high_sweep"
        swept_level = recent_high
        strength = (last_high - recent_high) / candle_range if candle_range > 0 else 0.0
    elif last_low < recent_low and last_close > recent_low:
        sweep_type = "bullish_low_sweep"
        swept_level = recent_low
        strength = (recent_low - last_low) / candle_range if candle_range > 0 else 0.0

    return {
        "type": sweep_type,
        "swept_level": round(swept_level, 8) if swept_level else None,
        "strength": round(max(0.0, min(strength, 1.0)), 4),
        "recent_high": round(recent_high, 8),
        "recent_low": round(recent_low, 8),
    }


def build_entry_exit_indicator_context(
    ohlcv_1h: list[list[float]] | None = None,
    ohlcv_15m: list[list[float]] | None = None,
    ohlcv_5m: list[list[float]] | None = None,
) -> dict[str, Any]:
    """Build extra AI-only indicators for entry and exit placement."""
    one_hour = ohlcv_1h or []
    intraday = ohlcv_15m or ohlcv_5m or one_hour
    sweep_source = ohlcv_5m or ohlcv_15m or one_hour
    return {
        "vwap_1h_24": _calculate_vwap(one_hour, lookback=24),
        "volume_profile_1h": _calculate_volume_profile(one_hour, lookback=96),
        "session_levels": _calculate_session_levels(one_hour),
        "liquidity_sweep": _detect_liquidity_sweep(sweep_source, lookback=20),
        "intraday_vwap": _calculate_vwap(intraday, lookback=48),
    }


class MarketDataWebSocketManager:
    """Manage WebSocket connections for real-time market data (Optimization 6)."""

    def __init__(self):
        self._running = False
        self._connections: dict[str, Any] = {}
        self._data: dict[str, dict] = {}
        self._last_update: dict[str, float] = {}

    async def start(self):
        """Start WebSocket manager background task."""
        if not settings.ai.websocket_market_data_enabled:
            logger.debug("[MarketWS] WebSocket market data disabled in config")
            return

        if self._running:
            return

        self._running = True
        logger.info("[MarketWS] WebSocket market data manager started")

    async def stop(self):
        """Stop WebSocket manager and close all connections."""
        self._running = False
        for ticker, conn in self._connections.items():
            try:
                if conn:
                    await conn.close()
            except Exception as e:
                logger.warning(f"[MarketWS] Failed to close connection for {ticker}: {e}")
        self._connections.clear()
        logger.info("[MarketWS] WebSocket manager stopped")

    async def subscribe_ticker(self, ticker: str) -> bool:
        """Subscribe to real-time updates for a ticker.

        Returns True if WebSocket subscription is active,
        False if using REST API fallback.
        """
        if not settings.ai.websocket_market_data_enabled:
            return False

        if not self._running:
            await self.start()

        if ticker in self._connections:
            return True

        try:
            exchange_name = getattr(settings.exchange, "name", "okx").lower()
            exchange = _get_or_create_exchange(
                exchange_name,
                api_key="",
                api_secret="",
            )
            symbol = await asyncio.to_thread(_resolve_symbol, exchange, ticker)

            ws_url = self._get_ws_url(exchange_name, symbol)
            if not ws_url:
                logger.debug(f"[MarketWS] No WebSocket URL for {exchange_name}")
                return False

            conn = await self._connect_ws(ws_url, ticker, symbol)
            if conn:
                self._connections[ticker] = conn
                logger.info(f"[MarketWS] Subscribed to {ticker} via WebSocket")
                return True
        except Exception as e:
            logger.warning(f"[MarketWS] Failed to subscribe {ticker}: {e}")

        return False

    def _get_ws_url(self, exchange: str, symbol: str) -> str | None:
        """Get WebSocket URL for exchange."""
        ws_urls = {
            "okx": "wss://ws.okx.com:8443/ws/v5/public",
            "binance": f"wss://stream.binance.com:9443/ws/{symbol.lower()}@ticker",
            "bitget": "wss://ws.bitget.com/v2/ws/public",
            "gate": f"wss://api.gateio.ws/ws/v4/{symbol.lower()}",
        }
        return ws_urls.get(exchange)

    async def _connect_ws(self, url: str, ticker: str, symbol: str):
        """Connect to WebSocket and handle messages."""
        try:
            import websockets
            conn = await websockets.connect(url, ping_interval=30, ping_timeout=10)

            # Subscribe to ticker channel
            if "okx" in url:
                subscribe_msg = {
                    "op": "subscribe",
                    "args": [{"channel": "tickers", "instId": symbol}]
                }
                await conn.send(json.dumps(subscribe_msg))

            # Start message handler
            asyncio.create_task(self._handle_messages(conn, ticker))
            return conn
        except ImportError:
            logger.warning("[MarketWS] websockets package not installed, using REST fallback")
            return None
        except Exception as e:
            logger.warning(f"[MarketWS] Connection failed: {e}")
            return None

    async def _handle_messages(self, conn, ticker: str):
        """Handle incoming WebSocket messages."""
        try:
            while self._running:
                msg = await conn.recv()
                data = json.loads(msg)

                # Parse ticker data based on exchange format
                parsed = self._parse_ws_data(data, ticker)
                if parsed:
                    self._data[ticker] = parsed
                    self._last_update[ticker] = time.monotonic()

                    # Update market cache directly
                    await self._update_market_cache(ticker, parsed)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.warning(f"[MarketWS] Message handler error for {ticker}: {e}")

    def _parse_ws_data(self, data: dict, ticker: str) -> dict | None:
        """Parse WebSocket data into standardized format."""
        # OKX format
        if "data" in data and isinstance(data["data"], list):
            for item in data["data"]:
                if "instId" in item:
                    return {
                        "price": float(item.get("last", 0)),
                        "high": float(item.get("high24h", 0)),
                        "low": float(item.get("low24h", 0)),
                        "volume": float(item.get("vol24h", 0)),
                        "change_pct": float(item.get("change24h", 0)),
                    }

        # Binance format
        if "c" in data or "p" in data:
            return {
                "price": float(data.get("c") or data.get("p") or 0),
                "high": float(data.get("h", 0)),
                "low": float(data.get("l", 0)),
                "volume": float(data.get("v", 0)),
                "change_pct": float(data.get("P", 0)),
            }

        return None

    async def _update_market_cache(self, ticker: str, data: dict):
        """Update market cache with WebSocket data."""
        global _market_cache

        # Get existing cache entry or create minimal context
        async with _market_cache_locks_guard:
            lock = _market_cache_locks.get(ticker)
            if lock is None:
                lock = asyncio.Lock()
                _market_cache_locks[ticker] = lock

        async with lock:
            existing = _market_cache.get(ticker)
            if existing:
                _, context = existing
                # Update with new data
                context.current_price = data.get("price", context.current_price)
                context.high_24h = data.get("high", context.high_24h)
                context.low_24h = data.get("low", context.low_24h)
                context.volume_24h = data.get("volume", context.volume_24h)
                context.price_change_24h = data.get("change_pct", context.price_change_24h)
            else:
                # Create minimal context
                context = MarketContext(
                    ticker=ticker,
                    current_price=data.get("price", 0),
                    high_24h=data.get("high", 0),
                    low_24h=data.get("low", 0),
                    volume_24h=data.get("volume", 0),
                    price_change_pct_24h=data.get("change_pct", 0),
                )

            _market_cache[ticker] = (time.monotonic(), context)
            _market_cache.move_to_end(ticker)

            # Cleanup
            while len(_market_cache) > _MARKET_CACHE_MAX_SIZE:
                _market_cache.popitem(last=False)

        logger.debug(f"[MarketWS] Updated cache for {ticker}: price={data.get('price')}")

    def get_latest_data(self, ticker: str) -> dict | None:
        """Get latest WebSocket data for ticker."""
        if ticker in self._last_update:
            if time.monotonic() - self._last_update[ticker] < 30:
                return self._data.get(ticker)
        return None


# Global WebSocket manager instance
_ws_manager = MarketDataWebSocketManager()


async def get_ws_market_manager() -> MarketDataWebSocketManager:
    """Get global WebSocket manager instance."""
    return _ws_manager


def _to_float(value: object, default: float = 0.0) -> float:
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return default
    return default


def _to_optional_float(value: object) -> float | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None


def _get_exchange() -> ccxt.Exchange:
    """Create a ccxt exchange instance (uses connection pool when available)."""
    if not _CCXT_AVAILABLE:
        raise RuntimeError("ccxt is not installed; market data fetch is unavailable")
    return _get_or_create_exchange(
        exchange_id=settings.exchange.name,
        api_key=settings.exchange.api_key,
        api_secret=settings.exchange.api_secret,
        password=settings.exchange.password,
        live=False,
        sandbox=settings.exchange.sandbox_mode,
    )


def _get_public_market_exchange(exchange_id: str) -> ccxt.Exchange:
    """Create a public-only market-data exchange without user credentials.
    
    For exchanges supporting perpetual contracts (funding_rate, open_interest),
    use 'swap' type to ensure derivative data is available.
    """
    exchange_cls = getattr(ccxt, exchange_id)
    exchange = exchange_cls({"enableRateLimit": True, "timeout": 12000})
    exchange.options["adjustForTimeDifference"] = True
    
    if exchange_id in {"binance", "okx", "bybit"}:
        exchange.options["defaultType"] = "swap"
    elif exchange_id in {"bitget", "gate"}:
        exchange.options["defaultType"] = "spot"
    
    return exchange


def _market_data_exchange_ids() -> list[str]:
    """Use the configured exchange first, then public fallbacks for market data.
    
    Priority order:
    1. Configured primary exchange (with user credentials if available)
    2. Binance public (best for perpetual contract data: funding_rate, open_interest)
    3. OKX public (good fallback for most pairs)
    4. Bybit public (supports derivatives)
    5. Bitget/Gate/Coinbase (limited derivative data)
    
    Note: If primary exchange is sandbox/demo mode, skip its public API fallback
    to avoid duplicate failures (sandbox often has fewer trading pairs).
    """
    primary = settings.exchange.name
    
    if settings.exchange.sandbox_mode:
        sandbox_exchanges = {"okx", "binance", "bybit"}
        if primary in sandbox_exchanges:
            filtered_fallbacks = [e for e in _PUBLIC_MARKET_DATA_FALLBACKS if e != primary]
            return list(dict.fromkeys(filtered_fallbacks))
    
    return list(dict.fromkeys([primary, *_PUBLIC_MARKET_DATA_FALLBACKS]))


def _get_market_data_exchange(exchange_id: str) -> ccxt.Exchange:
    if exchange_id == settings.exchange.name:
        return _get_exchange()
    return _get_public_market_exchange(exchange_id)


def _candles_to_ohlcv_dicts(candles: list[list[float]]) -> list[dict[str, float | str]]:
    ohlcv_data: list[dict[str, float | str]] = []
    for candle in candles:
        timestamp_ms = candle[0]
        timestamp_dt = datetime.fromtimestamp(timestamp_ms / 1000, tz=timezone.utc)
        ohlcv_data.append({
            "timestamp": timestamp_dt.isoformat(),
            "datetime": timestamp_dt.strftime("%Y-%m-%d %H:%M:%S"),
            "open": float(candle[1]),
            "high": float(candle[2]),
            "low": float(candle[3]),
            "close": float(candle[4]),
            "volume": float(candle[5]),
        })
    ohlcv_data.sort(key=lambda row: str(row["timestamp"]))
    return ohlcv_data


def _empty_market_context(ticker: str) -> MarketContext:
    return MarketContext(
        ticker=ticker,
        current_price=0.0,
        price_change_1h=0.0,
        price_change_4h=0.0,
        price_change_24h=0.0,
        volume_24h=0.0,
        high_24h=0.0,
        low_24h=0.0,
        bid_ask_spread=0.0,
        rsi_1h=None,
        atr_pct=None,
        ema_fast=None,
        ema_slow=None,
        orderbook_imbalance=None,
    )


async def _get_cache_lock(ticker: str) -> asyncio.Lock:
    async with _market_cache_locks_guard:
        lock = _market_cache_locks.get(ticker)
        if lock is None:
            lock = asyncio.Lock()
            _market_cache_locks[ticker] = lock
        return lock


async def fetch_market_context(ticker: str) -> MarketContext:
    """
    Fetch comprehensive market context for AI analysis.

    Priority (Optimization 6):
    1. WebSocket real-time data (if available and fresh)
    2. Cache (if fresh within TTL)
    3. REST API fetch

    Results are cached per ticker for up to 60 s.
    """
    now = time.monotonic()

    # Optimization 6: Check WebSocket data first
    ws_data = _ws_manager.get_latest_data(ticker)
    if ws_data and ws_data.get("price", 0) > 0:
        logger.debug(f"[MarketData] Using WebSocket data for {ticker}")
        # WebSocket data is already updating cache, return from cache
        entry = _market_cache.get(ticker)
        if entry:
            return entry[1]

    lock = await _get_cache_lock(ticker)
    async with lock:
        entry = _market_cache.get(ticker)
        if entry and (now - entry[0]) < _MARKET_CACHE_TTL:
            _market_cache.move_to_end(ticker)
            logger.debug(f"[MarketData] Cache hit for {ticker}")
            return entry[1]

        # Try WebSocket subscription for future updates
        await _ws_manager.subscribe_ticker(ticker)

        context = await _fetch_market_context_live(ticker)

        _market_cache[ticker] = (time.monotonic(), context)
        _market_cache.move_to_end(ticker)
        while len(_market_cache) > _MARKET_CACHE_MAX_SIZE:
            _market_cache.popitem(last=False)

        if len(_market_cache_locks) > _MARKET_CACHE_MAX_SIZE:
            stale_tickers = [t for t in _market_cache_locks if t not in _market_cache]
            for t in stale_tickers:
                _market_cache_locks.pop(t, None)

    return context


async def _fetch_market_context_live(ticker: str) -> MarketContext:
    if not _CCXT_AVAILABLE:
        logger.warning("[MarketData] ccxt is not installed; returning empty market context")
        return _empty_market_context(ticker)

    last_error = None
    for exchange_id in _market_data_exchange_ids():
        try:
            exchange = _get_market_data_exchange(exchange_id)
            symbol = await asyncio.to_thread(_resolve_symbol, exchange, ticker)

            fetch_results = cast(
                tuple[object, object, object, object, object, object],
                await asyncio.gather(
                asyncio.to_thread(exchange.fetch_ticker, symbol),
                asyncio.to_thread(exchange.fetch_ohlcv, symbol, "1h", None, 100),  # P1-4: Extended to 100 candles
                asyncio.to_thread(exchange.fetch_ohlcv, symbol, "4h", None, 100),  # P1-4: Extended to 100 candles
                asyncio.to_thread(exchange.fetch_ohlcv, symbol, "30m", None, 60),  # P1-4: Extended to 60 candles
                asyncio.to_thread(exchange.fetch_ohlcv, symbol, "5m", None, 30),   # P1-4: NEW - 5m for scalping
                asyncio.to_thread(exchange.fetch_order_book, symbol, 20),
                return_exceptions=True,
                ),
            )
            ticker_result, ohlcv_1h_result, ohlcv_4h_result, ohlcv_30m_result, ohlcv_5m_result, orderbook_result = fetch_results

            if isinstance(ticker_result, Exception):
                logger.warning(f"[MarketData] {exchange_id} ticker fetch failed for {ticker}: {ticker_result}")
                raise ticker_result

            ticker_data = cast(dict[str, Any], ticker_result)

            if isinstance(ohlcv_1h_result, Exception):
                logger.warning(f"[MarketData] {exchange_id} 1h OHLCV fetch failed for {ticker}: {ohlcv_1h_result}")
                ohlcv_1h: list[list[float]] = []
            else:
                ohlcv_1h = _clean_ohlcv_data(cast(list[list[Any]], ohlcv_1h_result), 100)  # P2-10: Clean data

            if isinstance(ohlcv_4h_result, Exception):
                logger.warning(f"[MarketData] {exchange_id} 4h OHLCV fetch failed for {ticker}: {ohlcv_4h_result}")
                ohlcv_4h: list[list[float]] = []
            else:
                ohlcv_4h = _clean_ohlcv_data(cast(list[list[Any]], ohlcv_4h_result), 100)  # P2-10: Clean data

            if isinstance(ohlcv_30m_result, Exception):
                logger.warning(f"[MarketData] {exchange_id} 30m OHLCV fetch failed for {ticker}: {ohlcv_30m_result}")
                ohlcv_30m: list[list[float]] = []
            else:
                ohlcv_30m = _clean_ohlcv_data(cast(list[list[Any]], ohlcv_30m_result), 60)  # P2-10: Clean data

            if isinstance(ohlcv_5m_result, Exception):
                logger.warning(f"[MarketData] {exchange_id} 5m OHLCV fetch failed for {ticker}: {ohlcv_5m_result}")
                ohlcv_5m: list[list[float]] = []
            else:
                ohlcv_5m = _clean_ohlcv_data(cast(list[list[Any]], ohlcv_5m_result), 30)  # P2-10: Clean data

            if isinstance(orderbook_result, Exception):
                logger.warning(f"[MarketData] {exchange_id} orderbook fetch failed for {ticker}: {orderbook_result}")
                orderbook: dict[str, list[list[float]]] = {"bids": [], "asks": []}
            else:
                orderbook = cast(dict[str, list[list[float]]], orderbook_result)

            current_price = _to_float(ticker_data.get("last"), 0.0)

            if current_price <= 0:
                logger.warning(f"[MarketData] {exchange_id} returned zero price for {ticker}")
                raise ValueError("Zero price from exchange")

            price_1h_ago = float(ohlcv_1h[-2][4]) if len(ohlcv_1h) >= 2 else current_price
            price_4h_ago = float(ohlcv_4h[-2][4]) if len(ohlcv_4h) >= 2 else current_price

            price_change_1h = ((current_price - price_1h_ago) / price_1h_ago * 100) if price_1h_ago else 0.0
            price_change_4h = ((current_price - price_4h_ago) / price_4h_ago * 100) if price_4h_ago else 0.0

            volumes = [c[5] for c in ohlcv_1h[-24:]]
            avg_volume = sum(volumes) / len(volumes) if volumes else 1.0
            current_volume = volumes[-1] if volumes else 0.0
            volume_change_pct = ((current_volume - avg_volume) / avg_volume * 100) if avg_volume else 0.0

            total_bids = sum(b[1] for b in orderbook["bids"][:10])
            total_asks = sum(a[1] for a in orderbook["asks"][:10])
            ob_imbalance = (total_bids / total_asks) if total_asks > 0 else 1.0

            best_bid = orderbook["bids"][0][0] if orderbook["bids"] else current_price
            best_ask = orderbook["asks"][0][0] if orderbook["asks"] else current_price
            spread = ((best_ask - best_bid) / current_price * 100) if current_price else 0.0

            rsi = _calculate_rsi([c[4] for c in ohlcv_1h], 14)
            atr = _calculate_atr(ohlcv_1h, 14)
            atr_pct = (atr / current_price * 100) if current_price and atr is not None else 0.0

            closes = [c[4] for c in ohlcv_1h]
            ema_fast = _calculate_ema(closes, 8)
            ema_slow = _calculate_ema(closes, 21)

            funding_rate, open_interest_data, long_short_ratio = await asyncio.gather(
                _safe_fetch_funding_rate(exchange, symbol),
                _safe_fetch_open_interest(exchange, symbol),
                _safe_fetch_long_short_ratio(exchange, symbol),
            )
            open_interest, open_interest_change_pct = open_interest_data

            context = MarketContext(
                ticker=ticker,
                current_price=current_price,
                price_change_1h=round(price_change_1h, 4),
                price_change_4h=round(price_change_4h, 4),
                price_change_24h=_to_float(ticker_data.get("percentage"), 0.0),
                volume_24h=_to_float(ticker_data.get("quoteVolume"), 0.0),
                volume_change_pct=round(volume_change_pct, 2),
                high_24h=_to_float(ticker_data.get("high"), 0.0),
                low_24h=_to_float(ticker_data.get("low"), 0.0),
                bid_ask_spread=round(spread, 6),
                funding_rate=funding_rate,
                open_interest=open_interest,
                open_interest_change_pct=round(open_interest_change_pct, 2) if open_interest_change_pct else None,
                rsi_1h=round(rsi, 2) if rsi else None,
                atr_pct=round(atr_pct, 4) if atr else None,
                ema_fast=round(ema_fast, 2) if ema_fast else None,
                ema_slow=round(ema_slow, 2) if ema_slow else None,
                orderbook_imbalance=round(ob_imbalance, 4),
                long_short_ratio=long_short_ratio,
            )

            ohlcv_15m = await _safe_fetch_ohlcv(exchange, symbol, "15m", 50)
            context_any = cast(Any, context)
            context_any._ohlcv_15m = ohlcv_15m
            context_any._ohlcv_30m = ohlcv_30m
            context_any._ohlcv_5m = ohlcv_5m
            context_any._ohlcv_1h = ohlcv_1h
            context_any._ohlcv_4h = ohlcv_4h
            context_any._market_data_source = exchange_id
            context_any._entry_exit_indicators = build_entry_exit_indicator_context(
                ohlcv_1h=ohlcv_1h,
                ohlcv_15m=ohlcv_15m,
                ohlcv_5m=ohlcv_5m,
            )

            logger.info(f"[MarketData] Fetched context for {ticker} via {exchange_id}: price={current_price}, RSI={rsi}, ATR%={atr_pct}")
            return context

        except Exception as e:
            last_error = e
            logger.warning(f"[MarketData] {exchange_id} market context unavailable for {ticker}: {e}")

    from commodity_data import fetch_commodity_market_context, is_special_commodity

    commodity_type = is_special_commodity(ticker)
    if commodity_type:
        logger.info(f"[MarketData] Detected {commodity_type} ticker {ticker}, trying Yahoo Finance fallback")
        commodity_context = await fetch_commodity_market_context(ticker)
        if commodity_context and commodity_context.current_price > 0:
            context_any = cast(Any, commodity_context)
            context_any._market_data_source = "yfinance"
            context_any._commodity_type = commodity_type
            return commodity_context

    logger.error(f"[MarketData] Failed to fetch context for {ticker} from all market data sources: {last_error}")
    return _empty_market_context(ticker)


async def _safe_fetch_funding_rate(exchange: Any, symbol: str) -> float | None:
    try:
        funding = await asyncio.to_thread(exchange.fetch_funding_rate, symbol)
        if isinstance(funding, dict):
            return _to_optional_float(funding.get("fundingRate"))
    except (OSError, ConnectionError, TimeoutError):
        return None
    except Exception:
        return None
    return None


async def _safe_fetch_open_interest(exchange: Any, symbol: str) -> tuple[float | None, float | None]:
    try:
        oi_data = await asyncio.to_thread(exchange.fetch_open_interest_history, symbol, "1h", None, 2)
        if oi_data and len(oi_data) >= 2:
            latest = cast(dict[str, Any], oi_data[-1])
            previous = cast(dict[str, Any], oi_data[-2])
            oi = _to_optional_float(latest.get("openInterestAmount"))
            prev_oi = _to_optional_float(previous.get("openInterestAmount"))
            if oi is None:
                return None, None
            if prev_oi is not None and prev_oi > 0:
                oi_change = ((oi - prev_oi) / prev_oi) * 100
                return oi, oi_change
            return oi, None
    except (OSError, ConnectionError, TimeoutError):
        pass
    except Exception:
        pass
    return None, None


async def _safe_fetch_long_short_ratio(exchange: Any, symbol: str) -> float | None:
    try:
        if hasattr(exchange, 'fetch_long_short_ratio'):
            ratio_data = await asyncio.to_thread(exchange.fetch_long_short_ratio, symbol)
            if isinstance(ratio_data, dict):
                return _to_optional_float(ratio_data.get("longShortRatio"))
    except (OSError, ConnectionError, TimeoutError):
        pass
    except Exception:
        pass
    return None


async def _safe_fetch_ohlcv(exchange: Any, symbol: str, timeframe: str, limit: int) -> list[list[float]]:
    """Fetch OHLCV data with error handling.

    P2-10: Apply data cleaning before returning.
    """
    try:
        raw_ohlcv = await asyncio.to_thread(exchange.fetch_ohlcv, symbol, timeframe, None, limit)
        return _clean_ohlcv_data(cast(list[list[Any]], raw_ohlcv), limit)  # P2-10: Clean data
    except (OSError, ConnectionError, TimeoutError):
        return []
    except Exception:
        return []


def _normalize_symbol(ticker: str, market_type: str | None = None) -> str:
    """Convert TradingView ticker to ccxt symbol format.

    ENHANCED: Handle .P perpetual contract tickers properly.
    """
    # BTCUSDT -> BTC/USDT
    ticker = ticker.upper().replace(" ", "")

    # ENHANCED: Detect perpetual contract
    is_perpetual = ticker.endswith(".P") or ticker.endswith("PERP")

    for suffix in (".P", "PERP"):
        if ticker.endswith(suffix):
            ticker = ticker[:-len(suffix)]
            break

# Prefer contract format for .P tickers
    prefer_contract = is_perpetual or str(market_type or "").lower() == "contract"

    for quote in ["USDT", "BUSD", "USDC", "USD"]:
        if ticker.endswith(quote):
            base = ticker[: -len(quote)]
            pair_symbol = f"{base}/{quote}"
            if prefer_contract:
                return f"{pair_symbol}:{quote}"
            return pair_symbol

    # Fallback: Add USDT pair
    pair_symbol = f"{ticker}/USDT"
    if prefer_contract:
        return f"{pair_symbol}:USDT"
    return pair_symbol


def _calculate_rsi(closes: list[float], period: int = 14) -> float | None:
    """Calculate RSI from a list of close prices."""
    if len(closes) < period + 1:
        return None
    deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    gains = [d if d > 0 else 0 for d in deltas]
    losses = [-d if d < 0 else 0 for d in deltas]

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period

    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def _calculate_atr(ohlcv: list[list[float]], period: int = 14) -> float | None:
    """Calculate ATR from OHLCV data.

    ENHANCED: Fallback calculation when OHLCV data is insufficient.
    - If len(ohlcv) < period + 1, use available data (min 7 candles)
    - If len(ohlcv) < 7, return None (insufficient for any estimate)
    """
    # Standard calculation with sufficient data
    if len(ohlcv) >= period + 1:
        trs: list[float] = []
        for i in range(1, len(ohlcv)):
            high, low, prev_close = ohlcv[i][2], ohlcv[i][3], ohlcv[i - 1][4]
            tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
            trs.append(tr)
        if len(trs) < period:
            return None
        atr = sum(trs[:period]) / period
        for i in range(period, len(trs)):
            atr = (atr * (period - 1) + trs[i]) / period
        return atr

    # ENHANCED: Fallback with fewer candles (minimum 7)
    min_candles = 7
    if len(ohlcv) >= min_candles:
        logger.debug(f"[MarketData] ATR fallback: using {len(ohlcv)} candles (requested {period + 1})")
        trs: list[float] = []
        for i in range(1, len(ohlcv)):
            high, low, prev_close = ohlcv[i][2], ohlcv[i][3], ohlcv[i - 1][4]
            tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
            trs.append(tr)

        # Calculate ATR with available data
        fallback_period = len(trs)
        atr = sum(trs) / fallback_period
        return atr

    # Insufficient data even for fallback
    return None


def _calculate_ema(data: list[float], period: int) -> float | None:
    """Calculate EMA from a list of values."""
    if len(data) < period:
        return None
    multiplier = 2 / (period + 1)
    ema = sum(data[:period]) / period
    for value in data[period:]:
        ema = (value - ema) * multiplier + ema
    return ema


async def fetch_correlated_assets_context() -> dict:
    """
    Fetch market context for correlated assets (BTC, ETH).
    Used by pre-filter Check 18 (Correlated Assets).
    """
    correlated: dict[str, float] = {}

    try:
        exchange = _get_exchange()

        correlated_results = cast(
            tuple[object, object, object, object],
            await asyncio.gather(
            asyncio.to_thread(exchange.fetch_ticker, "BTC/USDT"),
            asyncio.to_thread(exchange.fetch_ohlcv, "BTC/USDT", "1h", None, 2),
            asyncio.to_thread(exchange.fetch_ticker, "ETH/USDT"),
            asyncio.to_thread(exchange.fetch_ohlcv, "ETH/USDT", "1h", None, 2),
            return_exceptions=True,
            ),
        )
        btc_ticker, btc_ohlcv, eth_ticker, eth_ohlcv = correlated_results

        if not isinstance(btc_ticker, Exception) and not isinstance(btc_ohlcv, Exception):
            btc_ticker_data = cast(dict[str, Any], btc_ticker)
            btc_ohlcv_data = cast(list[list[float]], btc_ohlcv)
            btc_price = _to_float(btc_ticker_data.get("last"), 0.0)
            btc_1h_ago = float(btc_ohlcv_data[-2][4]) if len(btc_ohlcv_data) >= 2 else btc_price
            btc_change = ((btc_price - btc_1h_ago) / btc_1h_ago * 100) if btc_1h_ago else 0.0
            correlated["BTC_change_1h"] = round(btc_change, 2)

        if not isinstance(eth_ticker, Exception) and not isinstance(eth_ohlcv, Exception):
            eth_ticker_data = cast(dict[str, Any], eth_ticker)
            eth_ohlcv_data = cast(list[list[float]], eth_ohlcv)
            eth_price = _to_float(eth_ticker_data.get("last"), 0.0)
            eth_1h_ago = float(eth_ohlcv_data[-2][4]) if len(eth_ohlcv_data) >= 2 else eth_price
            eth_change = ((eth_price - eth_1h_ago) / eth_1h_ago * 100) if eth_1h_ago else 0.0
            correlated["ETH_change_1h"] = round(eth_change, 2)

    except Exception as e:
        logger.debug(f"[MarketData] Correlated assets context fetch failed: {e}")

    return correlated


async def fetch_whale_activity(ticker: str) -> dict[str, Any]:
    """
    Fetch whale activity data from multiple FREE sources.

    Sources:
    1. Blockchain explorers (Etherscan, Blockchain.com)
    2. Exchange public data (top trader long/short ratio)
    3. Whale Alert (free tier: 500 requests/day)

    Returns large transfer counts and net flow estimates.
    """
    whale_data: dict[str, Any] = {}

    symbol = ticker.upper().replace("USDT", "").replace("USD", "")

    # ── Source 1: Exchange Public Data (via CCXT) ──
    try:
        exchange = _get_exchange()
        ccxt_symbol = await asyncio.to_thread(_resolve_symbol, exchange, ticker)

        # Try to get top trader long/short ratio (Binance/Bybit public data)
        if hasattr(exchange, 'fapiPublicGetToptraderlongshortratio'):
            try:
                ratio_data = await asyncio.to_thread(
                    exchange.fapiPublicGetToptraderlongshortratio,
                    {'symbol': ccxt_symbol, 'period': '1h'}
                )
                if ratio_data:
                    top_long_ratio = float(ratio_data[0].get('longShortRatio', 1.0))
                    whale_data['top_trader_long_ratio'] = top_long_ratio

                    # Interpretation: ratio > 1.2 = heavy long bias (whales bullish)
                    # ratio < 0.8 = heavy short bias (whales bearish)
                    if top_long_ratio > 1.5:
                        whale_data['whale_sentiment'] = 'bullish'
                        whale_data['large_transfers_1h'] = 1
                    elif top_long_ratio < 0.67:
                        whale_data['whale_sentiment'] = 'bearish'
                        whale_data['large_transfers_1h'] = 1
            except Exception as e:
                logger.debug(f"[Whale] Top trader ratio fetch failed: {e}")

        # Try to get open interest history for flow analysis
        try:
            oi_history = await asyncio.to_thread(
                exchange.fetch_open_interest_history,
                ccxt_symbol, '1h', None, 24
            )
            if oi_history and len(oi_history) >= 12:
                recent_oi = oi_history[-1].get('openInterestAmount', 0)
                older_oi = oi_history[-12].get('openInterestAmount', 0)

                if older_oi > 0:
                    oi_change = (recent_oi - older_oi) / older_oi * 100

                    # Large OI increase = capital inflow
                    if oi_change > 5:
                        whale_data['net_flow_24h'] = recent_oi * 0.1  # Estimate
                        whale_data['flow_direction'] = 'inflow'
                    elif oi_change < -5:
                        whale_data['net_flow_24h'] = -recent_oi * 0.1
                        whale_data['flow_direction'] = 'outflow'
        except Exception as e:
            logger.debug(f"[Whale] OI history fetch failed: {e}")
    except Exception as e:
        logger.debug(f"[Whale] Exchange data fetch failed: {e}")

    # ── Source 2: Blockchain Explorer (FREE) ──
    try:
        if symbol in ['BTC', 'ETH', 'USDT']:
            blockchain_whale_data = await _fetch_blockchain_whale_data(symbol)
            if blockchain_whale_data:
                whale_data.update(blockchain_whale_data)
    except Exception as e:
        logger.debug(f"[Whale] Blockchain data fetch failed: {e}")

    # ── Source 3: Whale Alert API (FREE tier: 500/day) ──
    whale_api_key = get_secure_api_key("whale_alert_api_key")
    if whale_api_key:
        try:
            api_whale_data = await _fetch_whale_alert_api(whale_api_key, symbol)
            if api_whale_data:
                whale_data.update(api_whale_data)
        except Exception as e:
            logger.debug(f"[Whale] Whale Alert API failed: {e}")

    # ── Default values if no data found ──
    if 'large_transfers_1h' not in whale_data:
        whale_data['large_transfers_1h'] = 0
    if 'net_flow_24h' not in whale_data:
        whale_data['net_flow_24h'] = 0

    return whale_data


async def _fetch_blockchain_whale_data(symbol: str) -> dict[str, Any]:
    """
    Fetch large transaction data from blockchain explorers (FREE).

    Supported: BTC (Blockchain.com), ETH/USDT (Etherscan)
    """
    data: dict[str, Any] = {}
    threshold_usd = 1_000_000  # $1M threshold for "whale"

    try:
        import httpx

        async with httpx.AsyncClient(timeout=10.0) as client:
            # BTC: Use Blockchain.com API (FREE, no key required)
            if symbol == 'BTC':
                try:
                    resp = await client.get('https://blockchain.com/q/latesthash')
                    if resp.status_code == 200:
                        # Get recent blocks and check for large outputs
                        block_resp = await client.get(
                            f'https://blockchain.info/block/{resp.text}?format=json'
                        )
                        if block_resp.status_code == 200:
                            block_data = block_resp.json()
                            txs = block_data.get('tx', [])

                            large_count = 0
                            for tx in txs[:50]:  # Check first 50 txs
                                for out in tx.get('out', []):
                                    value_btc = out.get('value', 0) / 100_000_000  # Satoshis to BTC
                                    if value_btc > 50:  # >50 BTC = whale
                                        large_count += 1

                            if large_count > 0:
                                data['btc_large_tx_in_block'] = large_count
                                data['large_transfers_1h'] = max(data.get('large_transfers_1h', 0), large_count)
                except Exception as e:
                    logger.debug(f"[Whale/Blockchain] BTC data fetch failed: {e}")

            # ETH/USDT: Use Etherscan API (FREE tier: 5 calls/sec)
            if symbol in ['ETH', 'USDT']:
                etherscan_api_key = get_secure_api_key("etherscan_api_key") or os.getenv('ETHERSCAN_API_KEY', '')

                # Check ETH whale transfers using known exchange wallets
                if symbol == 'ETH' and etherscan_api_key:
                    known_whale_wallets = [
                        '0x28C6c06298d514Db089934071355E5743bf21d60',  # Binance Hot Wallet
                        '0x21a31Ee1afC51d94C2eFcCAa2092aD102828A4A2',  # Binance 2
                        '0x503828976D22510aad0201ac7EC88293211D23Da',  # Binance 3
                        '0x3f5CE5FBFe3E9af3971dD833D26BA9B5C936f0be',  # Binance 4
                        '0xBE0eB53F46cd7F98683ae9893d006c1cd29B6B93',  # Coinbase
                        '0xA9D1e08C7793af67eBd7fb3a9c32Bb95D88D4737',  # Kraken
                        '0x71C7656EC7ab88b098defB751B7401B5f6d8976F',  # Bitfinex
                    ]
                    try:
                        for wallet in known_whale_wallets[:3]:
                            resp = await client.get(
                                'https://api.etherscan.io/api',
                                params={
                                    'module': 'account',
                                    'action': 'txlist',
                                    'address': wallet,
                                    'startblock': '0',
                                    'endblock': '99999999',
                                    'page': 1,
                                    'offset': 25,
                                    'sort': 'desc',
                                    'apikey': etherscan_api_key,
                                },
                                timeout=5.0
                            )
                            if resp.status_code == 200:
                                result = resp.json().get('result', [])
                                if isinstance(result, list):
                                    for tx in result:
                                        timestamp = int(tx.get('timeStamp', 0))
                                        age_hours = (time.time() - timestamp) / 3600
                                        if age_hours < 1:
                                            value = int(tx.get('value', 0)) / 1e18
                                            if value > threshold_usd / 2000:  # ETH price ~$2000-3000
                                                data['eth_large_transfers_1h'] = data.get('eth_large_transfers_1h', 0) + 1
                                                data['large_transfers_1h'] = data.get('large_transfers_1h', 0) + 1
                    except Exception as e:
                        logger.debug(f"[Whale/Blockchain] ETH whale wallets fetch failed: {e}")

                # Check USDT contract for large transfers
                if symbol == 'USDT':
                    usdt_contract = '0xdAC17F958D2ee523a2206206994597C13D831ec7'
                    try:
                        resp = await client.get(
                            'https://api.etherscan.io/api',
                            params={
                                'module': 'account',
                                'action': 'tokentx',
                                'contractaddress': usdt_contract,
                                'page': 1,
                                'offset': 100,
                                'sort': 'desc',
                                'apikey': etherscan_api_key,
                            },
                            timeout=5.0
                        )
                        if resp.status_code == 200:
                            result = resp.json().get('result', [])
                            if isinstance(result, list):
                                large_count = 0
                                for tx in result:
                                    timestamp = int(tx.get('timeStamp', 0))
                                    age_hours = (time.time() - timestamp) / 3600
                                    if age_hours < 1:
                                        value = int(tx.get('value', 0)) / 1_000_000
                                        if value > threshold_usd:
                                            large_count += 1
                                if large_count > 0:
                                    data['usdt_large_transfers_1h'] = large_count
                                    data['large_transfers_1h'] = max(data.get('large_transfers_1h', 0), large_count)
                    except Exception as e:
                        logger.debug(f"[Whale/Blockchain] USDT contract fetch failed: {e}")
    except Exception as e:
        logger.debug(f"[Whale/Blockchain] Failed: {e}")

    return data


async def _fetch_whale_alert_api(api_key: str, symbol: str) -> dict[str, Any]:
    """
    Fetch whale data from Whale Alert API (FREE: 500 requests/day).

    Requires: WHALE_ALERT_API_KEY in env
    """
    data: dict[str, Any] = {}

    whale_threshold_usd = float(os.getenv("WHALE_THRESHOLD_USD", "1000000"))
    min_value = int(whale_threshold_usd * 0.5)  # Use half threshold for API

    try:
        import httpx

        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                'https://api.whale-alert.io/v1/transactions',
                params={
                    'api_key': api_key,
                    'currency': symbol,
                    'min_value': min_value,
                    'limit': 20,
                }
            )

            if resp.status_code == 200:
                api_data = resp.json()
                if not isinstance(api_data, dict):
                    return data
                transactions = api_data.get('transactions', [])
                if not isinstance(transactions, list):
                    return data

                large_transfers_1h = 0
                net_flow_24h = 0.0

                for tx in transactions:
                    if not isinstance(tx, dict):
                        continue
                    tx_time = tx.get('timestamp', 0)
                    tx_age_hours = (time.time() - _to_float(tx_time, 0.0)) / 3600

                    if tx_age_hours < 1:
                        large_transfers_1h += 1

                    if tx_age_hours < 24:
                        amount_usd = _to_float(tx.get('amount_usd'), 0.0)
                        from_info = tx.get('from', {})
                        to_info = tx.get('to', {})
                        from_type = from_info.get('owner_type', '') if isinstance(from_info, dict) else ''
                        to_type = to_info.get('owner_type', '') if isinstance(to_info, dict) else ''

                        # Exchange inflow/outflow tracking
                        if from_type == 'unknown' and to_type == 'exchange':
                            net_flow_24h += amount_usd  # Depositing to exchange = potential sell
                        elif from_type == 'exchange' and to_type == 'unknown':
                            net_flow_24h -= amount_usd  # Withdrawal from exchange = potential hold

                data['large_transfers_1h'] = large_transfers_1h
                data['net_flow_24h'] = net_flow_24h
                data['whale_alert_source'] = True
    except Exception as e:
        logger.debug(f"[Whale/API] Whale Alert failed: {e}")

    return data


async def fetch_enhanced_market_context(ticker: str) -> MarketContext:
    """
    Fetch comprehensive market context including correlated assets and whale activity.
    """
    gathered = cast(
        tuple[MarketContext, dict[str, float], dict[str, Any]],
        await asyncio.gather(
            fetch_market_context(ticker),
            fetch_correlated_assets_context(),
            fetch_whale_activity(ticker),
        ),
    )
    context, correlated, whale = gathered

    context_any = cast(Any, context)
    if correlated:
        context_any._correlated_assets = correlated
    if whale:
        context_any._whale_activity = whale

    return context


async def fetch_ohlcv_history(
    ticker: str,
    timeframe: str = "1h",
    days: int = 30,
    exchange_config: dict | None = None,
) -> list[dict]:
    """
    Fetch historical OHLCV data for backtesting.

    Args:
        ticker: Trading pair symbol (e.g., BTCUSDT)
        timeframe: Timeframe (1m, 5m, 15m, 1h, 4h, 1d)
        days: Number of days of history to fetch
        exchange_config: Optional exchange configuration

    Returns:
        List of OHLCV candles with timestamp
    """
    if not _CCXT_AVAILABLE:
        logger.warning("[MarketData] ccxt is not installed; OHLCV history unavailable")
        return []

    timeframe_minutes = {
        "1m": 1,
        "5m": 5,
        "15m": 15,
        "1h": 60,
        "4h": 240,
        "1d": 1440,
    }
    minutes = timeframe_minutes.get(timeframe, 60)
    bars_needed = max(1, (days * 24 * 60) // minutes)

    # Most public exchange endpoints cap a single OHLCV call. A recent window is
    # enough for the dashboard chart and avoids broken pseudo-pagination.
    limit = min(bars_needed, 1000)
    last_error = None

    for exchange_id in _market_data_exchange_ids():
        try:
            exchange = _get_market_data_exchange(exchange_id)
            symbol = await asyncio.to_thread(_resolve_symbol, exchange, ticker)
            candles = await asyncio.to_thread(exchange.fetch_ohlcv, symbol, timeframe, None, limit)
            if not candles:
                logger.warning(f"[MarketData] {exchange_id} returned no OHLCV candles for {ticker}")
                continue
            ohlcv_data = _candles_to_ohlcv_dicts(candles)
            logger.info(
                f"[MarketData] Fetched {len(ohlcv_data)} {timeframe} bars for {ticker} "
                f"over {days} days via {exchange_id}"
            )
            return ohlcv_data
        except Exception as e:
            last_error = e
            logger.warning(f"[MarketData] {exchange_id} OHLCV unavailable for {ticker}: {e}")

    logger.error(f"[MarketData] Failed to fetch OHLCV history for {ticker} from all market data sources: {last_error}")
    return []
