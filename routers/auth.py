"""
Signal Server - Authentication Router
User registration, login, and session management.
"""
from fastapi import APIRouter, Request, Response, HTTPException, Depends
from pydantic import BaseModel, Field
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from core.database import get_db, create_user, get_user_by_username, get_user_by_email, update_user_login
from core.security import hash_password, verify_password, validate_password_strength
from core.auth import create_token, set_auth_cookie, clear_auth_cookie, get_current_user


router = APIRouter(prefix="/api/auth", tags=["auth"])


# ─────────────────────────────────────────────
# Request Models
# ─────────────────────────────────────────────

class RegisterRequest(BaseModel):
    username: str = Field(min_length=3, max_length=32)
    email: str = Field(min_length=5, max_length=254)
    password: str = Field(min_length=8, max_length=256)
    invite_code: str = Field(default="", max_length=80)


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=32)
    password: str = Field(min_length=1, max_length=256)


class ChangePasswordRequest(BaseModel):
    current_password: str = Field(min_length=1)
    new_password: str = Field(min_length=8, max_length=256)


# ─────────────────────────────────────────────
# Routes
# ─────────────────────────────────────────────

@router.post("/register")
async def register(
    req: RegisterRequest,
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db),
):
    """Register a new user account."""
    from core.database import (
        AdminSettingModel, InviteCodeModel, UserModel,
        set_admin_setting, get_admin_setting,
    )
    from datetime import datetime, timezone

    username = req.username.lower().strip()
    email = req.email.lower().strip()

    ok, reason = validate_password_strength(req.password, username=username, email=email)
    if not ok:
        raise HTTPException(400, reason)

    if "@" not in email or "." not in email.rsplit("@", 1)[-1]:
        raise HTTPException(400, "Invalid email address")

    if await get_user_by_username(db, username):
        raise HTTPException(400, "Username already exists")
    if await get_user_by_email(db, email):
        raise HTTPException(400, "Email already registered")

    invite_required = await get_admin_setting(db, "registration_invite_required", "false")
    invite_code = req.invite_code.strip().upper()

    if invite_required.lower() == "true":
        if not invite_code:
            raise HTTPException(400, "Invite code is required")

        result = await db.execute(
            select(InviteCodeModel).where(
                InviteCodeModel.code == invite_code,
                InviteCodeModel.is_active == True,
            )
        )
        invite = result.scalar_one_or_none()

        if not invite:
            raise HTTPException(400, "Invalid or expired invite code")

        if invite.expires_at and invite.expires_at < datetime.now(timezone.utc):
            raise HTTPException(400, "Invite code has expired")

        if invite.max_uses > 0 and invite.used_count >= invite.max_uses:
            raise HTTPException(400, "Invite code has reached maximum uses")

    pw_hash = hash_password(req.password)
    try:
        user = await create_user(db, username, email, pw_hash)
    except ValueError as e:
        raise HTTPException(400, str(e))

    if invite_required.lower() == "true" and invite_code:
        result = await db.execute(
            select(InviteCodeModel).where(InviteCodeModel.code == invite_code)
        )
        invite = result.scalar_one_or_none()
        if invite:
            invite.used_count += 1
            invite.last_used_by = user.id
            invite.last_used_at = datetime.now(timezone.utc)
            if invite.max_uses > 0 and invite.used_count >= invite.max_uses:
                invite.is_active = False

    token = create_token(user.id, user.username, user.role, user.token_version or 0)
    set_auth_cookie(response, token, request)

    logger.info(f"[Auth] New user registered: {username}")

    return {
        "token": token,
        "user": {
            "id": user.id,
            "username": user.username,
            "email": user.email,
            "role": user.role,
        }
    }


@router.post("/login")
async def login(
    req: LoginRequest,
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db),
):
    """Login to an existing account."""
    username = req.username.lower().strip()

    user = await get_user_by_username(db, username)
    if not user or not verify_password(req.password, user.password_hash):
        raise HTTPException(401, "Invalid username or password")

    if not user.is_active:
        raise HTTPException(403, "Account is disabled")

    # Update last login
    await update_user_login(db, user.id)

    # Create token and set cookie
    token = create_token(user.id, user.username, user.role, user.token_version or 0)
    set_auth_cookie(response, token, request)

    logger.info(f"[Auth] User logged in: {username}")

    return {
        "token": token,
        "user": {
            "id": user.id,
            "username": user.username,
            "email": user.email,
            "role": user.role,
        }
    }


@router.post("/logout")
async def logout(
    request: Request,
    response: Response,
):
    """Logout and clear session."""
    clear_auth_cookie(response, request)
    return {"status": "ok"}


@router.get("/me")
async def get_me(
    response: Response,
    user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Get current user profile."""
    from core.database import SubscriptionPlanModel, get_user_by_id, get_user_active_subscription

    db_user = await get_user_by_id(db, user["sub"])
    if not db_user:
        raise HTTPException(404, "User not found")

    sub = await get_user_active_subscription(db, user["sub"])
    plan = await db.get(SubscriptionPlanModel, sub.plan_id) if sub else None

    # Set no-store headers
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"

    return {
        "id": db_user.id,
        "username": db_user.username,
        "email": db_user.email,
        "role": db_user.role,
        "balance_usdt": db_user.balance_usdt or 0,
        "created_at": db_user.created_at.isoformat() if db_user.created_at else None,
        "subscription": {
            "plan_name": plan.name if plan else None,
            "end_date": sub.end_date.isoformat() if sub else None,
            "status": sub.status if sub else None,
        } if sub else None,
    }


@router.post("/change-password")
async def change_password(
    req: ChangePasswordRequest,
    user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Change user password."""
    from core.database import get_user_by_id, update_user_password_hash

    db_user = await get_user_by_id(db, user["sub"])
    if not db_user:
        raise HTTPException(404, "User not found")

    # Verify current password
    if not verify_password(req.current_password, db_user.password_hash):
        raise HTTPException(400, "Current password is incorrect")

    # Validate new password
    ok, reason = validate_password_strength(
        req.new_password,
        username=db_user.username,
        email=db_user.email,
    )
    if not ok:
        raise HTTPException(400, reason)

    # Update password
    pw_hash = hash_password(req.new_password)
    await update_user_password_hash(db, db_user.id, pw_hash)

    logger.info(f"[Auth] Password changed for user: {db_user.username}")

    return {"status": "ok"}
