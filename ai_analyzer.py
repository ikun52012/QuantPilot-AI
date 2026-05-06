"""
QuantPilot AI - AI Analyzer
Uses LLM APIs (OpenAI / Anthropic / DeepSeek / OpenRouter) to analyze trading signals.
This is the brain of the system.
"""
import asyncio
import hashlib
import json
import math
import time as _time
from collections.abc import Awaitable, Callable, Coroutine
from typing import Any

import httpx
from loguru import logger

from core.ai_cost_tracker import ai_costs, extract_usage_from_response
from core.config import settings
from core.utils.common import first_valid, safe_float, safe_int
from models import AIAnalysis, MarketContext, SignalDirection, TradingViewSignal
from models import TrailingStopMode as _TrailingStopMode

TrailingStopMode = _TrailingStopMode

# Retry configuration for AI API calls
_AI_MAX_RETRIES = 3
_AI_BASE_DELAY = 1.0  # seconds; doubled each attempt
_RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}
_AI_TIMEOUT = httpx.Timeout(
    connect=settings.ai.connect_timeout_secs,
    read=settings.ai.read_timeout_secs,
    write=settings.ai.write_timeout_secs,
    pool=settings.ai.pool_timeout_secs,
)

# Global semaphore to limit concurrent AI API calls
_AI_SEMAPHORE = asyncio.Semaphore(settings.ai.max_concurrent_calls)

# ─────────────────────────────────────────────
# AI analysis result cache (#18) with dynamic TTL (Optimization 3)
# P0-FIX: Double-check locking for thread-safe singleton initialization
# ─────────────────────────────────────────────
_AI_CACHE_BASE_TTL = 60
_AI_CACHE_MAX_SIZE = 500
_AI_CACHE: dict[str, tuple[float, float, "AIAnalysis"]] = {}  # (timestamp, ttl, analysis)
_AI_CACHE_LOCK_INIT_LOCK = asyncio.Lock()  # P0-FIX: Init lock for double-check locking
_AI_CACHE_LOCK: asyncio.Lock | None = None  # P0-FIX: Singleton cache lock
_VOLATILITY_TRACKER: dict[str, float] = {}  # ticker -> recent volatility pct
_VOLATILITY_TRACKER_INIT_LOCK = asyncio.Lock()  # P0-FIX: Init lock
_VOLATILITY_TRACKER_LOCK: asyncio.Lock | None = None  # P0-FIX: Singleton lock

# ─────────────────────────────────────────────
# SMC analysis cache (P1-5: Performance optimization)
# P0-FIX: Double-check locking for thread-safe singleton initialization
# ─────────────────────────────────────────────
_SMC_CACHE_BASE_TTL = 120  # 2 minutes (SMC structure changes slower than price)
_SMC_CACHE_MAX_SIZE = 200
_SMC_CACHE: dict[str, tuple[float, float, dict[str, Any]]] = {}  # (timestamp, ttl, smc_dict)
_SMC_CACHE_INIT_LOCK = asyncio.Lock()  # P0-FIX: Init lock for double-check locking
_SMC_CACHE_LOCK: asyncio.Lock | None = None  # P0-FIX: Singleton cache lock


async def _get_ai_cache_lock() -> asyncio.Lock:
    """P0-FIX: Double-check locking for thread-safe singleton initialization.

    Prevents race condition when multiple coroutines check lock is None simultaneously.
    Pattern: first check (no lock) -> acquire init lock -> second check (with lock) -> create.
    """
    global _AI_CACHE_LOCK
    if _AI_CACHE_LOCK is None:  # First check (outside lock - fast path)
        async with _AI_CACHE_LOCK_INIT_LOCK:  # Acquire initialization lock
            if _AI_CACHE_LOCK is None:  # Second check (inside lock - safe path)
                _AI_CACHE_LOCK = asyncio.Lock()
                logger.debug("[P0-FIX] AI cache lock initialized with double-check pattern")
    return _AI_CACHE_LOCK


async def _get_smc_cache_lock() -> asyncio.Lock:
    """P0-FIX: Double-check locking for thread-safe SMC cache lock initialization."""
    global _SMC_CACHE_LOCK
    if _SMC_CACHE_LOCK is None:  # First check
        async with _SMC_CACHE_INIT_LOCK:  # Init lock
            if _SMC_CACHE_LOCK is None:  # Second check
                _SMC_CACHE_LOCK = asyncio.Lock()
                logger.debug("[P0-FIX] SMC cache lock initialized with double-check pattern")
    return _SMC_CACHE_LOCK


async def _get_volatility_lock() -> asyncio.Lock:
    """P0-FIX: Double-check locking for volatility tracker lock initialization."""
    global _VOLATILITY_TRACKER_LOCK
    if _VOLATILITY_TRACKER_LOCK is None:  # First check
        async with _VOLATILITY_TRACKER_INIT_LOCK:  # Init lock
            if _VOLATILITY_TRACKER_LOCK is None:  # Second check
                _VOLATILITY_TRACKER_LOCK = asyncio.Lock()
                logger.debug("[P0-FIX] Volatility tracker lock initialized with double-check pattern")
    return _VOLATILITY_TRACKER_LOCK


def _ohlcv_signature(ohlcv: list[list], samples: int = 5) -> str:
    """Generate hash signature from last N OHLCV candles to detect data changes."""
    if not ohlcv or len(ohlcv) < samples:
        return ""
    last_candles = ohlcv[-samples:]
    signature_data = []
    for candle in last_candles:
        try:
            signature_data.append(f"{candle[0]}:{candle[2]}:{candle[3]}:{candle[4]}")
        except (IndexError, TypeError):
            continue
    return hashlib.sha256("|".join(signature_data).encode()).hexdigest()[:16]


async def _get_cached_smc(ticker: str, timeframe: str, ohlcv_sig: str, cache_key: str) -> dict[str, Any] | None:
    """Get cached SMC analysis result if available and not expired.

    BUG-4 FIX: Also clean expired entries during reads.
    """
    lock = await _get_smc_cache_lock()  # BUG-1 FIX: Lazy init
    async with lock:
        # BUG-4 FIX: Clean expired entries during reads
        expired_keys = [k for k, (ts, ttl, _) in _SMC_CACHE.items() if _time.monotonic() - ts > ttl]
        for expired_key in expired_keys:
            del _SMC_CACHE[expired_key]

        entry = _SMC_CACHE.get(cache_key)
        if entry:
            timestamp, ttl, smc_dict = entry
            age = _time.monotonic() - timestamp
            if age < ttl:
                logger.debug(f"[SMC_CACHE] Hit for {ticker}:{timeframe} (age={age:.1f}s, ttl={ttl}s)")
                return smc_dict
    return None


async def _set_cached_smc(ticker: str, timeframe: str, ohlcv_sig: str, smc_ctx: Any, cache_key: str, ttl: float = _SMC_CACHE_BASE_TTL) -> None:
    """Cache SMC analysis result with TTL."""
    # Convert SMCContext to dict for caching
    smc_dict = {
        "timeframe": smc_ctx.timeframe,
        "fvgs": smc_ctx.fvgs,
        "order_blocks": smc_ctx.order_blocks,
        "structure": smc_ctx.structure,
        "premium_zone": smc_ctx.premium_zone,
        "discount_zone": smc_ctx.discount_zone,
        "equilibrium": smc_ctx.equilibrium,
        "risk_score": smc_ctx.risk_score,
        "entry_timing_score": smc_ctx.entry_timing_score,
        "timing_recommendation": smc_ctx.timing_recommendation,
    }

    lock = await _get_smc_cache_lock()  # BUG-1 FIX: Lazy init
    async with lock:
        # Enforce max size by removing oldest entries
        if len(_SMC_CACHE) >= _SMC_CACHE_MAX_SIZE:
            sorted_keys = sorted(_SMC_CACHE.keys(), key=lambda k: _SMC_CACHE[k][0])
            for old_key in sorted_keys[:len(_SMC_CACHE) - _SMC_CACHE_MAX_SIZE + 1]:
                del _SMC_CACHE[old_key]

        _SMC_CACHE[cache_key] = (_time.monotonic(), ttl, smc_dict)
        logger.debug(f"[SMC_CACHE] Stored for {ticker}:{timeframe} (ttl={ttl}s)")


def _reconstruct_smc_context(smc_dict: dict[str, Any], timeframe: str) -> Any:
    """Reconstruct SMCContext object from cached dict."""
    from smc_analyzer import MarketStructure, SMCContext

    structure_dict = smc_dict.get("structure")
    structure = None
    if structure_dict:
        if isinstance(structure_dict, dict):
            structure = MarketStructure(
                trend=structure_dict.get("trend"),
                last_bos=structure_dict.get("last_bos"),
                last_choch=structure_dict.get("last_choch"),
                swing_highs=structure_dict.get("swing_highs", []),
                swing_lows=structure_dict.get("swing_lows", []),
                break_strength=structure_dict.get("break_strength", 0.5),
            )
        else:
            structure = structure_dict

    return SMCContext(
        timeframe=timeframe,
        fvgs=smc_dict.get("fvgs", []),
        order_blocks=smc_dict.get("order_blocks", []),
        structure=structure,
        premium_zone=smc_dict.get("premium_zone", 0.0),
        discount_zone=smc_dict.get("discount_zone", 0.0),
        equilibrium=smc_dict.get("equilibrium", 0.0),
        risk_score=smc_dict.get("risk_score", 0.5),
        entry_timing_score=smc_dict.get("entry_timing_score", 0.5),
        timing_recommendation=smc_dict.get("timing_recommendation", "Fair"),
    )


async def _cached_analyze_smc_single_tf(
    ticker: str,
    ohlcv: list[list],
    timeframe: str,
    current_price: float,
    signal_direction: str,
    atr_pct: float = 0.0,  # P1-R1: Add ATR parameter for dynamic zones
) -> Any:
    """Analyze SMC for single timeframe with caching (P1-5).

    P1-R1: Pass atr_pct to enable dynamic premium/discount zones.
    P2-O4: Dynamic TTL based on ATR volatility.
    """
    from smc_analyzer import analyze_smc_single_tf

    # P2-R4: Dynamic OHLCV length threshold based on timeframe
    # detect_market_structure requires: 7 candles for 1h/30m/15m/5m, 11 for 4h/daily
    tf_minutes = {"5m": 5, "15m": 15, "30m": 30, "1h": 60, "4h": 240, "1d": 1440}
    tf_mins = tf_minutes.get(timeframe, 60)
    min_length = 11 if tf_mins >= 240 else 7  # 4h/daily need 11, others need 7

    if not ohlcv or len(ohlcv) < min_length:
        return None

    ohlcv_sig = _ohlcv_signature(ohlcv)

    atr_bucket = "high" if atr_pct > 3.0 else "low" if atr_pct < 1.0 else "normal"
    price_bucket = _price_to_bucket(current_price) if current_price > 0 else ""
    direction_bucket = signal_direction.lower()[:4]

    cache_key = f"{ticker}:{timeframe}:{ohlcv_sig}:{direction_bucket}:{price_bucket}:{atr_bucket}"

    cached_dict = await _get_cached_smc(ticker, timeframe, ohlcv_sig, cache_key)
    if cached_dict:
        return _reconstruct_smc_context(cached_dict, timeframe)

    # Perform actual analysis
    # P1-R1: Pass atr_pct to enable dynamic premium/discount zones
    smc_ctx = analyze_smc_single_tf(ohlcv, timeframe, current_price, signal_direction, atr_pct)

    # P2-O4: Dynamic TTL based on ATR volatility
    # High volatility (>3% ATR): Shorter TTL (60s) for fresher structure
    # Low volatility (<1% ATR): Longer TTL (180s) for stable structure
    # Normal volatility: Base TTL (120s)
    if atr_pct > 3.0:
        dynamic_ttl = 60
    elif atr_pct < 1.0:
        dynamic_ttl = 180
    else:
        dynamic_ttl = 120

    # Cache the result with dynamic TTL
    await _set_cached_smc(ticker, timeframe, ohlcv_sig, smc_ctx, cache_key, ttl=dynamic_ttl)

    return smc_ctx


