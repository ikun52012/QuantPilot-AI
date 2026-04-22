"""
Signal Server - Prometheus Metrics
Comprehensive metrics for monitoring and observability.
"""
import time
from typing import Optional
from prometheus_client import Counter, Histogram, Gauge, Info, CollectorRegistry, generate_latest
from fastapi import Response

from core.config import settings


# ─────────────────────────────────────────────
# Metrics Registry
# ─────────────────────────────────────────────

registry = CollectorRegistry()


# ─────────────────────────────────────────────
# Application Info
# ─────────────────────────────────────────────

APP_INFO = Info(
    "signal_server",
    "QuantPilot AI",
    registry=registry,
)
APP_INFO.info({
    "version": settings.app_version,
    "exchange": settings.exchange.name,
    "ai_provider": settings.ai.provider,
    "live_trading": str(settings.exchange.live_trading),
    "exchange_sandbox_mode": str(settings.exchange.sandbox_mode),
})


# ─────────────────────────────────────────────
# Signal Metrics
# ─────────────────────────────────────────────

SIGNALS_RECEIVED = Counter(
    "signals_received_total",
    "Total number of signals received",
    ["ticker", "direction", "user_id"],
    registry=registry,
)

SIGNALS_PASSED_PREFILTER = Counter(
    "signals_passed_prefilter_total",
    "Signals that passed pre-filter",
    ["ticker", "direction"],
    registry=registry,
)

SIGNALS_BLOCKED_PREFILTER = Counter(
    "signals_blocked_prefilter_total",
    "Signals blocked by pre-filter",
    ["ticker", "direction", "reason"],
    registry=registry,
)


# ─────────────────────────────────────────────
# AI Analysis Metrics
# ─────────────────────────────────────────────

AI_ANALYSIS_TOTAL = Counter(
    "ai_analysis_total",
    "Total AI analysis requests",
    ["provider", "recommendation"],
    registry=registry,
)

AI_ANALYSIS_LATENCY = Histogram(
    "ai_analysis_latency_seconds",
    "AI analysis latency in seconds",
    ["provider"],
    buckets=[0.5, 1.0, 2.0, 5.0, 10.0, 30.0, 60.0, 90.0],
    registry=registry,
)

AI_CONFIDENCE = Histogram(
    "ai_confidence",
    "AI confidence distribution",
    ["provider"],
    buckets=[0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0],
    registry=registry,
)


# ─────────────────────────────────────────────
# Trade Metrics
# ─────────────────────────────────────────────

TRADES_EXECUTED = Counter(
    "trades_executed_total",
    "Total trades executed",
    ["ticker", "direction", "status"],
    registry=registry,
)

TRADES_PNL = Histogram(
    "trades_pnl_percent",
    "Trade PnL percentage distribution",
    ["ticker", "direction"],
    buckets=[-20, -10, -5, -2, -1, 0, 1, 2, 5, 10, 20],
    registry=registry,
)

TRADE_LATENCY = Histogram(
    "trade_execution_latency_seconds",
    "Trade execution latency in seconds",
    ["exchange"],
    buckets=[0.1, 0.25, 0.5, 1.0, 2.0, 5.0, 10.0],
    registry=registry,
)


# ─────────────────────────────────────────────
# Exchange Metrics
# ─────────────────────────────────────────────

EXCHANGE_REQUESTS = Counter(
    "exchange_requests_total",
    "Total exchange API requests",
    ["exchange", "endpoint", "status"],
    registry=registry,
)

EXCHANGE_LATENCY = Histogram(
    "exchange_request_latency_seconds",
    "Exchange API request latency",
    ["exchange", "endpoint"],
    buckets=[0.1, 0.25, 0.5, 1.0, 2.0, 5.0],
    registry=registry,
)

EXCHANGE_ERRORS = Counter(
    "exchange_errors_total",
    "Total exchange API errors",
    ["exchange", "error_type"],
    registry=registry,
)


# ─────────────────────────────────────────────
# HTTP Metrics
# ─────────────────────────────────────────────

HTTP_REQUESTS = Counter(
    "http_requests_total",
    "Total HTTP requests",
    ["method", "path", "status"],
    registry=registry,
)

HTTP_LATENCY = Histogram(
    "http_request_latency_seconds",
    "HTTP request latency",
    ["method", "path"],
    buckets=[0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0],
    registry=registry,
)

HTTP_REQUESTS_IN_PROGRESS = Gauge(
    "http_requests_in_progress",
    "Number of HTTP requests in progress",
    ["method", "path"],
    registry=registry,
)


