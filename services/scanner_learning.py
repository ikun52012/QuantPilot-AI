"""Outcome learning utilities for the automatic market scanner."""
from __future__ import annotations

import json
import math
from datetime import UTC, datetime, timedelta
from typing import Any

from loguru import logger
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from core.config import settings
from core.database import (
    PositionModel,
    ScannerAuditModel,
    TradeModel,
    get_or_create_scanner_state,
    record_scanner_audit,
)
from core.utils.datetime import utcnow


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        parsed = float(value)
        return parsed if math.isfinite(parsed) else default
    except (TypeError, ValueError):
        return default


def _parse_json(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if not value:
        return {}
    try:
        parsed = json.loads(str(value))
        return parsed if isinstance(parsed, dict) else {}
    except (TypeError, json.JSONDecodeError):
        return {}


def _dt(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if isinstance(value, (int, float)):
        raw = float(value)
        if raw > 10_000_000_000:
            raw /= 1000.0
        # BUG FIX: Use timezone-aware datetime
        return datetime.fromtimestamp(raw, tz=UTC).replace(tzinfo=None)
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).replace(tzinfo=None)
        except ValueError:
            return None
    return None


def extract_scanner_payload(trade_payload: dict[str, Any]) -> dict[str, Any]:
    """Extract scanner metadata from current or legacy trade payloads."""
    scanner = trade_payload.get("scanner")
    if isinstance(scanner, dict) and (scanner.get("setup_hash") or scanner.get("score")):
        return dict(scanner)

    signal = trade_payload.get("signal") if isinstance(trade_payload.get("signal"), dict) else {}
    scanner = signal.get("scanner") if isinstance(signal.get("scanner"), dict) else {}
    if scanner and (scanner.get("setup_hash") or scanner.get("score")):
        return dict(scanner)

    message_payload = _parse_json(signal.get("message"))
    nested = message_payload.get("scanner") if isinstance(message_payload.get("scanner"), dict) else None
    if nested and (nested.get("setup_hash") or nested.get("score")):
        return dict(nested)
    if message_payload.get("setup_hash") or message_payload.get("score"):
        return message_payload
    return {}


async def _candidate_payload_for_setup(session: AsyncSession, setup_hash: str) -> dict[str, Any]:
    if not setup_hash:
        return {}
    result = await session.execute(
        select(ScannerAuditModel)
        .where(
            ScannerAuditModel.event_type == "candidate",
            ScannerAuditModel.setup_hash == setup_hash,
        )
        .order_by(ScannerAuditModel.created_at.desc())
        .limit(1)
    )
    row = result.scalar_one_or_none()
    return _parse_json(row.payload_json) if row else {}


async def _path_metrics(position: PositionModel, timeframe: str, skip_path_metrics: bool = False) -> dict[str, float | None]:
    if not settings.scanner.outcome_path_metrics_enabled:
        return {"mae_pct": None, "mfe_pct": None}
    if not position.opened_at or not position.closed_at:
        return {"mae_pct": None, "mfe_pct": None}

    hold_seconds = max(0.0, (position.closed_at - position.opened_at).total_seconds())
    days = max(2, min(90, int(hold_seconds / 86400.0) + 2))
    try:
        from market_data import fetch_ohlcv_history

        # BUG FIX: Skip path metrics for bulk sync to avoid N+1 API calls
        if skip_path_metrics:
            return {"mae_pct": None, "mfe_pct": None}

        rows = await fetch_ohlcv_history(position.ticker, timeframe=timeframe or "1h", days=days)
    except Exception as exc:
        logger.debug(f"[ScannerLearning] MAE/MFE fetch failed for {position.ticker}: {exc}")
        return {"mae_pct": None, "mfe_pct": None}

    entry = _safe_float(position.entry_price)
    if entry <= 0:
        return {"mae_pct": None, "mfe_pct": None}

    lows: list[float] = []
    highs: list[float] = []
    for row in rows or []:
        ts = _dt(row.get("timestamp") or row.get("time") or row.get("datetime"))
        if ts and position.opened_at <= ts <= position.closed_at:
            lows.append(_safe_float(row.get("low")))
            highs.append(_safe_float(row.get("high")))
    lows = [item for item in lows if item > 0]
    highs = [item for item in highs if item > 0]
    if not lows or not highs:
        return {"mae_pct": None, "mfe_pct": None}

    leverage = max(1.0, _safe_float(position.leverage, 1.0))
    direction = str(position.direction or "long").lower()
    if direction == "short":
        mae = (entry - max(highs)) / entry * 100.0 * leverage
        mfe = (entry - min(lows)) / entry * 100.0 * leverage
    else:
        mae = (min(lows) - entry) / entry * 100.0 * leverage
        mfe = (max(highs) - entry) / entry * 100.0 * leverage
    return {"mae_pct": round(min(0.0, mae), 4), "mfe_pct": round(max(0.0, mfe), 4)}


def _entry_slippage_pct(position: PositionModel, scanner_payload: dict[str, Any], trade_payload: dict[str, Any]) -> float | None:
    signal = trade_payload.get("signal") if isinstance(trade_payload.get("signal"), dict) else {}
    reference = _safe_float(
        scanner_payload.get("current_price")
        or scanner_payload.get("entry_reference")
        or signal.get("price")
    )
    actual = _safe_float(position.entry_price)
    if reference <= 0 or actual <= 0:
        return None
    direction = str(position.direction or "long").lower()
    if direction == "short":
        return round((reference - actual) / reference * 100.0, 4)
    return round((actual - reference) / reference * 100.0, 4)


async def sync_scanner_outcomes(
    session: AsyncSession,
    *,
    scope: str = "admin",
    days: int | None = None,
    max_positions: int | None = None,
    run_id: str = "outcome-sync",
    include_path_metrics: bool | None = None,
) -> dict[str, Any]:
    """Backfill closed scanner positions into append-only outcome_label audit rows."""
    lookback_days = max(1, int(days or settings.scanner.outcome_lookback_days))
    limit = max(1, int(max_positions or settings.scanner.outcome_max_sync_positions))
    cutoff = utcnow() - timedelta(days=lookback_days)
    result = await session.execute(
        select(PositionModel)
        .where(
            PositionModel.status == "closed",
            PositionModel.closed_at.is_not(None),
            PositionModel.closed_at >= cutoff,
        )
        .order_by(desc(PositionModel.closed_at))
        .limit(limit)
    )
    positions = list(result.scalars().all())
    labels: list[dict[str, Any]] = []
    skipped = 0

    original_path_setting = settings.scanner.outcome_path_metrics_enabled
    if include_path_metrics is not None:
        settings.scanner.outcome_path_metrics_enabled = bool(include_path_metrics)
    try:
        for position in positions:
            if not position.open_trade_id:
                skipped += 1
                continue
            open_trade = await session.get(TradeModel, position.open_trade_id)
            if not open_trade:
                skipped += 1
                continue
            trade_payload = _parse_json(open_trade.payload_json)
            scanner_payload = extract_scanner_payload(trade_payload)
            if not scanner_payload:
                skipped += 1
                continue

            setup_hash = str(scanner_payload.get("setup_hash") or "").strip()
            dedupe_hash = setup_hash or f"position:{position.id}"
            existing = await session.execute(
                select(ScannerAuditModel.id)
                .where(
                    ScannerAuditModel.event_type == "outcome_label",
                    ScannerAuditModel.setup_hash == dedupe_hash[:64],
                )
                .limit(1)
            )
            if existing.scalar_one_or_none():
                skipped += 1
                continue

            candidate_payload = await _candidate_payload_for_setup(session, setup_hash)
            score_breakdown = scanner_payload.get("score_breakdown") or candidate_payload.get("score_breakdown") or {}
            timeframe = str(scanner_payload.get("timeframe") or candidate_payload.get("timeframe") or "1h")
            pnl_pct = _safe_float(position.pnl_pct)
            pnl_usdt = _safe_float(position.unrealized_pnl_usdt)
            if position.close_trade_id:
                close_trade = await session.get(TradeModel, position.close_trade_id)
                if close_trade:
                    close_payload = _parse_json(close_trade.payload_json)
                    pnl_usdt = _safe_float(close_payload.get("pnl_usdt"), pnl_usdt)
            path = await _path_metrics(position, timeframe, skip_path_metrics=False)
            hold_minutes = 0.0
            if position.opened_at and position.closed_at:
                hold_minutes = max(0.0, (position.closed_at - position.opened_at).total_seconds() / 60.0)

            label = {
                "position_id": position.id,
                "open_trade_id": position.open_trade_id,
                "close_trade_id": position.close_trade_id or "",
                "setup_hash": setup_hash,
                "symbol": position.ticker,
                "timeframe": timeframe,
                "direction": str(position.direction or "").lower(),
                "score": _safe_float(scanner_payload.get("score") or candidate_payload.get("score")),
                "score_breakdown": score_breakdown,
                "pnl_pct": round(pnl_pct, 6),
                "pnl_usdt": round(pnl_usdt, 6),
                "fees_usdt": _safe_float(position.fees_total_usdt),
                "entry_slippage_pct": _entry_slippage_pct(position, scanner_payload, trade_payload),
                "mae_pct": path.get("mae_pct"),
                "mfe_pct": path.get("mfe_pct"),
                "outcome": "win" if pnl_pct > 0 else "loss" if pnl_pct < 0 else "flat",
                "hold_minutes": round(hold_minutes, 2),
                "close_reason": position.close_reason or "",
                "opened_at": position.opened_at.isoformat() if position.opened_at else None,
                "closed_at": position.closed_at.isoformat() if position.closed_at else None,
            }
            await record_scanner_audit(
                session,
                scope=scope,
                run_id=run_id,
                event_type="outcome_label",
                watch_symbol=position.ticker,
                exchange_symbol=position.ticker,
                direction=label["direction"],
                score=label["score"],
                setup_hash=dedupe_hash,
                reason=label["outcome"],
                payload=label,
            )
            labels.append(label)
    finally:
        settings.scanner.outcome_path_metrics_enabled = original_path_setting

    if labels:
        await _refresh_state_from_outcomes(session, scope=scope, days=lookback_days)
    return {"synced": len(labels), "skipped": skipped, "labels": labels}


async def _outcome_labels(session: AsyncSession, *, scope: str = "admin", days: int | None = None) -> list[dict[str, Any]]:
    lookback_days = max(1, int(days or settings.scanner.outcome_lookback_days))
    cutoff = utcnow() - timedelta(days=lookback_days)
    result = await session.execute(
        select(ScannerAuditModel)
        .where(
            ScannerAuditModel.scope == scope,
            ScannerAuditModel.event_type == "outcome_label",
            ScannerAuditModel.created_at >= cutoff,
        )
        .order_by(ScannerAuditModel.created_at.asc())
    )
    labels: list[dict[str, Any]] = []
    for row in result.scalars().all():
        payload = _parse_json(row.payload_json)
        if payload:
            payload.setdefault("score", row.score)
            payload.setdefault("symbol", row.exchange_symbol or row.watch_symbol)
            payload.setdefault("direction", row.direction)
            payload.setdefault("created_at", row.created_at.isoformat() if row.created_at else None)
            labels.append(payload)
    return labels


async def _refresh_state_from_outcomes(session: AsyncSession, *, scope: str, days: int) -> None:
    summary = await compute_outcome_summary(session, scope=scope, days=days, include_recent=False)
    state = await get_or_create_scanner_state(session, scope=scope)
    state.signal_wins = int(summary.get("wins") or 0)
    state.signal_losses = int(summary.get("losses") or 0)
    state.signal_win_rate = float(summary.get("win_rate") or 0.0)
    state.last_win_rate_update_at = utcnow()
    history = {
        "timestamp": utcnow().isoformat(),
        "source": "outcome_label",
        "win_rate": state.signal_win_rate,
        "wins": state.signal_wins,
        "losses": state.signal_losses,
        "expectancy_pct": summary.get("expectancy_pct"),
    }
    # BUG FIX: Append to history instead of replacing entire array
    try:
        existing_history = json.loads(state.win_rate_history_json or "[]")
        if not isinstance(existing_history, list):
            existing_history = []
    except (json.JSONDecodeError, TypeError):
        existing_history = []

    existing_history.append(history)
    # Keep only last 100 entries to prevent unbounded growth
    state.win_rate_history_json = json.dumps(existing_history[-100:])
    if settings.scanner.adaptive_threshold_enabled:
        total = int(summary.get("total") or 0)
        if total >= max(5, int(settings.scanner.walk_forward_min_samples)):
            target = float(settings.scanner.adaptive_win_rate_target)
            step = float(settings.scanner.adaptive_adjustment_step)
            base = float(settings.scanner.min_score)
            deviation = state.signal_win_rate - target
            adjustment = 0.0
            if deviation > 10.0:
                adjustment = -step * 2
            elif deviation > 5.0:
                adjustment = -step
            elif deviation < -10.0:
                adjustment = step * 2
            elif deviation < -5.0:
                adjustment = step
            state.adaptive_min_score = max(
                float(settings.scanner.adaptive_min_score_floor),
                min(float(settings.scanner.adaptive_min_score_ceiling), base + adjustment),
            )


async def compute_outcome_summary(
    session: AsyncSession,
    *,
    scope: str = "admin",
    days: int | None = None,
    include_recent: bool = True,
) -> dict[str, Any]:
    labels = await _outcome_labels(session, scope=scope, days=days)
    wins = [item for item in labels if _safe_float(item.get("pnl_pct")) > 0]
    losses = [item for item in labels if _safe_float(item.get("pnl_pct")) < 0]
    flats = [item for item in labels if _safe_float(item.get("pnl_pct")) == 0]
    total = len(labels)
    gross_profit = sum(_safe_float(item.get("pnl_pct")) for item in wins)
    gross_loss = abs(sum(_safe_float(item.get("pnl_pct")) for item in losses))
    pnl_values = [_safe_float(item.get("pnl_pct")) for item in labels]
    slippage = [_safe_float(item.get("entry_slippage_pct")) for item in labels if item.get("entry_slippage_pct") is not None]
    mae = [_safe_float(item.get("mae_pct")) for item in labels if item.get("mae_pct") is not None]
    mfe = [_safe_float(item.get("mfe_pct")) for item in labels if item.get("mfe_pct") is not None]

    def breakdown(field: str) -> dict[str, dict[str, Any]]:
        groups: dict[str, list[dict[str, Any]]] = {}
        for item in labels:
            key = str(item.get(field) or "unknown")
            groups.setdefault(key, []).append(item)
        return {
            key: {
                "count": len(items),
                "win_rate": round(sum(1 for item in items if _safe_float(item.get("pnl_pct")) > 0) / max(1, len(items)) * 100.0, 2),
                "expectancy_pct": round(sum(_safe_float(item.get("pnl_pct")) for item in items) / max(1, len(items)), 4),
            }
            for key, items in sorted(groups.items(), key=lambda pair: len(pair[1]), reverse=True)
        }

    summary = {
        "total": total,
        "wins": len(wins),
        "losses": len(losses),
        "flats": len(flats),
        "win_rate": round(len(wins) / max(1, total) * 100.0, 2),
        "expectancy_pct": round(sum(pnl_values) / max(1, total), 4),
        "avg_win_pct": round(gross_profit / max(1, len(wins)), 4),
        "avg_loss_pct": round(-gross_loss / max(1, len(losses)), 4),
        "profit_factor": round(gross_profit / gross_loss, 4) if gross_loss > 0 else 0.0,
        "avg_entry_slippage_pct": round(sum(slippage) / len(slippage), 4) if slippage else None,
        "avg_mae_pct": round(sum(mae) / len(mae), 4) if mae else None,
        "avg_mfe_pct": round(sum(mfe) / len(mfe), 4) if mfe else None,
        "by_symbol": breakdown("symbol"),
        "by_timeframe": breakdown("timeframe"),
        "by_direction": breakdown("direction"),
    }
    if include_recent:
        summary["recent"] = labels[-20:]
    return summary


async def compute_factor_performance(
    session: AsyncSession,
    *,
    scope: str = "admin",
    days: int | None = None,
) -> dict[str, Any]:
    labels = await _outcome_labels(session, scope=scope, days=days)
    stats: dict[str, dict[str, Any]] = {}
    for item in labels:
        pnl = _safe_float(item.get("pnl_pct"))
        breakdown = item.get("score_breakdown") if isinstance(item.get("score_breakdown"), dict) else {}
        for factor, raw_value in breakdown.items():
            value = _safe_float(raw_value)
            row = stats.setdefault(
                factor,
                {"count": 0, "wins": 0, "pnl_sum": 0.0, "contribution_sum": 0.0, "win_contrib": [], "loss_contrib": []},
            )
            row["count"] += 1
            row["wins"] += 1 if pnl > 0 else 0
            row["pnl_sum"] += pnl
            row["contribution_sum"] += value
            if pnl > 0:
                row["win_contrib"].append(value)
            elif pnl < 0:
                row["loss_contrib"].append(value)

    factors: dict[str, dict[str, Any]] = {}
    for factor, row in stats.items():
        count = int(row["count"])
        win_contrib = row["win_contrib"] or []
        loss_contrib = row["loss_contrib"] or []
        win_avg = sum(win_contrib) / len(win_contrib) if win_contrib else 0.0
        loss_avg = sum(loss_contrib) / len(loss_contrib) if loss_contrib else 0.0
        factors[factor] = {
            "count": count,
            "win_rate": round(int(row["wins"]) / max(1, count) * 100.0, 2),
            "expectancy_pct": round(float(row["pnl_sum"]) / max(1, count), 4),
            "avg_contribution": round(float(row["contribution_sum"]) / max(1, count), 4),
            "contribution_edge": round(win_avg - loss_avg, 4),
        }
    return {
        "labels": len(labels),
        "factors": dict(sorted(factors.items(), key=lambda pair: pair[1]["expectancy_pct"], reverse=True)),
    }


def _threshold_candidates() -> list[float]:
    floor = float(settings.scanner.adaptive_min_score_floor)
    ceiling = float(settings.scanner.adaptive_min_score_ceiling)
    step = max(0.5, float(settings.scanner.walk_forward_threshold_step))
    values: list[float] = []
    current = floor
    while current <= ceiling + 1e-9:
        values.append(round(current, 2))
        current += step
    return values or [float(settings.scanner.min_score)]


def _best_threshold(samples: list[dict[str, Any]]) -> dict[str, Any] | None:
    min_samples = max(3, int(settings.scanner.walk_forward_min_samples))
    if len(samples) < min_samples:
        return None
    ordered = sorted(samples, key=lambda item: str(item.get("closed_at") or item.get("created_at") or ""))
    split = max(1, int(len(ordered) * (1.0 - float(settings.scanner.walk_forward_validation_ratio))))
    train = ordered[:split]
    validation = ordered[split:]
    eval_samples = validation if len(validation) >= max(3, min_samples // 3) else train
    min_selected = max(3, min_samples // 3)
    best: dict[str, Any] | None = None
    for threshold in _threshold_candidates():
        subset = [item for item in eval_samples if _safe_float(item.get("score")) >= threshold]
        if len(subset) < min_selected:
            continue
        pnls = [_safe_float(item.get("pnl_pct")) for item in subset]
        wins = sum(1 for pnl in pnls if pnl > 0)
        expectancy = sum(pnls) / len(pnls)
        win_rate = wins / len(pnls) * 100.0
        candidate = {
            "threshold": threshold,
            "expectancy_pct": round(expectancy, 4),
            "win_rate": round(win_rate, 2),
            "selected": len(subset),
            "samples": len(samples),
            "train_samples": len(train),
            "validation_samples": len(validation),
            "validated": eval_samples is validation,
        }
        if best is None:
            best = candidate
            continue
        if (candidate["expectancy_pct"], candidate["win_rate"], candidate["threshold"]) > (
            best["expectancy_pct"], best["win_rate"], best["threshold"]
        ):
            best = candidate
    return best


async def compute_walk_forward_thresholds(
    session: AsyncSession,
    *,
    scope: str = "admin",
    days: int | None = None,
) -> dict[str, Any]:
    labels = [item for item in await _outcome_labels(session, scope=scope, days=days) if _safe_float(item.get("score")) > 0]
    groups: dict[str, list[dict[str, Any]]] = {}
    for item in labels:
        symbol = str(item.get("symbol") or "*").upper().strip() or "*"
        timeframe = str(item.get("timeframe") or "*").lower().strip() or "*"
        direction = str(item.get("direction") or "*").lower().strip() or "*"
        for key in (
            f"{symbol}|{timeframe}|{direction}",
            f"{symbol}|{timeframe}|*",
            f"*|{timeframe}|{direction}",
            "*|*|*",
        ):
            groups.setdefault(key, []).append(item)

    thresholds: dict[str, dict[str, Any]] = {}
    for key, samples in groups.items():
        best = _best_threshold(samples)
        if best:
            thresholds[key] = best
    return {
        "generated_at": utcnow().isoformat(),
        "labels": len(labels),
        "thresholds": thresholds,
    }
