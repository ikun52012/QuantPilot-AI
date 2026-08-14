
from datetime import timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from core.database import AdminAuditLogModel, AdminSettingModel, TradeModel, UserModel
from core.security import hash_password
from core.utils.datetime import utcnow


async def _login_admin(client: AsyncClient, db_session, test_admin_data):
    admin = UserModel(
        username=test_admin_data["username"].lower(),
        email=test_admin_data["email"].lower(),
        password_hash=hash_password(test_admin_data["password"]),
        role="admin",
        is_active=True,
    )
    db_session.add(admin)
    await db_session.commit()

    response = await client.post(
        "/api/auth/login",
        json={
            "username": test_admin_data["username"],
            "password": test_admin_data["password"],
        },
    )
    assert response.status_code == 200
    csrf = response.cookies.get("tvss_csrf")
    assert csrf
    return {"X-CSRF-Token": csrf}


async def _login_user(client: AsyncClient, db_session, test_user_data):
    user = UserModel(
        username=test_user_data["username"].lower(),
        email=test_user_data["email"].lower(),
        password_hash=hash_password(test_user_data["password"]),
        role="user",
        is_active=True,
    )
    db_session.add(user)
    await db_session.commit()

    response = await client.post(
        "/api/auth/login",
        json={
            "username": test_user_data["username"],
            "password": test_user_data["password"],
        },
    )
    assert response.status_code == 200
    csrf = response.cookies.get("tvss_csrf")
    assert csrf
    return {"X-CSRF-Token": csrf}, user.id


@pytest.mark.asyncio
async def test_update_status_exposes_manual_mode_by_default(client: AsyncClient, db_session, test_admin_data, monkeypatch):
    headers = await _login_admin(client, db_session, test_admin_data)
    monkeypatch.delenv("AUTO_UPDATE_ENABLED", raising=False)

    response = await client.get("/api/admin/update-status", headers=headers)
    assert response.status_code == 200
    data = response.json()
    assert data["deployment_mode"] == "manual"
    assert data["update_supported"] is False
    assert data["current_version"]


@pytest.mark.asyncio
async def test_check_update_reports_unavailable_one_click_when_updater_missing(client: AsyncClient, db_session, test_admin_data, monkeypatch):
    headers = await _login_admin(client, db_session, test_admin_data)
    monkeypatch.setenv("AUTO_UPDATE_ENABLED", "true")

    response = await client.get("/api/admin/check-update", headers=headers)
    assert response.status_code == 200
    data = response.json()
    assert "one_click_supported" in data
    assert data["one_click_supported"] is False


@pytest.mark.asyncio
async def test_perform_update_rejected_when_not_supported(client: AsyncClient, db_session, test_admin_data, monkeypatch):
    headers = await _login_admin(client, db_session, test_admin_data)
    monkeypatch.delenv("AUTO_UPDATE_ENABLED", raising=False)

    response = await client.post(
        "/api/admin/perform-update",
        json={"confirm": True, "backup_before_update": False},
        headers=headers,
    )
    assert response.status_code == 400
    assert "not available" in response.json()["detail"].lower()


@pytest.mark.asyncio
async def test_missing_update_task_returns_404(client: AsyncClient, db_session, test_admin_data):
    headers = await _login_admin(client, db_session, test_admin_data)

    response = await client.get("/api/admin/update-task/upd_missing", headers=headers)
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_check_update_uses_versioned_docker_image(client: AsyncClient, db_session, test_admin_data, monkeypatch):
    headers = await _login_admin(client, db_session, test_admin_data)

    async def fake_release_data():
        return {
            "status": "success",
            "current_version": "5.1.0",
            "latest_version": "5.5.1",
            "has_update": True,
            "docker_image": "ghcr.io/ikun52012/quantpilot-ai:v5.5.1",
            "updater_image": "ghcr.io/ikun52012/quantpilot-ai-updater:v5.5.1",
        }

    monkeypatch.setattr("routers.admin._fetch_latest_release_data", fake_release_data)

    response = await client.get("/api/admin/check-update", headers=headers)
    assert response.status_code == 200
    data = response.json()
    assert data["docker_image"].endswith(":v5.5.1")
    assert not data["docker_image"].endswith(":latest")


@pytest.mark.asyncio
async def test_update_filter_thresholds_merges_existing_values(client: AsyncClient, db_session, test_admin_data):
    headers = await _login_admin(client, db_session, test_admin_data)

    db_session.add(AdminSettingModel(key="prefilter_thresholds", value='{"atr_pct_max": 15.0, "min_pass_score": 60.0}'))
    await db_session.commit()

    response = await client.post(
        "/api/admin/filter-thresholds",
        json={"cooldown_seconds": 180},
        headers=headers,
    )
    assert response.status_code == 200

    stored = await db_session.scalar(select(AdminSettingModel).where(AdminSettingModel.key == "prefilter_thresholds"))
    assert stored is not None
    assert '"atr_pct_max": 15.0' in stored.value
    assert '"min_pass_score": 60.0' in stored.value
    assert '"cooldown_seconds": 180' in stored.value