# ─────────────────────────────────────────────
# Database Metrics
# ─────────────────────────────────────────────

DB_CONNECTIONS = Gauge(
    "db_connections",
    "Database connections",
    ["status"],
    registry=registry,
)

DB_QUERIES = Counter(
    "db_queries_total",
    "Total database queries",
    ["operation", "table"],
    registry=registry,
)

DB_LATENCY = Histogram(
    "db_query_latency_seconds",
    "Database query latency",
    ["operation", "table"],
    buckets=[0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5],
    registry=registry,
)


# ─────────────────────────────────────────────
# Cache Metrics
# ─────────────────────────────────────────────

CACHE_HITS = Counter(
    "cache_hits_total",
    "Total cache hits",
    ["cache_type"],
    registry=registry,
)

CACHE_MISSES = Counter(
    "cache_misses_total",
    "Total cache misses",
    ["cache_type"],
    registry=registry,
)

CACHE_SIZE = Gauge(
    "cache_size",
    "Current cache size",
    ["cache_type"],
    registry=registry,
)


# ─────────────────────────────────────────────
# User Metrics
# ─────────────────────────────────────────────

USERS_TOTAL = Gauge(
    "users_total",
    "Total number of users",
    ["role", "status"],
    registry=registry,
)

SUBSCRIPTIONS_TOTAL = Gauge(
    "subscriptions_total",
    "Total number of subscriptions",
    ["plan", "status"],
    registry=registry,
)


# ─────────────────────────────────────────────
# System Metrics
# ─────────────────────────────────────────────

SYSTEM_INFO = Info(
    "system",
    "System information",
    registry=registry,
)

try:
    import platform
    SYSTEM_INFO.info({
        "python_version": platform.python_version(),
        "platform": platform.platform(),
        "machine": platform.machine(),
    })
except Exception:
    pass


# ─────────────────────────────────────────────
# Helper Functions
# ─────────────────────────────────────────────

def record_signal_received(ticker: str, direction: str, user_id: Optional[str] = None):
    """Record a received signal."""
    SIGNALS_RECEIVED.labels(
        ticker=ticker,
        direction=direction,
        user_id=user_id or "admin",
    ).inc()


def record_prefilter_result(ticker: str, direction: str, passed: bool, reason: str = ""):
    """Record pre-filter result."""
    if passed:
        SIGNALS_PASSED_PREFILTER.labels(ticker=ticker, direction=direction).inc()
    else:
        SIGNALS_BLOCKED_PREFILTER.labels(
            ticker=ticker,
            direction=direction,
            reason=reason[:50] if reason else "unknown",
        ).inc()


def record_ai_analysis(provider: str, recommendation: str, confidence: float, latency: float):
    """Record AI analysis result."""
    AI_ANALYSIS_TOTAL.labels(provider=provider, recommendation=recommendation).inc()
    AI_ANALYSIS_LATENCY.labels(provider=provider).observe(latency)
    AI_CONFIDENCE.labels(provider=provider).observe(confidence)


def record_trade(ticker: str, direction: str, status: str, pnl: Optional[float] = None):
    """Record a trade execution."""
    TRADES_EXECUTED.labels(ticker=ticker, direction=direction, status=status).inc()
    if pnl is not None:
        TRADES_PNL.labels(ticker=ticker, direction=direction).observe(pnl)


def record_exchange_request(exchange: str, endpoint: str, status: str, latency: float):
    """Record an exchange API request."""
    EXCHANGE_REQUESTS.labels(exchange=exchange, endpoint=endpoint, status=status).inc()
    EXCHANGE_LATENCY.labels(exchange=exchange, endpoint=endpoint).observe(latency)


def record_http_request(method: str, path: str, status: int, latency: float):
    """Record an HTTP request."""
    # Normalize path to avoid high cardinality
    normalized_path = _normalize_path(path)
    HTTP_REQUESTS.labels(method=method, path=normalized_path, status=str(status)).inc()
    HTTP_LATENCY.labels(method=method, path=normalized_path).observe(latency)


def _normalize_path(path: str) -> str:
    """Normalize path for metrics (replace dynamic segments)."""
    import re
    # Replace UUIDs
    path = re.sub(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}', '{id}', path)
    # Replace numbers
    path = re.sub(r'/\d+', '/{id}', path)
    return path


async def metrics_endpoint() -> Response:
    """FastAPI endpoint for Prometheus metrics."""
    return Response(
        content=generate_latest(registry),
        media_type="text/plain; version=0.0.4; charset=utf-8",
    )
