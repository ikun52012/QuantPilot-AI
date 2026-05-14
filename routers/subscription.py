"""
Signal Server - Subscription Router
Subscription and payment management.
"""
import json
from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.auth import get_current_user
from core.database import (
    PaymentModel,
    SubscriptionModel,
    SubscriptionPlanModel,
    deactivate_user_subscriptions,
    get_db,
    get_user_active_subscription,
    lock_user_by_id,
)
from core.utils.datetime import to_utc, utcnow

router = APIRouter(prefix="/api", tags=["subscription"])


# ─────────────────────────────────────────────
# Request Models
# ─────────────────────────────────────────────

class SubscribeRequest(BaseModel):
    plan_id: str = Field(min_length=1, max_length=80)


class PaymentCreateRequest(BaseModel):
    subscription_id: str = Field(min_length=1, max_length=80)
    currency: str = Field(default="USDT", min_length=2, max_length=12)
    network: str = Field(default="TRC20", min_length=2, max_length=20)


class PaymentSubmitRequest(BaseModel):
    payment_id: str = Field(min_length=1, max_length=80)
    tx_hash: str = Field(min_length=6, max_length=200)


class RedeemCodeRequest(BaseModel):
    code: str = Field(min_length=4, max_length=80)


def _as_utc(dt):
    return to_utc(dt) if dt else dt


async def _activate_subscription_row(
    db: AsyncSession,
    subscription: SubscriptionModel,
    *,
    duration_days: int,
    now=None,
):
    """Activate one subscription while cancelling any other active rows for the user."""
    activation_time = now or utcnow()
    subscription.status = "active"
    subscription.start_date = activation_time
    subscription.end_date = activation_time + timedelta(days=duration_days)
    await db.flush()
    await deactivate_user_subscriptions(
        db,
        subscription.user_id,
        exclude_subscription_id=subscription.id,
    )


# ─────────────────────────────────────────────
# Public Routes
# ─────────────────────────────────────────────

@router.get("/plans")
async def list_plans(
    db: AsyncSession = Depends(get_db),
):
    """List active subscription plans (public)."""
    result = await db.execute(
        select(SubscriptionPlanModel).where(SubscriptionPlanModel.is_active)
    )
    plans = result.scalars().all()

    return [
        {
            "id": p.id,
            "name": p.name,
            "description": p.description,
            "price_usdt": p.price_usdt,
            "duration_days": p.duration_days,
            "features": json.loads(p.features_json) if p.features_json else [],
            "max_signals_per_day": p.max_signals_per_day,
        }
        for p in plans
    ]


@router.get("/registration-settings")
async def get_registration_settings(
    db: AsyncSession = Depends(get_db),
):
    """Get registration settings (public)."""
    from core.database import get_admin_setting

    invite_required = await get_admin_setting(db, "registration_invite_required", "false")
    return {"invite_required": invite_required.lower() == "true"}


@router.get("/payment-options")
async def get_payment_options(
    db: AsyncSession = Depends(get_db),
):
    """Return configured payment networks for the checkout modal."""
    from payment import get_payment_address, get_supported_payment_options

    networks = []
    for option in get_supported_payment_options():
        address = await get_payment_address(db, option["currency"], option["network"])
        if not address:
            continue
        networks.append({
            "network": option["network"],
            "name": option["name"],
            "currency": option["currency"],
            "fee": "USDT",
            "confirmation_time": f"{option['confirmations']} confirmations",
        })
    return {"networks": networks}


# ─────────────────────────────────────────────
# Subscription Routes
# ─────────────────────────────────────────────

