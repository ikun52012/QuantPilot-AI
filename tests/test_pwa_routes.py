"""Tests for PWA and compatibility routes."""

from contextlib import asynccontextmanager

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker


@asynccontextmanager
async def _noop_lifespan(app):
    yield


@pytest.fixture
def sync_client(db_session):
    from core.database import db_manager, get_db
    from core.factory import create_app

    app = create_app()
    app.router.lifespan_context = _noop_lifespan

    async def override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = override_get_db
    db_manager.async_session_factory = async_sessionmaker(db_session.bind, expire_on_commit=False)
    db_manager.engine = db_session.bind

    with TestClient(app) as client:
        yield client

    app.dependency_overrides.clear()
    db_manager.async_session_factory = None
    db_manager.engine = None


class TestServiceWorkerRoute:
    def test_sw_js_served_from_root_with_headers(self, sync_client: TestClient):
        response = sync_client.get("/sw.js")

        assert response.status_code == 200
        assert response.headers["service-worker-allowed"] == "/"
        assert response.headers["cache-control"] == "no-store, no-cache, must-revalidate, max-age=0"

    def test_sw_js_uses_network_first_for_dashboard_bundle(self, sync_client: TestClient):
        response = sync_client.get("/sw.js")

        assert response.status_code == 200
        assert "const NETWORK_FIRST_PATHS = new Set([" in response.text
        assert "'/static/app.js'" in response.text
        assert "'/static/style.css'" in response.text
        assert "handleNetworkFirstRequest" in response.text

    def test_sw_js_does_not_precache_dashboard_shell_routes(self, sync_client: TestClient):
        response = sync_client.get("/sw.js")

        assert response.status_code == 200
        assert "  '/'," not in response.text
        assert "  '/dashboard'," not in response.text


class TestCompatibilityRedirects:
    def test_share_get_redirects_with_query_before_hash(self, sync_client: TestClient):
        response = sync_client.get(
            "/share",
            params={"title": "Alpha", "text": "Risk on", "url": "https://example.com/signal"},
            follow_redirects=False,
        )

        assert response.status_code == 303
        assert response.headers["location"] == (
            "/dashboard?title=Alpha&text=Risk+on&url=https%3A%2F%2Fexample.com%2Fsignal#social"
        )

    def test_share_post_form_redirects_without_multipart_dependency(self, sync_client: TestClient):
        response = sync_client.post(
            "/share",
            data={"title": "Alpha", "text": "Risk on", "url": "https://example.com/signal"},
            follow_redirects=False,
        )

        assert response.status_code == 303
        assert response.headers["location"] == (
            "/dashboard?title=Alpha&text=Risk+on&url=https%3A%2F%2Fexample.com%2Fsignal#social"
        )

    def test_share_post_json_redirects(self, sync_client: TestClient):
        response = sync_client.post(
            "/share",
            json={"title": "Alpha", "text": "Risk on", "url": "https://example.com/signal"},
            follow_redirects=False,
        )

        assert response.status_code == 303
        assert response.headers["location"] == (
            "/dashboard?title=Alpha&text=Risk+on&url=https%3A%2F%2Fexample.com%2Fsignal#social"
        )

    def test_signal_redirects_with_query_before_hash(self, sync_client: TestClient):
        response = sync_client.get("/signal", params={"data": "tvsignal://open?id=42"}, follow_redirects=False)

        assert response.status_code == 303
        assert response.headers["location"] == "/dashboard?data=tvsignal%3A%2F%2Fopen%3Fid%3D42#dashboard"


class TestDashboardShell:
    def test_dashboard_html_versions_frontend_assets(self, sync_client: TestClient):
        register = sync_client.post(
            "/api/auth/register",
            json={
                "username": "dashuser",
                "email": "dash@example.com",
                "password": "Str0ng!Pass123",
            },
        )
        assert register.status_code == 200

        response = sync_client.get("/dashboard")

        assert response.status_code == 200
        assert '/static/style.css?v=' in response.text
        assert '/static/js/qp-core.js?v=' in response.text
        assert '/static/js/charts.js?v=' in response.text
        assert '/static/app.js?v=' in response.text
        assert "updateViaCache: 'none'" in response.text

    def test_manifest_exposes_share_and_protocol_handlers(self, sync_client: TestClient):
        response = sync_client.get("/static/manifest.json")

        assert response.status_code == 200
        manifest = response.json()
        assert manifest["start_url"] == "/dashboard"
        assert manifest["share_target"]["action"] == "/share"
        assert manifest["share_target"]["params"] == {"title": "title", "text": "text", "url": "url"}
        assert manifest["protocol_handlers"] == [{"protocol": "web+tvsignal", "url": "/signal?data=%s"}]


class TestWebSocketCookieFallback:
    def test_positions_socket_accepts_auth_cookie(self, sync_client: TestClient):
        register = sync_client.post(
            "/api/auth/register",
            json={
                "username": "socketuser",
                "email": "socket@example.com",
                "password": "Str0ng!Pass123",
            },
        )
        assert register.status_code == 200

        with sync_client.websocket_connect("/ws/positions") as websocket:
            connected = websocket.receive_json()
            assert connected["type"] == "connected"

            websocket.send_json({"type": "ping"})
            pong = websocket.receive_json()
            assert pong["type"] == "pong"
