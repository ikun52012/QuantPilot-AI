"""
QuantPilot AI - Commodity/Stock Token Market Data Fetcher
Fetches real market data from Yahoo Finance for special instruments like precious metals, oil, and stock tokens.
"""
import asyncio
import re
import time
from collections import OrderedDict
from typing import Any

from loguru import logger

from models import MarketContext

_YFINANCE_CACHE_TTL = 60
_YFINANCE_CACHE_MAX_SIZE = 100
_yfinance_cache: OrderedDict[str, tuple[float, MarketContext]] = OrderedDict()
_yfinance_cache_lock = asyncio.Lock()

_COMMODITY_SYMBOL_MAP = {
    "XAU": "GCUSD",
    "XAG": "SIUSD",
    "XPD": "PAUSD",
    "XPT": "PLUSD",
    "XBR": "BZUSD",
    "XTI": "CLUSD",
    "NG": "NGUSD",
}

_STOCK_TOKEN_PATTERN = re.compile(
    r"^(AAPL|TSLA|GOOGL|AMZN|META|MSFT|NVDA|AMD|INTC|NFLX|DIS|BA|CAT|GE|IBM|JPM|V|MA|WMT|PG|KO|PEP|MRK|ABBV|JNJ|UNH|CVX|XOM|COP|OXY|F|GM|TM|HMC|RACE|NIO|LI|XPEV|BABA|JD|PDD|BIDU|NTES|TME|SHOP|SQ|PYPL|COIN|MSTR|PLTR|SNOW|CRM|WDAY|ZEN|OKTA|DOCU|ZM|SLACK|TEAM|TWLO|DDOG|ESTC|MDB|NET|FSLY|FAST|NOW|SAP|ORCL|ADBE|INTU|PAYX|WDAY|VMW|EA|TTWO|ATVI|TTD|Roku|SPOT|LYFT|UBER|DASH|GRUB|Z|OPEN|REDfin|Zillow|COMP|EXPE|BKNG|MAR|HLT|WH|CCL|RCL|NCLH|LUV|DAL|AAL|UAL|ALK|HA|JBLU|SAVE|SPIR|ALGT|GLNG|GLOG|ZIM|DAC|TRTN|CMRE|SBLK|INSW|NMM|NM|DSX|SB|PANH|PXS|CGBL|GOGL|EGL|JOW|SHIP|TOPS|NEW|OPH|OMEX|DCIX|GNRT|NMNI|CTRV|PRGN|SBFY|SHIP|BULK|KKR|BX|APO|ARES|CG|FIGS|HIMS|ODD|CLOV|HCMA|WISH|OLLI|BIGC|STNE|DADA|YQ|IQ|BILI|TME|HZO|MCFT|BCBP|JOUT|LMNR|CCRN|CWH|SKFY|THMO|AMRK|PRFX|PRTS|BRMK|NSPR|JRVR|PDP|REAX|NOVN|DMRC|PAHC|COWI|ADIL|AIRI|ALNA|AMAR|ARAY|ARCK|ARDM|ASYS|ATIF|ATNF|ATXI|AVCO|AVGR|BBLG|BCDA|BDRX|BETR|BGTI|BHMN|BIOT|BKTP|BLNK|BOLT|BOPO|BOXL|BSET|BTAI|BWVI|BYFC|CAAS|CAJN|CARE|CBAT|CBMG|CCNC|CETX|CHFS|CHNR|CHYK|CJJD|CLPS|CLRB|CMCM|CMGM|COCP|CRBP|CRIS|CRNT|CRRC|CSTA|CTEK|CTIB|CTIX|CTXR|CYNK|DAIO|DGLY|DMMX|DONI|DPSI|DRIO|DSKL|DYSL|EARS|EBIX|ECGI|ECSL|EDAP|EDSA|EDUT|EFOI|EGLX|EHTH|EJFA|EKSO|ELTP|EMCI|EMGF|EMHD|EMMA|EMOM|EMRM|EMXC|ENDI|ENGC|ENJH|ENLV|ENRX|ENSV|ENZN|EODN|EPRF|ERES|ESES|ESGC|ESNT|ETEC|ETHE|EUUR|EVTV|EXEO|EXIS|EYEG|FAMI|FATBR|FBIH|FBLI|FBRX|FDIT|FENG|FFHL|FGBL|FGRO|FHBI|FHTX|FIVG|FNDP|FNHC|FNKO|FNRC|FNWB|FOMX|FORD|FPII|FPNC|FPLI|FPTI|FRGI|FRGT|FRXB|FSCT|FTFT|FTLO|FTVI|FUHY|FWAA|FXNL|FYBR|GBGI|GBLB|GBTS|GEGP|GELI|GENI|GLAC|GLBS|GLOP|GMGI|GNCI|GNCP|GNLX|GNRC|GNRS|GNTI|GNUS|GNSL|GNST|GNTR|GOGO|GOGL|GOLD|GLDG|GLXY|GPAK|GPPR|GRCL|GRMN|GRPH|GRVE|GSAI|GSMG|GSRT|GTCH|GTIP|GTTI|GUOS|GVSI|GWTR|GXGX|HAPP|HBIO|HBRW|HDSI|HENC|HEPA|HEPP|HERA|HERO|HGLW|HHGI|HLIO|HLKY|HMGH|HMHA|HMHC|HMNF|HOFV|HOFH|HOGC|HOOB|HQQQ|HRBR|HRII|HRKN|HRTX|HSDT|HSMR|HSSN|HSUN|HTEK|HTGM|HUIZ|HUSA|HVBC|HVLM|HVMC|HWEL|HYBT|HYDR|HYFM|HYGO|HYIIP|HYLN|HYMC|HYMT|HYNO|HYPT|HYRN|HYRO|HYRV|HYSR|HYTK|HYTT|HYWM|HZNO|IBGR|IBIO|IBLR|IBRX|ICBU|ICCJ|ICCC|ICCH|ICCI|ICCM|ICCN|ICCR|ICCT|ICCU|ICCW|ICCX|ICLC|ICLM|ICLK|ICLR|ICLS|ICLT|ICLZ|ICMD|ICNC|ICNM|ICNP|ICPB|ICPL|ICPP|ICPT|ICRG|ICRM|ICRS|ICRZ|ICUB|ICUG|ICVI|ICVR|ICWR|IDAI|IDEX|IDI|IDRA|IFBD|IFEB|IFFI|IFIO|IFLG|IFNN|IFNT|IFON|IFPK|IFRH|IFRU|IFSA|IFSH|IFSM|IFTL|IFTT|IFUN|IFUS|IHC|IIII|IIGD|IIGI|IIIT|IIIN|IIIP|IIVI|IIVR|IIXL|IJJP|IKCA|IKCL|IKCR|IKDI|IKIN|IKNR|IKON|IKOO|IKOV|IKPD|IKSI|IKTS|ILNS|IMAC|IMCI|IMGO|IMGN|IMHB|IMII|IMIM|IMIP|IMIX|IMKB|IMKN|IMKU|IMLA|IMLE|IMLM|IMLN|IMLP|IMLT|IMMB|IMMI|IMMR|IMMU|IMMZ|IMN|IMNP|IMNS|IMNU|IMNV|IMON|IMOU|IMPA|IMPC|IMPL|IMPP|IMPR|IMRA|IMRC|IMRO|IMRS|IMRT|IMRX|IMSC|IMSI|IMSN|IMSO|IMSP|IMSR|IMSS|IMST|IMTA|IMTC|IMTE|IMTI|IMTM|IMTN|IMTO|IMTP|IMTR|IMTS|IMTT|IMTU|IMTV|IMTW|IMTX|IMTY|IMTZ|IMUN|IMUP|IMUS|IMVA|IMVE|IMVI|IMVM|IMVN|IMVO|IMVP|IMVR|IMVS|IMVT|IMVU|IMVV|IMVW|IMVX|IMVY|IMVZ|IMWA|IMWE|IMWI|IMWJ|IMWL|IMWN|IMWO|IMWP|IMWQ|IMWR|IMWS|IMWT|IMWU|IMWV|IMWW|IMWX|IMWY|IMWZ|IMXA|IMXB|IMXC|IMXD|IMXE|IMXF|IMXG|IMXH|IMXI|IMXJ|IMXK|IMXL|IMXM|IMXN|IMXO|IMXP|IMXQ|IMXR|IMXS|IMXT|IMXU|IMXV|IMXW|IMXX|IMXY|IMXZ|IMYA|IMYB|IMYC|IMYD|IMYE|IMYF|IMYG|IMYH|IMYI|IMYJ|IMYK|IMYL|IMYM|IMYN|IMYO|IMYP|IMYQ|IMYR|IMYS|IMYT|IMYU|IMYV|IMYW|IMYX|IMYY|IMYZ|IMZA|IMZB|IMZC|IMZD|IMZE|IMZF|IMZG|IMZH|IMZI|IMZJ|IMZK|IMZL|IMZM|IMZN|IMZO|IMZP|IMZQ|IMZR|IMZS|IMZT|IMZU|IMZV|IMZW|IMZX|IMZY|IMZZ)(USDT|USD|USDC)?$",
    re.IGNORECASE
)