@router.get("/subscription")
@router.get("/my-subscription")
async def get_subscription(
    request: Request,
    user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Get current user's subscription status."""
    sub = await get_user_active_subscription(db, user["sub"])

    if not sub:
        return None if request.url.path.endswith("/my-subscription") else {"active": False, "subscription": None}

    plan = await db.get(SubscriptionPlanModel, sub.plan_id)
    payload = {
        "id": sub.id,
        "plan_id": sub.plan_id,
        "plan_name": plan.name if plan else None,
        "status": sub.status,
        "start_date": sub.start_date.isoformat() if sub.start_date else None,
        "end_date": sub.end_date.isoformat() if sub.end_date else None,
    }
    if request.url.path.endswith("/my-subscription"):
        return payload
    return {"active": True, "subscription": payload}


@router.post("/subscribe")
async def create_subscription(
    req: SubscribeRequest,
    user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Create a new subscription."""
    # Get plan
    result = await db.execute(
        select(SubscriptionPlanModel).where(SubscriptionPlanModel.id == req.plan_id)
    )
    plan = result.scalar_one_or_none()
    if not plan:
        raise HTTPException(404, "Plan not found")

    if not plan.is_active:
        raise HTTPException(400, "This plan is not available")

    db_user = await lock_user_by_id(db, user["sub"])
    if not db_user:
        raise HTTPException(404, "User not found")

    existing = await get_user_active_subscription(db, user["sub"])
    if existing:
        raise HTTPException(400, "You already have an active subscription")

    now = utcnow()
    activate_now = plan.price_usdt <= 0 or (db_user.balance_usdt or 0) >= plan.price_usdt

    if activate_now and plan.price_usdt > 0:
        db_user.balance_usdt = (db_user.balance_usdt or 0) - plan.price_usdt

    subscription = SubscriptionModel(
        user_id=user["sub"],
        plan_id=plan.id,
        status="pending",
        start_date=None,
        end_date=None,
    )
    db.add(subscription)
    await db.flush()
    if activate_now:
        await _activate_subscription_row(db, subscription, duration_days=plan.duration_days, now=now)
    await db.commit()

    return {
        "id": subscription.id,
        "subscription_id": subscription.id,
        "status": subscription.status,
        "plan_name": plan.name,
        "price_usdt": plan.price_usdt,
        "duration_days": plan.duration_days,
        "paid_from_balance": activate_now and plan.price_usdt > 0,
        "end_date": subscription.end_date.isoformat() if subscription.end_date else None,
    }


@router.get("/subscriptions")
async def list_user_subscriptions(
    user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """List user's subscription history."""
    result = await db.execute(
        select(SubscriptionModel)
        .where(SubscriptionModel.user_id == user["sub"])
        .order_by(SubscriptionModel.created_at.desc())
    )
    subs = result.scalars().all()

    output = []
    for s in subs:
        plan = await db.get(SubscriptionPlanModel, s.plan_id)
        output.append({
            "id": s.id,
            "plan_name": plan.name if plan else None,
            "status": s.status,
            "start_date": s.start_date.isoformat() if s.start_date else None,
            "end_date": s.end_date.isoformat() if s.end_date else None,
            "created_at": s.created_at.isoformat() if s.created_at else None,
        })
    return output


# ─────────────────────────────────────────────
# Payment Routes
# ─────────────────────────────────────────────

@router.post("/payments")
@router.post("/payment/create")
async def create_payment(
    req: PaymentCreateRequest,
    user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Create a payment request."""
    # Get subscription
    result = await db.execute(
        select(SubscriptionModel).where(SubscriptionModel.id == req.subscription_id)
    )
    subscription = result.scalar_one_or_none()

    if not subscription:
        raise HTTPException(404, "Subscription not found")

    if subscription.user_id != user["sub"]:
        raise HTTPException(403, "Not your subscription")

    if subscription.status == "active":
        raise HTTPException(400, "Subscription is already active")

    # Get plan price
    result = await db.execute(
        select(SubscriptionPlanModel).where(SubscriptionPlanModel.id == subscription.plan_id)
    )
    plan = result.scalar_one_or_none()

    if not plan:
        raise HTTPException(404, "Plan not found")

    # Check for existing pending payment
    existing_payment_result = await db.execute(
        select(PaymentModel).where(
            PaymentModel.subscription_id == req.subscription_id,
            PaymentModel.status == "pending"
        )
    )
    if existing_payment_result.scalar_one_or_none():
        raise HTTPException(400, "A pending payment already exists for this subscription")

    # Get payment address
    from payment import get_payment_address

    address = await get_payment_address(db, req.currency, req.network)
    if not address:
        raise HTTPException(400, f"Payment not available for {req.currency}/{req.network}")

    # Create payment
    payment = PaymentModel(
        user_id=user["sub"],
        subscription_id=subscription.id,
        amount=plan.price_usdt,
        currency=req.currency,
        network=req.network,
        wallet_address=address,
        status="pending",
        expires_at=utcnow() + timedelta(hours=24),
    )
    db.add(payment)
    await db.commit()

    network_name = req.network.upper()
    return {
        "id": payment.id,
        "payment_id": payment.id,
        "amount": payment.amount,
        "currency": payment.currency,
        "network": payment.network,
        "network_name": network_name,
        "confirmation_time": "Manual review after confirmations",
        "address": payment.wallet_address,
        "wallet_address": payment.wallet_address,
        "expires_at": payment.expires_at.isoformat() if payment.expires_at else None,
    }


@router.post("/payments/submit")
@router.post("/payment/submit-tx")
async def submit_payment(
    req: PaymentSubmitRequest,
    user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Submit a payment transaction hash."""
    # Get payment
    result = await db.execute(
        select(PaymentModel).where(PaymentModel.id == req.payment_id).with_for_update()
    )
    payment = result.scalar_one_or_none()

    if not payment:
        raise HTTPException(404, "Payment not found")

    if payment.user_id != user["sub"]:
        raise HTTPException(403, "Not your payment")

    if payment.status != "pending":
        raise HTTPException(400, f"Payment is {payment.status}")

    if payment.expires_at and utcnow() > payment.expires_at:
        payment.status = "expired"
        await db.commit()
        raise HTTPException(400, "Payment request has expired. Please create a new subscription.")

    normalized_tx_hash = req.tx_hash.strip()
    # Check for duplicate tx hash
    result = await db.execute(
        select(PaymentModel).where(PaymentModel.tx_hash == normalized_tx_hash, PaymentModel.id != payment.id)
    )
    if result.scalar_one_or_none():
        raise HTTPException(400, "Transaction hash already used")

    # Update payment
    payment.tx_hash = normalized_tx_hash
    payment.status = "submitted"
    await db.commit()

    return {"status": "submitted"}


@router.get("/payments")
@router.get("/my-payments")
async def list_payments(
    user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """List user's payments."""
    result = await db.execute(
        select(PaymentModel)
        .where(PaymentModel.user_id == user["sub"])
        .order_by(PaymentModel.created_at.desc())
    )
    payments = result.scalars().all()

    return [
        {
            "id": p.id,
            "amount": p.amount,
            "currency": p.currency,
            "network": p.network,
            "tx_hash": p.tx_hash,
            "status": p.status,
            "created_at": p.created_at.isoformat() if p.created_at else None,
            "confirmed_at": p.confirmed_at.isoformat() if p.confirmed_at else None,
        }
        for p in payments
    ]


# ─────────────────────────────────────────────
# Redeem Codes
# ─────────────────────────────────────────────

@router.post("/redeem")
@router.post("/redeem-code")
async def redeem_code(
    req: RedeemCodeRequest,
    user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Redeem a code for subscription or balance."""
    from core.database import RedeemCodeModel

    normalized_code = req.code.upper().strip()
    # Find code
    result = await db.execute(
        select(RedeemCodeModel).where(RedeemCodeModel.code == normalized_code).with_for_update()
    )
    code = result.scalar_one_or_none()

    if not code:
        raise HTTPException(404, "Invalid code")

    if not code.is_active:
        raise HTTPException(400, "Code is no longer active")

    if code.redeemed_by:
        raise HTTPException(400, "Code has already been redeemed")

    if code.expires_at and _as_utc(code.expires_at) < utcnow():
        raise HTTPException(400, "Code has expired")

    # Get user
    db_user = await lock_user_by_id(db, user["sub"])
    if not db_user:
        raise HTTPException(404, "User not found")

    # Apply code benefits
    if code.balance_usdt > 0:
        db_user.balance_usdt = (db_user.balance_usdt or 0) + code.balance_usdt

    subscription_payload = None
    if code.plan_id:
        plan = await db.get(SubscriptionPlanModel, code.plan_id)
        if not plan:
            raise HTTPException(404, "Subscription plan not found")
        duration_days = code.duration_days or plan.duration_days
        if duration_days <= 0:
            raise HTTPException(400, "Redeem code has no subscription duration")

        subscription = SubscriptionModel(
            user_id=user["sub"],
            plan_id=code.plan_id,
            status="pending",
            start_date=None,
            end_date=None,
        )
        db.add(subscription)
        await db.flush()
        await _activate_subscription_row(db, subscription, duration_days=duration_days)
        subscription_payload = {
            "plan_id": code.plan_id,
            "plan_name": plan.name if plan else "",
            "duration_days": duration_days,
            "end_date": subscription.end_date.isoformat() if subscription.end_date else None,
        }

    # Mark code as redeemed
    code.redeemed_by = user["sub"]
    code.redeemed_at = utcnow()
    code.is_active = False

    await db.commit()

    return {
        "status": "ok",
        "balance_usdt": code.balance_usdt,
        "balance_added": code.balance_usdt,
        "subscription": subscription_payload,
        "subscription_days": subscription_payload["duration_days"] if subscription_payload else 0,
    }
