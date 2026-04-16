"""
OpenClaw Signal Server - AI Analyzer
Uses LLM APIs (OpenAI / Anthropic / DeepSeek) to analyze trading signals.
This is the brain of the system.
"""
import asyncio
import json
import httpx
from loguru import logger
from config import settings
from models import TradingViewSignal, MarketContext, AIAnalysis

# Retry configuration for AI API calls
_AI_MAX_RETRIES = 3
_AI_BASE_DELAY = 1.0  # seconds; doubled each attempt
_RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}


# ─────────────────────────────────────────────
# System prompt - the "trading analyst" persona
# ─────────────────────────────────────────────
SYSTEM_PROMPT = """You are an expert cryptocurrency quantitative trading analyst with 15 years of experience.
You receive trading signals from a TradingView strategy and must analyze whether to EXECUTE, MODIFY, or REJECT each signal.

Your analysis process:
1. Evaluate the signal direction against current market context
2. Assess risk/reward ratio
3. Check for conflicting indicators
4. Consider market microstructure (orderbook, spread, volume)
5. Factor in broader market conditions (funding rate, 24h trend)

You MUST respond in valid JSON format with these exact fields:
{
    "confidence": 0.0-1.0,
    "recommendation": "execute" | "modify" | "reject",
    "reasoning": "Your detailed analysis in 2-3 sentences",
    "suggested_direction": "long" | "short" | null,
    "suggested_entry": null or float,
    "suggested_stop_loss": null or float,
    "suggested_take_profit": null or float,
    "position_size_pct": 0.1-1.0,
    "risk_score": 0.0-1.0,
    "market_condition": "trending_up" | "trending_down" | "ranging" | "volatile" | "calm",
    "warnings": ["list of risk warnings"]
}

Key rules:
- If confidence < 0.4, always recommend "reject"
- If funding rate is extreme (>0.05% or <-0.05%), warn about it
- If 1h price change > 5%, reduce position_size_pct
- If RSI > 75 and signal is long, be skeptical. If RSI < 25 and signal is short, be skeptical.
- If orderbook is heavily imbalanced against the signal direction, warn about it
- NEVER recommend more than position_size_pct = 1.0

Respond ONLY with the JSON object, no other text."""


def _build_user_prompt(signal: TradingViewSignal, market: MarketContext) -> str:
    """Build the user prompt with signal and market data."""
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

Should this signal be executed, modified, or rejected? Provide your analysis as JSON."""


# ─────────────────────────────────────────────
# Retry helper
# ─────────────────────────────────────────────

async def _with_retry(coro_factory, label: str) -> str:
    """
    Execute an async coroutine factory with exponential-backoff retry.
    Retries on rate-limit, server errors, and transient network failures.
    """
    last_exc: Exception | None = None
    for attempt in range(_AI_MAX_RETRIES):
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

async def _call_openai(system: str, user: str) -> str:
    """Call OpenAI/compatible API with automatic retry."""
    async def _do():
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                "https://api.openai.com/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {settings.ai.openai_api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": settings.ai.openai_model,
                    "messages": [
                        {"role": "system", "content": system},
                        {"role": "user", "content": user},
                    ],
                    "temperature": 0.3,
                    "max_tokens": 1000,
                    "response_format": {"type": "json_object"},
                },
            )
            resp.raise_for_status()
            data = resp.json()
            return data["choices"][0]["message"]["content"]

    return await _with_retry(_do, "openai")


async def _call_anthropic(system: str, user: str) -> str:
    """Call Anthropic Claude API with automatic retry."""
    async def _do():
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": settings.ai.anthropic_api_key,
                    "anthropic-version": "2023-06-01",
                    "Content-Type": "application/json",
                },
                json={
                    "model": settings.ai.anthropic_model,
                    "max_tokens": 1000,
                    "system": system,
                    "messages": [
                        {"role": "user", "content": user},
                    ],
                    "temperature": 0.3,
                },
            )
            resp.raise_for_status()
            data = resp.json()
            return data["content"][0]["text"]

    return await _with_retry(_do, "anthropic")


async def _call_deepseek(system: str, user: str) -> str:
    """Call DeepSeek API (OpenAI-compatible) with automatic retry."""
    async def _do():
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                "https://api.deepseek.com/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {settings.ai.deepseek_api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": settings.ai.deepseek_model,
                    "messages": [
                        {"role": "system", "content": system},
                        {"role": "user", "content": user},
                    ],
                    "temperature": 0.3,
                    "max_tokens": 1000,
                },
            )
            resp.raise_for_status()
            data = resp.json()
            return data["choices"][0]["message"]["content"]

    return await _with_retry(_do, "deepseek")


# ─────────────────────────────────────────────
# Main analysis function
# ─────────────────────────────────────────────

async def analyze_signal(
    signal: TradingViewSignal,
    market: MarketContext,
) -> AIAnalysis:
    """
    Send signal + market context to LLM and parse the response.
    Returns structured AIAnalysis.
    """
    user_prompt = _build_user_prompt(signal, market)
    provider = settings.ai.provider.lower()

    logger.info(f"[AI] Analyzing {signal.ticker} {signal.direction.value} via {provider}...")

    try:
        # Call the appropriate provider
        if provider == "openai":
            raw = await _call_openai(SYSTEM_PROMPT, user_prompt)
        elif provider == "anthropic":
            raw = await _call_anthropic(SYSTEM_PROMPT, user_prompt)
        elif provider == "deepseek":
            raw = await _call_deepseek(SYSTEM_PROMPT, user_prompt)
        else:
            raise ValueError(f"Unknown AI provider: {provider}")

        # Parse JSON response
        analysis = _parse_response(raw)
        logger.info(
            f"[AI] Result: {analysis.recommendation} "
            f"(confidence={analysis.confidence:.2f}, risk={analysis.risk_score:.2f})"
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

        data = json.loads(raw_clean)

        return AIAnalysis(
            confidence=float(data.get("confidence", 0.5)),
            recommendation=data.get("recommendation", "hold"),
            reasoning=data.get("reasoning", ""),
            suggested_direction=data.get("suggested_direction"),
            suggested_entry=data.get("suggested_entry"),
            suggested_stop_loss=data.get("suggested_stop_loss"),
            suggested_take_profit=data.get("suggested_take_profit"),
            position_size_pct=float(data.get("position_size_pct", 1.0)),
            risk_score=float(data.get("risk_score", 0.5)),
            market_condition=data.get("market_condition", ""),
            warnings=data.get("warnings", []),
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