def is_special_commodity(ticker: str) -> str | None:
    """
    Detect if ticker is a special commodity (precious metal, oil, stock token).
    Returns the commodity type: 'metal', 'oil', 'stock', or None.
    """
    ticker_upper = ticker.upper().replace(".P", "").replace("_P", "").replace("-", "")

    for metal_code in ["XAU", "XAG", "XPD", "XPT", "XBR", "XTI", "NG"]:
        if ticker_upper.startswith(metal_code):
            if metal_code in ["XBR", "XTI", "NG"]:
                return "oil"
            return "metal"

    if _STOCK_TOKEN_PATTERN.match(ticker_upper):
        return "stock"

    return None


def get_yfinance_symbol(ticker: str) -> str:
    """
    Convert crypto ticker to Yahoo Finance symbol.
    """
    ticker_upper = ticker.upper().replace(".P", "").replace("_P", "").replace("-", "")

    for commodity_code, yf_code in _COMMODITY_SYMBOL_MAP.items():
        if ticker_upper.startswith(commodity_code):
            return f"{yf_code}=X"

    stock_match = _STOCK_TOKEN_PATTERN.match(ticker_upper)
    if stock_match:
        stock_symbol = stock_match.group(1)
        return stock_symbol

    return ticker_upper


async def fetch_yfinance_data(symbol: str) -> dict[str, Any] | None:
    """
    Fetch market data from Yahoo Finance.
    Uses yfinance library if available, otherwise falls back to web scraping.
    """
    try:
        import yfinance
        has_yfinance = True
    except ImportError:
        has_yfinance = False
        logger.debug("[CommodityData] yfinance not installed, trying direct API")

    if has_yfinance:
        try:
            ticker_obj = await asyncio.to_thread(yfinance.Ticker, symbol)
            info = await asyncio.to_thread(lambda: ticker_obj.info)
            hist = await asyncio.to_thread(lambda: ticker_obj.history(period="5d", interval="1h"))

            if not info and hist.empty:
                return None

            current_price = info.get("currentPrice") or info.get("regularMarketPrice") or info.get("previousClose", 0)

            if not hist.empty:
                closes = hist["Close"].tolist()
                price_1h_ago = closes[-2] if len(closes) >= 2 else current_price
                price_24h_ago = closes[-24] if len(closes) >= 24 else closes[0] if closes else current_price
                price_change_1h = ((current_price - price_1h_ago) / price_1h_ago * 100) if price_1h_ago > 0 else 0
                price_change_24h = ((current_price - price_24h_ago) / price_24h_ago * 100) if price_24h_ago > 0 else 0

                high_24h = max(closes[-24:]) if len(closes) >= 24 else max(closes) if closes else current_price
                low_24h = min(closes[-24:]) if len(closes) >= 24 else min(closes) if closes else current_price

                volumes = hist["Volume"].tolist() if "Volume" in hist.columns else []
                volume_24h = sum(volumes[-24:]) if len(volumes) >= 24 else sum(volumes) if volumes else 0

                rsi = _calculate_rsi_yf(closes, 14)
                atr = _calculate_atr_yf(hist, 14)
                atr_pct = (atr / current_price * 100) if current_price > 0 and atr > 0 else 0

                ema_fast = _calculate_ema_yf(closes, 8)
                ema_slow = _calculate_ema_yf(closes, 21)
            else:
                price_change_1h = 0
                price_change_24h = info.get("regularMarketChangePercent", 0)
                high_24h = info.get("dayHigh") or current_price
                low_24h = info.get("dayLow") or current_price
                volume_24h = info.get("regularMarketVolume", 0)
                rsi = None
                atr_pct = None
                ema_fast = None
                ema_slow = None

            return {
                "current_price": float(current_price),
                "price_change_1h": float(price_change_1h),
                "price_change_4h": float(price_change_24h / 6),  # approximate
                "price_change_24h": float(price_change_24h),
                "volume_24h": float(volume_24h),
                "high_24h": float(high_24h),
                "low_24h": float(low_24h),
                "rsi_1h": rsi,
                "atr_pct": atr_pct,
                "ema_fast": ema_fast,
                "ema_slow": ema_slow,
                "source": "yfinance",
            }
        except Exception as e:
            logger.warning(f"[CommodityData] yfinance fetch failed for {symbol}: {e}")
            return None

    return await _fetch_yfinance_direct_api(symbol)


