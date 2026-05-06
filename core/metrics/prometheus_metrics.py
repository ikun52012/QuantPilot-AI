"""
P3-FIX: Prometheus Metrics Definitions
Comprehensive trading, AI, and system metrics for observability.
"""
try:
    from prometheus_client import REGISTRY, Counter, Gauge, Histogram, Info, Registry

    PROMETHEUS_AVAILABLE = True
except ImportError:
    PROMETHEUS_AVAILABLE = False

    # Mock classes for development without prometheus_client
    class MockMetric:
        def __init__(self, name, *args, **kwargs):
            self.name = name
        def labels(self, *args, **kwargs):
            return self
        def inc(self, *args, **kwargs):
            pass
        def dec(self, *args, **kwargs):
            pass
        def observe(self, *args, **kwargs):
            pass
        def set(self, *args, **kwargs):
            pass

    Counter = Gauge = Histogram = Info = MockMetric
    Registry = None
    REGISTRY = None


from loguru import logger

# ─────────────────────────────────────────────
# Trading Metrics
# ─────────────────────────────────────────────

TRADE_TOTAL = Counter(
    'quantpilot_trade_total',
    'Total number of trades executed',
    ['exchange', 'symbol', 'direction', 'result']  # result: success, failed, skipped
)

TRADE_LATENCY = Histogram(
    'quantpilot_trade_latency_seconds',
    'Trade execution latency distribution',
    ['exchange', 'stage'],  # stage: validate, pre_filter, ai_analyze, execute, confirm
    buckets=[0.1, 0.5, 1, 2, 5, 10, 30, 60]
)

POSITION_COUNT = Gauge(
    'quantpilot_position_count',
    'Number of active positions',
    ['exchange', 'symbol', 'direction']
)

PNL_TOTAL = Counter(
    'quantpilot_pnl_total_usdt',
    'Total realized PnL in USDT',
    ['exchange', 'user_id']
)


# ─────────────────────────────────────────────
# AI Analysis Metrics
# ─────────────────────────────────────────────

AI_ANALYSIS_TOTAL = Counter(
    'quantpilot_ai_analysis_total',
    'Total AI analyses performed',
    ['provider', 'model', 'result']  # result: success, failed, timeout, cached
)

AI_ANALYSIS_LATENCY = Histogram(
    'quantpilot_ai_analysis_latency_seconds',
    'AI analysis latency distribution',
    ['provider', 'model'],
    buckets=[1, 2, 5, 10, 20, 30, 60, 120]
)

AI_CACHE_HIT = Counter(
    'quantpilot_ai_cache_hit_total',
    'AI cache hits per layer',
    ['layer']  # layer: L1_memory, L2_redis, L3_disk, compute
)


# ─────────────────────────────────────────────
# System Health Metrics
# ─────────────────────────────────────────────

ERROR_RATE = Counter(
    'quantpilot_error_rate_total',
    'Error occurrences',
    ['module', 'error_type', 'severity']  # severity: critical, high, medium, low
)

GHOST_POSITION_COUNT = Gauge(
    'quantpilot_ghost_position_count',
    'Number of ghost positions detected',
    ['exchange', 'symbol']
)

LEVERAGE_SETUP_FAILURE = Counter(
    'quantpilot_leverage_setup_failure_total',
    'Leverage setup failures',
    ['exchange', 'symbol', 'retry_attempt']
)

EXCHANGE_ERRORS = Counter(
    'quantpilot_exchange_errors_total',
    'Exchange API errors',
    ['exchange', 'error_type']
)

LEVERAGE_SETUP_FAILURE = Counter(
    'quantpilot_leverage_setup_failure_total',
    'Leverage setup failures',
    ['exchange', 'symbol', 'leverage', 'retry_attempt']
)

SYSTEM_INFO = Info(
    'quantpilot_system',
    'System information'
)


# ─────────────────────────────────────────────
# Cache Metrics
# ─────────────────────────────────────────────

CACHE_HIT_RATE = Gauge(
    'quantpilot_cache_hit_rate_pct',
    'Cache hit rate percentage',
    ['cache_name', 'layer']
)

CACHE_SIZE = Gauge(
    'quantpilot_cache_size',
    'Current cache size',
    ['cache_name', 'layer']
)


