"""
QuantPilot AI - Application Lifespan Management
Handles startup and shutdown logic separately from app factory.
"""
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from loguru import logger
from sqlalchemy.exc import SQLAlchemyError

from core.cache import cache
from core.config import settings
from core.database import db_manager, seed_defaults

# Module-level scheduler reference for shutdown cleanup
_scheduler = None
_scheduler_lock_fd: int | None = None
_scheduler_lock_path: Path | None = None


def _pid_is_running(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False


def _acquire_scheduler_lock() -> bool:
    """Elect one local process to run APScheduler jobs."""
    global _scheduler_lock_fd, _scheduler_lock_path
    if _scheduler_lock_fd is not None:
        return True

    lock_dir = Path(__file__).resolve().parent.parent / "data"
    lock_dir.mkdir(parents=True, exist_ok=True)
    lock_path = lock_dir / "scheduler.lock"

    for attempt in range(2):
        try:
            fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode("ascii"))
            _scheduler_lock_fd = fd
            _scheduler_lock_path = lock_path
            return True
        except FileExistsError:
            try:
                raw_pid = lock_path.read_text(encoding="ascii").strip()
                existing_pid = int(raw_pid or "0")
            except (OSError, ValueError):
                existing_pid = 0
            if attempt == 0 and not _pid_is_running(existing_pid):
                try:
                    lock_path.unlink()
                    continue
                except OSError:
                    pass
            logger.warning(f"[Scheduler] Another process owns the scheduler lock (pid={existing_pid}); skipping jobs")
            return False
        except OSError as exc:
            logger.warning(f"[Scheduler] Could not acquire scheduler lock: {exc}; skipping jobs")
            return False
    return False


def _release_scheduler_lock() -> None:
    global _scheduler_lock_fd, _scheduler_lock_path
    if _scheduler_lock_fd is not None:
        try:
            os.close(_scheduler_lock_fd)
        except OSError:
            pass
        _scheduler_lock_fd = None
    if _scheduler_lock_path is not None:
        try:
            _scheduler_lock_path.unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            logger.debug(f"[Scheduler] Could not remove scheduler lock: {exc}")
        _scheduler_lock_path = None


async def _market_scanner_job():
    try:
        from services.market_scanner import run_scanner_once
        result = await run_scanner_once()
        if result.get("status") not in {"disabled", "skipped"}:
            logger.info(
                f"[Scheduler] Market scanner run: status={result.get('status')} "
                f"scanned={result.get('scanned', 0)} candidates={result.get('candidates', 0)}"
            )
    except Exception as e:
        logger.error(f"[Scheduler] Market scanner failed: {e}")


async def _scanner_rejection_summary_job():
    try:
        from core.database import get_scanner_rejection_summary
        from notifier import notify_scanner_rejection_summary

        async with db_manager.async_session_factory() as session:
            summary = await get_scanner_rejection_summary(session, scope="admin")
        await notify_scanner_rejection_summary(summary)
        if int(summary.get("rejected_or_held") or 0) > 0:
            logger.info(
                f"[Scheduler] Scanner rejection summary sent: "
                f"{summary.get('rejected_or_held')}/{summary.get('total_results')} rejected or held"
            )
    except Exception as e:
        logger.error(f"[Scheduler] Scanner rejection summary failed: {e}")


