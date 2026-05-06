"""
QuantPilot AI - Health Check Router
Provides system health monitoring and diagnostics API.
"""
import asyncio
import os
import time

from fastapi import APIRouter, HTTPException, Request
from loguru import logger
from pydantic import BaseModel

from core.config import settings
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
        return HealthCheckResult(
            status="healthy",
            latency_ms=latency,
        )
    except Exception as e:
        logger.warning(f"[Health] Database check failed: {e}")
        return HealthCheckResult(
            status="unhealthy",
            error=str(e),
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
                latency_ms=latency,
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
            error=str(e),
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
            sandbox_mode=settings.exchange.sandbox_mode,
        )
        await asyncio.to_thread(exchange.load_markets)

        latency = (time.time() - start) * 1000
        markets_count = len(exchange.markets)

        return HealthCheckResult(
            status="healthy",
            latency_ms=latency,
            details={"markets_loaded": markets_count},
        )
    except Exception as e:
        logger.warning(f"[Health] Exchange API check failed: {e}")
        return HealthCheckResult(
            status="unhealthy",
            error=str(e),
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
        return HealthCheckResult(
            status="healthy" if memory.percent < 90 else "degraded",
            details={
                "total_gb": round(memory.total / (1024**3), 2),
                "available_gb": round(memory.available / (1024**3), 2),
                "used_percent": memory.percent,
            },
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
    if _HEALTH_TOKEN:
        token = request.headers.get("X-Health-Token") or request.query_params.get("token")
        if token != _HEALTH_TOKEN:
            raise HTTPException(401, "Health check token required")
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
        version="1.0.0",
        uptime_seconds=time.time() - _START_TIME,
    )


@router.get("/quick")
async def quick_health_check():
    """
    Quick health check - only checks critical components.
    Faster response for load balancer health checks.
    """
    try:
        await check_database()
        return {"status": "healthy", "timestamp": utcnow().isoformat()}
    except Exception as e:
        return {"status": "unhealthy", "error": str(e), "timestamp": utcnow().isoformat()}


@router.get("/live")
async def liveness_check():
    """Kubernetes liveness probe - application is running."""
    return {"status": "alive", "timestamp": utcnow().isoformat()}


@router.get("/ready")
async def readiness_check():
    """Kubernetes readiness probe - application is ready to serve requests."""
    try:
        db_check = await check_database()
        if db_check.status != "healthy":
            return {"status": "not_ready", "reason": "Database unavailable"}
        return {"status": "ready", "timestamp": utcnow().isoformat()}
    except Exception as e:
        return {"status": "not_ready", "error": str(e)}
