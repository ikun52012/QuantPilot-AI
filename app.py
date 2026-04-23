"""
QuantPilot AI v4.1 - Main Application

Complete pipeline:
  TradingView Webhook → Pre-Filter → AI Analysis → Trade Execution → Notification

Features:
  - User auth (JWT) with admin/user roles
  - Subscription system with crypto payments
  - Homepage, dashboard, login/register pages
  - Enhanced pre-filter (15 checks)
  - Multi-TP, trailing stop, custom AI
  - Async database with PostgreSQL/SQLite support
  - Redis caching
  - Prometheus metrics
  - Rate limiting

Usage:
  uvicorn app:app --host 0.0.0.0 --port 8000
"""
import os
import sys
import re
from pathlib import Path
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, FileResponse, HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from loguru import logger

from core.config import settings
from core.database import db_manager, seed_defaults
from core.cache import cache
from core.middleware import setup_middleware
from core.metrics import metrics_endpoint

# Import routers
from routers.webhook import router as webhook_router
from routers.auth import router as auth_router
from routers.admin import router as admin_router
from routers.user import router as user_router
from routers.subscription import router as subscription_router
from routers.ai_config import router as ai_config_router


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


# Console logging — use UTF-8 wrapper to handle emoji on Windows GBK terminals
import io as _io
_stdout_utf8 = _io.TextIOWrapper(
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
# Application Lifespan
# ─────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application startup and shutdown."""
    # Startup
    logger.info("=" * 50)
    logger.info(f"📡 {settings.app_name} v{settings.app_version} starting...")
    logger.info(f"   AI Provider: {settings.ai.provider}")
    logger.info(f"   Exchange: {settings.exchange.name}")
    logger.info(f"   Live Trading: {'🔴 YES' if settings.exchange.live_trading else '🟢 NO (Paper)'}")
    logger.info(f"   Exchange Sandbox: {'🧪 YES' if settings.exchange.sandbox_mode else 'NO'}")
    logger.info(f"   Database: {settings.database.url.split('@')[-1] if '@' in settings.database.url else settings.database.url}")
    logger.info("=" * 50)

    # Initialize database
    await db_manager.init()
    async with db_manager.async_session_factory() as session:
        await seed_defaults(session)
        from core.runtime_settings import apply_persisted_admin_settings
        await apply_persisted_admin_settings(session)
        await session.commit()

    # Initialize cache (async for Redis connection)
    await cache.init_async()

    # Start scheduler
    from apscheduler.schedulers.asyncio import AsyncIOScheduler
    from apscheduler.triggers.cron import CronTrigger

    scheduler = AsyncIOScheduler()

    async def _daily_reset_job():
        """Daily reset at midnight UTC."""
        from pre_filter import reset_daily_counters
        reset_daily_counters()
        logger.info("[Scheduler] Daily trade counters reset")

    async def _position_monitor_job():
        """Reconcile open positions and settle paper TP/SL."""
        from position_monitor import run_position_monitor_once
        result = await run_position_monitor_once()
        if result.get("closed") or result.get("partials") or result.get("errors"):
            logger.info(f"[Scheduler] Position monitor result: {result}")

    scheduler.add_job(
        _daily_reset_job,
        CronTrigger(hour=0, minute=0, second=0, timezone="UTC"),
    )
    scheduler.add_job(
        _position_monitor_job,
        "interval",
        seconds=max(10, int(settings.position_monitor_interval_secs)),
        max_instances=1,
        coalesce=True,
    )
    scheduler.start()
    logger.info("[Scheduler] APScheduler started")

    yield

    # Shutdown
    scheduler.shutdown(wait=True)
    await db_manager.close()
    logger.info("Signal Server shutting down...")


# ─────────────────────────────────────────────
# Create Application
# ─────────────────────────────────────────────

app = FastAPI(
    title=settings.app_name,
    description="AI-powered crypto trading signal processor with subscriptions",
    version=settings.app_version,
    lifespan=lifespan,
    docs_url="/docs" if settings.debug else None,
    redoc_url="/redoc" if settings.debug else None,
)


# ─────────────────────────────────────────────
# Setup Middleware
# ─────────────────────────────────────────────

setup_middleware(app)


# ─────────────────────────────────────────────
# Mount Static Files
# ─────────────────────────────────────────────

STATIC_DIR = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


# ─────────────────────────────────────────────
# Include Routers
# ─────────────────────────────────────────────

app.include_router(webhook_router)
app.include_router(auth_router)
app.include_router(admin_router)
app.include_router(user_router)
app.include_router(subscription_router)
app.include_router(ai_config_router)


# ─────────────────────────────────────────────
# Page Routes
# ─────────────────────────────────────────────

_NO_STORE_HEADERS = {
    "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
    "Pragma": "no-cache",
    "Expires": "0",
    "Vary": "Cookie",
}


def _apply_no_store_headers(response):
    for key, value in _NO_STORE_HEADERS.items():
        response.headers[key] = value
    return response


def _forced_login_requested(request: Request) -> bool:
    return any(key in request.query_params for key in ("expired", "logout", "force"))


def _login_response(request: Request):
    from core.auth import clear_auth_cookie

    response = FileResponse(STATIC_DIR / "login.html")
    clear_auth_cookie(response, request)
    return _apply_no_store_headers(response)


def _redirect_no_store(url: str):
    return _apply_no_store_headers(RedirectResponse(url=url, status_code=303))


@app.get("/", response_class=HTMLResponse)
async def homepage(request: Request):
    """Serve homepage."""
    from core.auth import get_optional_user

    async with db_manager.async_session_factory() as db:
        user = await get_optional_user(request, db)
        if user:
            return _redirect_no_store("/dashboard")

    return _apply_no_store_headers(FileResponse(STATIC_DIR / "home.html"))


@app.get("/dashboard")
async def dashboard(request: Request):
    """Serve dashboard page."""
    from core.auth import get_optional_user

    async with db_manager.async_session_factory() as db:
        user = await get_optional_user(request, db)
        if not user:
            return _redirect_no_store("/login?expired=1")

    return _apply_no_store_headers(FileResponse(STATIC_DIR / "index.html"))


@app.get("/login")
async def login_page(request: Request):
    """Serve login page."""
    from core.auth import get_optional_user

    if _forced_login_requested(request):
        return _login_response(request)

    async with db_manager.async_session_factory() as db:
        user = await get_optional_user(request, db)
        if user:
            return _redirect_no_store("/dashboard")

    return _login_response(request)


@app.get("/register")
async def register_page(request: Request):
    """Serve register page."""
    from core.auth import get_optional_user

    async with db_manager.async_session_factory() as db:
        user = await get_optional_user(request, db)
        if user:
            return _redirect_no_store("/dashboard")

    return _apply_no_store_headers(FileResponse(STATIC_DIR / "register.html"))


# ─────────────────────────────────────────────
# Health & Metrics
# ─────────────────────────────────────────────

@app.get("/health")
async def health_check():
    """Health check endpoint."""
    from core.database import db_manager
    from sqlalchemy import text

    checks = {
        "status": "healthy",
        "version": settings.app_version,
        "database": "ok" if db_manager.engine else "not initialized",
        "cache": "ok" if cache._initialized else "not initialized",
    }

    try:
        async with db_manager.async_session_factory() as session:
            await session.execute(text("SELECT 1"))
        checks["database"] = "ok"
    except Exception as e:
        checks["database"] = f"error: {str(e)[:50]}"
        checks["status"] = "unhealthy"

    status_code = 200 if checks["status"] == "healthy" else 503
    return JSONResponse(content=checks, status_code=status_code)


@app.get("/metrics")
async def metrics():
    """Prometheus metrics endpoint."""
    return await metrics_endpoint()


# ─────────────────────────────────────────────
# Legacy Stats Endpoint
# ─────────────────────────────────────────────

@app.get("/stats")
async def get_stats():
    """Get today's trading statistics."""
    from sqlalchemy import select
    from core.database import TradeModel, WebhookEventModel
    from core.utils.datetime import utcnow

    today_start = utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    async with db_manager.async_session_factory() as session:
        events = await session.execute(
            select(WebhookEventModel).where(WebhookEventModel.created_at >= today_start)
        )
        trades = await session.execute(
            select(TradeModel).where(TradeModel.timestamp >= today_start)
        )
        event_rows = list(events.scalars().all())
        trade_rows = list(trades.scalars().all())

    executed = [trade for trade in trade_rows if trade.execute]
    rejected = [event for event in event_rows if event.status in {"blocked", "rejected", "error"}]
    return {
        "total_signals": len(event_rows),
        "executed": len(executed),
        "rejected": len(rejected),
        "tickers": sorted({trade.ticker for trade in executed if trade.ticker}),
    }


# ─────────────────────────────────────────────
# Error Handlers
# ─────────────────────────────────────────────

@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    """Global exception handler."""
    logger.error(f"Unhandled exception: {exc}", exc_info=True)
    return JSONResponse(
        status_code=500,
        content={"detail": "Internal server error"},
    )


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
