"""
QuantPilot AI - Application Lifespan Management
Handles startup and shutdown logic separately from app factory.
"""
from contextlib import asynccontextmanager

from fastapi import FastAPI
from loguru import logger

from core.config import settings
from core.database import db_manager, seed_defaults
from core.cache import cache

# Module-level scheduler reference for shutdown cleanup
_scheduler = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application startup and shutdown lifecycle."""
    await _on_startup()
    yield
    await _on_shutdown()


async def _on_startup():
    """Initialize all services on application startup."""
    logger.info("=" * 50)
    logger.info(f"QuantPilot AI v{settings.app_version} starting...")
    logger.info(f"   AI Provider: {settings.ai.provider}")
    logger.info(f"   Exchange: {settings.exchange.name}")
    logger.info(f"   Live Trading: {'YES' if settings.exchange.live_trading else 'NO (Paper)'}")
    logger.info(f"   Exchange Sandbox: {'YES' if settings.exchange.sandbox_mode else 'NO'}")
    logger.info(f"   Database: {settings.database.url.split('@')[-1] if '@' in settings.database.url else settings.database.url}")
    logger.info("=" * 50)

    await _init_database()
    await _init_cache()
    await _init_scheduler()
    await _restore_strategies()


async def _init_database():
    """Initialize database and seed defaults."""
    await db_manager.init()
    async with db_manager.async_session_factory() as session:
        await seed_defaults(session)
        from core.runtime_settings import apply_persisted_admin_settings
        await apply_persisted_admin_settings(session)
        await session.commit()
    logger.info("[Database] Initialized and seeded")


async def _init_cache():
    """Initialize cache layer (Redis or in-memory)."""
    await cache.init_async()
    logger.info("[Cache] Initialized")


async def _init_scheduler():
    """Initialize APScheduler with periodic jobs."""
    from apscheduler.schedulers.asyncio import AsyncIOScheduler
    from apscheduler.triggers.cron import CronTrigger

    scheduler = AsyncIOScheduler()

    async def _daily_reset_job():
        from pre_filter import _state_lock, reset_daily_counters
        with _state_lock:
            reset_daily_counters()
        logger.info("[Scheduler] Daily trade counters reset")

    async def _position_monitor_job():
        from position_monitor import run_position_monitor_once
        result = await run_position_monitor_once()
        if result.get("closed") or result.get("partials") or result.get("errors"):
            logger.info(f"[Scheduler] Position monitor: {result.get('closed', 0)} closed, {result.get('partials', 0)} partials")

    scheduler.add_job(
        _daily_reset_job,
        CronTrigger(hour=0, minute=0, second=0, timezone="UTC"),
        id="daily_reset",
        name="Daily trade counter reset",
    )
    scheduler.add_job(
        _position_monitor_job,
        "interval",
        seconds=max(10, int(settings.position_monitor_interval_secs)),
        max_instances=1,
        coalesce=True,
        id="position_monitor",
        name="Position monitor",
    )
    scheduler.start()
    logger.info(f"[Scheduler] Started (position monitor: {settings.position_monitor_interval_secs}s)")

    global _scheduler
    _scheduler = scheduler


async def _restore_strategies():
    """Restore active DCA/Grid strategies from database."""
    try:
        from strategies.dca import DCAEngine
        from strategies.grid import GridEngine
        from core.database import StrategyStateModel
        from sqlalchemy import select

        async with db_manager.async_session_factory() as session:
            result = await session.execute(
                select(StrategyStateModel).where(
                    StrategyStateModel.status == "active",
                    StrategyStateModel.strategy_type.in_(["dca", "grid"]),
                )
            )
            rows = list(result.scalars().all())

        restored_dca = 0
        restored_grid = 0
        for row in rows:
            if row.strategy_type == "dca":
                from routers.strategies import _restore_dca
                _restore_dca(row)
                restored_dca += 1
            elif row.strategy_type == "grid":
                from routers.strategies import _restore_grid
                _restore_grid(row)
                restored_grid += 1

        logger.info(f"[Startup] Restored {restored_dca} DCA and {restored_grid} Grid strategies")
    except Exception as e:
        logger.warning(f"[Startup] Failed to restore strategies: {e}")


async def _on_shutdown():
    """Cleanup all services on application shutdown."""
    global _scheduler
    if _scheduler:
        _scheduler.shutdown(wait=True)
        _scheduler = None
        logger.info("[Scheduler] Shut down")

    await db_manager.close()
    logger.info("QuantPilot AI shut down complete")
