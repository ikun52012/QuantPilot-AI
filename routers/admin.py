"""
Signal Server - Admin Router
Admin panel routes for user management, settings, and monitoring.
"""
import json
import os
import secrets
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Request, Response, HTTPException, Depends, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, update, delete, func

from core.database import (
    get_db, get_all_users, get_user_by_id, update_user_status,
    get_user_by_username, get_user_by_email, update_user_password_hash, set_admin_setting, get_admin_setting,
    AdminAuditLogModel, AdminSettingModel, UserModel, SubscriptionPlanModel, SubscriptionModel, PaymentModel,
    WebhookEventModel, TradeModel, PositionModel, InviteCodeModel, RedeemCodeModel,
    seed_defaults,
)
from core.security import hash_password, validate_password_strength, generate_webhook_secret, webhook_secret_hash, is_placeholder_webhook_secret
from core.auth import require_admin
from core.config import settings
from core.utils.datetime import utcnow


router = APIRouter(prefix="/api/admin", tags=["admin"])


# ─────────────────────────────────────────────
# Request Models
# ─────────────────────────────────────────────

class CreateUserRequest(BaseModel):
    username: str = Field(min_length=3, max_length=32)
    email: str = Field(min_length=5, max_length=254)
    password: str = Field(min_length=8, max_length=256)
    role: str = Field(default="user")
    balance_usdt: float = Field(default=0)
    live_trading_allowed: bool = Field(default=False)
    max_leverage: int = Field(default=20, ge=1, le=125)
    max_position_pct: float = Field(default=10.0, ge=0.1, le=100)


class UpdateUserRequest(BaseModel):
    username: str = Field(min_length=3, max_length=32)
    email: str = Field(min_length=5, max_length=254)
    role: str = Field(default="user")
    is_active: bool = Field(default=True)
    balance_usdt: float = Field(default=0)
    live_trading_allowed: bool = Field(default=False)
    max_leverage: int = Field(default=20, ge=1, le=125)
    max_position_pct: float = Field(default=10.0, ge=0.1, le=100)


