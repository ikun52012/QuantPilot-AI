"""
QuantPilot AI - Health Check Router
Provides system health monitoring and diagnostics API.
"""
import asyncio
import os
import secrets
import time

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from loguru import logger
from pydantic import BaseModel

from core.config import settings
from core.request_utils import client_ip
from core.utils.datetime import utcnow

router = APIRouter(prefix="/health", tags=["Health"])

_HEALTH_TOKEN = os.getenv("HEALTH_CHECK_TOKEN", "")


class HealthCheckResult(BaseModel):
    status: str
    latency_ms: float | None = None
    error: str | None = None
    details: dict | None = None


class HealthCheckResponse(BaseModel):
    status: str
    timestamp: str
    checks: dict[str, HealthCheckResult]
    version: str
    uptime_seconds: float


_START_TIME = time.time()


def _health_token_ok(request: Request) -> bool:
    token = request.headers.get("X-Health-Token") or request.query_params.get("token") or ""
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        token = auth.split(" ", 1)[1].strip()
    return bool(_HEALTH_TOKEN and token and secrets.compare_digest(token, _HEALTH_TOKEN))


def _require_detailed_health_access(request: Request) -> None:
    if _HEALTH_TOKEN:
        if not _health_token_ok(request):
            raise HTTPException(401, "Health check token required")
        return
    if settings.is_production:
        raise HTTPException(404, "Not found")


async def check_database() -> HealthCheckResult:
    """Check database connection."""
    try:
        from sqlalchemy import text

        from core.database import db_manager

        start = time.time()
        async with db_manager.async_session_factory() as session:
            result = await session.execute(text("SELECT 1"))
            result.scalar()

        latency = (time.time() - start) * 1000
        # P2-FIX: Round latency to prevent precise fingerprinting
        return HealthCheckResult(
            status="healthy",
            latency_ms=round(latency, 1),
        )
    except Exception as e:
        logger.warning(f"[Health] Database check failed: {e}")
        return HealthCheckResult(
            status="unhealthy",
            error="Database unavailable",
        )


async def check_redis() -> HealthCheckResult:
    """Check Redis connection."""
    try:
        from core.cache import cache

        redis_obj = getattr(cache, "_redis", None)
        if not redis_obj:
            return HealthCheckResult(
                status="degraded",
                error="Redis not configured, using in-memory cache",
            )

        if not redis_obj.is_connected():
            return HealthCheckResult(
                status="degraded",
                error="Redis not connected",
            )

        start = time.time()
        client = await redis_obj._get_client()
        if client:
            await client.ping()
            latency = (time.time() - start) * 1000
            return HealthCheckResult(
                status="healthy",
                latency_ms=round(latency, 1),
            )
        else:
            return HealthCheckResult(
                status="degraded",
                error="Redis client unavailable",
            )
    except Exception as e:
        logger.warning(f"[Health] Redis check failed: {e}")
        return HealthCheckResult(
            status="degraded",
            error="Redis unavailable",
        )


async def check_exchange_api() -> HealthCheckResult:
    """Check exchange API connectivity."""
    try:
        from exchange import _get_or_create_exchange

        start = time.time()
        exchange = _get_or_create_exchange(
            settings.exchange.name,
            settings.exchange.api_key,
            settings.exchange.api_secret,
            sandbox=settings.exchange.sandbox_mode,
        )
        await asyncio.wait_for(
            asyncio.to_thread(exchange.load_markets),
            timeout=15.0,
        )

        latency = (time.time() - start) * 1000
        markets_count = len(exchange.markets)

        # P2-FIX: Sanitize markets_count to prevent system fingerprinting
        # Only return approximate range instead of exact count
        if markets_count > 1000:
            markets_range = "1000+"
        elif markets_count > 500:
            markets_range = "500-1000"
        elif markets_count > 100:
            markets_range = "100-500"
        else:
            markets_range = f"<100 ({markets_count})"

        return HealthCheckResult(
            status="healthy",
            latency_ms=round(latency, 1),
            details={"markets_loaded": markets_range},
        )
    except Exception as e:
        logger.warning(f"[Health] Exchange API check failed: {e}")
        return HealthCheckResult(
            status="unhealthy",
            error="Exchange API unavailable",
        )