async def _update_volatility_tracker(ticker: str, market: MarketContext) -> float:
    """Update volatility tracker for dynamic TTL calculation (Optimization 3)."""
    volatility_pct = 0.0
    if market.current_price > 0 and market.high_24h > 0 and market.low_24h > 0:
        volatility_pct = abs(market.high_24h - market.low_24h) / market.current_price * 100

    lock = await _get_volatility_lock()  # BUG-1 FIX: Lazy init
    async with lock:
        _VOLATILITY_TRACKER[ticker] = volatility_pct
        if len(_VOLATILITY_TRACKER) > 200:
            oldest = next(iter(_VOLATILITY_TRACKER))
            _VOLATILITY_TRACKER.pop(oldest)

    return volatility_pct


async def _get_dynamic_cache_ttl(ticker: str) -> float:
    """Calculate dynamic cache TTL based on market volatility (Optimization 3).

    High volatility (>5%) -> shorter TTL (30s) for fresher analysis
    Low volatility (<2%) -> longer TTL (120s) for better cache hit rate
    Normal volatility -> base TTL (60s)
    """
    if not settings.ai.dynamic_cache_ttl_enabled:
        return _AI_CACHE_BASE_TTL

    lock = await _get_volatility_lock()  # BUG-1 FIX: Lazy init
    async with lock:
        volatility = _VOLATILITY_TRACKER.get(ticker, 0.0)

    base_ttl = settings.ai.dynamic_cache_ttl_base

    if volatility > 5.0:
        dynamic_ttl = base_ttl * settings.ai.dynamic_cache_ttl_high_volatility_multiplier
        logger.debug(f"[AI/Cache] High volatility {ticker} ({volatility:.1f}%): TTL={dynamic_ttl:.0f}s")
        return dynamic_ttl
    elif volatility < 2.0:
        dynamic_ttl = base_ttl * settings.ai.dynamic_cache_ttl_low_volatility_multiplier
        logger.debug(f"[AI/Cache] Low volatility {ticker} ({volatility:.1f}%): TTL={dynamic_ttl:.0f}s")
        return dynamic_ttl

    return base_ttl


def _ai_cache_key(
    ticker: str,
    direction: str,
    price_bucket: str = "",
    timeframe: str = "",
    config_signature: str = "",
    ohlcv_signature: str = "",  # P2-8: Add OHLCV signature to detect market changes
) -> str:
    """Generate cache key with price bucket and timeframe to avoid stale cache hits.

    Price is bucketed to 1% intervals to allow cache hits for similar prices
    while avoiding incorrect hits when price moves significantly.
    P2-8: Include OHLCV signature to detect market structure changes.
    """
    key = f"{ticker}:{direction}"
    if timeframe:
        key += f":{timeframe}"
    if price_bucket:
        key += f":{price_bucket}"
    if ohlcv_signature:
        key += f":{ohlcv_signature[:8]}"  # P2-8: Only first 8 chars to keep key reasonable length
    if config_signature:
        key += f":{config_signature}"
    return key


def _price_to_bucket(price: float, bucket_pct: float = 1.0) -> str:
    """Convert price to a bucket string for cache key grouping.

    Groups prices into 1% intervals to balance cache hit rate with accuracy.
    """
    if price <= 0:
        return ""
    bucket_size = price * bucket_pct / 100
    bucket = int(price / bucket_size) * bucket_size
    return f"{bucket:.2f}"


async def _get_cached_analysis(
    ticker: str,
    direction: str,
    price_bucket: str = "",
    timeframe: str = "",
    config_signature: str = "",
    ohlcv_signature: str = "",  # P2-8: Add OHLCV signature
) -> AIAnalysis | None:
    """Get cached analysis with dynamic TTL support.

    BUG-4 FIX: Also clean expired entries during reads.
    """
    key = _ai_cache_key(ticker, direction, price_bucket, timeframe, config_signature, ohlcv_signature)
    lock = await _get_ai_cache_lock()  # BUG-1 FIX: Lazy init
    async with lock:
        # BUG-4 FIX: Clean expired entries during reads
        now = _time.monotonic()
        expired_keys = [k for k, (ts, ttl, _) in _AI_CACHE.items() if now - ts > ttl]
        for expired_key in expired_keys:
            del _AI_CACHE[expired_key]

        entry = _AI_CACHE.get(key)
        if entry:
            timestamp, ttl, analysis = entry
            if (_time.monotonic() - timestamp) < ttl:
                return analysis
    return None


async def _set_cached_analysis(
    ticker: str,
    direction: str,
    analysis: AIAnalysis,
    price_bucket: str = "",
    timeframe: str = "",
    config_signature: str = "",
    ohlcv_signature: str = "",  # P2-8: Add OHLCV signature
) -> None:
    """Set cached analysis with dynamic TTL based on volatility."""
    key = _ai_cache_key(ticker, direction, price_bucket, timeframe, config_signature, ohlcv_signature)
    ttl = await _get_dynamic_cache_ttl(ticker)

    lock = await _get_ai_cache_lock()  # BUG-1 FIX: Lazy init
    async with lock:
        _AI_CACHE[key] = (_time.monotonic(), ttl, analysis)

        now = _time.monotonic()
        stale = [k for k, (ts, t, _) in _AI_CACHE.items() if now - ts > t]
        for k in stale:
            del _AI_CACHE[k]

        while len(_AI_CACHE) > _AI_CACHE_MAX_SIZE:
            oldest_key = min(_AI_CACHE.keys(), key=lambda k: _AI_CACHE[k][0])
            del _AI_CACHE[oldest_key]


# ─────────────────────────────────────────────
# System prompt - the "trading analyst" persona
# Enhanced with multi-TP and trailing stop awareness
# ─────────────────────────────────────────────
SYSTEM_PROMPT = """You are an expert cryptocurrency quantitative trading analyst with 15 years of experience.
You receive trading signals from a TradingView strategy and must analyze whether to EXECUTE, MODIFY, or REJECT each signal.

Your analysis process:
1. Evaluate the signal direction against current market context
2. Assess risk/reward ratio
3. Check for conflicting indicators
4. Consider market microstructure (orderbook, spread, volume)
5. Factor in broader market conditions (funding rate, 24h trend)
6. Use VWAP, volume profile, session highs/lows, and liquidity sweeps to improve entry and exit placement
7. Determine optimal take-profit targets (up to 4 levels)
8. Assess volatility to suggest appropriate trailing stop parameters
9. Recommend trailing stop mode based on market conditions and confidence

You MUST respond in valid JSON format with these exact fields:
{
    "confidence": 0.0-1.0,
    "recommendation": "execute" | "modify" | "reject",
    "reasoning": "Your detailed analysis in 2-3 sentences",
    "suggested_direction": "long" | "short" | null,
    "suggested_entry": null or float,
    "suggested_stop_loss": null or float,
    "suggested_take_profit": null or float,
    "suggested_tp1": null or float (first target, typically 1-2% from entry),
    "suggested_tp2": null or float (second target, typically 2-4% from entry),
    "suggested_tp3": null or float (third target, typically 4-6% from entry),
    "suggested_tp4": null or float (fourth target, typically 6-10% from entry),
    "tp1_qty_pct": 25.0 (% of position to close at TP1),
    "tp2_qty_pct": 25.0,
    "tp3_qty_pct": 25.0,
    "tp4_qty_pct": 25.0,
    "position_size_pct": 0.1-1.0,
    "recommended_leverage": 1-125,
    "risk_score": 0.0-1.0,
    "market_condition": "trending_up" | "trending_down" | "ranging" | "volatile" | "calm",
    "trend_strength": "strong" | "moderate" | "weak" | "none",
    "recommended_trailing_stop_mode": "none" | "breakeven_on_tp1" | "step_trailing" | "moving",
    "warnings": ["list of risk warnings"]
}

Key rules:
- If confidence < 0.4, always recommend "reject"
- Reject trades whose realistic reward/risk is below the active profile requirement
- Stop loss must be placed beyond a logical invalidation area, not at a random fixed distance
- For long trades, stop loss must be below entry and take profits above entry
- For short trades, stop loss must be above entry and take profits below entry
- If funding rate is extreme (>0.05% or <-0.05%), warn about it
- If 1h price change > 5%, reduce position_size_pct
- If RSI > 75 and signal is long, be skeptical. If RSI < 25 and signal is short, be skeptical.
- If orderbook is heavily imbalanced against the signal direction, warn about it
- recommended_leverage is only a recommendation for the operator and must decrease as risk_score/volatility rises
- NEVER recommend more than position_size_pct = 1.0
- For take-profit levels: space them based on ATR and volatility
  - In trending markets, use wider TP spacing
  - In ranging markets, use tighter TP spacing
- TP quantities should sum to ≤ 100%
- Use "modify" only when the trade thesis is valid but entry quality is poor and you can provide a materially better `suggested_entry`
- Use "reject" when the setup is invalid, structurally conflicted, or lacks a safe invalidation level
- Use "hold" only when the data is insufficient or ambiguous; do not use "hold" as a synonym for reject
- If you recommend "execute" or "modify", include a valid `suggested_stop_loss` based on your multi-timeframe invalidation analysis plus valid TP levels required by the server
- For "execute" or "modify", `suggested_stop_loss` must be a finite number, never null/0/equal to entry, and on the protective side of the final entry
- ATR/timeframe SL ranges are guidance only; do not force SL to a fixed percentage if the structure gives a better invalidation level
- If using "modify", calculate SL/TP from `suggested_entry`; if using "execute", calculate SL/TP from the signal price
- Prefer entries near fair value, discount/premium imbalance, VWAP reclaim/rejection, value-area edges, or post-sweep retests instead of chasing extended moves
- Use session highs/lows, POC/HVN, LVN gaps, liquidity sweep levels, and SMC invalidation points as candidates for TP/SL placement
- If you cannot produce a valid stop loss, set recommendation="reject" instead of execute/modify
- If you recommend "reject" or "hold", set `suggested_entry`, `suggested_stop_loss`, `suggested_take_profit`, and all `suggested_tp*` fields to null
- If `suggested_direction` matches the incoming signal direction, return null instead of repeating the same direction

Trailing Stop Mode Selection Guidelines:
- "none": Strong trend + high confidence (>0.75). Let profits run, maximize R:R.
- "step_trailing": Moderate trend, ranging market, or volatile conditions. Lock profits at each TP level.
- "breakeven_on_tp1": High risk score (>0.7), weak trend, or uncertain market. Protect capital first.
- "moving": Only for classic trailing stop preference (not recommended for multi-TP trades).

Trend Strength Assessment:
- "strong": Clear directional momentum, EMA alignment, price making higher highs/lower lows consistently
- "moderate": Some trend direction but with pullbacks, EMAs somewhat aligned
- "weak": Price oscillating around key levels, no clear direction
- "none": No trend, pure ranging/choppy market

Respond ONLY with the JSON object, no other text."""


