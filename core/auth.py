"""
Signal Server - Authentication Module (Enhanced)
JWT-based auth with PyJWT, TOTP 2FA support.
"""
import os
import time
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional

import jwt
from fastapi import Request, HTTPException, Depends
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from core.config import settings
from core.security import verify_password
from core.database import get_db, get_user_by_id


# ─────────────────────────────────────────────
# JWT Configuration
# ─────────────────────────────────────────────

JWT_ALGORITHM = "HS256"
AUTH_COOKIE_NAME = "tvss_token"
CSRF_COOKIE_NAME = "tvss_csrf"

# Security scheme for OpenAPI
security = HTTPBearer(auto_error=False)


def _get_jwt_secret() -> str:
    """Get or generate JWT secret."""
    secret = settings.jwt_secret
    if not secret:
        if settings.exchange.live_trading:
            raise RuntimeError("JWT_SECRET must be set when LIVE_TRADING=true")
        secret = secrets.token_urlsafe(48)
        logger.warning("[Auth] JWT_SECRET is not set; generated an ephemeral development secret")
    return secret


JWT_SECRET = _get_jwt_secret()


# ─────────────────────────────────────────────
# JWT Token (PyJWT implementation)
# ─────────────────────────────────────────────

def create_token(
    user_id: str,
    username: str,
    role: str = "user",
    token_version: int = 0,
    pending_2fa: bool = False,
) -> str:
    """Create a JWT token using PyJWT."""
    now = int(time.time())
    payload = {
        "sub": user_id,
        "username": username,
        "role": role,
        "ver": int(token_version or 0),
        "iat": now,
        "exp": now + (settings.jwt_expiry_hours * 3600),
    }
    if pending_2fa:
        payload["2fa_pending"] = True
        # Short-lived token for 2FA verification (5 minutes)
        payload["exp"] = now + 300

    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


def verify_token(token: str) -> Optional[dict]:
    """Verify and decode a JWT token using PyJWT."""
    try:
        payload = jwt.decode(
            token,
            JWT_SECRET,
            algorithms=[JWT_ALGORITHM],
            options={"require": ["sub", "username", "exp", "iat"]},
        )
        if not payload.get("sub") or not payload.get("username"):
            return None
        return payload
    except jwt.ExpiredSignatureError:
        logger.debug("[Auth] Token expired")
        return None
    except jwt.InvalidTokenError as e:
        logger.debug(f"[Auth] Token verification failed: {e}")
        return None


# ─────────────────────────────────────────────
# CSRF Protection
# ─────────────────────────────────────────────

def create_csrf_token() -> str:
    """Generate a CSRF token."""
    return secrets.token_urlsafe(32)


def _request_is_https(request: Optional[Request] = None) -> bool:
    """Check if request is HTTPS."""
    if not request:
        return False
    forwarded_proto = request.headers.get("x-forwarded-proto", "").split(",")[0].strip().lower()
    cf_visitor = request.headers.get("cf-visitor", "").lower()
    return (
        forwarded_proto == "https" or
        request.url.scheme == "https" or
        '"scheme":"https"' in cf_visitor
    )


def _cookie_secure(request: Optional[Request] = None) -> bool:
    """Determine if cookies should be secure."""
    mode = settings.cookie_secure.lower().strip()
    if mode in {"force", "always"}:
        return True
    if mode in {"false", "0", "no", "off"}:
        return False
    if _request_is_https(request):
        return True
    return False


# ─────────────────────────────────────────────
# Cookie Management
# ─────────────────────────────────────────────

def set_auth_cookie(response, token: str, request: Optional[Request] = None):
    """Set authentication cookies."""
    max_age = settings.jwt_expiry_hours * 3600
    csrf_token = create_csrf_token()
    secure = _cookie_secure(request)

    response.set_cookie(
        AUTH_COOKIE_NAME,
        token,
        max_age=max_age,
        httponly=True,
        secure=secure,
        samesite="lax",
        path="/",
    )
    response.set_cookie(
        CSRF_COOKIE_NAME,
        csrf_token,
        max_age=max_age,
        httponly=False,
        secure=secure,
        samesite="lax",
        path="/",
    )