async def _fetch_yfinance_direct_api(symbol: str) -> dict[str, Any] | None:
    """
    Fetch data directly from Yahoo Finance API via HTTP.
    Fallback when yfinance library is not installed.
    """
    import httpx

    try:
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?interval=1h&range=5d"
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}

        async with httpx.AsyncClient(timeout=15.0, headers=headers) as client:
            resp = await client.get(url)
            if resp.status_code != 200:
                return None

            data = resp.json()
            result = data.get("chart", {}).get("result", [])
            if not result:
                return None

            quote = result[0]
            meta = quote.get("meta", {})
            indicators = quote.get("indicators", {}).get("quote", [])

            if not indicators:
                return None

            quotes = indicators[0]
            closes = [c for c in quotes.get("close", []) if c is not None]
            volumes = [v for v in quotes.get("volume", []) if v is not None]
            highs = [h for h in quotes.get("high", []) if h is not None]
            lows = [low for low in quotes.get("low", []) if low is not None]

            current_price = meta.get("regularMarketPrice", closes[-1] if closes else 0)
            price_1h_ago = closes[-2] if len(closes) >= 2 else current_price
            price_24h_ago = closes[-24] if len(closes) >= 24 else closes[0] if closes else current_price

            price_change_1h = ((current_price - price_1h_ago) / price_1h_ago * 100) if price_1h_ago > 0 else 0
            price_change_24h = ((current_price - price_24h_ago) / price_24h_ago * 100) if price_24h_ago > 0 else 0

            high_24h = max(highs[-24:]) if len(highs) >= 24 else max(highs) if highs else current_price
            low_24h = min(lows[-24:]) if len(lows) >= 24 else min(lows) if lows else current_price
            volume_24h = sum(volumes[-24:]) if len(volumes) >= 24 else sum(volumes) if volumes else 0

            rsi = _calculate_rsi_yf(closes, 14)
            atr_pct = None
            ema_fast = _calculate_ema_yf(closes, 8)
            ema_slow = _calculate_ema_yf(closes, 21)

            return {
                "current_price": float(current_price),
                "price_change_1h": float(price_change_1h),
                "price_change_4h": float(price_change_24h / 6),
                "price_change_24h": float(price_change_24h),
                "volume_24h": float(volume_24h),
                "high_24h": float(high_24h),
                "low_24h": float(low_24h),
                "rsi_1h": rsi,
                "atr_pct": atr_pct,
                "ema_fast": ema_fast,
                "ema_slow": ema_slow,
                "source": "yfinance_api",
            }
    except Exception as e:
        logger.warning(f"[CommodityData] Yahoo API fetch failed for {symbol}: {e}")
        return None


