
import pytest
from httpx import AsyncClient

from core.database import UserModel
from core.security import hash_password


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