class CreatePlanRequest(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    description: str = Field(default="")
    price_usdt: float = Field(ge=0)
    duration_days: int = Field(ge=1)
    features: list[str] = Field(default_factory=list)
    max_signals_per_day: int = Field(default=0)


class CreateInviteCodeRequest(BaseModel):
    code: str = Field(default="", max_length=80)
    max_uses: int = Field(default=1, ge=1)
    note: str = Field(default="")
    expires_days: int = Field(default=30)
    expires_at: str = Field(default="")


class CreateRedeemCodeRequest(BaseModel):
    code: str = Field(default="", max_length=80)
    plan_id: Optional[str] = None
    duration_days: int = Field(default=0)
    balance_usdt: float = Field(default=0)
    note: str = Field(default="")
    expires_days: int = Field(default=30)
    expires_at: str = Field(default="")


class SetPasswordRequest(BaseModel):
    password: str = Field(min_length=8, max_length=256)


class GrantSubscriptionRequest(BaseModel):
    plan_id: str = Field(min_length=1, max_length=80)
    duration_days: int = Field(default=0, ge=0)
    status: str = Field(default="active", max_length=20)


class PaymentAddressRequest(BaseModel):
    network: str = Field(min_length=2, max_length=30)
    address: str = Field(min_length=1, max_length=200)
    currency: str = Field(default="USDT", min_length=2, max_length=12)


class RegistrationSettingsRequest(BaseModel):
    invite_required: bool = False


def _generate_code(prefix: str) -> str:
    token = secrets.token_urlsafe(9).upper().replace("-", "").replace("_", "")
    return f"{prefix}-{token[:12]}"


def _parse_expiry(expires_at: str = "", expires_days: int = 30) -> Optional[datetime]:
    if expires_at:
        try:
            parsed = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed
        except ValueError:
            raise HTTPException(400, "Invalid expires_at date")
    if expires_days:
        return utcnow() + timedelta(days=expires_days)
    return None


def _validate_role(role: str) -> str:
    role = str(role or "user").lower().strip()
    if role not in {"user", "admin"}:
        raise HTTPException(400, "Role must be user or admin")
    return role


def _validate_subscription_status(status: str) -> str:
    status = str(status or "active").lower().strip()
    if status not in {"active", "pending", "cancelled", "expired"}:
        raise HTTPException(400, "Invalid subscription status")
    return status


async def _admin_count(db: AsyncSession) -> int:
    result = await db.execute(
        select(func.count()).select_from(UserModel).where(UserModel.role == "admin")
    )
    return int(result.scalar() or 0)


# ─────────────────────────────────────────────
# User Management
# ─────────────────────────────────────────────

@router.get("/users")
async def list_users(
    admin: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """List all users."""
    users = await get_all_users(db)
    output = []
    for u in users:
        sub_result = await db.execute(
            select(SubscriptionModel, SubscriptionPlanModel)
            .join(SubscriptionPlanModel, SubscriptionModel.plan_id == SubscriptionPlanModel.id, isouter=True)
            .where(
                SubscriptionModel.user_id == u.id,
                SubscriptionModel.status == "active",
                SubscriptionModel.end_date >= utcnow(),
            )
            .order_by(SubscriptionModel.end_date.desc())
            .limit(1)
        )
        sub_row = sub_result.first()
        subscription = None
        if sub_row:
            sub, plan = sub_row
            subscription = {
                "id": sub.id,
                "plan_id": sub.plan_id,
                "plan_name": plan.name if plan else sub.plan_id,
                "status": sub.status,
                "end_date": sub.end_date.isoformat() if sub.end_date else None,
            }

        output.append({
            "id": u.id,
            "username": u.username,
            "email": u.email,
            "role": u.role,
            "balance_usdt": u.balance_usdt or 0,
            "is_active": u.is_active,
            "created_at": u.created_at.isoformat() if u.created_at else None,
            "last_login": u.last_login.isoformat() if u.last_login else None,
            "live_trading_allowed": u.live_trading_allowed,
            "max_leverage": u.max_leverage,
            "max_position_pct": u.max_position_pct,
            "subscription": subscription,
        })
    return output


@router.post("/users")
async def create_user(
    req: CreateUserRequest,
    admin: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
    request: Request = None,
):
    """Create a new user from admin panel."""
    from core.database import create_user as db_create_user, get_user_by_username, get_user_by_email

    # Check for existing
    if await get_user_by_username(db, req.username):
        raise HTTPException(400, "Username already exists")
    if await get_user_by_email(db, req.email):
        raise HTTPException(400, "Email already registered")
    ok, reason = validate_password_strength(req.password, username=req.username, email=req.email)
    if not ok:
        raise HTTPException(400, reason)
    role = _validate_role(req.role)

    # Create user
    pw_hash = hash_password(req.password)
    user = await db_create_user(
        db,
        req.username,
        req.email,
        pw_hash,
        role,
    )

    # Update additional fields
    user.balance_usdt = req.balance_usdt
    user.live_trading_allowed = req.live_trading_allowed
    user.max_leverage = req.max_leverage
    user.max_position_pct = req.max_position_pct

    await db.commit()

    # Audit log
    await _add_audit_log(db, admin, "create_user", "user", user.id, f"Created user {user.username}", request)

    return {"id": user.id, "username": user.username}


@router.put("/users/{user_id}")
@router.put("/user/{user_id}")
async def update_user(
    user_id: str,
    req: UpdateUserRequest,
    admin: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
    request: Request = None,
):
    """Update a user."""
    user = await get_user_by_id(db, user_id)
    if not user:
        raise HTTPException(404, "User not found")

    # Check for conflicts
    if user.username != req.username:
        existing = await get_user_by_username(db, req.username)
        if existing:
            raise HTTPException(400, "Username already exists")

    if user.email != req.email:
        existing = await get_user_by_email(db, req.email)
        if existing:
            raise HTTPException(400, "Email already registered")

    new_role = _validate_role(req.role)
    if user.role == "admin" and new_role != "admin" and await _admin_count(db) <= 1:
        raise HTTPException(400, "Cannot demote the last admin account")
    if user.role == "admin" and not req.is_active and await _admin_count(db) <= 1:
        raise HTTPException(400, "Cannot disable the last admin account")
    if user.id == admin.get("sub") and (new_role != "admin" or not req.is_active):
        raise HTTPException(400, "Use another admin account to change your own admin access")

    old_role = user.role
    old_active = bool(user.is_active)

    # Update fields
    user.username = req.username.lower().strip()
    user.email = req.email.lower().strip()
    user.role = new_role
    user.is_active = req.is_active
    user.balance_usdt = req.balance_usdt
    user.live_trading_allowed = req.live_trading_allowed
    user.max_leverage = req.max_leverage
    user.max_position_pct = req.max_position_pct

    # Bump token version if auth-relevant fields changed.
    if old_role != user.role or old_active != bool(user.is_active):
        user.token_version = (user.token_version or 0) + 1

    await db.commit()

    # Audit log
    await _add_audit_log(db, admin, "update_user", "user", user_id, f"Updated user {user.username}", request)

    return {
        "status": "ok",
        "user": {
            "id": user.id,
            "username": user.username,
            "email": user.email,
            "role": user.role,
            "is_active": user.is_active,
            "balance_usdt": user.balance_usdt,
            "live_trading_allowed": user.live_trading_allowed,
            "max_leverage": user.max_leverage,
            "max_position_pct": user.max_position_pct,
        },
    }


@router.delete("/users/{user_id}")
@router.delete("/user/{user_id}")
async def delete_user(
    user_id: str,
    admin: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
    request: Request = None,
):
    """Delete a user."""
    user = await get_user_by_id(db, user_id)
    if not user:
        raise HTTPException(404, "User not found")
    if user.id == admin.get("sub"):
        raise HTTPException(400, "Use another admin account to delete your own user")

    # Prevent deleting last admin
    if user.role == "admin":
        if await _admin_count(db) <= 1:
            raise HTTPException(400, "Cannot delete the last admin account")

    await db.execute(
        update(RedeemCodeModel)
        .where(RedeemCodeModel.redeemed_by == user_id)
        .values(redeemed_by=None)
    )
    await db.execute(delete(PaymentModel).where(PaymentModel.user_id == user_id))
    await db.execute(delete(SubscriptionModel).where(SubscriptionModel.user_id == user_id))
    await db.execute(delete(TradeModel).where(TradeModel.user_id == user_id))
    await db.execute(delete(PositionModel).where(PositionModel.user_id == user_id))
    await db.execute(delete(UserModel).where(UserModel.id == user_id))
    await db.commit()

    # Audit log
    await _add_audit_log(db, admin, "delete_user", "user", user_id, f"Deleted user {user.username}", request)

    return {"status": "ok"}


@router.post("/user/{user_id}/toggle")
async def toggle_user(
    user_id: str,
    admin: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
    request: Request = None,
):
    """Toggle a user's active state."""
    user = await get_user_by_id(db, user_id)
    if not user:
        raise HTTPException(404, "User not found")
    if user.role == "admin" and (user.id == admin.get("sub") or await _admin_count(db) <= 1):
        raise HTTPException(400, "Admin accounts must be updated explicitly from another admin account")
    user.is_active = not bool(user.is_active)
    user.token_version = (user.token_version or 0) + 1
    await db.commit()
    await _add_audit_log(db, admin, "toggle_user", "user", user_id, f"Set active={user.is_active} for {user.username}", request)
    return {"status": "ok", "is_active": user.is_active}


@router.post("/user/{user_id}/password")
async def set_user_password(
    user_id: str,
    req: SetPasswordRequest,
    admin: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
    request: Request = None,
):
    """Set a user's password from the admin panel."""
    user = await get_user_by_id(db, user_id)
    if not user:
        raise HTTPException(404, "User not found")
    ok, reason = validate_password_strength(req.password, username=user.username, email=user.email)
    if not ok:
        raise HTTPException(400, reason)
    await update_user_password_hash(db, user_id, hash_password(req.password))
    await db.commit()
    await _add_audit_log(db, admin, "set_password", "user", user_id, f"Set password for {user.username}", request)
    return {"status": "ok"}


@router.post("/user/{user_id}/subscription")
async def grant_user_subscription(
    user_id: str,
    req: GrantSubscriptionRequest,
    admin: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
    request: Request = None,
):
    """Grant or create a subscription for a user."""
    user = await get_user_by_id(db, user_id)
    if not user:
        raise HTTPException(404, "User not found")
    plan = await db.get(SubscriptionPlanModel, req.plan_id)
    if not plan:
        raise HTTPException(404, "Plan not found")
    status = _validate_subscription_status(req.status)

    now = utcnow()
    duration_days = req.duration_days or plan.duration_days
    sub = SubscriptionModel(
        user_id=user_id,
        plan_id=plan.id,
        status=status,
        start_date=now if status == "active" else None,
        end_date=(now + timedelta(days=duration_days)) if status == "active" else None,
    )
    db.add(sub)
    await db.commit()
    await _add_audit_log(db, admin, "grant_subscription", "user", user_id, f"Granted {plan.name} to {user.username}", request)
    return {"status": "ok", "subscription_id": sub.id}


@router.post("/users/{user_id}/reset-password")
async def reset_user_password(
    user_id: str,
    admin: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
    request: Request = None,
):
    """Reset a user's password to a random value."""
    user = await get_user_by_id(db, user_id)
    if not user:
        raise HTTPException(404, "User not found")

    # Generate random password
    new_password = secrets.token_urlsafe(12)
    pw_hash = hash_password(new_password)

    await update_user_password_hash(db, user_id, pw_hash)

    # Audit log
    await _add_audit_log(db, admin, "reset_password", "user", user_id, f"Reset password for {user.username}", request)

    return {"new_password": new_password}


# ─────────────────────────────────────────────
# Subscription Plans
# ─────────────────────────────────────────────

@router.get("/plans")
async def list_plans(
    admin: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """List all subscription plans."""
    result = await db.execute(select(SubscriptionPlanModel))
    plans = result.scalars().all()
    return [
        {
            "id": p.id,
            "name": p.name,
            "description": p.description,
            "price_usdt": p.price_usdt,
            "duration_days": p.duration_days,
            "features": json.loads(p.features_json) if p.features_json else [],
            "is_active": p.is_active,
            "max_signals_per_day": p.max_signals_per_day,
        }
        for p in plans
    ]


@router.post("/plans")
async def create_plan(
    req: CreatePlanRequest,
    admin: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Create a subscription plan."""
    plan = SubscriptionPlanModel(
        name=req.name,
        description=req.description,
        price_usdt=req.price_usdt,
        duration_days=req.duration_days,
        features_json=json.dumps(req.features),
        max_signals_per_day=req.max_signals_per_day,
    )
    db.add(plan)
    await db.commit()
    return {"id": plan.id, "name": plan.name}


@router.put("/plans/{plan_id}")
async def update_plan(
    plan_id: str,
    req: CreatePlanRequest,
    admin: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Update a subscription plan."""
    result = await db.execute(
        select(SubscriptionPlanModel).where(SubscriptionPlanModel.id == plan_id)
    )
    plan = result.scalar_one_or_none()
    if not plan:
        raise HTTPException(404, "Plan not found")

    plan.name = req.name
    plan.description = req.description
    plan.price_usdt = req.price_usdt
    plan.duration_days = req.duration_days
    plan.features_json = json.dumps(req.features)
    plan.max_signals_per_day = req.max_signals_per_day

    await db.commit()
    return {"status": "ok"}


@router.delete("/plans/{plan_id}")
async def delete_plan(
    plan_id: str,
    admin: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Delete an unused plan, or deactivate it if historical rows reference it."""
    plan = await db.get(SubscriptionPlanModel, plan_id)
    if not plan:
        raise HTTPException(404, "Plan not found")

    sub_count = await db.scalar(
        select(func.count()).select_from(SubscriptionModel).where(SubscriptionModel.plan_id == plan_id)
    )
    redeem_count = await db.scalar(
        select(func.count()).select_from(RedeemCodeModel).where(RedeemCodeModel.plan_id == plan_id)
    )
    if int(sub_count or 0) or int(redeem_count or 0):
        plan.is_active = False
        result = {"status": "deactivated", "reason": "Plan has historical subscriptions or card codes"}
    else:
        await db.delete(plan)
        result = {"status": "deleted"}
    await db.commit()
    return result


# ─────────────────────────────────────────────
# Settings
# ─────────────────────────────────────────────

@router.get("/settings")
async def get_settings(
    admin: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Get admin settings."""
    result = await db.execute(select(AdminSettingModel))
    settings_list = result.scalars().all()
    return {s.key: s.value for s in settings_list}


@router.put("/settings")
async def update_settings(
    settings_data: dict,
    admin: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
    request: Request = None,
):
    """Update admin settings."""
    for key, value in settings_data.items():
        await set_admin_setting(db, key, str(value))

    await db.commit()

    # Audit log
    await _add_audit_log(db, admin, "update_settings", "settings", "", "Updated admin settings", request)

    return {"status": "ok"}


@router.get("/webhook-config")
async def get_webhook_config(
    admin: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
    request: Request = None,
):
    """Get webhook configuration."""
    secret = await get_admin_setting(db, "webhook_secret", "")

    if is_placeholder_webhook_secret(secret):
        secret = generate_webhook_secret()
        await set_admin_setting(db, "webhook_secret", secret)
        await db.commit()

    # Build template
    base_url = str(request.base_url).rstrip("/") if request else ""
    template = json.dumps({
        "secret": secret,
        "ticker": "{{ticker}}",
        "exchange": "{{exchange}}",
        "direction": "long",
        "price": "{{close}}",
        "timeframe": "{{interval}}",
        "strategy": "{{strategy.order.comment}}",
        "message": "{{strategy.order.action}} {{ticker}} @ {{close}}",
    }, indent=2)

    return {
        "webhook_url": f"{base_url}/webhook",
        "secret": secret,
        "template": template,
    }


# ─────────────────────────────────────────────
# Webhook Events
# ─────────────────────────────────────────────

@router.get("/webhook-events")
async def list_webhook_events(
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    admin: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """List recent webhook events."""
    result = await db.execute(
        select(WebhookEventModel)
        .order_by(WebhookEventModel.created_at.desc())
        .offset(offset)
        .limit(limit)
    )
    events = result.scalars().all()
    return [
        {
            "id": e.id,
            "user_id": e.user_id,
            "ticker": e.ticker,
            "direction": e.direction,
            "status": e.status,
            "status_code": e.status_code,
            "reason": e.reason,
            "client_ip": e.client_ip,
            "created_at": e.created_at.isoformat() if e.created_at else None,
        }
        for e in events
    ]


# ─────────────────────────────────────────────
# Audit Logs
# ─────────────────────────────────────────────

@router.get("/audit-logs")
async def list_audit_logs(
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    admin: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """List admin audit logs."""
    result = await db.execute(
        select(AdminAuditLogModel)
        .order_by(AdminAuditLogModel.created_at.desc())
        .offset(offset)
        .limit(limit)
    )
    logs = result.scalars().all()
    return [
        {
            "id": l.id,
            "admin_id": l.admin_id,
            "admin_username": l.admin_username,
            "action": l.action,
            "target_type": l.target_type,
            "target_id": l.target_id,
            "summary": l.summary,
            "client_ip": l.client_ip,
            "created_at": l.created_at.isoformat() if l.created_at else None,
        }
        for l in logs
    ]


# ─────────────────────────────────────────────
# Invite Codes
# ─────────────────────────────────────────────

@router.get("/invite-codes")
async def list_invite_codes(
    admin: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """List all invite codes."""
    result = await db.execute(select(InviteCodeModel))
    codes = result.scalars().all()
    return [
        {
            "code": c.code,
            "note": c.note,
            "max_uses": c.max_uses,
            "used_count": c.used_count,
            "is_active": c.is_active,
            "expires_at": c.expires_at.isoformat() if c.expires_at else None,
        }
        for c in codes
    ]


@router.post("/invite-codes")
async def create_invite_code(
    req: CreateInviteCodeRequest,
    admin: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Create an invite code."""
    code_value = (req.code or _generate_code("INV")).upper().strip()
    if await db.get(InviteCodeModel, code_value):
        raise HTTPException(400, "Invite code already exists")

    code = InviteCodeModel(
        code=code_value,
        note=req.note,
        max_uses=req.max_uses,
        expires_at=_parse_expiry(req.expires_at, req.expires_days),
        created_by=admin.get("sub"),
    )
    db.add(code)
    await db.commit()
    return {"code": code.code}


@router.get("/registration")
async def get_registration_settings(
    admin: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    invite_required = await get_admin_setting(db, "registration_invite_required", "false")
    return {"invite_required": invite_required.lower() == "true"}


@router.post("/registration")
async def save_registration_settings(
    req: RegistrationSettingsRequest,
    admin: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
    request: Request = None,
):
    await set_admin_setting(db, "registration_invite_required", "true" if req.invite_required else "false")
    await db.commit()
    await _add_audit_log(db, admin, "update_registration", "settings", "", f"Invite required={req.invite_required}", request)
    return {"status": "ok", "invite_required": req.invite_required}


@router.get("/redeem-codes")
async def list_redeem_codes(
    admin: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(RedeemCodeModel, SubscriptionPlanModel, UserModel)
        .join(SubscriptionPlanModel, RedeemCodeModel.plan_id == SubscriptionPlanModel.id, isouter=True)
        .join(UserModel, RedeemCodeModel.redeemed_by == UserModel.id, isouter=True)
        .order_by(RedeemCodeModel.created_at.desc())
    )
    return [
        {
            "code": code.code,
            "plan_id": code.plan_id,
            "plan_name": plan.name if plan else "",
            "duration_days": code.duration_days,
            "balance_usdt": code.balance_usdt,
            "note": code.note,
            "is_active": code.is_active,
            "redeemed_by": code.redeemed_by,
            "redeemed_by_username": redeemed.username if redeemed else "",
            "redeemed_at": code.redeemed_at.isoformat() if code.redeemed_at else None,
            "expires_at": code.expires_at.isoformat() if code.expires_at else None,
        }
        for code, plan, redeemed in result.all()
    ]


@router.post("/redeem-codes")
async def create_redeem_code(
    req: CreateRedeemCodeRequest,
    admin: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    if req.plan_id:
        plan = await db.get(SubscriptionPlanModel, req.plan_id)
        if not plan:
            raise HTTPException(404, "Plan not found")
    if not req.plan_id and req.balance_usdt <= 0:
        raise HTTPException(400, "Choose a plan or balance amount")
    code_value = (req.code or _generate_code("CARD")).upper().strip()
    if await db.get(RedeemCodeModel, code_value):
        raise HTTPException(400, "Redeem code already exists")
    code = RedeemCodeModel(
        code=code_value,
        plan_id=req.plan_id or None,
        duration_days=req.duration_days,
        balance_usdt=req.balance_usdt,
        note=req.note,
        expires_at=_parse_expiry(req.expires_at, req.expires_days),
        created_by=admin.get("sub"),
    )
    db.add(code)
    await db.commit()
    return {"code": code.code}


@router.get("/payment-addresses")
async def list_payment_addresses(
    admin: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    from payment import SUPPORTED_NETWORKS, get_payment_address

    addresses = {}
    for network, info in SUPPORTED_NETWORKS.items():
        address = await get_payment_address(db, info["currency"], network)
        addresses[network] = {"network": network, "currency": info["currency"], "address": address or ""}
    return addresses


@router.post("/payment-addresses")
async def save_payment_address(
    req: PaymentAddressRequest,
    admin: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
    request: Request = None,
):
    from payment import set_payment_address

    network = req.network.upper().strip()
    currency = req.currency.upper().strip()
    await set_payment_address(db, currency, network, req.address.strip())
    await _add_audit_log(db, admin, "save_payment_address", "payment", network, f"Updated {currency}/{network} address", request)
    return {"status": "ok"}


@router.get("/payments")
async def list_admin_payments(
    admin: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(PaymentModel, UserModel)
        .join(UserModel, PaymentModel.user_id == UserModel.id, isouter=True)
        .order_by(PaymentModel.created_at.desc())
    )
    return [
        {
            "id": p.id,
            "user_id": p.user_id,
            "username": u.username if u else "",
            "subscription_id": p.subscription_id,
            "amount": p.amount,
            "currency": p.currency,
            "network": p.network,
            "tx_hash": p.tx_hash,
            "wallet_address": p.wallet_address,
            "status": p.status,
            "created_at": p.created_at.isoformat() if p.created_at else None,
            "confirmed_at": p.confirmed_at.isoformat() if p.confirmed_at else None,
            "expires_at": p.expires_at.isoformat() if p.expires_at else None,
        }
        for p, u in result.all()
    ]


@router.post("/payment/{payment_id}/confirm")
async def confirm_payment(
    payment_id: str,
    admin: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
    request: Request = None,
):
    payment = await db.get(PaymentModel, payment_id)
    if not payment:
        raise HTTPException(404, "Payment not found")
    if payment.status == "confirmed":
        return {"status": "confirmed"}
    if payment.status == "rejected":
        raise HTTPException(400, "Rejected payments cannot be confirmed")
    await _activate_payment_subscription(db, payment)
    await db.commit()
    await _add_audit_log(db, admin, "confirm_payment", "payment", payment_id, f"Confirmed payment {payment_id}", request)
    return {"status": "confirmed"}


@router.post("/payment/{payment_id}/reject")
async def reject_payment(
    payment_id: str,
    admin: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
    request: Request = None,
):
    payment = await db.get(PaymentModel, payment_id)
    if not payment:
        raise HTTPException(404, "Payment not found")
    if payment.status == "confirmed":
        raise HTTPException(400, "Confirmed payments cannot be rejected")
    payment.status = "rejected"
    await db.commit()
    await _add_audit_log(db, admin, "reject_payment", "payment", payment_id, f"Rejected payment {payment_id}", request)
    return {"status": "rejected"}


@router.post("/payment/{payment_id}/verify")
async def verify_payment(
    payment_id: str,
    admin: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
    request: Request = None,
):
    payment = await db.get(PaymentModel, payment_id)
    if not payment:
        raise HTTPException(404, "Payment not found")
    if payment.status == "confirmed":
        return {"status": "confirmed", "verification": {"verified": True, "status": "confirmed"}}
    if payment.status == "rejected":
        raise HTTPException(400, "Rejected payments cannot be verified")
    if not payment.tx_hash:
        raise HTTPException(400, "Payment has no transaction hash")

    from chain_verify import verify_payment_tx

    verification = await verify_payment_tx(
        tx_hash=payment.tx_hash,
        network=payment.network,
        expected_amount=payment.amount,
        expected_address=payment.wallet_address,
    )
    if verification.get("verified"):
        await _activate_payment_subscription(db, payment)
        await db.commit()
        status = "confirmed"
    else:
        status = verification.get("status", "pending")
    await _add_audit_log(db, admin, "verify_payment", "payment", payment_id, f"Verification status={status}", request)
    return {"status": status, "verification": verification}


@router.get("/system")
async def get_system_status(
    request: Request,
    admin: dict = Depends(require_admin),
):
    """Return lightweight system/admin diagnostics for the admin dashboard."""
    data_dir = Path(__file__).resolve().parent.parent / "data"
    logs_dir = Path(__file__).resolve().parent.parent / "logs"
    return {
        "version": settings.app_version,
        "commit": "",
        "webhook_url": f"{str(request.base_url).rstrip('/')}/webhook",
        "live_trading": settings.exchange.live_trading,
        "exchange_sandbox_mode": settings.exchange.sandbox_mode,
        "storage": {
            "data": {"path": str(data_dir), "writable": os_access_writable(data_dir)},
            "logs": {"path": str(logs_dir), "writable": os_access_writable(logs_dir)},
        },
    }


@router.get("/backups")
async def get_backups(
    admin: dict = Depends(require_admin),
):
    from backups import list_backups

    backups = await list_backups()
    return [
        {
            "filename": Path(b.get("file", "")).name,
            "name": b.get("name"),
            "size": int(float(b.get("size_mb") or 0) * 1024 * 1024),
            "created_at": b.get("created_at"),
            "note": b.get("note", ""),
        }
        for b in backups
    ]


@router.post("/backups")
async def create_admin_backup(
    admin: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
    request: Request = None,
):
    from backups import create_backup

    backup = await create_backup()
    await _add_audit_log(db, admin, "create_backup", "backup", backup.get("backup_name", ""), "Created backup", request)
    return {"filename": Path(backup.get("file", "")).name, **backup}


@router.get("/backups/{filename}")
async def download_backup(
    filename: str,
    admin: dict = Depends(require_admin),
):
    from backups import backup_path

    safe_name = Path(filename).name
    target = backup_path / safe_name
    if not target.exists() or target.suffix != ".zip":
        raise HTTPException(404, "Backup not found")
    return FileResponse(target, filename=safe_name)


@router.post("/backups/{filename}/restore")
async def stage_backup_restore(
    filename: str,
    admin: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
    request: Request = None,
):
    from backups import stage_restore

    backup_name = Path(filename).stem
    result = stage_restore(backup_name)
    if result.get("status") == "error":
        raise HTTPException(404, result.get("reason", "Backup not found"))
    await _add_audit_log(db, admin, "stage_restore", "backup", backup_name, "Staged backup restore", request)
    return {"status": "staged", "message": result.get("instructions", ""), **result}


@router.get("/position-monitor")
async def get_position_monitor(
    admin: dict = Depends(require_admin),
):
    from position_monitor import get_monitor_state

    return await get_monitor_state()


@router.post("/position-monitor/run")
async def run_position_monitor(
    admin: dict = Depends(require_admin),
):
    from position_monitor import run_position_monitor_once

    return await run_position_monitor_once({})


# ─────────────────────────────────────────────
# Helper Functions
# ─────────────────────────────────────────────

def os_access_writable(path: Path) -> bool:
    try:
        path.mkdir(parents=True, exist_ok=True)
        return os.access(path, os.W_OK)
    except Exception:
        return False


async def _activate_payment_subscription(db: AsyncSession, payment: PaymentModel) -> None:
    now = utcnow()
    payment.status = "confirmed"
    payment.confirmed_at = now
    if payment.subscription_id:
        subscription = await db.get(SubscriptionModel, payment.subscription_id)
        if subscription:
            plan = await db.get(SubscriptionPlanModel, subscription.plan_id)
            duration_days = plan.duration_days if plan else 30
            subscription.status = "active"
            subscription.start_date = now
            subscription.end_date = now + timedelta(days=duration_days)

async def _add_audit_log(
    db: AsyncSession,
    admin: dict,
    action: str,
    target_type: str,
    target_id: str,
    summary: str,
    request: Optional[Request],
):
    """Add an audit log entry."""
    client_ip = ""
    if request:
        client_ip = (
            request.headers.get("cf-connecting-ip") or
            request.headers.get("x-forwarded-for", "").split(",")[0].strip() or
            (request.client.host if request.client else "")
        )

    log = AdminAuditLogModel(
        admin_id=admin.get("sub"),
        admin_username=admin.get("username", ""),
        action=action,
        target_type=target_type,
        target_id=target_id,
        summary=summary,
        client_ip=client_ip,
    )
    db.add(log)