def sync_scanner_scheduler() -> dict:
    """Add, update, or remove the scanner interval job after runtime setting changes."""
    scheduler = _scheduler
    if scheduler is None:
        return {"status": "unavailable", "reason": "scheduler is not initialized"}

    job = scheduler.get_job("market_scanner")
    if not settings.scanner.enabled:
        if job:
            scheduler.remove_job("market_scanner")
            logger.info("[Scheduler] Market scanner disabled at runtime")
            return {"status": "removed", "enabled": False}
        return {"status": "disabled", "enabled": False}

    interval = max(60, int(settings.scanner.interval_secs))
    if job:
        job.reschedule(trigger="interval", seconds=interval)
        job.modify(max_instances=1, coalesce=True)
        logger.info(f"[Scheduler] Market scanner rescheduled: {interval}s/{settings.scanner.mode}")
        return {"status": "rescheduled", "enabled": True, "interval_secs": interval}

    scheduler.add_job(
        _market_scanner_job,
        "interval",
        seconds=interval,
        max_instances=1,
        coalesce=True,
        id="market_scanner",
        name="Automatic market scanner",
        replace_existing=True,
    )
    logger.info(f"[Scheduler] Market scanner enabled: {interval}s/{settings.scanner.mode}")
    return {"status": "added", "enabled": True, "interval_secs": interval}


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
    logger.info(f"   Database: {settings.database.url.split('@')[-1] if '@' in settings.database.url else settings.database.url}")
    logger.info("=" * 50)

    await _init_database()

    logger.info(f"   AI Provider: {settings.ai.provider}")
    logger.info(f"   Exchange: {settings.exchange.name}")
    logger.info(f"   Live Trading: {'YES' if settings.exchange.live_trading else 'NO (Paper)'}")
    logger.info(f"   Exchange Sandbox: {'YES' if settings.exchange.sandbox_mode else 'NO'}")

    await _init_cache()
    await _init_scheduler()
    await _restore_strategies()


async def _init_database():
    """Initialize database and seed defaults."""
    await db_manager.init()
    async with db_manager.async_session_factory() as session:
        try:
            await seed_defaults(session)
            from core.runtime_settings import apply_persisted_admin_settings
            await apply_persisted_admin_settings(session)
            await session.commit()
        except SQLAlchemyError:
            # BUG FIX: Rollback on failure so partial state changes are not committed.
            await session.rollback()
            raise
        except Exception:
            # BUG FIX: Rollback on failure so partial state changes are not committed.
            await session.rollback()
            raise
    logger.info("[Database] Initialized and seeded")


async def _init_cache():
    """Initialize cache layer (Redis or in-memory)."""
    await cache.init_async()
    logger.info("[Cache] Initialized")


