# QuantPilot AI - Deployment Guide

> Version: v4.4.0 | Last Updated: 2026-04

---

## Table of Contents

1. [System Requirements](#1-system-requirements)
2. [Quick Start (Local Development)](#2-quick-start-local-development)
3. [Production Deployment (Docker)](#3-production-deployment-docker)
4. [Production Deployment (Bare Metal)](#4-production-deployment-bare-metal)
5. [Environment Variables Configuration](#5-environment-variables-configuration)
6. [Troubleshooting](#6-troubleshooting)
7. [Security Checklist](#7-security-checklist)
8. [New Features in v4.4.0](#8-new-features-in-v440)

---

## 1. System Requirements

| Component | Minimum Version | Notes |
|-----------|-----------------|-------|
| Python | **3.10+** | Required for `X\|Y` union type syntax |
| pip | 21.0+ | - |
| Docker | 24.0+ | Docker deployment only |
| Docker Compose | 2.20+ | Docker deployment only |
| SQLite | 3.37+ | Default database (bundled with Python) |
| PostgreSQL | 14+ | Recommended for production |
| Redis | 6.0+ | Optional, falls back to in-memory cache |

**Hardware Recommendations (Production):**
- CPU: 2+ cores
- Memory: 512MB+ (1GB+ recommended with AI analysis)
- Disk: 10GB+ (database + logs)

---

## 2. Quick Start (Local Development)

### 2.1 Clone and Enter Directory

```bash
git clone https://github.com/your-repo/QuantPilot-AI.git
cd QuantPilot-AI
```

### 2.2 Install Dependencies

```bash
# Recommended: use virtual environment
python3 -m venv venv
source venv/bin/activate        # Linux/macOS
# or
.\venv\Scripts\Activate.ps1     # Windows PowerShell

pip install -r requirements.txt
```

### 2.3 Create Configuration File

```bash
cp .env.example .env
# Edit .env, fill at least these fields:
#   JWT_SECRET=<random 32-byte hex string>
#   WEBHOOK_SECRET=<your TradingView webhook password>
```

Generate secure keys:

```bash
python -c "import secrets; print(secrets.token_hex(32))"
```

### 2.4 Environment Check (Recommended)

```bash
python scripts/check_env.py
```

After all checks pass:

### 2.5 Start the Server

**Windows:**
```powershell
.\scripts\start.ps1
# or manually:
$env:PYTHONIOENCODING="utf-8"; python -m uvicorn app:app --host 0.0.0.0 --port 8000
```

**Linux/macOS:**
```bash
bash scripts/start.sh
# or manually:
PYTHONIOENCODING=utf-8 uvicorn app:app --host 0.0.0.0 --port 8000
```

### 2.6 Verify Service

```bash
curl http://localhost:8000/health
# Expected: {"status":"healthy","version":"4.4.0","database":"ok","cache":"ok"}
```

Open browser at `http://localhost:8000`, login with default account:
- Username: `admin`
- Password: `123456` (change immediately after first login!)

---

## 3. Production Deployment (Docker)

### 3.1 Prerequisites

```bash
# Ensure .env file is configured (see Section 5)
cp .env.example .env
# Set strong passwords
echo "POSTGRES_PASSWORD=$(openssl rand -hex 16)" >> .env
echo "JWT_SECRET=$(openssl rand -hex 32)" >> .env
echo "WEBHOOK_SECRET=$(openssl rand -hex 16)" >> .env
```

### 3.2 Build and Start

```bash
docker compose up -d --build
```

### 3.3 View Startup Logs

```bash
docker compose logs -f signal-server
```

### 3.4 Verify Service

```bash
curl http://localhost:8000/health
```

### 3.5 Service Management

```bash
# Stop all services
docker compose down

# Restart application (without rebuilding database)
docker compose restart signal-server

# Rebuild after code update
docker compose up -d --build signal-server

# View all container status
docker compose ps
```

### 3.6 Data Backup

```bash
# Backup SQLite database (if using SQLite)
docker compose exec signal-server cp /app/data/server.db /app/data/backups/server_$(date +%Y%m%d).db

# Backup PostgreSQL data
docker compose exec postgres pg_dump -U signal signal_server > backup_$(date +%Y%m%d).sql
```

---

## 4. Production Deployment (Bare Metal)

For servers without Docker.

### 4.1 Install Python 3.10+

```bash
# Ubuntu/Debian
sudo apt update
sudo apt install python3.12 python3.12-venv python3-pip -y

# CentOS/RHEL
sudo dnf install python3.12 -y
```

### 4.2 Create System User

```bash
sudo useradd --system --no-create-home --shell /bin/false signal
sudo mkdir -p /opt/signal-server
sudo chown signal:signal /opt/signal-server
```

### 4.3 Deploy Code

```bash
sudo cp -r . /opt/signal-server/
cd /opt/signal-server
sudo -u signal python3.12 -m venv venv
sudo -u signal ./venv/bin/pip install -r requirements.txt
```

### 4.4 Configure systemd Service

Create `/etc/systemd/system/signal-server.service`:

```ini
[Unit]
Description=QuantPilot AI
After=network.target postgresql.service

[Service]
Type=simple
User=signal
Group=signal
WorkingDirectory=/opt/signal-server
EnvironmentFile=/opt/signal-server/.env
Environment=PYTHONIOENCODING=utf-8
Environment=PYTHONUTF8=1
ExecStart=/opt/signal-server/venv/bin/uvicorn app:app --host 127.0.0.1 --port 8000 --workers 1
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable signal-server
sudo systemctl start signal-server
sudo systemctl status signal-server
```

### 4.5 Configure Nginx Reverse Proxy

```nginx
server {
    listen 80;
    server_name your-domain.com;
    return 301 https://$host$request_uri;
}

server {
    listen 443 ssl http2;
    server_name your-domain.com;

    ssl_certificate     /etc/letsencrypt/live/your-domain.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/your-domain.com/privkey.pem;

    location / {
        proxy_pass         http://127.0.0.1:8000;
        proxy_set_header   Host              $host;
        proxy_set_header   X-Real-IP         $remote_addr;
        proxy_set_header   X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header   X-Forwarded-Proto $scheme;
        proxy_read_timeout 90s;
    }

    # WebSocket support
    location /ws/ {
        proxy_pass         http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header   Upgrade $http_upgrade;
        proxy_set_header   Connection "upgrade";
        proxy_set_header   Host $host;
        proxy_read_timeout 86400;
    }
}
```

---

## 5. Environment Variables Configuration

### 5.1 Required Variables

| Variable | Description | Example |
|----------|-------------|---------|
| `JWT_SECRET` | JWT signing key (32-byte hex) | `openssl rand -hex 32` |
| `WEBHOOK_SECRET` | TradingView webhook password | Custom string |
| `DATABASE_URL` | Database connection string | See below |

### 5.2 Database Configuration

```bash
# SQLite (development/small production)
DATABASE_URL=sqlite+aiosqlite:///./data/server.db

# PostgreSQL (recommended for production)
DATABASE_URL=postgresql+asyncpg://user:password@host:5432/database
```

### 5.3 Exchange Configuration

```bash
EXCHANGE=binance              # binance / okx / bybit / bitget / gate / coinbase
EXCHANGE_API_KEY=xxx
EXCHANGE_API_SECRET=xxx
EXCHANGE_PASSWORD=xxx         # Required for some exchanges (e.g., OKX)
LIVE_TRADING=false            # Must explicitly set to true for live trading
EXCHANGE_SANDBOX_MODE=false
```

### 5.4 AI Configuration

```bash
AI_PROVIDER=deepseek          # openai / anthropic / deepseek / openrouter / custom
DEEPSEEK_API_KEY=xxx
# or
OPENAI_API_KEY=xxx
# or
ANTHROPIC_API_KEY=xxx
```

### 5.5 Telegram Notifications (Optional)

```bash
TELEGRAM_BOT_TOKEN=xxx
TELEGRAM_CHAT_ID=xxx
```

### 5.6 Redis (Optional)

```bash
REDIS_ENABLED=true
REDIS_URL=redis://localhost:6379/0
REDIS_TTL=300
```

### 5.7 Trading Control (New in v4.4.0)

```bash
POSITION_MONITOR_INTERVAL_SECS=60    # Position monitor check interval
```

---

## 6. Troubleshooting

### Q: Startup error `SettingsError: error parsing value for field "exchange"`

**Cause**: Older `core/config.py` with `BaseSettings` was misparsed by pydantic-settings v2.14+.

**Solution**: Fixed. `Settings` class now uses `BaseModel` + `from_env()` factory method. Verify `core/config.py` ends with:
```python
settings = Settings.from_env()
```

---

### Q: Windows console `UnicodeEncodeError: 'gbk' codec can't encode character`

**Cause**: loguru logs contain emoji, Windows GBK terminal cannot encode.

**Solution**: Fixed. `app.py` console handler uses UTF-8 wrapper. Set:
```powershell
$env:PYTHONIOENCODING = "utf-8"
$env:PYTHONUTF8 = "1"
```

---

### Q: `ModuleNotFoundError: No module named 'passlib'` or `'jose'`

**Solution**:
```bash
pip install "passlib[bcrypt]" "python-jose[cryptography]"
```
These packages are in `requirements.txt`, run `pip install -r requirements.txt`.

---

### Q: Database directory missing, SQLite cannot create file

**Solution**:
```bash
mkdir -p data/backups logs trade_logs
```
Startup script creates these automatically.

---

### Q: `/health` returns `database: error`

**Check steps**:
1. Verify `DATABASE_URL` is correct
2. For PostgreSQL: verify service running, user permissions correct
3. Check logs: `docker compose logs signal-server` or `logs/server_*.log`

---

### Q: Docker container exits immediately after startup

```bash
docker compose logs signal-server
```
Common causes:
- Missing required variables in `.env`
- PostgreSQL health check failed (insufficient wait time)
- Dependency installation failed

---

### Q: Python 3.9 error `TypeError: unsupported type`

Project uses Python 3.10+ native type hint syntax (`list[str]`, `X | Y`), must upgrade Python.

---

### Q: WebSocket connection fails

**Solution**: Ensure Nginx reverse proxy has WebSocket upgrade headers configured:
```nginx
proxy_http_version 1.1;
proxy_set_header Upgrade $http_upgrade;
proxy_set_header Connection "upgrade";
```

---

## 7. Security Checklist

Verify each item before production deployment:

- [ ] `JWT_SECRET` uses at least 32-byte random value (`openssl rand -hex 32`)
- [ ] `WEBHOOK_SECRET` is set and matches TradingView alert configuration
- [ ] `DEFAULT_ADMIN_PASSWORD` changed (or change immediately after first login)
- [ ] `LIVE_TRADING=false` (unless confirmed for live trading, explicitly set `true`)
- [ ] PostgreSQL password not using default `signal`
- [ ] Port `8000` not exposed to public, access via Nginx/reverse proxy
- [ ] HTTPS configured (Let's Encrypt or commercial certificate)
- [ ] Log directory has rotation configured (100MB / 30 days)
- [ ] Regular backups of `data/` directory
- [ ] Two-factor authentication (TOTP) enabled for admin accounts

---

## 8. New Features in v4.4.0

### DCA Strategy Engine
- Dollar-cost averaging with configurable entry spacing
- Multiple sizing methods: fixed, martingale, geometric, fibonacci
- Automatic stop-loss and take-profit management

### Grid Trading Strategy
- Arithmetic and geometric grid spacing
- Auto-replenish grid levels
- PnL tracking per grid level

### Backtest Engine
- Historical strategy simulation with realistic execution
- Multiple strategies: Simple Trend, SMC/FVG, AI Assistant
- Comprehensive performance metrics (Sharpe, Sortino, max drawdown)

### WebSocket Real-time Updates
- Position updates streaming
- Price alerts and market data
- System status monitoring (admin only)

### Social Signal Sharing
- Community signal sharing and subscription
- Auto-execute subscribed signals
- Signal performance tracking

### Trading Control
- Global kill-switch for emergency stops
- Read-only and paused modes
- Admin audit logging

### Database Improvements
- Alembic migrations support
- Order event ledger for reconciliation
- Strategy state persistence

---

## Appendix: Directory Structure

```
QuantPilot-AI/
├── app.py                     # FastAPI main entry
├── requirements.txt           # Python dependencies
├── .env                       # Environment config (not in git)
├── .env.example               # Config template
├── Dockerfile                 # Docker image definition
├── docker-compose.yml         # Docker Compose orchestration
├── DEPLOY.md                  # This document
├── alembic.ini                # Alembic migration config
├── core/
│   ├── config.py              # Configuration loader (BaseModel + from_env)
│   ├── database.py            # SQLAlchemy async database layer
│   ├── security.py            # Fernet encryption + password hashing
│   ├── auth.py                # JWT authentication
│   ├── cache.py               # Redis / in-memory cache
│   ├── middleware.py          # CORS, rate limiting, CSRF middleware
│   ├── factory.py             # Application factory pattern
│   ├── lifespan.py            # Startup/shutdown lifecycle
│   ├── trading_control.py     # Global trading control/kill-switch
│   └── totp.py                # Two-factor authentication
├── routers/
│   ├── webhook.py             # TradingView webhook handler
│   ├── auth.py                # Login/register endpoints
│   ├── admin.py               # Admin endpoints
│   ├── user.py                # User endpoints
│   ├── subscription.py        # Subscription management
│   ├── ai_config.py           # AI configuration endpoints
│   ├── strategies.py          # DCA/Grid strategy endpoints
│   ├── backtest.py            # Backtest endpoints
│   ├── websocket.py           # WebSocket endpoints
│   ├── social.py              # Social signal sharing
│   ├── chart.py               # Chart data endpoints
│   └── i18n.py                # Internationalization
├── strategies/
│   ├── dca.py                 # DCA strategy engine
│   └── grid.py                # Grid trading engine
├── backtest/
│   ├── engine.py              # Backtest engine
│   ├── strategies.py          # Strategy implementations
│   └ metrics.py              # Performance metrics calculator
├── services/
│   ├── signal_processor.py    # Signal processing pipeline
│   └── order_reconciler.py    # Order audit/reconciliation
├── migrations/                # Alembic migrations
├── scripts/
│   ├── check_env.py           # Pre-deployment environment checker
│   ├── start.ps1              # Windows startup script
│   ├── start.sh               # Linux/macOS startup script
│   └── docker-entrypoint.sh   # Docker entrypoint
├── static/                    # Frontend static files
├── tests/                     # Test suite
├── data/                      # Database files and backups
├── logs/                      # Application logs (rotated)
└── trade_logs/                # Trade record logs
```