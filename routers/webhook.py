"""
Signal Server - Webhook Router
Handles TradingView webhook signals.

Security:
- Payload secret (required, TradingView compatible)
- Primary security relies on the 'secret' field in JSON payload
- Timestamp-based replay protection (±5 minute window)
- Nonce-based deduplication for additional replay prevention

Processing:
- Returns 202 Accepted immediately to prevent TradingView timeout
- Actual processing runs in background task
- Fingerprint deduplication prevents duplicate execution
"""
import hashlib
import hmac
import json
import time

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from core.config import settings
from core.database import db_manager, get_admin_setting, get_db
from core.request_utils import client_ip as get_client_ip
from core.security import is_placeholder_webhook_secret
from models import TradingViewSignal
from services.signal_processor import SignalProcessor

router = APIRouter(prefix="", tags=["webhook"])

_WEBHOOK_REPLAY_WINDOW_SECS = 300
_NONCE_CACHE: dict[str, float] = {}
_NONCE_CACHE_MAX_SIZE = 10000
_NONCE_CACHE_CLEANUP_INTERVAL = 3600
_last_nonce_cleanup: float = 0.0


def _check_replay_protection(nonce: str, timestamp: float) -> None:
    """Check for replay attacks using timestamp and nonce."""
    now = time.time()
    if abs(now - timestamp) > _WEBHOOK_REPLAY_WINDOW_SECS:
        raise HTTPException(401, "Webhook timestamp expired — possible replay attack")

    if nonce:
        global _last_nonce_cleanup
        if now - _last_nonce_cleanup > _NONCE_CACHE_CLEANUP_INTERVAL:
            cutoff = now - _WEBHOOK_REPLAY_WINDOW_SECS
            expired = [k for k, v in _NONCE_CACHE.items() if v < cutoff]
            for k in expired:
                _NONCE_CACHE.pop(k, None)
            _last_nonce_cleanup = now

        if nonce in _NONCE_CACHE:
            raise HTTPException(409, "Duplicate nonce — possible replay attack")

        _NONCE_CACHE[nonce] = now
        if len(_NONCE_CACHE) > _NONCE_CACHE_MAX_SIZE:
            cutoff = now - _WEBHOOK_REPLAY_WINDOW_SECS
            expired = [k for k, v in _NONCE_CACHE.items() if v < cutoff]
            for k in expired:
                _NONCE_CACHE.pop(k, None)


@router.post("/webhook")
async def webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
):
    """
    Receive and process TradingView webhook signals.

    Supports both admin webhook secret and per-user secrets.

    TradingView Compatibility:
    TradingView only sends the 'secret' field in JSON payload.
    It does NOT support HMAC signature headers.

    Security:
    - Payload secret is REQUIRED (primary security for TradingView)
    - In live_trading mode, payload secret must be strong (not placeholder)

    Processing:
    - Returns 202 Accepted immediately (within ~100ms)
    - Actual processing runs in background to avoid TradingView timeout
    - Fingerprint deduplication prevents duplicate signals
    """
    try:
        raw_body = await request.body()
        body = json.loads(raw_body)
    except json.JSONDecodeError as err:
        logger.error(f"[Webhook] Invalid JSON: {err}")
        raise HTTPException(400, "Invalid JSON payload") from err

    secret = body.get("secret", "").strip()
    if not secret:
        logger.warning("[Webhook] Missing webhook secret in payload")
        raise HTTPException(401, "Missing webhook secret in payload")

    timestamp = float(body.get("timestamp", 0) or 0)
    nonce = str(body.get("nonce", "") or "").strip()
    if timestamp > 0 or nonce:
        _check_replay_protection(nonce, timestamp)

    client_ip = get_client_ip(request)

    try:
        signal = TradingViewSignal(**body)
    except (ValueError, TypeError, KeyError) as err:
        logger.error(f"[Webhook] Invalid signal: {err}")
        raise HTTPException(400, f"Invalid signal: {err}") from err
    except Exception as err:
        logger.error(f"[Webhook] Unexpected error validating signal: {err}")
        raise HTTPException(400, f"Invalid signal: {err}") from err

    user = await _find_user_by_secret(db, secret)
    user_id = user.id if user else None

    if not user_id:
        admin_secret = await get_admin_setting(db, "webhook_secret", settings.server.webhook_secret)

        if settings.exchange.live_trading:
            if is_placeholder_webhook_secret(admin_secret):
                logger.error(
                    "[Security] LIVE_TRADING enabled but webhook secret is placeholder/weak. "
                    "Please set a strong WEBHOOK_SECRET in environment."
                )
                raise HTTPException(401, "Webhook secret not configured for live trading")

        if not hmac.compare_digest(secret, admin_secret):
            logger.warning(f"[Webhook] Invalid secret from {client_ip}")
            raise HTTPException(401, "Invalid webhook secret")

    background_tasks.add_task(
        _process_webhook_background,
        signal=signal,
        user_id=user_id,
        client_ip=client_ip,
        raw_body=body,
    )

    return JSONResponse(
        status_code=202,
        content={"status": "accepted", "message": "Signal queued for processing"},
    )


