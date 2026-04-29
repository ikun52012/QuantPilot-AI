import pytest
from httpx import AsyncClient

from core.database import UserModel
from core.security import hash_password
from tests.test_admin_updates import _login_admin


@pytest.mark.asyncio
async def test_admin_reset_password_returns_temporary_password_and_allows_login(
    client: AsyncClient,
    db_session,
    test_admin_data,
    test_user_data,
):
    headers = await _login_admin(client, db_session, test_admin_data)

    user = UserModel(
        username=test_user_data["username"].lower(),
        email=test_user_data["email"].lower(),
        password_hash=hash_password(test_user_data["password"]),
        is_active=True,
    )
    db_session.add(user)
    await db_session.commit()

    response = await client.post(f"/api/admin/users/{user.id}/reset-password", headers=headers)
    assert response.status_code == 200
    data = response.json()
    assert "temporary_password" in data
    assert "log" not in data["message"].lower()

    login = await client.post(
        "/api/auth/login",
        json={
            "username": test_user_data["username"],
            "password": data["temporary_password"],
        },
    )
    assert login.status_code == 200