def _calculate_rsi_yf(closes: list[float], period: int = 14) -> float | None:
    """Calculate RSI from price closes."""
    if len(closes) < period + 1:
        return None

    deltas = [closes[i + 1] - closes[i] for i in range(len(closes) - 1)]
    gains = [d if d > 0 else 0 for d in deltas]
    losses = [-d if d < 0 else 0 for d in deltas]

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    if avg_loss == 0:
        return 100.0

    rs = avg_gain / avg_loss
    rsi = 100 - (100 / (1 + rs))
    return round(rsi, 2)


def _calculate_atr_yf(hist: Any, period: int = 14) -> float:
    """Calculate ATR from yfinance history DataFrame."""
    try:
        if len(hist) < period + 1:
            return 0

        highs = hist["High"].tolist()
        lows = hist["Low"].tolist()
        closes = hist["Close"].tolist()

        tr_list = []
        for i in range(1, len(highs)):
            tr = max(
                highs[i] - lows[i],
                abs(highs[i] - closes[i - 1]),
                abs(lows[i] - closes[i - 1])
            )
            tr_list.append(tr)

        atr = sum(tr_list[-period:]) / period
        return atr
    except (KeyError, IndexError, TypeError, ValueError, AttributeError):
        return 0
    except Exception:
        return 0