# ─────────────────────────────────────────────
# SMC / FVG entry optimization instructions
# ─────────────────────────────────────────────
SMC_FVG_PROMPT = """
## Smart Money Concepts (SMC) & Fair Value Gap (FVG) Entry Optimization

You will receive multi-timeframe SMC analysis data including:
- **Market Structure**: BOS (Break of Structure), CHoCH (Change of Character), trend direction per timeframe
- **Fair Value Gaps (FVG)**: Imbalance zones where price moved too fast — these are high-probability retracement targets
- **Order Blocks (OB)**: Last opposing candle before a strong impulse — institutional entry footprints
- **Premium/Discount Zones**: Fibonacci-based value areas from recent swing range
- **Confluence Zones**: Areas where multiple timeframe levels overlap (highest probability)

### Entry Optimization Rules:
1. **If the signal price is in a PREMIUM zone for a LONG trade**: recommend "modify" and suggest entry at the nearest unfilled bullish FVG or bullish OB in the discount zone. This gives a better risk/reward.
2. **If the signal price is in a DISCOUNT zone for a SHORT trade**: recommend "modify" and suggest entry at the nearest unfilled bearish FVG or bearish OB in the premium zone.
3. **If a confluence zone exists near the signal price (within 1-2 ATR)**: prefer that zone as the entry point.
4. **HTF (4H) structure takes priority**: If HTF trend conflicts with the signal direction AND there's no CHoCH, be very skeptical.
5. **Use FVG midpoints as entry targets**: The midpoint of an unfilled FVG is the optimal entry within that zone.
6. **Order Block entries**: Enter at the OB midpoint; place stop loss beyond the OB boundary.
7. **When modifying entry**: Set `suggested_entry` to the optimal price. The system will use limit orders or wait for price to reach this level.
8. **Stop loss placement**: Place SL beyond the nearest structural invalidation point (below swing low for longs, above swing high for shorts), not at a random fixed distance.
9. **If no SMC data is available**: Fall back to standard technical analysis (RSI, EMA, ATR).
"""


RISK_PROFILE_PROMPTS = {
    "conservative": """AI risk profile: CONSERVATIVE.
- Filter aggressively; reject marginal, late, overextended, or noisy trades.
- Require realistic total reward/risk of at least 1:2 before execute/modify.
- Prefer 1x-5x leverage; never recommend above 10x.
- Use wider volatility-aware stops, smaller position_size_pct, and fewer trades.
- If market structure is unclear, reject instead of forcing a plan.""",
    "balanced": """AI risk profile: BALANCED.
- Trade only clean setups with acceptable confirmation and liquidity.
- Require realistic total reward/risk of at least 1:1.5 before execute/modify.
- Prefer 2x-10x leverage; never recommend above 20x.
- Balance capital protection with reasonable participation.
- Modify entries/exits when the signal is usable but raw levels are weak.""",
    "aggressive": """AI risk profile: AGGRESSIVE.
- Accept more momentum/breakout opportunities, but never ignore invalidation.
- Require realistic total reward/risk of at least 1:1.2 before execute/modify.
- Prefer 5x-20x leverage; never recommend above 50x.
- Use tighter invalidation and faster TP scaling when volatility is high.
- Still reject trades with impossible stops, severe spread, or strong opposite orderbook pressure.""",
}


def _effective_risk_config(user_settings: dict | None = None):
    risk_cfg = (user_settings or {}).get("risk") or {}

    class _Risk:
        _mode = str(first_valid(risk_cfg.get("exit_management_mode"), settings.risk.exit_management_mode) or "ai").lower().strip()
        exit_management_mode = _mode if _mode in {"ai", "custom"} else "ai"

        _profile = str(first_valid(risk_cfg.get("ai_risk_profile"), settings.risk.ai_risk_profile) or "balanced").lower().strip()
        ai_risk_profile = _profile if _profile in RISK_PROFILE_PROMPTS else "balanced"

        ai_exit_system_prompt = str(first_valid(risk_cfg.get("ai_exit_system_prompt"), settings.risk.ai_exit_system_prompt) or "")

    return _Risk()


def _analysis_config_signature(user_settings: dict | None = None) -> str:
    risk_config = _effective_risk_config(user_settings)
    tp_config = _effective_take_profit_config(user_settings)
    ts_config = _effective_trailing_stop_config(user_settings)

    payload = {
        "provider": settings.ai.provider,
        "openai_model": settings.ai.openai_model,
        "anthropic_model": settings.ai.anthropic_model,
        "deepseek_model": settings.ai.deepseek_model,
        "mistral_model": settings.ai.mistral_model,
        "openrouter_enabled": settings.ai.openrouter_enabled,
        "openrouter_model": settings.ai.openrouter_model,
        "custom_provider_enabled": settings.ai.custom_provider_enabled,
        "custom_provider_name": settings.ai.custom_provider_name,
        "custom_provider_model": settings.ai.custom_provider_model,
        "custom_provider_api_url": settings.ai.custom_provider_api_url,
        "temperature": settings.ai.temperature,
        "max_tokens": settings.ai.max_tokens,
        "voting_enabled": settings.ai.voting_enabled,
        "voting_models": settings.ai.voting_models,
        "voting_weights": settings.ai.voting_weights,
        "voting_strategy": settings.ai.voting_strategy,
        "ai_custom_system_prompt": settings.ai.custom_system_prompt,
        "exit_management_mode": risk_config.exit_management_mode,
        "ai_risk_profile": risk_config.ai_risk_profile,
        "ai_exit_system_prompt": risk_config.ai_exit_system_prompt,
        "take_profit": {
            "num_levels": tp_config.num_levels,
            "tp1_pct": tp_config.tp1_pct,
            "tp2_pct": tp_config.tp2_pct,
            "tp3_pct": tp_config.tp3_pct,
            "tp4_pct": tp_config.tp4_pct,
            "tp1_qty": tp_config.tp1_qty,
            "tp2_qty": tp_config.tp2_qty,
            "tp3_qty": tp_config.tp3_qty,
            "tp4_qty": tp_config.tp4_qty,
        },
        "trailing_stop": {
            "mode": ts_config.mode,
            "trail_pct": ts_config.trail_pct,
            "activation_profit_pct": ts_config.activation_profit_pct,
            "trailing_step_pct": ts_config.trailing_step_pct,
        },
        "prefilter": (user_settings or {}).get("_prefilter_summary") or {},
    }
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def _get_effective_system_prompt(user_settings: dict | None = None) -> str:
    """Return system prompt with optional custom additions."""
    base = SYSTEM_PROMPT

    # Always include SMC/FVG optimization instructions
    base += "\n" + SMC_FVG_PROMPT

    risk_config = _effective_risk_config(user_settings)
    base += "\n\n" + RISK_PROFILE_PROMPTS.get(risk_config.ai_risk_profile, RISK_PROFILE_PROMPTS["balanced"])

    num_tp = _effective_take_profit_config(user_settings).num_levels

    if risk_config.exit_management_mode == "ai":
        beyond_level_instruction = ""
        if num_tp < 4:
            beyond_level_instruction = (
                f" For TP levels beyond {num_tp} (TP{num_tp+1} to TP4), you MUST set them to null "
                f"(not 0, not a number, but null). Similarly, tp{num_tp+1}_qty_pct through tp4_qty_pct should be 0."
            )
        tp_instruction = (
            f"\n\nExit management mode: AI-generated exits are enabled. "
            f"You must provide suggested_stop_loss plus exactly {num_tp} take-profit targets. "
            f"The server is configured for {num_tp} TP levels. "
            f"You MUST generate suggested_tp1 through suggested_tp{num_tp} with valid prices. "
            f"tp1_qty_pct through tp{num_tp}_qty_pct should sum to 100% or less."
            f"{beyond_level_instruction} "
            f"Use your multi-timeframe invalidation level for suggested_stop_loss; "
            f"ATR/timeframe ranges are guidance, while the server only safety-validates direction and finite prices. "
            f"Obey the requested risk profile for exit levels."
        )
        base += tp_instruction
        if risk_config.ai_exit_system_prompt:
            base += f"\nExit-generation instructions:\n{risk_config.ai_exit_system_prompt}"
    else:
        base += (
            "\n\nExit management mode: custom fixed exits are enabled. "
            "You may still comment on risk, but the server will ignore AI stop-loss "
            "and take-profit prices and use configured custom percentages."
        )
    if settings.ai.custom_system_prompt:
        base += f"\n\nAdditional instructions from the user:\n{settings.ai.custom_system_prompt}"
    return base


def _effective_take_profit_config(user_settings: dict | None = None):
    tp_cfg = (user_settings or {}).get("take_profit") or {}

    class _TP:
        num_levels = max(1, min(safe_int(first_valid(tp_cfg.get("num_levels"), settings.take_profit.num_levels), 1), 4))
        tp1_pct = safe_float(first_valid(tp_cfg.get("tp1_pct"), settings.take_profit.tp1_pct), settings.take_profit.tp1_pct)
        tp2_pct = safe_float(first_valid(tp_cfg.get("tp2_pct"), settings.take_profit.tp2_pct), settings.take_profit.tp2_pct)
        tp3_pct = safe_float(first_valid(tp_cfg.get("tp3_pct"), settings.take_profit.tp3_pct), settings.take_profit.tp3_pct)
        tp4_pct = safe_float(first_valid(tp_cfg.get("tp4_pct"), settings.take_profit.tp4_pct), settings.take_profit.tp4_pct)
        tp1_qty = safe_float(first_valid(tp_cfg.get("tp1_qty"), settings.take_profit.tp1_qty), settings.take_profit.tp1_qty)
        tp2_qty = safe_float(first_valid(tp_cfg.get("tp2_qty"), settings.take_profit.tp2_qty), settings.take_profit.tp2_qty)
        tp3_qty = safe_float(first_valid(tp_cfg.get("tp3_qty"), settings.take_profit.tp3_qty), settings.take_profit.tp3_qty)
        tp4_qty = safe_float(first_valid(tp_cfg.get("tp4_qty"), settings.take_profit.tp4_qty), settings.take_profit.tp4_qty)

    return _TP()


def _effective_trailing_stop_config(user_settings: dict | None = None):
    trailing_cfg = (user_settings or {}).get("trailing_stop") or {}

    class _TS:
        mode = str(trailing_cfg.get("mode") or settings.trailing_stop.mode)
        trail_pct = safe_float(first_valid(trailing_cfg.get("trail_pct"), settings.trailing_stop.trail_pct), settings.trailing_stop.trail_pct)
        activation_profit_pct = safe_float(
            first_valid(trailing_cfg.get("activation_profit_pct"), settings.trailing_stop.activation_profit_pct),
            settings.trailing_stop.activation_profit_pct,
        )
        trailing_step_pct = safe_float(
            first_valid(trailing_cfg.get("trailing_step_pct"), settings.trailing_stop.trailing_step_pct),
            settings.trailing_stop.trailing_step_pct,
        )

    return _TS()