async def check_ai_api() -> HealthCheckResult:
    """Check AI API connectivity."""
    try:

        provider = settings.ai.provider.lower()

        if provider == "deepseek":
            if not settings.ai.deepseek_api_key:
                return HealthCheckResult(
                    status="degraded",
                    error="DeepSeek API key not configured",
                )
        elif provider == "openai":
            if not settings.ai.openai_api_key:
                return HealthCheckResult(
                    status="degraded",
                    error="OpenAI API key not configured",
                )
        elif provider == "anthropic":
            if not settings.ai.anthropic_api_key:
                return HealthCheckResult(
                    status="degraded",
                    error="Anthropic API key not configured",
                )
        elif provider == "mistral":
            if not settings.ai.mistral_api_key:
                return HealthCheckResult(
                    status="degraded",
                    error="Mistral API key not configured",
                )
        elif provider == "openrouter":
            if not settings.ai.openrouter_api_key:
                return HealthCheckResult(
                    status="degraded",
                    error="OpenRouter API key not configured",
                )
        elif provider == "custom":
            if not settings.ai.custom_provider_api_url or not settings.ai.custom_provider_api_key:
                return HealthCheckResult(
                    status="degraded",
                    error="Custom AI provider URL or API key not configured",
                )
        else:
            return HealthCheckResult(
                status="unhealthy",
                error="Configured AI provider is unsupported",
            )

        return HealthCheckResult(
            status="healthy",
            details={"provider": provider},
        )
    except Exception as e:
        logger.warning(f"[Health] AI API check failed: {e}")
        return HealthCheckResult(
            status="unhealthy",
            error=str(e),
        )


async def check_position_monitor() -> HealthCheckResult:
    """Check position monitor status."""
    try:
        from position_monitor import get_monitor_state

        state = await get_monitor_state()
        return HealthCheckResult(
            status="healthy",
            details=state,
        )
    except Exception as e:
        logger.warning(f"[Health] Position monitor check failed: {e}")
        return HealthCheckResult(
            status="degraded",
            error=str(e),
        )


async def check_websocket_connections() -> HealthCheckResult:
    """Check WebSocket connections count."""
    try:
        from routers.websocket import manager

        active_count = len(manager.active_connections)
        return HealthCheckResult(
            status="healthy",
            details={"active_connections": active_count},
        )
    except Exception as e:
        logger.warning(f"[Health] WebSocket check failed: {e}")
        return HealthCheckResult(
            status="degraded",
            error=str(e),
        )


async def check_memory() -> HealthCheckResult:
    """Check system memory usage."""
    try:
        import psutil

        memory = psutil.virtual_memory()
        # P2-FIX: Sanitize memory details to prevent system fingerprinting
        # Only return usage percentage range, not exact values
        if memory.percent < 50:
            usage_range = "low (<50%)"
        elif memory.percent < 75:
            usage_range = "moderate (50-75%)"
        elif memory.percent < 90:
            usage_range = "high (75-90%)"
        else:
            usage_range = "critical (>90%)"

        return HealthCheckResult(
            status="healthy" if memory.percent < 90 else "degraded",
            details={"usage_range": usage_range},
        )
    except ImportError:
        return HealthCheckResult(
            status="degraded",
            error="psutil not installed",
        )
    except Exception as e:
        return HealthCheckResult(
            status="unhealthy",
            error=str(e),
        )


@router.get("/", response_model=HealthCheckResponse)
async def health_check(request: Request):
    """
    Full health check of all system components.
    Returns status: healthy, degraded, or unhealthy.

    Optional: Set HEALTH_CHECK_TOKEN env var to require a token for access.
    """
    _require_detailed_health_access(request)
    checks = await asyncio.gather(
        check_database(),
        check_redis(),
        check_exchange_api(),
        check_ai_api(),
        check_position_monitor(),
        check_websocket_connections(),
        check_memory(),
        return_exceptions=True,
    )

    check_results = {
        "database": checks[0] if not isinstance(checks[0], Exception) else HealthCheckResult(status="unhealthy", error=str(checks[0])),
        "redis": checks[1] if not isinstance(checks[1], Exception) else HealthCheckResult(status="unhealthy", error=str(checks[1])),
        "exchange_api": checks[2] if not isinstance(checks[2], Exception) else HealthCheckResult(status="unhealthy", error=str(checks[2])),
        "ai_api": checks[3] if not isinstance(checks[3], Exception) else HealthCheckResult(status="unhealthy", error=str(checks[3])),
        "position_monitor": checks[4] if not isinstance(checks[4], Exception) else HealthCheckResult(status="unhealthy", error=str(checks[4])),
        "websocket": checks[5] if not isinstance(checks[5], Exception) else HealthCheckResult(status="unhealthy", error=str(checks[5])),
        "memory": checks[6] if not isinstance(checks[6], Exception) else HealthCheckResult(status="unhealthy", error=str(checks[6])),
    }

    # Determine overall status
    statuses = [c.status for c in check_results.values()]
    if "unhealthy" in statuses:
        overall_status = "unhealthy"
    elif "degraded" in statuses:
        overall_status = "degraded"
    else:
        overall_status = "healthy"

    return HealthCheckResponse(
        status=overall_status,
        timestamp=utcnow().isoformat(),
        checks=check_results,
        version=settings.app_version,
        uptime_seconds=time.time() - _START_TIME,
    )


