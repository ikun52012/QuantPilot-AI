"""
P2-FIX: Event Types for Event-Driven Architecture
Standardized event definitions for inter-component communication.
"""
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any


class EventTypes(str, Enum):
    """Standardized event type identifiers."""

    # Trading Events
    TRADE_RECEIVED = "trade_received"
    TRADE_PRE_FILTERED = "trade_pre_filtered"
    TRADE_AI_ANALYZED = "trade_ai_analyzed"
    TRADE_EXECUTED = "trade_executed"
    TRADE_FAILED = "trade_failed"
    TRADE_SKIPPED = "trade_skipped"

    # Position Events
    POSITION_OPENED = "position_opened"
    POSITION_UPDATED = "position_updated"
    POSITION_CLOSED = "position_closed"
    POSITION_TP_HIT = "position_tp_hit"
    POSITION_SL_HIT = "position_sl_hit"
    POSITION_GHOST_DETECTED = "position_ghost_detected"

    # AI Analysis Events
    AI_ANALYSIS_STARTED = "ai_analysis_started"
    AI_ANALYSIS_COMPLETED = "ai_analysis_completed"
    AI_ANALYSIS_FAILED = "ai_analysis_failed"
    AI_ANALYSIS_TIMEOUT = "ai_analysis_timeout"
    AI_CACHE_HIT = "ai_cache_hit"
    AI_CACHE_MISS = "ai_cache_miss"

    # System Events
    SYSTEM_ERROR = "system_error"
    SYSTEM_WARNING = "system_warning"
    EXCHANGE_ERROR = "exchange_error"
    LEVERAGE_SETUP_FAILED = "leverage_setup_failed"

    # User Events
    USER_LOGIN = "user_login"
    USER_BALANCE_UPDATED = "user_balance_updated"


@dataclass
class Event:
    """Base event class."""

    event_type: EventTypes
    timestamp: datetime = field(default_factory=lambda: datetime.utcnow())
    event_id: str = field(default_factory=lambda: "")
    source: str = "unknown"
    data: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class TradeEvent(Event):
    """Trade-related event."""

    ticker: str = ""
    direction: str = ""
    signal_price: float = 0.0
    user_id: str | None = None
    exchange: str = ""
    decision: Any | None = None
    order_id: str | None = None
    status: str = "pending"
    error_reason: str | None = None


@dataclass
class PositionEvent(Event):
    """Position-related event."""

    position_id: str = ""
    ticker: str = ""
    direction: str = ""
    entry_price: float = 0.0
    current_price: float = 0.0
    pnl_pct: float = 0.0
    status: str = "open"
    close_reason: str | None = None
    tp_level: int | None = None
    user_id: str | None = None


@dataclass
class AIAnalysisEvent(Event):
    """AI analysis event."""

    ticker: str = ""
    direction: str = ""
    provider: str = ""
    model: str = ""
    confidence: float = 0.0
    recommendation: str = ""
    analysis_time_ms: float = 0.0
    cache_layer: str | None = None  # L1, L2, L3, compute
    error: str | None = None
