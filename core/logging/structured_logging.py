"""
P3-FIX: Structured Logging Implementation
JSON-formatted logs with trace IDs, service metadata, and contextual fields.

Features:
    - JSON structure for machine parsing
    - Trace ID propagation across requests
    - Service metadata (name, version, environment)
    - Contextual fields (exchange, symbol, user_id)
    - Exception stack traces in structured format
    - Log rotation and compression
    - Integration with observability platforms (Grafana Loki, ELK)
"""
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from loguru import logger


class StructuredFormatter:
    """JSON-structured log formatter.

    P3-FIX: Produces machine-readable logs for observability platforms.

    Output format:
    {
        "timestamp": "2026-05-06T10:00:00.123Z",
        "service": "QuantPilot",
        "version": "4.5.5",
        "level": "INFO",
        "message": "Trade executed successfully",
        "module": "exchange",
        "function": "execute_trade",
        "line": 1050,
        "trace_id": "abc123",
        "user_id": "user-001",
        "exchange": "binance",
        "symbol": "BTCUSDT",
        "extra": {...},
        "exception": {...}  // if error
    }
    """

    def __init__(
        self,
        service_name: str = "QuantPilot",
        version: str = "4.5.5",
        environment: str = "production",
    ):
        self.service_name = service_name
        self.version = version
        self.environment = environment

    def format(self, record: dict) -> str:
        """Format log record as JSON string.

        Args:
            record: Loguru record dict

        Returns:
            JSON-formatted log string
        """
        # Base log structure
        log_data = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "service": self.service_name,
            "version": self.version,
            "environment": self.environment,
            "level": record["level"].name,
            "level_no": record["level"].no,
            "message": record["message"],
            "module": record["module"],
            "function": record["function"],
            "line": record["line"],
            "file": record["file"].path if record.get("file") else "",
            "process_id": record["process"].id if record.get("process") else None,
            "thread_id": record["thread"].id if record.get("thread") else None,
        }

        # Add contextual fields from extra
        extra = record.get("extra", {})

        # Trace ID (for request tracing)
        log_data["trace_id"] = extra.get("trace_id", "")

        # User context
        log_data["user_id"] = extra.get("user_id", "")

        # Trading context
        log_data["exchange"] = extra.get("exchange", "")
        log_data["symbol"] = extra.get("symbol", "")
        log_data["direction"] = extra.get("direction", "")
        log_data["position_id"] = extra.get("position_id", "")

        # Performance metrics (if present)
        log_data["duration_ms"] = extra.get("duration_ms", None)
        log_data["latency_seconds"] = extra.get("latency_seconds", None)

        # Additional extra fields
        additional_extra = {}
        for key, value in extra.items():
            if key not in {
                "trace_id", "user_id", "exchange", "symbol",
                "direction", "position_id", "duration_ms", "latency_seconds"
            }:
                additional_extra[key] = value

        if additional_extra:
            log_data["extra"] = additional_extra

        # Exception information (if present)
        if record.get("exception"):
            exception_info = record["exception"]
            log_data["exception"] = {
                "type": exception_info.type.__name__ if exception_info.type else "Unknown",
                "message": str(exception_info),
                "traceback": exception_info.traceback if exception_info.traceback else "",
            }
            log_data["exception_type"] = exception_info.type.__name__ if exception_info.type else "Unknown"

        # Convert to JSON
        try:
            return json.dumps(log_data, default=str, ensure_ascii=False, separators=(",", ":"))
        except Exception as e:
            # Fallback if JSON serialization fails
            return json.dumps({
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "level": "ERROR",
                "message": f"Log serialization error: {e}",
                "original_message": str(record.get("message", "")),
            }, ensure_ascii=False)


