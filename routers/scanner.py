"""Admin APIs for the automatic market scanner."""
from __future__ import annotations

import json
import time
from datetime import timedelta
from typing import Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from core.auth import require_admin
from core.config import settings
from core.database import (
    ScannerAuditModel,
    get_db,
    get_or_create_scanner_state,
    get_scanner_rejection_summary,
    list_scanner_audits,
)
from core.runtime_settings import save_scanner_settings
from services.market_scanner import get_market_scanner_service, run_scanner_once

router = APIRouter(prefix="/api/scanner", tags=["scanner"])

# In-memory rate limit for run-once (admin anti-spam)
_RUN_ONCE_RATELIMIT: dict[str, float] = {}
_RUN_ONCE_WINDOW_SECS = 60.0


class ScannerSettingsUpdate(BaseModel):
    enabled: bool | None = None
    mode: str | None = Field(default=None, description="observe, paper, or live")
    interval_secs: int | None = Field(default=None, ge=60)
    watchlist: list[str] | str | None = None
    source_mode: str | None = Field(default=None, description="manual, follow_exchange, custom_exchange, or hybrid")
    source_exchange: str | None = None
    source_market_type: str | None = Field(default=None, description="spot or contract")
    data_source_policy: str | None = Field(default=None, description="strict, fallback, or confirm")
    universe_top_n: int | None = Field(default=None, ge=1, le=1000)
    universe_min_quote_volume: float | None = Field(default=None, ge=0)
    universe_cache_ttl_secs: int | None = Field(default=None, ge=0, le=86400)
    confirm_max_volume_deviation_pct: float | None = Field(default=None, ge=0)
    include_symbols: list[str] | str | None = None
    exclude_symbols: list[str] | str | None = None
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
    learning_enabled: bool | None = None
    outcome_lookback_days: int | None = Field(default=None, ge=1, le=365)
    outcome_max_sync_positions: int | None = Field(default=None, ge=1, le=1000)
    outcome_path_metrics_enabled: bool | None = None
    walk_forward_enabled: bool | None = None
    walk_forward_min_samples: int | None = Field(default=None, ge=3, le=1000)
    walk_forward_validation_ratio: float | None = Field(default=None, ge=0.1, le=0.5)
    walk_forward_threshold_step: float | None = Field(default=None, ge=0.5, le=10)
    hard_filters_enabled: bool | None = None
    require_support_zone: bool | None = None
    require_structure_alignment: bool | None = None
    min_mtf_confirmations: int | None = Field(default=None, ge=1, le=10)
    min_rr_ratio: float | None = Field(default=None, ge=0, le=10)
    mtf_consensus_enabled: bool | None = None
    mtf_consensus_min_margin: float | None = Field(default=None, ge=0, le=100)
    mtf_consensus_htf_weight: float | None = Field(default=None, ge=0.1, le=10)
    mtf_consensus_ltf_weight: float | None = Field(default=None, ge=0.1, le=10)
    liquidity_filter_enabled: bool | None = None
    liquidity_order_size_usdt: float | None = Field(default=None, ge=0)
    min_quote_volume_24h: float | None = Field(default=None, ge=0)
    min_orderbook_depth_usdt: float | None = Field(default=None, ge=0)
    max_estimated_slippage_pct: float | None = Field(default=None, ge=0)
    min_orderbook_imbalance_long: float | None = Field(default=None, ge=0)
    max_orderbook_imbalance_short: float | None = Field(default=None, ge=0)
    event_filter_enabled: bool | None = None
    funding_blackout_minutes: int | None = Field(default=None, ge=0, le=240)
    max_abs_funding_rate: float | None = Field(default=None, ge=0, le=1)
    low_liquidity_utc_hours: list[int] | str | None = None
    event_blackout_utc_windows: list[str] | str | None = None
    portfolio_risk_enabled: bool | None = None
    max_same_direction_exposure: int | None = Field(default=None, ge=1, le=100)
    max_correlated_signals_per_run: int | None = Field(default=None, ge=1, le=100)
    correlation_buckets: dict[str, Any] | str | None = None


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
    quality = payload.get("quality") if isinstance(payload.get("quality"), dict) else {}
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
        "target_exchange": payload.get("target_exchange") or quality.get("target_exchange"),
        "source_exchange": payload.get("source_exchange") or quality.get("source_exchange"),
        "actual_data_source": payload.get("actual_data_source") or quality.get("actual_data_source"),
        "tradable": payload.get("tradable") if "tradable" in payload else quality.get("tradable"),
        "tradability_reason": payload.get("tradability_reason") or quality.get("tradability_reason"),
        "payload": payload,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


