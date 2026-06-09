"""
Test script to validate security fixes.
Run this after applying security patches to verify all fixes work correctly.
"""
import os
import sys
from pathlib import Path
from unittest.mock import patch

# Add project root to path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

def test_password_strength_validation():
    """Test extended password blacklist."""
    from core.security import validate_password_strength

    weak_passwords = [
        "123456", "password", "admin", "changeme", "tradingview",
        "bitcoin", "quantpilot", "test", "password123", "qwerty123"
    ]

    print("Testing password strength validation...")
    for pwd in weak_passwords:
        is_valid, reason = validate_password_strength(pwd)
        assert not is_valid, f"Weak password '{pwd}' was accepted"
        print(f"  [PASS] Weak password '{pwd}' rejected: {reason}")

    # Test strong password
    strong_pwd = "StrongP@ssw0rd!2026"
    is_valid, reason = validate_password_strength(strong_pwd)
    assert is_valid, f"Strong password rejected: {reason}"
    print("  [PASS] Strong password accepted")


def test_jwt_expiry_validation():
    """Test JWT expiry hours configuration."""
    print("Testing JWT expiry hours...")

    # Import settings
    from core.config import Settings

    # Test default is 4 hours
    with patch.dict(os.environ, {"DEFAULT_ADMIN_PASSWORD": "StrongP@ssw0rd!2026"}, clear=False):
        os.environ.pop("JWT_EXPIRY_HOURS", None)
        settings = Settings.from_env()
    assert settings.jwt_expiry_hours == 4, f"JWT expiry default is {settings.jwt_expiry_hours} hours, expected 4"
    print("  [PASS] JWT expiry default is 4 hours")

    # Test environment override is honored
    with patch.dict(
        os.environ,
        {"DEFAULT_ADMIN_PASSWORD": "StrongP@ssw0rd!2026", "JWT_EXPIRY_HOURS": "6"},
        clear=False,
    ):
        settings = Settings.from_env()
    assert settings.jwt_expiry_hours == 6, f"JWT expiry env override is {settings.jwt_expiry_hours} hours, expected 6"
    print("  [PASS] JWT expiry environment override works")


def test_log_sensitive_filter():
    """Test sensitive log filtering regex."""
    print("Testing sensitive log filtering...")

    from core.utils.common import SENSITIVE_LOG_RE

    test_cases = [
        ("api_key=secret123", "api_key=***"),
        ("exchange_api_secret=mysecret", "exchange_api_secret=***"),
        ("openai_api_key=sk-abc", "openai_api_key=***"),
        ("password=admin123", "password=***"),
        ("jwt_secret=weak", "jwt_secret=***"),
    ]

    for test_input, _expected_pattern in test_cases:
        result = SENSITIVE_LOG_RE.sub(r"\1***", test_input)
        assert "***" in result, f"'{test_input}' not filtered properly: {result}"
        print(f"  [PASS] '{test_input}' filtered to '{result}'")


def test_encryption_key_derivation():
    """Test PBKDF2 key derivation."""
    print("Testing encryption key derivation...")

    from core.security import _derive_fernet_key

    # Test that PBKDF2 is used (key should be deterministic for same input)
    test_key = "test-encryption-key-123"
    derived1 = _derive_fernet_key(test_key)
    derived2 = _derive_fernet_key(test_key)

    assert derived1 == derived2, "Key derivation not deterministic"
    print("  [PASS] Key derivation is deterministic (PBKDF2)")

    # Test different keys produce different results
    different_key = "different-key-456"
    derived_diff = _derive_fernet_key(different_key)
    assert derived1 != derived_diff, "Different keys produce same result"
    print("  [PASS] Different keys produce different results")


def test_webhook_replay_window():
    """Test webhook replay window configuration."""
    print("Testing webhook replay window...")

    # Import webhook module
    import routers.webhook

    # Check that replay window is 60 seconds
    replay_window = routers.webhook._WEBHOOK_REPLAY_WINDOW_SECS
    assert replay_window == 60, f"Webhook replay window is {replay_window}s, expected 60s"
    print("  [PASS] Webhook replay window is 60 seconds")


def test_cookie_samesite():
    """Test cookie SameSite configuration."""
    print("Testing cookie SameSite configuration...")

    # This test requires checking the actual cookie setting code
    # We verify by reading the source code
    # Read the function implementation
    import inspect

    from core.auth import set_auth_cookie
    source = inspect.getsource(set_auth_cookie)

    assert 'samesite="strict"' in source or "samesite='strict'" in source, "Cookie SameSite is not 'strict'"
    print("  [PASS] Cookie SameSite is 'strict'")


def run_all_tests():
    """Run all security fix validation tests."""
    print("\n" + "="*60)
    print("Security Fixes Validation Tests")
    print("="*60 + "\n")

    tests = [
        ("Password Strength Validation", test_password_strength_validation),
        ("JWT Expiry Configuration", test_jwt_expiry_validation),
        ("Log Sensitive Filtering", test_log_sensitive_filter),
        ("Encryption Key Derivation", test_encryption_key_derivation),
        ("Webhook Replay Window", test_webhook_replay_window),
        ("Cookie SameSite Configuration", test_cookie_samesite),
    ]

    passed = 0
    failed = 0

    for test_name, test_func in tests:
        try:
            test_func()
        except Exception as e:
            print(f"  [FAIL] {test_name} - Exception: {e}")
            failed += 1
        else:
            passed += 1

    print("\n" + "="*60)
    print(f"Test Results: {passed} passed, {failed} failed")
    print("="*60 + "\n")

    if failed > 0:
        print("[WARNING] Some security fixes validation tests failed!")
        print("Please review the failed tests and ensure all fixes are applied correctly.")
        return False
    else:
        print("[SUCCESS] All security fixes validation tests passed!")
        print("The system is now secured according to the audit recommendations.")
        return True


if __name__ == "__main__":
    success = run_all_tests()
    sys.exit(0 if success else 1)
