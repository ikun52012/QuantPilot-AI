"""
QuantPilot AI - Market Data Fetcher
Fetches real-time market data from the exchange via ccxt.
"""
import asyncio
import inspect
import time
import threading
from collections import OrderedDict
import ccxt
from loguru import logger
from core.config import settings
from models import MarketContext
from exchange import _build_exchange, _resolve_symbol

# 鈹€鈹€鈹€ TTL cache for market context (per ticker) 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€
_MARKET_CACHE_TTL = 30  # seconds
_MARKET_CACHE_MAX_SIZE = 500
_market_cache: OrderedDict[str, tuple[float, MarketContext]] = OrderedDict()
_market_cache_lock = threading.Lock()


def _get_exchange() -> ccxt.Exchange:
    """Create a ccxt exchange instance."""
    return _build_exchange()


async def fetch_market_context(ticker: str) -> MarketContext:
    """
    Fetch comprehensive market context for AI analysis.
    Results are cached per ticker for up to 30 s to avoid hammering the exchange
    on rapid consecutive signals for the same symbol.
    """
    now = time.monotonic()
    with _market_cache_lock:
        entry = _market_cache.get(ticker)
        if entry and (now - entry[0]) < _MARKET_CACHE_TTL:
            _market_cache.move_to_end(ticker)
            logger.debug(f"[MarketData] Cache hit for {ticker}")
            return entry[1]

    context = await _fetch_market_context_live(ticker)

    with _market_cache_lock:
        _market_cache[ticker] = (time.monotonic(), context)
        _market_cache.move_to_end(ticker)
        while len(_market_cache) > _MARKET_CACHE_MAX_SIZE:
            _market_cache.popitem(last=False)
    return context


async def _close_exchange(exchange):
    close = getattr(exchange, "close", None)
    if not close:
        return
    result = await asyncio.to_thread(close)
    if inspect.isawaitable(result):
        await result


async def _fetch_market_context_live(ticker: str) -> MarketContext:
    exchange = _get_exchange()
    symbol = await asyncio.to_thread(_resolve_symbol, exchange, ticker)

    try:
        # CCXT is synchronous here; run calls in a worker thread so webhook
        # processing does not block the FastAPI event loop.
        ticker_data = await asyncio.to_thread(exchange.fetch_ticker, symbol)
        ohlcv_1h = await asyncio.to_thread(exchange.fetch_ohlcv, symbol, "1h", None, 30)
        ohlcv_4h = await asyncio.to_thread(exchange.fetch_ohlcv, symbol, "4h", None, 10)
        orderbook = await asyncio.to_thread(exchange.fetch_order_book, symbol, 20)

        # Calculate price changes
        current_price = ticker_data.get("last", 0.0)
        price_1h_ago = ohlcv_1h[-2][4] if len(ohlcv_1h) >= 2 else current_price
        price_4h_ago = ohlcv_4h[-2][4] if len(ohlcv_4h) >= 2 else current_price

        price_change_1h = ((current_price - price_1h_ago) / price_1h_ago * 100) if price_1h_ago else 0.0
        price_change_4h = ((current_price - price_4h_ago) / price_4h_ago * 100) if price_4h_ago else 0.0

        # Volume analysis
        volumes = [c[5] for c in ohlcv_1h[-24:]]   # last 24 candles
        avg_volume = sum(volumes) / len(volumes) if volumes else 1.0
        current_volume = volumes[-1] if volumes else 0.0
        volume_change_pct = ((current_volume - avg_volume) / avg_volume * 100) if avg_volume else 0.0

        # Orderbook imbalance
        total_bids = sum(b[1] for b in orderbook["bids"][:10])
        total_asks = sum(a[1] for a in orderbook["asks"][:10])
        ob_imbalance = (total_bids / total_asks) if total_asks > 0 else 1.0

        # Bid-ask spread
        best_bid = orderbook["bids"][0][0] if orderbook["bids"] else current_price
        best_ask = orderbook["asks"][0][0] if orderbook["asks"] else current_price
        spread = ((best_ask - best_bid) / current_price * 100) if current_price else 0.0

        # Calculate simple RSI from 1h candles
        rsi = _calculate_rsi([c[4] for c in ohlcv_1h], 14)

        # Calculate ATR %
        atr = _calculate_atr(ohlcv_1h, 14)
        atr_pct = (atr / current_price * 100) if current_price else 0.0

        # EMA fast/slow from 1h candles
        closes = [c[4] for c in ohlcv_1h]
        ema_fast = _calculate_ema(closes, 8)
        ema_slow = _calculate_ema(closes, 21)

        # Try to get funding rate (futures only)
        funding_rate = None
        try:
            funding = await asyncio.to_thread(exchange.fetch_funding_rate, symbol)
            funding_rate = funding.get("fundingRate")
        except Exception:
            pass

        context = MarketContext(
            ticker=ticker,
            current_price=current_price,
            price_change_1h=round(price_change_1h, 4),
            price_change_4h=round(price_change_4h, 4),
            price_change_24h=ticker_data.get("percentage", 0.0) or 0.0,
            volume_24h=ticker_data.get("quoteVolume", 0.0) or 0.0,
            volume_change_pct=round(volume_change_pct, 2),
            high_24h=ticker_data.get("high", 0.0) or 0.0,
            low_24h=ticker_data.get("low", 0.0) or 0.0,
            bid_ask_spread=round(spread, 6),
            funding_rate=funding_rate,
            rsi_1h=round(rsi, 2) if rsi else None,
            atr_pct=round(atr_pct, 4) if atr else None,
            ema_fast=round(ema_fast, 2) if ema_fast else None,
            ema_slow=round(ema_slow, 2) if ema_slow else None,
            orderbook_imbalance=round(ob_imbalance, 4),
        )

        logger.info(f"[MarketData] Fetched context for {ticker}: price={current_price}, RSI={rsi}, ATR%={atr_pct}")
        return context

    except Exception as e:
        logger.error(f"[MarketData] Failed to fetch context for {ticker}: {e}")
        # Return minimal context on failure
        return MarketContext(ticker=ticker, current_price=0.0)
    finally:
        try:
            await _close_exchange(exchange)
        except AttributeError:
            pass


def _normalize_symbol(ticker: str) -> str:
    """Convert TradingView ticker to ccxt symbol format."""
    # BTCUSDT -> BTC/USDT
    ticker = ticker.upper().replace(" ", "")
    for quote in ["USDT", "BUSD", "USDC", "USD"]:
        if ticker.endswith(quote):
            base = ticker[: -len(quote)]
            return f"{base}/{quote}"
    return ticker


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


def _calculate_atr(ohlcv: list, period: int = 14) -> float | None:
    """Calculate ATR from OHLCV data."""
    if len(ohlcv) < period + 1:
        return None
    trs = []
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


def _calculate_ema(data: list[float], period: int) -> float | None:
    """Calculate EMA from a list of values."""
    if len(data) < period:
        return None
    multiplier = 2 / (period + 1)
    ema = sum(data[:period]) / period
    for value in data[period:]:
        ema = (value - ema) * multiplier + ema
    return ema
