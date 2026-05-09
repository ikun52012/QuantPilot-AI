"""
Signal Server - Security Module (Enhanced)
Encryption, password hashing, and security utilities.
"""
import base64
import hashlib
import hmac
import os
import re
import secrets
import threading
from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet, InvalidToken
from loguru import logger

# Data directory for encryption key
DATA_DIR = Path(__file__).parent.parent / "data"
KEY_FILE = DATA_DIR / "app_encryption.key"
ENCRYPTED_PREFIX = "enc:v1:"

# Sensitive keys that should be encrypted
SENSITIVE_KEYS = {
    "api_key",
    "api_secret",
    "password",
    "bot_token",
    "secret",
    "openai_api_key",
    "anthropic_api_key",
    "deepseek_api_key",
    "custom_provider_api_key",
    "whale_alert_api_key",
    "etherscan_api_key",
    "glassnode_api_key",
    "cryptoquant_api_key",
}

# Password hashing settings
_HASH_ITERATIONS = 260_000  # OWASP minimum for PBKDF2-SHA256
_SALT_SIZE = 32

# Common passwords to reject
_COMMON_PASSWORDS = {
    "123456", "12345678", "123456789", "password", "qwerty123",
    "admin123", "admin123456", "letmein123", "tradingview",
}


# ─────────────────────────────────────────────
# In-Memory Secure API Key Storage
# ─────────────────────────────────────────────

_secure_keys_lock = threading.Lock()
_secure_keys: dict[str, str] = {}


def set_secure_api_key(key_name: str, key_value: str) -> None:
    """
    Store an API key in secure in-memory storage.

    Keys are NOT written to environment variables to prevent
    exposure through process listings or subprocess spawning.
    """
    with _secure_keys_lock:
        _secure_keys[key_name] = key_value


def get_secure_api_key(key_name: str) -> str | None:
    """
    Retrieve an API key from secure in-memory storage.

    Falls back to environment variable if not in memory,
    but logs a warning about the security risk.
    """
    with _secure_keys_lock:
        if key_name in _secure_keys:
            return _secure_keys[key_name]

    # Fallback to env for backwards compatibility
    env_value = os.getenv(key_name.upper(), "")
    if env_value:
        logger.warning(
            f"[Security] API key '{key_name}' read from environment variable. "
            "Consider using secure storage via admin panel."
        )
    return env_value


def clear_secure_api_key(key_name: str) -> None:
    """Remove an API key from secure storage."""
    with _secure_keys_lock:
        _secure_keys.pop(key_name, None)


def has_secure_api_key(key_name: str) -> bool:
    """Check if an API key exists in secure storage."""
    with _secure_keys_lock:
        return key_name in _secure_keys or bool(os.getenv(key_name.upper(), ""))


