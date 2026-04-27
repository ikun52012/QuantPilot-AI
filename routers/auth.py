"""
Signal Server - Authentication Router
User registration, login, session management, and 2FA (TOTP).
"""
from datetime import datetime, timezone

from fastapi import APIRouter, Request, Response, HTTPException, Depends
from pydantic import BaseModel, Field
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from core.database import get_db, create_user, get_user_by_username, get_user_by_email, update_user_login, InviteCodeModel
from core.security import hash_password, verify_password, validate_password_strength
from core.auth import (
    create_token, set_auth_cookie, clear_auth_cookie,
    get_current_user, get_pending_2fa_user,
)
from core.request_utils import client_ip
from core.login_guard import is_locked_out, record_failed_attempt, record_successful_login, remaining_lockout_seconds
from core.utils.datetime import utcnow, make_naive


router = APIRouter(prefix="/api/auth", tags=["auth"])


class RegisterRequest(BaseModel):
    username: str = Field(min_length=3, max_length=32)
    email: str = Field(min_length=5, max_length=254)
    password: str = Field(min_length=8, max_length=256)
    invite_code: str = Field(default="", max_length=80)


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=32)
    password: str = Field(min_length=1, max_length=256)
    totp_code: str = Field(default="", max_length=10)


class ChangePasswordRequest(BaseModel):
    current_password: str = Field(min_length=1)
    new_password: str = Field(min_length=8, max_length=256)


class TotpSetupRequest(BaseModel):
    """Request to begin 2FA setup (no body needed)."""
    pass


class TotpEnableRequest(BaseModel):
    """Confirm 2FA setup with a valid TOTP code."""
    code: str = Field(min_length=6, max_length=6)


class TotpVerifyRequest(BaseModel):
    """Verify 2FA during login."""
    code: str = Field(min_length=1, max_length=20)


class TotpDisableRequest(BaseModel):
    """Disable 2FA with password confirmation."""
    password: str = Field(min_length=1)


def _to_naive_utc(dt):
    """Convert datetime to naive UTC for PostgreSQL compatibility."""
    if dt is None:
        return None
    if dt.tzinfo is not None:
        return dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


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

        if invite.expires_at and _to_naive_utc(invite.expires_at) < utcnow():
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
            invite.last_used_at = utcnow()
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
    """Login to an existing account. If 2FA is enabled, returns a pending token."""
    username = req.username.lower().strip()

    # Brute-force protection
    ip = client_ip(request)
    if is_locked_out(ip):
        secs = remaining_lockout_seconds(ip)
        raise HTTPException(429, f"Too many failed attempts. Try again in {secs} seconds.")

    user = await get_user_by_username(db, username)
    if not user or not verify_password(req.password, user.password_hash):
        remaining = record_failed_attempt(ip)
        if remaining is None:
            raise HTTPException(429, "Too many failed attempts. Account temporarily locked for 15 minutes.")
        raise HTTPException(401, f"Invalid username or password. {remaining} attempts remaining.")

    if not user.is_active:
        raise HTTPException(403, "Account is disabled")

    # Check if 2FA is enabled
    if getattr(user, "totp_enabled", False) and getattr(user, "totp_secret", ""):
        # If TOTP code provided inline, verify it now
        if req.totp_code:
            from core.totp import verify_totp_code, decrypt_totp_secret, verify_recovery_code
            import json

            secret = decrypt_totp_secret(user.totp_secret)
            if verify_totp_code(secret, req.totp_code):
                # 2FA passed — issue full token
                await update_user_login(db, user.id)
                token = create_token(user.id, user.username, user.role, user.token_version or 0)
                set_auth_cookie(response, token, request)
                logger.info(f"[Auth] User logged in with 2FA (inline): {username}")
                record_successful_login(ip)
                return {
                    "token": token,
                    "user": {
                        "id": user.id,
                        "username": user.username,
                        "email": user.email,
                        "role": user.role,
                    }
                }

            # Try recovery code
            recovery_hashes = json.loads(getattr(user, "totp_recovery_codes_json", "[]") or "[]")
            matched_idx = verify_recovery_code(req.totp_code, recovery_hashes)
            if matched_idx is not None:
                recovery_hashes.pop(matched_idx)
                user.totp_recovery_codes_json = json.dumps(recovery_hashes)
                await update_user_login(db, user.id)
                token = create_token(user.id, user.username, user.role, user.token_version or 0)
                set_auth_cookie(response, token, request)
                logger.info(f"[Auth] User logged in with recovery code: {username}")
                record_successful_login(ip)
                return {
                    "token": token,
                    "requires_2fa": False,
                    "recovery_codes_remaining": len(recovery_hashes),
                    "user": {
                        "id": user.id,
                        "username": user.username,
                        "email": user.email,
                        "role": user.role,
                    }
                }

            remaining = record_failed_attempt(ip)
            if remaining is None:
                raise HTTPException(429, "Too many failed attempts. Account temporarily locked for 15 minutes.")
            raise HTTPException(401, f"Invalid 2FA code. {remaining} attempts remaining.")

        # No code provided — issue a short-lived pending token
        pending_token = create_token(
            user.id, user.username, user.role,
            user.token_version or 0, pending_2fa=True,
        )
        set_auth_cookie(response, pending_token, request)
        return {
            "requires_2fa": True,
            "token": pending_token,
        }

    # No 2FA — normal login
    await update_user_login(db, user.id)
    token = create_token(user.id, user.username, user.role, user.token_version or 0)
    set_auth_cookie(response, token, request)

    logger.info(f"[Auth] User logged in: {username}")
    record_successful_login(ip)

    return {
        "token": token,
        "user": {
            "id": user.id,
            "username": user.username,
            "email": user.email,
            "role": user.role,
        }
    }


