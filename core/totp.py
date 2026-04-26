"""
QuantPilot AI - TOTP Two-Factor Authentication
Provides TOTP setup, verification, and recovery code management.
"""
import io
import secrets
import hashlib
import hmac
from typing import Optional

import pyotp
import qrcode
import qrcode.constants
from loguru import logger

from core.config import settings
from core.security import encrypt_value, decrypt_value


# ─────────────────────────────────────────────
# TOTP Secret Management
# ─────────────────────────────────────────────

def generate_totp_secret() -> str:
    """Generate a new TOTP secret key."""
    return pyotp.random_base32(length=32)


def encrypt_totp_secret(secret: str) -> str:
    """Encrypt TOTP secret for database storage."""
    return encrypt_value(secret)


def decrypt_totp_secret(encrypted: str) -> str:
    """Decrypt TOTP secret from database."""
    return decrypt_value(encrypted)


def get_totp_uri(secret: str, username: str) -> str:
    """Generate otpauth:// URI for authenticator apps."""
    totp = pyotp.TOTP(secret)
    issuer = settings.app_name or "QuantPilot AI"
    return totp.provisioning_uri(name=username, issuer_name=issuer)


def generate_qr_code_base64(uri: str) -> str:
    """Generate QR code as base64-encoded PNG."""
    import base64

    qr = qrcode.QRCode(
        version=1,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=6,
        border=4,
    )
    qr.add_data(uri)
    qr.make(fit=True)

    img = qr.make_image(fill_color="black", back_color="white")
    buffer = io.BytesIO()
    img.save(buffer, format="PNG")
    buffer.seek(0)

    return base64.b64encode(buffer.getvalue()).decode("utf-8")


# ─────────────────────────────────────────────
# TOTP Verification
# ─────────────────────────────────────────────

def verify_totp_code(secret: str, code: str) -> bool:
    """
    Verify a TOTP code with a ±1 window tolerance (30s each side).
    """
    if not secret or not code:
        return False
    code = code.strip().replace(" ", "")
    if not code.isdigit() or len(code) != 6:
        return False
    totp = pyotp.TOTP(secret)
    return totp.verify(code, valid_window=1)


# ─────────────────────────────────────────────
# Recovery Codes
# ─────────────────────────────────────────────

def generate_recovery_codes(count: int = 8) -> list[str]:
    """Generate a set of one-time recovery codes."""
    codes = []
    for _ in range(count):
        raw = secrets.token_hex(4)  # 8 hex chars
        codes.append(f"{raw[:4]}-{raw[4:]}".upper())
    return codes


def hash_recovery_code(code: str) -> str:
    """Hash a recovery code for secure storage."""
    normalized = code.strip().upper().replace("-", "")
    return hashlib.sha256(normalized.encode()).hexdigest()


def verify_recovery_code(code: str, hashed_codes: list[str]) -> Optional[int]:
    """
    Verify a recovery code against stored hashes.
    Returns the index of the matched code, or None.
    """
    code_hash = hash_recovery_code(code)
    for idx, stored_hash in enumerate(hashed_codes):
        if hmac.compare_digest(code_hash, stored_hash):
            return idx
    return None
