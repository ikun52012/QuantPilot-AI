import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from starlette.requests import Request

from core.config import DATA_DIR, settings
from routers import health as health_module
from routers.health import HealthCheckResult


def _request(path: str) -> Request:
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": path,
            "query_string": b"",
            "headers": [],
            "client": ("127.0.0.1", 12345),
            "server": ("127.0.0.1", 8000),
            "scheme": "http",
        }
    )


def test_pytest_runtime_data_is_not_the_production_data_directory():
    production_data = (Path(__file__).resolve().parents[1] / "data").resolve(strict=False)

    assert str(DATA_DIR) == os.environ.get("DATA_DIR")
    assert DATA_DIR.resolve(strict=False) != production_data


@pytest.mark.asyncio
async def test_readiness_fails_when_exchange_market_data_is_unavailable(monkeypatch):
    from core import lifespan

    health_module._HEALTH_RATE_LIMIT.clear()
    monkeypatch.setattr(settings.exchange, "live_trading", False)
    monkeypatch.setattr(lifespan, "_scheduler", SimpleNamespace(running=True))
    monkeypatch.setattr(
        health_module,
        "check_database",
        AsyncMock(return_value=HealthCheckResult(status="healthy")),
    )
    monkeypatch.setattr(
        health_module,
        "check_exchange_api",
        AsyncMock(return_value=HealthCheckResult(status="unhealthy", error="offline")),
    )
    monkeypatch.setattr(
        health_module,
        "check_ai_api",
        AsyncMock(return_value=HealthCheckResult(status="healthy")),
    )

    response = await health_module.readiness_check(_request("/health/ready"))
    body = json.loads(response.body)

    assert response.status_code == 503
    assert body["status"] == "not_ready"
    assert body["trading_ready"] is False


@pytest.mark.asyncio
async def test_custom_ai_health_requires_url_and_key(monkeypatch):
    monkeypatch.setattr(settings.ai, "provider", "custom")
    monkeypatch.setattr(settings.ai, "custom_provider_api_url", "")
    monkeypatch.setattr(settings.ai, "custom_provider_api_key", "")

    result = await health_module.check_ai_api()

    assert result.status == "degraded"
    assert "not configured" in str(result.error)
