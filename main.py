"""
OpenClaw Signal Server - Main Application

Complete pipeline:
  TradingView Webhook → Pre-Filter → AI Analysis → Trade Execution → Notification

Usage:
  uvicorn main:app --host 0.0.0.0 --port 8000
"""
import sys
from datetime import datetime
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from loguru import logger

from config import settings
from models import (
    TradingViewSignal,
    TradeDecision,
    SignalDirection,
)
from pre_filter import run_pre_filter, increment_trade_count
from ai_analyzer import analyze_signal
from market_data import fetch_market_context
from exchange import execute_trade, get_account_balance
from notifier import (
    notify_signal_received,
    notify_pre_filter_blocked,
    notify_ai_analysis,
    notify_trade_executed,
    notify_error,
)
from trade_logger import log_trade, get_today_stats, get_today_trades

# ─────────────────────────────────────────────
# Logging setup
# ─────────────────────────────────────────────
logger.remove()
logger.add(sys.stdout, level="INFO", format="{time:HH:mm:ss} | {level:<7} | {message}")
logger.add("logs/server_{time:YYYY-MM-DD}.log", rotation="1 day", retention="30 days", level="DEBUG")


# ─────────────────────────────────────────────
# App lifecycle
# ─────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("=" * 50)
    logger.info("🐉 OpenClaw Signal Server starting...")
    logger.info(f"   AI Provider: {settings.ai.provider}")
    logger.info(f"   Exchange: {settings.exchange.name}")
    logger.info(f"   Live Trading: {'🔴 YES' if settings.exchange.live_trading else '🟢 NO (Paper)'}")
    logger.info("=" * 50)
    yield
    logger.info("OpenClaw Signal Server shutting down...")


# ─────────────────────────────────────────────
# FastAPI app
# ─────────────────────────────────────────────
app = FastAPI(
    title="OpenClaw Signal Server",
    description="AI-optimized crypto trading signal processor",
    version="1.0.0",
    lifespan=lifespan,
)


# ─────────────────────────────────────────────
# Health check
# ─────────────────────────────────────────────
@app.get("/")
async def root():
    return {
        "name": "OpenClaw Signal Server",
        "status": "running",
        "version": "1.0.0",
        "ai_provider": settings.ai.provider,
        "live_trading": settings.exchange.live_trading,
        "time": datetime.utcnow().isoformat(),
    }


@app.get("/health")
async def health():
    return {"status": "ok"}