@router.get("/quick")
async def quick_health_check(request: Request):
    """
    Quick health check - only checks critical components.
    Rate-limited to prevent abuse from unauthenticated access.
    """
    # Rate limit unauthenticated health checks
    from core.request_utils import client_ip

    ip = client_ip(request)
    if not await _check_health_rate_limit(ip):
        return JSONResponse(status_code=429, content={"status": "rate_limited"})

    try:
        db_check = await check_database()
        if db_check.status != "healthy":
            return JSONResponse(
                status_code=503,
                content={"status": "unhealthy", "timestamp": utcnow().isoformat()},
            )
        return {"status": "healthy", "timestamp": utcnow().isoformat()}
    except Exception:
        return JSONResponse(
            status_code=503,
            content={"status": "unhealthy", "timestamp": utcnow().isoformat()},
        )


@router.get("/live")
async def liveness_check(request: Request):
    """Kubernetes liveness probe - application is running."""
    ip = client_ip(request)
    if not await _check_health_rate_limit(ip):
        return JSONResponse(status_code=429, content={"status": "rate_limited"})
    return {"status": "alive", "timestamp": utcnow().isoformat()}


@router.get("/ready")
async def readiness_check(request: Request):
    """Trading readiness probe, including market-data and AI dependencies."""
    ip = client_ip(request)
    if not await _check_health_rate_limit(ip):
        return JSONResponse(status_code=429, content={"status": "rate_limited"})
    try:
        db_check = await check_database()
        if db_check.status != "healthy":
            return JSONResponse(status_code=503, content={"status": "not_ready", "reason": "Database unavailable"})

        if settings.exchange.live_trading:
            redis_check = await check_redis()
            if redis_check.status != "healthy":
                return JSONResponse(
                    status_code=503,
                    content={"status": "not_ready", "reason": "Redis coordination unavailable"},
                )

        from core import lifespan as lifespan_module

        scheduler = getattr(lifespan_module, "_scheduler", None)
        if scheduler is None or not bool(getattr(scheduler, "running", False)):
            return JSONResponse(
                status_code=503,
                content={"status": "not_ready", "reason": "Trading scheduler unavailable"},
            )

        exchange_check, ai_check = await asyncio.gather(
            check_exchange_api(),
            check_ai_api(),
        )
        if exchange_check.status != "healthy":
            return JSONResponse(
                status_code=503,
                content={
                    "status": "not_ready",
                    "reason": "Exchange market data unavailable",
                    "trading_ready": False,
                    "analysis_ready": ai_check.status == "healthy",
                },
            )
        if ai_check.status != "healthy":
            return JSONResponse(
                status_code=503,
                content={
                    "status": "not_ready",
                    "reason": "AI analysis provider unavailable",
                    "trading_ready": False,
                    "analysis_ready": False,
                },
            )
        return {
            "status": "ready",
            "trading_ready": True,
            "analysis_ready": True,
            "timestamp": utcnow().isoformat(),
        }
    except Exception:
        return JSONResponse(status_code=503, content={"status": "not_ready", "reason": "Readiness check failed"})


# Rate limiting for health endpoints
_HEALTH_RATE_LIMIT: dict[str, float] = {}
_HEALTH_RATE_WINDOW = 10.0  # seconds
_HEALTH_RATE_MAX = 30  # max requests per window
_HEALTH_RATE_MAX_ENTRIES = 5000  # Hard cap to prevent memory exhaustion
_HEALTH_RATE_LOCK = None  # asyncio.Lock, lazily initialized


def _get_health_rate_lock() -> asyncio.Lock:
    """Lazily initialize the health rate limit lock."""
    global _HEALTH_RATE_LOCK
    if _HEALTH_RATE_LOCK is None:
        _HEALTH_RATE_LOCK = asyncio.Lock()
    return _HEALTH_RATE_LOCK


async def _check_health_rate_limit(ip: str) -> bool:
    """Check rate limit for health check endpoints.

    P2-FIX: Added asyncio.Lock to prevent race conditions in concurrent access.
    """
    import time
    now = time.time()

    async with _get_health_rate_lock():
        last = _HEALTH_RATE_LIMIT.get(ip, 0)
        if now - last < _HEALTH_RATE_WINDOW:
            return False
        _HEALTH_RATE_LIMIT[ip] = now
        # BUG FIX: Hard cap with LRU eviction
        if len(_HEALTH_RATE_LIMIT) > _HEALTH_RATE_MAX_ENTRIES:
            sorted_entries = sorted(_HEALTH_RATE_LIMIT.items(), key=lambda x: x[1])
            to_remove = len(_HEALTH_RATE_LIMIT) - _HEALTH_RATE_MAX_ENTRIES
            for k, _ in sorted_entries[:to_remove]:
                _HEALTH_RATE_LIMIT.pop(k, None)
        # Also cleanup expired entries
        cutoff = now - _HEALTH_RATE_WINDOW
        expired = [k for k, v in _HEALTH_RATE_LIMIT.items() if v < cutoff]
        for k in expired:
            _HEALTH_RATE_LIMIT.pop(k, None)
        return True