def _format_entry_exit_indicators(market: MarketContext) -> str:
    """Format optional VWAP/profile/session/liquidity data for the AI prompt."""
    indicators = getattr(market, "_entry_exit_indicators", None) or {}
    if not isinstance(indicators, dict):
        return ""

    vwap = indicators.get("vwap_1h_24") or {}
    intraday_vwap = indicators.get("intraday_vwap") or {}
    profile = indicators.get("volume_profile_1h") or {}
    session = indicators.get("session_levels") or {}
    sweep = indicators.get("liquidity_sweep") or {}

    lines = ["## Entry / Exit Placement Indicators"]
    if vwap.get("vwap") is not None:
        lines.append(f"- 24h VWAP (1h): {vwap.get('vwap')} (price distance {vwap.get('distance_pct')}%)")
    if intraday_vwap.get("vwap") is not None:
        lines.append(f"- Intraday VWAP: {intraday_vwap.get('vwap')} (price distance {intraday_vwap.get('distance_pct')}%)")
    if profile.get("poc") is not None:
        lines.append(
            "- Volume Profile: "
            f"POC={profile.get('poc')}, Value Area={profile.get('value_area_low')} - {profile.get('value_area_high')}, "
            f"HVN={profile.get('high_volume_nodes')}, LVN={profile.get('low_volume_nodes')}"
        )
    if session.get("session_high") is not None or session.get("session_low") is not None:
        lines.append(
            "- UTC Session Levels: "
            f"current high={session.get('session_high')}, current low={session.get('session_low')}, "
            f"prior high={session.get('prior_session_high')}, prior low={session.get('prior_session_low')}"
        )
    if sweep.get("type") and sweep.get("type") != "none":
        lines.append(
            "- Liquidity Sweep: "
            f"{sweep.get('type')} at {sweep.get('swept_level')} "
            f"(strength={sweep.get('strength')}, recent high={sweep.get('recent_high')}, recent low={sweep.get('recent_low')})"
        )
    elif sweep.get("recent_high") is not None:
        lines.append(f"- Liquidity Sweep: none detected; recent high={sweep.get('recent_high')}, recent low={sweep.get('recent_low')}")

    if len(lines) == 1:
        return ""
    lines.append(
        "Use VWAP and volume-profile levels to judge fair value, POC/HVN as likely magnets, "
        "LVN as fast-move zones, session highs/lows as liquidity pools, and sweep levels as structural invalidation or TP candidates."
    )
    return "\n" + "\n".join(lines) + "\n"


def _build_user_prompt(
    signal: TradingViewSignal,
    market: MarketContext,
    smc_text: str = "",
    user_settings: dict | None = None,
) -> str:
    """Build the user prompt with signal, market data, and SMC analysis."""
    risk_config = _effective_risk_config(user_settings)
    tp_config = _effective_take_profit_config(user_settings)
    ts_config = _effective_trailing_stop_config(user_settings)
    prefilter_summary = ((user_settings or {}).get("_prefilter_summary") or {}) if isinstance(user_settings, dict) else {}
    entry_exit_indicator_section = _format_entry_exit_indicators(market)

    missing_data_items = []
    if market.current_price <= 0:
        missing_data_items.append("current price")
    if market.volume_24h <= 0:
        missing_data_items.append("24h volume")
    if market.atr_pct is None:
        missing_data_items.append("ATR")
    if market.rsi_1h is None:
        missing_data_items.append("RSI")
    if market.bid_ask_spread <= 0:
        missing_data_items.append("bid-ask spread")
    if market.orderbook_imbalance is None:
        missing_data_items.append("orderbook imbalance")
    if market.funding_rate is None:
        missing_data_items.append("funding rate")

    missing_data_section = ""
    if missing_data_items:
        missing_data_section = f"""
## ⚠️ Market Data Limitations
This ticker may be a less common trading instrument (e.g., stock token, precious metal, or niche asset).
The following market metrics are unavailable: {', '.join(missing_data_items)}.
When market data is limited:
- Base your analysis primarily on the signal price, direction, timeframe, and strategy context
- You may still recommend "execute" if the signal appears reasonable, but use lower confidence
- Set warnings to inform about missing data limitations
- Use simpler stop-loss and take-profit estimates based on typical asset volatility for this type of instrument
- DO NOT automatically reject solely due to missing market data; evaluate what information IS available
- IMPORTANT: You MUST provide suggested_stop_loss even if ATR is unavailable - use 1.5% distance as fallback for low-volatility assets, or 2-3% for volatile assets
- IMPORTANT: You MUST provide suggested_tp1 (at least one take-profit target) - use 3-5% distance as fallback
"""

    tp_section = f"""
## Take-Profit Configuration
- Active TP Levels: {tp_config.num_levels}
- TP1 Target %: {tp_config.tp1_pct}% (Close {tp_config.tp1_qty}%)
- TP2 Target %: {tp_config.tp2_pct}% (Close {tp_config.tp2_qty}%)
- TP3 Target %: {tp_config.tp3_pct}% (Close {tp_config.tp3_qty}%)
- TP4 Target %: {tp_config.tp4_pct}% (Close {tp_config.tp4_qty}%)"""

    ts_section = f"""
## Trailing Stop Configuration
- Mode: {ts_config.mode}
- Trail Distance %: {ts_config.trail_pct}%
- Activation Profit %: {ts_config.activation_profit_pct}%
- Trailing Step %: {ts_config.trailing_step_pct}%"""

    prefilter_lines = []
    if prefilter_summary:
        score = prefilter_summary.get("score")
        if score is not None:
            prefilter_lines.append(f"- Pre-filter Score: {score}")
        hard_fail_count = prefilter_summary.get("hard_fail_count")
        if hard_fail_count is not None:
            prefilter_lines.append(f"- Hard Fails Before AI: {hard_fail_count}")
        soft_fail_count = prefilter_summary.get("soft_fail_count")
        if soft_fail_count is not None:
            prefilter_lines.append(f"- Soft Fails Before AI: {soft_fail_count}")
        missing_data_count = prefilter_summary.get("missing_data_count")
        if missing_data_count is not None and missing_data_count > 0:
            prefilter_lines.append(f"- Checks Skipped (Missing Data): {missing_data_count}")
        notable = prefilter_summary.get("notable_checks") or []
        if notable:
            prefilter_lines.append(f"- Notable Checks: {'; '.join(str(item) for item in notable)}")

    prefilter_section = ""
    if prefilter_lines:
        prefilter_section = "\n## Pre-Filter Context\n" + "\n".join(prefilter_lines)

    exit_instructions = [
        "IMPORTANT: The server expects EXACTLY "
        f"{tp_config.num_levels} take-profit targets.",
        f"- Generate valid prices for suggested_tp1 through suggested_tp{tp_config.num_levels}",
        "- If recommendation is 'execute' or 'modify', suggested_stop_loss must be a finite numeric price on the protective side of the final entry and should reflect your multi-timeframe invalidation level",
        "- For LONG: stop loss must be below final entry; for SHORT: stop loss must be above final entry",
        "- ATR/timeframe SL ranges are advisory only; do not change a stronger structural stop just to fit a fixed percentage range",
        "- For recommendation='modify', calculate suggested_stop_loss and TP levels from suggested_entry, not from the raw signal price",
        "- If no valid stop loss can be calculated, use recommendation='reject' instead of execute/modify",
        "- Use recommendation='modify' only if you also provide a valid suggested_entry different from the raw signal price",
        "- If recommendation is 'execute', suggested_entry may be null when the signal price is already acceptable",
        "- If recommendation is 'reject' or 'hold', set suggested_entry, suggested_stop_loss, and all TP fields to null",
        "- If pre-filter context shows stretched or weak conditions, lower confidence and risk instead of ignoring it",
    ]
    if tp_config.num_levels < 4:
        exit_instructions.extend([
            f"- Set suggested_tp{tp_config.num_levels + 1} through suggested_tp4 to null (not a number, but null)",
            f"- Set tp{tp_config.num_levels + 1}_qty_pct through tp4_qty_pct to 0",
        ])
    exit_instructions.append(
        f"- The first {tp_config.num_levels} TP quantities should sum to 100% or less"
    )

    if risk_config.exit_management_mode == "ai":
        exit_instruction_text = "\n".join(exit_instructions)
    else:
        exit_instruction_text = (
            "IMPORTANT: The server is using custom fixed exits. You may comment on risk and trade quality, "
            "but AI stop-loss and take-profit prices will be ignored."
        )

    # Add timeframe-specific SL/TP guidance
    from timeframe_exits import format_timeframe_exit_instructions
    timeframe_instruction = format_timeframe_exit_instructions(str(signal.timeframe or "60"))

    return f"""Analyze this trading signal:

## Signal
- Ticker: {signal.ticker}
- Direction: {signal.direction.value}
- Signal Price: {signal.price}
- Timeframe: {signal.timeframe}
- Strategy: {signal.strategy}
- Message: {signal.message}
{missing_data_section}

## Current Market Context
- Current Price: {market.current_price}
- Price Change 1h: {market.price_change_1h:+.4f}%
- Price Change 4h: {market.price_change_4h:+.4f}%
- Price Change 24h: {market.price_change_24h:+.4f}%
- 24h Volume: ${market.volume_24h:,.0f}
- Volume vs Avg: {market.volume_change_pct:+.2f}%
- 24h High: {market.high_24h}
- 24h Low: {market.low_24h}
- Bid-Ask Spread: {market.bid_ask_spread:.6f}%
- Funding Rate: {market.funding_rate if market.funding_rate is not None else 'N/A'}
- RSI (1h): {market.rsi_1h if market.rsi_1h is not None else 'N/A'}
- ATR%: {market.atr_pct if market.atr_pct is not None else 'N/A'}%
- EMA Fast: {market.ema_fast if market.ema_fast is not None else 'N/A'}
- EMA Slow: {market.ema_slow if market.ema_slow is not None else 'N/A'}
- Orderbook Imbalance (bid/ask): {market.orderbook_imbalance if market.orderbook_imbalance is not None else 'N/A'}
{entry_exit_indicator_section}
{tp_section}
{ts_section}
{prefilter_section}

{timeframe_instruction}

{exit_instruction_text}

{smc_text}
Should this signal be executed, modified, or rejected? If the entry price is suboptimal based on SMC analysis, recommend "modify" and provide a better suggested_entry price. Provide your analysis as JSON."""


