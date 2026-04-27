"""
QuantPilot AI - AI Analyzer
Uses LLM APIs (OpenAI / Anthropic / DeepSeek / OpenRouter) to analyze trading signals.
This is the brain of the system.
"""
import asyncio
import json
import httpx
import os
from loguru import logger
from core.config import settings
from models import TradingViewSignal, MarketContext, AIAnalysis, TrailingStopMode

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

# ─────────────────────────────────────────────
# AI analysis result cache (#18)
# ─────────────────────────────────────────────
import time as _time

_AI_CACHE_TTL = 30
_AI_CACHE_MAX_SIZE = 500
_AI_CACHE: dict[str, tuple[float, "AIAnalysis"]] = {}
_AI_CACHE_LOCK = asyncio.Lock()


def _ai_cache_key(ticker: str, direction: str, price_bucket: str = "", timeframe: str = "") -> str:
    """Generate cache key with price bucket and timeframe to avoid stale cache hits.

    Price is bucketed to 1% intervals to allow cache hits for similar prices
    while avoiding incorrect hits when price moves significantly.
    """
    key = f"{ticker}:{direction}"
    if timeframe:
        key += f":{timeframe}"
    if price_bucket:
        key += f":{price_bucket}"
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


async def _get_cached_analysis(ticker: str, direction: str, price_bucket: str = "", timeframe: str = ""):
    key = _ai_cache_key(ticker, direction, price_bucket, timeframe)
    async with _AI_CACHE_LOCK:
        entry = _AI_CACHE.get(key)
        if entry and (_time.monotonic() - entry[0]) < _AI_CACHE_TTL:
            return entry[1]
    return None


async def _set_cached_analysis(ticker: str, direction: str, analysis, price_bucket: str = "", timeframe: str = ""):
    key = _ai_cache_key(ticker, direction, price_bucket, timeframe)
    async with _AI_CACHE_LOCK:
        _AI_CACHE[key] = (_time.monotonic(), analysis)
        now = _time.monotonic()
        stale = [k for k, (ts, _) in _AI_CACHE.items() if now - ts > _AI_CACHE_TTL]
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
6. Determine optimal take-profit targets (up to 4 levels)
7. Assess volatility to suggest appropriate trailing stop parameters

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


def _get_effective_system_prompt() -> str:
    """Return system prompt with optional custom additions."""
    base = SYSTEM_PROMPT

    # Always include SMC/FVG optimization instructions
    base += "\n" + SMC_FVG_PROMPT

    profile = settings.risk.ai_risk_profile.lower().strip()
    base += "\n\n" + RISK_PROFILE_PROMPTS.get(profile, RISK_PROFILE_PROMPTS["balanced"])

    num_tp = settings.take_profit.num_levels

    if settings.risk.exit_management_mode == "ai":
        tp_instruction = (
            f"\n\nExit management mode: AI-generated exits are enabled. "
            f"You must provide suggested_stop_loss plus exactly {num_tp} take-profit targets. "
            f"The server is configured for {num_tp} TP levels. "
            f"You MUST generate suggested_tp1 through suggested_tp{num_tp} with valid prices. "
            f"For TP levels beyond {num_tp} (TP{num_tp+1} to TP4), you MUST set them to null (not 0, not a number, but null). "
            f"Example: if num_levels=2, suggested_tp1 and suggested_tp2 should have valid prices, "
            f"but suggested_tp3 and suggested_tp4 must be null. "
            f"Similarly, tp1_qty_pct through tp{num_tp}_qty_pct should sum to 100% or less, "
            f"and tp{num_tp+1}_qty_pct through tp4_qty_pct should be 0. "
            f"Obey the requested risk profile for exit levels."
        )
        base += tp_instruction
        if settings.risk.ai_exit_system_prompt:
            base += f"\nExit-generation instructions:\n{settings.risk.ai_exit_system_prompt}"
    else:
        base += (
            "\n\nExit management mode: custom fixed exits are enabled. "
            "You may still comment on risk, but the server will ignore AI stop-loss "
            "and take-profit prices and use configured custom percentages."
        )
    if settings.ai.custom_system_prompt:
        base += f"\n\nAdditional instructions from the user:\n{settings.ai.custom_system_prompt}"
    return base