def mask_secret(value: Any, *, front: int = 4, back: int = 4) -> str:
    """Return a safe masked representation of a stored secret."""
    secret = str(value or "").strip()
    if not secret:
        return ""
    if len(secret) <= front + back:
        if len(secret) <= 4:
            return "****"
        edge = min(2, max(1, len(secret) // 4))
        return f"{secret[:edge]}****{secret[-edge:]}"
    return f"{secret[:front]}****{secret[-back:]}"


# ─────────────────────────────────────────────
# Encryption
# ─────────────────────────────────────────────

def _derive_fernet_key(raw: str) -> bytes:
    """Derive a Fernet-compatible key from a raw string."""
    raw = (raw or "").strip()
    try:
        Fernet(raw.encode())
        return raw.encode()
    except (ValueError, TypeError, Exception):
        digest = hashlib.sha256(raw.encode()).digest()
        return base64.urlsafe_b64encode(digest)


def _load_or_create_key() -> bytes:
    """Load or create the encryption key."""
    env_key = os.getenv("APP_ENCRYPTION_KEY", "").strip()
    if env_key:
        return _derive_fernet_key(env_key)

    DATA_DIR.mkdir(parents=True, exist_ok=True)

    if KEY_FILE.exists():
        try:
            return KEY_FILE.read_text(encoding="utf-8").strip().encode()
        except PermissionError as err:
            logger.error(f"[Security] Permission denied reading {KEY_FILE}. Check file permissions.")
            raise RuntimeError(
                f"Cannot read encryption key file: {KEY_FILE}. Ensure proper permissions (chmod 600)."
            ) from err

    key = Fernet.generate_key()

    try:
        KEY_FILE.write_text(key.decode(), encoding="utf-8")
    except PermissionError as err:
        logger.error(f"[Security] Permission denied writing to {KEY_FILE}. Check directory permissions.")
        raise RuntimeError(
            f"Cannot write encryption key file: {KEY_FILE}. Ensure directory is writable."
        ) from err

    try:
        KEY_FILE.chmod(0o600)
    except (OSError, PermissionError):
        pass

    logger.warning("[Security] Generated persistent APP_ENCRYPTION_KEY in data/app_encryption.key")
    return key


# Initialize Fernet cipher
_FERNET = Fernet(_load_or_create_key())


def encrypt_value(value: str) -> str:
    """Encrypt a string value."""
    if not value:
        return value
    if str(value).startswith(ENCRYPTED_PREFIX):
        return str(value)
    token = _FERNET.encrypt(str(value).encode()).decode()
    return ENCRYPTED_PREFIX + token


def decrypt_value(value: str) -> str:
    """Decrypt a string value."""
    if not isinstance(value, str) or not value.startswith(ENCRYPTED_PREFIX):
        return value
    token = value[len(ENCRYPTED_PREFIX):]
    try:
        return _FERNET.decrypt(token.encode()).decode()
    except InvalidToken:
        logger.warning("[Security] Could not decrypt a stored secret; keeping encrypted value")
        return value


def encrypt_settings_payload(data: Any) -> Any:
    """Recursively encrypt sensitive values in a payload."""
    if isinstance(data, dict):
        encrypted = {}
        for key, value in data.items():
            if key in SENSITIVE_KEYS and isinstance(value, str) and value:
                encrypted[key] = encrypt_value(value)
            else:
                encrypted[key] = encrypt_settings_payload(value)
        return encrypted
    if isinstance(data, list):
        return [encrypt_settings_payload(item) for item in data]
    return data


def decrypt_settings_payload(data: Any) -> Any:
    """Recursively decrypt sensitive values in a payload."""
    if isinstance(data, dict):
        decrypted = {}
        for key, value in data.items():
            if key in SENSITIVE_KEYS and isinstance(value, str):
                decrypted[key] = decrypt_value(value)
            else:
                decrypted[key] = decrypt_settings_payload(value)
        return decrypted
    if isinstance(data, list):
        return [decrypt_settings_payload(item) for item in data]
    return data


# ─────────────────────────────────────────────
# Password Hashing
# ─────────────────────────────────────────────

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
    except (ValueError, IndexError, TypeError):
        return False
    except Exception:
        return False


def validate_password_strength(
    password: str,
    username: str = "",
    email: str = ""
) -> tuple[bool, str]:
    """Validate password strength."""
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
# Token Generation
# ─────────────────────────────────────────────

def generate_token(length: int = 32) -> str:
    """Generate a secure random token."""
    return secrets.token_urlsafe(length)


def generate_webhook_secret() -> str:
    """Generate a webhook secret."""
    return secrets.token_urlsafe(32)


def is_placeholder_webhook_secret(secret: str) -> bool:
    """
    Return True for weak/placeholder webhook secrets that must not be accepted.

    Checks:
    - Known placeholder values from documentation
    - Secrets shorter than 16 characters (too weak)
    - Common weak patterns
    """
    normalized = str(secret or "").strip()
    if not normalized:
        return True

    # Check length - minimum 16 characters for security
    if len(normalized) < 16:
        logger.warning(f"[Security] Webhook secret too short ({len(normalized)} chars), minimum 16 required")
        return True

    # Check known placeholders
    lower = normalized.lower()
    placeholders = {
        "replace-with-a-long-random-webhook-secret",
        "replace-with-long-random-webhook-secret",
        "your-webhook-secret",
        "{{your-webhook-secret}}",
        "changeme",
        "change-me",
        "secret",
        "webhook-secret",
        "my-secret",
        "test-secret",
        "dev-secret",
        "12345678",
        "abcdefgh",
    }

    if lower in placeholders or lower.startswith("replace-with-"):
        return True

    # Check for simple patterns (all same char, sequential, etc.)
    if len(set(normalized)) < 4:
        logger.warning("[Security] Webhook secret has too few unique characters")
        return True

    return False


def webhook_secret_hash(secret: str) -> str:
    """Hash a webhook secret for storage."""
    secret = str(secret or "").strip()
    if not secret:
        return ""
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


# ─────────────────────────────────────────────
# HMAC Verification
# ─────────────────────────────────────────────

def verify_hmac_signature(
    secret: str,
    message: bytes,
    signature: str,
    algorithm: str = "sha256"
) -> bool:
    """Verify an HMAC signature."""
    if not secret or not signature:
        return False

    digest = hmac.new(secret.encode(), message, getattr(hashlib, algorithm)).hexdigest()
    expected = f"{algorithm}={digest}"

    return (
        hmac.compare_digest(signature, expected) or
        hmac.compare_digest(signature, digest)
    )