def validate_ai_analysis_against_signal(
    signal: TradingViewSignal,
    market: MarketContext,
    analysis: AIAnalysis,
    user_settings: dict | None = None,
) -> AIAnalysis:
    """Sanitize AI exits against the final signal direction before caching/use."""
    if analysis.recommendation not in {"execute", "modify"}:
        return analysis

    risk_config = _effective_risk_config(user_settings)
    if risk_config.exit_management_mode != "ai":
        return analysis

    result = analysis.model_copy(deep=True)
    entry = result.suggested_entry if result.recommendation == "modify" and result.suggested_entry else signal.price
    entry = safe_float(entry, safe_float(market.current_price, 0.0))
    if entry <= 0:
        result.recommendation = "reject"
        result.reasoning = f"[AI SAFETY] Cannot validate exits without a valid entry price. Original: {result.reasoning}"
        return result

    is_long = signal.direction == SignalDirection.LONG
    is_short = signal.direction == SignalDirection.SHORT
    if not (is_long or is_short):
        return result

    def _finite_positive(value: float | None) -> float | None:
        parsed = safe_float(value, 0.0)
        return parsed if parsed > 0 and math.isfinite(parsed) else None

    def _valid_sl(value: float | None) -> bool:
        parsed = _finite_positive(value)
        if parsed is None:
            return False
        return parsed < entry if is_long else parsed > entry

    def _valid_tp(value: float | None) -> bool:
        parsed = _finite_positive(value)
        if parsed is None:
            return False
        return parsed > entry if is_long else parsed < entry

    if not _valid_sl(result.suggested_stop_loss):
        result.warnings.append("AI stop loss failed post-validation; server fallback will be used if possible")
        result.suggested_stop_loss = None

    tp_fields = ["suggested_tp1", "suggested_tp2", "suggested_tp3", "suggested_tp4"]
    valid_tp_count = 0
    for field in tp_fields:
        if _valid_tp(getattr(result, field)):
            valid_tp_count += 1
        else:
            setattr(result, field, None)

    if valid_tp_count == 0 and _valid_tp(result.suggested_take_profit):
        result.suggested_tp1 = result.suggested_take_profit
        result.tp1_qty_pct = 100.0
        result.tp2_qty_pct = result.tp3_qty_pct = result.tp4_qty_pct = 0.0
        valid_tp_count = 1

    if valid_tp_count == 0:
        result.recommendation = "reject"
        result.reasoning = f"[AI SAFETY] No valid take-profit target for execute/modify. Original: {result.reasoning}"

    return result


# ─────────────────────────────────────────────
# Retry helper
# ─────────────────────────────────────────────

async def _with_retry(coro_factory: Callable[[], Awaitable[str]], label: str) -> str:
    """
    Execute an async coroutine factory with exponential-backoff retry.
    Retries on rate-limit, server errors, and transient network failures.
    Uses global semaphore to limit concurrent AI API calls.
    """
    async with _AI_SEMAPHORE:
        last_exc: Exception = RuntimeError(f"[AI/{label}] No attempts made (_AI_MAX_RETRIES={_AI_MAX_RETRIES})")
        for attempt in range(max(_AI_MAX_RETRIES, 1)):
            try:
                return await coro_factory()
            except httpx.HTTPStatusError as exc:
                last_exc = exc
                if exc.response.status_code in _RETRYABLE_STATUS_CODES and attempt < _AI_MAX_RETRIES - 1:
                    delay = _AI_BASE_DELAY * (2 ** attempt)
                    logger.warning(
                        f"[AI/{label}] HTTP {exc.response.status_code}, "
                        f"retrying in {delay:.1f}s (attempt {attempt + 1}/{_AI_MAX_RETRIES})"
                    )
                    await asyncio.sleep(delay)
                else:
                    raise
            except httpx.NetworkError as exc:
                last_exc = exc
                if attempt < _AI_MAX_RETRIES - 1:
                    delay = _AI_BASE_DELAY * (2 ** attempt)
                    logger.warning(
                        f"[AI/{label}] Network error, "
                        f"retrying in {delay:.1f}s (attempt {attempt + 1}/{_AI_MAX_RETRIES})"
                    )
                    await asyncio.sleep(delay)
                else:
                    raise
        raise last_exc  # unreachable but satisfies type checkers


# ─────────────────────────────────────────────
# Provider implementations
# ─────────────────────────────────────────────

async def _call_openai(system: str, user: str, model: str | None = None) -> str:
    """Call OpenAI/compatible API with automatic retry."""
    model_name = model or settings.ai.openai_model
    async def _do() -> str:
        async with httpx.AsyncClient(timeout=_AI_TIMEOUT) as client:
            resp = await client.post(
                "https://api.openai.com/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {settings.ai.openai_api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": model_name,
                    "messages": [
                        {"role": "system", "content": system},
                        {"role": "user", "content": user},
                    ],
                    "temperature": settings.ai.temperature,
                    "max_tokens": settings.ai.max_tokens,
                    "response_format": {"type": "json_object"},
                },
            )
            resp.raise_for_status()
            data = resp.json()
            pt, ct, tt = extract_usage_from_response(data)
            ai_costs.record("openai", model_name, pt, ct, tt)
            content = data["choices"][0]["message"]["content"]
            if content is None:
                raise ValueError(f"OpenAI API returned null content for model {model_name}")
            return str(content)

    return await _with_retry(_do, "openai")


async def _call_anthropic(system: str, user: str, model: str | None = None) -> str:
    """Call Anthropic Claude API with automatic retry."""
    model_name = model or settings.ai.anthropic_model
    async def _do() -> str:
        async with httpx.AsyncClient(timeout=_AI_TIMEOUT) as client:
            resp = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": settings.ai.anthropic_api_key,
                    "anthropic-version": "2023-06-01",
                    "Content-Type": "application/json",
                },
                json={
                    "model": model_name,
                    "max_tokens": settings.ai.max_tokens,
                    "system": system,
                    "messages": [
                        {"role": "user", "content": user},
                    ],
                    "temperature": settings.ai.temperature,
                },
            )
            resp.raise_for_status()
            data = resp.json()
            content = data["content"][0]["text"]
            if content is None:
                raise ValueError(f"Anthropic API returned null content for model {model_name}")
            return str(content)

    return await _with_retry(_do, "anthropic")


async def _call_deepseek(system: str, user: str, model: str | None = None) -> str:
    """Call DeepSeek API (OpenAI-compatible) with automatic retry."""
    model_name = model or settings.ai.deepseek_model
    async def _do() -> str:
        async with httpx.AsyncClient(timeout=_AI_TIMEOUT) as client:
            resp = await client.post(
                "https://api.deepseek.com/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {settings.ai.deepseek_api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": model_name,
                    "messages": [
                        {"role": "system", "content": system},
                        {"role": "user", "content": user},
                    ],
                    "temperature": settings.ai.temperature,
                    "max_tokens": settings.ai.max_tokens,
                },
            )
            resp.raise_for_status()
            data = resp.json()
            pt, ct, tt = extract_usage_from_response(data)
            ai_costs.record("deepseek", model_name, pt, ct, tt)
            content = data["choices"][0]["message"]["content"]
            if content is None:
                raise ValueError(f"DeepSeek API returned null content for model {model_name}")
            return str(content)

    return await _with_retry(_do, "deepseek")


async def _call_mistral(system: str, user: str, model: str | None = None) -> str:
    """Call Mistral API (OpenAI-compatible) with automatic retry."""
    model_name = model or settings.ai.mistral_model
    async def _do() -> str:
        async with httpx.AsyncClient(timeout=_AI_TIMEOUT) as client:
            resp = await client.post(
                "https://api.mistral.ai/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {settings.ai.mistral_api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": model_name,
                    "messages": [
                        {"role": "system", "content": system},
                        {"role": "user", "content": user},
                    ],
                    "temperature": settings.ai.temperature,
                    "max_tokens": settings.ai.max_tokens,
                    "response_format": {"type": "json_object"},
                },
            )
            resp.raise_for_status()
            data = resp.json()
            pt, ct, tt = extract_usage_from_response(data)
            ai_costs.record("mistral", model_name, pt, ct, tt)
            content = data["choices"][0]["message"]["content"]
            if content is None:
                raise ValueError(f"Mistral API returned null content for model {model_name}")
            return str(content)

    return await _with_retry(_do, "mistral")


async def _call_openrouter(system: str, user: str, model: str | None = None) -> str:
    """Call OpenRouter's OpenAI-compatible chat completions API."""
    model_name = model or settings.ai.openrouter_model
    async def _do() -> str:
        if not settings.ai.openrouter_api_key:
            raise ValueError("OpenRouter API key is not configured")

        headers = {
            "Authorization": f"Bearer {settings.ai.openrouter_api_key}",
            "Content-Type": "application/json",
        }
        if settings.ai.openrouter_site_url:
            headers["HTTP-Referer"] = settings.ai.openrouter_site_url
        if settings.ai.openrouter_app_name:
            headers["X-Title"] = settings.ai.openrouter_app_name

        async with httpx.AsyncClient(timeout=_AI_TIMEOUT) as client:
            resp = await client.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers=headers,
                json={
                    "model": model_name,
                    "messages": [
                        {"role": "system", "content": system},
                        {"role": "user", "content": user},
                    ],
                    "temperature": settings.ai.temperature,
                    "max_tokens": settings.ai.max_tokens,
                },
            )
            resp.raise_for_status()
            data = resp.json()
            pt, ct, tt = extract_usage_from_response(data)
            ai_costs.record("openrouter", model_name, pt, ct, tt)
            content = data["choices"][0]["message"]["content"]
            if content is None:
                raise ValueError(f"OpenRouter API returned null content for model {model_name}")
            return str(content)

    return await _with_retry(_do, "openrouter")


