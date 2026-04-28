"""
Signal Server - Webhook Router
Handles TradingView webhook signals.

Security layers:
1. HMAC signature (optional, for enhanced security)
2. Payload secret (required, TradingView compatible)
"""
import hmac
import json
from fastapi import APIRouter, Request, HTTPException, Depends
from fastapi.responses import JSONResponse
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from core.database import get_admin_setting, get_db
from core.config import settings
from models import TradingViewSignal
from services.signal_processor import SignalProcessor, verify_webhook_signature
from core.security import is_placeholder_webhook_secret
from core.request_utils import client_ip as get_client_ip


router = APIRouter(prefix="", tags=["webhook"])


@router.post("/webhook")
async def webhook(
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """
    Receive and process TradingView webhook signals.

    Supports both admin webhook secret and per-user secrets.
    
    TradingView Compatibility:
    TradingView does NOT support HMAC signature headers.
    It only sends the 'secret' field in JSON payload.
    
    Security:
    - HMAC signature is optional (extra security layer for other integrations)
    - Payload secret is REQUIRED (primary security for TradingView)
    - In live_trading mode, payload secret must be strong (not placeholder)
    """
    # Get raw body
    try:
        raw_body = await request.body()
        body = json.loads(raw_body)
    except json.JSONDecodeError as e:
        logger.error(f"[Webhook] Invalid JSON: {e}")
        raise HTTPException(400, "Invalid JSON payload")

    # Verify HMAC signature if present (optional, extra security)
    signature = (
        request.headers.get("x-tvss-signature", "") or
        request.headers.get("x-signal-signature", "") or
        request.headers.get("x-webhook-signature", "")
    )
    if signature:
        # Signature present - verify it
        if not verify_webhook_signature(raw_body, signature):
            logger.warning("[Webhook] Invalid HMAC signature (signature was present but invalid)")
            raise HTTPException(401, "Invalid HMAC signature")
    else:
        # No signature header - normal for TradingView
        # Allow request, payload secret will be validated below
        logger.debug("[Webhook] No HMAC signature header (TradingView compatibility mode)")

    # Extract secret from payload (REQUIRED for TradingView)
    secret = body.get("secret", "").strip()
    if not secret:
        logger.warning("[Webhook] Missing webhook secret in payload")
        raise HTTPException(401, "Missing webhook secret in payload")

    # Get client IP
    client_ip = get_client_ip(request)

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
        
        # Live trading mode security check
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
            UserModel.deleted_at.is_(None),
        )
    )
    return result.scalar_one_or_none()
