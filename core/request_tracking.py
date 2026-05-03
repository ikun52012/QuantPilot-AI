"""
QuantPilot AI - Request Tracking Middleware
Provides request ID tracking for log correlation and debugging.
"""
import uuid
from collections.abc import Callable
from contextvars import ContextVar

from fastapi import Request, Response
from loguru import logger
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.types import ASGIApp

request_id_var: ContextVar[str] = ContextVar("request_id", default="")
request_start_time: ContextVar[float] = ContextVar("request_start_time", default=0.0)


def get_request_id() -> str:
    """Get current request ID from context."""
    return request_id_var.get()


def get_request_duration_ms() -> float:
    """Get request duration in milliseconds."""
    import time
    start = request_start_time.get()
    if start > 0:
        return (time.time() - start) * 1000
    return 0.0


class RequestTrackingMiddleware(BaseHTTPMiddleware):
    """Middleware to track requests with unique IDs and timing."""

    def __init__(
        self,
        app: ASGIApp,
        header_name: str = "X-Request-ID",
        generate_if_missing: bool = True,
    ):
        super().__init__(app)
        self.header_name = header_name
        self.generate_if_missing = generate_if_missing

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        import time

        # Get or generate request ID
        request_id = request.headers.get(self.header_name, "")
        if not request_id and self.generate_if_missing:
            request_id = str(uuid.uuid4())[:8]

        # Store in context
        request_id_var.set(request_id)
        request_start_time.set(time.time())

        # Bind to logger
        bound_logger = logger.bind(request_id=request_id)

        # Process request
        try:
            response: Response = await call_next(request)
        except Exception as e:
            duration_ms = get_request_duration_ms()
            bound_logger.error(
                f"[Request] {request.method} {request.url.path} failed: {e}",
                duration_ms=duration_ms,
                status="error",
            )
            raise

        # Add request ID to response headers
        response.headers[self.header_name] = request_id

        # Log request completion
        duration_ms = get_request_duration_ms()
        bound_logger.info(
            f"[Request] {request.method} {request.url.path} {response.status_code}",
            duration_ms=duration_ms,
            status=response.status_code,
        )

        return response


class ExceptionHandlerMiddleware(BaseHTTPMiddleware):
    """Middleware to handle exceptions with structured responses."""

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        from fastapi.responses import JSONResponse

        from core.errors import TradingSystemError

        try:
            return await call_next(request)
        except TradingSystemError as e:
            logger.error(
                f"[{e.error_code}] {e.message}",
                request_id=get_request_id(),
                error_category=e.error_category,
                context=e.context,
            )
            return JSONResponse(
                status_code=400 if e.recoverable else 500,
                content={
                    "status": "error",
                    "error_code": e.error_code,
                    "error_category": e.error_category,
                    "message": e.message,
                    "recoverable": e.recoverable,
                    "request_id": get_request_id(),
                },
            )
        except Exception as e:
            logger.exception(
                f"[UNEXPECTED] {e}",
                request_id=get_request_id(),
            )
            return JSONResponse(
                status_code=500,
                content={
                    "status": "error",
                    "error_code": "UNEXPECTED_ERROR",
                    "message": str(e),
                    "request_id": get_request_id(),
                },
            )
