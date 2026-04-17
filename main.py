"""
Signal Server - Main Application

Complete pipeline:
  TradingView Webhook → Pre-Filter → AI Analysis → Trade Execution → Notification

Includes homepage, dashboard frontend and API endpoints for positions, analytics, settings.

Usage:
  uvicorn main:app --host 0.0.0.0 --port 8000
"""
import sys
import json
from datetime import datetime
from pathlib import Path
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from loguru import logger
from pydantic import BaseModel
from typing import Optional

from config import settings
from models import (
    TradingViewSignal,
    TradeDecision,
    SignalDirection,
    TakeProfitLevel,
    TrailingStopConfig,
    TrailingStopMode,
)
from pre_filter import run_pre_filter, increment_trade_count
from ai_analyzer import analyze_signal
from market_data import fetch_market_context
from exchange import (
    execute_trade,
    get_account_balance,
    get_open_positions,
    get_recent_orders,
    test_exchange_connection,
    get_supported_exchanges,
)
from notifier import (
    notify_signal_received,
    notify_pre_filter_blocked,
    notify_ai_analysis,
    notify_trade_executed,
    notify_error,
    send_telegram,
)
from trade_logger import log_trade, get_today_stats, get_today_trades, get_trade_history
from analytics import calculate_performance, get_daily_pnl, get_trade_distribution, invalidate_performance_cache

# ─────────────────────────────────────────────
# Logging setup
# ─────────────────────────────────────────────
logger.remove()
logger.add(sys.stdout, level="INFO", format="{time:HH:mm:ss} | {level:<7} | {message}")
logger.add("logs/server_{time:YYYY-MM-DD}.log", rotation="1 day", retention="30 days", level="DEBUG")

# Settings file for runtime config changes
SETTINGS_FILE = Path(__file__).parent / "runtime_settings.json"


def _load_runtime_settings() -> dict:
    if SETTINGS_FILE.exists():
        try:
            return json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def _save_runtime_settings(data: dict):
    current = _load_runtime_settings()
    current.update(data)
    SETTINGS_FILE.write_text(json.dumps(current, indent=2, ensure_ascii=False), encoding="utf-8")


# ─────────────────────────────────────────────
# App lifecycle
# ─────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("=" * 50)
    logger.info("📡 Signal Server starting...")
    logger.info(f"   AI Provider: {settings.ai.provider}")
    logger.info(f"   Exchange: {settings.exchange.name}")
    logger.info(f"   Live Trading: {'🔴 YES' if settings.exchange.live_trading else '🟢 NO (Paper)'}")
    logger.info(f"   Supported Exchanges: {', '.join(get_supported_exchanges())}")
    logger.info(f"   TP Levels: {settings.take_profit.num_levels}")
    logger.info(f"   Trailing Stop: {settings.trailing_stop.mode}")
    logger.info("=" * 50)

    # Apply runtime settings on startup (non-sensitive fields only; secrets come from .env)
    rs = _load_runtime_settings()
    if rs.get("exchange"):
        settings.exchange.name = rs["exchange"].get("name", settings.exchange.name)
    if rs.get("ai"):
        settings.ai.provider = rs["ai"].get("provider", settings.ai.provider)
        settings.ai.temperature = rs["ai"].get("temperature", settings.ai.temperature)
        settings.ai.max_tokens = rs["ai"].get("max_tokens", settings.ai.max_tokens)
        settings.ai.custom_system_prompt = rs["ai"].get("custom_system_prompt", settings.ai.custom_system_prompt)
    if rs.get("telegram"):
        settings.telegram.chat_id = rs["telegram"].get("chat_id", settings.telegram.chat_id)
    if rs.get("risk"):
        settings.risk.max_position_pct = rs["risk"].get("max_position_pct", settings.risk.max_position_pct)
        settings.risk.max_daily_trades = rs["risk"].get("max_daily_trades", settings.risk.max_daily_trades)
        settings.risk.max_daily_loss_pct = rs["risk"].get("max_daily_loss_pct", settings.risk.max_daily_loss_pct)
    if rs.get("take_profit"):
        tp = rs["take_profit"]
        settings.take_profit.num_levels = tp.get("num_levels", settings.take_profit.num_levels)
        settings.take_profit.tp1_pct = tp.get("tp1_pct", settings.take_profit.tp1_pct)
        settings.take_profit.tp2_pct = tp.get("tp2_pct", settings.take_profit.tp2_pct)
        settings.take_profit.tp3_pct = tp.get("tp3_pct", settings.take_profit.tp3_pct)
        settings.take_profit.tp4_pct = tp.get("tp4_pct", settings.take_profit.tp4_pct)
        settings.take_profit.tp1_qty = tp.get("tp1_qty", settings.take_profit.tp1_qty)
        settings.take_profit.tp2_qty = tp.get("tp2_qty", settings.take_profit.tp2_qty)
        settings.take_profit.tp3_qty = tp.get("tp3_qty", settings.take_profit.tp3_qty)
        settings.take_profit.tp4_qty = tp.get("tp4_qty", settings.take_profit.tp4_qty)
    if rs.get("trailing_stop"):
        ts = rs["trailing_stop"]
        settings.trailing_stop.mode = ts.get("mode", settings.trailing_stop.mode)
        settings.trailing_stop.trail_pct = ts.get("trail_pct", settings.trailing_stop.trail_pct)
        settings.trailing_stop.activation_profit_pct = ts.get("activation_profit_pct", settings.trailing_stop.activation_profit_pct)
        settings.trailing_stop.trailing_step_pct = ts.get("trailing_step_pct", settings.trailing_stop.trailing_step_pct)

    yield
    logger.info("Signal Server shutting down...")