async def _source_performance_payload(db: AsyncSession) -> dict[str, Any]:
    stmt = (
        select(ScannerAuditModel)
        .where(ScannerAuditModel.event_type == "result")
        .order_by(desc(ScannerAuditModel.created_at))
        .limit(200)
    )
    result = await db.execute(stmt)
    stats: dict[str, dict[str, Any]] = {}
    for row in result.scalars().all():
        try:
            payload = json.loads(row.payload_json or "{}")
        except (TypeError, json.JSONDecodeError):
            payload = {}
        source = str(payload.get("actual_data_source") or payload.get("source_exchange") or "unknown").lower().strip()
        target = str(payload.get("target_exchange") or "unknown").lower().strip()
        key = f"{source}->{target}"
        item = stats.setdefault(key, {"source": source, "target": target, "total": 0, "statuses": {}, "ai_used": 0})
        item["total"] += 1
        res = payload.get("result") if isinstance(payload.get("result"), dict) else {}
        status = str(res.get("status") or "unknown").lower().strip()
        item["statuses"][status] = int(item["statuses"].get(status, 0)) + 1
        if payload.get("ai_used"):
            item["ai_used"] += 1
    return dict(sorted(stats.items(), key=lambda item: item[1]["total"], reverse=True))


@router.get("/status")
async def scanner_status(
    admin: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    state = await get_or_create_scanner_state(db, scope="admin")
    service = get_market_scanner_service()
    source_performance = await _source_performance_payload(db)
    return {
        "enabled": settings.scanner.enabled,
        "mode": settings.scanner.mode,
        "interval_secs": settings.scanner.interval_secs,
        "watchlist": settings.scanner.watchlist,
        "source_mode": settings.scanner.source_mode,
        "source_exchange": settings.scanner.source_exchange,
        "source_market_type": settings.scanner.source_market_type,
        "data_source_policy": settings.scanner.data_source_policy,
        "universe_top_n": settings.scanner.universe_top_n,
        "universe_min_quote_volume": settings.scanner.universe_min_quote_volume,
        "universe_cache_ttl_secs": settings.scanner.universe_cache_ttl_secs,
        "confirm_max_volume_deviation_pct": settings.scanner.confirm_max_volume_deviation_pct,
        "include_symbols": settings.scanner.include_symbols,
        "exclude_symbols": settings.scanner.exclude_symbols,
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
        "source": {
            "mode": settings.scanner.source_mode,
            "target_exchange": settings.exchange.name,
            "target_market_type": settings.exchange.market_type,
            "source_exchange": settings.scanner.source_exchange or settings.exchange.name,
            "source_market_type": settings.scanner.source_market_type or settings.exchange.market_type,
            "data_source_policy": settings.scanner.data_source_policy,
            "universe_top_n": settings.scanner.universe_top_n,
            "universe_min_quote_volume": settings.scanner.universe_min_quote_volume,
            "universe_cache_ttl_secs": settings.scanner.universe_cache_ttl_secs,
            "confirm_max_volume_deviation_pct": settings.scanner.confirm_max_volume_deviation_pct,
            "include_symbols": settings.scanner.include_symbols,
            "exclude_symbols": settings.scanner.exclude_symbols,
            "last_universe": service.last_status.get("last_universe", {}),
            "source_health": service.source_health,
            "source_performance": source_performance,
            "live_universe_snapshot": service.last_status.get("live_universe_snapshot", {}),
        },
        "scoring": {
            "mtf_confirmation_bonus": settings.scanner.mtf_confirmation_bonus,
            "mtf_conflict_penalty": settings.scanner.mtf_conflict_penalty,
            "score_weights": settings.scanner.score_weights,
            "ema200_enabled": settings.scanner.ema200_enabled,
            "htf_conflict_enabled": settings.scanner.htf_conflict_enabled,
            "regime_filter_enabled": settings.scanner.regime_filter_enabled,
        },
        "learning": {
            "learning_enabled": settings.scanner.learning_enabled,
            "outcome_lookback_days": settings.scanner.outcome_lookback_days,
            "outcome_max_sync_positions": settings.scanner.outcome_max_sync_positions,
            "outcome_path_metrics_enabled": settings.scanner.outcome_path_metrics_enabled,
            "walk_forward_enabled": settings.scanner.walk_forward_enabled,
            "walk_forward_min_samples": settings.scanner.walk_forward_min_samples,
            "walk_forward_validation_ratio": settings.scanner.walk_forward_validation_ratio,
            "walk_forward_threshold_step": settings.scanner.walk_forward_threshold_step,
            "hard_filters_enabled": settings.scanner.hard_filters_enabled,
            "require_support_zone": settings.scanner.require_support_zone,
            "require_structure_alignment": settings.scanner.require_structure_alignment,
            "min_mtf_confirmations": settings.scanner.min_mtf_confirmations,
            "min_rr_ratio": settings.scanner.min_rr_ratio,
            "mtf_consensus_enabled": settings.scanner.mtf_consensus_enabled,
            "mtf_consensus_min_margin": settings.scanner.mtf_consensus_min_margin,
            "mtf_consensus_htf_weight": settings.scanner.mtf_consensus_htf_weight,
            "mtf_consensus_ltf_weight": settings.scanner.mtf_consensus_ltf_weight,
            "liquidity_filter_enabled": settings.scanner.liquidity_filter_enabled,
            "liquidity_order_size_usdt": settings.scanner.liquidity_order_size_usdt,
            "min_quote_volume_24h": settings.scanner.min_quote_volume_24h,
            "min_orderbook_depth_usdt": settings.scanner.min_orderbook_depth_usdt,
            "max_estimated_slippage_pct": settings.scanner.max_estimated_slippage_pct,
            "min_orderbook_imbalance_long": settings.scanner.min_orderbook_imbalance_long,
            "max_orderbook_imbalance_short": settings.scanner.max_orderbook_imbalance_short,
            "event_filter_enabled": settings.scanner.event_filter_enabled,
            "funding_blackout_minutes": settings.scanner.funding_blackout_minutes,
            "max_abs_funding_rate": settings.scanner.max_abs_funding_rate,
            "low_liquidity_utc_hours": settings.scanner.low_liquidity_utc_hours,
            "event_blackout_utc_windows": settings.scanner.event_blackout_utc_windows,
            "portfolio_risk_enabled": settings.scanner.portfolio_risk_enabled,
            "max_same_direction_exposure": settings.scanner.max_same_direction_exposure,
            "max_correlated_signals_per_run": settings.scanner.max_correlated_signals_per_run,
            "correlation_buckets": settings.scanner.correlation_buckets,
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


@router.get("/universe-preview")
async def scanner_universe_preview(
    refresh: bool = Query(default=False),
    limit: int = Query(default=200, ge=1, le=1000),
    admin: dict = Depends(require_admin),
):
    service = get_market_scanner_service()
    return await service.preview_universe(force_refresh=refresh, limit=limit)


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
    admin_id = str(admin.get("id") or admin.get("username") or "admin")
    now = __import__("time").time()
    last_run = _RUN_ONCE_RATELIMIT.get(admin_id, 0.0)
    if now - last_run < _RUN_ONCE_WINDOW_SECS:
        remaining = int(_RUN_ONCE_WINDOW_SECS - (now - last_run))
        from fastapi import HTTPException
        raise HTTPException(
            status_code=429,
            detail=f"Run-once rate limit: please wait {remaining}s before triggering another scan."
        )
    _RUN_ONCE_RATELIMIT[admin_id] = now
    return await run_scanner_once()


class BacktestRequest(BaseModel):
    symbols: list[str] = Field(default_factory=lambda: list(settings.scanner.watchlist))
    timeframes: list[str] | None = None
    lookback_days: int = Field(default=30, ge=1, le=365)
    simulation_bars: int = Field(default=24, ge=6, le=100)
    min_score_threshold: float = Field(default=65.0, ge=40.0, le=95.0)
    weights_override: dict[str, float] | None = None


class RulesUpdateRequest(BaseModel):
    rules: list[dict[str, Any]]
    weights: dict[str, float] | None = None


@router.post("/backtest")
async def scanner_backtest(
    request: BacktestRequest,
    admin: dict = Depends(require_admin),
):
    from services.scanner_backtest import get_backtester
    backtester = get_backtester()
    backtester.lookback_days = request.lookback_days
    backtester.simulation_bars = request.simulation_bars
    backtester.min_score_threshold = request.min_score_threshold
    summary = await backtester.run_backtest(
        symbols=request.symbols,
        timeframes=request.timeframes,
        weights_override=request.weights_override,
    )
    return summary.to_dict()


@router.get("/backtest/history")
async def scanner_backtest_history(
    admin: dict = Depends(require_admin),
):
    from services.scanner_backtest import get_backtester

    backtester = get_backtester()
    loaded = backtester.load_results()
    if not loaded or not backtester._results:
        return {"results": [], "loaded": False}
    return {"results": [r.to_dict() for r in backtester._results[-100:]], "loaded": True, "count": len(backtester._results)}


@router.get("/rules")
async def scanner_get_rules(admin: dict = Depends(require_admin)):
    from services.scanner_rules import DEFAULT_ENGINE
    return {"rules": DEFAULT_ENGINE.get_rules(), "weights": DEFAULT_ENGINE.weights}


@router.post("/rules")
async def scanner_update_rules(
    request: RulesUpdateRequest,
    admin: dict = Depends(require_admin),
):
    from services.scanner_rules import DEFAULT_ENGINE, ScoringRule, save_rules_config
    rules = [ScoringRule.from_dict(r) for r in request.rules]
    DEFAULT_ENGINE.set_rules(rules)
    if request.weights:
        DEFAULT_ENGINE.set_weights(request.weights)
    save_rules_config()
    return {"saved": True, "rules_count": len(rules), "weights": DEFAULT_ENGINE.weights}


@router.post("/rules/reset")
async def scanner_reset_rules(admin: dict = Depends(require_admin)):
    from services.scanner_rules import DEFAULT_ENGINE, DEFAULT_RULES, save_rules_config
    DEFAULT_ENGINE.rules = DEFAULT_RULES.copy()
    DEFAULT_ENGINE.weights = dict(settings.scanner.score_weights or {})
    save_rules_config()
    return {"reset": True, "rules_count": len(DEFAULT_ENGINE.rules)}


@router.get("/rules/correlations")
async def scanner_rules_correlations(
    admin: dict = Depends(require_admin),
):
    from services.scanner_backtest import get_backtester
    from services.scanner_rules import DEFAULT_ENGINE
    backtester = get_backtester()
    loaded = backtester.load_results()
    if not loaded or len(backtester._results) < 20:
        return {"correlations": {}, "orthogonal_groups": [], "message": "Insufficient backtest data (need 20+ results)"}
    history = [{"score_breakdown": r.score_breakdown} for r in backtester._results]
    correlations = DEFAULT_ENGINE.analyze_correlations(history)
    groups = DEFAULT_ENGINE.identify_orthogonal_groups(correlations)
    suggestions = DEFAULT_ENGINE.suggest_weight_adjustments(correlations)
    return {
        "correlations": correlations,
        "orthogonal_groups": groups,
        "suggested_weights": suggestions,
    }


@router.get("/analytics/funnel")
async def scanner_funnel_analytics(
    days: int = Query(default=7, ge=1, le=30),
    admin: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    from core.utils.datetime import utcnow

    cutoff = utcnow() - timedelta(days=days)
    stmt = select(
        ScannerAuditModel.event_type,
        func.count(ScannerAuditModel.id).label("count"),
    ).where(
        ScannerAuditModel.created_at >= cutoff,
    ).group_by(ScannerAuditModel.event_type)

    result = await db.execute(stmt)
    rows = result.all()

    event_counts = {row[0]: row[1] for row in rows}

    scanned = event_counts.get("scanned", 0)
    data_errors = event_counts.get("data_error", 0)
    cooldowns = event_counts.get("cooldown", 0)
    filtered = event_counts.get("filtered", 0)
    candidates = event_counts.get("candidate", 0)
    direction_conflicts = event_counts.get("direction_conflict", 0)
    deduped = event_counts.get("deduped", 0)
    sent_to_ai = event_counts.get("sent_to_ai", 0)
    results = event_counts.get("result", 0)

    result_stmt = select(ScannerAuditModel.payload_json).where(
        ScannerAuditModel.event_type == "result",
        ScannerAuditModel.created_at >= cutoff,
    )
    result_rows = await db.execute(result_stmt)
    payloads = result_rows.scalars().all()

    status_counts: dict[str, int] = {}
    for payload_json in payloads:
        try:
            payload = json.loads(payload_json or "{}")
            status = str(payload.get("result", {}).get("status", "unknown")).lower()
            status_counts[status] = status_counts.get(status, 0) + 1
        except (json.JSONDecodeError, TypeError):
            pass

    funnel = {
        "scanned": scanned,
        "data_errors": data_errors,
        "cooldown_blocked": cooldowns,
        "filtered_low_score": filtered,
        "candidates_found": candidates,
        "direction_conflicts": direction_conflicts,
        "setup_deduped": deduped,
        "sent_to_ai": sent_to_ai,
        "processed_results": results,
        "conversion_rate": round(candidates / max(1, scanned) * 100, 2),
        "ai_usage_rate": round(sent_to_ai / max(1, candidates) * 100, 2),
        "success_rate": round(
            (
                status_counts.get("executed", 0)
                + status_counts.get("paper_executed", 0)
                + status_counts.get("filled", 0)
                + status_counts.get("simulated", 0)
            ) / max(1, results) * 100,
            2,
        ),
    }

    return {
        "funnel": funnel,
        "event_counts": event_counts,
        "status_counts": status_counts,
        "days": days,
    }


@router.get("/analytics/win-rate")
async def scanner_win_rate_analytics(
    days: int = Query(default=7, ge=1, le=30),
    admin: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    state = await get_or_create_scanner_state(db, scope="admin")

    try:
        history = json.loads(state.win_rate_history_json or "[]")
    except (json.JSONDecodeError, TypeError):
        history = []

    return {
        "signal_wins": int(state.signal_wins or 0),
        "signal_losses": int(state.signal_losses or 0),
        "signal_win_rate": float(state.signal_win_rate or 0.0),
        "adaptive_min_score": float(state.adaptive_min_score or 0.0),
        "win_rate_history": history[-50:],
        "last_updated": state.last_win_rate_update_at.isoformat() if state.last_win_rate_update_at else None,
    }


@router.get("/analytics/outcomes")
async def scanner_outcome_analytics(
    days: int = Query(default=30, ge=1, le=365),
    sync: bool = Query(default=True),
    admin: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    from services.scanner_learning import compute_outcome_summary, sync_scanner_outcomes

    sync_result = {"synced": 0, "skipped": 0}
    if sync:
        sync_result = await sync_scanner_outcomes(db, scope="admin", days=days, include_path_metrics=False)
        await db.commit()
    summary = await compute_outcome_summary(db, scope="admin", days=days)
    return {"sync": sync_result, "summary": summary, "days": days}


@router.get("/analytics/walk-forward")
async def scanner_walk_forward_analytics(
    days: int = Query(default=30, ge=1, le=365),
    admin: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    from services.scanner_learning import compute_walk_forward_thresholds

    return await compute_walk_forward_thresholds(db, scope="admin", days=days)


@router.get("/analytics/factor-contribution")
async def scanner_factor_contribution(
    days: int = Query(default=7, ge=1, le=30),
    admin: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    from services.scanner_learning import compute_factor_performance

    outcome_performance = await compute_factor_performance(db, scope="admin", days=days)
    if outcome_performance.get("factors"):
        return outcome_performance

    from core.utils.datetime import utcnow

    cutoff = utcnow() - timedelta(days=days)
    stmt = select(ScannerAuditModel.payload_json).where(
        ScannerAuditModel.event_type == "candidate",
        ScannerAuditModel.created_at >= cutoff,
    )
    rows = await db.execute(stmt)
    payloads = rows.scalars().all()

    factor_totals: dict[str, float] = {}
    factor_counts: dict[str, int] = {}

    for payload_json in payloads:
        try:
            payload = json.loads(payload_json or "{}")
            breakdown = payload.get("score_breakdown", {}) or {}
            for factor, value in breakdown.items():
                factor_totals[factor] = factor_totals.get(factor, 0.0) + abs(float(value))
                factor_counts[factor] = factor_counts.get(factor, 0) + 1
        except (json.JSONDecodeError, TypeError, ValueError):
            pass

    factor_avg = {
        factor: round(factor_totals.get(factor, 0.0) / max(1, factor_counts.get(factor, 1)), 2)
        for factor in factor_totals
    }

    sorted_factors = sorted(factor_avg.items(), key=lambda x: abs(x[1]), reverse=True)

    return {
        "factor_avg_contribution": factor_avg,
        "top_factors": sorted_factors[:10],
        "factor_counts": factor_counts,
        "candidates_analyzed": len(payloads),
    }


@router.get("/analytics/ai-circuit-breaker")
async def scanner_ai_circuit_breaker_status(
    admin: dict = Depends(require_admin),
):
    from services.signal_processor import _AI_CB_COOLDOWN_SECS, _AI_CB_FAILURES, _AI_CB_OPEN_UNTIL, _AI_CB_THRESHOLD

    now = time.time()
    is_open = now < _AI_CB_OPEN_UNTIL
    remaining = int(_AI_CB_OPEN_UNTIL - now) if is_open else 0

    return {
        "failures": _AI_CB_FAILURES,
        "threshold": _AI_CB_THRESHOLD,
        "cooldown_secs": _AI_CB_COOLDOWN_SECS,
        "is_open": is_open,
        "remaining_cooldown_secs": remaining,
        "status": "circuit_breaker_open" if is_open else "normal",
    }


@router.get("/analytics/cooldown-levels")
async def scanner_cooldown_levels(
    admin: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    stmt = select(
        ScannerAuditModel.exchange_symbol,
        func.count(ScannerAuditModel.id).label("reject_count"),
    ).where(
        ScannerAuditModel.event_type == "result",
    ).group_by(ScannerAuditModel.exchange_symbol).order_by(desc("reject_count")).limit(20)

    result = await db.execute(stmt)
    rows = result.all()

    symbols_with_penalty = []
    for row in rows:
        symbol = row[0]
        count = row[1]
        level = min(count, int(settings.scanner.adaptive_cooldown_levels))
        symbols_with_penalty.append({
            "symbol": symbol,
            "reject_count": count,
            "cooldown_level": level,
        })

    return {
        "symbols_with_penalty": symbols_with_penalty,
        "adaptive_cooldown_levels": int(settings.scanner.adaptive_cooldown_levels),
        "adaptive_cooldown_base_secs": int(settings.scanner.adaptive_cooldown_base_secs),
        "adaptive_cooldown_multiplier": float(settings.scanner.adaptive_cooldown_multiplier),
    }
