"""
QuantPilot AI - Authentication Module
JWT-based auth with PBKDF2 password hashing.

⚠️ DEPRECATED: This file is the legacy authentication module.
Please use core/auth.py instead, which provides:
    - Async-compatible JWT authentication
    - FastAPI dependencies for route protection
    - Password hashing and verification

To import:
    from core.auth import hash_password, verify_password, get_current_user, require_admin

This file is kept for backward compatibility and will be removed in a future version.
"""
import os
import time
import hashlib
import hmac
import json
import base64
import secrets
import re
from datetime import datetime, timedelta
from loguru import logger

# ─────────────────────────────────────────────
# Configuration
# ─────────────────────────────────────────────
_LEGACY_DEFAULT_JWT_SECRET = "tvss-change-this-secret-in-production-2026"
JWT_SECRET = os.getenv("JWT_SECRET", "")
JWT_ALGORITHM = "HS256"
JWT_EXPIRY_HOURS = int(os.getenv("JWT_EXPIRY_HOURS", "24"))

if not JWT_SECRET:
    if os.getenv("LIVE_TRADING", "false").lower() == "true":
        raise RuntimeError("JWT_SECRET must be set when LIVE_TRADING=true")
    JWT_SECRET = secrets.token_urlsafe(48)
    logger.warning("[Auth] JWT_SECRET is not set; generated an ephemeral development secret")
elif JWT_SECRET == _LEGACY_DEFAULT_JWT_SECRET:
    logger.warning("[Auth] JWT_SECRET uses the legacy default value; change it before deployment")


# ─────────────────────────────────────────────
# Password hashing (PBKDF2-SHA256, stdlib only)
# ─────────────────────────────────────────────
_HASH_ITERATIONS = 260_000  # OWASP minimum for PBKDF2-SHA256
_SALT_SIZE = 32
_COMMON_PASSWORDS = {
    "123456",
    "12345678",
    "123456789",
    "password",
    "qwerty123",
    "admin123",
    "admin123456",
    "letmein123",
    "tradingview",
}


def hash_password(password: str) -> str:
    """Hash password using PBKDF2-SHA256."""
    salt = os.urandom(_SALT_SIZE)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, _HASH_ITERATIONS)
    # Store as: iterations$salt_hex$hash_hex
    return f"{_HASH_ITERATIONS}${salt.hex()}${dk.hex()}"


def verify_password(password: str, password_hash: str) -> bool:
    """Verify a password against its PBKDF2-SHA256 hash."""
    try:
        parts = password_hash.split("$")
        if len(parts) != 3:
            return False
        iterations = int(parts[0])
        salt = bytes.fromhex(parts[1])
        stored_hash = bytes.fromhex(parts[2])
        dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)
        return hmac.compare_digest(dk, stored_hash)
    except Exception:
        return False


def validate_password_strength(password: str, username: str = "", email: str = "") -> tuple[bool, str]:
    """Return a user-facing validation result for account passwords."""
    password = password or ""
    lowered = password.lower()
    if len(password) < 8:
        return False, "Password must be at least 8 characters"
    if len(password) > 256:
        return False, "Password is too long"
    if lowered in _COMMON_PASSWORDS:
        return False, "Password is too common"
    if username and username.lower() in lowered:
        return False, "Password cannot contain the username"
    local_email = (email or "").split("@", 1)[0].lower()
    if local_email and local_email in lowered:
        return False, "Password cannot contain the email name"
    checks = [
        (re.search(r"[a-z]", password), "a lowercase letter"),
        (re.search(r"[A-Z]", password), "an uppercase letter"),
        (re.search(r"\d", password), "a number"),
        (re.search(r"[^A-Za-z0-9]", password), "a special character"),
    ]
    missing = [label for ok, label in checks if not ok]
    if missing:
        return False, "Password must include " + ", ".join(missing)
    return True, ""


# ─────────────────────────────────────────────
# JWT token (minimal, no third-party dependency)
# ─────────────────────────────────────────────
def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _b64url_decode(s: str) -> bytes:
    padding = 4 - len(s) % 4
    if padding != 4:
        s += "=" * padding
    return base64.urlsafe_b64decode(s)


