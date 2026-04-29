"""
Security tests.
"""
import shutil
import uuid
from pathlib import Path

from core.database import (
    _generate_bootstrap_admin_password,
    _load_or_create_bootstrap_admin_password,
)
from core.security import (
    decrypt_value,
    encrypt_value,
    generate_token,
    hash_password,
    validate_password_strength,
    verify_password,
    webhook_secret_hash,
)


class TestPasswordHashing:
    """Tests for password hashing."""

    def test_hash_password(self):
        """Test password hashing creates valid hash."""
        password = "TestPassword123!"
        hashed = hash_password(password)
        assert hashed != password
        assert "$" in hashed
        parts = hashed.split("$")
        assert len(parts) == 3
        assert parts[0].isdigit()

    def test_verify_password_correct(self):
        """Test password verification with correct password."""
        password = "TestPassword123!"
        hashed = hash_password(password)
        assert verify_password(password, hashed) is True

    def test_verify_password_incorrect(self):
        """Test password verification with incorrect password."""
        password = "TestPassword123!"
        hashed = hash_password(password)
        assert verify_password("WrongPassword123!", hashed) is False

    def test_verify_password_invalid_hash(self):
        """Test password verification with invalid hash."""
        assert verify_password("password", "invalid_hash") is False


class TestPasswordStrength:
    """Tests for password strength validation."""

    def test_strong_password(self):
        """Test strong password passes validation."""
        ok, reason = validate_password_strength(
            "StrongPass123!",
            username="user",
            email="user@example.com"
        )
        assert ok is True

    def test_short_password(self):
        """Test short password fails."""
        ok, reason = validate_password_strength("Short1!")
        assert ok is False
        assert "8 characters" in reason

    def test_no_uppercase(self):
        """Test password without uppercase fails."""
        ok, reason = validate_password_strength("lowercase123!")
        assert ok is False
        assert "uppercase" in reason.lower()

    def test_no_lowercase(self):
        """Test password without lowercase fails."""
        ok, reason = validate_password_strength("UPPERCASE123!")
        assert ok is False
        assert "lowercase" in reason.lower()

    def test_no_number(self):
        """Test password without number fails."""
        ok, reason = validate_password_strength("NoNumberPass!")
        assert ok is False
        assert "number" in reason.lower()

    def test_no_special(self):
        """Test password without special character fails."""
        ok, reason = validate_password_strength("NoSpecial123")
        assert ok is False
        assert "special" in reason.lower()

    def test_contains_username(self):
        """Test password containing username fails."""
        ok, reason = validate_password_strength(
            "johnpassword123!",
            username="john",
            email="john@example.com"
        )
        assert ok is False
        assert "username" in reason.lower()

    def test_common_password(self):
        """Test common password fails."""
        ok, reason = validate_password_strength("password123!")
        assert ok is False


class TestEncryption:
    """Tests for value encryption."""

    def test_encrypt_decrypt(self):
        """Test encryption and decryption roundtrip."""
        value = "my_secret_api_key_123"
        encrypted = encrypt_value(value)
        assert encrypted != value
        assert encrypted.startswith("enc:v1:")

        decrypted = decrypt_value(encrypted)
        assert decrypted == value

    def test_encrypt_empty(self):
        """Test encrypting empty value."""
        assert encrypt_value("") == ""
        assert encrypt_value(None) is None

    def test_decrypt_plain(self):
        """Test decrypting plain (non-encrypted) value."""
        value = "plain_value"
        assert decrypt_value(value) == value


class TestTokenGeneration:
    """Tests for token generation."""

    def test_generate_token(self):
        """Test token generation."""
        token = generate_token(32)
        assert len(token) > 32
        assert isinstance(token, str)

    def test_generate_webhook_secret(self):
        """Test webhook secret generation."""
        secret = generate_token()
        assert len(secret) > 32

    def test_webhook_secret_hash(self):
        """Test webhook secret hashing."""
        secret = "my_webhook_secret"
        hashed = webhook_secret_hash(secret)
        assert hashed != secret
        assert len(hashed) == 64  # SHA256 hex length

    def test_webhook_secret_hash_empty(self):
        """Test hashing empty secret."""
        assert webhook_secret_hash("") == ""
        assert webhook_secret_hash(None) == ""


class TestBootstrapAdminPassword:
    """Tests for first-deployment admin bootstrap password handling."""

    def test_generated_bootstrap_password_is_strong_shape(self):
        password = _generate_bootstrap_admin_password()
        assert len(password) >= 28
        assert any(ch.islower() for ch in password)
        assert any(ch.isupper() for ch in password)
        assert any(ch.isdigit() for ch in password)
        assert any(ch in "!@#$%^&*_-+=" for ch in password)

    def test_bootstrap_password_file_is_reused(self):
        root = Path(".test_tmp") / f"bootstrap-{uuid.uuid4().hex}"
        path = root / "data" / "bootstrap_admin_password.txt"
        try:
            first, first_path = _load_or_create_bootstrap_admin_password("admin", path)
            second, second_path = _load_or_create_bootstrap_admin_password("admin", path)

            assert first == second
            assert first_path == second_path == path
            assert "password=" in path.read_text(encoding="utf-8")
        finally:
            shutil.rmtree(root, ignore_errors=True)
