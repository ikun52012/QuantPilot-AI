"""Admin APIs for the automatic market scanner."""
from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from core.auth import require_admin
from core.config import settings
from core.database import get_db, get_or_create_scanner_state, get_scanner_rejection_summary, list_scanner_audits
from core.runtime_settings import save_scanner_settings
from services.market_scanner import get_market_scanner_service, run_scanner_once

router = APIRouter(prefix="/api/scanner", tags=["scanner"])


class ScannerSettingsUpdate(BaseModel):
    enabled: bool | None = None
    mode: str | None = Field(default=None, description="observe, paper, or live")
    interval_secs: int | None = Field(default=None, ge=60)
    watchlist: list[str] | str | None = None
    timeframes: list[str] | str | None = None
    min_score: float | None = Field(default=None, ge=0, le=100)
    max_candidates_per_run: int | None = Field(default=None, ge=1, le=50)
    symbol_cooldown_secs: int | None = Field(default=None, ge=0)
    setup_cooldown_secs: int | None = Field(default=None, ge=60)
    max_signals_per_day: int | None = Field(default=None, ge=0)
    max_ai_calls_per_day: int | None = Field(default=None, ge=0)
    rsi_lower: float | None = Field(default=None, ge=1, le=99)
    rsi_upper: float | None = Field(default=None, ge=1, le=99)
    min_atr_pct: float | None = Field(default=None, ge=0)
    max_spread_pct: float | None = Field(default=None, ge=0)
    live_symbol_whitelist: list[str] | str | None = None
    shutdown_timeout_secs: int | None = Field(default=None, ge=1)
    symbol_map: dict[str, Any] | str | None = None
    max_concurrent_fetches: int | None = Field(default=None, ge=1, le=50)
    bundle_cache_ttl_secs: int | None = Field(default=None, ge=0, le=3600)
    ai_min_confidence: float | None = Field(default=None, ge=0, le=1)
    rejected_symbol_cooldown_secs: int | None = Field(default=None, ge=0)
    blocked_symbol_cooldown_secs: int | None = Field(default=None, ge=0)
    mtf_confirmation_bonus: float | None = Field(default=None, ge=0, le=50)
    mtf_conflict_penalty: float | None = Field(default=None, ge=0, le=50)
    min_volume_ratio: float | None = Field(default=None, ge=0)
    max_candle_gap_ratio: float | None = Field(default=None, ge=0, le=1)
    max_price_deviation_pct: float | None = Field(default=None, ge=0)
    score_weights: dict[str, Any] | str | None = None
    ema200_enabled: bool | None = None
    htf_conflict_enabled: bool | None = None
    regime_filter_enabled: bool | None = None


def _state_payload(state: Any) -> dict[str, Any]:
    return {
        "scope": state.scope,
        "date_key": state.date_key,
        "scan_count": int(state.scan_count or 0),
        "ai_call_count": int(state.ai_call_count or 0),
        "signal_count": int(state.signal_count or 0),
        "data_failure_streak": int(getattr(state, "data_failure_streak", 0) or 0),
        "last_scan_at": state.last_scan_at.isoformat() if state.last_scan_at else None,
        "last_data_failure_at": (
            state.last_data_failure_at.isoformat() if getattr(state, "last_data_failure_at", None) else None
        ),
        "degraded_mode": state.degraded_mode or "",
        "degraded_reason": state.degraded_reason or "",
        "updated_at": state.updated_at.isoformat() if state.updated_at else None,
    }