def setup_structured_logging(
    log_dir: str = "./logs",
    rotation: str = "00:00",  # Rotate daily at midnight
    retention: str = "30 days",
    compression: str = "gz",
    json_logs: bool = True,
    console_output: bool = True,
    console_json: bool = False,  # Human-readable console
    service_name: str = "QuantPilot",
    version: str = "4.5.5",
    environment: str = "production",
) -> None:
    """Setup structured logging with Loguru.

    P3-FIX: Configures JSON-structured logs for production observability.

    Args:
        log_dir: Directory for log files
        rotation: Rotation schedule (daily by default)
        retention: How long to keep old logs
        compression: Compression format for rotated logs
        json_logs: Enable JSON-formatted file logs
        console_output: Enable console output
        console_json: Use JSON format for console (False = human-readable)
        service_name: Service name metadata
        version: Version metadata
        environment: Environment metadata
    """
    # Remove default logger
    logger.remove()

    # Create log directory
    log_path = Path(log_dir)
    log_path.mkdir(parents=True, exist_ok=True)

    formatter = StructuredFormatter(
        service_name=service_name,
        version=version,
        environment=environment,
    )

    # File logger (JSON structured)
    if json_logs:
        logger.add(
            str(log_path / "quantpilot_{time:YYYY-MM-DD}.json"),
            rotation=rotation,
            retention=retention,
            compression=compression,
            format=formatter.format,
            level="DEBUG",
            enqueue=True,  # Async logging
            backtrace=True,
            diagnose=True,
            serialize=True,
        )
        logger.info(f"[P3-FIX] JSON structured logging enabled: {log_path}")

    # Console logger (human-readable or JSON)
    if console_output:
        if console_json:
            # JSON console output (for containerized environments)
            logger.add(
                sys.stdout,
                format=formatter.format,
                level="INFO",
                colorize=False,
            )
        else:
            # Human-readable console with colors
            logger.add(
                sys.stdout,
                format="<green>{time:YYYY-MM-DD HH:mm:ss}</green> | "
                       "<level>{level: <8}</level> | "
                       "<cyan>{module}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> | "
                       "<level>{message}</level>",
                level="INFO",
                colorize=True,
            )

    # Error-only log file (for quick error review)
    logger.add(
        str(log_path / "errors_{time:YYYY-MM-DD}.log"),
        rotation=rotation,
        retention=retention,
        compression=compression,
        format="{time:YYYY-MM-DD HH:mm:ss} | {level} | {module}:{function}:{line} | {message}",
        level="ERROR",
        filter=lambda record: record["level"].no >= 40,  # ERROR and above
        enqueue=True,
    )

    logger.info(
        f"[P3-FIX] Logging configured: "
        f"service={service_name}, version={version}, env={environment}, "
        f"json_logs={json_logs}, console={console_output}"
    )


def log_with_context(
    level: str,
    message: str,
    trace_id: str | None = None,
    user_id: str | None = None,
    exchange: str | None = None,
    symbol: str | None = None,
    direction: str | None = None,
    position_id: str | None = None,
    **kwargs: Any,
) -> None:
    """Log with explicit context fields.

    P3-FIX: Helper function for structured logging with context.

    Args:
        level: Log level (DEBUG/INFO/WARNING/ERROR/CRITICAL)
        message: Log message
        trace_id: Request trace ID
        user_id: User ID
        exchange: Exchange name
        symbol: Trading symbol
        direction: Trade direction
        position_id: Position ID
        **kwargs: Additional context fields
    """
    # Build context dict
    context = {}
    if trace_id:
        context["trace_id"] = trace_id
    if user_id:
        context["user_id"] = user_id
    if exchange:
        context["exchange"] = exchange
    if symbol:
        context["symbol"] = symbol
    if direction:
        context["direction"] = direction
    if position_id:
        context["position_id"] = position_id

    # Add additional kwargs
    context.update(kwargs)

    # Log with context
    logger.bind(**context).log(level.upper(), message)


