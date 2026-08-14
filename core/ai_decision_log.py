"""AI Decision Audit Trail — Round-4 audit P0 fix.

Persists every AI analysis call (prompt + raw response + parsed analysis +
market context + enhanced data) so the system can answer:
  * "What did the AI decide 2 weeks ago for BTCUSDT and what data did it see?"
  * "What was the prompt that led to that reject?"
  * "Which provider/model was used and what did it cost?"

Storage strategy:
  * Primary: PostgreSQL table ``ai_decision_log`` (preferred when DB is up).
  * Fallback: JSONL files under ``logs/ai_decisions/`` (always written, used
    as a safety net and for offline analysis).

The DB schema is intentionally denormalised (prompt_text, response_text,
market_context_json all inlined) to keep queries simple and survive schema
drift in other tables.
"""
from __future__ import annotations

import asyncio
import json
import os
import threading
import uuid
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from loguru import logger

from core.utils.datetime import utcnow

_LOG_DIR = Path(__file__).parent.parent / "logs" / "ai_decisions"
_LOG_DIR.mkdir(parents=True, exist_ok=True)
_LOG_LOCK = threading.RLock()
_LAST_LOG_CLEANUP_DATE = ""


def _retention_days() -> int:
    try:
        return max(1, min(int(os.getenv("AI_DECISION_LOG_RETENTION_DAYS", "90")), 3650))
    except ValueError:
        return 90


def _cleanup_expired_jsonl(now=None) -> int:
    """Delete expired daily AI decision logs once per process day."""
    global _LAST_LOG_CLEANUP_DATE
    now = now or utcnow()
    date_key = now.strftime("%Y-%m-%d")
    if _LAST_LOG_CLEANUP_DATE == date_key:
        return 0

    cutoff = (now - timedelta(days=_retention_days())).date()
    deleted = 0
    with _LOG_LOCK:
        if _LAST_LOG_CLEANUP_DATE == date_key:
            return 0
        for path in _LOG_DIR.glob("ai_decisions_*.jsonl"):
            try:
                file_date = date.fromisoformat(path.stem.removeprefix("ai_decisions_"))
                if file_date < cutoff:
                    path.unlink()
                    deleted += 1
            except (OSError, ValueError):
                logger.warning(f"[AI/DecisionLog] Could not evaluate expired log file {path}")
        _LAST_LOG_CLEANUP_DATE = date_key
    return deleted


async def persist_ai_decision(
    ticker: str,
    direction: str,
    signal_price: float,
    timeframe: str,
    strategy: str,
    system_prompt: str,
    user_prompt: str,
    raw_response: str,
    analysis: Any,
    market_context: Any,
    enhanced_data: dict | None,
    provider: str,
    model_id: str,
    user_id: str = "",
) -> str | None:
    """Persist one AI decision record.

    Returns the decision_id (UUID) on success, or ``None`` if persistence
    failed entirely. Errors are logged but never raised — audit logging is
    best-effort and must not break the trading flow.
    """
    decision_id = str(uuid.uuid4())
    now = utcnow().isoformat()

    # Serialize market context defensively
    def _safe_json(obj: Any) -> str:
        try:
            return json.dumps(obj, default=str, ensure_ascii=False)
        except Exception:
            return "{}"

    market_dict: dict[str, Any] = {}
    try:
        if hasattr(market_context, "model_dump"):
            market_dict = market_context.model_dump(mode="json")
        elif hasattr(market_context, "__dict__"):
            market_dict = {
                k: v for k, v in vars(market_context).items()
                if not k.startswith("_") and _is_jsonable(v)
            }
    except Exception:
        pass

    analysis_dict: dict[str, Any] = {}
    try:
        if hasattr(analysis, "model_dump"):
            analysis_dict = analysis.model_dump(mode="json")
        elif hasattr(analysis, "__dict__"):
            analysis_dict = {k: v for k, v in vars(analysis).items() if _is_jsonable(v)}
    except Exception:
        pass

    record = {
        "decision_id": decision_id,
        "timestamp": now,
        "ticker": ticker,
        "direction": direction,
        "signal_price": signal_price,
        "timeframe": timeframe,
        "strategy": strategy,
        "user_id": user_id,
        "provider": provider,
        "model_id": model_id,
        "system_prompt": (system_prompt or "")[:8000],  # truncate to keep size sane
        "user_prompt": (user_prompt or "")[:20000],
        "raw_response": (raw_response or "")[:20000],
        "analysis_json": _safe_json(analysis_dict),
        "market_context_json": _safe_json(market_dict),
        "enhanced_data_json": _safe_json(enhanced_data or {}),
        "recommendation": analysis_dict.get("recommendation", ""),
        "confidence": analysis_dict.get("confidence", 0),
        "risk_score": analysis_dict.get("risk_score", 0),
    }

    # 1) Always write JSONL safety-net file
    try:
        date_str = utcnow().strftime("%Y-%m-%d")
        file_path = _LOG_DIR / f"ai_decisions_{date_str}.jsonl"
        # Offload the file write to a thread to avoid blocking the event loop
        await asyncio.to_thread(_append_jsonl, file_path, record)
    except Exception as e:
        logger.warning(f"[AI/DecisionLog] JSONL write failed: {e}")

    # 2) Best-effort DB insert
    try:
        await _insert_into_db(record)
    except Exception as e:
        logger.debug(f"[AI/DecisionLog] DB insert failed (JSONL still written): {e}")

    return decision_id


def _append_jsonl(file_path: Path, record: dict) -> None:
    with _LOG_LOCK:
        _cleanup_expired_jsonl()
        with file_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, default=str, ensure_ascii=False) + "\n")


def _is_jsonable(v: Any) -> bool:
    if v is None or isinstance(v, (bool, int, float, str)):
        return True
    try:
        json.dumps(v, default=str)
        return True
    except Exception:
        return False


async def _insert_into_db(record: dict) -> None:
    """Insert into the Alembic-managed ``ai_decision_log`` table."""
    from core.database import AIDecisionLogModel, db_manager

    session_factory = db_manager.async_session_factory
    if session_factory is None:
        raise RuntimeError("Database is not initialized")

    async with session_factory() as session:
        session.add(AIDecisionLogModel(
            decision_id=record["decision_id"],
            timestamp=record["timestamp"],
            ticker=record["ticker"],
            direction=record["direction"],
            signal_price=record["signal_price"],
            timeframe=record["timeframe"],
            strategy=record["strategy"],
            user_id=record["user_id"] or None,
            provider=record["provider"],
            model_id=record["model_id"],
            system_prompt=record["system_prompt"],
            user_prompt=record["user_prompt"],
            raw_response=record["raw_response"],
            analysis_json=record["analysis_json"],
            market_context_json=record["market_context_json"],
            enhanced_data_json=record["enhanced_data_json"],
            recommendation=record["recommendation"],
            confidence=record["confidence"],
            risk_score=record["risk_score"],
            created_at=utcnow(),
        ))
        await session.commit()
