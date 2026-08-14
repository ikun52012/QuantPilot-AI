"""Durable webhook delivery worker.

The HTTP endpoint persists an authenticated webhook event before returning
202.  This worker claims persisted events, applies a short processing lease,
and retries transient failures.  A process crash therefore leaves a
recoverable database row instead of losing an in-memory BackgroundTask.
"""
from __future__ import annotations

import asyncio
import json
from datetime import timedelta
from typing import Any

from loguru import logger
from sqlalchemy import and_, func, or_, select, update

from core.config import settings
from core.database import WebhookEventModel, db_manager
from core.redis_coordination import DistributedLockLost, DistributedLockTimeout, distributed_lock
from core.utils.datetime import utcnow
from models import TradingViewSignal
from services.signal_processor import SignalProcessor

WEBHOOK_MAX_ATTEMPTS = 3
WEBHOOK_LEASE_SECONDS = 180
WEBHOOK_RETRY_BASE_SECONDS = 5


async def _renew_processing_lease(event_id: str, stop_event: asyncio.Event) -> None:
    """Keep a long-running database claim from being reclaimed as stale."""
    interval = max(5.0, WEBHOOK_LEASE_SECONDS / 3)
    while not stop_event.is_set():
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval)
            return
        except TimeoutError:
            pass

        try:
            async with db_manager.async_session_factory() as session:
                renewed = await session.execute(
                    update(WebhookEventModel)
                    .where(
                        WebhookEventModel.id == event_id,
                        WebhookEventModel.status == "processing",
                    )
                    .values(updated_at=utcnow())
                )
                if renewed.rowcount != 1:
                    await session.rollback()
                    return
                await session.commit()
        except Exception as exc:
            # A transient heartbeat error is retried. The renewable Redis lock
            # still protects configured multi-instance deployments.
            logger.warning(
                f"[WebhookWorker] Could not renew database lease for {event_id}: {exc}"
            )


def _retryable_result(result: dict[str, Any]) -> bool:
    status = str(result.get("status") or "").lower()
    if status not in {"error", "rejected"}:
        return False

    # Automatic replay is allowed only when the processor explicitly proves
    # that the failure happened before exchange submission.  Error strings are
    # not sufficient evidence: a create-order timeout can mean the exchange
    # accepted the order even though the client never received its response.
    if not bool(result.get("retry_safe")):
        return False
    if str(result.get("failure_stage") or "") != "pre_execution":
        return False
    if result.get("requires_reconciliation") or result.get("rollback_success"):
        return False
    if any(
        result.get(key)
        for key in (
            "order_id",
            "exchange_order_id",
            "client_order_id",
            "order",
            "order_details",
        )
    ):
        return False
    return True


def _requires_manual_reconciliation(result: dict[str, Any]) -> bool:
    if bool(result.get("requires_reconciliation")):
        return True
    if bool(result.get("rollback_required")) and not bool(result.get("rollback_success")):
        return True
    return any(
        result.get(key)
        for key in (
            "order_id",
            "exchange_order_id",
            "client_order_id",
            "order",
            "order_details",
            "order_response",
            "accepted_without_id",
        )
    ) and not bool(result.get("rollback_success"))


async def _claim_event(event_id: str | None = None) -> str | None:
    """Atomically claim one due event and start/renew its processing lease.

    SQLite ignores ``FOR UPDATE``, so a compare-and-update predicate is used
    to prevent the background kick and scheduler from claiming the same row.
    """
    now = utcnow()
    stale_before = now - timedelta(seconds=WEBHOOK_LEASE_SECONDS)
    attempts_remaining = (
        func.coalesce(WebhookEventModel.attempt_count, 0) < WEBHOOK_MAX_ATTEMPTS
    )
    due_filter = or_(
        WebhookEventModel.status == "received",
        and_(
            WebhookEventModel.status == "retrying",
            or_(
                WebhookEventModel.next_attempt_at.is_(None),
                WebhookEventModel.next_attempt_at <= now,
            ),
        ),
        and_(
            WebhookEventModel.status == "processing",
            or_(
                WebhookEventModel.updated_at.is_(None),
                WebhookEventModel.updated_at <= stale_before,
            ),
        ),
    )

    async with db_manager.async_session_factory() as session:
        if event_id:
            candidate_id = event_id
        else:
            candidate_id = (
                await session.execute(
                    select(WebhookEventModel.id)
                    .where(due_filter)
                    .order_by(WebhookEventModel.created_at.asc())
                    .limit(1)
                )
            ).scalar_one_or_none()
        if candidate_id is None:
            return None

        claimed = await session.execute(
            update(WebhookEventModel)
            .where(
                WebhookEventModel.id == candidate_id,
                due_filter,
                attempts_remaining,
            )
            .values(
                status="processing",
                status_code=202,
                reason="processing",
                attempt_count=func.coalesce(WebhookEventModel.attempt_count, 0) + 1,
                next_attempt_at=None,
                updated_at=now,
            )
        )
        if claimed.rowcount != 1:
            exhausted = await session.execute(
                update(WebhookEventModel)
                .where(
                    WebhookEventModel.id == candidate_id,
                    due_filter,
                    ~attempts_remaining,
                )
                .values(
                    status="failed",
                    status_code=503,
                    reason="webhook processing lease expired after maximum attempts",
                    next_attempt_at=None,
                    updated_at=now,
                )
            )
            if exhausted.rowcount == 1:
                await session.commit()
                logger.error(
                    f"[WebhookWorker] Event {candidate_id} exhausted recovery attempts"
                )
                try:
                    from notifier import notify_error

                    await notify_error(
                        f"Webhook event {candidate_id} exhausted recovery attempts"
                    )
                except Exception:
                    pass
            else:
                await session.rollback()
            return None
        await session.commit()
        return str(candidate_id)


