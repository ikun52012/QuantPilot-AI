import unittest

try:
    import loguru  # noqa: F401
    import fastapi  # noqa: F401
except ModuleNotFoundError as exc:
    raise unittest.SkipTest(f"runtime dependency not installed: {exc.name}")

from core.auth import create_token, verify_token
from core.security import validate_password_strength


class AuthSecurityTests(unittest.TestCase):
    def test_password_strength_rejects_weak_passwords(self):
        for password in ("12345678", "password", "lowercase1!", "UPPERCASE1!", "NoNumber!", "NoSymbol1"):
            ok, _ = validate_password_strength(password, username="alice", email="alice@example.com")
            self.assertFalse(ok, password)

    def test_password_strength_accepts_complex_password(self):
        ok, reason = validate_password_strength("BetterPass1!", username="alice", email="alice@example.com")
        self.assertTrue(ok, reason)

    def test_token_contains_version_claim(self):
        token = create_token("user-1", "alice", "user", token_version=7)
        payload = verify_token(token)
        self.assertIsNotNone(payload)
        self.assertEqual(payload["ver"], 7)


if __name__ == "__main__":
    unittest.main()
