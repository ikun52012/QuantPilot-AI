"""
API endpoint tests.
"""
import json

import pytest
from httpx import AsyncClient

from core.security import hash_password
from core.database import UserModel


def _csrf_headers(response) -> dict[str, str]:
    csrf = response.cookies.get("tvss_csrf")
    assert csrf
    return {"X-CSRF-Token": csrf}


@pytest.fixture(autouse=True)
def clear_login_guard_state():
    from core.login_guard import _attempts, _lockouts

    _attempts.clear()
    _lockouts.clear()
    yield
    _attempts.clear()
    _lockouts.clear()


class TestHealthEndpoint:
    """Tests for health check endpoint."""

    @pytest.mark.asyncio
    async def test_health_check(self, client: AsyncClient):
        """Test health check returns healthy status."""
        response = await client.get("/health")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "healthy"
        assert "version" in data


class TestAuthEndpoints:
    """Tests for authentication endpoints."""

    @pytest.mark.asyncio
    async def test_register_user(self, client: AsyncClient, test_user_data):
        """Test user registration."""
        response = await client.post("/api/auth/register", json=test_user_data)
        assert response.status_code == 200
        data = response.json()
        assert "token" in data
        assert data["user"]["username"] == test_user_data["username"].lower()

    @pytest.mark.asyncio
    async def test_register_duplicate_username(self, client: AsyncClient, test_user_data):
        """Test registration with duplicate username fails."""
        await client.post("/api/auth/register", json=test_user_data)
        response = await client.post("/api/auth/register", json=test_user_data)
        assert response.status_code == 400
        assert "already exists" in response.json()["detail"].lower()

    @pytest.mark.asyncio
    async def test_register_weak_password(self, client: AsyncClient):
        """Test registration with weak password fails."""
        response = await client.post("/api/auth/register", json={
            "username": "weakpass",
            "email": "weak@example.com",
            "password": "12345678",
        })
        assert response.status_code == 400

    @pytest.mark.asyncio
    async def test_login_user(self, client: AsyncClient, test_user_data, db_session):
        """Test user login."""
        # Create user directly
        user = UserModel(
            username=test_user_data["username"].lower(),
            email=test_user_data["email"].lower(),
            password_hash=hash_password(test_user_data["password"]),
        )
        db_session.add(user)
        await db_session.commit()

        response = await client.post("/api/auth/login", json={
            "username": test_user_data["username"],
            "password": test_user_data["password"],
        })
        assert response.status_code == 200
        data = response.json()
        assert "token" in data

    @pytest.mark.asyncio
    async def test_login_invalid_password(self, client: AsyncClient, test_user_data, db_session):
        """Test login with invalid password fails."""
        user = UserModel(
            username=test_user_data["username"].lower(),
            email=test_user_data["email"].lower(),
            password_hash=hash_password(test_user_data["password"]),
        )
        db_session.add(user)
        await db_session.commit()

        response = await client.post("/api/auth/login", json={
            "username": test_user_data["username"],
            "password": "WrongPassword123!",
        })
        assert response.status_code == 401

    @pytest.mark.asyncio
    async def test_get_me_unauthenticated(self, client: AsyncClient):
        """Test getting user info without auth fails."""
        response = await client.get("/api/auth/me")
        assert response.status_code == 401

    @pytest.mark.asyncio
    async def test_2fa_verify_failed_attempts_are_limited(self, client: AsyncClient, test_user_data, db_session):
        from core.totp import encrypt_totp_secret
        from core.login_guard import _attempts, _lockouts

        _attempts.clear()
        _lockouts.clear()

        user = UserModel(
            username=test_user_data["username"].lower(),
            email=test_user_data["email"].lower(),
            password_hash=hash_password(test_user_data["password"]),
            totp_secret=encrypt_totp_secret("JBSWY3DPEHPK3PXP"),
            totp_enabled=True,
        )
        db_session.add(user)
        await db_session.commit()

        login = await client.post("/api/auth/login", json={
            "username": test_user_data["username"],
            "password": test_user_data["password"],
        })
        assert login.status_code == 200
        pending_token = login.json()["token"]

        for _ in range(4):
            response = await client.post(
                "/api/auth/2fa/verify",
                headers={"Authorization": f"Bearer {pending_token}"},
                json={"code": "000000"},
            )
            assert response.status_code == 401

        response = await client.post(
            "/api/auth/2fa/verify",
            headers={"Authorization": f"Bearer {pending_token}"},
            json={"code": "000000"},
        )
        assert response.status_code == 429


class TestUserEndpoints:
    @pytest.mark.asyncio
    async def test_positions_returns_real_unrealized_pnl(self, client: AsyncClient, test_user_data, db_session):
        from core.utils.datetime import utcnow
        from core.database import PositionModel

        login = await client.post("/api/auth/register", json=test_user_data)
        assert login.status_code == 200
        user_id = login.json()["user"]["id"]

        db_session.add(PositionModel(
            user_id=user_id,
            ticker="BTCUSDT",
            direction="long",
            status="open",
            entry_price=50000,
            quantity=0.01,
            remaining_quantity=0.01,
            opened_at=utcnow(),
            unrealized_pnl_usdt=12.34,
            current_pnl_pct=2.5,
        ))
        await db_session.commit()

        response = await client.get("/api/positions")
        assert response.status_code == 200
        data = response.json()
        assert data[0]["unrealizedPnl"] == 12.34
        assert data[0]["unrealized_pnl"] == 12.34


