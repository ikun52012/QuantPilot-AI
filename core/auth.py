"""
Signal Server - Authentication Module (Enhanced)
JWT-based auth with PBKDF2 password hashing.
"""
import os
import time
import hashlib
import hmac
import json
import base64
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional

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
# JWT Token (Minimal implementation)
# ─────────────────────────────────────────────

def _b64url_encode(data: bytes) -> str:
    """Base64 URL-safe encode without padding."""
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _b64url_decode(s: str) -> bytes:
    """Base64 URL-safe decode with padding restoration."""
    padding = 4 - len(s) % 4
    if padding != 4:
        s += "=" * padding
    return base64.urlsafe_b64decode(s)


def create_token(
    user_id: str,
    username: str,
    role: str = "user",
    token_version: int = 0
) -> str:
    """Create a JWT token."""
    header = {"alg": JWT_ALGORITHM, "typ": "JWT"}
    now = int(time.time())
    payload = {
        "sub": user_id,
        "username": username,
        "role": role,
        "ver": int(token_version or 0),
        "iat": now,
        "exp": now + (settings.jwt_expiry_hours * 3600),
    }

    header_b64 = _b64url_encode(json.dumps(header, separators=(",", ":")).encode())
    payload_b64 = _b64url_encode(json.dumps(payload, separators=(",", ":")).encode())
    message = f"{header_b64}.{payload_b64}"
    signature = hmac.new(JWT_SECRET.encode(), message.encode(), hashlib.sha256).digest()
    sig_b64 = _b64url_encode(signature)

    return f"{message}.{sig_b64}"


def verify_token(token: str) -> Optional[dict]:
    """Verify and decode a JWT token."""
    try:
        parts = token.split(".")
        if len(parts) != 3:
            return None

        header_b64, payload_b64, sig_b64 = parts
        message = f"{header_b64}.{payload_b64}"
        expected_sig = hmac.new(JWT_SECRET.encode(), message.encode(), hashlib.sha256).digest()
        actual_sig = _b64url_decode(sig_b64)

        if not hmac.compare_digest(expected_sig, actual_sig):
            return None

        header = json.loads(_b64url_decode(header_b64))
        if header.get("alg") != JWT_ALGORITHM or header.get("typ") != "JWT":
            return None

        payload = json.loads(_b64url_decode(payload_b64))

        # Check expiry
        if payload.get("exp", 0) < int(time.time()):
            return None
        if not payload.get("sub") or not payload.get("username"):
            return None

        return payload
    except Exception as e:
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
    # Emit both Secure and non-Secure expirations so logout also works when a
    # reverse proxy changes X-Forwarded-Proto or a browser holds an old cookie.
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
    Raises HTTPException if not authenticated.
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
