"""
API endpoint tests.
"""

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from core.database import PaymentModel, SubscriptionModel, SubscriptionPlanModel, UserModel
from core.security import hash_password
from core.utils.datetime import utcnow
from tests.test_admin_updates import _login_admin


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
        from core.login_guard import _attempts, _lockouts
        from core.totp import encrypt_totp_secret

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

    @pytest.mark.asyncio
    async def test_2fa_verify_rejects_non_pending_token(self, client: AsyncClient, test_user_data):
        login = await client.post("/api/auth/register", json=test_user_data)
        assert login.status_code == 200
        token = login.json()["token"]

        response = await client.post(
            "/api/auth/2fa/verify",
            headers={"Authorization": f"Bearer {token}"},
            json={"code": "000000"},
        )
        assert response.status_code == 403
        assert "not pending" in response.json()["detail"].lower()

    @pytest.mark.asyncio
    async def test_2fa_verify_rejects_revoked_pending_token(self, client: AsyncClient, test_user_data, db_session):
        from core.totp import encrypt_totp_secret

        user = UserModel(
            username=test_user_data["username"].lower(),
            email=test_user_data["email"].lower(),
            password_hash=hash_password(test_user_data["password"]),
            totp_secret=encrypt_totp_secret("JBSWY3DPEHPK3PXP"),
            totp_enabled=True,
            token_version=0,
        )
        db_session.add(user)
        await db_session.commit()

        login = await client.post("/api/auth/login", json={
            "username": test_user_data["username"],
            "password": test_user_data["password"],
        })
        assert login.status_code == 200
        pending_token = login.json()["token"]

        user.token_version = 1
        await db_session.commit()

        response = await client.post(
            "/api/auth/2fa/verify",
            headers={"Authorization": f"Bearer {pending_token}"},
            json={"code": "000000"},
        )
        assert response.status_code == 401
        assert "revoked" in response.json()["detail"].lower()


