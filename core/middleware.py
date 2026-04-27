"""
QuantPilot AI - Middleware
Request processing middleware including rate limiting, logging, and security.
"""
import time
import json
import re
import hmac
import hashlib
import uuid
from typing import Callable, Optional
from datetime import datetime

from fastapi import Request, Response
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.cors import CORSMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware
from loguru import logger

from core.config import settings
from core.request_utils import client_ip


# ─────────────────────────────────────────────
# Request ID Middleware
# ─────────────────────────────────────────────

class RequestIDMiddleware(BaseHTTPMiddleware):
    """Inject a unique X-Request-ID into every request/response for tracing."""

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        request_id = request.headers.get("x-request-id", "") or str(uuid.uuid4())[:12]
        request.state.request_id = request_id

        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        return response


# ─────────────────────────────────────────────
# Logging Middleware
# ─────────────────────────────────────────────

_SENSITIVE_LOG_RE = re.compile(
    r"(?i)(api[_-]?key|api[_-]?secret|secret|password|token)(['\"]?\s*[:=]\s*['\"]?)[^,'\"\s}]+"
)


def _sanitize_log_message(message: str) -> str:
    """Sanitize sensitive data from log messages."""
    return _SENSITIVE_LOG_RE.sub(r"\1\2***", message)


class LoggingMiddleware(BaseHTTPMiddleware):
    """Middleware for request/response logging."""

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        # Skip logging for health checks
        if request.url.path in {"/health", "/metrics"}:
            return await call_next(request)

        start_time = time.time()
        request_id = getattr(request.state, "request_id", "")

        # Log request
        ip = self._get_client_ip(request)
        logger.info(
            f"[Request] {request.method} {request.url.path} "
            f"from {ip} rid={request_id}"
        )

        try:
            response = await call_next(request)
            duration = time.time() - start_time

            # Log response
            logger.info(
                f"[Response] {request.method} {request.url.path} "
                f"{response.status_code} in {duration:.3f}s rid={request_id}"
            )

            # Add timing header
            response.headers["X-Response-Time"] = f"{duration:.3f}s"
            return response

        except Exception as e:
            duration = time.time() - start_time
            logger.error(
                f"[Request] {request.method} {request.url.path} "
                f"failed after {duration:.3f}s rid={request_id}: {e}"
            )
            raise

    def _get_client_ip(self, request: Request) -> str:
        """Extract client IP from request."""
        return client_ip(request)


# ─────────────────────────────────────────────
# Rate Limiting Middleware
# ─────────────────────────────────────────────

class RateLimitMiddleware(BaseHTTPMiddleware):
    """Simple in-memory rate limiting middleware."""

    def __init__(self, app, enabled: bool = True):
        super().__init__(app)
        self.enabled = enabled
        self._login_attempts: dict[str, list[float]] = {}
        self._register_attempts: dict[str, list[float]] = {}
        self._webhook_attempts: dict[str, list[float]] = {}
        self._api_requests: dict[str, list[float]] = {}
        self._lock = __import__("threading").Lock()

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        if not self.enabled:
            return await call_next(request)

        ip = self._get_client_ip(request)
        path = request.url.path

        # Login rate limiting
        if path == "/api/auth/login" and request.method == "POST":
            if not self._check_rate(
                self._login_attempts,
                ip,
                settings.rate_limit.login_max_attempts,
                settings.rate_limit.login_window_secs,
            ):
                logger.warning(f"[RateLimit] Login rate limit hit for {ip}")
                return JSONResponse(
                    status_code=429,
                    content={"detail": "Too many login attempts. Please wait 5 minutes."}
                )

        # Register rate limiting
        elif path == "/api/auth/register" and request.method == "POST":
            if not self._check_rate(
                self._register_attempts,
                ip,
                settings.rate_limit.register_max_attempts,
                settings.rate_limit.register_window_secs,
            ):
                logger.warning(f"[RateLimit] Register rate limit hit for {ip}")
                return JSONResponse(
                    status_code=429,
                    content={"detail": "Too many registration attempts. Please wait 10 minutes."}
                )

        # Webhook rate limiting — 30 requests per 60 seconds per IP
        elif path == "/webhook" and request.method == "POST":
            if not self._check_rate(
                self._webhook_attempts,
                ip,
                settings.rate_limit.webhook_max_attempts,
                settings.rate_limit.webhook_window_secs,
            ):
                logger.warning(f"[RateLimit] Webhook rate limit hit for {ip}")
                return JSONResponse(
                    status_code=429,
                    content={"detail": "Too many webhook requests. Please slow down."}
                )

        # General API rate limiting
        elif path.startswith("/api/"):
            if not self._check_rate(
                self._api_requests,
                ip,
                120,  # 120 requests per minute
                60,
            ):
                logger.warning(f"[RateLimit] API rate limit hit for {ip}")
                return JSONResponse(
                    status_code=429,
                    content={"detail": "Too many requests. Please slow down."}
                )

        response = await call_next(request)

        # Clear rate limit on successful login
        if path == "/api/auth/login" and response.status_code == 200:
            with self._lock:
                self._login_attempts.pop(ip, None)

        return response

    def _check_rate(
        self,
        store: dict[str, list[float]],
        key: str,
        max_attempts: int,
        window_secs: int,
    ) -> bool:
        """Check if request is within rate limit."""
        now = time.time()
        cutoff = now - window_secs

        with self._lock:
            attempts = [t for t in store.get(key, []) if t > cutoff]
            if len(attempts) >= max_attempts:
                store[key] = attempts
                return False
            attempts.append(now)
            store[key] = attempts
            return True

    def _get_client_ip(self, request: Request) -> str:
        """Extract client IP from request."""
        return client_ip(request)


