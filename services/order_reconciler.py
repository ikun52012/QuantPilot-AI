"""Order event recording and conservative reconciliation helpers."""
import json
import uuid
from datetime import timedelta
from typing import Any

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.database import OrderEventModel
from core.utils.datetime import utcnow

CONFIRMED_STATUSES = {"filled", "closed", "simulated", "confirmed"}
FAILED_STATUSES = {"error", "failed", "rejected", "cancelled", "canceled", "expired"}


def _safe_dump(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump()
    if isinstance(value, dict):
        return {k: _safe_dump(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_safe_dump(item) for item in value]
    return value


def _extract_order_id(result: dict, *names: str) -> str:
    for name in names:
        value = result.get(name)
        if value:
            return str(value)

    nested = result.get("order") or result.get("order_details") or {}
    if isinstance(nested, dict):
        for name in names:
            value = nested.get(name)
            if value:
                return str(value)
    return ""


def _event_status(result: dict) -> tuple[str, str, str]:
    status = str(result.get("status") or "").strip().lower()
    if status in {"simulated", "paper"}:
        return "simulated", "not_required", ""
    if status in CONFIRMED_STATUSES:
        return "confirmed", "not_required", ""
    if status in FAILED_STATUSES:
        reason = str(result.get("reason") or result.get("error") or "exchange rejected order")
        return "retryable", "pending", reason
    if not status:
        return "manual_review", "manual_review", "missing exchange status"
    return status, "not_required", ""


async def record_order_event(
    session: AsyncSession,
    decision,
    result: dict,
    user_id: str | None = None,
    trade_id: str | None = None,
    position_id: str | None = None,
) -> OrderEventModel:
    """Record one order execution attempt for audit and reconciliation."""
    result = dict(result or {})
    status, retry_state, last_error = _event_status(result)

    direction = getattr(getattr(decision, "direction", None), "value", None) or getattr(decision, "direction", "")
    signal = getattr(decision, "signal", None)
    client_order_id = (
        _extract_order_id(result, "client_order_id", "clientOrderId", "client_oid")
        or f"qp_{uuid.uuid4().hex[:18]}"
    )

    event = OrderEventModel(
        user_id=user_id,
        position_id=position_id,
        trade_id=trade_id,
        client_order_id=client_order_id,
        exchange_order_id=_extract_order_id(result, "exchange_order_id", "order_id", "id"),
        ticker=str(getattr(decision, "ticker", "") or ""),
        direction=str(direction or ""),
        order_type=str(result.get("order_type") or result.get("type") or "market"),
        status=status,
        retry_state=retry_state,
        attempt_count=1,
        last_error=last_error,
        next_retry_at=utcnow() + timedelta(minutes=1) if retry_state == "pending" else None,
        payload_json=json.dumps({
            "decision": _safe_dump(decision),
            "signal": _safe_dump(signal),
            "result": _safe_dump(result),
        }, ensure_ascii=False, default=str),
    )
    session.add(event)
    await session.flush()
    return event


async def list_order_events(
    session: AsyncSession,
    status: str | None = None,
    limit: int = 100,
) -> list[OrderEventModel]:
    """Return recent order events for the admin console."""
    query = select(OrderEventModel).order_by(OrderEventModel.created_at.desc())
    if status:
        query = query.where(OrderEventModel.status == status)
    query = query.limit(max(1, min(int(limit or 100), 500)))
    result = await session.execute(query)
    return list(result.scalars().all())


async def run_order_reconciliation(session: AsyncSession) -> dict:
    """
    Mark retryable events that need operator review.

    This intentionally does not place duplicate exchange orders. Retrying live
    orders requires idempotent exchange-specific order lookup, so this service
    promotes stale retryable rows into manual review until that connector exists.
    """
    now = utcnow()
    result = await session.execute(
        select(OrderEventModel)
        .where(
            OrderEventModel.retry_state == "pending",
            OrderEventModel.next_retry_at.is_not(None),
            OrderEventModel.next_retry_at <= now,
        )
        .limit(200)
    )
    events = list(result.scalars().all())

    for event in events:
        event.retry_state = "manual_review"
        event.status = "manual_review"
        event.updated_at = now
        if not event.last_error:
            event.last_error = "retry window reached; manual reconciliation required"

    await session.flush()
    # BUG FIX: Notify admin when orders are promoted to manual review
    if events:
        try:
            from notifier import notify_error
            await notify_error(
                f"[OrderReconciler] {len(events)} order(s) promoted to manual_review. "
                f"Check admin console for details."
            )
        except Exception as notify_err:
            logger.warning(f"[OrderReconciler] Failed to send admin notification: {notify_err}")
    return {
        "checked": len(events),
        "manual_review": len(events),
        "replayed_orders": 0,
        "note": "No duplicate orders were submitted during reconciliation.",
    }


async def approve_order_event(session: AsyncSession, event_id: str, admin_notes: str = "") -> dict:
    """Approve a manual review order event and mark it for re-execution."""
    result = await session.execute(
        select(OrderEventModel).where(OrderEventModel.id == event_id)
    )
    event = result.scalar_one_or_none()
    if not event:
        return {"success": False, "error": "Order event not found"}
    if event.status != "manual_review":
        return {"success": False, "error": f"Order event status is '{event.status}', must be 'manual_review' to approve"}

    event.status = "approved"
    event.retry_state = "approved"
    event.updated_at = utcnow()
    if admin_notes:
        payload = json.loads(event.payload_json or "{}")
        payload["admin_notes"] = admin_notes
        event.payload_json = json.dumps(payload, ensure_ascii=False, default=str)

    await session.flush()
    logger.info(f"[OrderReconciler] Order event {event_id} approved by admin")
    return {"success": True, "event_id": event_id, "status": "approved"}


async def reject_order_event(session: AsyncSession, event_id: str, admin_notes: str = "") -> dict:
    """Reject a manual review order event permanently."""
    result = await session.execute(
        select(OrderEventModel).where(OrderEventModel.id == event_id)
    )
    event = result.scalar_one_or_none()
    if not event:
        return {"success": False, "error": "Order event not found"}
    if event.status != "manual_review":
        return {"success": False, "error": f"Order event status is '{event.status}', must be 'manual_review' to reject"}

    event.status = "rejected"
    event.retry_state = "not_required"
    event.updated_at = utcnow()
    if admin_notes:
        payload = json.loads(event.payload_json or "{}")
        payload["admin_notes"] = admin_notes
        event.payload_json = json.dumps(payload, ensure_ascii=False, default=str)

    await session.flush()
    logger.info(f"[OrderReconciler] Order event {event_id} rejected by admin")
    return {"success": True, "event_id": event_id, "status": "rejected"}


async def retry_order_event(session: AsyncSession, event_id: str, admin_notes: str = "") -> dict:
    """Retry a manual review order event by resetting retry state."""
    result = await session.execute(
        select(OrderEventModel).where(OrderEventModel.id == event_id)
    )
    event = result.scalar_one_or_none()
    if not event:
        return {"success": False, "error": "Order event not found"}
    if event.status not in ("manual_review", "retryable", "failed", "error", "rejected"):
        return {"success": False, "error": f"Order event status is '{event.status}', cannot retry"}

    event.status = "retryable"
    event.retry_state = "pending"
    event.attempt_count = 0
    event.next_retry_at = utcnow()
    event.updated_at = utcnow()
    if admin_notes:
        payload = json.loads(event.payload_json or "{}")
        payload["admin_notes"] = admin_notes
        event.payload_json = json.dumps(payload, ensure_ascii=False, default=str)

    await session.flush()
    logger.info(f"[OrderReconciler] Order event {event_id} queued for retry by admin")
    return {"success": True, "event_id": event_id, "status": "retryable"}
