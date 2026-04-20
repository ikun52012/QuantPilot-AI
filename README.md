# 📡 TradingView AI Signal Server (v4.0)

![System Status](https://img.shields.io/badge/status-active-success) ![Python](https://img.shields.io/badge/python-3.10%2B-blue) ![FastAPI](https://img.shields.io/badge/FastAPI-0.136-009688) ![Docker](https://img.shields.io/badge/docker-compose-2496ED)

**TradingView Signal Server** is a production-grade cryptocurrency quantitative trading integration platform. It combines TradingView's Webhook signal mechanism and advanced filtering rules with powerful AI models (OpenAI GPT, Anthropic Claude, DeepSeek, or custom LLMs) to perform secondary artificial intelligence decision-making. Finally, it automates order placement and execution on mainstream crypto exchanges like Binance and OKX.

This system is equipped with a stunning "Midnight Glassmorphism" interactive frontend dashboard. It features a complete multi-tenant/multi-user role-based access control system and a USDT subscription payment pipeline, making it ready to be deployed commercially as a complete SaaS quantitative advisory platform.

---

## ✨ Core Features

- **🤖 Invincible AI Trading Analysis Pipeline**
  - Built-in integration with OpenAI (GPT-4o), Anthropic (Claude 3.5 Sonnet), DeepSeek, and supports fully custom OpenAI-compatible endpoints.
  - The AI performs secondary risk assessment based on market depth and direction context, identifying false breakouts and choppy markets. It automatically recommends optimal take-profit tiers and adaptive stop-loss points.
- **🛡️ 15-Layer Pre-Filter System**
  - Features an extremely strict preliminary Webhook signal processor to prevent entries during malicious market conditions like whale manipulation or black swan high-volatility events.
  - Supports circuit breakers for maximum daily trades and maximum daily account drawdown.
- **💸 Robust Multi-Tenant Architecture & Crypto Payment System**
  - Complete and secure JWT session control with independent user dashboards and a global Super Admin Dashboard.
  - Create and manage paid subscription plans. Features built-in multi-chain USDT transaction hash verification (TRC20, ERC20, BEP20, Solana, etc.), invite codes, and a free-trial ecosystem for a closed-loop business model.
- **⚡ Multi-Exchange Live & Paper Trading Engine (Powered by ccxt)**
  - Out-of-the-box support for Binance, OKX, Bybit, Bitget, Gate.io, and Coinbase.
  - Fully controlled via environment variables or individual user settings for Paper Trading (local simulated records) and Live Trading (high-concurrency real order placements).
- **🎯 Smart Tiered Risk Management & Trailing Stops (Multi-TP)**
  - Customize up to 4 sequential stages (TP1 to TP4) of tiered position closing to secure bounce profits.
  - In-house developed smart trailing stop module that steps up the hard stop-loss based on percentage steps, letting your profits run.
- **📱 Real-time Telegram Notifications**
  - From receiving a signal, triggering pre-filter blocks, AI smart analysis, to exchange order execution, all pipeline events are broadcasted in real-time to your Telegram Bot.

---

## 🏗️ Architecture & Signal Lifecycle

```mermaid
graph LR
A[TradingView Webhook] --> B(15-Layer Pre-Filter)
B --> |Passed| C{AI Strategy & Secondary Decision}
B --> |Rejected| F[Local Logs & Telegram Notification]
C --> |Trade Approved| D[Exchange API Order Execution]
C --> |Trade Rejected| F
D --> E[Real-time Multi-TP & Trailing Stop Mounting]
D --> G[Full-pipeline Telegram Broadcast]
```

---

## 🎨 Cutting-edge Interactive Dashboard (Midnight Glassmorphism)

We've completely overhauled the "boring financial backend" stereotype. The dashboard implements a **Midnight Glassmorphism** design aesthetic:
- **Deep, dreamy cyberpunk abyss interface** paired with dynamic colorful micro-animation particles, showcasing your quant-geek taste.
- **Spotlight Hover micro-interaction system**.
- Seamless real-time responsive modern UI (mobile and web compatible) for maximum operational comfort.

---

## 🚀 Quick Start Guide

### 1. Prerequisites
Before starting your money-making machine, ensure your server terminal has the following:
- **Python 3.10+**
- **Docker & Docker Compose** (Recommended deployment method).
- A TradingView account (Any tier, but paid tiers are recommended to create Webhooks).

Use **Python 3.10+** for local installs. Docker uses Python 3.12 by default and is the recommended path on Windows, especially if your local machine only has a 32-bit Python interpreter.

### 2. Local Source Deployment (For Custom Development)

```bash
# 1. Clone the repository
git clone https://github.com/your-organization/signal-server.git
cd signal-server

# 2. Install core Python dependencies (venv recommended)
pip install -r requirements.txt

# 3. Configure your pipeline
cp .env.example .env
nano .env # Setup API keys for Exchanges, AI, Telegram Bot, etc.

# 4. Ignite! 🔥
python main.py
# Set UVICORN_RELOAD=true only for local auto-reload development.
```
Visit `http://0.0.0.0:8000` locally or on your LAN to access the quant dashboard!

Open:

- Homepage: `http://localhost:8000/`
- Login: `http://localhost:8000/login`
- Dashboard: `http://localhost:8000/dashboard`

Default first-deployment login:

```text
Username: admin
Password: 123456
```

Change `DEFAULT_ADMIN_PASSWORD` and `JWT_SECRET` before exposing the service to the internet. `WEBHOOK_SECRET` can be left empty; the app will generate a persistent admin webhook secret and show it in Admin Settings. Keep `UVICORN_RELOAD=false` in deployments.

### 3. One-Click Docker Deployment
When you are ready to deploy it as a 24/7 cloud miner:

```bash
# After configuring your .env file
docker-compose up -d --build

# Monitor live terminal logs
docker-compose logs -f
```
_Note: Generated SQLite databases and critical logs will be persistently mapped to `./data` and `./logs`._

---

## ⚙️ Core `.env` Configuration Guide

Here are the high-frequency variables you must pay attention to:
*   **`AI_PROVIDER`**: Model integration base. Valid fields are `openai`, `anthropic`, `deepseek`, or `custom` for your own base (if `custom`, ensure you fill out the related custom fields below it).
*   **`EXCHANGE`**: Default platform egress, like `binance`. If operating as a SaaS provider, tenant users can also enter their own Exchange Keys in their web dashboard.
*   **`LIVE_TRADING`**: Critical! Set to `true` for production live trading, and strictly keep it `false` for paper trading during sandbox strategy testing.
*   **`JWT_SECRET`** & **`WEBHOOK_SECRET`**: The authorization lifelines of the entire server. **Must be set to unbreakable, random, ultra-long hashes!**
*   **`DEFAULT_ADMIN_PASSWORD`**: The default super-admin password generated on first run (Default: 123456). Please change this immediately after your first successful login.

---

## 📬 TradingView Webhook Integration

1. Craft your high-win-rate strategy chart in TradingView.
2. Open the "Create Alert" dialog.
3. Under the Notifications tab, check `Webhook URL` -> Enter your HTTPS endpoint, for example `https://<your-domain>/webhook`.
4. The "Message" box requires a structured JSON Payload. To view your specific authenticated Payload template, please log into the platform and check your User Settings dashboard.

Minimal long signal:

```json
{
  "secret": "copy-from-dashboard",
  "ticker": "{{ticker}}",
  "exchange": "{{exchange}}",
  "direction": "long",
  "price": {{close}},
  "timeframe": "{{interval}}",
  "strategy": "{{strategy.order.comment}}",
  "message": "{{strategy.order.action}} {{ticker}} @ {{close}}",
  "bar_time": "{{time}}"
}
```

For short signals, change only `"direction": "short"`.

The server records webhook events and ignores duplicate payload fingerprints within the retry window, which helps prevent repeated TradingView deliveries from placing duplicate orders.

---

## Production Hardening

- Runtime admin secrets, webhook secrets, and per-user exchange keys are encrypted at rest with `APP_ENCRYPTION_KEY`. If it is omitted, the app generates a persistent key in `data/app_encryption.key`; back this file up and keep the `data/` volume mounted permanently.
- Per-user webhook lookup uses a stored hash, so the dashboard can show each user's real secret while the database index does not keep the raw value.
- Browser write APIs use a double-submit CSRF token in addition to the HttpOnly session cookie.
- Leave `PUBLIC_BASE_URL` empty to auto-detect the current domain from request/proxy headers. Set it only if auto-detection is wrong, such as `https://cs.hyzcjs.com`.
- Set `COOKIE_SECURE=true` when deploying behind HTTPS.
- Docker Compose binds the app to `127.0.0.1:8000` by default. Expose it through Nginx, Caddy, Cloudflare Tunnel, or another HTTPS reverse proxy.
- Trade logs are written to SQLite for long-term querying while legacy JSON logs remain readable.
- Admin actions are recorded in an audit log and displayed in the Admin System panel.
- Payment TX hashes are checked for duplicate submission before admin confirmation. Admins can also run best-effort on-chain verification for TRC20, ERC20, BEP20, and Arbitrum from the Pending Payments panel. Aptos is detected but staged for manual/indexer review.
- Advanced trailing modes are monitored by a scheduled position monitor. Set `POSITION_MONITOR_INTERVAL_SECS` to tune the scan interval, and review the Position Monitor panel after enabling live trading.
- Backups can be created from the Admin Backup panel. Always keep `data/app_encryption.key` or `APP_ENCRYPTION_KEY`; encrypted secrets cannot be recovered without it.

### Commercial Operation Notes

- Each user has isolated exchange keys, webhook secret, TP settings, trade history, and performance charts.
- Admins can decide whether a user may enable live trading, plus set max leverage and max position percentage caps.
- Exchange TP/SL order parameters are tried through exchange-aware candidates. Always test each target exchange with a small paper/live pilot before trusting automation with real size.
- Webhook Diagnostics shows recent invalid secrets, duplicate alerts, pre-filter blocks, AI rejects, and executed signals so TradingView issues can be traced from the dashboard.
- For fully unattended payments, configure explorer API keys in `.env` and still keep manual review available for chain/API outages.

---

## 🛡️ Disclaimer & Risk Warning

**Please review this declaration carefully before launching:**
Deploying and running automated quantitative trading for futures or spot markets is an **extremely high-risk operation**. This project serves as an open structured routing hub and AI empowerment tool. All commands executed through this tool **do not constitute, nor are they equivalent to, any financial or investment advice**. The developers and contributors of this codebase **assume no liability whatsoever** for any asset liquidations, slippage blowouts, or total capital losses caused by exchange API outages, network jitter, or rare AI hallucinations. We strongly advise all users to maintain long-term paper trading using `LIVE_TRADING=false` before injecting real capital.

> *All Trading Involves Absolute Risk. Code your own destiny.* ☕
