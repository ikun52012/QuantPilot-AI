# 🐉 Signal Server

AI-powered crypto trading signal optimization system.

**TradingView → AI Analysis → Exchange Execution**

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
   Trade Execution (Binance)
        ↓
   Telegram Notification + Logging
```

## Features

- 📡 Receives TradingView webhook alerts
- 🔍 Rule-based pre-filter (blocks 60-70% low-quality signals)
- 🤖 AI analysis via LLM API (OpenAI / Anthropic / DeepSeek)
- 📈 Auto-executes trades on Binance (futures)
- 📱 Real-time Telegram notifications
- 📝 Complete trade logging & statistics
- 🐳 Docker one-click deployment
- 🔒 Paper trading mode for safe testing

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

# Check status
curl http://localhost:8000/health
```

## Configuration

Copy `.env.example` to `.env` and fill in:

- **AI Provider**: Choose between OpenAI, Anthropic, or DeepSeek
- **Exchange**: Binance API keys
- **Telegram**: Bot token and chat ID (optional)
- **Risk Management**: Daily trade limits, max loss, position sizing

## API Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/` | GET | Server status |
| `/health` | GET | Health check |
| `/webhook` | POST | TradingView webhook receiver |
| `/stats` | GET | Today's trading statistics |
| `/trades` | GET | Today's trade log |
| `/balance` | GET | Account balance |
| `/test-signal` | POST | Send test signal |

## TradingView Setup

See `tradingview_alert_template.txt` for webhook alert JSON templates.

> ⚠️ TradingView Webhook requires Pro subscription or higher.

## License

MIT
