"""
P3-FIX: Prometheus Metrics for QuantPilot
Comprehensive observability metrics for trading, AI, and system monitoring.
"""

from .prometheus_metrics import (
    TRADE_TOTAL,
    TRADE_LATENCY,
    POSITION_COUNT,
    PNL_TOTAL,
    AI_ANALYSIS_TOTAL,
    AI_ANALYSIS_LATENCY,
    AI_CACHE_HIT,
    ERROR_RATE,
    GHOST_POSITION_COUNT,
    LEVERAGE_SETUP_FAILURE,
    setup_metrics,
)

__all__ = [
    "TRADE_TOTAL",
    "TRADE_LATENCY",
    "POSITION_COUNT",
    "PNL_TOTAL",
    "AI_ANALYSIS_TOTAL",
    "AI_ANALYSIS_LATENCY",
    "AI_CACHE_HIT",
    "ERROR_RATE",
    "GHOST_POSITION_COUNT",
    "LEVERAGE_SETUP_FAILURE",
    "setup_metrics",
]