def log_trade_event(
    action: str,
    exchange: str,
    symbol: str,
    direction: str,
    status: str,
    trace_id: str | None = None,
    user_id: str | None = None,
    order_id: str | None = None,
    latency_seconds: float | None = None,
    **kwargs: Any,
) -> None:
    """Log trade event with standardized fields.

    P3-FIX: Standardized trade event logging for observability.

    Args:
        action: Action name (execute/confirm/fail/skip)
        exchange: Exchange name
        symbol: Trading symbol
        direction: Trade direction
        status: Trade status
        trace_id: Trace ID
        user_id: User ID
        order_id: Order ID
        latency_seconds: Execution latency
        **kwargs: Additional fields
    """
    context = {
        "action": action,
        "exchange": exchange,
        "symbol": symbol,
        "direction": direction,
        "trade_status": status,
    }

    if trace_id:
        context["trace_id"] = trace_id
    if user_id:
        context["user_id"] = user_id
    if order_id:
        context["order_id"] = order_id
    if latency_seconds:
        context["latency_seconds"] = latency_seconds

    context.update(kwargs)

    # Determine log level based on status
    if status in {"failed", "error", "timeout"}:
        level = "ERROR"
    elif status in {"skipped", "rejected"}:
        level = "WARNING"
    else:
        level = "INFO"

    logger.bind(**context).log(
        level,
        f"[TRADE] {action}: {symbol} {direction} on {exchange} - {status}",
    )


def log_ai_analysis(
    provider: str,
    model: str,
    ticker: str,
    direction: str,
    result: str,
    confidence: float,
    cache_layer: str | None = None,
    latency_seconds: float | None = None,
    trace_id: str | None = None,
    **kwargs: Any,
) -> None:
    """Log AI analysis event with standardized fields.

    P3-FIX: Standardized AI logging for observability.

    Args:
        provider: AI provider name
        model: Model name
        ticker: Trading ticker
        direction: Signal direction
        result: Analysis result
        confidence: Confidence score
        cache_layer: Cache layer (L1/L2/L3/compute)
        latency_seconds: Analysis latency
        trace_id: Trace ID
        **kwargs: Additional fields
    """
    context = {
        "ai_provider": provider,
        "ai_model": model,
        "ticker": ticker,
        "direction": direction,
        "ai_result": result,
        "confidence": confidence,
    }

    if cache_layer:
        context["cache_layer"] = cache_layer
    if latency_seconds:
        context["latency_seconds"] = latency_seconds
    if trace_id:
        context["trace_id"] = trace_id

    context.update(kwargs)

    # Determine log level
    if result in {"failed", "timeout", "error"}:
        level = "ERROR"
    elif result == "cached":
        level = "DEBUG"
    else:
        level = "INFO"

    logger.bind(**context).log(
        level,
        f"[AI] {provider}/{model}: {ticker} {direction} - {result} (confidence={confidence:.2f})",
    )


def log_system_error(
    module: str,
    error_type: str,
    error_message: str,
    severity: str = "high",
    trace_id: str | None = None,
    exception: Exception | None = None,
    **kwargs: Any,
) -> None:
    """Log system error with standardized fields.

    P3-FIX: Standardized error logging for observability.

    Args:
        module: Module name
        error_type: Error type
        error_message: Error message
        severity: Severity (critical/high/medium/low)
        trace_id: Trace ID
        exception: Exception object
        **kwargs: Additional fields
    """
    context = {
        "module": module,
        "error_type": error_type,
        "severity": severity,
    }

    if trace_id:
        context["trace_id"] = trace_id

    context.update(kwargs)

    # Determine log level from severity
    severity_to_level = {
        "critical": "CRITICAL",
        "high": "ERROR",
        "medium": "WARNING",
        "low": "WARNING",
    }
    level = severity_to_level.get(severity, "ERROR")

    if exception:
        logger.bind(**context).log(
            level,
            f"[ERROR] {module}: {error_type} - {error_message}",
            exception=exception,
        )
    else:
        logger.bind(**context).log(
            level,
            f"[ERROR] {module}: {error_type} - {error_message}",
        )