class TestUserEndpoints:
    @pytest.mark.asyncio
    async def test_exchange_settings_accept_market_and_order_type(self, client: AsyncClient, test_user_data):
        login = await client.post("/api/auth/register", json=test_user_data)
        assert login.status_code == 200
        headers = _csrf_headers(login)

        response = await client.post(
            "/api/settings/exchange",
            headers=headers,
            json={
                "exchange": "okx",
                "api_key": "k",
                "api_secret": "s",
                "password": "p",
                "live_trading": False,
                "sandbox_mode": True,
                "market_type": "contract",
                "default_order_type": "limit",
                "stop_loss_order_type": "market",
            },
        )
        assert response.status_code == 200

    @pytest.mark.asyncio
    async def test_positions_returns_real_unrealized_pnl(self, client: AsyncClient, test_user_data, db_session):
        from core.database import PositionModel
        from core.utils.datetime import utcnow

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

    @pytest.mark.asyncio
    async def test_positions_include_pending_limit_orders(self, client: AsyncClient, test_user_data, db_session):
        from core.database import PositionModel
        from core.utils.datetime import utcnow

        login = await client.post("/api/auth/register", json=test_user_data)
        assert login.status_code == 200
        user_id = login.json()["user"]["id"]

        db_session.add(PositionModel(
            user_id=user_id,
            ticker="BTCUSDT",
            direction="long",
            status="pending",
            entry_price=50000,
            quantity=0.01,
            remaining_quantity=0.01,
            order_type="limit",
            opened_at=utcnow(),
        ))
        await db_session.commit()

        response = await client.get("/api/positions")
        assert response.status_code == 200
        data = response.json()
        assert len(data) == 1
        assert data[0]["symbol"] == "BTCUSDT"
        assert data[0]["status"] == "pending"

    @pytest.mark.asyncio
    async def test_positions_deduplicate_exchange_symbol_aliases(self, client: AsyncClient, test_user_data, db_session, monkeypatch):
        from core.database import PositionModel
        from core.utils.datetime import utcnow

        login = await client.post("/api/auth/register", json=test_user_data)
        assert login.status_code == 200
        user_id = login.json()["user"]["id"]

        db_session.add(PositionModel(
            user_id=user_id,
            ticker="SPYUSDT.P",
            direction="long",
            status="open",
            entry_price=500.0,
            quantity=1.0,
            remaining_quantity=1.0,
            opened_at=utcnow(),
            live_trading=True,
        ))
        await db_session.commit()

        async def fake_exchange_config_for_user(_db, _user):
            return {"live_trading": True, "sandbox_mode": False, "exchange": "okx"}

        async def fake_get_open_positions(_exchange_config):
            return [
                {
                    "symbol": "SPY/USDT:USDT",
                    "side": "long",
                    "contracts": 1.0,
                    "entryPrice": 500.0,
                    "entry_price": 500.0,
                    "markPrice": 501.0,
                    "mark_price": 501.0,
                    "unrealizedPnl": 1.0,
                    "unrealized_pnl": 1.0,
                    "percentage": 0.2,
                    "leverage": 1.0,
                }
            ]

        monkeypatch.setattr("routers.user._exchange_config_for_user", fake_exchange_config_for_user)
        monkeypatch.setattr("exchange.get_open_positions", fake_get_open_positions)

        response = await client.get("/api/positions")
        assert response.status_code == 200
        data = response.json()
        assert len(data) == 1
        assert data[0]["symbol"] == "SPYUSDT.P"
        assert data[0]["source"] == "db"

    @pytest.mark.asyncio
    async def test_user_settings_returns_exchange_market_and_order_fields(self, client: AsyncClient, test_user_data):
        login = await client.post("/api/auth/register", json=test_user_data)
        assert login.status_code == 200
        headers = _csrf_headers(login)

        response = await client.post(
            "/api/user/settings/exchange",
            headers=headers,
            json={
                "exchange": "okx",
                "api_key": "k",
                "api_secret": "s",
                "password": "p",
                "live_trading": True,
                "sandbox_mode": True,
                "market_type": "contract",
                "default_order_type": "limit",
                "stop_loss_order_type": "market",
            },
        )
        assert response.status_code == 200

        settings_response = await client.get("/api/user/settings")
        assert settings_response.status_code == 200
        exchange = settings_response.json()["exchange"]
        assert exchange["name"] == "okx"
        assert exchange["market_type"] == "contract"
        assert exchange["default_order_type"] == "limit"
        assert exchange["stop_loss_order_type"] == "market"

    @pytest.mark.asyncio
    async def test_user_settings_return_limit_timeout_overrides(self, client: AsyncClient, test_user_data):
        login = await client.post("/api/auth/register", json=test_user_data)
        assert login.status_code == 200
        headers = _csrf_headers(login)

        response = await client.post(
            "/api/user/settings/exchange",
            headers=headers,
            json={
                "exchange": "okx",
                "api_key": "k",
                "api_secret": "s",
                "password": "p",
                "live_trading": True,
                "sandbox_mode": True,
                "market_type": "contract",
                "default_order_type": "limit",
                "stop_loss_order_type": "market",
                "limit_timeout_overrides": {
                    "15m": 7200,
                    "30m": 14400,
                    "1h": 21600,
                    "4h": 86400,
                    "1d": 432000,
                },
            },
        )
        assert response.status_code == 200

        settings_response = await client.get("/api/user/settings")
        assert settings_response.status_code == 200
        exchange = settings_response.json()["exchange"]
        assert exchange["limit_timeout_overrides"] == {
            "15m": 7200,
            "30m": 14400,
            "1h": 21600,
            "4h": 86400,
            "1d": 432000,
        }

    @pytest.mark.asyncio
    async def test_user_exchange_settings_allow_clearing_credentials(self, client: AsyncClient, test_user_data):
        login = await client.post("/api/auth/register", json=test_user_data)
        assert login.status_code == 200
        headers = _csrf_headers(login)

        initial = await client.post(
            "/api/user/settings/exchange",
            headers=headers,
            json={
                "exchange": "okx",
                "api_key": "k",
                "api_secret": "s",
                "password": "p",
                "live_trading": True,
                "sandbox_mode": True,
                "market_type": "contract",
                "default_order_type": "limit",
                "stop_loss_order_type": "market",
            },
        )
        assert initial.status_code == 200

        cleared = await client.post(
            "/api/user/settings/exchange",
            headers=headers,
            json={
                "exchange": "okx",
                "api_key": "",
                "api_secret": "",
                "password": "",
                "live_trading": False,
                "sandbox_mode": False,
                "market_type": "contract",
                "default_order_type": "limit",
                "stop_loss_order_type": "market",
            },
        )
        assert cleared.status_code == 200

        settings_response = await client.get("/api/user/settings")
        assert settings_response.status_code == 200
        exchange = settings_response.json()["exchange"]
        assert exchange["api_configured"] is False
        assert "password" not in exchange

    @pytest.mark.asyncio
    async def test_history_returns_take_profit_levels(self, client: AsyncClient, test_user_data, db_session):
        login = await client.post("/api/auth/register", json=test_user_data)
        assert login.status_code == 200
        user_id = login.json()["user"]["id"]

        payload = {
            "signal": {"price": 50000},
            "analysis": {
                "suggested_stop_loss": 49000,
                "suggested_tp1": 51000,
                "suggested_tp2": 52000,
                "tp1_qty_pct": 50,
                "tp2_qty_pct": 50,
            },
            "result": {
                "entry_price": 50000,
                "take_profit_orders": [
                    {"level": 1, "price": 51000, "qty_pct": 50},
                    {"level": 2, "price": 52000, "qty_pct": 50},
                ],
            },
        }
        db_session.add(
            __import__("core.database", fromlist=["TradeModel"]).TradeModel(
                user_id=user_id,
                timestamp=utcnow(),
                ticker="BTCUSDT",
                direction="long",
                execute=True,
                order_status="filled",
                pnl_pct=0.0,
                payload_json=__import__("json").dumps(payload),
            )
        )
        await db_session.commit()

        response = await client.get("/api/history")
        assert response.status_code == 200
        item = response.json()[0]
        assert len(item["take_profit_levels"]) == 2
        assert item["take_profit_levels"][0]["price"] == 51000
        assert item["take_profit_levels"][1]["price"] == 52000

    @pytest.mark.asyncio
    async def test_chart_position_markers_include_pending_positions(self, client: AsyncClient, test_user_data, db_session):
        from core.database import PositionModel
        from core.utils.datetime import utcnow

        login = await client.post("/api/auth/register", json=test_user_data)
        assert login.status_code == 200
        user_id = login.json()["user"]["id"]

        db_session.add(PositionModel(
            user_id=user_id,
            ticker="BTCUSDT",
            direction="long",
            status="pending",
            entry_price=50000,
            quantity=0.01,
            remaining_quantity=0.01,
            order_type="limit",
            opened_at=utcnow(),
        ))
        await db_session.commit()

        response = await client.get("/api/chart/positions/BTCUSDT")
        assert response.status_code == 200
        payload = response.json()
        assert payload["count"] == 1
        assert payload["markers"][0]["text"].startswith("LONG @ 50000.00")

    @pytest.mark.asyncio
    async def test_chart_position_markers_match_symbol_aliases(self, client: AsyncClient, test_user_data, db_session):
        from core.database import PositionModel
        from core.utils.datetime import utcnow

        login = await client.post("/api/auth/register", json=test_user_data)
        assert login.status_code == 200
        user_id = login.json()["user"]["id"]

        db_session.add(PositionModel(
            user_id=user_id,
            ticker="SPYUSDT.P",
            direction="long",
            status="open",
            entry_price=500.0,
            quantity=1.0,
            remaining_quantity=1.0,
            opened_at=utcnow(),
        ))
        await db_session.commit()

        response = await client.get("/api/chart/positions/SPYUSDT")
        assert response.status_code == 200
        payload = response.json()
        assert payload["count"] == 1
        assert payload["markers"][0]["text"].startswith("LONG @ 500.00")


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


