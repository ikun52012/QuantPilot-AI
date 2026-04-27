"""
QuantPilot AI - Market Data Fetcher
Fetches real-time market data from the exchange via ccxt.
"""
import asyncio
import os
import time
from collections import OrderedDict
from datetime import datetime, timezone
from typing import Optional
from loguru import logger
from core.config import settings
from models import MarketContext
from exchange import _CCXT_AVAILABLE, ccxt, _get_or_create_exchange, _resolve_symbol

_MARKET_CACHE_TTL = 30
_MARKET_CACHE_MAX_SIZE = 500
_PUBLIC_MARKET_DATA_FALLBACKS = ("okx", "bitget", "gate", "coinbase")
_market_cache: OrderedDict[str, tuple[float, MarketContext]] = OrderedDict()
_market_cache_locks: dict[str, asyncio.Lock] = {}
_market_cache_locks_guard = asyncio.Lock()


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
        sandbox=False,
    )


def _get_public_market_exchange(exchange_id: str) -> ccxt.Exchange:
    """Create a public-only market-data exchange without user credentials."""
    exchange_cls = getattr(ccxt, exchange_id)
    exchange = exchange_cls({"enableRateLimit": True, "timeout": 12000})
    exchange.options["adjustForTimeDifference"] = True
    if exchange_id in {"okx", "bybit", "bitget", "gate"}:
        exchange.options["defaultType"] = "spot"
    return exchange


def _market_data_exchange_ids() -> list[str]:
    """Use the configured exchange first, then public fallbacks for market data."""
    return list(dict.fromkeys([settings.exchange.name, *_PUBLIC_MARKET_DATA_FALLBACKS]))


def _get_market_data_exchange(exchange_id: str) -> ccxt.Exchange:
    if exchange_id == settings.exchange.name:
        return _get_exchange()
    return _get_public_market_exchange(exchange_id)


