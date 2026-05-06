"""
QuantPilot AI - Middleware
Request processing middleware including rate limiting, logging, and security.
"""
import hmac
import re
import time
import uuid
from collections.abc import Awaitable, Callable

from fastapi import Request, Response
from fastapi.responses import JSONResponse
from loguru import logger
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.cors import CORSMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware

from core.config import settings
from core.metrics import record_http_request
from core.request_utils import client_ip

# ─────────────────────────────────────────────
# Request ID Middleware
# ─────────────────────────────────────────────

class RequestIDMiddleware(BaseHTTPMiddleware):
    """Inject a unique X-Request-ID into every request/response for tracing."""

    async def dispatch(self, request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
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

    async def dispatch(self, request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
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

            # Record Prometheus metrics
            record_http_request(
                method=request.method,
                path=request.url.path,
                status=response.status_code,
                latency=duration,
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
            # Record failed request metrics
            record_http_request(
                method=request.method,
                path=request.url.path,
                status=500,
                latency=duration,
            )
            raise

    def _get_client_ip(self, request: Request) -> str:
        """Extract client IP from request."""
        return str(client_ip(request))


# ─────────────────────────────────────────────
# Rate Limiting Middleware
# ─────────────────────────────────────────────

class RateLimitMiddleware(BaseHTTPMiddleware):
    """Rate limiting middleware with in-memory store and optional Redis backing.

    When Redis is enabled (settings.redis.enabled = true), rate-limit counters
    are persisted in Redis, surviving restarts and working across multiple
    processes.  Falls back to in-memory dicts when Redis is unavailable.
    """

    def __init__(self, app, enabled: bool = True):
        super().__init__(app)
        self.enabled = enabled
        self._login_attempts: dict[str, list[float]] = {}
        self._register_attempts: dict[str, list[float]] = {}
        self._webhook_attempts: dict[str, list[float]] = {}
        self._api_requests: dict[str, list[float]] = {}
        self._lock = __import__("threading").Lock()
        self._redis = None
        self._redis_checked = False

    async def _get_redis(self):
        """Lazily retrieve Redis client from the cache manager."""
        if self._redis_checked:
            return self._redis
        self._redis_checked = True
        if not settings.redis.enabled:
            if settings.exchange.live_trading:
                logger.error(
                    "[RateLimit] Redis is required for production (LIVE_TRADING=true) "
                    "rate limiting. Enable REDIS_ENABLED=true or rate limits will be per-process."
                )
            return None
        try:
            from core.cache import cache
            redis_obj = getattr(cache, "_redis", None)
            if redis_obj and getattr(redis_obj, "is_connected", lambda: False)():
                self._redis = redis_obj
                return self._redis
            if redis_obj:
                await redis_obj._get_client()
                if redis_obj.is_connected():
                    self._redis = redis_obj
                    return self._redis
        except (ConnectionError, TimeoutError):
            logger.warning("[RateLimit] Redis connection failed, using in-memory fallback")
        except Exception:
            logger.warning("[RateLimit] Redis connection failed, using in-memory fallback")
        return None

    async def _redis_check_rate(self, prefix: str, key: str, max_attempts: int, window_secs: int) -> bool:
        """Check rate limit using Redis sliding-window counters."""
        redis = await self._get_redis()
        if not redis:
            return self._check_rate_memory(prefix, key, max_attempts, window_secs)

        redis_key = f"rl:{prefix}:{key}"
        try:
            client = await redis._get_client()
            if not client:
                return self._check_rate_memory(prefix, key, max_attempts, window_secs)

            current = await client.get(redis_key)
            if current is None:
                await client.setex(redis_key, window_secs, "1")
                return True

            count = int(current)
            if count >= max_attempts:
                return False

            # Increment within the remaining TTL
            ttl = await client.ttl(redis_key)
            if ttl <= 0:
                await client.setex(redis_key, window_secs, "1")
            else:
                await client.setex(redis_key, ttl, str(count + 1))
            return True
        except (ConnectionError, TimeoutError, OSError):
            return self._check_rate_memory(prefix, key, max_attempts, window_secs)
        except Exception:
            return self._check_rate_memory(prefix, key, max_attempts, window_secs)

    async def _redis_clear_key(self, prefix: str, key: str) -> None:
        """Remove a rate-limit key from Redis."""
        redis = await self._get_redis()
        if not redis:
            return
        try:
            client = await redis._get_client()
            if client:
                await client.delete(f"rl:{prefix}:{key}")
        except (ConnectionError, TimeoutError):
            logger.warning("[RateLimit] Redis clear failed due to connection error")
        except Exception:
            logger.warning("[RateLimit] Redis clear failed due to unexpected error")

    def _check_rate_memory(self, prefix: str, key: str, max_attempts: int, window_secs: int) -> bool:
        """Fallback in-memory rate limit check."""
        store_map = {
            "login": self._login_attempts,
            "register": self._register_attempts,
            "webhook": self._webhook_attempts,
            "api": self._api_requests,
        }
        store = store_map.get(prefix, self._api_requests)

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

    async def dispatch(self, request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        if not self.enabled:
            return await call_next(request)

        ip = self._get_client_ip(request)
        path = request.url.path

        # Login rate limiting
        if path == "/api/auth/login" and request.method == "POST":
            if not await self._redis_check_rate(
                "login", ip,
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
            if not await self._redis_check_rate(
                "register", ip,
                settings.rate_limit.register_max_attempts,
                settings.rate_limit.register_window_secs,
            ):
                logger.warning(f"[RateLimit] Register rate limit hit for {ip}")
                return JSONResponse(
                    status_code=429,
                    content={"detail": "Too many registration attempts. Please wait 10 minutes."}
                )

        # Webhook rate limiting
        elif path == "/webhook" and request.method == "POST":
            if not await self._redis_check_rate(
                "webhook", ip,
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
            if not await self._redis_check_rate(
                "api", ip,
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
            await self._redis_clear_key("login", ip)
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
        """In-memory rate limit check (kept for backward-compatible signature)."""
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
        return str(client_ip(request))


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

    async def dispatch(self, request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
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

    async def dispatch(self, request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
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
                except ValueError:
                    logger.debug("[RequestSize] Invalid content-length header")
                return await call_next(request)

            body = await request.body()
            if len(body) > self.MAX_SIZE:
                return JSONResponse(
                    status_code=413,
                    content={"detail": "Request body too large"}
                )

            async def receive() -> dict[str, object]:
                return {"type": "http.request", "body": body, "more_body": False}

            request._receive = receive  # type: ignore[attr-defined]

        return await call_next(request)


# ─────────────────────────────────────────────
# Security Headers Middleware
# ─────────────────────────────────────────────

class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Add security headers to responses."""

    async def dispatch(self, request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
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
                "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net https://static.cloudflareinsights.com; "
                "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com https://fonts.bunny.net https://cdn.jsdelivr.net; "
                "font-src 'self' https://fonts.gstatic.com https://fonts.bunny.net https://cdn.jsdelivr.net; "
                "img-src 'self' data: https:; "
                "connect-src 'self' https: wss: https://cloudflareinsights.com; "
                "worker-src 'self';"
            )

        return response


# ─────────────────────────────────────────────
# Middleware Setup
# ─────────────────────────────────────────────

def setup_middleware(app) -> None:
    """Setup all middleware for the FastAPI app."""

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