async def _next_due_event_id() -> str | None:
    """Read the next candidate without mutating its lease state."""
    now = utcnow()
    stale_before = now - timedelta(seconds=WEBHOOK_LEASE_SECONDS)
    due_filter = or_(
        WebhookEventModel.status == "received",
        and_(
            WebhookEventModel.status == "retrying",
            or_(
                WebhookEventModel.next_attempt_at.is_(None),
                WebhookEventModel.next_attempt_at <= now,
            ),
        ),
        and_(
            WebhookEventModel.status == "processing",
            or_(
                WebhookEventModel.updated_at.is_(None),
                WebhookEventModel.updated_at <= stale_before,
            ),
        ),
    )
    async with db_manager.async_session_factory() as session:
        candidate = (
            await session.execute(
                select(WebhookEventModel.id)
                .where(
                    due_filter,
                    func.coalesce(WebhookEventModel.attempt_count, 0) < WEBHOOK_MAX_ATTEMPTS,
                )
                .order_by(WebhookEventModel.created_at.asc())
                .limit(1)
            )
        ).scalar_one_or_none()
        return str(candidate) if candidate is not None else None


def _schedule_retry(event: WebhookEventModel, reason: str) -> None:
    now = utcnow()
    attempt = int(event.attempt_count or 0)
    event.updated_at = now
    event.status_code = 503
    event.reason = str(reason or "transient webhook processing failure")[:1000]
    if attempt < WEBHOOK_MAX_ATTEMPTS:
        delay = WEBHOOK_RETRY_BASE_SECONDS * (2 ** max(0, attempt - 1))
        event.status = "retrying"
        event.next_attempt_at = now + timedelta(seconds=delay)
    else:
        event.status = "failed"
        event.next_attempt_at = None


async def process_webhook_event(event_id: str | None = None) -> dict[str, Any]:
    """Claim and process one persisted webhook event."""
    candidate_id = event_id or await _next_due_event_id()
    if candidate_id is None:
        return {"status": "idle"}

    try:
        async with distributed_lock(
            f"webhook-event:{candidate_id}",
            ttl_seconds=WEBHOOK_LEASE_SECONDS,
            blocking_timeout_seconds=0.1,
            allow_local_fallback=not settings.redis.enabled,
        ):
            return await _process_webhook_event_under_lease(str(candidate_id))
    except DistributedLockTimeout:
        # Another worker still owns a renewable lease for this exact event.
        # Do not increment attempt_count or reclaim its database row.
        return {"status": "busy", "event_id": str(candidate_id)}
    except DistributedLockLost as exc:
        # The protected operation may have crossed the exchange boundary.  A
        # lease loss is therefore terminal until an operator reconciles it.
        async with db_manager.async_session_factory() as session:
            event = await session.get(WebhookEventModel, str(candidate_id))
            if event is not None:
                event.status = "manual_review"
                event.status_code = 500
                event.reason = str(exc)[:1000]
                event.next_attempt_at = None
                event.updated_at = utcnow()
                await session.commit()
        return {
            "status": "manual_review",
            "event_id": str(candidate_id),
            "reason": str(exc),
        }