async def _init_scheduler():
    """Initialize APScheduler with periodic jobs."""
    if not _acquire_scheduler_lock():
        return

    from apscheduler.schedulers.asyncio import AsyncIOScheduler
    from apscheduler.triggers.cron import CronTrigger

    scheduler = AsyncIOScheduler()

    async def _daily_reset_job():
        from pre_filter import _state_lock, reset_daily_counters
        with _state_lock:
            reset_daily_counters()
        logger.info("[Scheduler] Daily trade counters reset")

    async def _daily_backup_job():
        try:
            from backups import create_backup
            result = await create_backup(note="scheduled-daily")
            logger.info(f"[Scheduler] Daily backup created: {result.get('name', 'unknown')}")
        except Exception as e:
            logger.error(f"[Scheduler] Daily backup failed: {e}")

    async def _cleanup_old_records_job():
        try:
            from core.database import cleanup_old_records, db_manager
            async with db_manager.async_session_factory() as session:
                deleted = await cleanup_old_records(session)
                await session.commit()
            if deleted:
                logger.info(f"[Scheduler] Database cleanup: deleted {sum(deleted.values())} old records")
        except Exception as e:
            logger.error(f"[Scheduler] Database cleanup failed: {e}")

    async def _cleanup_scanner_audits_job():
        try:
            from datetime import timedelta

            from sqlalchemy import delete

            from core.database import ScannerAuditModel, db_manager
            from core.utils.datetime import utcnow

            cutoff = utcnow() - timedelta(days=30)
            async with db_manager.async_session_factory() as session:
                result = await session.execute(
                    delete(ScannerAuditModel).where(ScannerAuditModel.created_at < cutoff)
                )
                await session.commit()
                deleted = result.rowcount
                if deleted:
                    logger.info(f"[Scheduler] Scanner audit cleanup: deleted {deleted} old records")
        except Exception as e:
            logger.error(f"[Scheduler] Scanner audit cleanup failed: {e}")

    async def _cleanup_old_backups_job():
        try:
            from backups import cleanup_old_backups
            result = await cleanup_old_backups(max_backups=7)
            if result.get("deleted"):
                logger.info(f"[Scheduler] Backup cleanup: deleted {result['deleted']} old backups, kept {result['kept']}")
        except Exception as e:
            logger.error(f"[Scheduler] Backup cleanup failed: {e}")

    async def _position_monitor_job():
        from position_monitor import run_position_monitor_once
        result = await run_position_monitor_once()
        if result.get("closed") or result.get("partials") or result.get("errors"):
            logger.info(f"[Scheduler] Position monitor: {result.get('closed', 0)} closed, {result.get('partials', 0)} partials")

    async def _exchange_pool_cleanup_job():
        try:
            from exchange import cleanup_idle_exchange_pool
            cleaned = cleanup_idle_exchange_pool()
            if cleaned:
                logger.info(f"[Scheduler] Exchange pool cleanup: removed {cleaned} idle connections")
        except Exception as e:
            logger.debug(f"[Scheduler] Exchange pool cleanup failed: {e}")

    scheduler.add_job(
        _daily_reset_job,
        CronTrigger(hour=0, minute=0, second=0, timezone="UTC"),
        id="daily_reset",
        name="Daily trade counter reset",
    )
    scheduler.add_job(
        _daily_backup_job,
        CronTrigger(hour=2, minute=0, second=0, timezone="UTC"),
        id="daily_backup",
        name="Daily database backup",
    )
    scheduler.add_job(
        _cleanup_old_records_job,
        CronTrigger(hour=3, minute=0, second=0, timezone="UTC"),
        id="cleanup_old_records",
        name="Daily database cleanup",
    )
    scheduler.add_job(
        _cleanup_old_backups_job,
        CronTrigger(hour=3, minute=30, second=0, timezone="UTC"),
        id="cleanup_old_backups",
        name="Daily backup cleanup",
    )
    scheduler.add_job(
        _cleanup_scanner_audits_job,
        CronTrigger(hour=4, minute=0, second=0, timezone="UTC"),
        id="cleanup_scanner_audits",
        name="Daily scanner audit cleanup",
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
    # P2-11: Periodic exchange pool cleanup
    scheduler.add_job(
        _exchange_pool_cleanup_job,
        "interval",
        seconds=1800,
        max_instances=1,
        coalesce=True,
        id="exchange_pool_cleanup",
        name="Exchange pool cleanup",
    )
    scheduler.add_job(
        _scanner_rejection_summary_job,
        CronTrigger(hour=23, minute=55, second=0, timezone="UTC"),
        id="scanner_rejection_summary",
        name="Scanner AI rejection daily summary",
        replace_existing=True,
    )

    global _scheduler
    _scheduler = scheduler
    scanner_sync = sync_scanner_scheduler()
    scheduler.start()
    scanner_msg = (
        f", scanner: {settings.scanner.interval_secs}s/{settings.scanner.mode}"
        if settings.scanner.enabled else ", scanner: disabled"
    )
    logger.info(
        f"[Scheduler] Started (position monitor: {settings.position_monitor_interval_secs}s{scanner_msg}; "
        f"scanner_job={scanner_sync.get('status')})"
    )


async def _restore_strategies():
    """Restore active DCA/Grid strategies from database."""
    try:
        from sqlalchemy import select

        from core.database import StrategyStateModel

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
    try:
        from services.market_scanner import shutdown_market_scanner_service
        await shutdown_market_scanner_service()
    except Exception as e:
        logger.debug(f"[Scanner] Shutdown cleanup failed: {e}")

    if _scheduler:
        _scheduler.shutdown(wait=True)
        _scheduler = None
        logger.info("[Scheduler] Shut down")
    _release_scheduler_lock()

    try:
        from exchange import cleanup_idle_exchange_pool
        cleanup_idle_exchange_pool(max_idle_secs=0)
        logger.info("[Exchange] All connections closed")
    except Exception as e:
        logger.debug(f"[Exchange] Pool cleanup on shutdown: {e}")

    await db_manager.close()
    logger.info("QuantPilot AI shut down complete")
