/**
 * QuantPilot AI - k6 Load Test Script
 * ===================================
 * Production-ready k6 load test for the QuantPilot AI crypto trading
 * signal platform.
 *
 * Endpoints tested:
 *   - POST /webhook       (TradingView signals)
 *   - GET  /health        (Health check)
 *   - POST /api/auth/login
 *   - GET  /api/auth/me
 *   - GET  /api/positions
 *
 * How to run:
 *   k6 run tests/load/k6_script.js
 *
 * Environment variables:
 *   WEBHOOK_SECRET    Webhook secret for payload auth (default: test-webhook-secret-123)
 *   API_USERNAME      Username for API user login      (default: loadtest_user)
 *   API_PASSWORD      Password for API user login      (default: LoadTest123!)
 *   BASE_URL          Target host                        (default: http://localhost:8000)
 */

import http from "k6/http";
import { check, sleep } from "k6";
import { Trend, Rate } from "k6/metrics";

// ─────────────────────────────────────────────
// Configuration
// ─────────────────────────────────────────────

const WEBHOOK_SECRET = __ENV.WEBHOOK_SECRET || "test-webhook-secret-123";
const API_USERNAME = __ENV.API_USERNAME || "loadtest_user";
const API_PASSWORD = __ENV.API_PASSWORD || "LoadTest123!";
const BASE_URL = __ENV.BASE_URL || "http://localhost:8000";

export const options = {
  // Ramp-up: 0 -> 50 VUs in 30s, hold 2m, ramp-down to 0 in 30s
  stages: [
    { duration: "30s", target: 50 },
    { duration: "2m", target: 50 },
    { duration: "30s", target: 0 },
  ],

  // Thresholds
  thresholds: {
    // 95% of requests must complete within 500ms
    http_req_duration: ["p(95)<500"],
    // Error rate must stay below 1%
    http_req_failed: ["rate<0.01"],
  },

  // Connection reuse for better performance
  noConnectionReuse: false,
  // Disable TLS verification for local testing (remove in prod)
  insecureSkipTLSVerify: true,
};

// Custom metrics
const webhookDuration = new Trend("webhook_duration");
const apiDuration = new Trend("api_duration");
const webhookFailRate = new Rate("webhook_fail_rate");
const apiFailRate = new Rate("api_fail_rate");

// ─────────────────────────────────────────────
// Helpers
// ─────────────────────────────────────────────

const TICKERS = ["BTCUSDT", "ETHUSDT", "SOLUSDT"];
const DIRECTIONS = ["long", "short", "close_long", "close_short"];
const TIMEFRAMES = ["1", "5", "15", "60", "240", "D"];
const STRATEGIES = [
  "Crypto Quant Pro v6",
  "Smart Money Concepts",
  "AI Breakout Strategy",
  "Trend Following v2",
];

function randomChoice(arr) {
  return arr[Math.floor(Math.random() * arr.length)];
}

function makeWebhookPayload() {
  const ticker = randomChoice(TICKERS);
  const direction = randomChoice(DIRECTIONS);
  const basePrices = { BTCUSDT: 65000.0, ETHUSDT: 3500.0, SOLUSDT: 145.0 };
  const base = basePrices[ticker] || 100.0;
  const price = parseFloat((base * (0.98 + Math.random() * 0.04)).toFixed(2));

  return JSON.stringify({
    secret: WEBHOOK_SECRET,
    ticker: ticker,
    exchange: "BINANCE",
    direction: direction,
    price: price,
    timeframe: randomChoice(TIMEFRAMES),
    strategy: randomChoice(STRATEGIES),
    message: `${direction.toUpperCase()} ${ticker} @ ${price}`,
  });
}

function buildUrl(path) {
  return `${BASE_URL}${path}`;
}

// ─────────────────────────────────────────────
// Setup: create a shared auth token for API users
// ─────────────────────────────────────────────

export function setup() {
  const loginRes = http.post(
    buildUrl("/api/auth/login"),
    JSON.stringify({
      username: API_USERNAME,
      password: API_PASSWORD,
      totp_code: "",
    }),
    { headers: { "Content-Type": "application/json" } }
  );

  const loginOk = check(loginRes, {
    "login status is 200": (r) => r.status === 200,
    "login returns token": (r) => {
      try {
        return r.json("token") !== undefined;
      } catch (e) {
        return false;
      }
    },
  });

  if (!loginOk) {
    console.error("Setup login failed:", loginRes.status, loginRes.body);
    return { token: null };
  }

  return { token: loginRes.json("token") };
}

// ─────────────────────────────────────────────
// Default (main) function
// ─────────────────────────────────────────────

export default function (data) {
  // 85% webhook traffic, 10% health checks, 5% API calls
  const dice = Math.random();

  if (dice < 0.85) {
    sendWebhookSignal();
  } else if (dice < 0.95) {
    checkHealth();
  } else {
    if (data.token) {
      callAuthenticatedApi(data.token);
    } else {
      // Fallback to webhook if we have no auth token
      sendWebhookSignal();
    }
  }

  // Wait between 1s and 5s to simulate realistic pacing
  sleep(1 + Math.random() * 4);
}

// ─────────────────────────────────────────────
// Scenarios
// ─────────────────────────────────────────────

function sendWebhookSignal() {
  const payload = makeWebhookPayload();
  const res = http.post(buildUrl("/webhook"), payload, {
    headers: { "Content-Type": "application/json" },
    tags: { name: "POST /webhook" },
  });

  webhookDuration.add(res.timings.duration);

  const ok = check(res, {
    "webhook status is 202": (r) => r.status === 202,
    "webhook accepted": (r) => {
      try {
        return r.json("status") === "accepted";
      } catch (e) {
        return false;
      }
    },
  });

  webhookFailRate.add(!ok);
}

function checkHealth() {
  const res = http.get(buildUrl("/health/quick"), {
    tags: { name: "GET /health/quick" },
  });

  const ok = check(res, {
    "health status is 200": (r) => r.status === 200,
    "health is healthy": (r) => {
      try {
        return r.json("status") === "healthy";
      } catch (e) {
        return false;
      }
    },
  });

  if (!ok) {
    console.warn("Health check failed:", res.status, res.body);
  }
}

function callAuthenticatedApi(token) {
  const headers = {
    Authorization: `Bearer ${token}`,
    "Content-Type": "application/json",
  };

  // GET /api/auth/me
  const meRes = http.get(buildUrl("/api/auth/me"), {
    headers: headers,
    tags: { name: "GET /api/auth/me" },
  });

  apiDuration.add(meRes.timings.duration);

  const meOk = check(meRes, {
    "GET /api/auth/me status is 200": (r) => r.status === 200,
    "GET /api/auth/me returns user": (r) => {
      try {
        return r.json("username") !== undefined;
      } catch (e) {
        return false;
      }
    },
  });

  apiFailRate.add(!meOk);

  if (meRes.status === 401) {
    console.warn("Token expired or invalid during /api/auth/me");
  }

  // GET /api/positions
  const posRes = http.get(buildUrl("/api/positions"), {
    headers: headers,
    tags: { name: "GET /api/positions" },
  });

  apiDuration.add(posRes.timings.duration);

  const posOk = check(posRes, {
    "GET /api/positions status is 200": (r) => r.status === 200,
  });

  apiFailRate.add(!posOk);

  if (posRes.status === 401) {
    console.warn("Token expired or invalid during /api/positions");
  }
}