# ─────────────────────────────────────────────
# Database Metrics
# ─────────────────────────────────────────────

DB_CONNECTION_POOL_SIZE = Gauge(
    'quantpilot_db_connection_pool_size',
    'Database connection pool size',
    ['database']
)

DB_CONNECTION_POOL_USED = Gauge(
    'quantpilot_db_connection_pool_used',
    'Database connections in use',
    ['database']
)

DB_QUERY_LATENCY = Histogram(
    'quantpilot_db_query_latency_seconds',
    'Database query latency',
    ['query_type'],
    buckets=[0.01, 0.05, 0.1, 0.5, 1, 2, 5]
)


def setup_metrics() -> None:
    """Initialize metrics system and set baseline values.

    P3-FIX: Called during application startup.
    """
    if not PROMETHEUS_AVAILABLE:
        logger.warning("[P3-FIX] Prometheus client not installed, metrics disabled")
        return

    # Set system info
    try:
        from core.config import settings

        SYSTEM_INFO.info({
            'version': settings.app_version,
            'app_name': settings.app_name,
            'exchange': settings.exchange.name,
            'ai_provider': settings.ai.provider,
            'live_trading': str(settings.exchange.live_trading),
        })

        logger.info(
            f"[P3-FIX] Prometheus metrics initialized: "
            f"version={settings.app_version}, "
            f"exchange={settings.exchange.name}, "
            f"ai_provider={settings.ai.provider}"
        )

    except Exception as e:
        logger.warning(f"[P3-FIX] Failed to set system info: {e}")


def record_trade_metrics(
    exchange: str,
    symbol: str,
    direction: str,
    result: str,
    latency_seconds: float,
    stage: str = "execute",
) -> None:
    """Helper to record trade metrics.

    Args:
        exchange: Exchange name
        symbol: Trading symbol
        direction: Trade direction (long/short)
        result: Trade result (success/failed/skipped)
        latency_seconds: Execution latency
        stage: Execution stage (validate/pre_filter/ai_analyze/execute/confirm)
    """
    TRADE_TOTAL.labels(
        exchange=exchange,
        symbol=symbol,
        direction=direction,
        result=result
    ).inc()

    TRADE_LATENCY.labels(
        exchange=exchange,
        stage=stage
    ).observe(latency_seconds)


def record_ai_metrics(
    provider: str,
    model: str,
    result: str,
    latency_seconds: float,
    cache_layer: str = None,
) -> None:
    """Helper to record AI analysis metrics.

    Args:
        provider: AI provider
        model: Model name
        result: Analysis result (success/failed/timeout)
        latency_seconds: Analysis latency
        cache_layer: Cache layer if hit (L1_memory/L2_redis/L3_disk/compute)
    """
    AI_ANALYSIS_TOTAL.labels(
        provider=provider,
        model=model,
        result=result
    ).inc()

    AI_ANALYSIS_LATENCY.labels(
        provider=provider,
        model=model
    ).observe(latency_seconds)

    if cache_layer:
        AI_CACHE_HIT.labels(layer=cache_layer).inc()


def record_error_metrics(
    module: str,
    error_type: str,
    severity: str = "medium",
) -> None:
    """Helper to record error metrics.

    Args:
        module: Module name (ai_analyzer/exchange/position_monitor)
        error_type: Error type (NetworkError/Timeout/AuthenticationError)
        severity: Error severity (critical/high/medium/low)
    """
    ERROR_RATE.labels(
        module=module,
        error_type=error_type,
        severity=severity
    ).inc()


def record_leverage_failure(
    exchange: str,
    symbol: str,
    leverage: int,
    retry_attempt: int,
) -> None:
    """Helper to record leverage setup failure.

    Args:
        exchange: Exchange name
        symbol: Trading symbol
        leverage: Requested leverage
        retry_attempt: Retry attempt number
    """
    LEVERAGE_SETUP_FAILURE.labels(
        exchange=exchange,
        symbol=symbol,
        leverage=str(leverage),
        retry_attempt=str(retry_attempt)
    ).inc()


def update_position_metrics(
    exchange: str,
    symbol: str,
    direction: str,
    count: int,
) -> None:
    """Helper to update position count.

    Args:
        exchange: Exchange name
        symbol: Trading symbol
        direction: Position direction
        count: Number of positions
    """
    POSITION_COUNT.labels(
        exchange=exchange,
        symbol=symbol,
        direction=direction
    ).set(count)