@router.post("/2fa/verify")
async def verify_2fa(
    req: TotpVerifyRequest,
    request: Request,
    response: Response,
    user: dict = Depends(get_pending_2fa_user),
    db: AsyncSession = Depends(get_db),
):
    """Verify 2FA code during login to get a full access token."""
    import json
    from core.totp import verify_totp_code, decrypt_totp_secret, verify_recovery_code
    from core.database import get_user_by_id

    ip = client_ip(request)
    if is_locked_out(ip):
        secs = remaining_lockout_seconds(ip)
        raise HTTPException(429, f"Too many failed attempts. Try again in {secs} seconds.")

    db_user = await get_user_by_id(db, user["sub"])
    if not db_user:
        raise HTTPException(404, "User not found")

    secret = decrypt_totp_secret(db_user.totp_secret)
    code = req.code.strip()

    # Try TOTP code first
    if verify_totp_code(secret, code):
        await update_user_login(db, db_user.id)
        token = create_token(db_user.id, db_user.username, db_user.role, db_user.token_version or 0)
        set_auth_cookie(response, token, request)
        record_successful_login(ip)
        logger.info(f"[Auth] 2FA verified for user: {db_user.username}")
        return {
            "token": token,
            "user": {
                "id": db_user.id,
                "username": db_user.username,
                "email": db_user.email,
                "role": db_user.role,
            }
        }

    # Try recovery code
    recovery_hashes = json.loads(getattr(db_user, "totp_recovery_codes_json", "[]") or "[]")
    matched_idx = verify_recovery_code(code, recovery_hashes)
    if matched_idx is not None:
        recovery_hashes.pop(matched_idx)
        db_user.totp_recovery_codes_json = json.dumps(recovery_hashes)
        await update_user_login(db, db_user.id)
        token = create_token(db_user.id, db_user.username, db_user.role, db_user.token_version or 0)
        set_auth_cookie(response, token, request)
        record_successful_login(ip)
        logger.info(f"[Auth] 2FA verified via recovery code for user: {db_user.username}")
        return {
            "token": token,
            "recovery_codes_remaining": len(recovery_hashes),
            "user": {
                "id": db_user.id,
                "username": db_user.username,
                "email": db_user.email,
                "role": db_user.role,
            }
        }

    remaining = record_failed_attempt(ip)
    if remaining is None:
        raise HTTPException(429, "Too many failed attempts. Account temporarily locked for 15 minutes.")
    raise HTTPException(401, f"Invalid 2FA code or recovery code. {remaining} attempts remaining.")