def _candles_to_ohlcv_dicts(candles: list) -> list[dict]:
    ohlcv_data = []
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
    ohlcv_data.sort(key=lambda x: x.get("timestamp", ""))
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
    Results are cached per ticker for up to 30 s to avoid hammering the exchange
    on rapid consecutive signals for the same symbol.
    """
    now = time.monotonic()

    lock = await _get_cache_lock(ticker)
    async with lock:
        entry = _market_cache.get(ticker)
        if entry and (now - entry[0]) < _MARKET_CACHE_TTL:
            _market_cache.move_to_end(ticker)
            logger.debug(f"[MarketData] Cache hit for {ticker}")
            return entry[1]

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

            ticker_data, ohlcv_1h, ohlcv_4h, orderbook = await asyncio.gather(
                asyncio.to_thread(exchange.fetch_ticker, symbol),
                asyncio.to_thread(exchange.fetch_ohlcv, symbol, "1h", None, 30),
                asyncio.to_thread(exchange.fetch_ohlcv, symbol, "4h", None, 10),
                asyncio.to_thread(exchange.fetch_order_book, symbol, 20),
            )

            current_price = ticker_data.get("last", 0.0)
            price_1h_ago = ohlcv_1h[-2][4] if len(ohlcv_1h) >= 2 else current_price
            price_4h_ago = ohlcv_4h[-2][4] if len(ohlcv_4h) >= 2 else current_price

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
            atr_pct = (atr / current_price * 100) if current_price else 0.0

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
                price_change_24h=ticker_data.get("percentage", 0.0) or 0.0,
                volume_24h=ticker_data.get("quoteVolume", 0.0) or 0.0,
                volume_change_pct=round(volume_change_pct, 2),
                high_24h=ticker_data.get("high", 0.0) or 0.0,
                low_24h=ticker_data.get("low", 0.0) or 0.0,
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
            context._ohlcv_15m = ohlcv_15m
            context._ohlcv_1h = ohlcv_1h
            context._ohlcv_4h = ohlcv_4h
            context._market_data_source = exchange_id

            logger.info(f"[MarketData] Fetched context for {ticker} via {exchange_id}: price={current_price}, RSI={rsi}, ATR%={atr_pct}")
            return context

        except Exception as e:
            last_error = e
            logger.warning(f"[MarketData] {exchange_id} market context unavailable for {ticker}: {e}")

    logger.error(f"[MarketData] Failed to fetch context for {ticker} from all market data sources: {last_error}")
    return MarketContext(ticker=ticker, current_price=0.0)


async def _safe_fetch_funding_rate(exchange, symbol) -> Optional[float]:
    try:
        funding = await asyncio.to_thread(exchange.fetch_funding_rate, symbol)
        return funding.get("fundingRate")
    except Exception:
        return None


async def _safe_fetch_open_interest(exchange, symbol) -> tuple[Optional[float], Optional[float]]:
    try:
        oi_data = await asyncio.to_thread(exchange.fetch_open_interest_history, symbol, "1h", None, 2)
        if oi_data and len(oi_data) >= 2:
            oi = oi_data[-1].get("openInterestAmount", 0)
            prev_oi = oi_data[-2].get("openInterestAmount", 0)
            if prev_oi > 0:
                oi_change = ((oi - prev_oi) / prev_oi) * 100
                return oi, oi_change
            return oi, None
    except Exception:
        pass
    return None, None


async def _safe_fetch_long_short_ratio(exchange, symbol) -> Optional[float]:
    try:
        if hasattr(exchange, 'fetch_long_short_ratio'):
            ratio_data = await asyncio.to_thread(exchange.fetch_long_short_ratio, symbol)
            return ratio_data.get("longShortRatio")
    except Exception:
        pass
    return None


async def _safe_fetch_ohlcv(exchange, symbol, timeframe, limit) -> list:
    try:
        return await asyncio.to_thread(exchange.fetch_ohlcv, symbol, timeframe, None, limit)
    except Exception:
        return []


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


async def fetch_correlated_assets_context() -> dict:
    """
    Fetch market context for correlated assets (BTC, ETH).
    Used by pre-filter Check 18 (Correlated Assets).
    """
    correlated = {}

    try:
        exchange = _get_exchange()

        btc_ticker, btc_ohlcv, eth_ticker, eth_ohlcv = await asyncio.gather(
            asyncio.to_thread(exchange.fetch_ticker, "BTC/USDT"),
            asyncio.to_thread(exchange.fetch_ohlcv, "BTC/USDT", "1h", None, 2),
            asyncio.to_thread(exchange.fetch_ticker, "ETH/USDT"),
            asyncio.to_thread(exchange.fetch_ohlcv, "ETH/USDT", "1h", None, 2),
            return_exceptions=True,
        )

        if not isinstance(btc_ticker, Exception) and not isinstance(btc_ohlcv, Exception):
            btc_price = btc_ticker.get("last", 0.0)
            btc_1h_ago = btc_ohlcv[-2][4] if isinstance(btc_ohlcv, list) and len(btc_ohlcv) >= 2 else btc_price
            btc_change = ((btc_price - btc_1h_ago) / btc_1h_ago * 100) if btc_1h_ago else 0.0
            correlated["BTC_change_1h"] = round(btc_change, 2)

        if not isinstance(eth_ticker, Exception) and not isinstance(eth_ohlcv, Exception):
            eth_price = eth_ticker.get("last", 0.0)
            eth_1h_ago = eth_ohlcv[-2][4] if isinstance(eth_ohlcv, list) and len(eth_ohlcv) >= 2 else eth_price
            eth_change = ((eth_price - eth_1h_ago) / eth_1h_ago * 100) if eth_1h_ago else 0.0
            correlated["ETH_change_1h"] = round(eth_change, 2)

    except Exception as e:
        logger.debug(f"[MarketData] Correlated assets context fetch failed: {e}")

    return correlated


async def fetch_whale_activity(ticker: str) -> dict:
    """
    Fetch whale activity data from multiple FREE sources.

    Sources:
    1. Blockchain explorers (Etherscan, Blockchain.com)
    2. Exchange public data (top trader long/short ratio)
    3. Whale Alert (free tier: 500 requests/day)

    Returns large transfer counts and net flow estimates.
    """
    whale_data = {}

    symbol = ticker.upper().replace("USDT", "").replace("USD", "")

    # Get configurable threshold from admin settings
    from core.security import get_secure_api_key
    whale_threshold_usd = float(get_secure_api_key("whale_threshold_usd") or os.getenv("WHALE_THRESHOLD_USD", "1000000"))  # $1M default

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


async def _fetch_blockchain_whale_data(symbol: str) -> dict:
    """
    Fetch large transaction data from blockchain explorers (FREE).

    Supported: BTC (Blockchain.com), ETH/USDT (Etherscan)
    """
    data = {}
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


async def _fetch_whale_alert_api(api_key: str, symbol: str) -> dict:
    """
    Fetch whale data from Whale Alert API (FREE: 500 requests/day).

    Requires: WHALE_ALERT_API_KEY in env
    """
    data = {}

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
                transactions = api_data.get('transactions', [])

                large_transfers_1h = 0
                net_flow_24h = 0.0

                for tx in transactions:
                    tx_time = tx.get('timestamp', 0)
                    tx_age_hours = (time.time() - tx_time) / 3600

                    if tx_age_hours < 1:
                        large_transfers_1h += 1

                    if tx_age_hours < 24:
                        amount_usd = tx.get('amount_usd', 0)
                        from_type = tx.get('from', {}).get('owner_type', '')
                        to_type = tx.get('to', {}).get('owner_type', '')

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
    context, correlated, whale = await asyncio.gather(
        fetch_market_context(ticker),
        fetch_correlated_assets_context(),
        fetch_whale_activity(ticker),
    )

    if correlated:
        context._correlated_assets = correlated
    if whale:
        context._whale_activity = whale

    return context


async def fetch_ohlcv_history(
    ticker: str,
    timeframe: str = "1h",
    days: int = 30,
    exchange_config: Optional[dict] = None,
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