class TestSubscriptionConsistency:
    @pytest.mark.asyncio
    async def test_admin_grant_subscription_cancels_previous_active(self, client: AsyncClient, db_session, test_admin_data, test_user_data):
        headers = await _login_admin(client, db_session, test_admin_data)

        user = UserModel(
            username=test_user_data["username"].lower(),
            email=test_user_data["email"].lower(),
            password_hash=hash_password(test_user_data["password"]),
            is_active=True,
        )
        plan_a = SubscriptionPlanModel(name="Alpha", description="", price_usdt=10, duration_days=30, features_json="[]")
        plan_b = SubscriptionPlanModel(name="Beta", description="", price_usdt=20, duration_days=60, features_json="[]")
        db_session.add_all([user, plan_a, plan_b])
        await db_session.commit()

        first = await client.post(
            f"/api/admin/user/{user.id}/subscription",
            json={"plan_id": plan_a.id, "status": "active"},
            headers=headers,
        )
        assert first.status_code == 200

        second = await client.post(
            f"/api/admin/user/{user.id}/subscription",
            json={"plan_id": plan_b.id, "status": "active"},
            headers=headers,
        )
        assert second.status_code == 200

        rows = (await db_session.execute(
            select(SubscriptionModel)
            .where(SubscriptionModel.user_id == user.id)
            .order_by(SubscriptionModel.created_at.asc())
        )).scalars().all()
        assert len(rows) == 2
        assert rows[0].status == "cancelled"
        assert rows[1].status == "active"

    @pytest.mark.asyncio
    async def test_confirm_payment_cancels_previous_active_subscription(self, client: AsyncClient, db_session, test_admin_data, test_user_data):
        headers = await _login_admin(client, db_session, test_admin_data)

        user = UserModel(
            username=test_user_data["username"].lower(),
            email=test_user_data["email"].lower(),
            password_hash=hash_password(test_user_data["password"]),
            is_active=True,
        )
        plan_a = SubscriptionPlanModel(name="Alpha", description="", price_usdt=10, duration_days=30, features_json="[]")
        plan_b = SubscriptionPlanModel(name="Beta", description="", price_usdt=20, duration_days=60, features_json="[]")
        db_session.add_all([user, plan_a, plan_b])
        await db_session.flush()

        active_sub = SubscriptionModel(
            user_id=user.id,
            plan_id=plan_a.id,
            status="active",
            start_date=(now := utcnow()),
            end_date=now,
        )
        pending_sub = SubscriptionModel(user_id=user.id, plan_id=plan_b.id, status="pending")
        db_session.add_all([active_sub, pending_sub])
        await db_session.flush()

        payment = PaymentModel(
            user_id=user.id,
            subscription_id=pending_sub.id,
            amount=20,
            currency="USDT",
            network="TRC20",
            wallet_address="TADDR123",
            status="submitted",
            tx_hash="tx-123456",
        )
        db_session.add(payment)
        await db_session.commit()

        response = await client.post(f"/api/admin/payment/{payment.id}/confirm", headers=headers)
        assert response.status_code == 200

        await db_session.refresh(active_sub)
        await db_session.refresh(pending_sub)
        await db_session.refresh(payment)
        assert payment.status == "confirmed"
        assert active_sub.status == "cancelled"
        assert pending_sub.status == "active"

    @pytest.mark.asyncio
    async def test_payment_tx_hash_unique_only_when_non_empty(self, db_session, test_user_data):
        user = UserModel(
            username=test_user_data["username"].lower(),
            email=test_user_data["email"].lower(),
            password_hash=hash_password(test_user_data["password"]),
            is_active=True,
        )
        db_session.add(user)
        await db_session.flush()

        db_session.add_all([
            PaymentModel(user_id=user.id, amount=10, tx_hash=""),
            PaymentModel(user_id=user.id, amount=20, tx_hash=""),
        ])
        await db_session.flush()

        db_session.add(PaymentModel(user_id=user.id, amount=30, tx_hash="tx-dup"))
        await db_session.flush()

        db_session.add(PaymentModel(user_id=user.id, amount=40, tx_hash="tx-dup"))
        with pytest.raises(IntegrityError):
            await db_session.flush()


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