async def _call_custom(system: str, user: str, model: str | None = None) -> str:
    """Call custom AI provider API with automatic retry."""
    model_name = model or settings.ai.custom_provider_model or "gpt-3.5-turbo"
    async def _do() -> str:
        async with httpx.AsyncClient(timeout=_AI_TIMEOUT) as client:
            if not settings.ai.custom_provider_api_url:
                raise ValueError("Custom AI provider API URL is not configured")
            if not settings.ai.custom_provider_api_key:
                raise ValueError("Custom AI provider API key is not configured")

            payload = {
                "model": model_name,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                "temperature": settings.ai.temperature,
                "max_tokens": settings.ai.max_tokens,
            }

            url = settings.ai.custom_provider_api_url.rstrip("/")
            if not url.endswith("/chat/completions"):
                url += "/chat/completions"

            resp = await client.post(
                url,
                headers={
                    "Authorization": f"Bearer {settings.ai.custom_provider_api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
            )
            resp.raise_for_status()
            data = resp.json()

            if "choices" in data and len(data["choices"]) > 0:
                content = data["choices"][0]["message"]["content"]
                if content is None:
                    raise ValueError(f"Custom API returned null content for model {model_name}")
                return str(content)
            elif "content" in data and len(data["content"]) > 0:
                text = data["content"][0]["text"]
                if text is None:
                    raise ValueError(f"Custom API returned null text for model {model_name}")
                return str(text)
            else:
                if "text" in data:
                    return str(data["text"])
                elif "response" in data:
                    return str(data["response"])
                elif "message" in data:
                    return str(data["message"])
                else:
                    raise ValueError(f"Unexpected response format: {data}")

    return await _with_retry(_do, settings.ai.custom_provider_name)


# ─────────────────────────────────────────────
# Main analysis function
# ─────────────────────────────────────────────

def _parse_model_id(model_id: str) -> tuple[str, str]:
    """
    Parse model ID in format 'provider/model_name' or legacy formats.
    Returns (provider, model_name).
    """
    model_id = model_id.strip().lower()
    if not model_id:
        return "", ""

    if "/" in model_id:
        provider, model = model_id.split("/", 1)
        return provider.strip(), model.strip()

    if ":" in model_id:
        provider, model = model_id.split(":", 1)
        return provider.strip(), model.strip()

    legacy_providers = {"openai", "anthropic", "deepseek", "openrouter", "custom", "mistral"}
    if model_id in legacy_providers:
        return model_id, ""

    return "", model_id


async def _call_model_by_id(model_id: str, system: str, user: str) -> tuple[str, str]:
    """
    Call a model by its ID (provider/model_name format).
    Returns (raw_response, model_id).
    """
    provider, model_name = _parse_model_id(model_id)
    if not provider and model_name:
        provider = "openrouter"

    if provider == "openai":
        raw = await _call_openai(system, user, model=model_name)
        return raw, model_id

    elif provider == "anthropic":
        raw = await _call_anthropic(system, user, model=model_name)
        return raw, model_id

    elif provider == "deepseek":
        raw = await _call_deepseek(system, user, model=model_name)
        return raw, model_id

    elif provider == "mistral":
        raw = await _call_mistral(system, user, model=model_name)
        return raw, model_id

    elif provider == "openrouter":
        raw = await _call_openrouter(system, user, model=model_name)
        return raw, model_id

    elif provider == "custom":
        raw = await _call_custom(system, user, model=model_name)
        return raw, model_id

    elif provider == "local":
        return _local_rule_analysis(system, user), model_id

    else:
        raise ValueError(f"Unknown provider in model ID: {model_id}")


def _local_rule_analysis(system: str, user: str) -> str:
    """
    Local rule-based fallback when no AI models are available.

    Analyzes the signal context using simple rules:
    - Check signal price vs market context hints
    - Apply conservative risk defaults
    - Provide basic directional validation

    Returns a JSON analysis based on extracted signal data.
    """
    import json
    import re

    # Try to extract signal data from user prompt
    signal_price = 0.0
    signal_direction = "long"

    try:
        # Extract price
        price_match = re.search(r"Signal Price:\s*([\d.]+)", user)
        if price_match:
            signal_price = float(price_match.group(1))

        # Extract direction
        dir_match = re.search(r"Direction:\s*(\w+)", user)
        if dir_match:
            signal_direction = dir_match.group(1).lower()

        # Extract ticker
        ticker_match = re.search(r"Ticker:\s*(\w+)", user)
        if ticker_match:
            ticker_match.group(1).upper()

        # Extract timeframe
        tf_match = re.search(r"Timeframe:\s*(\w+)", user)
        if tf_match:
            tf_match.group(1)

    except (TypeError, AttributeError):
        pass
    except Exception:
        pass

    # Extract market context data
    current_price = signal_price
    price_change_24h = 0.0
    rsi_1h = 50.0
    atr_pct = 2.0

    try:
        # Current price
        cp_match = re.search(r"Current Price:\s*([\d.]+)", user)
        if cp_match:
            current_price = float(cp_match.group(1))

        # 24h change
        ch_match = re.search(r"Price Change 24h:\s*([+-]?[\d.]+)%", user)
        if ch_match:
            price_change_24h = float(ch_match.group(1))

        # RSI
        rsi_match = re.search(r"RSI \(1h\):\s*([\d.]+)", user)
        if rsi_match:
            rsi_1h = float(rsi_match.group(1))

        # ATR
        atr_match = re.search(r"ATR%:\s*([\d.]+)%", user)
        if atr_match:
            atr_pct = float(atr_match.group(1))

    except (TypeError, ValueError, AttributeError):
        pass
    except Exception:
        pass

    # Apply intelligent rules
    warnings = ["Local fallback mode - AI API unavailable"]
    recommendation = "hold"
    confidence = 0.4
    reasoning = "AI model unavailable. Applying local risk rules."

    # Rule 1: Extreme RSI conditions
    if signal_direction == "long" and rsi_1h > 75:
        warnings.append(f"RSI overbought ({rsi_1h:.0f}) - cautious for LONG")
        confidence = max(0.2, confidence - 0.1)
    elif signal_direction == "short" and rsi_1h < 25:
        warnings.append(f"RSI oversold ({rsi_1h:.0f}) - cautious for SHORT")
        confidence = max(0.2, confidence - 0.1)

    # Rule 2: Large 24h move - volatility warning
    if abs(price_change_24h) > 5:
        warnings.append(f"Large 24h move ({price_change_24h:+.1f}%) - elevated volatility")
        confidence = max(0.2, confidence - 0.1)

    # Rule 3: Price alignment check
    if current_price > 0 and signal_price > 0:
        price_diff_pct = abs(current_price - signal_price) / current_price * 100
        if price_diff_pct > 2:
            warnings.append(f"Signal price differs {price_diff_pct:.1f}% from market - verify data")

    # Calculate conservative SL/TP based on ATR
    suggested_stop_loss = None
    suggested_tp1 = None

    # BUG FIX: When ATR is missing, use fallback default percentage (1.5% SL, 3% TP)
    # This prevents "No valid stop loss" error when market data is unavailable
    sl_distance_pct = atr_pct * 1.5 if atr_pct > 0 else 1.5  # Default 1.5% SL distance
    tp_distance_pct = atr_pct * 2.0 if atr_pct > 0 else 3.0  # Default 3% TP distance

    if signal_price > 0:
        if signal_direction == "long":
            suggested_stop_loss = round(signal_price * (1 - sl_distance_pct / 100), 4)
            suggested_tp1 = round(signal_price * (1 + tp_distance_pct / 100), 4)
        else:
            suggested_stop_loss = round(signal_price * (1 + sl_distance_pct / 100), 4)
            suggested_tp1 = round(signal_price * (1 - tp_distance_pct / 100), 4)

        # Always recommend execute when we have valid SL/TP (even without ATR)
        if suggested_stop_loss and suggested_tp1:
            recommendation = "execute"
            if atr_pct > 0:
                reasoning = (
                    f"Local fallback: Conservative SL/TP calculated from ATR ({atr_pct:.1f}%). "
                    f"SL={suggested_stop_loss}, TP={suggested_tp1}. "
                    f"Recommend manual review before execution."
                )
            else:
                reasoning = (
                    f"Local fallback: Default SL/TP used (ATR unavailable). "
                    f"SL={suggested_stop_loss} ({sl_distance_pct:.1f}% distance), TP={suggested_tp1} ({tp_distance_pct:.1f}% distance). "
                    f"Recommend manual review - verify market conditions."
                )
                warnings.append("ATR unavailable - using default stop distance")
            confidence = 0.5
            warnings.append("Conservative position sizing recommended")

    # Adjust leverage based on volatility
    recommended_leverage = 3.0
    if atr_pct > 4.0:
        recommended_leverage = 1.0
        warnings.append("High volatility - use minimal leverage")
    elif atr_pct > 2.5:
        recommended_leverage = 2.0

    return json.dumps({
        "confidence": confidence,
        "recommendation": recommendation,
        "reasoning": reasoning,
        "suggested_direction": None,
        "suggested_entry": None,
        "suggested_stop_loss": suggested_stop_loss,
        "suggested_take_profit": suggested_tp1,
        "suggested_tp1": suggested_tp1,
        "suggested_tp2": None,
        "suggested_tp3": None,
        "suggested_tp4": None,
        "tp1_qty_pct": 100.0,
        "tp2_qty_pct": 0.0,
        "tp3_qty_pct": 0.0,
        "tp4_qty_pct": 0.0,
        "position_size_pct": 0.3,  # Conservative sizing in fallback
        "recommended_leverage": recommended_leverage,
        "risk_score": 0.6,
        "market_condition": "volatile" if atr_pct > 3.0 else "normal",
        "warnings": warnings,
    })


def _aggregate_legacy_voting_results(
    results: list[dict[str, object]],
    weights: dict[str, float],
    strategy: str,
) -> dict[str, object]:
    """Compatibility adapter for older dict-based voting tests/tools."""
    def _legacy_confidence(value: object) -> float:
        if isinstance(value, bool):
            return float(value)
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, str):
            try:
                return float(value)
            except ValueError:
                return 0.0
        return 0.0

    if not results:
        return {"action": "hold", "confidence": 0.0, "reason": "No voting results available"}

    model_ids = list(weights.keys()) or [f"model{i + 1}" for i in range(len(results))]
    ordered_weights = [float(weights.get(model_ids[i], 1.0)) if i < len(model_ids) else 1.0 for i in range(len(results))]
    total_weight = sum(ordered_weights) or float(len(results))

    if strategy == "best_confidence":
        return max(results, key=lambda item: _legacy_confidence(item.get("confidence")))

    if strategy == "consensus":
        counts: dict[str, int] = {}
        for result in results:
            action = result.get("action", "hold")
            action_key = str(action)
            counts[action_key] = counts.get(action_key, 0) + 1
        action = max(counts, key=lambda action_key: counts[action_key])
        if counts[action] <= len(results) / 2:
            return {"action": "hold", "confidence": 0.0, "reason": "Consensus not reached"}
        confidence_total = 0.0
        for result in results:
            if str(result.get("action", "hold")) == action:
                confidence_total += _legacy_confidence(result.get("confidence"))
        confidence = confidence_total / counts[action]
        return {"action": action, "confidence": confidence, "reason": "Consensus reached"}

    action_weights: dict[str, float] = {}
    weighted_confidence = 0.0
    for result, weight in zip(results, ordered_weights, strict=False):
        normalized = weight / total_weight
        action = str(result.get("action", "hold"))
        action_weights[action] = action_weights.get(action, 0.0) + normalized
        weighted_confidence += _legacy_confidence(result.get("confidence")) * normalized

    action = max(action_weights, key=lambda action_key: action_weights[action_key])
    return {"action": action, "confidence": weighted_confidence, "reason": "Weighted voting aggregate"}


def _analysis_has_exit_plan(analysis: AIAnalysis) -> bool:
    if not analysis.suggested_stop_loss or analysis.suggested_stop_loss <= 0:
        return False
    return any(
        tp and tp > 0
        for tp in (analysis.suggested_tp1, analysis.suggested_tp2, analysis.suggested_tp3, analysis.suggested_tp4)
    )


def _best_exit_source(analyses: list[AIAnalysis], recommendation: str) -> AIAnalysis | None:
    candidates = [
        analysis
        for analysis in analyses
        if analysis.recommendation == recommendation and _analysis_has_exit_plan(analysis)
    ]
    if not candidates and recommendation in {"execute", "modify"}:
        candidates = [analysis for analysis in analyses if _analysis_has_exit_plan(analysis)]
    if not candidates:
        return None
    return max(candidates, key=lambda item: item.confidence)


def _aggregate_voting_results(
    results: Any,
    strategy: Any,
    weights: Any = None,
) -> Coroutine[Any, Any, AIAnalysis] | dict[str, object]:
    """Aggregate voting results, supporting current async and legacy sync callers."""
    if isinstance(strategy, dict) and isinstance(weights, str):
        return _aggregate_legacy_voting_results(results, strategy, weights)
    return _aggregate_voting_results_async(results, strategy, weights or {})