def clear_auth_cookie(response, request: Optional[Request] = None):
    """Clear authentication cookies."""
    for secure in (False, True):
        response.delete_cookie(AUTH_COOKIE_NAME, path="/", secure=secure, samesite="lax")
        response.delete_cookie(CSRF_COOKIE_NAME, path="/", secure=secure, samesite="lax")


# ─────────────────────────────────────────────
# FastAPI Dependencies
# ─────────────────────────────────────────────

async def get_current_user(
    request: Request,
    db: AsyncSession = Depends(get_db),
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
) -> dict:
    """
    FastAPI dependency to extract and verify user from JWT.
    Raises HTTPException if not authenticated or 2FA is pending.
    """
    token = ""

    # Try Authorization header first
    if credentials:
        token = credentials.credentials
    else:
        token = request.cookies.get(AUTH_COOKIE_NAME, "")

    if not token:
        raise HTTPException(status_code=401, detail="Not authenticated")

    payload = verify_token(token)
    if not payload:
        raise HTTPException(status_code=401, detail="Invalid or expired token")

    # Block access if 2FA verification is still pending
    if payload.get("2fa_pending"):
        raise HTTPException(status_code=403, detail="2FA verification required")

    # Verify user still exists and is active
    user = await get_user_by_id(db, payload["sub"])
    if not user:
        raise HTTPException(status_code=401, detail="User no longer exists")
    if not user.is_active:
        raise HTTPException(status_code=403, detail="Account is disabled")
    if int(payload.get("ver", 0)) != int(user.token_version or 0):
        raise HTTPException(status_code=401, detail="Token has been revoked")

    return {
        "sub": user.id,
        "username": user.username,
        "role": user.role,
        "email": user.email,
    }


async def get_pending_2fa_user(
    request: Request,
    db: AsyncSession = Depends(get_db),
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
) -> dict:
    """
    FastAPI dependency for 2FA verification endpoint.
    Accepts tokens with 2fa_pending=True.
    """
    token = ""
    if credentials:
        token = credentials.credentials
    else:
        token = request.cookies.get(AUTH_COOKIE_NAME, "")

    if not token:
        raise HTTPException(status_code=401, detail="Not authenticated")

    payload = verify_token(token)
    if not payload:
        raise HTTPException(status_code=401, detail="Invalid or expired token")

    user = await get_user_by_id(db, payload["sub"])
    if not user:
        raise HTTPException(status_code=401, detail="User no longer exists")
    if not user.is_active:
        raise HTTPException(status_code=403, detail="Account is disabled")

    return {
        "sub": user.id,
        "username": user.username,
        "role": user.role,
        "email": getattr(user, "email", ""),
        "2fa_pending": payload.get("2fa_pending", False),
    }


async def require_admin(
    user: dict = Depends(get_current_user)
) -> dict:
    """FastAPI dependency that requires admin role."""
    if user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")
    return user


async def get_optional_user(
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> Optional[dict]:
    """Returns user payload if authenticated, None otherwise."""
    token = request.cookies.get(AUTH_COOKIE_NAME, "")
    if not token:
        return None

    payload = verify_token(token)
    if not payload:
        return None

    # Don't count 2FA-pending tokens as fully authenticated
    if payload.get("2fa_pending"):
        return None

    try:
        user = await get_user_by_id(db, payload["sub"])
        if not user or not user.is_active:
            return None
        if int(payload.get("ver", 0)) != int(user.token_version or 0):
            return None
        return {
            "sub": user.id,
            "username": user.username,
            "role": user.role,
            "email": user.email,
        }
    except Exception:
        return None