def _build_user_prompt(signal: TradingViewSignal, market: MarketContext, smc_text: str = "") -> str:
    """Build the user prompt with signal, market data, and SMC analysis."""
    tp_config = settings.take_profit
    ts_config = settings.trailing_stop

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

    return f"""Analyze this trading signal:

## Signal
- Ticker: {signal.ticker}
- Direction: {signal.direction.value}
- Signal Price: {signal.price}
- Timeframe: {signal.timeframe}
- Strategy: {signal.strategy}
- Message: {signal.message}

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
{tp_section}
{ts_section}

IMPORTANT: The server expects EXACTLY {tp_config.num_levels} take-profit targets.
- Generate valid prices for suggested_tp1 through suggested_tp{tp_config.num_levels}
- Set suggested_tp{tp_config.num_levels + 1} through suggested_tp4 to null (not a number, but null)
- Set tp{tp_config.num_levels + 1}_qty_pct through tp4_qty_pct to 0
- The first {tp_config.num_levels} TP quantities should sum to 100% or less

{smc_text}
Should this signal be executed, modified, or rejected? If the entry price is suboptimal based on SMC analysis, recommend "modify" and provide a better suggested_entry price. Provide your analysis as JSON."""


# ─────────────────────────────────────────────
# Retry helper
# ─────────────────────────────────────────────

async def _with_retry(coro_factory, label: str) -> str:
    """
    Execute an async coroutine factory with exponential-backoff retry.
    Retries on rate-limit, server errors, and transient network failures.
    """
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

async def _call_openai(system: str, user: str, model: str = None) -> str:
    """Call OpenAI/compatible API with automatic retry."""
    model_name = model or settings.ai.openai_model
    async def _do():
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
            return data["choices"][0]["message"]["content"]

    return await _with_retry(_do, "openai")


async def _call_anthropic(system: str, user: str, model: str = None) -> str:
    """Call Anthropic Claude API with automatic retry."""
    model_name = model or settings.ai.anthropic_model
    async def _do():
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
            return data["content"][0]["text"]

    return await _with_retry(_do, "anthropic")


async def _call_deepseek(system: str, user: str, model: str = None) -> str:
    """Call DeepSeek API (OpenAI-compatible) with automatic retry."""
    model_name = model or settings.ai.deepseek_model
    async def _do():
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
            return data["choices"][0]["message"]["content"]

    return await _with_retry(_do, "deepseek")


async def _call_mistral(system: str, user: str, model: str = None) -> str:
    """Call Mistral API (OpenAI-compatible) with automatic retry."""
    model_name = model or settings.ai.mistral_model
    async def _do():
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
            return data["choices"][0]["message"]["content"]

    return await _with_retry(_do, "mistral")


async def _call_openrouter(system: str, user: str, model: str = None) -> str:
    """Call OpenRouter's OpenAI-compatible chat completions API."""
    model_name = model or settings.ai.openrouter_model
    async def _do():
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
            return data["choices"][0]["message"]["content"]

    return await _with_retry(_do, "openrouter")