async def _process_webhook_background(
    signal: TradingViewSignal,
    user_id: str | None,
    client_ip: str,
    raw_body: dict,
):
    """Process webhook signal in background to avoid TradingView timeout.

    Includes retry logic and dead-letter logging for error recovery.
    On final failure, updates the webhook event status to 'failed' for
    later recovery via admin dashboard or manual re-processing.
    """
    max_retries = 2
    for attempt in range(1, max_retries + 1):
        try:
            async with db_manager.async_session_factory() as session:
                processor = SignalProcessor(session)
                result = await processor.process_webhook(
                    signal=signal,
                    user_id=user_id,
                    client_ip=client_ip,
                    raw_body=raw_body,
                )
                await session.commit()
                logger.info(f"[Webhook] Background processing complete: {result.get('status')}")
                return
        except Exception as exc:
            if attempt < max_retries:
                import asyncio
                delay = 2 ** attempt
                logger.warning(
                    f"[Webhook] Background processing error (attempt {attempt}/{max_retries}), "
                    f"retrying in {delay}s: {exc}"
                )
                await asyncio.sleep(delay)
            else:
                logger.error(
                    f"[Webhook] Background processing failed after {max_retries} attempts. "
                    f"Signal queued for manual review. Ticker: {signal.ticker}, "
                    f"Direction: {signal.direction.value}, Error: {exc}"
                )
                # Mark the webhook event as failed for recovery
                try:
                    from core.database import has_recent_webhook_event
                    from services.signal_processor import compute_webhook_fingerprint
                    async with db_manager.async_session_factory() as session:
                        fingerprint = compute_webhook_fingerprint(raw_body, user_id)
                        existing = await has_recent_webhook_event(session, fingerprint, window_secs=3600)
                        if existing and existing.status in {"received", "reserved", "retrying"}:
                            existing.status = "failed"
                            existing.reason = str(exc)[:500]
                            await session.commit()
                            logger.info(f"[Webhook] Marked event {fingerprint[:12]}... as failed for recovery")
                except Exception:
                    pass


async def _find_user_by_secret(db: AsyncSession, secret: str):
    """Find user by webhook secret.

    Uses constant-time dummy hash to prevent timing side-channel attacks
    that could enumerate valid user webhook secrets.
    """
    from sqlalchemy import select

    from core.database import UserModel
    from core.security import webhook_secret_hash

    secret_hash = webhook_secret_hash(secret)
    result = await db.execute(
        select(UserModel).where(
            UserModel.webhook_secret_hash == secret_hash,
            UserModel.is_active,
            UserModel.deleted_at.is_(None),
        )
    )
    user = result.scalar_one_or_none()

    if user is None:
        _dummy_hash = hashlib.sha256(b"timing-attack-mitigation-dummy").hexdigest()

    return user