@router.post("/2fa/setup")
async def setup_2fa(
    user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Begin 2FA setup — returns QR code and secret. Does NOT enable 2FA yet."""
    from core.database import get_user_by_id
    from core.totp import generate_totp_secret, encrypt_totp_secret, get_totp_uri, generate_qr_code_base64

    db_user = await get_user_by_id(db, user["sub"])
    if not db_user:
        raise HTTPException(404, "User not found")

    if getattr(db_user, "totp_enabled", False):
        raise HTTPException(400, "2FA is already enabled. Disable it first to reconfigure.")

    secret = generate_totp_secret()
    db_user.totp_secret = encrypt_totp_secret(secret)

    uri = get_totp_uri(secret, db_user.username)
    qr_base64 = generate_qr_code_base64(uri)

    logger.info(f"[Auth] 2FA setup initiated for user: {db_user.username}")

    return {
        "secret": secret,
        "uri": uri,
        "qr_code": qr_base64,
        "message": "Scan the QR code with your authenticator app, then call /api/auth/2fa/enable with a valid code.",
    }


@router.post("/2fa/enable")
async def enable_2fa(
    req: TotpEnableRequest,
    user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Confirm 2FA setup by verifying a TOTP code. Generates recovery codes."""
    import json
    from core.database import get_user_by_id
    from core.totp import (
        decrypt_totp_secret, verify_totp_code,
        generate_recovery_codes, hash_recovery_code,
    )

    db_user = await get_user_by_id(db, user["sub"])
    if not db_user:
        raise HTTPException(404, "User not found")

    if getattr(db_user, "totp_enabled", False):
        raise HTTPException(400, "2FA is already enabled")

    secret = decrypt_totp_secret(db_user.totp_secret)
    if not secret or secret.startswith("enc:"):
        raise HTTPException(400, "Call /api/auth/2fa/setup first")

    if not verify_totp_code(secret, req.code):
        raise HTTPException(400, "Invalid TOTP code. Make sure your authenticator is synced.")

    # Generate recovery codes
    recovery_codes = generate_recovery_codes(8)
    hashed_codes = [hash_recovery_code(c) for c in recovery_codes]

    db_user.totp_enabled = True
    db_user.totp_recovery_codes_json = json.dumps(hashed_codes)

    logger.info(f"[Auth] 2FA enabled for user: {db_user.username}")

    return {
        "status": "ok",
        "message": "2FA has been enabled. Save your recovery codes securely.",
        "recovery_codes": recovery_codes,
    }


@router.post("/2fa/disable")
async def disable_2fa(
    req: TotpDisableRequest,
    user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Disable 2FA. Requires password confirmation."""
    from core.database import get_user_by_id

    db_user = await get_user_by_id(db, user["sub"])
    if not db_user:
        raise HTTPException(404, "User not found")

    if not verify_password(req.password, db_user.password_hash):
        raise HTTPException(400, "Incorrect password")

    db_user.totp_enabled = False
    db_user.totp_secret = ""
    db_user.totp_recovery_codes_json = "[]"

    logger.info(f"[Auth] 2FA disabled for user: {db_user.username}")

    return {"status": "ok", "message": "2FA has been disabled."}


@router.get("/2fa/status")
async def get_2fa_status(
    user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Check if 2FA is enabled for the current user."""
    import json
    from core.database import get_user_by_id

    db_user = await get_user_by_id(db, user["sub"])
    if not db_user:
        raise HTTPException(404, "User not found")

    recovery_hashes = json.loads(getattr(db_user, "totp_recovery_codes_json", "[]") or "[]")

    return {
        "enabled": bool(getattr(db_user, "totp_enabled", False)),
        "recovery_codes_remaining": len(recovery_hashes),
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

    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"

    return {
        "id": db_user.id,
        "username": db_user.username,
        "email": db_user.email,
        "role": db_user.role,
        "balance_usdt": db_user.balance_usdt or 0,
        "created_at": db_user.created_at.isoformat() if db_user.created_at else None,
        "totp_enabled": bool(getattr(db_user, "totp_enabled", False)),
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

    if not verify_password(req.current_password, db_user.password_hash):
        raise HTTPException(400, "Current password is incorrect")

    ok, reason = validate_password_strength(
        req.new_password,
        username=db_user.username,
        email=db_user.email,
    )
    if not ok:
        raise HTTPException(400, reason)

    pw_hash = hash_password(req.new_password)
    await update_user_password_hash(db, db_user.id, pw_hash)

    logger.info(f"[Auth] Password changed for user: {db_user.username}")

    return {"status": "ok"}
