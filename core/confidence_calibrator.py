"""Confidence Calibrator — Round-4 audit P0 fix.

LLM-reported confidence is well-known to be miscalibrated (e.g. GPT-4 saying
0.8 confidence when actual win-rate is 0.55). This module reads historical
AI decisions + their realised outcomes from ``trade_logs`` and produces an
isotonic-regression-style mapping from raw confidence buckets to empirical
hit-rates. The calibrated confidence is what should be used downstream for
the 0.4 reject threshold.

The calibration table is persisted to ``data/ai_calibration.json`` and
refreshed periodically by ``refresh_calibration_table`` (called from a
background task or admin endpoint).

Design choices:
  * Uses 10 raw-confidence buckets (0.0-0.1, 0.1-0.2, ..., 0.9-1.0).
  * For each bucket, computes ``hits / total`` where a "hit" is a trade that
    hit TP1 (or closed with pnl_pct >= +0.5%) and a "miss" is one that hit
    SL (or closed with pnl_pct <= -0.5%); neutral trades are excluded.
  * Isotonic regression: enforces monotonic non-decreasing calibration curve
    (higher raw confidence → higher calibrated confidence).
  * Falls back to linear identity mapping when insufficient data (<30 samples
    in a bucket).
  * Per-(ticker_class, market_regime) tables for finer calibration; aggregated
    ``__default__`` table used as fallback.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

from loguru import logger

from core.config import DATA_DIR

_CALIBRATION_FILE = DATA_DIR / "ai_calibration.json"
_CALIBRATION_LOCK = asyncio.Lock()
_CALIBRATION_CACHE: dict[str, dict[str, float]] = {}  # key -> {bucket_str: calibrated_conf}
_MIN_BUCKET_SAMPLES = 30
_BUCKETS = [round(0.1 * i, 1) for i in range(10)]  # 0.0, 0.1, ..., 0.9


def _bucket(raw_confidence: float) -> str:
    """Return the bucket key for a raw confidence value."""
    b = min(0.9, max(0.0, round(int(raw_confidence * 10) / 10, 1)))
    return f"{b:.1f}"


def _classify_ticker(ticker: str) -> str:
    """Coarse ticker class for per-class calibration."""
    t = (ticker or "").upper()
    if "BTC" in t:
        return "btc"
    if "ETH" in t:
        return "eth"
    if any(x in t for x in ("SOL", "AVAX", "MATIC", "ARB", "OP", "NEAR", "APT", "INJ")):
        return "l1"
    if any(x in t for x in ("DOGE", "SHIB", "PEPE", "WIF", "BONK", "FLOKI")):
        return "meme"
    return "alt"


def _is_hit(outcome: str | None, pnl_pct: float | None) -> bool | None:
    """Return True for hit, False for miss, None for neutral/no-data."""
    if outcome in ("win", "tp_hit"):
        return True
    if outcome in ("loss", "sl_hit"):
        return False
    if pnl_pct is None:
        return None
    if pnl_pct >= 0.5:
        return True
    if pnl_pct <= -0.5:
        return False
    return None


async def refresh_calibration_table(db_session=None) -> dict[str, dict[str, float]]:
    """Recompute the calibration table from historical trade logs.

    Persists the result to ``data/ai_calibration.json`` and updates the
    in-memory cache. Safe to call from a scheduled task.

    Returns the new table.
    """
    async with _CALIBRATION_LOCK:
        try:
            table = await _build_table_from_db(db_session)
        except Exception as e:
            logger.warning(f"[Calibrator] Failed to build table from DB: {e}; trying JSON logs")
            table = _build_table_from_json_logs()
        if not table:
            table = _build_table_from_json_logs()

        try:
            _CALIBRATION_FILE.parent.mkdir(parents=True, exist_ok=True)
            _CALIBRATION_FILE.write_text(json.dumps(table, indent=2), encoding="utf-8")
        except OSError as e:
            logger.warning(f"[Calibrator] Failed to persist calibration table: {e}")

        _CALIBRATION_CACHE.clear()
        _CALIBRATION_CACHE.update(table)
        logger.info(
            f"[Calibrator] Refreshed calibration table: "
            f"{len(table)} classes, {sum(len(v) for v in table.values())} buckets"
        )
        return table


async def _build_table_from_db(db_session) -> dict[str, dict[str, float]]:
    """Aggregate closed positions and their opening trade's AI analysis."""
    table: dict[str, dict[str, list[int]]] = {}
    table_float: dict[str, dict[str, float]] = {}

    try:
        from sqlalchemy import select

        from core.database import PositionModel, TradeModel, db_manager

        if db_session is None:
            session_factory = db_manager.async_session_factory
            if session_factory is None:
                return {}
            db_session = session_factory()
            should_close = True
        else:
            should_close = False

        try:
            result = await db_session.execute(
                select(PositionModel, TradeModel.payload_json)
                .outerjoin(TradeModel, TradeModel.id == PositionModel.open_trade_id)
                .where(PositionModel.status == "closed")
                .order_by(PositionModel.closed_at.desc())
                .limit(5000)
            )
            rows = result.all()
        finally:
            if should_close:
                await db_session.close()
    except Exception as e:
        logger.debug(f"[Calibrator] DB query failed: {e}")
        return table_float

    for row in rows:
        position = row[0]
        try:
            payload = json.loads(row[1] or "{}")
        except (TypeError, json.JSONDecodeError):
            payload = {}
        analysis = payload.get("analysis") if isinstance(payload, dict) else {}
        if not isinstance(analysis, dict):
            analysis = {}
        scanner = payload.get("scanner") if isinstance(payload, dict) else {}
        if not isinstance(scanner, dict):
            scanner = {}

        ticker = str(position.ticker or "")
        pnl_pct = position.pnl_pct
        outcome = position.close_reason
        regime = str(
            analysis.get("market_condition")
            or scanner.get("market_regime")
            or "unknown"
        )
        try:
            raw_conf = float(analysis.get("confidence") or 0)
        except (TypeError, ValueError):
            continue
        if raw_conf > 1:
            raw_conf /= 100.0
        if not 0 < raw_conf <= 1:
            continue
        hit = _is_hit(outcome, pnl_pct)
        if hit is None:
            continue

        ticker_class = _classify_ticker(ticker)
        for key in (f"{ticker_class}:{regime}", ticker_class, "__default__"):
            b = _bucket(raw_conf)
            entry = table.setdefault(key, {}).setdefault(b, [0, 0])
            entry[0] += 1 if hit else 0
            entry[1] += 1

    # Convert to float mapping + isotonic enforce
    for key, buckets in table.items():
        float_buckets: dict[str, float] = {}
        prev = 0.0
        for b in [f"{x:.1f}" for x in _BUCKETS]:
            hits, total = buckets.get(b, [0, 0])
            if total < _MIN_BUCKET_SAMPLES:
                continue
            rate = hits / total
            # Isotonic: never decrease
            rate = max(prev, rate)
            float_buckets[b] = round(rate, 3)
            prev = rate
        if float_buckets:
            table_float[key] = float_buckets

    return table_float


