"""
Signal Server - Webhook Router
Handles TradingView webhook signals.
"""
import json
from fastapi import APIRouter, Request, HTTPException, Depends
from fastapi.responses import JSONResponse
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from core.database import get_admin_setting, get_db
from core.config import settings
from core.auth import get_optional_user
from models import TradingViewSignal
from services.signal_processor import SignalProcessor, verify_webhook_signature
from core.security import is_placeholder_webhook_secret


router = APIRouter(prefix="", tags=["webhook"])


@router.post("/webhook")
async def webhook(
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """
    Receive and process TradingView webhook signals.

    Supports both admin webhook secret and per-user secrets.
    """
    # Get raw body
    try:
        raw_body = await request.body()
        body = json.loads(raw_body)
    except json.JSONDecodeError as e:
        logger.error(f"[Webhook] Invalid JSON: {e}")
        raise HTTPException(400, "Invalid JSON payload")

    # Verify HMAC signature if configured
    signature = (
        request.headers.get("x-tvss-signature", "") or
        request.headers.get("x-signal-signature", "") or
        request.headers.get("x-webhook-signature", "")
    )
    if not verify_webhook_signature(raw_body, signature):
        logger.warning("[Webhook] Invalid HMAC signature")
        raise HTTPException(401, "Invalid signature")

    # Extract secret
    secret = body.get("secret", "").strip()
    if not secret:
        raise HTTPException(401, "Missing webhook secret")

    # Get client IP
    client_ip = (
        request.headers.get("cf-connecting-ip") or
        request.headers.get("x-forwarded-for", "").split(",")[0].strip() or
        (request.client.host if request.client else "unknown")
    )

    # Validate signal
    try:
        signal = TradingViewSignal(**body)
    except Exception as e:
        logger.error(f"[Webhook] Invalid signal: {e}")
        raise HTTPException(400, f"Invalid signal: {e}")

    # Determine user by secret
    user = await _find_user_by_secret(db, secret)
    user_id = user.id if user else None

    # Verify secret
    if not user_id:
        # Check admin secret
        admin_secret = await get_admin_setting(db, "webhook_secret", settings.server.webhook_secret)
        if is_placeholder_webhook_secret(admin_secret) or secret != admin_secret:
            logger.warning(f"[Webhook] Invalid secret from {client_ip}")
            raise HTTPException(401, "Invalid webhook secret")

    # Process signal
    processor = SignalProcessor(db)
    result = await processor.process_webhook(
        signal=signal,
        user_id=user_id,
        client_ip=client_ip,
        raw_body=body,
    )

    return JSONResponse(content=result)


async def _find_user_by_secret(db: AsyncSession, secret: str):
    """Find user by webhook secret."""
    from core.security import webhook_secret_hash
    from core.database import UserModel
    from sqlalchemy import select

    secret_hash = webhook_secret_hash(secret)
    result = await db.execute(
        select(UserModel).where(
            UserModel.webhook_secret_hash == secret_hash,
            UserModel.is_active == True,
        )
    )
    return result.scalar_one_or_none()