async def _process_webhook_event_under_lease(candidate_id: str) -> dict[str, Any]:
    """Process one event while a renewable cross-process lease is held."""
    claimed_id = await _claim_event(candidate_id)
    if claimed_id is None:
        return {"status": "idle", "event_id": candidate_id}

    lease_stop = asyncio.Event()
    lease_heartbeat = asyncio.create_task(
        _renew_processing_lease(claimed_id, lease_stop),
        name=f"webhook-db-lease:{claimed_id}",
    )
    try:
        async with db_manager.async_session_factory() as session:
            event = await session.get(WebhookEventModel, claimed_id)
            if event is None:
                return {"status": "missing", "event_id": claimed_id}
            try:
                payload = json.loads(str(event.payload_json or "{}"))
            except (TypeError, json.JSONDecodeError) as exc:
                event.status = "failed"
                event.status_code = 400
                event.reason = f"Stored webhook payload is invalid: {exc}"
                event.next_attempt_at = None
                event.updated_at = utcnow()
                await session.commit()
                return {"status": "failed", "event_id": claimed_id, "reason": event.reason}

            if not isinstance(payload, dict):
                payload = {}
            event_user_id = event.user_id
            event_client_ip = event.client_ip or "durable_worker"
            event_fingerprint = event.fingerprint
            # Release the read transaction before market, AI, and exchange I/O.
            # SignalProcessor creates short checkpoints for its own DB work.
            await session.rollback()
            # Authentication already succeeded before persistence. Never persist
            # or reconstruct the original secret in the durable queue.
            payload["secret"] = ""
            signal = TradingViewSignal(**payload)
            processor = SignalProcessor(session)
            result = await processor.process_webhook(
                signal=signal,
                user_id=event_user_id,
                client_ip=event_client_ip,
                raw_body=payload,
                reserved_event_id=claimed_id,
                reserved_fingerprint=event_fingerprint,
            )

            event = await session.get(WebhookEventModel, claimed_id)
            if event is not None:
                event.updated_at = utcnow()
                if bool(result.get("rollback_success")):
                    # The entry existed and was deliberately unwound. Preserve
                    # a terminal status that also blocks TradingView redelivery
                    # from reopening the same fingerprint.
                    event.status = "rolled_back"
                    event.status_code = 200
                    event.reason = str(
                        result.get("reason")
                        or result.get("error")
                        or "entry rolled back after execution safety failure"
                    )[:1000]
                    event.next_attempt_at = None
                elif _requires_manual_reconciliation(result):
                    # `error` is intentionally ignored by ordinary webhook
                    # dedupe so pre-execution failures can be redelivered.
                    # Ambiguous exchange results must therefore be promoted to
                    # a non-replayable manual-review status.
                    event.status = "manual_review"
                    event.status_code = 500
                    event.reason = str(
                        result.get("reason")
                        or result.get("error")
                        or "exchange result requires reconciliation"
                    )[:1000]
                    event.next_attempt_at = None
                    try:
                        from notifier import notify_error

                        await notify_error(
                            f"Webhook event {claimed_id} requires exchange reconciliation: "
                            f"{event.reason}"
                        )
                    except Exception:
                        pass
                elif _retryable_result(result):
                    _schedule_retry(event, str(result.get("reason") or "transient processing failure"))
                    if event.status == "failed":
                        try:
                            from notifier import notify_error

                            await notify_error(
                                f"Webhook event {claimed_id} exhausted retries: {event.reason}"
                            )
                        except Exception:
                            pass
                else:
                    if event.status == "processing":
                        event.status = str(result.get("status") or "processed")[:20]
                        event.status_code = 200
                        event.reason = str(result.get("reason") or "")
                    event.next_attempt_at = None
            await session.commit()
            return {"event_id": claimed_id, **result}
    except Exception as exc:
        logger.exception(f"[WebhookWorker] Event {claimed_id} failed: {exc}")
        async with db_manager.async_session_factory() as session:
            event = await session.get(WebhookEventModel, claimed_id)
            if event is not None:
                # An unclassified exception may have happened after the
                # exchange accepted an order (for example while persisting the
                # audit record). Replaying it automatically is unsafe.
                event.status = "manual_review"
                event.status_code = 500
                event.reason = (
                    f"Unclassified processing failure; automatic replay disabled: {exc}"
                )[:1000]
                event.next_attempt_at = None
                event.updated_at = utcnow()
                try:
                    from notifier import notify_error

                    await notify_error(
                        f"Webhook event {claimed_id} requires manual reconciliation: {event.reason}"
                    )
                except Exception:
                    pass
                await session.commit()
                return {
                    "status": event.status,
                    "event_id": claimed_id,
                    "reason": event.reason,
                }
        return {"status": "failed", "event_id": claimed_id, "reason": str(exc)}
    finally:
        lease_stop.set()
        try:
            await lease_heartbeat
        except asyncio.CancelledError:
            pass


async def process_due_webhook_events(limit: int = 10) -> dict[str, Any]:
    """Drain a bounded number of due events for the scheduler."""
    processed: list[dict[str, Any]] = []
    for _ in range(max(1, min(int(limit), 100))):
        result = await process_webhook_event()
        if result.get("status") in {"idle", "busy"}:
            break
        processed.append(result)
    return {
        "processed": len(processed),
        "results": processed,
    }


async def requeue_webhook_event(event_id: str) -> dict[str, Any]:
    """Manually place a terminal webhook event back on the durable queue."""
    async with db_manager.async_session_factory() as session:
        event = await session.get(WebhookEventModel, event_id)
        if event is None:
            return {"status": "missing", "event_id": event_id}
        active = (
            await session.execute(
                select(WebhookEventModel.id)
                .where(
                    WebhookEventModel.fingerprint == event.fingerprint,
                    WebhookEventModel.status.in_(("received", "reserved", "retrying", "processing")),
                    WebhookEventModel.id != event.id,
                )
                .limit(1)
            )
        ).scalar_one_or_none()
        if active:
            return {
                "status": "conflict",
                "event_id": event_id,
                "reason": f"Another active event already exists: {active}",
            }
        event.status = "received"
        event.status_code = 202
        event.reason = "manually requeued"
        event.attempt_count = 0
        event.next_attempt_at = utcnow()
        event.updated_at = utcnow()
        await session.commit()
        return {"status": "queued", "event_id": event_id}