class TestSocialEndpoints:
    @pytest.mark.asyncio
    async def test_follow_user_uses_post_and_updates_followers(self, client: AsyncClient, test_user_data):
        publisher = {
            "username": "signaler",
            "email": "signaler@example.com",
            "password": "Str0ng!Pass123",
        }
        follower = {
            "username": "follower",
            "email": "follower@example.com",
            "password": "Str0ng!Pass123",
        }

        pub_resp = await client.post("/api/auth/register", json=publisher)
        assert pub_resp.status_code == 200
        pub_headers = _csrf_headers(pub_resp)
        share = await client.post("/api/social/share", json={
            "ticker": "BTCUSDT",
            "direction": "long",
            "entry_price": 50000,
            "confidence": 0.8,
        }, headers=pub_headers)
        assert share.status_code == 200

        await client.post("/api/auth/logout", headers=pub_headers)
        follow_login = await client.post("/api/auth/register", json=follower)
        assert follow_login.status_code == 200
        follow_headers = _csrf_headers(follow_login)

        get_response = await client.get(f"/api/social/follow/{publisher['username']}")
        assert get_response.status_code in {403, 405}

        post_response = await client.post(f"/api/social/follow/{publisher['username']}", headers=follow_headers)
        assert post_response.status_code == 200
        assert post_response.json()["status"] == "following"

    @pytest.mark.asyncio
    async def test_inline_2fa_login_failed_attempts_are_limited(self, client: AsyncClient, test_user_data, db_session):
        from core.totp import encrypt_totp_secret

        user = UserModel(
            username=test_user_data["username"].lower(),
            email=test_user_data["email"].lower(),
            password_hash=hash_password(test_user_data["password"]),
            totp_secret=encrypt_totp_secret("JBSWY3DPEHPK3PXP"),
            totp_enabled=True,
        )
        db_session.add(user)
        await db_session.commit()

        for _ in range(4):
            response = await client.post("/api/auth/login", json={
                "username": test_user_data["username"],
                "password": test_user_data["password"],
                "totp_code": "000000",
            })
            assert response.status_code == 401

        response = await client.post("/api/auth/login", json={
            "username": test_user_data["username"],
            "password": test_user_data["password"],
            "totp_code": "000000",
        })
        assert response.status_code == 429


class TestPlanEndpoints:
    """Tests for subscription plan endpoints."""

    @pytest.mark.asyncio
    async def test_list_plans(self, client: AsyncClient):
        """Test listing subscription plans."""
        response = await client.get("/api/plans")
        assert response.status_code == 200
        data = response.json()
        assert isinstance(data, list)


class TestI18nEndpoints:
    """Tests for public and authenticated translation endpoints."""

    @pytest.mark.asyncio
    async def test_public_translations_available_without_auth(self, client: AsyncClient):
        response = await client.get("/api/i18n/public/translations/zh")
        assert response.status_code == 200
        data = response.json()["translations"]
        assert data["nav"]["charts"] == "图表"
        assert data["nav"]["strategy_editor"] == "编辑器"
        assert data["pages"]["editor"]["strategy_draft"] == "策略草稿"

    @pytest.mark.asyncio
    async def test_private_translations_still_require_auth(self, client: AsyncClient):
        response = await client.get("/api/i18n/translations/zh")
        assert response.status_code == 401


class TestOfflineTradeSync:
    """Tests for PWA offline trade sync endpoint."""

    @pytest.mark.asyncio
    async def test_sync_offline_trade_requires_auth(self, client: AsyncClient):
        response = await client.post("/api/user/trades/sync", json={
            "ticker": "BTCUSDT",
            "direction": "manual",
        })
        assert response.status_code == 401

    @pytest.mark.asyncio
    async def test_sync_offline_trade_records_current_user(self, client: AsyncClient, test_user_data):
        login = await client.post("/api/auth/register", json=test_user_data)
        assert login.status_code == 200

        response = await client.post(
            "/api/user/trades/sync",
            headers={"X-PWA-Sync": "1"},
            json={
                "id": "offline-1",
                "ticker": "btcusdt",
                "direction": "long",
                "entry_price": 50000,
                "quantity": 0.01,
                "pnl_pct": 1.5,
            },
        )
        assert response.status_code == 200
        assert response.json() == {"status": "synced", "id": "offline-1"}

        trades = await client.get("/api/trades")
        assert trades.status_code == 200
        data = trades.json()
        assert data[0]["id"] == "offline-1"
        assert data[0]["ticker"] == "BTCUSDT"
        assert data[0]["order_status"] == "offline_synced"


class TestWebhookEndpoint:
    """Tests for webhook endpoint."""

    @pytest.mark.asyncio
    async def test_webhook_missing_secret(self, client: AsyncClient):
        """Test webhook without secret fails."""
        response = await client.post("/webhook", json={
            "ticker": "BTCUSDT",
            "direction": "long",
            "price": 50000,
        })
        assert response.status_code == 401

    @pytest.mark.asyncio
    async def test_webhook_invalid_signal(self, client: AsyncClient):
        """Test webhook with invalid signal data fails."""
        response = await client.post("/webhook", json={
            "secret": "test-secret",
            "ticker": "BTCUSDT",
            "direction": "invalid_direction",
            "price": 50000,
        })
        assert response.status_code == 400