# ─────────────────────────────────────────────
# FastAPI app
# ─────────────────────────────────────────────
app = FastAPI(
    title="Signal Server",
    description="AI-optimized crypto trading signal processor",
    version="3.0.0",
    lifespan=lifespan,
)

# Mount static files
STATIC_DIR = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


# ─────────────────────────────────────────────
# Homepage (landing page)
# ─────────────────────────────────────────────
@app.get("/", response_class=HTMLResponse)
async def homepage():
    """Serve the beautiful landing homepage."""
    return FileResponse(STATIC_DIR / "home.html")


# ─────────────────────────────────────────────
# Dashboard (serve frontend)
# ─────────────────────────────────────────────
@app.get("/dashboard")
async def dashboard():
    """Serve the dashboard frontend."""
    return FileResponse(STATIC_DIR / "index.html")


# ─────────────────────────────────────────────
# Health & Status
# ─────────────────────────────────────────────
@app.get("/api/status")
async def api_status():
    return {
        "name": "Signal Server",
        "status": "running",
        "version": "3.0.0",
        "ai_provider": settings.ai.provider,
        "exchange": settings.exchange.name,
        "live_trading": settings.exchange.live_trading,
        "supported_exchanges": get_supported_exchanges(),
        "tp_levels": settings.take_profit.num_levels,
        "trailing_stop_mode": settings.trailing_stop.mode,
        # Custom AI provider info
        "custom_provider_enabled": settings.ai.custom_provider_enabled,
        "custom_provider_name": settings.ai.custom_provider_name,
        "custom_provider_model": settings.ai.custom_provider_model,
        "custom_provider_url": settings.ai.custom_provider_api_url,
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
    Pipeline: Parse → Market Data → Pre-Filter → AI Analysis → Decision → Execute → Log
    """
    try:
        body = await request.json()
        signal = TradingViewSignal(**body)

        # Authenticate
        if settings.server.webhook_secret:
            if signal.secret != settings.server.webhook_secret:
                logger.warning(f"[Webhook] ❌ Invalid secret from {request.client.host}")
                raise HTTPException(status_code=403, detail="Invalid webhook secret")

        logger.info(f"[Webhook] 📡 Signal: {signal.ticker} {signal.direction.value} @ {signal.price}")
        await notify_signal_received(signal.ticker, signal.direction.value, signal.price)

        # Fetch market context
        market = await fetch_market_context(signal.ticker)

        # Pre-filter
        filter_result = run_pre_filter(
            signal, market,
            max_daily_trades=settings.risk.max_daily_trades,
            max_daily_loss_pct=settings.risk.max_daily_loss_pct,
        )

        if not filter_result.passed:
            await notify_pre_filter_blocked(signal.ticker, signal.direction.value, filter_result.reason)
            decision = TradeDecision(
                execute=False, ticker=signal.ticker,
                reason=f"Pre-filter: {filter_result.reason}", signal=signal,
            )
            trade_id = log_trade(decision, {"status": "blocked_by_prefilter"})
            return JSONResponse(content={
                "status": "blocked", "trade_id": trade_id,
                "reason": filter_result.reason, "checks": filter_result.checks,
            })

        # AI Analysis
        analysis = await analyze_signal(signal, market)
        await notify_ai_analysis(signal.ticker, analysis)

        # Decision
        decision = _make_decision(signal, analysis, market)

        # Execute
        order_result = {"status": "not_executed"}
        if decision.execute:
            order_result = await execute_trade(decision)
            increment_trade_count()
            await notify_trade_executed(decision, order_result)

        trade_id = log_trade(decision, order_result)
        invalidate_performance_cache()

        return JSONResponse(content={
            "status": "executed" if decision.execute else "rejected",
            "trade_id": trade_id,
            "ai_confidence": analysis.confidence,
            "ai_recommendation": analysis.recommendation,
            "ai_reasoning": analysis.reasoning,
            "tp_levels": len(decision.take_profit_levels),
            "trailing_stop": decision.trailing_stop.mode.value if decision.trailing_stop else "none",
            "order": order_result,
        })

    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"[Webhook] Pipeline error: {e}")
        await notify_error(f"Pipeline error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ─────────────────────────────────────────────
# Decision logic (enhanced with multi-TP & trailing stop)
# ─────────────────────────────────────────────
CONFIDENCE_THRESHOLD = 0.5
RISK_THRESHOLD = 0.8


def _make_decision(signal, analysis, market) -> TradeDecision:
    if analysis.recommendation == "reject":
        return TradeDecision(
            execute=False, ticker=signal.ticker,
            reason=f"AI rejected (conf={analysis.confidence:.2f}): {analysis.reasoning}",
            signal=signal, ai_analysis=analysis,
        )
    if analysis.confidence < CONFIDENCE_THRESHOLD:
        return TradeDecision(
            execute=False, ticker=signal.ticker,
            reason=f"Confidence too low: {analysis.confidence:.2f}",
            signal=signal, ai_analysis=analysis,
        )
    if analysis.risk_score > RISK_THRESHOLD:
        return TradeDecision(
            execute=False, ticker=signal.ticker,
            reason=f"Risk too high: {analysis.risk_score:.2f}",
            signal=signal, ai_analysis=analysis,
        )

    direction = analysis.suggested_direction or signal.direction
    entry = analysis.suggested_entry or signal.price or market.current_price
    qty = _calc_qty(entry, analysis.suggested_stop_loss, market) * analysis.position_size_pct

    # Build multi-TP levels
    tp_levels = _build_tp_levels(analysis, entry, direction)

    # Build trailing stop config
    trailing_config = _build_trailing_config()

    return TradeDecision(
        execute=True, direction=direction, ticker=signal.ticker,
        entry_price=entry, stop_loss=analysis.suggested_stop_loss,
        take_profit=analysis.suggested_take_profit,
        take_profit_levels=tp_levels,
        trailing_stop=trailing_config,
        quantity=round(qty, 6),
        reason=f"AI approved (conf={analysis.confidence:.2f}): {analysis.reasoning}",
        signal=signal, ai_analysis=analysis,
    )


def _build_tp_levels(analysis, entry, direction) -> list[TakeProfitLevel]:
    """Build take-profit levels from AI analysis and settings."""
    tp_levels = []
    num = settings.take_profit.num_levels

    # Map AI-suggested TPs with fallback to settings-based % distances
    tp_prices = []
    tp_qtys = []

    is_long = direction in (SignalDirection.LONG,)

    for i in range(1, num + 1):
        ai_tp = getattr(analysis, f"suggested_tp{i}", None)
        ai_qty = getattr(analysis, f"tp{i}_qty_pct", 25.0)
        default_pct = getattr(settings.take_profit, f"tp{i}_pct", 2.0 * i)
        default_qty = getattr(settings.take_profit, f"tp{i}_qty", 25.0)

        if ai_tp and ai_tp > 0:
            price = ai_tp
        else:
            # Calculate from entry ± percentage
            pct = default_pct / 100.0
            price = entry * (1 + pct) if is_long else entry * (1 - pct)

        qty_pct = ai_qty if ai_qty != 25.0 else default_qty

        tp_levels.append(TakeProfitLevel(price=round(price, 8), qty_pct=qty_pct))

    return tp_levels


def _build_trailing_config() -> TrailingStopConfig:
    """Build trailing stop config from runtime settings."""
    mode_str = settings.trailing_stop.mode.lower()
    try:
        mode = TrailingStopMode(mode_str)
    except ValueError:
        mode = TrailingStopMode.NONE

    return TrailingStopConfig(
        mode=mode,
        trail_pct=settings.trailing_stop.trail_pct,
        activation_profit_pct=settings.trailing_stop.activation_profit_pct,
        trailing_step_pct=settings.trailing_stop.trailing_step_pct,
    )


def _calc_qty(entry, stop_loss, market, risk_pct=1.0):
    if not entry or entry <= 0:
        return 0.0
    if stop_loss and stop_loss > 0:
        risk_per_unit = abs(entry - stop_loss)
        if risk_per_unit > 0:
            risk_capital = 10000 * risk_pct * 0.01
            max_qty = (10000 * settings.risk.max_position_pct * 0.01) / entry
            return min(risk_capital / risk_per_unit, max_qty)
    return (10000 * settings.risk.max_position_pct * 0.01) / entry


# ═══════════════════════════════════════════════
# DASHBOARD API ENDPOINTS
# ═══════════════════════════════════════════════

@app.get("/stats")
async def stats():
    return get_today_stats()


@app.get("/trades")
async def trades():
    return get_today_trades()


@app.get("/balance")
async def balance():
    return await get_account_balance()


# ── Positions ──
@app.get("/api/positions")
async def api_positions():
    return await get_open_positions()


@app.get("/api/orders")
async def api_orders(symbol: str = None, limit: int = 50):
    return await get_recent_orders(symbol, limit)


# ── History ──
@app.get("/api/history")
async def api_history(days: int = 30):
    return get_trade_history(days)


# ── Performance Analytics ──
@app.get("/api/performance")
async def api_performance(days: int = 30):
    return calculate_performance(days)


@app.get("/api/daily-pnl")
async def api_daily_pnl(days: int = 30):
    return get_daily_pnl(days)


@app.get("/api/distribution")
async def api_distribution():
    return get_trade_distribution()


# ── Connection Test ──
class ConnectionTestRequest(BaseModel):
    exchange: str
    api_key: str
    api_secret: str
    password: str = ""


@app.post("/api/test-connection")
async def api_test_connection(req: ConnectionTestRequest):
    return await test_exchange_connection(req.exchange, req.api_key, req.api_secret, req.password)


# ── Settings ──
class ExchangeSettingsRequest(BaseModel):
    exchange: str = ""
    api_key: str = ""
    api_secret: str = ""
    password: str = ""


class AISettingsRequest(BaseModel):
    provider: str = ""
    api_key: str = ""
    temperature: float = 0.3
    max_tokens: int = 1000
    custom_system_prompt: str = ""
    # Custom AI provider fields
    custom_provider_enabled: bool = False
    custom_provider_name: str = "custom"
    custom_provider_model: str = ""
    custom_provider_api_url: str = ""


class TelegramSettingsRequest(BaseModel):
    bot_token: str = ""
    chat_id: str = ""


class RiskSettingsRequest(BaseModel):
    max_position_pct: float = 10.0
    max_daily_trades: int = 10
    max_daily_loss_pct: float = 5.0


class TakeProfitSettingsRequest(BaseModel):
    num_levels: int = 1
    tp1_pct: float = 2.0
    tp2_pct: float = 4.0
    tp3_pct: float = 6.0
    tp4_pct: float = 10.0
    tp1_qty: float = 25.0
    tp2_qty: float = 25.0
    tp3_qty: float = 25.0
    tp4_qty: float = 25.0


class TrailingStopSettingsRequest(BaseModel):
    mode: str = "none"
    trail_pct: float = 1.0
    activation_profit_pct: float = 1.0
    trailing_step_pct: float = 0.5


@app.post("/api/settings/exchange")
async def save_exchange_settings(req: ExchangeSettingsRequest):
    if req.exchange:
        settings.exchange.name = req.exchange
    if req.api_key:
        settings.exchange.api_key = req.api_key
    if req.api_secret:
        settings.exchange.api_secret = req.api_secret
    if req.password:
        settings.exchange.password = req.password

    # Only persist the exchange name – never write API keys to plain-text JSON.
    _save_runtime_settings({"exchange": {"name": settings.exchange.name}})
    logger.info(f"[Settings] Exchange updated: {settings.exchange.name}")
    return {"status": "saved", "exchange": settings.exchange.name}


@app.post("/api/settings/ai")
async def save_ai_settings(req: AISettingsRequest):
    if req.provider:
        settings.ai.provider = req.provider
    if req.api_key:
        if req.provider == "openai":
            settings.ai.openai_api_key = req.api_key
        elif req.provider == "anthropic":
            settings.ai.anthropic_api_key = req.api_key
        elif req.provider == "deepseek":
            settings.ai.deepseek_api_key = req.api_key
        elif req.provider == req.custom_provider_name:
            settings.ai.custom_provider_api_key = req.api_key

    # Handle custom provider settings
    settings.ai.custom_provider_enabled = req.custom_provider_enabled
    if req.custom_provider_name:
        settings.ai.custom_provider_name = req.custom_provider_name
    if req.custom_provider_model:
        settings.ai.custom_provider_model = req.custom_provider_model
    if req.custom_provider_api_url:
        settings.ai.custom_provider_api_url = req.custom_provider_api_url

    settings.ai.temperature = req.temperature
    settings.ai.max_tokens = req.max_tokens
    settings.ai.custom_system_prompt = req.custom_system_prompt

    # Persist non-secret AI settings
    _save_runtime_settings({
        "ai": {
            "provider": settings.ai.provider,
            "temperature": settings.ai.temperature,
            "max_tokens": settings.ai.max_tokens,
            "custom_system_prompt": settings.ai.custom_system_prompt,
            "custom_provider_enabled": settings.ai.custom_provider_enabled,
            "custom_provider_name": settings.ai.custom_provider_name,
            "custom_provider_model": settings.ai.custom_provider_model,
        }
    })
    logger.info(f"[Settings] AI provider updated: {settings.ai.provider}")
    return {"status": "saved", "provider": settings.ai.provider}


@app.post("/api/settings/telegram")
async def save_telegram_settings(req: TelegramSettingsRequest):
    if req.bot_token:
        settings.telegram.bot_token = req.bot_token
    if req.chat_id:
        settings.telegram.chat_id = req.chat_id

    # Persist only the (non-secret) chat_id; bot_token stays in memory / .env.
    _save_runtime_settings({"telegram": {"chat_id": settings.telegram.chat_id}})
    logger.info("[Settings] Telegram updated")
    return {"status": "saved"}


@app.post("/api/settings/risk")
async def save_risk_settings(req: RiskSettingsRequest):
    settings.risk.max_position_pct = req.max_position_pct
    settings.risk.max_daily_trades = req.max_daily_trades
    settings.risk.max_daily_loss_pct = req.max_daily_loss_pct

    _save_runtime_settings({
        "risk": {
            "max_position_pct": req.max_position_pct,
            "max_daily_trades": req.max_daily_trades,
            "max_daily_loss_pct": req.max_daily_loss_pct,
        }
    })
    logger.info("[Settings] Risk settings updated")
    return {"status": "saved"}


@app.post("/api/settings/take-profit")
async def save_take_profit_settings(req: TakeProfitSettingsRequest):
    settings.take_profit.num_levels = req.num_levels
    settings.take_profit.tp1_pct = req.tp1_pct
    settings.take_profit.tp2_pct = req.tp2_pct
    settings.take_profit.tp3_pct = req.tp3_pct
    settings.take_profit.tp4_pct = req.tp4_pct
    settings.take_profit.tp1_qty = req.tp1_qty
    settings.take_profit.tp2_qty = req.tp2_qty
    settings.take_profit.tp3_qty = req.tp3_qty
    settings.take_profit.tp4_qty = req.tp4_qty

    _save_runtime_settings({
        "take_profit": {
            "num_levels": req.num_levels,
            "tp1_pct": req.tp1_pct, "tp2_pct": req.tp2_pct,
            "tp3_pct": req.tp3_pct, "tp4_pct": req.tp4_pct,
            "tp1_qty": req.tp1_qty, "tp2_qty": req.tp2_qty,
            "tp3_qty": req.tp3_qty, "tp4_qty": req.tp4_qty,
        }
    })
    logger.info(f"[Settings] Take-profit updated: {req.num_levels} levels")
    return {"status": "saved", "num_levels": req.num_levels}


@app.post("/api/settings/trailing-stop")
async def save_trailing_stop_settings(req: TrailingStopSettingsRequest):
    settings.trailing_stop.mode = req.mode
    settings.trailing_stop.trail_pct = req.trail_pct
    settings.trailing_stop.activation_profit_pct = req.activation_profit_pct
    settings.trailing_stop.trailing_step_pct = req.trailing_step_pct

    _save_runtime_settings({
        "trailing_stop": {
            "mode": req.mode,
            "trail_pct": req.trail_pct,
            "activation_profit_pct": req.activation_profit_pct,
            "trailing_step_pct": req.trailing_step_pct,
        }
    })
    logger.info(f"[Settings] Trailing stop updated: {req.mode}")
    return {"status": "saved", "mode": req.mode}


@app.post("/api/test-telegram")
async def api_test_telegram():
    await send_telegram("🧪 <b>Test Message</b>\n\nSignal Server is connected!")
    return {"status": "sent"}


# ── Test Signal ──
@app.post("/test-signal")
async def test_signal():
    market = await fetch_market_context("BTCUSDT")
    signal = TradingViewSignal(
        secret=settings.server.webhook_secret,
        ticker="BTCUSDT", exchange="BINANCE",
        direction=SignalDirection.LONG,
        price=market.current_price,
        timeframe="60", strategy="Test Signal",
        message="Manual test",
    )
    return await _process_internal(signal)


async def _process_internal(signal):
    market = await fetch_market_context(signal.ticker)
    fr = run_pre_filter(signal, market,
        max_daily_trades=settings.risk.max_daily_trades,
        max_daily_loss_pct=settings.risk.max_daily_loss_pct)
    if not fr.passed:
        return {"status": "blocked", "reason": fr.reason}

    analysis = await analyze_signal(signal, market)
    decision = _make_decision(signal, analysis, market)
    order_result = {"status": "not_executed"}
    if decision.execute:
        order_result = await execute_trade(decision)
    trade_id = log_trade(decision, order_result)
    invalidate_performance_cache()
    return {
        "status": "executed" if decision.execute else "rejected",
        "trade_id": trade_id,
        "ai": {"confidence": analysis.confidence, "recommendation": analysis.recommendation, "reasoning": analysis.reasoning},
        "tp_levels": len(decision.take_profit_levels),
        "trailing_stop": decision.trailing_stop.mode.value if decision.trailing_stop else "none",
        "order": order_result,
    }


# ─────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host=settings.server.host, port=settings.server.port, reload=True)