def _build_table_from_json_logs() -> dict[str, dict[str, float]]:
    """Fallback: scan trade_logs JSON files for AI decisions + outcomes."""
    logs_dir = Path(__file__).parent.parent / "trade_logs"
    table: dict[str, dict[str, list[int]]] = {}
    if not logs_dir.exists():
        return {}

    for log_file in logs_dir.glob("*.json"):
        try:
            data = json.loads(log_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if not isinstance(data, dict):
            continue
        # Files can be either single-trade or list-of-trades
        trades = data.get("trades") if "trades" in data else [data]
        for trade in trades:
            if not isinstance(trade, dict):
                continue
            ai = trade.get("ai_analysis") or {}
            raw_conf = ai.get("confidence")
            if raw_conf is None:
                continue
            try:
                raw_conf = float(raw_conf)
            except (TypeError, ValueError):
                continue
            pnl_pct = trade.get("pnl_pct") or trade.get("pnl_percentage")
            outcome = trade.get("outcome")
            hit = _is_hit(outcome, pnl_pct)
            if hit is None:
                continue
            ticker = trade.get("ticker", "")
            regime = trade.get("market_regime", "unknown")
            ticker_class = _classify_ticker(ticker)
            for key in (f"{ticker_class}:{regime}", ticker_class, "__default__"):
                b = _bucket(raw_conf)
                entry = table.setdefault(key, {}).setdefault(b, [0, 0])
                entry[0] += 1 if hit else 0
                entry[1] += 1

    table_float: dict[str, dict[str, float]] = {}
    for key, buckets in table.items():
        float_buckets: dict[str, float] = {}
        prev = 0.0
        for b in [f"{x:.1f}" for x in _BUCKETS]:
            hits, total = buckets.get(b, [0, 0])
            if total < _MIN_BUCKET_SAMPLES:
                continue
            rate = hits / total
            rate = max(prev, rate)
            float_buckets[b] = round(rate, 3)
            prev = rate
        if float_buckets:
            table_float[key] = float_buckets

    return table_float


def _load_cached_table() -> dict[str, dict[str, float]]:
    """Load the calibration table from disk into the cache (once)."""
    if _CALIBRATION_CACHE:
        return _CALIBRATION_CACHE
    try:
        if _CALIBRATION_FILE.exists():
            data = json.loads(_CALIBRATION_FILE.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                legacy_identity = {
                    f"{bucket:.1f}": round(bucket + 0.05, 3)
                    for bucket in _BUCKETS
                }
                cleaned = {
                    key: buckets
                    for key, buckets in data.items()
                    if isinstance(buckets, dict) and buckets != legacy_identity
                }
                _CALIBRATION_CACHE.update(cleaned)
                return _CALIBRATION_CACHE
    except (json.JSONDecodeError, OSError) as e:
        logger.warning(f"[Calibrator] Failed to load calibration file: {e}")
    return _CALIBRATION_CACHE


def calibrate_confidence(
    raw_confidence: float,
    ticker: str = "",
    market_regime: str = "",
) -> float:
    """Return the empirically-calibrated confidence for a raw LLM confidence.

    Falls back to identity (raw_confidence) if no calibration data is
    available. Always returns a value in [0, 1].
    """
    if raw_confidence is None or raw_confidence != raw_confidence:  # NaN check
        return 0.0
    raw_confidence = max(0.0, min(1.0, float(raw_confidence)))
    table = _load_cached_table()

    # Try most-specific key first, then fall back
    ticker_class = _classify_ticker(ticker)
    regime = market_regime or "unknown"
    for key in (f"{ticker_class}:{regime}", ticker_class, "__default__"):
        buckets = table.get(key)
        if buckets:
            b = _bucket(raw_confidence)
            calibrated = buckets.get(b)
            if calibrated is not None:
                return round(calibrated, 3)

    # No calibration data at all — return identity
    return round(raw_confidence, 3)
