import pytest
from httpx import AsyncClient

from core.database import UserModel
from core.security import hash_password
from tests.test_admin_updates import _login_admin


@pytest.mark.asyncio
async def test_admin_reset_password_returns_temporary_password_securely(
    client: AsyncClient,
    db_session,
    test_admin_data,
    test_user_data,
):
    """Security: Password reset returns temporary password over HTTPS with cache-control headers."""
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

    # Password MUST be returned so admin can deliver it to user
    assert "temporary_password" in data
    assert data["status"] == "success"
    assert data.get("user_id") == user.id
    assert data.get("username") == user.username
    assert len(data["temporary_password"]) >= 12  # Strong temporary password

    # Response must have cache-control headers to prevent caching
    assert "no-store" in response.headers.get("cache-control", "").lower()

    # Verify the temporary password actually works
    login = await client.post(
        "/api/auth/login",
        json={
            "username": test_user_data["username"],
            "password": data["temporary_password"],
        },
    )
    assert login.status_code == 200
