"""
P3-FIX: Prometheus Metrics for QuantPilot
Comprehensive observability metrics for trading, AI, and system monitoring.
"""

# Create metrics endpoint (Prometheus exporter route)
from fastapi import APIRouter
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from starlette.responses import Response

from .prometheus_metrics import (
    AI_ANALYSIS_LATENCY,
    AI_ANALYSIS_TOTAL,
    AI_CACHE_HIT,
    ERROR_RATE,
    EXCHANGE_ERRORS,
    GHOST_POSITION_COUNT,
    LEVERAGE_SETUP_FAILURE,
    PNL_TOTAL,
    POSITION_COUNT,
    TRADE_LATENCY,
    TRADE_TOTAL,
    setup_metrics,
)
from .recorders import (
    record_ai_analysis,
    record_ai_cost,
    record_exchange_request,
    record_filter_performance,
    record_http_request,
    record_prefilter_result,
    record_signal_received,
    record_trade,
    update_db_pool_metrics,
    update_exchange_pool_metrics,
    update_trading_control_mode,
)

metrics_router = APIRouter()

@metrics_router.get("/metrics")
async def metrics_endpoint():
    """Prometheus metrics endpoint."""
    return Response(
        content=generate_latest(),
        media_type=CONTENT_TYPE_LATEST,
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
    "EXCHANGE_ERRORS",
    "GHOST_POSITION_COUNT",
    "LEVERAGE_SETUP_FAILURE",
    "setup_metrics",
    "record_signal_received",
    "record_prefilter_result",
    "record_ai_analysis",
    "record_trade",
    "record_exchange_request",
    "record_http_request",
    "update_db_pool_metrics",
    "record_ai_cost",
    "update_trading_control_mode",
    "record_filter_performance",
    "update_exchange_pool_metrics",
    "metrics_router",
    "metrics_endpoint",
]