def create_token(user_id: str, username: str, role: str = "user", token_version: int = 0) -> str:
    """Create a JWT token."""
    header = {"alg": JWT_ALGORITHM, "typ": "JWT"}
    now = int(time.time())
    payload = {
        "sub": user_id,
        "username": username,
        "role": role,
        "ver": int(token_version or 0),
        "iat": now,
        "exp": now + (JWT_EXPIRY_HOURS * 3600),
    }

    header_b64 = _b64url_encode(json.dumps(header, separators=(",", ":")).encode())
    payload_b64 = _b64url_encode(json.dumps(payload, separators=(",", ":")).encode())
    message = f"{header_b64}.{payload_b64}"
    signature = hmac.new(JWT_SECRET.encode(), message.encode(), hashlib.sha256).digest()
    sig_b64 = _b64url_encode(signature)

    return f"{message}.{sig_b64}"


def verify_token(token: str) -> dict | None:
    """Verify and decode a JWT token. Returns payload dict or None."""
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
# FastAPI dependency
# ─────────────────────────────────────────────
from fastapi import Request, HTTPException

AUTH_COOKIE_NAME = "tvss_token"
CSRF_COOKIE_NAME = "tvss_csrf"


def create_csrf_token() -> str:
    return secrets.token_urlsafe(32)


def _request_is_https(request: Request | None = None) -> bool:
    if not request:
        return False
    forwarded_proto = request.headers.get("x-forwarded-proto", "").split(",")[0].strip().lower()
    cf_visitor = request.headers.get("cf-visitor", "").lower()
    return forwarded_proto == "https" or request.url.scheme == "https" or '"scheme":"https"' in cf_visitor


def _cookie_secure(request: Request | None = None) -> bool:
    mode = os.getenv("COOKIE_SECURE", "auto").lower().strip()
    if mode in {"force", "always"}:
        return True
    if mode in {"false", "0", "no", "off"}:
        return False
    if _request_is_https(request):
        return True
    # In auto/true mode, do not force Secure when this request appears to be HTTP.
    # This prevents login loops on direct-IP, local, or proxy setups missing x-forwarded-proto.
    return False


def set_auth_cookie(response, token: str, request: Request | None = None):
    """Set the browser cookie used by page routes."""
    max_age = JWT_EXPIRY_HOURS * 3600
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


def clear_auth_cookie(response, request: Request | None = None):
    """Clear auth cookie on logout."""
    for secure in (False, True):
        response.delete_cookie(AUTH_COOKIE_NAME, path="/", secure=secure, samesite="lax")
        response.delete_cookie(CSRF_COOKIE_NAME, path="/", secure=secure, samesite="lax")


def get_current_user(request: Request) -> dict:
    """
    FastAPI dependency to extract and verify user from JWT.
    Usage: user = Depends(get_current_user)
    """
    auth_header = request.headers.get("Authorization", "")
    token = ""
    if auth_header.startswith("Bearer "):
        token = auth_header[7:]
    else:
        token = request.cookies.get(AUTH_COOKIE_NAME, "")

    if not token:
        raise HTTPException(status_code=401, detail="Not authenticated")

    payload = verify_token(token)
    if not payload:
        raise HTTPException(status_code=401, detail="Invalid or expired token")

    # Re-check the database so disabled accounts and role changes take effect
    # without waiting for old tokens to expire.
    try:
        from database import get_user_by_id
        db_user = get_user_by_id(payload["sub"])
    except Exception as e:
        logger.debug(f"[Auth] User lookup failed: {e}")
        db_user = None

    if not db_user:
        raise HTTPException(status_code=401, detail="User no longer exists")
    if not db_user.get("is_active", 1):
        raise HTTPException(status_code=403, detail="Account is disabled")
    if int(payload.get("ver", 0)) != int(db_user.get("token_version") or 0):
        raise HTTPException(status_code=401, detail="Token has been revoked")

    payload["username"] = db_user["username"]
    payload["role"] = db_user["role"]
    return payload


def require_admin(request: Request) -> dict:
    """FastAPI dependency that requires admin role."""
    user = get_current_user(request)
    if user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")
    return user


def get_optional_user(request: Request) -> dict | None:
    """Returns user payload if authenticated, None otherwise."""
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        token = auth_header[7:]
    else:
        token = request.cookies.get(AUTH_COOKIE_NAME, "")
    if not token:
        return None
    payload = verify_token(token)
    if not payload:
        return None
    try:
        from database import get_user_by_id
        db_user = get_user_by_id(payload["sub"])
    except Exception:
        return None
    if not db_user or not db_user.get("is_active", 1):
        return None
    if int(payload.get("ver", 0)) != int(db_user.get("token_version") or 0):
        return None
    payload["username"] = db_user["username"]
    payload["role"] = db_user["role"]
    return payload
