# 📡 Signal Server

AI-powered crypto trading signal optimization system.

**TradingView → AI Analysis → Exchange Execution → Dashboard**

## Architecture

```
TradingView Alert (Webhook)
        ↓
   FastAPI Server
        ↓
   Pre-Filter (rule-based, instant)
        ↓
   AI Analysis (OpenAI / Claude / DeepSeek)
        ↓
   Trade Execution (Binance / OKX / Bybit / Bitget / Gate.io)
        ↓
   Telegram Notification + Dashboard
```

## Features

- 📡 Receives TradingView webhook alerts
- 🔍 Rule-based pre-filter (blocks 60-70% low-quality signals)
- 🤖 AI analysis via LLM API (OpenAI / Anthropic / DeepSeek)
- 📈 Multi-exchange support: **Binance, OKX, Bybit, Bitget, Gate.io, Coinbase**
- 📱 Real-time Telegram notifications
- 📊 **Web Dashboard** with positions, equity curve, Sharpe ratio, and more
- 📝 Complete trade logging & analytics
- 🐳 Docker one-click deployment
- 🔒 Paper trading mode for safe testing

## Dashboard

Access the dashboard at `http://your-server:8000/dashboard`

- **Dashboard** — KPI overview, equity curve, recent signals
- **Positions** — Real-time open positions & account balance
- **History** — Full trade history with AI confidence scores
- **Analytics** — Sharpe ratio, Sortino ratio, profit factor, max drawdown, win/loss distribution
- **Settings** — Configure exchange API keys, AI provider, Telegram, risk management

## Quick Start

```bash
# Clone
git clone https://github.com/ikun52012/signal-server.git
cd signal-server

# Configure
cp .env.example .env
# Edit .env with your API keys

# Deploy
docker compose up -d

# Open dashboard
open http://localhost:8000/dashboard
```

## Supported Exchanges

| Exchange | Futures | Passphrase Required |
|----------|---------|-------------------|
| Binance  | ✅ | No |
| OKX      | ✅ | Yes |
| Bybit    | ✅ | No |
| Bitget   | ✅ | Yes |
| Gate.io  | ✅ | No |
| Coinbase | ❌ | No |

## API Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/dashboard` | GET | Web dashboard |
| `/webhook` | POST | TradingView webhook receiver |
| `/api/positions` | GET | Open positions |
| `/api/performance` | GET | Performance metrics (Sharpe, Sortino, etc.) |
| `/api/daily-pnl` | GET | Daily P&L for charting |
| `/api/history` | GET | Trade history |
| `/api/settings/*` | POST | Update settings via dashboard |
| `/balance` | GET | Account balance |
| `/stats` | GET | Today's statistics |

## License

MIT
