"""Order event recording and conservative reconciliation helpers."""
import hashlib
import json
from datetime import UTC, datetime, timedelta
from typing import Any

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.config import settings
from core.database import OrderEventModel
from core.reconciliation_journal import get_uncommitted_order_intents, update_order_intent
from core.utils.datetime import utcnow

CONFIRMED_STATUSES = {"filled", "closed", "simulated", "confirmed"}
FAILED_STATUSES = {"error", "failed", "rejected", "cancelled", "canceled", "expired"}


async def recover_order_intent_journal(session: AsyncSession) -> dict[str, int]:
    """Link fsynced exchange intents to DB events, surfacing crash gaps safely."""
    linked = 0
    recovered = 0
    for intent in get_uncommitted_order_intents():
        try:
            updated_at = datetime.fromisoformat(str(intent.get("updated_at") or "").replace("Z", "+00:00"))
            if updated_at.tzinfo is None:
                updated_at = updated_at.replace(tzinfo=UTC)
            if datetime.now(UTC) - updated_at < timedelta(seconds=30):
                continue
        except (TypeError, ValueError):
            pass
        client_order_id = str(intent.get("client_order_id") or "")
        if not client_order_id:
            continue
        result = await session.execute(
            select(OrderEventModel).where(
                OrderEventModel.client_order_id == client_order_id
            ).limit(1)
        )
        event = result.scalar_one_or_none()
        if event is not None:
            update_order_intent(str(intent.get("id")), status="db_committed")
            linked += 1
            continue

        # No committed row exists after restart/scheduled reconciliation.  The
        # intent may have crossed the exchange boundary, so create a durable
        # manual-review event and never replay it automatically.
        event = OrderEventModel(
            user_id=str(intent.get("user_id") or "") or None,
            client_order_id=client_order_id,
            exchange_order_id=str((intent.get("result") or {}).get("order_id") or ""),
            ticker=str(intent.get("ticker") or ""),
            direction=str(intent.get("direction") or ""),
            order_type="unknown",
            status="manual_review",
            retry_state="manual_review",
            attempt_count=1,
            last_error="Recovered from fsynced order intent without a committed database event",
            payload_json=json.dumps({"journal_intent": intent}, ensure_ascii=False, default=str),
            created_at=utcnow(),
            updated_at=utcnow(),
        )
        session.add(event)
        await session.flush()
        update_order_intent(str(intent.get("id")), status="db_recovered_manual_review")
        recovered += 1
    return {"linked": linked, "recovered": recovered}


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
    if bool(result.get("rollback_success")):
        return "rolled_back", "not_required", str(
            result.get("reason") or "entry was closed after protection failure"
        )
    if status in FAILED_STATUSES:
        reason = str(result.get("reason") or result.get("error") or "exchange rejected order")
        order_id = _extract_order_id(result, "exchange_order_id", "order_id", "id")
        if (
            bool(result.get("requires_reconciliation"))
            or bool(result.get("rollback_required"))
            or bool(order_id)
        ):
            return "manual_review", "manual_review", reason
        if settings.order_execution.auto_reject_failed_orders:
            return "rejected", "not_required", reason
        if settings.order_execution.auto_approve_failed_orders:
            # Approval is an audit acknowledgement only; it never submits or
            # retries an exchange order.
            return "acknowledged", "not_required", reason
        return "failed", "not_required", reason
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
    )
    if not client_order_id:
        idempotency_key = str(getattr(decision, "idempotency_key", "") or "")
        if idempotency_key:
            digest = hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()[:24]
            client_order_id = f"qp_{digest}"

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
    offset: int = 0,
) -> list[OrderEventModel]:
    """Return recent order events for the admin console."""
    query = select(OrderEventModel).order_by(OrderEventModel.created_at.desc())
    if status:
        query = query.where(OrderEventModel.status == status)
    query = query.offset(max(0, int(offset or 0))).limit(max(1, min(int(limit or 100), 500)))
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
    """Acknowledge a reconciled event without resubmitting an exchange order."""
    result = await session.execute(
        select(OrderEventModel).where(OrderEventModel.id == event_id)
    )
    event = result.scalar_one_or_none()
    if not event:
        return {"success": False, "error": "Order event not found"}
    if event.status != "manual_review":
        return {"success": False, "error": f"Order event status is '{event.status}', must be 'manual_review' to approve"}

    event.status = "acknowledged"
    event.retry_state = "not_required"
    event.next_retry_at = None
    event.updated_at = utcnow()
    if admin_notes:
        payload = json.loads(event.payload_json or "{}")
        payload["admin_notes"] = admin_notes
        event.payload_json = json.dumps(payload, ensure_ascii=False, default=str)

    await session.flush()
    logger.info(f"[OrderReconciler] Order event {event_id} acknowledged by admin")
    return {
        "success": True,
        "event_id": event_id,
        "status": "acknowledged",
        "replayed_order": False,
    }


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
    """Refuse unsafe blind resubmission of an ambiguous live order."""
    result = await session.execute(
        select(OrderEventModel).where(OrderEventModel.id == event_id)
    )
    event = result.scalar_one_or_none()
    if not event:
        return {"success": False, "error": "Order event not found"}
    if admin_notes:
        payload = json.loads(event.payload_json or "{}")
        payload["admin_notes"] = admin_notes
        event.payload_json = json.dumps(payload, ensure_ascii=False, default=str)
    await session.flush()
    logger.warning(
        f"[OrderReconciler] Refused blind retry for order event {event_id}; "
        "exchange reconciliation is required"
    )
    return {
        "success": False,
        "event_id": event_id,
        "status": event.status,
        "error": (
            "Blind order resubmission is disabled. Verify the deterministic "
            "client_order_id on the exchange, then acknowledge this event or "
            "manually requeue the original webhook only after proving no order exists."
        ),
        "replayed_order": False,
    }
