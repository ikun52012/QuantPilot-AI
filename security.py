"""
Security helpers for encrypted-at-rest runtime and user secrets.

⚠️ DEPRECATED: This file is the legacy security module.
Please use core/security.py instead, which provides:
    - Fernet encryption for sensitive data
    - Webhook secret hashing
    - Settings payload encryption/decryption

To import:
    from core.security import encrypt_value, decrypt_value, hash_password, webhook_secret_hash

This file is kept for backward compatibility and will be removed in a future version.
"""
import base64
import hashlib
import os
from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet, InvalidToken
from loguru import logger

DATA_DIR = Path(__file__).parent / "data"
KEY_FILE = DATA_DIR / "app_encryption.key"
ENCRYPTED_PREFIX = "enc:v1:"

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
}


def _derive_fernet_key(raw: str) -> bytes:
    raw = (raw or "").strip()
    try:
        Fernet(raw.encode())
        return raw.encode()
    except Exception:
        digest = hashlib.sha256(raw.encode()).digest()
        return base64.urlsafe_b64encode(digest)


def _load_or_create_key() -> bytes:
    env_key = os.getenv("APP_ENCRYPTION_KEY", "").strip()
    if env_key:
        return _derive_fernet_key(env_key)

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if KEY_FILE.exists():
        return KEY_FILE.read_text(encoding="utf-8").strip().encode()

    key = Fernet.generate_key()
    KEY_FILE.write_text(key.decode(), encoding="utf-8")
    try:
        KEY_FILE.chmod(0o600)
    except OSError:
        pass
    logger.warning("[Security] Generated persistent APP_ENCRYPTION_KEY in data/app_encryption.key")
    return key


_FERNET = Fernet(_load_or_create_key())


def encrypt_value(value: str) -> str:
    if not value:
        return value
    if str(value).startswith(ENCRYPTED_PREFIX):
        return str(value)
    token = _FERNET.encrypt(str(value).encode()).decode()
    return ENCRYPTED_PREFIX + token


def decrypt_value(value: str) -> str:
    if not isinstance(value, str) or not value.startswith(ENCRYPTED_PREFIX):
        return value
    token = value[len(ENCRYPTED_PREFIX):]
    try:
        return _FERNET.decrypt(token.encode()).decode()
    except InvalidToken:
        logger.warning("[Security] Could not decrypt a stored secret; keeping encrypted value")
        return value


def encrypt_settings_payload(data: Any) -> Any:
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