@pytest.mark.asyncio
async def test_live_risk_update_is_rejected_and_runtime_is_restored(
    client: AsyncClient,
    db_session,
    test_admin_data,
    monkeypatch,
):
    from core.config import settings

    headers = await _login_admin(client, db_session, test_admin_data)
    original_mode = settings.risk.live_data_quality_mode
    monkeypatch.setattr(settings.exchange, "live_trading", True)
    monkeypatch.setattr(settings.risk, "live_data_quality_mode", original_mode)

    response = await client.post(
        "/api/admin/risk-thresholds",
        json={"live_data_quality_mode": "warn"},
        headers=headers,
    )

    assert response.status_code == 409
    assert settings.risk.live_data_quality_mode == original_mode
    stored = await db_session.scalar(
        select(AdminSettingModel).where(AdminSettingModel.key == "live_data_quality_mode")
    )
    assert stored is None


@pytest.mark.asyncio
async def test_order_execution_settings_reject_non_object_json(
    client: AsyncClient,
    db_session,
    test_admin_data,
):
    headers = await _login_admin(client, db_session, test_admin_data)

    response = await client.post(
        "/api/admin/order-execution-settings",
        json=[{"auto_approve_failed_orders": True}],
        headers=headers,
    )

    assert response.status_code == 400


@pytest.mark.asyncio
async def test_clear_trade_history_requires_confirmation(client: AsyncClient, db_session, test_admin_data):
    headers = await _login_admin(client, db_session, test_admin_data)
    db_session.add(TradeModel(timestamp=utcnow(), ticker="BTCUSDT", payload_json="{}"))
    await db_session.commit()

    response = await client.post("/api/admin/trades/clear", json={"confirm": False}, headers=headers)

    assert response.status_code == 400
    trades = (await db_session.execute(select(TradeModel))).scalars().all()
    assert len(trades) == 1


@pytest.mark.asyncio
async def test_clear_trade_history_deletes_only_matching_rows_and_audits(client: AsyncClient, db_session, test_admin_data):
    headers = await _login_admin(client, db_session, test_admin_data)
    old_trade = TradeModel(timestamp=utcnow() - timedelta(days=10), ticker="BTCUSDT", payload_json="{}")
    recent_trade = TradeModel(timestamp=utcnow(), ticker="ETHUSDT", payload_json="{}")
    db_session.add_all([old_trade, recent_trade])
    await db_session.commit()

    response = await client.post(
        "/api/admin/trades/clear",
        json={"confirm": True, "older_than_days": 7},
        headers=headers,
    )

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "deleted": {"trades": 1}, "older_than_days": 7}
    trades = (await db_session.execute(select(TradeModel).order_by(TradeModel.ticker))).scalars().all()
    assert [trade.ticker for trade in trades] == ["ETHUSDT"]

    audit = await db_session.scalar(
        select(AdminAuditLogModel).where(AdminAuditLogModel.action == "clear_trade_history")
    )
    assert audit is not None
    assert audit.target_type == "trades"
    assert audit.target_id == "older_than_7d"


@pytest.mark.asyncio
async def test_user_clear_history_deletes_only_own_rows(client: AsyncClient, db_session, test_user_data):
    headers, user_id = await _login_user(client, db_session, test_user_data)
    own_trade = TradeModel(user_id=user_id, timestamp=utcnow(), ticker="BTCUSDT", payload_json="{}")
    other_trade = TradeModel(user_id="other-user", timestamp=utcnow(), ticker="ETHUSDT", payload_json="{}")
    db_session.add_all([own_trade, other_trade])
    await db_session.commit()

    response = await client.post("/api/history/clear", json={"confirm": True}, headers=headers)

    assert response.status_code == 200
    assert response.json()["deleted"] == {"trades": 1}
    trades = (await db_session.execute(select(TradeModel))).scalars().all()
    assert len(trades) == 1
    assert trades[0].ticker == "ETHUSDT"


@pytest.mark.asyncio
async def test_cleanup_backups_requires_confirmation(client: AsyncClient, db_session, test_admin_data):
    headers = await _login_admin(client, db_session, test_admin_data)

    response = await client.post("/api/admin/backups/cleanup", json={"confirm": False}, headers=headers)

    assert response.status_code == 400


@pytest.mark.asyncio
async def test_cleanup_backups_audits_result(client: AsyncClient, db_session, test_admin_data, monkeypatch):
    headers = await _login_admin(client, db_session, test_admin_data)

    async def fake_cleanup_old_backups(max_backups: int = 7):
        return {"deleted": 2, "kept": max_backups}

    monkeypatch.setattr("backups.cleanup_old_backups", fake_cleanup_old_backups)

    response = await client.post("/api/admin/backups/cleanup", json={"confirm": True, "max_backups": 7}, headers=headers)

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "deleted": 2, "kept": 7, "max_backups": 7}
    audit = await db_session.scalar(
        select(AdminAuditLogModel).where(AdminAuditLogModel.action == "cleanup_backups")
    )
    assert audit is not None
    assert audit.target_type == "backup"
    assert audit.target_id == "keep_7"
