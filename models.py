"""
OpenClaw Signal Server - Data Models
"""
from datetime import datetime
from enum import Enum
from typing import Optional
from pydantic import BaseModel, Field


class SignalDirection(str, Enum):
    LONG = "long"
    SHORT = "short"
    CLOSE_LONG = "close_long"
    CLOSE_SHORT = "close_short"


class SignalSource(str, Enum):
    TRADINGVIEW = "tradingview"
    MANUAL = "manual"


# ─────────────────────────────────────────────
# Incoming signal from TradingView webhook
# ─────────────────────────────────────────────
class TradingViewSignal(BaseModel):
    """
    Expected JSON from TradingView alert webhook.

    In TradingView alert message, use this JSON template:
    {
        "secret": "{{your-webhook-secret}}",
        "ticker": "{{ticker}}",
        "exchange": "{{exchange}}",
        "direction": "long",
        "price": {{close}},
        "timeframe": "{{interval}}",
        "strategy": "Crypto Quant Pro v6",
        "message": "{{strategy.order.comment}}"
    }
    """
    secret: str = ""
    ticker: str                         # e.g. "BTCUSDT"
    exchange: str = "BINANCE"           # e.g. "BINANCE"
    direction: SignalDirection
    price: float
    timeframe: str = "60"               # e.g. "60" for 1h
    strategy: str = "unknown"
    message: str = ""
    timestamp: datetime = Field(default_factory=datetime.utcnow)


# ─────────────────────────────────────────────
# Pre-filter result
# ─────────────────────────────────────────────
class PreFilterResult(BaseModel):
    passed: bool
    reason: str = ""
    checks: dict = {}


# ─────────────────────────────────────────────
# Market context snapshot for AI analysis
# ─────────────────────────────────────────────
class MarketContext(BaseModel):
    ticker: str
    current_price: float
    price_change_1h: float = 0.0        # % change
    price_change_4h: float = 0.0
    price_change_24h: float = 0.0
    volume_24h: float = 0.0
    volume_change_pct: float = 0.0      # vs 24h avg
    high_24h: float = 0.0
    low_24h: float = 0.0
    bid_ask_spread: float = 0.0
    funding_rate: Optional[float] = None
    open_interest: Optional[float] = None
    rsi_1h: Optional[float] = None
    atr_pct: Optional[float] = None
    ema_fast: Optional[float] = None
    ema_slow: Optional[float] = None
    orderbook_imbalance: Optional[float] = None  # bid/ask ratio


# ─────────────────────────────────────────────
# AI analysis result
# ─────────────────────────────────────────────
class AIAnalysis(BaseModel):
    confidence: float = Field(ge=0.0, le=1.0, description="0-1 confidence score")
    recommendation: str = "hold"        # execute / modify / reject / hold
    reasoning: str = ""                 # AI's explanation
    suggested_direction: Optional[SignalDirection] = None
    suggested_entry: Optional[float] = None
    suggested_stop_loss: Optional[float] = None
    suggested_take_profit: Optional[float] = None
    position_size_pct: float = 1.0      # suggested position size as % of default
    risk_score: float = Field(default=0.5, ge=0.0, le=1.0)
    market_condition: str = ""          # trending / ranging / volatile / calm
    warnings: list[str] = []
    raw_response: str = ""              # raw LLM output for debugging


# ─────────────────────────────────────────────
# Final trade decision
# ─────────────────────────────────────────────
class TradeDecision(BaseModel):
    execute: bool = False
    direction: Optional[SignalDirection] = None
    ticker: str = ""
    entry_price: Optional[float] = None
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None
    quantity: Optional[float] = None
    reason: str = ""
    signal: Optional[TradingViewSignal] = None
    ai_analysis: Optional[AIAnalysis] = None
    timestamp: datetime = Field(default_factory=datetime.utcnow)


# ─────────────────────────────────────────────
# Trade log entry
# ─────────────────────────────────────────────
class TradeLog(BaseModel):
    id: str = ""
    timestamp: datetime = Field(default_factory=datetime.utcnow)
    ticker: str
    direction: str
    entry_price: float
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None
    quantity: float = 0.0
    status: str = "open"                # open / closed / cancelled
    exit_price: Optional[float] = None
    pnl: Optional[float] = None
    pnl_pct: Optional[float] = None
    ai_confidence: float = 0.0
    ai_reasoning: str = ""
    signal_source: str = "tradingview"
    close_reason: str = ""
