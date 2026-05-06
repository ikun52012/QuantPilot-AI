"""
P3-FIX: Metrics Recording Helper Functions
Helper functions for recording metrics to Prometheus.
"""
import re

from loguru import logger

from .prometheus_metrics import (
    AI_ANALYSIS_LATENCY,
    AI_ANALYSIS_TOTAL,
    AI_CONFIDENCE,
    AI_COST_USD,
    DB_CONNECTIONS,
    DB_POOL_OVERFLOW,
    DB_POOL_SIZE,
    EXCHANGE_LATENCY,
    EXCHANGE_POOL_SIZE,
    EXCHANGE_REQUESTS,
    FILTER_PERFORMANCE,
    HTTP_LATENCY,
    HTTP_REQUESTS,
    SIGNALS_BLOCKED_PREFILTER,
    SIGNALS_PASSED_PREFILTER,
    SIGNALS_RECEIVED,
    TRADES_EXECUTED,
    TRADES_PNL,
    TRADING_CONTROL_MODE,
)


def record_signal_received(ticker: str, direction: str, user_id: str | None = None):
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


def record_trade(ticker: str, direction: str, status: str, pnl: float | None = None):
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
    normalized_path = _normalize_path(path)
    HTTP_REQUESTS.labels(method=method, path=normalized_path, status=str(status)).inc()
    HTTP_LATENCY.labels(method=method, path=normalized_path).observe(latency)


def update_db_pool_metrics(pool_type: str = "async"):
    """Update database connection pool metrics from engine."""
    try:
        from core.database import db_manager
        if db_manager.engine and hasattr(db_manager.engine, 'pool'):
            pool = db_manager.engine.pool
            pool_size = pool.size() if hasattr(pool, 'size') else 0
            checked_out = pool.checkedout() if hasattr(pool, 'checkedout') else 0
            overflow = pool.overflow() if hasattr(pool, 'overflow') else 0
            DB_POOL_SIZE.labels(pool_type=pool_type).set(pool_size)
            DB_POOL_OVERFLOW.labels(pool_type=pool_type).set(overflow)
            DB_CONNECTIONS.labels(status="active").set(checked_out)
            DB_CONNECTIONS.labels(status="idle").set(max(0, pool_size - checked_out))
    except (AttributeError, TypeError):
        pass
    except Exception as e:
        logger.debug(f"[Metrics] Failed to update DB pool metrics: {e}")


def record_ai_cost(provider: str, cost_usd: float):
    """Record AI API cost."""
    AI_COST_USD.labels(provider=provider).inc(cost_usd)


def update_trading_control_mode(mode: str):
    """Update trading control mode metric."""
    mode_values = {"enabled": 0, "read_only": 1, "paused": 2, "emergency_stop": 3}
    for m in mode_values:
        TRADING_CONTROL_MODE.labels(mode=m).set(1 if m == mode else 0)


def record_filter_performance(check_name: str, latency: float):
    """Record pre-filter check execution time."""
    FILTER_PERFORMANCE.labels(check_name=check_name).observe(latency)


def update_exchange_pool_metrics():
    """Update exchange connection pool size metrics."""
    try:
        from exchange import _exchange_pool
        exchange_counts = {}
        for key in _exchange_pool:
            exchange_id = key.split(":")[0]
            exchange_counts[exchange_id] = exchange_counts.get(exchange_id, 0) + 1
        for exchange_id, count in exchange_counts.items():
            EXCHANGE_POOL_SIZE.labels(exchange=exchange_id).set(count)
    except (ImportError, AttributeError, TypeError):
        pass
    except Exception as e:
        logger.debug(f"[Metrics] Failed to update exchange pool metrics: {e}")


def _normalize_path(path: str) -> str:
    """Normalize path for metrics (replace dynamic segments)."""
    path = re.sub(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}', '{id}', path)
    path = re.sub(r'/\d+', '/{id}', path)
    return path