async def _call_custom(system: str, user: str, model: str = None) -> str:
    """Call custom AI provider API with automatic retry."""
    model_name = model or settings.ai.custom_provider_model or "gpt-3.5-turbo"
    async def _do():
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

            resp = await client.post(
                settings.ai.custom_provider_api_url,
                headers={
                    "Authorization": f"Bearer {settings.ai.custom_provider_api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
            )
            resp.raise_for_status()
            data = resp.json()

            if "choices" in data and len(data["choices"]) > 0:
                return data["choices"][0]["message"]["content"]
            elif "content" in data and len(data["content"]) > 0:
                return data["content"][0]["text"]
            else:
                if "text" in data:
                    return data["text"]
                elif "response" in data:
                    return data["response"]
                elif "message" in data:
                    return data["message"]
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
    Returns a conservative analysis JSON.
    """
    import json
    return json.dumps({
        "confidence": 0.5,
        "recommendation": "hold",
        "reasoning": "Local rule fallback: No AI model available. Holding position for manual review.",
        "suggested_direction": None,
        "suggested_entry": None,
        "suggested_stop_loss": None,
        "suggested_take_profit": None,
        "position_size_pct": 0.5,
        "recommended_leverage": 1.0,
        "risk_score": 0.5,
        "market_condition": "unknown",
        "warnings": ["Local fallback mode - manual review recommended"],
    })


def _aggregate_legacy_voting_results(
    results: list[dict],
    weights: dict[str, float],
    strategy: str,
) -> dict:
    """Compatibility adapter for older dict-based voting tests/tools."""
    if not results:
        return {"action": "hold", "confidence": 0.0, "reason": "No voting results available"}

    model_ids = list(weights.keys()) or [f"model{i + 1}" for i in range(len(results))]
    ordered_weights = [float(weights.get(model_ids[i], 1.0)) if i < len(model_ids) else 1.0 for i in range(len(results))]
    total_weight = sum(ordered_weights) or float(len(results))

    if strategy == "best_confidence":
        return max(results, key=lambda item: float(item.get("confidence") or 0.0))

    if strategy == "consensus":
        counts = {}
        for result in results:
            action = result.get("action", "hold")
            counts[action] = counts.get(action, 0) + 1
        action = max(counts, key=counts.get)
        if counts[action] <= len(results) / 2:
            return {"action": "hold", "confidence": 0.0, "reason": "Consensus not reached"}
        confidence = sum(float(r.get("confidence") or 0.0) for r in results if r.get("action") == action) / counts[action]
        return {"action": action, "confidence": confidence, "reason": "Consensus reached"}

    action_weights = {}
    weighted_confidence = 0.0
    for result, weight in zip(results, ordered_weights):
        normalized = weight / total_weight
        action = result.get("action", "hold")
        action_weights[action] = action_weights.get(action, 0.0) + normalized
        weighted_confidence += float(result.get("confidence") or 0.0) * normalized

    action = max(action_weights, key=action_weights.get)
    return {"action": action, "confidence": weighted_confidence, "reason": "Weighted voting aggregate"}


def _aggregate_voting_results(results, strategy, weights=None):
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
            best_idx = max(range(len(analyses)), key=lambda i: analyses[i].confidence)
            result = analyses[best_idx]
            result.confidence = avg_confidence
            result.reasoning = f"Consensus reached: {execute_votes}/{len(analyses)} vote execute. {result.reasoning}"
            logger.info(
                f"[AI/Voting] Consensus reached: {execute_votes}/{len(analyses)} vote execute"
            )
            return result
        else:
            return AIAnalysis(
                confidence=0.3,
                recommendation="reject",
                reasoning=f"Consensus failed: only {execute_votes}/{len(analyses)} vote execute. Majority required.",
                risk_score=0.7,
                warnings=["Voting consensus not reached"],
            )

    if strategy == "weighted":
        total_weight = sum(weights.get(mid, 1.0) for mid in model_ids)
        if total_weight <= 0:
            total_weight = len(analyses)

        weighted_confidence = 0.0
        weighted_risk = 0.0
        weighted_position_pct = 0.0
        weighted_leverage = 0.0

        recommendation_votes = {}
        direction_votes = {}

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

        final_recommendation = max(recommendation_votes.keys(), key=lambda k: recommendation_votes[k])

        best_idx = max(range(len(analyses)), key=lambda i: analyses[i].confidence)
        best_analysis = analyses[best_idx]

        combined_reasoning = f"Weighted voting ({len(results)} models): "
        combined_reasoning += ", ".join(f"{mid}:{a.recommendation}({a.confidence:.2f})" for a, mid in results)

        result = AIAnalysis(
            confidence=weighted_confidence,
            recommendation=final_recommendation,
            reasoning=combined_reasoning,
            suggested_direction=best_analysis.suggested_direction,
            suggested_entry=best_analysis.suggested_entry,
            suggested_stop_loss=best_analysis.suggested_stop_loss,
            suggested_take_profit=best_analysis.suggested_take_profit,
            suggested_tp1=best_analysis.suggested_tp1,
            suggested_tp2=best_analysis.suggested_tp2,
            suggested_tp3=best_analysis.suggested_tp3,
            suggested_tp4=best_analysis.suggested_tp4,
            tp1_qty_pct=best_analysis.tp1_qty_pct,
            tp2_qty_pct=best_analysis.tp2_qty_pct,
            tp3_qty_pct=best_analysis.tp3_qty_pct,
            tp4_qty_pct=best_analysis.tp4_qty_pct,
            position_size_pct=min(weighted_position_pct, 1.0),
            recommended_leverage=min(weighted_leverage, 50.0),
            risk_score=weighted_risk,
            market_condition=best_analysis.market_condition,
            warnings=[f"Voting result from {len(results)} models"] + best_analysis.warnings,
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
) -> AIAnalysis:
    """
    Send signal + market context to LLM and parse the response.
    Includes multi-timeframe SMC/FVG analysis for optimal entry detection.
    Results are cached for 30s per ticker+direction+price_bucket+timeframe.

    Voting mode: If voting_enabled, calls multiple models concurrently and aggregates.
    """
    price_bucket = _price_to_bucket(market.current_price) if market.current_price > 0 else ""
    timeframe = str(signal.timeframe or "")

    cache_key_suffix = ""
    if settings.ai.voting_enabled and settings.ai.voting_models:
        cache_key_suffix = ":voting"

    cached = await _get_cached_analysis(
        signal.ticker,
        signal.direction.value + cache_key_suffix,
        price_bucket,
        timeframe,
    )
    if cached is not None:
        logger.info(f"[AI] Using cached analysis for {signal.ticker} {signal.direction.value} @ {price_bucket}")
        return cached

    smc_text = ""
    try:
        from smc_analyzer import (
            analyze_smc_single_tf, find_confluence_zones,
            format_smc_for_ai, MultiTimeframeSMC,
        )

        ohlcv_4h = getattr(market, "_ohlcv_4h", None) or []
        ohlcv_1h = getattr(market, "_ohlcv_1h", None) or []
        ohlcv_15m = getattr(market, "_ohlcv_15m", None) or []

        htf_ctx = analyze_smc_single_tf(ohlcv_4h, "4h", market.current_price) if len(ohlcv_4h) >= 5 else None
        mtf_ctx = analyze_smc_single_tf(ohlcv_1h, "1h", market.current_price) if len(ohlcv_1h) >= 5 else None
        ltf_ctx = analyze_smc_single_tf(ohlcv_15m, "15m", market.current_price) if len(ohlcv_15m) >= 5 else None

        direction = signal.direction.value if signal.direction else "long"
        confluence = find_confluence_zones(htf_ctx, mtf_ctx, ltf_ctx, direction, market.current_price)

        mtf_smc = MultiTimeframeSMC(htf=htf_ctx, mtf=mtf_ctx, ltf=ltf_ctx, confluence_zones=confluence)
        smc_text = format_smc_for_ai(mtf_smc, direction, market.current_price)

        logger.info(
            f"[AI/SMC] {signal.ticker}: "
            f"FVGs={sum(len(c.fvgs) for c in [htf_ctx, mtf_ctx, ltf_ctx] if c)}, "
            f"OBs={sum(len(c.order_blocks) for c in [htf_ctx, mtf_ctx, ltf_ctx] if c)}, "
            f"Confluences={len(confluence)}"
        )
    except Exception as e:
        logger.warning(f"[AI/SMC] SMC analysis failed, proceeding without: {e}")

    system_prompt = _get_effective_system_prompt()
    user_prompt = _build_user_prompt(signal, market, smc_text)

    if settings.ai.voting_enabled and settings.ai.voting_models:
        logger.info(
            f"[AI/Voting] Starting multi-model voting for {signal.ticker} {signal.direction.value}: "
            f"{settings.ai.voting_models}"
        )

        voting_tasks = []
        for model_id in settings.ai.voting_models:
            voting_tasks.append(_call_model_by_id(model_id, system_prompt, user_prompt))

        try:
            raw_results = await asyncio.gather(*voting_tasks, return_exceptions=True)

            valid_results = []
            for i, result in enumerate(raw_results):
                model_id = settings.ai.voting_models[i]
                if isinstance(result, Exception):
                    logger.warning(f"[AI/Voting] Model {model_id} failed: {result}")
                    continue
                try:
                    analysis = _parse_response(result[0])
                    valid_results.append((analysis, model_id))
                except Exception as e:
                    logger.warning(f"[AI/Voting] Failed to parse {model_id} response: {e}")

            if not valid_results:
                logger.error("[AI/Voting] All voting models failed")
                return _fallback_analysis("All voting models failed")

            final_analysis = await _aggregate_voting_results(
                valid_results,
                settings.ai.voting_strategy,
                settings.ai.voting_weights,
            )

            await _set_cached_analysis(signal.ticker, signal.direction.value + cache_key_suffix, final_analysis, price_bucket, timeframe)
            return final_analysis

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

        analysis = _parse_response(raw)
        logger.info(
            f"[AI] Result: {analysis.recommendation} "
            f"(confidence={analysis.confidence:.2f}, risk={analysis.risk_score:.2f})"
        )
        await _set_cached_analysis(signal.ticker, signal.direction.value + cache_key_suffix, analysis, price_bucket, timeframe)
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

        data = json.loads(raw_clean)
        recommendation = str(data.get("recommendation", "hold")).lower().strip()
        if recommendation not in {"execute", "modify", "reject", "hold"}:
            recommendation = "hold"

        warnings = data.get("warnings", [])
        if isinstance(warnings, str):
            warnings = [warnings]
        elif not isinstance(warnings, list):
            warnings = []
        suggested_direction = data.get("suggested_direction")
        if isinstance(suggested_direction, str):
            suggested_direction = suggested_direction.lower().strip() or None

        return AIAnalysis(
            confidence=float(data.get("confidence", 0.5)),
            recommendation=recommendation,
            reasoning=data.get("reasoning", ""),
            suggested_direction=suggested_direction,
            suggested_entry=data.get("suggested_entry"),
            suggested_stop_loss=data.get("suggested_stop_loss"),
            suggested_take_profit=data.get("suggested_take_profit"),
            suggested_tp1=data.get("suggested_tp1"),
            suggested_tp2=data.get("suggested_tp2"),
            suggested_tp3=data.get("suggested_tp3"),
            suggested_tp4=data.get("suggested_tp4"),
            tp1_qty_pct=float(data.get("tp1_qty_pct", 25.0)),
            tp2_qty_pct=float(data.get("tp2_qty_pct", 25.0)),
            tp3_qty_pct=float(data.get("tp3_qty_pct", 25.0)),
            tp4_qty_pct=float(data.get("tp4_qty_pct", 25.0)),
            position_size_pct=float(data.get("position_size_pct", 1.0)),
            recommended_leverage=float(data.get("recommended_leverage", 1.0)),
            risk_score=float(data.get("risk_score", 0.5)),
            market_condition=data.get("market_condition", ""),
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