def update_cache_metrics(
    cache_name: str,
    layer: str,
    hit_rate_pct: float,
    size: int,
) -> None:
    """Helper to update cache metrics.

    Args:
        cache_name: Cache instance name
        layer: Cache layer (L1/L2/L3)
        hit_rate_pct: Hit rate percentage
        size: Current cache size
    """
    CACHE_HIT_RATE.labels(
        cache_name=cache_name,
        layer=layer
    ).set(hit_rate_pct)

    CACHE_SIZE.labels(
        cache_name=cache_name,
        layer=layer
    ).set(size)


def update_ghost_position_metrics(
    exchange: str,
    symbol: str,
    count: int,
) -> None:
    """Helper to update ghost position count.

    Args:
        exchange: Exchange name
        symbol: Trading symbol
        count: Number of ghost positions
    """
    GHOST_POSITION_COUNT.labels(
        exchange=exchange,
        symbol=symbol
    ).set(count)


# ─────────────────────────────────────────────
# Additional Metrics for Recorders
# ─────────────────────────────────────────────

SIGNALS_RECEIVED = Counter(
    'quantpilot_signals_received_total',
    'Total number of signals received',
    ['ticker', 'direction', 'user_id']
)

SIGNALS_PASSED_PREFILTER = Counter(
    'quantpilot_signals_passed_prefilter_total',
    'Signals that passed pre-filter',
    ['ticker', 'direction']
)

SIGNALS_BLOCKED_PREFILTER = Counter(
    'quantpilot_signals_blocked_prefilter_total',
    'Signals blocked by pre-filter',
    ['ticker', 'direction', 'reason']
)

AI_CONFIDENCE = Histogram(
    'quantpilot_ai_confidence',
    'AI analysis confidence distribution',
    ['provider'],
    buckets=[0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0]
)

TRADES_EXECUTED = Counter(
    'quantpilot_trades_executed_total',
    'Total number of trades executed (legacy)',
    ['ticker', 'direction', 'status']
)

TRADES_PNL = Histogram(
    'quantpilot_trades_pnl_usdt',
    'Trade PnL distribution',
    ['ticker', 'direction'],
    buckets=[-100, -50, -20, -10, -5, -2, -1, 0, 1, 2, 5, 10, 20, 50, 100]
)

EXCHANGE_REQUESTS = Counter(
    'quantpilot_exchange_requests_total',
    'Exchange API requests',
    ['exchange', 'endpoint', 'status']
)

EXCHANGE_LATENCY = Histogram(
    'quantpilot_exchange_latency_seconds',
    'Exchange API latency',
    ['exchange', 'endpoint'],
    buckets=[0.1, 0.5, 1, 2, 5, 10, 20]
)

HTTP_REQUESTS = Counter(
    'quantpilot_http_requests_total',
    'HTTP API requests',
    ['method', 'path', 'status']
)

HTTP_LATENCY = Histogram(
    'quantpilot_http_latency_seconds',
    'HTTP request latency',
    ['method', 'path'],
    buckets=[0.01, 0.05, 0.1, 0.5, 1, 2, 5]
)

DB_POOL_SIZE = Gauge(
    'quantpilot_db_pool_size',
    'Database connection pool size',
    ['pool_type']
)

DB_POOL_OVERFLOW = Gauge(
    'quantpilot_db_pool_overflow',
    'Database pool overflow connections',
    ['pool_type']
)

DB_CONNECTIONS = Gauge(
    'quantpilot_db_connections',
    'Database connections count',
    ['status']
)

AI_COST_USD = Counter(
    'quantpilot_ai_cost_usd',
    'AI API cost in USD',
    ['provider']
)

TRADING_CONTROL_MODE = Gauge(
    'quantpilot_trading_control_mode',
    'Trading control mode',
    ['mode']
)

FILTER_PERFORMANCE = Histogram(
    'quantpilot_filter_performance_seconds',
    'Pre-filter check latency',
    ['check_name'],
    buckets=[0.001, 0.005, 0.01, 0.05, 0.1, 0.5]
)

EXCHANGE_POOL_SIZE = Gauge(
    'quantpilot_exchange_pool_size',
    'Exchange connection pool size',
    ['exchange']
)