async def _aggregate_voting_results_async(
    results: list[tuple[AIAnalysis, str]],
    strategy: str,
    weights: dict[str, float],
) -> AIAnalysis:
    """
    Aggregate multiple AI analysis results using voting strategy.

    Strategies:
    - weighted: Weighted average of confidence, vote on recommendation
    - consensus: Only proceed if majority agrees on execute
    - best_confidence: Take result from highest confidence model

    results: list of (AIAnalysis, model_id)
    """
    if not results:
        return _fallback_analysis("No voting results available")

    if len(results) == 1:
        return results[0][0]

    analyses = [r[0] for r in results]
    model_ids = [r[1] for r in results]

    if strategy == "best_confidence":
        best_idx = max(range(len(analyses)), key=lambda i: analyses[i].confidence)
        logger.info(
            f"[AI/Voting] Best confidence strategy: selected {model_ids[best_idx]} "
            f"(confidence={analyses[best_idx].confidence:.2f})"
        )
        return analyses[best_idx]

    if strategy == "consensus":
        execute_votes = sum(1 for a in analyses if a.recommendation == "execute")
        majority_threshold = len(analyses) / 2

        if execute_votes > majority_threshold:
            avg_confidence = sum(a.confidence for a in analyses) / len(analyses)
            execute_candidates = [a for a in analyses if a.recommendation == "execute"]
            exit_source = _best_exit_source(execute_candidates, "execute")
            result = (exit_source or max(execute_candidates, key=lambda a: a.confidence)).model_copy(deep=True)
            result.recommendation = "execute"
            result.confidence = avg_confidence
            result.reasoning = f"Consensus reached: {execute_votes}/{len(analyses)} vote execute. {result.reasoning}"
            logger.info(
                f"[AI/Voting] Consensus reached: {execute_votes}/{len(analyses)} vote execute"
            )
            return result
        else:
            non_execute_votes = {}
            for a in analyses:
                rec = a.recommendation
                if rec != "execute":
                    non_execute_votes[rec] = non_execute_votes.get(rec, 0) + 1
            if non_execute_votes:
                final_rec = max(non_execute_votes, key=lambda key: non_execute_votes[key])
            else:
                final_rec = "hold"
            best_idx = max(range(len(analyses)), key=lambda i: analyses[i].confidence)
            result = analyses[best_idx].model_copy(deep=True)
            result.recommendation = final_rec
            if final_rec not in {"execute", "modify"}:
                result.suggested_entry = None
                result.suggested_stop_loss = None
                result.suggested_take_profit = None
                result.suggested_tp1 = None
                result.suggested_tp2 = None
                result.suggested_tp3 = None
                result.suggested_tp4 = None
                result.tp1_qty_pct = result.tp2_qty_pct = result.tp3_qty_pct = result.tp4_qty_pct = 0.0
            result.reasoning = f"Consensus failed: only {execute_votes}/{len(analyses)} vote execute. Majority is {final_rec}. {result.reasoning}"
            result.warnings = ["Voting consensus not reached"] + result.warnings
            logger.info(
                f"[AI/Voting] Consensus failed: {execute_votes}/{len(analyses)} vote execute, falling back to {final_rec}"
            )
            return result

    if strategy == "weighted":
        total_weight = sum(weights.get(mid, 1.0) for mid in model_ids)
        if total_weight <= 0:
            total_weight = len(analyses)

        weighted_confidence = 0.0
        weighted_risk = 0.0
        weighted_position_pct = 0.0
        weighted_leverage = 0.0

        recommendation_votes: dict[str, float] = {}
        direction_votes: dict[str, float] = {}

        for analysis, model_id in results:
            w = weights.get(model_id, 1.0) / total_weight

            weighted_confidence += analysis.confidence * w
            weighted_risk += analysis.risk_score * w
            weighted_position_pct += analysis.position_size_pct * w
            weighted_leverage += analysis.recommended_leverage * w

            rec = analysis.recommendation
            recommendation_votes[rec] = recommendation_votes.get(rec, 0) + w

            if analysis.suggested_direction:
                dir_key = analysis.suggested_direction.value if hasattr(analysis.suggested_direction, 'value') else str(analysis.suggested_direction)
                direction_votes[dir_key] = direction_votes.get(dir_key, 0) + w

        final_recommendation = max(recommendation_votes, key=lambda key: recommendation_votes[key])

        best_idx = max(range(len(analyses)), key=lambda i: analyses[i].confidence)
        best_analysis = analyses[best_idx]
        exit_source = _best_exit_source(analyses, final_recommendation) or best_analysis
        include_exits = final_recommendation in {"execute", "modify"}

        combined_reasoning = f"Weighted voting ({len(results)} models): "
        combined_reasoning += ", ".join(f"{mid}:{a.recommendation}({a.confidence:.2f})" for a, mid in results)

        result = AIAnalysis(
            confidence=weighted_confidence,
            recommendation=final_recommendation,
            reasoning=combined_reasoning,
            suggested_direction=exit_source.suggested_direction if include_exits else None,
            suggested_entry=exit_source.suggested_entry if include_exits else None,
            suggested_stop_loss=exit_source.suggested_stop_loss if include_exits else None,
            suggested_take_profit=exit_source.suggested_take_profit if include_exits else None,
            suggested_tp1=exit_source.suggested_tp1 if include_exits else None,
            suggested_tp2=exit_source.suggested_tp2 if include_exits else None,
            suggested_tp3=exit_source.suggested_tp3 if include_exits else None,
            suggested_tp4=exit_source.suggested_tp4 if include_exits else None,
            tp1_qty_pct=exit_source.tp1_qty_pct if include_exits else 0.0,
            tp2_qty_pct=exit_source.tp2_qty_pct if include_exits else 0.0,
            tp3_qty_pct=exit_source.tp3_qty_pct if include_exits else 0.0,
            tp4_qty_pct=exit_source.tp4_qty_pct if include_exits else 0.0,
            position_size_pct=min(weighted_position_pct, 1.0),
            recommended_leverage=min(weighted_leverage, 50.0),
            risk_score=weighted_risk,
            market_condition=exit_source.market_condition,
            trend_strength=exit_source.trend_strength,
            recommended_trailing_stop_mode=exit_source.recommended_trailing_stop_mode,
            warnings=[f"Voting result from {len(results)} models"] + exit_source.warnings,
            raw_response=f"Voting aggregate: {combined_reasoning}",
        )

        logger.info(
            f"[AI/Voting] Weighted aggregate: confidence={weighted_confidence:.2f}, "
            f"recommendation={final_recommendation}, models={model_ids}"
        )
        return result

    return analyses[0]


async def analyze_signal(
    signal: TradingViewSignal,
    market: MarketContext,
    user_settings: dict | None = None,
) -> AIAnalysis:
    """
    Send signal + market context to LLM and parse the response.
    Includes multi-timeframe SMC/FVG analysis for optimal entry detection.
    Results are cached with dynamic TTL based on market volatility (Optimization 3).

    Voting mode: If voting_enabled, calls multiple models concurrently and aggregates.
    """
    # Optimization 3: Update volatility tracker for dynamic TTL
    await _update_volatility_tracker(signal.ticker, market)

    price_bucket = _price_to_bucket(market.current_price) if market.current_price > 0 else ""
    timeframe = str(signal.timeframe or "")
    config_signature = _analysis_config_signature(user_settings)

    # P2-8: Add OHLCV signature to cache key to detect market structure changes
    ohlcv_htf = getattr(market, "_ohlcv_4h", None) or getattr(market, "_ohlcv_1h", None) or []
    ohlcv_signature = _ohlcv_signature(ohlcv_htf, samples=3) if ohlcv_htf else ""
    cache_enabled = bool(price_bucket and ohlcv_signature)

    cache_key_suffix = ""
    if settings.ai.voting_enabled and settings.ai.voting_models:
        cache_key_suffix = ":voting"

    if cache_enabled:
        cached = await _get_cached_analysis(
            signal.ticker,
            signal.direction.value + cache_key_suffix,
            price_bucket,
            timeframe,
            config_signature,
            ohlcv_signature,  # P2-8: Include OHLCV signature
        )
        if cached is not None:
            logger.info(f"[AI] Using cached analysis for {signal.ticker} {signal.direction.value} @ {price_bucket}")
            return cached
    else:
        logger.debug(f"[AI/Cache] Bypassing cache for {signal.ticker}: missing price bucket or OHLCV signature")

    smc_text = ""
    try:
        from smc_analyzer import (
            MultiTimeframeSMC,
            check_htf_structure_conflict,
            find_confluence_zones,
            format_smc_for_ai,
            select_timeframes_for_signal,
        )

        # P0-3: Dynamic timeframe selection based on signal timeframe
        selected_tfs = select_timeframes_for_signal(str(signal.timeframe or "60"))
        ohlcv_htf = getattr(market, f"_ohlcv_{selected_tfs['htf']}", None) or []
        ohlcv_mtf = getattr(market, f"_ohlcv_{selected_tfs['mtf']}", None) or []
        ohlcv_stf = getattr(market, f"_ohlcv_{selected_tfs['stf']}", None) or []
        ohlcv_ltf = getattr(market, f"_ohlcv_{selected_tfs['ltf']}", None) or []

        direction = signal.direction.value if signal.direction else "long"

        # P1-R1: Get ATR percentage for dynamic premium/discount zones
        atr_pct = market.atr_pct or 0.0

        # P1-5: Analyze each timeframe with caching for performance
        # P1-R1: Pass atr_pct to enable dynamic zones based on volatility
        # P2-R4: Use minimum threshold 7 (all TFs need at least 7 candles)
        htf_ctx = await _cached_analyze_smc_single_tf(signal.ticker, ohlcv_htf, selected_tfs['htf'], market.current_price, direction, atr_pct) if len(ohlcv_htf) >= 7 else None
        mtf_ctx = await _cached_analyze_smc_single_tf(signal.ticker, ohlcv_mtf, selected_tfs['mtf'], market.current_price, direction, atr_pct) if len(ohlcv_mtf) >= 7 else None
        stf_ctx = await _cached_analyze_smc_single_tf(signal.ticker, ohlcv_stf, selected_tfs['stf'], market.current_price, direction, atr_pct) if len(ohlcv_stf) >= 7 else None
        ltf_ctx = await _cached_analyze_smc_single_tf(signal.ticker, ohlcv_ltf, selected_tfs['ltf'], market.current_price, direction, atr_pct) if len(ohlcv_ltf) >= 7 else None

        # Find confluence zones
        confluence = find_confluence_zones(htf_ctx, mtf_ctx, stf_ctx, ltf_ctx, direction, market.current_price)

        # P0-2: HTF structure conflict check
        htf_conflict, htf_conflict_type, htf_risk_penalty = check_htf_structure_conflict(htf_ctx, direction)

        # Calculate overall risk score
        overall_risk = sum(ctx.risk_score for ctx in [htf_ctx, mtf_ctx, stf_ctx, ltf_ctx] if ctx) / 4.0 if any([htf_ctx, mtf_ctx, stf_ctx, ltf_ctx]) else 0.5
        overall_risk += htf_risk_penalty

        mtf_smc = MultiTimeframeSMC(
            htf=htf_ctx,
            mtf=mtf_ctx,
            stf=stf_ctx,
            ltf=ltf_ctx,
            confluence_zones=confluence,
            overall_risk_score=round(min(1.0, overall_risk), 2),
            htf_conflict=htf_conflict,
            htf_conflict_type=htf_conflict_type,
        )
        smc_text = format_smc_for_ai(mtf_smc, direction, market.current_price)

        logger.info(
            f"[AI/SMC] {signal.ticker}: "
            f"TFs={selected_tfs}, FVGs={sum(len(c.fvgs) for c in [htf_ctx, mtf_ctx, stf_ctx, ltf_ctx] if c)}, "
            f"OBs={sum(len(c.order_blocks) for c in [htf_ctx, mtf_ctx, stf_ctx, ltf_ctx] if c)}, "
            f"Confluences={len(confluence)}, Risk={mtf_smc.overall_risk_score:.2f}, "
            f"HTF_Conflict={htf_conflict}"
        )
    except Exception as e:
        logger.warning(f"[AI/SMC] SMC analysis failed, proceeding without: {e}")

    system_prompt = _get_effective_system_prompt(user_settings)
    user_prompt = _build_user_prompt(signal, market, smc_text, user_settings)

    if settings.ai.voting_enabled and settings.ai.voting_models:
        logger.info(
            f"[AI/Voting] Starting multi-model voting for {signal.ticker} {signal.direction.value}: "
            f"{settings.ai.voting_models}"
        )

        voting_tasks: list[Awaitable[tuple[str, str]]] = []
        for model_id in settings.ai.voting_models:
            voting_tasks.append(_call_model_by_id(model_id, system_prompt, user_prompt))

        try:
            # Per-model timeout: don't let one slow model block the entire vote
            _voting_timeout = settings.ai.read_timeout_secs + 5
            raw_results = await asyncio.wait_for(
                asyncio.gather(*voting_tasks, return_exceptions=True),
                timeout=_voting_timeout,
            )

            valid_results: list[tuple[AIAnalysis, str]] = []
            for i, result in enumerate(raw_results):
                model_id = settings.ai.voting_models[i]
                if isinstance(result, BaseException):
                    logger.warning(f"[AI/Voting] Model {model_id} failed: {result}")
                    continue
                try:
                    raw_response, _returned_model_id = result
                    analysis = validate_ai_analysis_against_signal(
                        signal,
                        market,
                        _parse_response(raw_response),
                        user_settings,
                    )
                    valid_results.append((analysis, model_id))
                except Exception as e:
                    logger.warning(f"[AI/Voting] Failed to parse {model_id} response: {e}")

            if not valid_results:
                logger.error("[AI/Voting] All voting models failed")
                return _fallback_analysis("All voting models failed")

            final_analysis = await _aggregate_voting_results_async(
                valid_results,
                settings.ai.voting_strategy,
                settings.ai.voting_weights,
            )

            if cache_enabled:
                await _set_cached_analysis(
                    signal.ticker,
                    signal.direction.value + cache_key_suffix,
                    final_analysis,
                    price_bucket,
                    timeframe,
                    config_signature,
                    ohlcv_signature,  # P2-8: Include OHLCV signature
                )
            return final_analysis

        except asyncio.TimeoutError:
            logger.error(f"[AI/Voting] Voting timed out after {_voting_timeout}s")
            return _fallback_analysis(f"Voting timed out after {_voting_timeout}s")
        except Exception as e:
            logger.error(f"[AI/Voting] Voting failed: {e}")
            return _fallback_analysis(f"Voting error: {e}")

    provider = settings.ai.provider.lower()
    logger.info(f"[AI] Analyzing {signal.ticker} {signal.direction.value} via {provider}...")

    try:
        if provider == "openai":
            raw = await _call_openai(system_prompt, user_prompt)
        elif provider == "anthropic":
            raw = await _call_anthropic(system_prompt, user_prompt)
        elif provider == "deepseek":
            raw = await _call_deepseek(system_prompt, user_prompt)
        elif provider == "mistral":
            raw = await _call_mistral(system_prompt, user_prompt)
        elif provider == "openrouter":
            raw = await _call_openrouter(system_prompt, user_prompt)
        elif (
            settings.ai.custom_provider_enabled
            and provider in {"custom", settings.ai.custom_provider_name.lower()}
        ):
            raw = await _call_custom(system_prompt, user_prompt)
        else:
            raise ValueError(f"Unknown AI provider: {provider}")

        analysis = validate_ai_analysis_against_signal(signal, market, _parse_response(raw), user_settings)
        logger.info(
            f"[AI] Result: {analysis.recommendation} "
            f"(confidence={analysis.confidence:.2f}, risk={analysis.risk_score:.2f})"
        )
        if cache_enabled:
            await _set_cached_analysis(
                signal.ticker,
                signal.direction.value + cache_key_suffix,
                analysis,
                price_bucket,
                timeframe,
                config_signature,
                ohlcv_signature,  # P2-8: Include OHLCV signature
            )
        return analysis

    except httpx.HTTPStatusError as e:
        logger.error(f"[AI] API error: {e.response.status_code} - {e.response.text}")
        return _fallback_analysis(f"API error: {e.response.status_code}")
    except Exception as e:
        logger.error(f"[AI] Analysis failed: {e}")
        return _fallback_analysis(str(e))


