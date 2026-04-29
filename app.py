"""
QuantPilot AI v4.5.3 - Main Application Entry Point

Complete pipeline:
  TradingView Webhook -> Pre-Filter -> AI Analysis -> Trade Execution -> Notification

Usage:
  uvicorn app:app --host 0.0.0.0 --port 8000
"""
import io
import re
import sys
from pathlib import Path

from loguru import logger

from core.config import settings
from core.factory import create_app

# ─────────────────────────────────────────────
# Logging Setup
# ─────────────────────────────────────────────

logger.remove()
_SENSITIVE_LOG_RE = re.compile(
    r"(?i)(api[_-]?key|api[_-]?secret|secret|password|token)(['\"]?\s*[:=]\s*['\"]?)[^,'\"\s}]+"
)


def _sanitize_log_record(record):
    record["message"] = _SENSITIVE_LOG_RE.sub(r"\1\2***", record["message"])
    return True


# Console logging - UTF-8 wrapper for Windows GBK terminals
_stdout_utf8 = io.TextIOWrapper(
    sys.stdout.buffer,
    encoding="utf-8",
    errors="replace",
    line_buffering=True,
) if hasattr(sys.stdout, "buffer") else sys.stdout

logger.add(
    _stdout_utf8,
    level="DEBUG" if settings.debug else "INFO",
    format="{time:HH:mm:ss} | {level:<7} | {message}",
    filter=_sanitize_log_record,
    colorize=False,
)

# File logging
log_dir = Path(__file__).parent / "logs"
log_dir.mkdir(exist_ok=True)

logger.add(
    "logs/server_{time:YYYY-MM-DD}.log",
    rotation="100 MB",
    retention="30 days",
    level="DEBUG",
    encoding="utf-8",
    filter=_sanitize_log_record,
)

# JSON logging (optional)
if settings.json_logs:
    logger.add(
        "logs/server.jsonl",
        rotation="100 MB",
        retention="30 days",
        level="INFO",
        serialize=True,
        encoding="utf-8",
        filter=_sanitize_log_record,
    )

# ─────────────────────────────────────────────
# Create Application
# ─────────────────────────────────────────────

app = create_app()

# ─────────────────────────────────────────────
# Development Entry Point
# ─────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "app:app",
        host=settings.server.host,
        port=settings.server.port,
        reload=settings.debug,
    )
