"""
QuantPilot AI - Unified Error Handling Framework
Provides structured exception handling with error codes and context tracking.
"""
from typing import Any


class TradingSystemError(Exception):
    """Base exception class for all trading system errors."""

    error_code: str = "UNKNOWN"
    error_category: str = "general"
    recoverable: bool = True
    context: dict[str, Any] = {}

    def __init__(
        self,
        error_code: str | None = None,
        message: str = "",
        context: dict[str, Any] | None = None,
        recoverable: bool = True,
    ):
        self.error_code = error_code or self.error_code
        self.message = message
        self.context = context or {}
        self.recoverable = recoverable
        super().__init__(f"[{self.error_code}] {message}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "error_code": self.error_code,
            "error_category": self.error_category,
            "message": self.message,
            "recoverable": self.recoverable,
            "context": self.context,
        }


class ExchangeError(TradingSystemError):
    """Exchange-related errors."""

    error_category = "exchange"

    def __init__(self, error_code: str = "EXCHANGE_ERROR", **kwargs):
        super().__init__(error_code, **kwargs)


class AIAnalysisError(TradingSystemError):
    """AI analysis-related errors."""

    error_category = "ai"

    def __init__(self, error_code: str = "AI_ERROR", **kwargs):
        super().__init__(error_code, **kwargs)


class PositionError(TradingSystemError):
    """Position-related errors."""

    error_category = "position"

    def __init__(self, error_code: str = "POSITION_ERROR", **kwargs):
        super().__init__(error_code, **kwargs)


class RiskError(TradingSystemError):
    """Risk management errors."""

    error_category = "risk"
    recoverable = False

    def __init__(self, error_code: str = "RISK_ERROR", **kwargs):
        super().__init__(error_code, **kwargs)


class DatabaseError(TradingSystemError):
    """Database-related errors."""

    error_category = "database"

    def __init__(self, error_code: str = "DATABASE_ERROR", **kwargs):
        super().__init__(error_code, **kwargs)


class WebhookError(TradingSystemError):
    """Webhook processing errors."""

    error_category = "webhook"

    def __init__(self, error_code: str = "WEBHOOK_ERROR", **kwargs):
        super().__init__(error_code, **kwargs)


class ValidationError(TradingSystemError):
    """Validation-related errors."""

    error_category = "validation"

    def __init__(self, error_code: str = "VALIDATION_ERROR", **kwargs):
        super().__init__(error_code, **kwargs)


class ConfigurationError(TradingSystemError):
    """Configuration-related errors."""

    error_category = "config"
    recoverable = False

    def __init__(self, error_code: str = "CONFIG_ERROR", **kwargs):
        super().__init__(error_code, **kwargs)


class AuthenticationError(TradingSystemError):
    """Authentication-related errors."""

    error_category = "auth"
    recoverable = False

    def __init__(self, error_code: str = "AUTH_ERROR", **kwargs):
        super().__init__(error_code, **kwargs)


class RateLimitError(TradingSystemError):
    """Rate limiting errors."""

    error_category = "rate_limit"
    recoverable = True

    def __init__(self, error_code: str = "RATE_LIMIT", retry_after: float = 60.0, **kwargs):
        kwargs.setdefault("context", {}).update({"retry_after": retry_after})
        super().__init__(error_code, **kwargs)


class InsufficientFundsError(ExchangeError):
    """Insufficient funds for trade."""

    error_code = "INSUFFICIENT_FUNDS"
    recoverable = False


class OrderRejectedError(ExchangeError):
    """Order rejected by exchange."""

    error_code = "ORDER_REJECTED"


class APIKeyError(ExchangeError):
    """Invalid API key."""

    error_code = "INVALID_API_KEY"
    recoverable = False


class NetworkTimeoutError(ExchangeError):
    """Network timeout."""

    error_code = "NETWORK_TIMEOUT"
    recoverable = True


class PositionNotFoundError(PositionError):
    """Position not found."""

    error_code = "POSITION_NOT_FOUND"


class PositionConflictError(PositionError):
    """Position conflict detected."""

    error_code = "POSITION_CONFLICT"


class SLTPSetupError(PositionError):
    """Stop loss/take profit setup failed."""

    error_code = "SLTP_SETUP_FAILED"


class RiskLimitExceededError(RiskError):
    """Risk limit exceeded."""

    error_code = "RISK_LIMIT_EXCEEDED"


class CorrelationRiskError(RiskError):
    """Correlation risk exceeded."""

    error_code = "CORRELATION_RISK"


class DuplicateSignalError(WebhookError):
    """Duplicate webhook signal."""

    error_code = "DUPLICATE_SIGNAL"
    recoverable = False


class SignalValidationError(WebhookError):
    """Signal validation failed."""

    error_code = "INVALID_SIGNAL"


class AIResponseParseError(AIAnalysisError):
    """Failed to parse AI response."""

    error_code = "AI_PARSE_ERROR"


class AIProviderError(AIAnalysisError):
    """AI provider API error."""

    error_code = "AI_PROVIDER_ERROR"


class DatabaseConnectionError(DatabaseError):
    """Database connection failed."""

    error_code = "DB_CONNECTION_ERROR"
    recoverable = True


class DatabaseQueryError(DatabaseError):
    """Database query failed."""

    error_code = "DB_QUERY_ERROR"