def _calculate_ema_yf(closes: list[float], period: int) -> float | None:
    """Calculate EMA from price closes."""
    if len(closes) < period:
        return None

    multiplier = 2 / (period + 1)
    ema = sum(closes[:period]) / period

    for price in closes[period:]:
        ema = (price - ema) * multiplier + ema

    return round(ema, 4)


async def fetch_commodity_market_context(ticker: str) -> MarketContext | None:
    """
    Fetch market context for special commodities/stock tokens.
    Returns MarketContext with real market data from Yahoo Finance.
    """
    commodity_type = is_special_commodity(ticker)
    if not commodity_type:
        return None

    now = time.monotonic()

    async with _yfinance_cache_lock:
        entry = _yfinance_cache.get(ticker)
        if entry and (now - entry[0]) < _YFINANCE_CACHE_TTL:
            _yfinance_cache.move_to_end(ticker)
            logger.debug(f"[CommodityData] Cache hit for {ticker}")
            return entry[1]

    yf_symbol = get_yfinance_symbol(ticker)
    logger.info(f"[CommodityData] Fetching {commodity_type} data for {ticker} via {yf_symbol}")

    data = await fetch_yfinance_data(yf_symbol)

    if not data:
        logger.warning(f"[CommodityData] No data available for {ticker}")
        return None

    context = MarketContext(
        ticker=ticker,
        current_price=data["current_price"],
        price_change_1h=data["price_change_1h"],
        price_change_4h=data["price_change_4h"],
        price_change_24h=data["price_change_24h"],
        volume_24h=data["volume_24h"],
        volume_change_pct=0.0,
        high_24h=data["high_24h"],
        low_24h=data["low_24h"],
        bid_ask_spread=0.0,
        funding_rate=None,
        open_interest=None,
        open_interest_change_pct=None,
        rsi_1h=data["rsi_1h"],
        atr_pct=data["atr_pct"],
        ema_fast=data["ema_fast"],
        ema_slow=data["ema_slow"],
        orderbook_imbalance=None,
        long_short_ratio=None,
    )

    async with _yfinance_cache_lock:
        _yfinance_cache[ticker] = (time.monotonic(), context)
        _yfinance_cache.move_to_end(ticker)
        while len(_yfinance_cache) > _YFINANCE_CACHE_MAX_SIZE:
            _yfinance_cache.popitem(last=False)

    logger.info(
        f"[CommodityData] ✅ Got {commodity_type} data for {ticker}: "
        f"price={context.current_price:.2f}, 24h_change={context.price_change_24h:+.2f}%"
    )

    return context
