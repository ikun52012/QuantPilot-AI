"""
QuantPilot AI - Locust Load Test Script
========================================
Production-ready Locust load test for the QuantPilot AI crypto trading
signal platform.

Endpoints tested:
  - POST /webhook       (TradingView signals)
  - GET  /health        (Health check)
  - POST /api/auth/login
  - GET  /api/auth/me
  - GET  /api/positions

Usage:
  locust -f tests/load/locustfile.py --host http://localhost:8000

Environment variables:
  WEBHOOK_SECRET    Webhook secret for payload auth (default: test-webhook-secret-123)
  API_USERNAME      Username for API user login      (default: loadtest_user)
  API_PASSWORD      Password for API user login      (default: LoadTest123!)
"""
from __future__ import annotations

import os
import random
from typing import Any

from locust import FastHttpUser, between, task

# ─────────────────────────────────────────────
# Configuration
# ─────────────────────────────────────────────

WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "test-webhook-secret-123")
API_USERNAME = os.getenv("API_USERNAME", "loadtest_user")
API_PASSWORD = os.getenv("API_PASSWORD", "LoadTest123!")

TICKERS = ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
DIRECTIONS = ["long", "short", "close_long", "close_short"]
TIMEFRAMES = ["1", "5", "15", "60", "240", "D"]
STRATEGIES = [
    "Crypto Quant Pro v6",
    "Smart Money Concepts",
    "AI Breakout Strategy",
    "Trend Following v2",
]


def _make_webhook_payload() -> dict[str, Any]:
    """Generate a realistic TradingView webhook payload."""
    ticker = random.choice(TICKERS)
    direction = random.choice(DIRECTIONS)
    # Generate a realistic price based on ticker
    base_prices = {"BTCUSDT": 65000.0, "ETHUSDT": 3500.0, "SOLUSDT": 145.0}
    base = base_prices.get(ticker, 100.0)
    price = round(base * random.uniform(0.98, 1.02), 2)

    return {
        "secret": WEBHOOK_SECRET,
        "ticker": ticker,
        "exchange": "BINANCE",
        "direction": direction,
        "price": price,
        "timeframe": random.choice(TIMEFRAMES),
        "strategy": random.choice(STRATEGIES),
        "message": f"{direction.upper()} {ticker} @ {price}",
    }


# ─────────────────────────────────────────────
# User Behaviours
# ─────────────────────────────────────────────

class WebhookSignalUser(FastHttpUser):
    """
    Simulates TradingView sending webhook signals.
    Heavy weight (10) because webhook traffic dominates real usage.
    """

    weight = 10
    wait_time = between(1, 5)

    @task(8)
    def send_webhook_signal(self) -> None:
        """Send a realistic TradingView signal to /webhook."""
        payload = _make_webhook_payload()
        with self.client.post(
            "/webhook",
            json=payload,
            catch_response=True,
            name="POST /webhook",
        ) as response:
            if response.status_code == 202:
                response.success()
            elif response.status_code == 401:
                response.failure("Webhook rejected: invalid secret")
            elif response.status_code == 400:
                response.failure(f"Webhook rejected: bad request ({response.text})")
            else:
                response.failure(
                    f"Unexpected status {response.status_code}: {response.text}"
                )

    @task(2)
    def check_health(self) -> None:
        """Lightweight health check to verify server availability."""
        with self.client.get(
            "/health/quick",
            catch_response=True,
            name="GET /health/quick",
        ) as response:
            if response.status_code == 200:
                response.success()
            else:
                response.failure(
                    f"Health check failed: {response.status_code}"
                )


class ApiUser(FastHttpUser):
    """
    Simulates authenticated dashboard users.
    Light weight (1) because API browsing is rarer than signals.
    """

    weight = 1
    wait_time = between(1, 5)

    def __init__(self, environment) -> None:
        super().__init__(environment)
        self._token: str | None = None

    def on_start(self) -> None:
        """Authenticate before running tasks and store JWT token."""
        self._login()

    def _login(self) -> None:
        """Perform login and store bearer token for subsequent requests."""
        try:
            with self.client.post(
                "/api/auth/login",
                json={
                    "username": API_USERNAME,
                    "password": API_PASSWORD,
                    "totp_code": "",
                },
                catch_response=True,
                name="POST /api/auth/login",
            ) as response:
                if response.status_code != 200:
                    response.failure(
                        f"Login failed: {response.status_code} - {response.text}"
                    )
                    return

                data = response.json()
                self._token = data.get("token")
                if not self._token:
                    response.failure("Login response missing token")
        except Exception as exc:
            self.environment.events.request.fire(
                request_type="POST",
                name="POST /api/auth/login",
                response_time=0,
                response_length=0,
                exception=exc,
            )

    def _auth_headers(self) -> dict[str, str]:
        """Return Authorization header if token is available."""
        if self._token:
            return {"Authorization": f"Bearer {self._token}"}
        return {}

    @task(3)
    def get_me(self) -> None:
        """Fetch current user profile (/api/auth/me)."""
        with self.client.get(
            "/api/auth/me",
            headers=self._auth_headers(),
            catch_response=True,
            name="GET /api/auth/me",
        ) as response:
            if response.status_code == 200:
                response.success()
            elif response.status_code == 401:
                response.failure("Unauthorized: token expired or invalid")
                # Attempt re-login once
                self._login()
            else:
                response.failure(
                    f"Get me failed: {response.status_code} - {response.text}"
                )

    @task(2)
    def get_positions(self) -> None:
        """Fetch open positions (/api/positions)."""
        with self.client.get(
            "/api/positions",
            headers=self._auth_headers(),
            catch_response=True,
            name="GET /api/positions",
        ) as response:
            if response.status_code == 200:
                response.success()
            elif response.status_code == 401:
                response.failure("Unauthorized: token expired or invalid")
                self._login()
            else:
                response.failure(
                    f"Get positions failed: {response.status_code} - {response.text}"
                )

    @task(1)
    def check_health(self) -> None:
        """Lightweight health check."""
        with self.client.get(
            "/health/quick",
            catch_response=True,
            name="GET /health/quick (api)",
        ) as response:
            if response.status_code == 200:
                response.success()
            else:
                response.failure(
                    f"Health check failed: {response.status_code}"
                )
