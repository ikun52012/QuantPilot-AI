"""
API endpoint tests.
"""
import pytest
from httpx import AsyncClient

from core.security import hash_password
from core.database import UserModel


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
