"""
QuantPilot AI - Unified Exception Framework

Provides a hierarchy of domain-specific exceptions so that call sites can
catch precisely the errors they expect instead of the overly-broad
``except Exception`` anti-pattern.

Usage:
    from core.exceptions import (
        TradingSystemError,
        ExchangeError,
        InsufficientFundsError,
        OrderValidationError,
        RiskLimitExceededError,
        AIAnalysisError,
    )
"""
from __future__ import annotations


class TradingSystemError(Exception):
    """Base exception for all domain errors in the trading system."""

    def __init__(self, message: str, *, error_code: str = "UNKNOWN", context: dict | None = None):
        super().__init__(message)
        self.error_code = error_code
        self.context = context or {}


# ---------------------------------------------------------------------------
# Exchange / Execution
# ---------------------------------------------------------------------------

class ExchangeError(TradingSystemError):
    """Raised when an exchange API call fails."""

    def __init__(self, message: str, *, error_code: str = "EXCHANGE_ERROR", context: dict | None = None):
        super().__init__(message, error_code=error_code, context=context)


class InsufficientFundsError(ExchangeError):
    """Raised when the exchange reports insufficient balance."""

    def __init__(self, message: str = "Insufficient funds", *, context: dict | None = None):
        super().__init__(message, error_code="INSUFFICIENT_FUNDS", context=context)


class OrderNotFoundError(ExchangeError):
    """Raised when an exchange order cannot be found."""

    def __init__(self, order_id: str = "", *, context: dict | None = None):
        super().__init__(f"Order not found: {order_id}", error_code="ORDER_NOT_FOUND", context=context)


class OrderValidationError(ExchangeError):
    """Raised when an order fails local validation (size, price, etc.)."""

    def __init__(self, message: str, *, context: dict | None = None):
        super().__init__(message, error_code="ORDER_VALIDATION", context=context)


class PositionCloseError(ExchangeError):
    """Raised when closing a position fails."""

    def __init__(self, message: str, *, context: dict | None = None):
        super().__init__(message, error_code="POSITION_CLOSE_FAILED", context=context)


class NetworkError(ExchangeError):
    """Raised on transient network issues (retryable)."""

    def __init__(self, message: str = "Network error", *, context: dict | None = None):
        super().__init__(message, error_code="NETWORK_ERROR", context=context)


class AuthenticationError(ExchangeError):
    """Raised on API key / permission issues."""

    def __init__(self, message: str = "Authentication failed", *, context: dict | None = None):
        super().__init__(message, error_code="AUTH_ERROR", context=context)


# ---------------------------------------------------------------------------
# Risk / Pre-filter
# ---------------------------------------------------------------------------

class RiskLimitExceededError(TradingSystemError):
    """Raised when a risk limit (daily loss, max positions, etc.) is exceeded."""

    def __init__(self, message: str, *, error_code: str = "RISK_LIMIT", context: dict | None = None):
        super().__init__(message, error_code=error_code, context=context)


class PreFilterBlockedError(TradingSystemError):
    """Raised when the pre-filter blocks a signal."""

    def __init__(self, reason: str, *, score: float = 0.0, checks: dict | None = None):
        super().__init__(reason, error_code="PREFILTER_BLOCKED", context={"score": score, "checks": checks or {}})
        self.score = score
        self.checks = checks or {}


# ---------------------------------------------------------------------------
# AI / Analysis
# ---------------------------------------------------------------------------

class AIAnalysisError(TradingSystemError):
    """Raised when the AI analysis pipeline fails."""

    def __init__(self, message: str, *, error_code: str = "AI_ERROR", context: dict | None = None):
        super().__init__(message, error_code=error_code, context=context)


class AIParseError(AIAnalysisError):
    """Raised when the AI returns unparseable output."""

    def __init__(self, message: str = "Failed to parse AI response", *, context: dict | None = None):
        super().__init__(message, error_code="AI_PARSE_ERROR", context=context)


class AIRetryExhaustedError(AIAnalysisError):
    """Raised when all AI retry attempts are exhausted."""

    def __init__(self, message: str = "AI retries exhausted", *, context: dict | None = None):
        super().__init__(message, error_code="AI_RETRY_EXHAUSTED", context=context)


# ---------------------------------------------------------------------------
# Database / Storage
# ---------------------------------------------------------------------------

class DatabaseError(TradingSystemError):
    """Raised on database operation failures."""

    def __init__(self, message: str, *, context: dict | None = None):
        super().__init__(message, error_code="DB_ERROR", context=context)


# ---------------------------------------------------------------------------
# Configuration / Validation
# ---------------------------------------------------------------------------

class ConfigValidationError(TradingSystemError):
    """Raised when a configuration value is invalid."""

    def __init__(self, message: str, *, context: dict | None = None):
        super().__init__(message, error_code="CONFIG_INVALID", context=context)


class WebhookValidationError(TradingSystemError):
    """Raised when a webhook payload fails validation."""

    def __init__(self, message: str, *, context: dict | None = None):
        super().__init__(message, error_code="WEBHOOK_INVALID", context=context)


# ---------------------------------------------------------------------------
# Retry classification helper
# ---------------------------------------------------------------------------

def is_retryable(exc: Exception) -> bool:
    """Return True if the exception represents a transient failure that may succeed on retry."""
    if isinstance(exc, (NetworkError,)):
        return True
    if isinstance(exc, ExchangeError):
        # Most exchange errors are not retryable unless explicitly network-related
        return False
    if isinstance(exc, (AIAnalysisError, DatabaseError)):
        # AI and DB errors may be transient
        return True
    return False