# ─────────────────────────────────────────────
# MAIN WEBHOOK ENDPOINT
# ─────────────────────────────────────────────
@app.post("/webhook")
async def webhook(request: Request):
    """
    Main webhook endpoint for TradingView alerts.

    Pipeline:
      1. Parse & authenticate signal
      2. Fetch market context
      3. Run pre-filter (rule-based)
      4. Run AI analysis (LLM API)
      5. Make trade decision
      6. Execute trade
      7. Log & notify
    """
    try:
        # ── Step 1: Parse signal ──
        body = await request.json()
        signal = TradingViewSignal(**body)

        # Authenticate
        if settings.server.webhook_secret:
            if signal.secret != settings.server.webhook_secret:
                logger.warning(f"[Webhook] ❌ Invalid secret from {request.client.host}")
                raise HTTPException(status_code=403, detail="Invalid webhook secret")

        logger.info(f"[Webhook] 📡 Signal received: {signal.ticker} {signal.direction.value} @ {signal.price}")

        # Notify signal received
        await notify_signal_received(signal.ticker, signal.direction.value, signal.price)

        # ── Step 2: Fetch market context ──
        logger.info(f"[Pipeline] Fetching market context for {signal.ticker}...")
        market = await fetch_market_context(signal.ticker)

        # ── Step 3: Pre-filter ──
        logger.info("[Pipeline] Running pre-filter checks...")
        filter_result = run_pre_filter(
            signal, market,
            max_daily_trades=settings.risk.max_daily_trades,
            max_daily_loss_pct=settings.risk.max_daily_loss_pct,
        )

        if not filter_result.passed:
            logger.warning(f"[Pipeline] ❌ Pre-filter blocked: {filter_result.reason}")
            await notify_pre_filter_blocked(signal.ticker, signal.direction.value, filter_result.reason)

            decision = TradeDecision(
                execute=False,
                ticker=signal.ticker,
                reason=f"Pre-filter: {filter_result.reason}",
                signal=signal,
            )
            trade_id = log_trade(decision, {"status": "blocked_by_prefilter"})

            return JSONResponse(content={
                "status": "blocked",
                "trade_id": trade_id,
                "reason": filter_result.reason,
                "checks": filter_result.checks,
            })

        # ── Step 4: AI Analysis ──
        logger.info("[Pipeline] 🤖 Running AI analysis...")
        analysis = await analyze_signal(signal, market)
        await notify_ai_analysis(signal.ticker, analysis)

        # ── Step 5: Make decision ──
        decision = _make_decision(signal, analysis, market)
        logger.info(
            f"[Pipeline] Decision: {'✅ EXECUTE' if decision.execute else '❌ SKIP'} "
            f"- {decision.reason}"
        )

        # ── Step 6: Execute trade ──
        order_result = {"status": "not_executed"}
        if decision.execute:
            logger.info("[Pipeline] 📤 Executing trade...")
            order_result = await execute_trade(decision)
            increment_trade_count()
            await notify_trade_executed(decision, order_result)
        else:
            logger.info("[Pipeline] ⏭️ Skipping execution")

        # ── Step 7: Log ──
        trade_id = log_trade(decision, order_result)

        return JSONResponse(content={
            "status": "executed" if decision.execute else "rejected",
            "trade_id": trade_id,
            "ai_confidence": analysis.confidence,
            "ai_recommendation": analysis.recommendation,
            "ai_reasoning": analysis.reasoning,
            "order": order_result,
        })

    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"[Webhook] Pipeline error: {e}")
        await notify_error(f"Pipeline error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ─────────────────────────────────────────────
# Decision logic
# ─────────────────────────────────────────────
CONFIDENCE_THRESHOLD = 0.5     # minimum AI confidence to execute
RISK_THRESHOLD = 0.8           # maximum risk score to execute


def _make_decision(
    signal: TradingViewSignal,
    analysis,
    market,
) -> TradeDecision:
    """
    Combine signal + AI analysis into a final trade decision.
    """
    # AI says reject
    if analysis.recommendation == "reject":
        return TradeDecision(
            execute=False,
            ticker=signal.ticker,
            reason=f"AI rejected (confidence={analysis.confidence:.2f}): {analysis.reasoning}",
            signal=signal,
            ai_analysis=analysis,
        )

    # Confidence too low
    if analysis.confidence < CONFIDENCE_THRESHOLD:
        return TradeDecision(
            execute=False,
            ticker=signal.ticker,
            reason=f"AI confidence too low: {analysis.confidence:.2f} < {CONFIDENCE_THRESHOLD}",
            signal=signal,
            ai_analysis=analysis,
        )

    # Risk too high
    if analysis.risk_score > RISK_THRESHOLD:
        return TradeDecision(
            execute=False,
            ticker=signal.ticker,
            reason=f"Risk too high: {analysis.risk_score:.2f} > {RISK_THRESHOLD}",
            signal=signal,
            ai_analysis=analysis,
        )

    # Build execution decision
    direction = analysis.suggested_direction or signal.direction
    entry_price = analysis.suggested_entry or signal.price or market.current_price
    stop_loss = analysis.suggested_stop_loss
    take_profit = analysis.suggested_take_profit

    # Calculate quantity based on AI position sizing
    base_qty = _calculate_quantity(entry_price, stop_loss, market)
    adjusted_qty = base_qty * analysis.position_size_pct

    return TradeDecision(
        execute=True,
        direction=direction,
        ticker=signal.ticker,
        entry_price=entry_price,
        stop_loss=stop_loss,
        take_profit=take_profit,
        quantity=round(adjusted_qty, 6),
        reason=f"AI approved (confidence={analysis.confidence:.2f}): {analysis.reasoning}",
        signal=signal,
        ai_analysis=analysis,
    )


def _calculate_quantity(
    entry: float,
    stop_loss: float | None,
    market,
    risk_pct: float = 1.0,
) -> float:
    """Calculate position quantity based on risk percentage."""
    if not entry or entry <= 0:
        return 0.0

    # Default risk-based calculation
    if stop_loss and stop_loss > 0:
        risk_per_unit = abs(entry - stop_loss)
        if risk_per_unit > 0:
            # Assume $10,000 account for paper trading
            risk_capital = 10000 * risk_pct * 0.01
            max_qty = (10000 * settings.risk.max_position_pct * 0.01) / entry
            qty = min(risk_capital / risk_per_unit, max_qty)
            return max(qty, 0.0)

    # Fallback: fixed percentage of account
    max_notional = 10000 * settings.risk.max_position_pct * 0.01
    return max_notional / entry if entry > 0 else 0.0


# ─────────────────────────────────────────────
# Dashboard endpoints
# ─────────────────────────────────────────────
@app.get("/stats")
async def stats():
    """Get today's trading statistics."""
    return get_today_stats()


@app.get("/trades")
async def trades():
    """Get today's trade log."""
    return get_today_trades()


@app.get("/balance")
async def balance():
    """Get account balance from exchange."""
    return await get_account_balance()


# ─────────────────────────────────────────────
# Manual signal endpoint (for testing)
# ─────────────────────────────────────────────
@app.post("/test-signal")
async def test_signal():
    """Send a test signal through the pipeline."""
    test = TradingViewSignal(
        secret=settings.server.webhook_secret,
        ticker="BTCUSDT",
        exchange="BINANCE",
        direction=SignalDirection.LONG,
        price=0.0,      # will use market price
        timeframe="60",
        strategy="Test Signal",
        message="Manual test signal",
    )

    # Redirect to webhook handler
    from starlette.testclient import TestClient
    logger.info("[Test] Sending test signal through pipeline...")

    # Fetch current price
    market = await fetch_market_context("BTCUSDT")
    test.price = market.current_price

    # Process manually
    return await _process_signal_internal(test)


async def _process_signal_internal(signal: TradingViewSignal):
    """Process a signal without HTTP request context."""
    market = await fetch_market_context(signal.ticker)

    filter_result = run_pre_filter(
        signal, market,
        max_daily_trades=settings.risk.max_daily_trades,
        max_daily_loss_pct=settings.risk.max_daily_loss_pct,
    )

    if not filter_result.passed:
        return {"status": "blocked", "reason": filter_result.reason}

    analysis = await analyze_signal(signal, market)
    decision = _make_decision(signal, analysis, market)

    order_result = {"status": "not_executed"}
    if decision.execute:
        order_result = await execute_trade(decision)

    trade_id = log_trade(decision, order_result)

    return {
        "status": "executed" if decision.execute else "rejected",
        "trade_id": trade_id,
        "ai": {
            "confidence": analysis.confidence,
            "recommendation": analysis.recommendation,
            "reasoning": analysis.reasoning,
        },
        "order": order_result,
    }


# ─────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "main:app",
        host=settings.server.host,
        port=settings.server.port,
        reload=True,
    )