def _audit_payload(row: Any) -> dict[str, Any]:
    try:
        payload = json.loads(row.payload_json or "{}")
    except (TypeError, json.JSONDecodeError):
        payload = {}
    return {
        "id": row.id,
        "scope": row.scope,
        "run_id": row.run_id,
        "event_type": row.event_type,
        "watch_symbol": row.watch_symbol,
        "exchange_symbol": row.exchange_symbol,
        "direction": row.direction,
        "score": row.score,
        "setup_hash": row.setup_hash,
        "reason": row.reason,
        "payload": payload,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


@router.get("/status")
async def scanner_status(
    admin: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    state = await get_or_create_scanner_state(db, scope="admin")
    service = get_market_scanner_service()
    return {
        "enabled": settings.scanner.enabled,
        "mode": settings.scanner.mode,
        "interval_secs": settings.scanner.interval_secs,
        "watchlist": settings.scanner.watchlist,
        "timeframes": settings.scanner.timeframes,
        "min_score": settings.scanner.min_score,
        "max_candidates_per_run": settings.scanner.max_candidates_per_run,
        "live_symbol_whitelist": settings.scanner.live_symbol_whitelist,
        "symbol_map": settings.scanner.symbol_map,
        "shutdown_timeout_secs": settings.scanner.shutdown_timeout_secs,
        "daily_limits": {
            "max_signals_per_day": settings.scanner.max_signals_per_day,
            "max_ai_calls_per_day": settings.scanner.max_ai_calls_per_day,
        },
        "cooldowns": {
            "symbol_cooldown_secs": settings.scanner.symbol_cooldown_secs,
            "setup_cooldown_secs": settings.scanner.setup_cooldown_secs,
            "rejected_symbol_cooldown_secs": settings.scanner.rejected_symbol_cooldown_secs,
            "blocked_symbol_cooldown_secs": settings.scanner.blocked_symbol_cooldown_secs,
        },
        "thresholds": {
            "rsi_lower": settings.scanner.rsi_lower,
            "rsi_upper": settings.scanner.rsi_upper,
            "min_atr_pct": settings.scanner.min_atr_pct,
            "max_spread_pct": settings.scanner.max_spread_pct,
            "ai_min_confidence": settings.scanner.ai_min_confidence,
            "min_volume_ratio": settings.scanner.min_volume_ratio,
            "max_candle_gap_ratio": settings.scanner.max_candle_gap_ratio,
            "max_price_deviation_pct": settings.scanner.max_price_deviation_pct,
        },
        "performance": {
            "max_concurrent_fetches": settings.scanner.max_concurrent_fetches,
            "bundle_cache_ttl_secs": settings.scanner.bundle_cache_ttl_secs,
        },
        "scoring": {
            "mtf_confirmation_bonus": settings.scanner.mtf_confirmation_bonus,
            "mtf_conflict_penalty": settings.scanner.mtf_conflict_penalty,
            "score_weights": settings.scanner.score_weights,
            "ema200_enabled": settings.scanner.ema200_enabled,
            "htf_conflict_enabled": settings.scanner.htf_conflict_enabled,
            "regime_filter_enabled": settings.scanner.regime_filter_enabled,
        },
        "state": _state_payload(state),
        "runtime": service.last_status,
    }


@router.post("/settings")
async def scanner_update_settings(
    update: ScannerSettingsUpdate,
    admin: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    data = update.model_dump(exclude_unset=True)
    updated = await save_scanner_settings(db, data)
    await db.commit()

    from core.lifespan import sync_scanner_scheduler

    scheduler = sync_scanner_scheduler()
    state = await get_or_create_scanner_state(db, scope="admin")
    return {
        "settings": updated,
        "scheduler": scheduler,
        "state": _state_payload(state),
    }


@router.get("/audits")
async def scanner_audits(
    event_type: str | None = Query(default=None, max_length=40),
    symbol: str | None = Query(default=None, max_length=60),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    admin: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    rows = await list_scanner_audits(
        db,
        event_type=event_type,
        symbol=symbol,
        limit=limit,
        offset=offset,
    )
    return {"items": [_audit_payload(row) for row in rows], "limit": limit, "offset": offset}


@router.get("/rejection-summary")
async def scanner_rejection_summary(
    date_key: str | None = Query(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    admin: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    return await get_scanner_rejection_summary(db, scope="admin", date_key=date_key)


@router.post("/rejection-summary/send")
async def scanner_send_rejection_summary(
    date_key: str | None = Query(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    admin: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    from notifier import notify_scanner_rejection_summary

    summary = await get_scanner_rejection_summary(db, scope="admin", date_key=date_key)
    await notify_scanner_rejection_summary(summary)
    return {"sent": int(summary.get("rejected_or_held") or 0) > 0, "summary": summary}


@router.post("/run-once")
async def scanner_run_once(admin: dict = Depends(require_admin)):
    return await run_scanner_once()