# ─────────────────────────────────────────────
# CSRF Middleware
# ─────────────────────────────────────────────

CSRF_COOKIE_NAME = "tvss_csrf"


class CSRFMiddleware(BaseHTTPMiddleware):
    """CSRF protection middleware."""

    EXEMPT_PATHS = {
        "/webhook",
        "/api/auth/login",
        "/api/auth/register",
        "/api/auth/logout",
        "/api/auth/2fa/verify",
    }
    SAFE_METHODS = {"GET", "HEAD", "OPTIONS", "TRACE"}

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        # Skip for safe methods
        if request.method in self.SAFE_METHODS:
            return await call_next(request)

        # Skip for exempt paths
        if request.url.path in self.EXEMPT_PATHS:
            return await call_next(request)

        # Background Sync runs from the service worker, which cannot read the
        # non-HttpOnly CSRF cookie after the page is gone. Require a custom
        # same-origin header so plain cross-site form posts still cannot hit it.
        if (
            request.url.path == "/api/user/trades/sync"
            and request.headers.get("x-pwa-sync") == "1"
        ):
            return await call_next(request)

        # Check for auth cookie
        if not request.cookies.get("tvss_token"):
            return await call_next(request)

        # Validate CSRF token
        cookie_token = request.cookies.get(CSRF_COOKIE_NAME, "")
        header_token = request.headers.get("x-csrf-token", "")

        if not cookie_token or not header_token:
            return JSONResponse(
                status_code=403,
                content={"detail": "CSRF token missing"}
            )

        if not hmac.compare_digest(cookie_token, header_token):
            return JSONResponse(
                status_code=403,
                content={"detail": "CSRF token invalid"}
            )

        return await call_next(request)


# ─────────────────────────────────────────────
# Request Size Limit Middleware
# ─────────────────────────────────────────────

class RequestSizeLimitMiddleware(BaseHTTPMiddleware):
    """Limit request body size."""

    MAX_SIZE = 100_000  # 100KB

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        if request.method in {"POST", "PUT", "PATCH"}:
            content_length = request.headers.get("content-length")
            if content_length:
                try:
                    size = int(content_length)
                    if size > self.MAX_SIZE:
                        return JSONResponse(
                            status_code=413,
                            content={"detail": "Request body too large"}
                        )
                    return await call_next(request)
                except ValueError:
                    pass

            body = await request.body()
            if len(body) > self.MAX_SIZE:
                return JSONResponse(
                    status_code=413,
                    content={"detail": "Request body too large"}
                )

            async def receive():
                return {"type": "http.request", "body": body, "more_body": False}

            request._receive = receive  # type: ignore[attr-defined]

        return await call_next(request)


# ─────────────────────────────────────────────
# Security Headers Middleware
# ─────────────────────────────────────────────

class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Add security headers to responses."""

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        response = await call_next(request)

        # Core security headers
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-XSS-Protection"] = "1; mode=block"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"

        # HSTS — only when served over HTTPS or behind a proxy
        forwarded_proto = request.headers.get("x-forwarded-proto", "").split(",")[0].strip().lower()
        if request.url.scheme == "https" or forwarded_proto == "https":
            response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"

        # Content Security Policy for HTML responses
        if "text/html" in response.headers.get("content-type", ""):
            response.headers["Content-Security-Policy"] = (
                "default-src 'self'; "
                "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
                "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com https://fonts.bunny.net https://cdn.jsdelivr.net; "
                "font-src 'self' https://fonts.gstatic.com https://fonts.bunny.net https://cdn.jsdelivr.net; "
                "img-src 'self' data: https:; "
                "connect-src 'self' https: wss:;"
            )

        return response


# ─────────────────────────────────────────────
# Middleware Setup
# ─────────────────────────────────────────────

def setup_middleware(app):
    """Setup all middleware for the FastAPI app."""
    from fastapi import FastAPI

    # CORS
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.server.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Trusted hosts (production)
    if settings.server.trusted_hosts and settings.server.trusted_hosts != ["*"]:
        app.add_middleware(
            TrustedHostMiddleware,
            allowed_hosts=settings.server.trusted_hosts,
        )

    # Custom middleware (order matters — outermost first)
    app.add_middleware(SecurityHeadersMiddleware)
    app.add_middleware(RequestSizeLimitMiddleware)
    app.add_middleware(CSRFMiddleware)
    app.add_middleware(RateLimitMiddleware, enabled=settings.rate_limit.enabled)
    app.add_middleware(LoggingMiddleware)
    app.add_middleware(RequestIDMiddleware)