def _parse_response(raw: str) -> AIAnalysis:
    """Parse raw LLM response into structured AIAnalysis."""
    try:
        def _float_or_default(value: Any, default: float) -> float:
            try:
                parsed = float(value)
                return parsed if math.isfinite(parsed) else default
            except (TypeError, ValueError):
                return default

        def _clamp(value: float, low: float, high: float) -> float:
            return max(low, min(high, value))

        def _optional_float(value: Any) -> float | None:
            if value is None or value == "":
                return None
            try:
                parsed = float(value)
                return parsed if math.isfinite(parsed) else None
            except (TypeError, ValueError):
                return None

        # Try to extract JSON from the response
        raw_clean = raw.strip()

        # Handle markdown code blocks
        if raw_clean.startswith("```"):
            lines = raw_clean.split("\n")
            json_lines = []
            in_block = False
            for line in lines:
                if line.strip().startswith("```") and not in_block:
                    in_block = True
                    continue
                elif line.strip().startswith("```") and in_block:
                    break
                elif in_block:
                    json_lines.append(line)
            raw_clean = "\n".join(json_lines)

        if not raw_clean.startswith("{"):
            start = raw_clean.find("{")
            end = raw_clean.rfind("}")
            if start != -1 and end != -1 and end > start:
                raw_clean = raw_clean[start:end + 1]

        parsed = json.loads(raw_clean)
        if not isinstance(parsed, dict):
            raise ValueError("AI response was not a JSON object")
        data: dict[str, object] = parsed
        recommendation = str(data.get("recommendation", "hold")).lower().strip()
        if recommendation not in {"execute", "modify", "reject", "hold"}:
            recommendation = "hold"

        warnings_raw = data.get("warnings", [])
        if isinstance(warnings_raw, str):
            warnings = [warnings_raw]
        elif isinstance(warnings_raw, list):
            warnings = [str(item) for item in warnings_raw if item is not None]
        else:
            warnings = []

        suggested_direction: SignalDirection | None = None
        suggested_direction_raw = data.get("suggested_direction")
        if isinstance(suggested_direction_raw, str):
            normalized_direction = suggested_direction_raw.lower().strip()
            if normalized_direction:
                try:
                    suggested_direction = SignalDirection(normalized_direction)
                except ValueError:
                    suggested_direction = None

        confidence = _clamp(_float_or_default(data.get("confidence", 0.5), 0.5), 0.0, 1.0)
        risk_score = _clamp(_float_or_default(data.get("risk_score", 0.5), 0.5), 0.0, 1.0)
        position_size_pct = _clamp(_float_or_default(data.get("position_size_pct", 1.0), 1.0), 0.0, 1.0)
        recommended_leverage = max(1.0, min(50.0, _float_or_default(data.get("recommended_leverage", 1.0), 1.0)))

        tp_qty_pcts = [
            _clamp(_float_or_default(data.get("tp1_qty_pct", 25.0), 25.0), 0.0, 100.0),
            _clamp(_float_or_default(data.get("tp2_qty_pct", 25.0), 25.0), 0.0, 100.0),
            _clamp(_float_or_default(data.get("tp3_qty_pct", 0.0), 0.0), 0.0, 100.0),
            _clamp(_float_or_default(data.get("tp4_qty_pct", 0.0), 0.0), 0.0, 100.0),
        ]
        tp_sum = sum(tp_qty_pcts)
        if tp_sum == 0:
            tp_qty_pcts = [25.0, 25.0, 25.0, 25.0]
        elif tp_sum > 100:
            logger.warning(f"[AI] TP sum {tp_sum}% exceeds 100%, capping")
            tp_sum_cap_factor = 100.0 / tp_sum
            tp_qty_pcts = [value * tp_sum_cap_factor for value in tp_qty_pcts]
            data["tp1_qty_pct"], data["tp2_qty_pct"], data["tp3_qty_pct"], data["tp4_qty_pct"] = tp_qty_pcts

        reasoning = str(data.get("reasoning", ""))
        if recommendation in ("execute", "modify") and confidence < 0.4:
            logger.warning(f"[AI] Execute recommendation with low confidence {confidence}, forcing reject")
            recommendation = "reject"
            reasoning = f"[AI SAFETY] Confidence {confidence:.2f} below execute threshold 0.4. Original: {reasoning}"

        raw_stop_loss = data.get("suggested_stop_loss")
        suggested_stop_loss = _optional_float(raw_stop_loss)
        if recommendation in ("execute", "modify") and suggested_stop_loss is None:
            if raw_stop_loss not in (None, ""):
                logger.warning("[AI] Execute/modify with non-finite stop loss, forcing reject")
                recommendation = "reject"
                reasoning = f"[AI SAFETY] Invalid stop loss for execute/modify. Original: {reasoning}"
            else:
                logger.warning("[AI] Execute/modify without stop loss; server will apply fallback or reject")
                warnings.append("AI omitted stop loss; server will apply fallback or reject")

        return AIAnalysis(
            confidence=confidence,
            recommendation=recommendation,
            reasoning=reasoning,
            suggested_direction=suggested_direction,
            suggested_entry=_optional_float(data.get("suggested_entry")),
            suggested_stop_loss=suggested_stop_loss,
            suggested_take_profit=_optional_float(data.get("suggested_take_profit")),
            suggested_tp1=_optional_float(data.get("suggested_tp1")),
            suggested_tp2=_optional_float(data.get("suggested_tp2")),
            suggested_tp3=_optional_float(data.get("suggested_tp3")),
            suggested_tp4=_optional_float(data.get("suggested_tp4")),
            tp1_qty_pct=_clamp(_float_or_default(data.get("tp1_qty_pct", 25.0), 25.0), 0.0, 100.0),
            tp2_qty_pct=_clamp(_float_or_default(data.get("tp2_qty_pct", 25.0), 25.0), 0.0, 100.0),
            tp3_qty_pct=_clamp(_float_or_default(data.get("tp3_qty_pct", 25.0), 25.0), 0.0, 100.0),
            tp4_qty_pct=_clamp(_float_or_default(data.get("tp4_qty_pct", 25.0), 25.0), 0.0, 100.0),
            position_size_pct=position_size_pct,
            recommended_leverage=recommended_leverage,
            risk_score=risk_score,
            market_condition=str(data.get("market_condition", "")),
            trend_strength=str(data.get("trend_strength", "moderate")),
            recommended_trailing_stop_mode=str(data.get("recommended_trailing_stop_mode", "none")),
            warnings=warnings,
            raw_response=raw,
        )
    except (json.JSONDecodeError, ValueError) as e:
        logger.warning(f"[AI] Failed to parse response: {e}")
        logger.debug(f"[AI] Raw response: {raw}")
        return AIAnalysis(
            confidence=0.3,
            recommendation="hold",
            reasoning=f"Failed to parse AI response: {e}",
            raw_response=raw,
        )


def _fallback_analysis(error: str) -> AIAnalysis:
    """Return a conservative fallback when AI analysis fails."""
    return AIAnalysis(
        confidence=0.0,
        recommendation="reject",
        reasoning=f"AI analysis unavailable: {error}. Rejecting for safety.",
        risk_score=1.0,
        warnings=[f"AI error: {error}"],
    )


# Alias for backward compatibility with tests
analyze_signal_with_ai = analyze_signal
