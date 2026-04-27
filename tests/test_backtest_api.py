import pytest


@pytest.mark.asyncio
async def test_run_backtest_preserves_client_errors(client, db_session, test_admin_data, monkeypatch):
    from core.database import UserModel
    from core.security import hash_password

    admin = UserModel(
        username=test_admin_data["username"].lower(),
        email=test_admin_data["email"].lower(),
        password_hash=hash_password(test_admin_data["password"]),
        role="admin",
        is_active=True,
    )
    db_session.add(admin)
    await db_session.commit()

    login = await client.post(
        "/api/auth/login",
        json={
            "username": test_admin_data["username"],
            "password": test_admin_data["password"],
        },
    )
    assert login.status_code == 200
    csrf = login.cookies.get("tvss_csrf")
    assert csrf

    async def no_data(*args, **kwargs):
        return []

    monkeypatch.setattr("routers.backtest.fetch_ohlcv_history", no_data)

    response = await client.post(
        "/api/backtest/run",
        json={"ticker": "BTCUSDT", "strategy": "simple_trend"},
        headers={"X-CSRF-Token": csrf},
    )
    assert response.status_code == 400
    assert "No historical data available" in response.json()["detail"]
