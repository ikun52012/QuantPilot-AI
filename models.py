"""
TradingView Signal Server - Data Models
"""
from datetime import datetime
from enum import Enum
from typing import Optional
from pydantic import BaseModel, Field, field_validator
from core.utils.datetime import utcnow


class SignalDirection(str, Enum):
    LONG = "long"
    SHORT = "short"
    CLOSE_LONG = "close_long"
    CLOSE_SHORT = "close_short"


class SignalSource(str, Enum):
    TRADINGVIEW = "tradingview"
    MANUAL = "manual"


# ─────────────────────────────────────────────
# Trailing-stop strategy types
# ─────────────────────────────────────────────
class TrailingStopMode(str, Enum):
    NONE = "none"
    MOVING = "moving"                        # Classic moving trailing stop
    BREAKEVEN_ON_TP1 = "breakeven_on_tp1"    # Move SL to entry when TP1 hit
    STEP_TRAILING = "step_trailing"          # Move SL to TP(n-1) when TP(n) hit
    PROFIT_PCT_TRAILING = "profit_pct_trailing"  # Activate trailing when profit % threshold reached


class TrailingStopConfig(BaseModel):
    """Configuration for trailing-stop behaviour."""
    mode: TrailingStopMode = TrailingStopMode.NONE
    # For MOVING mode: trailing distance as percentage of price
    trail_pct: float = Field(default=1.0, ge=0.1, le=20.0, description="Trailing distance %")
    # For PROFIT_PCT_TRAILING: activate trailing after this profit %
    activation_profit_pct: float = Field(default=1.0, ge=0.1, le=50.0,
                                          description="Profit % to activate trailing")
    # For PROFIT_PCT_TRAILING: trailing step once activated
    trailing_step_pct: float = Field(default=0.5, ge=0.1, le=10.0,
                                      description="Trailing step % after activation")


# ─────────────────────────────────────────────
# Multi take-profit configuration
# ─────────────────────────────────────────────
class TakeProfitLevel(BaseModel):
    """A single take-profit target."""
    price: float = Field(gt=0, description="TP price level")
    qty_pct: float = Field(default=25.0, ge=1.0, le=100.0,
                           description="Percentage of position to close at this TP")


class TakeProfitConfig(BaseModel):
    """Up to 4 take-profit levels."""
    enabled: bool = True
    levels: list[TakeProfitLevel] = Field(default_factory=list, max_length=4)


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
    ticker: str = Field(min_length=1, max_length=40)  # e.g. "BTCUSDT"
    exchange: str = Field(default="BINANCE", max_length=30)  # e.g. "BINANCE"
    direction: SignalDirection
    price: float = Field(gt=0)
    timeframe: str = Field(default="60", max_length=20)  # e.g. "60" for 1h
    strategy: str = Field(default="unknown", max_length=120)
    message: str = Field(default="", max_length=2000)
    timestamp: datetime = Field(default_factory=lambda: utcnow())

    @field_validator("secret")
    @classmethod
    def _strip_secret(cls, value: str) -> str:
        return value.strip()

    @field_validator("ticker", "exchange", "timeframe", "strategy")
    @classmethod
    def _strip_required_strings(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("value cannot be empty")
        return value

    @field_validator("ticker")
    @classmethod
    def _validate_ticker(cls, value: str) -> str:
        normalized = value.upper().strip()
        allowed = set("ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789/:._-")
        if any(ch not in allowed for ch in normalized):
            raise ValueError("ticker contains unsupported characters")
        return normalized

    @field_validator("message")
    @classmethod
    def _strip_message(cls, value: str) -> str:
        return value.strip()


# ─────────────────────────────────────────────
# Pre-filter result
# ─────────────────────────────────────────────
class PreFilterResult(BaseModel):
    passed: bool
    reason: str = ""
    checks: dict = Field(default_factory=dict)


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
# AI analysis result (enhanced with multi-TP)
# ─────────────────────────────────────────────
class AIAnalysis(BaseModel):
    confidence: float = Field(ge=0.0, le=1.0, description="0-1 confidence score")
    recommendation: str = "hold"        # execute / modify / reject / hold
    reasoning: str = ""                 # AI's explanation
    suggested_direction: Optional[SignalDirection] = None
    suggested_entry: Optional[float] = None
    suggested_stop_loss: Optional[float] = None
    suggested_take_profit: Optional[float] = None      # Legacy single TP
    suggested_tp1: Optional[float] = None
    suggested_tp2: Optional[float] = None
    suggested_tp3: Optional[float] = None
    suggested_tp4: Optional[float] = None
    tp1_qty_pct: float = Field(default=25.0, ge=0.0, le=100.0)   # % of position to close at TP1
    tp2_qty_pct: float = Field(default=25.0, ge=0.0, le=100.0)
    tp3_qty_pct: float = Field(default=25.0, ge=0.0, le=100.0)
    tp4_qty_pct: float = Field(default=25.0, ge=0.0, le=100.0)
    position_size_pct: float = Field(default=1.0, ge=0.0, le=1.0)  # suggested position size as % of default
    recommended_leverage: float = Field(default=1.0, ge=1.0, le=125.0)
    risk_score: float = Field(default=0.5, ge=0.0, le=1.0)
    market_condition: str = ""          # trending / ranging / volatile / calm
    warnings: list[str] = Field(default_factory=list)
    raw_response: str = ""              # raw LLM output for debugging


# ─────────────────────────────────────────────
# Final trade decision (enhanced with multi-TP & trailing stop)
# ─────────────────────────────────────────────
class TradeDecision(BaseModel):
    execute: bool = False
    direction: Optional[SignalDirection] = None
    ticker: str = ""
    entry_price: Optional[float] = None
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None              # Legacy single TP
    take_profit_levels: list[TakeProfitLevel] = Field(default_factory=list)
    trailing_stop: TrailingStopConfig = Field(default_factory=TrailingStopConfig)
    quantity: Optional[float] = None
    reason: str = ""
    signal: Optional[TradingViewSignal] = None
    ai_analysis: Optional[AIAnalysis] = None
    timestamp: datetime = Field(default_factory=lambda: utcnow())


# ─────────────────────────────────────────────
# Trade log entry
# ─────────────────────────────────────────────
class TradeLog(BaseModel):
    id: str = ""
    timestamp: datetime = Field(default_factory=lambda: utcnow())
    ticker: str
    direction: str
    entry_price: float
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None
    tp1: Optional[float] = None
    tp2: Optional[float] = None
    tp3: Optional[float] = None
    tp4: Optional[float] = None
    trailing_stop_mode: str = "none"
    quantity: float = 0.0
    status: str = "open"                # open / closed / cancelled
    exit_price: Optional[float] = None
    pnl: Optional[float] = None
    pnl_pct: Optional[float] = None
    ai_confidence: float = 0.0
    ai_reasoning: str = ""
    signal_source: str = "tradingview"
    close_reason: str = ""
